import argparse
import base64
import csv
import json
import logging
import os
import random
import re
import string
import sys
import time
from collections import Counter
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
import selenium.webdriver.support.expected_conditions as EC
import wget
import yt_dlp
from selenium.common import TimeoutException
from selenium.common.exceptions import InvalidSessionIdException
from selenium.webdriver.remote.webdriver import By
from selenium.webdriver.support.wait import WebDriverWait
from seleniumbase import Driver

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = ("mp4", "mkv", "webm", "mov", "avi", "m4v")

BATCH_FILE_FIELDS = ("url", "email", "password", "login_url")

# Saved in each course folder: where every downloaded lecture's files are, by lecture ID
COURSE_INDEX_FILENAME = "course.json"

# Elements that download_videos_from_links() downloads from on a lecture page
LECTURE_CONTENT_SELECTOR = (
    "[class*='lecture-attachment-type-'], iframe[data-testid^='embed-player']"
)

# The video player iframe, without an axis so it can be prefixed with // or descendant-or-self::
EMBED_PLAYER_XPATH = "iframe[starts-with(@data-testid, 'embed-player')]"

LOGIN_ERROR_SELECTOR = "div.toast, span.text-with-icon"

# Characters that are not allowed (or awkward) in file and folder names
FILENAME_REPLACEMENTS = str.maketrans({**dict.fromkeys('\n :/|?<>"\\', "-"), "*": ""})


def create_folder(course_title):
    course_path = os.path.join(os.getcwd(), "courses", course_title)
    os.makedirs(course_path, exist_ok=True)
    return course_path


def clean_string(data):
    logger.debug(f"Cleaning string: {data}")
    # Remove all non-ASCII characters (including emojis) and non-printable ones
    data = data.encode("ascii", "ignore").decode("ascii")
    data = "".join(char for char in data if char in string.printable)
    return data.translate(FILENAME_REPLACEMENTS)


def truncate_title_to_fit_filename(title, max_filename_length=250):
    # the file name length should not be too long
    # truncate the title to accommodate the max used file extension length and lecture index prefix
    max_title_length = max_filename_length - len(".mp4.part-Frag0000.part") - 3
    if len(title) > max_title_length:
        title = title[:max_title_length]
        logger.warning(f"Truncating title: {title}")
    return title


def lecture_name(video_index, title):
    # Base name shared by a lecture's html snapshot, videos and attachment folder
    return f"{video_index:02d}-{title}"


def widget_video_title(title, widget_index, total_widgets):
    # Append -n to the video title if the lecture has multiple videos
    return f"{title}-{widget_index + 1}" if total_widgets > 1 else title


def find_existing_video(output_path, name):
    # The extension isn't known until download, so match on the name with video extensions only:
    # ".*" would also match the lecture's own .html snapshot (same base name)
    for ext in VIDEO_EXTENSIONS:
        path = os.path.join(output_path, f"{name}.{ext}")
        if os.path.isfile(path):
            return path
    return None


def skip_existing(path, label):
    if os.path.isfile(path):
        logger.info(f"Skipping existing {label}: {os.path.basename(path)}")
        return True
    return False


def write_file(path, content):
    # Write to a temporary file first so an interrupted write never leaves a partial file
    # at path, which the existence checks would then skip
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part_path = f"{path}.part"
    with open(part_path, "wb") as f:
        f.write(content)
    os.replace(part_path, path)


def is_placeholder(src):
    return not src or src.startswith("data:")


def image_download_target(image_index, loader_src, src):
    """
    Works out how to save a lecture content image. Returns (filename, url, base64_data), where
    either url or base64_data is set, or (None, None, None) if there is nothing to save.
    Shared by download_content_image() and is_lecture_already_saved() so their names match.
    """
    image_link = next((s for s in (loader_src, src) if not is_placeholder(s)), None)
    if image_link:
        filename = os.path.basename(urlparse(unquote(image_link)).path)
        return filename or f"image-{image_index}.png", image_link, None

    # No real URL, but Teachable sometimes inlines the full image as a data: URI
    # (not just a blurred preview), so decode and save that instead
    data_uri = loader_src if loader_src.startswith("data:") else src
    match = re.match(
        r"data:image/([a-zA-Z0-9.+-]+);base64,(.+)", data_uri or "", re.DOTALL
    )
    if not match:
        return None, None, None
    ext = match.group(1).replace("jpeg", "jpg")
    return f"image-{image_index}.{ext}", None, match.group(2)


def audio_attachment_filename(name, href):
    return (name or "").strip() or os.path.basename(urlparse(href).path)


def chapter_folder_name(chapter_idx, chapter_title):
    return f"{chapter_idx:02d}-{chapter_title}"


def chapter_matches(video, query):
    # A number picks the chapter by position; text matches part of the folder name,
    # cleaned the same way so "Speaking Tips" matches "03-Speaking-Tips"
    if query.isdigit():
        return video["chapter_idx"] == int(query)
    chapter = os.path.basename(video["chapter_path"])
    return clean_string(query).lower() in chapter.lower()


def lecture_id(link):
    # Lecture URLs end in /lectures/<id>, which stays the same when the course is reordered
    path = urlparse(link or "").path
    match = re.search(r"/lectures/(\d+)", path)
    return match.group(1) if match else path


def lecture_files(folder, name):
    # A lecture's html snapshot, videos (name[-n].ext), subtitles (name[-n].lang.ext)
    # and attachment folder (name)
    if not os.path.isdir(folder):
        return []
    pattern = re.compile(rf"{re.escape(name)}((-\d+)?(\.[^.]+)?\.[^.]+)?")
    return [f for f in os.listdir(folder) if pattern.fullmatch(f)]


def find_saved_lecture_idx(folder, title, idx, claimed):
    # For lectures saved before the course index existed: the number of an unclaimed
    # snapshot with this title, preferring the current one
    if not os.path.isdir(folder):
        return None
    pattern = re.compile(rf"(\d+)-{re.escape(title)}\.html")
    numbers = [
        int(match.group(1))
        for f in os.listdir(folder)
        if (match := pattern.fullmatch(f))
        and (folder, f.removesuffix(".html")) not in claimed
    ]
    return min(numbers, key=lambda n: (n != idx, n)) if numbers else None


class TeachableDownloader:
    def __init__(
        self,
        verbose_arg=False,
        complete_lecture_arg=False,
        user_agent_arg="",
        origin_arg="",
        referer_arg="",
        timeout_arg=10,
        start_chapter_arg=None,
        only_chapter_arg=None,
        renumber_only_arg=False,
    ):
        # external_pdf: download PDFs instead of opening them in Chrome's viewer
        self.driver = Driver(uc=True, headed=True, external_pdf=True)
        # Selenium commands have no timeout by default, so a frozen browser would hang the
        # script forever; bound each command so it raises instead
        try:
            self.driver.command_executor.client_config.timeout = 120
        except Exception as e:
            logger.debug(f"Could not set a command executor timeout: {e}")
        self.headers = {
            "User-Agent": user_agent_arg,
            "Origin": origin_arg or "https://player.hotmart.com",
            "Referer": referer_arg or "https://player.hotmart.com",
        }
        self.verbose = verbose_arg
        self._complete_lecture = complete_lecture_arg
        self.global_timeout = timeout_arg
        self.start_chapter = start_chapter_arg
        self.only_chapter = only_chapter_arg
        self.renumber_only = renumber_only_arg
        # Chapter folders already given to a chapter of the current course
        self.used_chapter_paths = set()
        self.course_index = {}
        self.course_index_path = None

    # --- Helpers ---

    def wait_for(self, by, selector, timeout=None):
        return WebDriverWait(self.driver, timeout or self.global_timeout).until(
            EC.presence_of_element_located((by, selector))
        )

    def is_dead_session_error(self, e):
        return (
            isinstance(e, InvalidSessionIdException)
            or "invalid session id" in str(e).lower()
        )

    def try_step(self, action, func, *args, level=logging.WARNING):
        # Run one step, logging a failure instead of aborting the rest (unless the browser
        # session itself is gone). Returns func's result, or False if it raised.
        try:
            return func(*args)
        except Exception as e:
            if self.is_dead_session_error(e):
                raise
            logger.log(level, f"Could not {action}: {e}", exc_info=self.verbose)
            return False

    def download_file(self, url, path, label, headers=None):
        # Returns True if the file exists afterwards
        if skip_existing(path, label):
            return True

        filename = os.path.basename(path)
        logger.info(f"Downloading {label}: {filename}")
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.warning(f"Could not download {label} {filename}: {e}")
            return False
        if not response.ok:
            logger.warning(
                f"Failed to download {label} {filename} (HTTP {response.status_code}): {url}"
            )
            return False

        write_file(path, response.content)
        return True

    def save_page_html(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.driver.page_source)
        logger.info(f"Saved webpage as html: {path}")

    # --- Cloudflare ---

    def bypass_cloudflare_if_present(self):
        if self.driver.find_elements(By.ID, "challenge-stage"):
            self.bypass_cloudflare()
        else:
            logger.debug("No need to bypass cloudflare")

    def bypass_cloudflare(self):
        if int(self.driver.capabilities["browserVersion"].split(".")[0]) < 115:
            return
        logger.info("Bypassing cloudflare")
        time.sleep(1)

        try:
            # make sure the challenge is focused
            self.driver.find_element(By.ID, "challenge-stage").click()
            # open page in new tab
            self.driver.execute_script(
                f'window.open("{self.driver.current_url}","_blank");'
            )
            input(
                "\033[93mWarning: Bypassing Cloudflare\nplease click on the captcha checkbox if not done already "
                "and press enter to continue (do not close any of the tabs)\033[0m"
            )
            # close the first tab and switch to the new one
            self.driver.switch_to.window(window_name=self.driver.window_handles[0])
            self.driver.close()
            self.driver.switch_to.window(window_name=self.driver.window_handles[0])
        except Exception as e:
            logger.error(f"Could not bypass cloudflare: {e}")

    # --- Login ---

    def construct_sign_in_url(self, course_url):
        parsed_url = urlparse(course_url)
        return urlunparse(
            (parsed_url.scheme, parsed_url.netloc, "/sign_in", "", "", "")
        )

    def go_to_login_page(self, course_url):
        logger.info("Trying to find login")
        self.driver.get(course_url)

        # Step 1: course site -> Login link (falls back to constructed sign-in URL)
        try:
            self.follow_link(By.LINK_TEXT, "Login")
        except TimeoutException:
            logger.warning("Login link not found, navigating to fallback URL")
            self.driver.get(self.construct_sign_in_url(course_url))

        # Step 2: Teachable SSO lands on the OTP form first; switch to the password form
        self.switch_to_password_login()

    def switch_to_password_login(self):
        # Step 1 redirects through sso.teachable.com, so wait for it to land on a login form
        try:
            WebDriverWait(self.driver, self.global_timeout).until(
                EC.url_matches(r"/identity/login/(otp|password)")
            )
        except TimeoutException:
            logger.info("Not on an SSO login page - no action required")
            return

        parsed_url = urlparse(self.driver.current_url)
        # Pattern: /secure/{schoolID}/identity/login/otp
        match = re.search(r"(/secure/\d+/identity/login)/otp", parsed_url.path)
        if not match:
            logger.info("Already on the password login page")
            return

        try:
            self.follow_link(By.ID, "login-with-password-link")
            return
        except TimeoutException:
            logger.warning(
                "'Log in with a password' link not found - constructing password login URL"
            )

        password_login_url = urlunparse(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                f"{match.group(1)}/password",
                "",
                "force=true",
                "",
            )
        )
        logger.info(f"Switching from OTP to password login: {password_login_url}")
        self.driver.get(password_login_url)

    def follow_link(self, by, selector, timeout=None):
        element = self.wait_for(by, selector, timeout)
        href = element.get_attribute("href")
        if href and not href.endswith("#"):
            self.driver.get(href)
        else:
            element.click()

    def login(self, email, password):
        logger.info("Logging in")
        self.bypass_cloudflare_if_present()

        email_element = self.wait_for(By.ID, "email")
        password_element = self.wait_for(By.ID, "password")
        commit_element = self.wait_for(By.NAME, "commit")

        logger.debug("Filling in login form")
        email_element.click()
        email_element.clear()
        # Type with send_keys (real keyboard events) rather than setting .value with JS,
        # since risk-based captchas score whether the submission looks human
        self.type_like_human(email_element, email)
        time.sleep(random.uniform(0.3, 0.8))

        password_element.click()
        password_element.clear()
        self.type_like_human(password_element, password)
        time.sleep(random.uniform(0.4, 1.0))

        commit_element.click()

        # Wait for whichever outcome comes first: redirect away from login, error toast, or new device challenge
        logger.debug("Waiting for login result")
        WebDriverWait(self.driver, self.global_timeout).until(
            EC.any_of(
                EC.none_of(EC.url_contains("/identity/login")),
                EC.presence_of_element_located((By.CSS_SELECTOR, LOGIN_ERROR_SELECTOR)),
                EC.presence_of_element_located((By.NAME, "otp_code")),
            )
        )

        # Check for login error due to incorrect credentials
        for element in self.driver.find_elements(By.CSS_SELECTOR, LOGIN_ERROR_SELECTOR):
            if "Your email or password is incorrect" in element.text:
                raise RuntimeError("Incorrect email or password")

        # Check for new device challenge
        if self.driver.find_elements(By.NAME, "otp_code"):
            # wait for user to enter code
            input(
                "\033[93mWarning: New device challenge\nplease enter the code sent to your email and press enter to "
                "continue\033[0m"
            )
        logger.info("Logged in, switching to course page")

    def type_like_human(self, element, text):
        for char in text:
            element.send_keys(char)
            time.sleep(random.uniform(0.03, 0.12))

    def logged_out_reason(self):
        # A sign-in form (email + password together, not just a stray "email"
        # input elsewhere on the page, e.g. a newsletter signup), a login/OTP
        # URL, a "Login" link on the course page, or the "content locked"
        # message all mean we're not authenticated yet. Returns why, or None
        # if none of that applies.
        try:
            if self.driver.find_elements(By.ID, "email") and self.driver.find_elements(
                By.ID, "password"
            ):
                return "sign-in form (email + password fields) present"
            if re.search(r"/sign_in|/identity/login", self.driver.current_url):
                return "current URL looks like a sign-in/OTP page"
            if self.driver.find_elements(By.LINK_TEXT, "Login"):
                return "'Login' link present on the page"
            if "content locked" in self.driver.page_source.lower():
                return "'content locked' message present"
        except Exception as e:
            logger.warning(f"logged_out_reason: could not inspect page: {e}")
            return f"could not read browser state ({e})"
        return None

    def wait_for_manual_login(self, start_url):
        logger.info("Manual login mode: waiting for you to log in.")
        if self.driver.current_url != start_url:
            self.driver.get(start_url)

        print(
            "\033[93mManual login mode: please log in yourself in the browser window (enter your email/password "
            "and solve the captcha if one appears). The download will start automatically once you're logged "
            "in.\033[0m"
        )

        poll_count = 0
        while reason := self.logged_out_reason():
            poll_count += 1
            if poll_count % 5 == 0:
                logger.info(
                    f"Still waiting ({reason}); current url: {self.driver.current_url}"
                )
            time.sleep(3)

        # logged_out_reason() can return None a moment before the browser
        # finishes redirecting to the final course page, so wait for the URL
        # to stop changing, then force a clean reload - otherwise the
        # downloader can grab elements from a DOM that's still mid-navigation
        # and hit stale element errors.
        previous_url = None
        current_url = self.driver.current_url
        for _ in range(self.global_timeout * 2):
            if current_url == previous_url:
                break
            previous_url = current_url
            time.sleep(0.5)
            current_url = self.driver.current_url

        logger.info(
            "Manual login detected, reloading course page before starting download."
        )
        self.driver.get(start_url)
        self.wait_for(By.TAG_NAME, "body")

    def login_if_needed(self, course_url, email, password, login_url):
        self.driver.get(course_url)
        self.bypass_cloudflare_if_present()

        if not self.logged_out_reason():
            logger.info("Already logged in, skipping login")
            return

        if not email or not password:
            raise ValueError("no email/password given for this course")

        if login_url:
            self.driver.get(login_url)
        else:
            self.go_to_login_page(course_url)
        self.login(email, password)

    def log_in(self, course_url, email, password, login_url, manual_login):
        if manual_login:
            self.wait_for_manual_login(course_url)
        else:
            self.login_if_needed(course_url, email, password, login_url)

    # --- Entry points ---

    def run(self, course_url, email, password, login_url, manual_login=False):
        logger.info("Starting login")
        if (
            self.try_step(
                "login",
                self.log_in,
                course_url,
                email,
                password,
                login_url,
                manual_login,
                level=logging.ERROR,
            )
            is False
        ):
            return
        self.download_course(course_url)

    def run_batch(self, entries, email, password, login_url, manual_login=False):
        """
        This method handles batch downloading of courses. Courses are grouped by site and account, and each
        group is logged in to once before its courses are downloaded.

        :param entries: List[dict]
            Course entries from read_batch_file(), each with "url", "email", "password" and "login_url" keys.
            Missing values fall back to the email, password and login_url arguments below.
        :param email: str
            The default email address used to log in to the platform.
        :param password: str
            The default password associated with the provided email address.
        :param login_url: str
            The default URL of the login page. If not provided, it is found from each group's first course URL.
        :param manual_login: bool
            If True, the user logs in themselves in the browser window for each group and the download
            starts automatically once they are logged in.
        :return: None
        """
        # Group courses by (site, account), keeping the order they first appear in
        groups = {}
        for entry in entries:
            entry_email = entry["email"] or email
            group = groups.setdefault(
                (urlparse(entry["url"]).netloc, entry_email),
                {
                    "password": entry["password"] or password,
                    "login_url": entry["login_url"] or login_url,
                    "urls": [],
                },
            )
            group["urls"].append(entry["url"])

        current_email = None
        for (domain, group_email), group in groups.items():
            urls = group["urls"]
            logger.info(f"Starting login for {domain} ({len(urls)} course(s))")

            # Every school signs in through sso.teachable.com, so the previous account's session
            # would carry over; clear cookies for all domains when switching accounts
            if current_email is not None and group_email != current_email:
                logger.info("Switching account, clearing cookies")
                self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            current_email = group_email

            if (
                self.try_step(
                    f"login to {domain}",
                    self.log_in,
                    urls[0],
                    group_email,
                    group["password"],
                    group["login_url"],
                    manual_login,
                    level=logging.ERROR,
                )
                is False
            ):
                continue

            logger.info(f"Running batch download of courses for {domain}")
            for url in urls:
                self.download_course(url)

    def download_course(self, course_url):
        logger.info(f"Starting download of course: {course_url}")
        self.try_step(
            f"download course {course_url}",
            self.pick_course_downloader,
            course_url,
            level=logging.ERROR,
        )

    # --- Course pages ---

    def pick_course_downloader(self, course_url):
        # Check if we are already on the course page
        if self.driver.current_url != course_url:
            logger.info("Switching to course page")
            self.driver.get(course_url)
            self.bypass_cloudflare_if_present()

        self.wait_for(By.TAG_NAME, "body")

        # https://support.teachable.com/hc/en-us/articles/360058715732-Course-Design-Templates
        logger.info("Picking course downloader")
        if self.driver.find_elements(By.ID, "__next"):
            logger.info("Choosing __next format")
            self.download_course_simple(course_url)
        elif self.driver.find_elements(By.CLASS_NAME, "course-mainbar"):
            logger.info("Choosing course-mainbar format")
            self.download_course_classic(course_url)
        elif self.driver.find_elements(By.CSS_SELECTOR, ".block__curriculum"):
            logger.info("Choosing .block__curriculum format")
            self.download_course_colossal(course_url)
        else:
            logger.error(
                "Downloader does not support this course template. Please open an issue on github."
            )

    def prepare_course(self, course_url, title_by, title_selector):
        self.used_chapter_paths = set()
        course_path = create_folder(
            self.get_course_title(course_url, title_by, title_selector)
        )
        self.load_course_index(course_path)
        if not self.renumber_only:
            self.try_step(
                "save course html",
                self.save_page_html,
                os.path.join(course_path, "course.html"),
                level=logging.ERROR,
            )
        return course_path

    # --- Course index ---

    def load_course_index(self, course_path):
        self.course_index_path = os.path.join(course_path, COURSE_INDEX_FILENAME)
        try:
            with open(self.course_index_path, encoding="utf-8") as f:
                self.course_index = json.load(f)["lectures"]
        except FileNotFoundError:
            self.course_index = {}
        except (OSError, ValueError, KeyError) as e:
            logger.warning(
                f"Could not read {COURSE_INDEX_FILENAME}, starting a new one: {e}"
            )
            self.course_index = {}

    def save_course_index(self):
        content = json.dumps({"lectures": self.course_index}, indent=2)
        write_file(self.course_index_path, content.encode("utf-8"))

    def record_lecture(self, video):
        self.course_index[video["id"]] = {
            "chapter": os.path.basename(video["download_path"]),
            "idx": video["idx"],
            "title": video["title"],
        }

    def indexed_chapter_folder(self, course_path, links):
        # The folder holding most of this chapter's saved lectures, if any
        folders = [
            path
            for link in links
            if (entry := self.course_index.get(lecture_id(link)))
            and os.path.isdir(path := os.path.join(course_path, entry["chapter"]))
            and path not in self.used_chapter_paths
        ]
        return Counter(folders).most_common(1)[0][0] if folders else None

    def rename_indexed_chapter(self, old_path, new_path):
        old, new = os.path.basename(old_path), os.path.basename(new_path)
        for entry in self.course_index.values():
            if entry["chapter"] == old:
                entry["chapter"] = new

    def place_lectures(self, video_list):
        # Points each lecture at its saved files, or with --renumber-only moves them to
        # the lecture's current position
        course_path = os.path.dirname(self.course_index_path)
        claimed = set()
        placed = {}

        # Lectures in the course index keep the files they were saved to
        for i, video in enumerate(video_list):
            if entry := self.course_index.get(video["id"]):
                folder = os.path.join(course_path, entry["chapter"])
                key = (folder, lecture_name(entry["idx"], entry["title"]))
                if key not in claimed:
                    claimed.add(key)
                    placed[i] = (folder, entry["idx"], entry["title"])

        # Others reuse a snapshot saved before the index existed, or the next free number
        for i, video in enumerate(video_list):
            if i in placed:
                continue
            folder, title = video["chapter_path"], video["title"]
            idx = find_saved_lecture_idx(folder, title, video["idx"], claimed)
            idx = idx or video["idx"]
            while (folder, lecture_name(idx, title)) in claimed:
                idx += 1
            claimed.add((folder, lecture_name(idx, title)))
            placed[i] = (folder, idx, title)

        for i, video in enumerate(video_list):
            target = (video["chapter_path"], video["idx"], video["title"])
            video["download_path"], video["idx"], video["title"] = placed[i]
            if not self.renumber_only:
                continue
            if not lecture_files(
                video["download_path"], lecture_name(video["idx"], video["title"])
            ):
                # Nothing saved yet, so it gets its current position next time
                self.course_index.pop(video["id"], None)
                continue
            if placed[i] != target:
                self.move_lecture(video, *target)
            self.record_lecture(video)

        if self.renumber_only:
            self.save_course_index()
        return video_list

    def move_lecture(self, video, folder, idx, title):
        old_folder = video["download_path"]
        old_name = lecture_name(video["idx"], video["title"])
        new_name = lecture_name(idx, title)
        files = lecture_files(old_folder, old_name)
        if not files:
            return
        if any(
            os.path.exists(os.path.join(folder, new_name + f[len(old_name) :]))
            for f in files
        ):
            logger.warning(f"Cannot renumber {old_name}: {new_name} already exists")
            return

        logger.info(
            f"Renumbering lecture: {os.path.basename(old_folder)}/{old_name} -> "
            f"{os.path.basename(folder)}/{new_name}"
        )
        os.makedirs(folder, exist_ok=True)
        for f in files:
            os.rename(
                os.path.join(old_folder, f),
                os.path.join(folder, new_name + f[len(old_name) :]),
            )
        video["download_path"], video["idx"], video["title"] = folder, idx, title

    # --- Chapters ---

    def wait_for_sections(self, selector):
        try:
            return WebDriverWait(self.driver, self.global_timeout).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
            )
        except TimeoutException:
            logger.warning(f"No chapters found ({selector})")
            return []

    def resolve_chapter_folder(self, course_path, chapter_idx, chapter_title):
        # If the course's chapter order changed since the last run, reuse the folder with
        # the same title under its old number instead of starting a new one
        fresh_path = os.path.join(
            course_path, chapter_folder_name(chapter_idx, chapter_title)
        )
        if os.path.isdir(fresh_path) or not os.path.isdir(course_path):
            return fresh_path

        for existing in sorted(os.listdir(course_path)):
            path = os.path.join(course_path, existing)
            if (
                os.path.isdir(path)
                and re.sub(r"^\d+-", "", existing) == chapter_title
                # Chapters can share a title; give each its own folder
                and path not in self.used_chapter_paths
            ):
                logger.debug(f"Reusing chapter folder {existing} for {chapter_title}")
                return path
        return fresh_path

    def renumber_chapter_folder(self, download_path, chapter_idx, chapter_title):
        # Returns the folder's path after renaming it to the chapter's current number
        correct_path = os.path.join(
            os.path.dirname(download_path),
            chapter_folder_name(chapter_idx, chapter_title),
        )
        if download_path == correct_path or not os.path.isdir(download_path):
            return download_path
        if os.path.exists(correct_path):
            logger.warning(
                f"Cannot renumber {os.path.basename(download_path)}: {os.path.basename(correct_path)} already exists"
            )
            return download_path
        logger.info(
            f"Renumbering chapter folder: {os.path.basename(download_path)} -> {os.path.basename(correct_path)}"
        )
        os.rename(download_path, correct_path)
        return correct_path

    def add_chapter(
        self, video_list, course_path, chapter_idx, chapter_title, lectures
    ):
        # Lists lectures at their current position (place_lectures() then points them at
        # saved files); folders are only created once a lecture is downloaded
        chapter_title = clean_string(chapter_title)
        chapter_path = self.indexed_chapter_folder(
            course_path, [link for link, _ in lectures]
        ) or self.resolve_chapter_folder(course_path, chapter_idx, chapter_title)
        if self.renumber_only:
            old_path = chapter_path
            chapter_path = self.renumber_chapter_folder(
                chapter_path, chapter_idx, chapter_title
            )
            self.rename_indexed_chapter(old_path, chapter_path)
        self.used_chapter_paths.add(chapter_path)
        logger.info(f"Found chapter: {os.path.basename(chapter_path)}")

        for idx, (link, title) in enumerate(lectures, start=1):
            title = truncate_title_to_fit_filename(clean_string(title))
            logger.info(f"Found lecture: {title}")
            video_list.append(
                {
                    "link": link,
                    "id": lecture_id(link),
                    "title": title,
                    "chapter_path": chapter_path,
                    "download_path": chapter_path,
                    "chapter_idx": chapter_idx,
                    "idx": idx,
                }
            )

    def download_course_colossal(self, course_url):
        logger.info("Detected block course format")
        course_path = self.prepare_course(course_url, By.CSS_SELECTOR, ".course__title")

        # Unhide all elements
        logger.info("Unhiding all elements")
        self.driver.execute_script(
            '[...document.querySelectorAll(".hidden")].forEach(e => e.classList.remove("hidden"))'
        )

        video_list = []
        sections = self.wait_for_sections(".block__curriculum__section")
        for chapter_idx, section in enumerate(sections, start=1):
            chapter_title = section.find_element(
                By.CSS_SELECTOR, ".block__curriculum__section__title"
            ).text
            lectures = [
                (
                    item.get_attribute("href"),
                    item.find_element(
                        By.CSS_SELECTOR,
                        ".block__curriculum__section__list__item__lecture-name",
                    ).text,
                )
                for item in section.find_elements(
                    By.CSS_SELECTOR, ".block__curriculum__section__list__item__link"
                )
            ]
            self.add_chapter(
                video_list, course_path, chapter_idx, chapter_title, lectures
            )

        self.download_videos_from_links(video_list)

    def download_course_classic(self, course_url):
        logger.info("Detected _mainbar course format")
        course_path = self.prepare_course(
            course_url,
            By.CSS_SELECTOR,
            "body > section > div.course-sidebar > div > h2",
        )
        self.get_course_image(course_path, By.CLASS_NAME, "course-image")

        video_list = []
        sections = self.wait_for_sections(".course-section")
        for chapter_idx, section in enumerate(sections, start=1):
            chapter_title = section.find_element(By.CSS_SELECTOR, ".section-title").text
            lectures = [
                (
                    item.find_element(By.CLASS_NAME, "item").get_attribute("href"),
                    item.find_element(By.CLASS_NAME, "lecture-name").text,
                )
                for item in section.find_elements(By.CSS_SELECTOR, ".section-item")
            ]
            self.add_chapter(
                video_list, course_path, chapter_idx, chapter_title, lectures
            )

        self.download_videos_from_links(video_list)

    def download_course_simple(self, course_url):
        logger.info("Detected next course format")
        course_path = self.prepare_course(course_url, By.CSS_SELECTOR, ".heading")
        self.get_course_image(
            course_path, By.XPATH, '//*[@id="__next"]/div/div/div[2]/div/div[1]/img'
        )

        video_list = []
        sections = self.wait_for_sections(".slim-section")
        for chapter_idx, section in enumerate(sections, start=1):
            chapter_title = section.find_element(By.CSS_SELECTOR, ".heading").text
            # A drip tag means the chapter isn't released yet
            if section.find_elements(By.CSS_SELECTOR, ".drip-tag"):
                logger.warning(f'Chapter "{chapter_title}" not available, skipping')
                continue

            lectures = []
            for bar in section.find_elements(By.CSS_SELECTOR, ".bar"):
                video = bar.find_element(By.CSS_SELECTOR, ".text")
                lectures.append((video.get_attribute("href"), video.text))
            self.add_chapter(
                video_list, course_path, chapter_idx, chapter_title, lectures
            )

        self.download_videos_from_links(video_list)

    def get_course_title(self, course_url, by, selector):
        if self.driver.current_url != course_url:
            self.driver.get(course_url)
        try:
            logger.info("Getting course title")
            course_title = self.wait_for(by, selector).text
        except Exception:
            logger.warning("Could not get course title, using tab title instead")
            course_title = self.driver.title
        return clean_string(course_title)

    def get_course_image(self, course_path, by, selector):
        if self.renumber_only:
            return
        image_elements = self.driver.find_elements(by, selector)
        if not image_elements:
            logger.warning("No course image element found")
            return

        logger.info("Found course image")
        image_path = os.path.join(course_path, "course-image.jpg")
        image_src = image_elements[0].get_attribute("src")
        # Try the high-definition version first, falling back to the original link
        image_src_hd = re.sub(r"/resize=.+?/", "/", image_src)
        if not self.download_file(image_src_hd, image_path, "course image"):
            self.download_file(image_src, image_path, "course image")

    # --- Lectures ---

    def is_lecture_already_saved(self, video):
        title = video["title"]
        output_path = video["download_path"]
        name = lecture_name(video["idx"], title)

        html_path = os.path.join(output_path, f"{name}.html")
        if not os.path.exists(html_path):
            return False
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.debug(f"Could not read saved lecture html: {html_path} cause: {e}")
            return False
        if "content locked" in content.lower():
            return False
        body_match = re.search(
            r"<body[^>]*>(.*)</body>", content, re.DOTALL | re.IGNORECASE
        )
        if (
            not body_match
            or len(re.sub(r"<[^>]+>", "", body_match.group(1)).strip()) < 20
        ):
            logger.debug(
                f"{title}: saved html body is empty/near-empty, treating as incomplete"
            )
            return False

        def missing(label, filename):
            logger.debug(f"{title}: missing expected {label} {filename}, will re-check")
            return False

        # Videos: one per video widget (or player iframe, see find_video_widgets), named
        # the same way download_video_widgets does
        video_count = len(re.findall(r"lecture-attachment-type-video", content)) or len(
            re.findall(r'data-testid="embed-player', content)
        )
        for i in range(video_count):
            expected = lecture_name(
                video["idx"], widget_video_title(title, i, video_count)
            )
            if not find_existing_video(output_path, expected):
                return missing("video", expected)

        attachment_path = os.path.join(output_path, name)

        # File-type attachments (downloaded via wget). A lecture can have
        # more than one separate file-attachment block.
        for file_block in re.finditer(
            r'lecture-attachment-type-file["\'][^>]*>(.*?)</div>\s*</div>',
            content,
            re.DOTALL,
        ):
            for href in re.findall(r'href="([^"]+)"', file_block.group(1)):
                filename = os.path.basename(urlparse(href).path)
                if filename and not os.path.isfile(
                    os.path.join(attachment_path, filename)
                ):
                    return missing("attachment", filename)

        # Audio attachments (the mp3 player widget)
        for audio_match in re.finditer(
            r'audioloader__placeholder["\'][^>]*>(.*?)</div>', content, re.DOTALL
        ):
            block = audio_match.group(1)
            href_match = re.search(r'data-href="([^"]+)"', block)
            if not href_match:
                continue
            name_match = re.search(r'audioloader__name["\'][^>]*>([^<]*)<', block)
            filename = audio_attachment_filename(
                name_match and name_match.group(1), href_match.group(1)
            )
            if filename and not os.path.isfile(os.path.join(attachment_path, filename)):
                return missing("audio attachment", filename)

        # PDF-embed attachments
        for pdf_block in re.finditer(
            r'lecture-attachment-type-pdf_embed["\'][^>]*>(.*?)</div>\s*</div>',
            content,
            re.DOTALL,
        ):
            name_match = re.search(
                r'data-x-origin-download-name="([^"]+)"', pdf_block.group(1)
            )
            if name_match and not os.path.isfile(
                os.path.join(attachment_path, name_match.group(1))
            ):
                return missing("PDF", name_match.group(1))

        # Inline content images. Number them like download_content_images does (one counter
        # from 1, in page order) so the "image-{idx}.{ext}" fallback names match.
        image_idx = 0
        for text_block in re.finditer(
            r'lecture-text-container["\'][^>]*>(.*?)</div>', content, re.DOTALL
        ):
            for img in re.findall(r"<img[^>]*>", text_block.group(1)):
                image_idx += 1
                loader_src_match = re.search(r'data-imageloader-src="([^"]+)"', img)
                src_match = re.search(r'\bsrc="([^"]+)"', img)
                filename, _, _ = image_download_target(
                    image_idx,
                    loader_src_match.group(1) if loader_src_match else "",
                    src_match.group(1) if src_match else "",
                )
                if filename and not os.path.isfile(
                    os.path.join(attachment_path, filename)
                ):
                    return missing("image", filename)

        return True

    def filter_chapters(self, video_list):
        query = self.only_chapter or self.start_chapter
        if not query:
            return video_list

        first = next(
            (i for i, v in enumerate(video_list) if chapter_matches(v, query)), None
        )
        if first is None:
            chapters = dict.fromkeys(
                os.path.basename(v["chapter_path"]) for v in video_list
            )
            logger.error(
                f"No chapter matches '{query}', nothing downloaded. Chapters: {', '.join(chapters)}"
            )
            return []

        chapter_path = video_list[first]["chapter_path"]
        if self.only_chapter:
            logger.info(f"Downloading only chapter: {os.path.basename(chapter_path)}")
            chapter_idx = video_list[first]["chapter_idx"]
            return [v for v in video_list if v["chapter_idx"] == chapter_idx]
        logger.info(f"Starting from chapter: {os.path.basename(chapter_path)}")
        return video_list[first:]

    def open_lecture(self, link, title):
        self.driver.get(link)
        # Wait for the lecture content the steps below look for (attachments or a video player)
        try:
            self.wait_for(By.CSS_SELECTOR, LECTURE_CONTENT_SELECTOR)
        except TimeoutException:
            logger.warning(f"No attachments or video found for lecture: {title}")

    def download_videos_from_links(self, video_list):
        video_list = self.place_lectures(video_list)
        if self.renumber_only:
            return

        for video in self.filter_chapters(video_list):
            link = video["link"]
            title = video["title"]
            download_path = video["download_path"]
            idx = video["idx"]

            # Recorded before downloading, so an interrupted lecture resumes in the same place
            self.record_lecture(video)
            self.try_step("save course index", self.save_course_index)

            if self.is_lecture_already_saved(video):
                logger.info(f"Skipping already saved lecture: {title}")
                continue

            # Created lazily, right before it's actually needed
            os.makedirs(download_path, exist_ok=True)

            if self.driver.current_url != link:
                logger.info(f"Navigating to lecture: {title}")
                self.open_lecture(link, title)
                self.scroll_page_to_trigger_lazy_content()

            logger.info(f"Downloading lecture: {title}")
            self.try_step(
                f"save html for {title}",
                self.save_page_html,
                os.path.join(download_path, f"{lecture_name(idx, title)}.html"),
                level=logging.ERROR,
            )
            self.try_step(
                f"download attachments for {title}",
                self.download_attachments,
                title,
                idx,
                download_path,
            )

            # Some lectures offer the video as a downloadable file; otherwise use the player
            if not self.try_step(
                f"download video file for {title}",
                self.download_video_file,
                title,
                idx,
                download_path,
                level=logging.DEBUG,
            ):
                self.download_video_widgets(link, title, idx, download_path)

            if self._complete_lecture:
                self.try_step(f"complete lecture {title}", self.complete_lecture)

            logger.info(f"Finished lecture: {title}")

    def find_video_widgets(self):
        # Most templates wrap each video in a widget that mounts its player iframe later via JS;
        # for templates without those widgets, fall back to the player iframes on the page
        widgets = self.driver.find_elements(
            By.CLASS_NAME, "lecture-attachment-type-video"
        )
        return widgets or self.driver.find_elements(By.XPATH, f"//{EMBED_PLAYER_XPATH}")

    def download_video_widgets(self, link, title, idx, download_path):
        # Iterate the widgets and wait for each one's iframe individually as each video widget
        # mounts its embed-player iframe asynchronously via JS after the page loads
        video_widgets = self.find_video_widgets()
        total_widgets = len(video_widgets)
        for i, widget in enumerate(video_widgets):
            current_widget = widget
            # Signed video links are created when the page loads and expire after a while.
            # On lectures with many videos, later links can expire (403) before we reach
            # them, so reload the page for fresh links and retry once.
            for attempt_num in range(2):
                try:
                    if attempt_num > 0:
                        logger.info(
                            f"Video {i + 1}/{total_widgets} failed, likely an expired link — reloading the page and retrying once"
                        )
                        self.open_lecture(link, title)
                        refreshed_widgets = self.find_video_widgets()
                        if i >= len(refreshed_widgets):
                            logger.warning(
                                f"Widget {i + 1}/{total_widgets} no longer found after reload; giving up on it"
                            )
                            break
                        current_widget = refreshed_widgets[i]

                    if self.download_video_widget(
                        current_widget, i, total_widgets, title, idx, download_path
                    ):
                        break
                except Exception as e:
                    if self.is_dead_session_error(e):
                        raise
                    logger.warning(f"Could not find video: {title} cause: {e}")
                finally:
                    # Reset top-level context so the next widget (or next lecture)
                    # doesn't inherit a broken frame context
                    self.driver.switch_to.default_content()

    def download_video_widget(
        self, widget, widget_index, total_widgets, title, idx, download_path
    ):
        # Later widgets can take much longer to mount their iframe, so wait well past
        # global_timeout. Scroll once, then poll: re-scrolling can reset the lazy-loader's
        # visibility timer and stop the widget from mounting at all.
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", widget
        )

        max_wait_seconds = max(self.global_timeout * 3, 30)
        try:
            # descendant-or-self: a widget may be the iframe itself (see find_video_widgets)
            frames = WebDriverWait(
                self.driver, max_wait_seconds, poll_frequency=1
            ).until(
                lambda _: widget.find_elements(
                    By.XPATH, f"descendant-or-self::{EMBED_PLAYER_XPATH}"
                )
            )
        except TimeoutException:
            raise TimeoutException(
                f"video {widget_index + 1}/{total_widgets} never mounted its player within {max_wait_seconds}s"
            ) from None

        logger.info("Switching to video frame")
        self.driver.switch_to.frame(frames[0])

        # Lazy-loaded iframes may not have rendered the video data
        # yet, so wait for it rather than failing immediately.
        script_text = self.wait_for(By.ID, "__NEXT_DATA__")
        json_text = json.loads(script_text.get_attribute("innerHTML"))
        link = json_text["props"]["pageProps"]["applicationData"]["mediaAssets"][0][
            "urlEncrypted"
        ]

        video_title = widget_video_title(title, widget_index, total_widgets)
        self.try_step(
            f"download subtitle for {video_title}",
            self.download_subtitle,
            link,
            video_title,
            idx,
            download_path,
        )
        return self.download_video(link, video_title, idx, download_path)

    def scroll_page_to_trigger_lazy_content(self):
        try:
            last_height = self.driver.execute_script(
                "return document.body.scrollHeight"
            )
            scroll_position = 0
            while scroll_position < last_height:
                scroll_position += 900
                self.driver.execute_script(f"window.scrollTo(0, {scroll_position});")
                time.sleep(0.3)
                last_height = self.driver.execute_script(
                    "return document.body.scrollHeight"
                )
            self.driver.execute_script("window.scrollTo(0, 0);")
        except Exception as e:
            logger.debug(f"Could not scroll page to trigger lazy content: {e}")

    # --- Attachments ---

    def download_attachments(self, title, video_index, output_path):
        output_path = os.path.join(output_path, lecture_name(video_index, title))
        for action, func in (
            ("download file attachments", self.download_file_attachments),
            ("download PDF attachments", self.download_pdf_attachments),
            ("download audio attachments", self.download_audio_attachments),
            ("download content images", self.download_content_images),
        ):
            self.try_step(f"{action} for {title}", func, output_path, title)

    def download_file_attachments(self, output_path, title):
        # A lecture can have more than one separate file-attachment block
        links = self.driver.find_elements(
            By.CSS_SELECTOR, ".lecture-attachment-type-file a"
        )
        if not links:
            logger.debug(f"No file attachments found for video: {title}")
            return

        for link in links:
            href = link.get_attribute("href")
            # wget names the file after the URL, not the link text, so
            # check against that to detect an already-downloaded file.
            url_filename = os.path.basename(urlparse(href).path)
            if url_filename and skip_existing(
                os.path.join(output_path, url_filename), "attachment"
            ):
                continue
            logger.info(f"Downloading attachment: {link.text} for video: {title}")
            # wget treats out as a file name unless the folder already exists
            os.makedirs(output_path, exist_ok=True)
            wget.download(href, out=output_path)

    def download_pdf_attachments(self, output_path, title):
        blocks = self.driver.find_elements(
            By.CLASS_NAME, "lecture-attachment-type-pdf_embed"
        )
        if not blocks:
            logger.debug(f"No PDF attachments found for video: {title}")
            return

        for block in blocks:
            pdf_links = block.find_elements(By.TAG_NAME, "a")
            if not pdf_links:
                continue
            pdf_filename = pdf_links[0].get_attribute("data-x-origin-download-name")
            if pdf_filename and skip_existing(
                os.path.join(output_path, pdf_filename), "PDF attachment"
            ):
                continue
            logger.info(
                f"Downloading PDF attachment: {pdf_filename} for video: {title}"
            )
            # Use the webdriver to download the PDF
            if not self.webdriver_download(
                output_path, pdf_links[0].click, pdf_filename
            ):
                logger.warning(f"Could not download PDF attachment: {pdf_filename}")

    def download_audio_attachments(self, output_path, title):
        # The mp3 player widget's file URL sits in the button's data-href, not an <a href>
        widgets = self.driver.find_elements(By.CLASS_NAME, "audioloader__placeholder")
        if not widgets:
            logger.debug(f"No audio attachments found for video: {title}")
            return

        for widget in widgets:
            self.try_step(
                f"download audio attachment for {title}",
                self.download_audio_attachment,
                widget,
                output_path,
            )

    def download_audio_attachment(self, widget, output_path):
        buttons = widget.find_elements(By.TAG_NAME, "button")
        audio_link = buttons[0].get_attribute("data-href") if buttons else None
        if not audio_link:
            return
        names = widget.find_elements(By.CLASS_NAME, "audioloader__name")
        filename = audio_attachment_filename(names[0].text if names else "", audio_link)
        self.download_file(
            audio_link,
            os.path.join(output_path, filename),
            "audio attachment",
            headers=self.headers,
        )

    def download_content_images(self, output_path, title):
        # Images embedded directly in the lecture's rich-text content
        image_elements = self.driver.find_elements(
            By.CSS_SELECTOR, ".lecture-text-container img"
        )
        if not image_elements:
            logger.debug(f"No content images found for video: {title}")
            return

        logger.debug(f"Found {len(image_elements)} content image(s)")
        for idx, image_element in enumerate(image_elements, start=1):
            self.try_step(
                f"download image {idx} for {title}",
                self.download_content_image,
                image_element,
                idx,
                output_path,
            )

    def download_content_image(self, image_element, idx, output_path):
        def image_srcs():
            return (
                image_element.get_attribute("data-imageloader-src") or "",
                image_element.get_attribute("src") or "",
            )

        def resolved_srcs(_):
            srcs = image_srcs()
            return srcs if not all(is_placeholder(s) for s in srcs) else False

        loader_src, src = image_srcs()
        # Scrolling the whole page once doesn't load every lazy image, so scroll each
        # image that still has a base64 placeholder into view and wait for its real URL.
        if is_placeholder(loader_src) and is_placeholder(src):
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", image_element
            )
            try:
                loader_src, src = WebDriverWait(
                    self.driver, 8, poll_frequency=0.5
                ).until(resolved_srcs)
                logger.debug(
                    f"Image {idx} resolved to a real URL after a dedicated wait"
                )
            except TimeoutException:
                pass

        filename, image_link, base64_data = image_download_target(idx, loader_src, src)
        if not filename:
            logger.debug(f"Skipping image {idx}: no usable source")
            return

        path = os.path.join(output_path, filename)
        if image_link:
            self.download_file(image_link, path, "image", headers=self.headers)
            return

        if skip_existing(path, "image"):
            return
        logger.info(f"Saving inline (base64) image: {filename}")
        write_file(path, base64.b64decode(base64_data))

    # --- Videos and subtitles ---

    def download_video_file(self, title, video_index, output_path, timeout=60):
        name = lecture_name(video_index, title)
        if find_existing_video(output_path, name):
            logger.info(f"Skipping existing video file: {name}")
            return True

        video_links = self.driver.find_elements(
            By.CSS_SELECTOR, ".lecture-attachment-type-video a"
        )
        if not video_links:
            logger.debug(f"No video file link found for lecture: {title}")
            return False

        downloaded_file = self.webdriver_download(
            output_path, video_links[0].click, timeout=timeout
        )
        if not downloaded_file:
            return False

        _, extension = os.path.splitext(downloaded_file)
        new_filename = f"{name}{extension}"
        os.rename(downloaded_file, os.path.join(output_path, new_filename))
        logger.info(f"Downloaded video file {new_filename}")
        return True

    def video_ydl_options(self, **options):
        return {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
                {"key": "FFmpegMetadata"},
            ],
            "http_headers": self.headers,
            "verbose": self.verbose,
            **options,
        }

    # This function is needed because yt-dlp subtitle downloader is not working
    def download_subtitle(self, link, title, video_index, output_path):
        ydl_opts = self.video_ydl_options(
            allsubtitles=True, subtitleslangs=["all"], writesubtitles=True
        )
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(link, download=False)

        for lang, sub in (info.get("requested_subtitles") or {}).items():
            path = os.path.join(
                output_path, f"{lecture_name(video_index, title)}.{lang}.{sub['ext']}"
            )
            if skip_existing(path, "subtitle"):
                continue
            # The subtitle URL points at a playlist whose sixth line is the actual file
            try:
                playlist = requests.get(sub["url"], headers=self.headers, timeout=30)
                playlist.raise_for_status()
                subtitle_url = urljoin(sub["url"], playlist.text.split("\n")[5])
            except (requests.RequestException, IndexError) as e:
                logger.warning(f"Could not download subtitle: {title} cause: {e}")
                continue
            self.download_file(subtitle_url, path, "subtitle", headers=self.headers)

    def download_video(self, link, title, video_index, output_path):
        name = lecture_name(video_index, title)
        if find_existing_video(output_path, name):
            logger.info(f"Skipping existing video: {name}")
            return True

        ydl_opts = self.video_ydl_options(
            concurrentfragments=15,
            outtmpl=os.path.join(output_path, f"{name}.mp4"),
        )
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([link])
            return True
        except Exception as e:
            logger.error(f"Could not download video: {title} cause: {e}")
            return False

    def webdriver_download(
        self, output_path, trigger_download, expected_filename=None, timeout=120
    ):
        # Returns the downloaded file's path, or None if it didn't appear within timeout
        os.makedirs(output_path, exist_ok=True)
        self.driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": output_path},
        )
        files_before_download = set(os.listdir(output_path))

        trigger_download()

        # Wait for the download to complete
        deadline = time.time() + timeout
        while time.time() < deadline:
            files_after_download = set(os.listdir(output_path))
            if expected_filename:
                if expected_filename in files_after_download:
                    logger.debug(f"Expected file {expected_filename} found.")
                    return os.path.join(output_path, expected_filename)
            else:
                new_files = files_after_download - files_before_download
                if len(new_files) == 1:
                    new_file = next(iter(new_files))
                    if not new_file.endswith(".crdownload"):
                        logger.debug(f"New file found: {new_file}")
                        return os.path.join(output_path, new_file)
            time.sleep(1)

        logger.error("Download timed out.")
        return None

    def complete_lecture(self):
        self.driver.switch_to.default_content()
        self.driver.find_element(By.ID, "lecture_complete_button").click()
        logger.info("Completed lecture")
        time.sleep(3)

    def cleanup(self):
        logger.info("Cleaning up")
        self.driver.quit()
        if os.path.exists("cookies.txt"):
            os.remove("cookies.txt")


def read_batch_file(file_path):
    """
    Reads course entries from a batch file, in one of two formats.

    Plain text, one course URL per line (credentials come from --email/--password):

        https://www.school-a.com/p/course-one
        https://www.school-a.com/p/course-two

    CSV with a header row. Only the url column is required; empty values fall back to
    --email, --password and --login_url, and login_url is found automatically if still empty.
    Quote any value that contains a comma:

        url,email,password,login_url
        https://www.school-a.com/p/course-one,me@example.com,my-password,
        https://www.school-a.com/p/course-two,me@example.com,my-password,
        https://www.school-b.com/courses/x,other@example.com,"pass,with,commas",https://sso.teachable.com/secure/1234567/identity/login/password?force=true

    In both formats, blank lines and lines starting with '#' are skipped.

    Returns a list of dicts with url, email, password and login_url keys; missing values are None.
    """
    try:
        with open(file_path, "r", newline="") as file:
            lines = [
                line
                for line in file.read().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        return []
    except OSError as e:
        logger.error(f"Could not read file: {file_path}. Error: {e}")
        return []

    if lines and lines[0].split(",")[0].strip().lower() == "url":
        rows = csv.DictReader(lines, skipinitialspace=True)
        entries = [
            {
                field: (row.get(field) or "").strip() or None
                for field in BATCH_FILE_FIELDS
            }
            for row in rows
        ]
    else:
        entries = [
            dict.fromkeys(BATCH_FILE_FIELDS) | {"url": line.strip()} for line in lines
        ]
    entries = [entry for entry in entries if entry["url"]]

    if entries:
        logger.info(f"Successfully read {len(entries)} courses from file: {file_path}")
    else:
        logger.warning(f"No courses found in file: {file_path}")

    return entries


def get_args_error(args):
    if not args.file and not args.url:
        return "A course URL (--url) or a batch file (--file) is required."
    if args.file and (args.start_chapter or args.only_chapter):
        return "--start-chapter and --only-chapter only work with a single --url."
    # A batch file can carry its own credentials per course
    if not (args.manual_login or args.file or (args.email and args.password)):
        return (
            "Required arguments are missing. Choose email/password or --manual-login."
        )
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="Teachable-Dl",
        description="Download courses",
    )
    parser.add_argument("--url", required=False, help="URL of the course")
    parser.add_argument("-e", "--email", required=False, help="Email of the account")
    parser.add_argument(
        "-p", "--password", required=False, help="Password of the account"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity level (repeat for more verbosity)",
    )
    parser.add_argument(
        "--complete-lecture",
        action="store_true",
        default=False,
        help="Complete the lecture after downloading",
    )
    parser.add_argument(
        "--login_url", required=False, help="(Optional) URL to teachable SSO login page"
    )
    parser.add_argument(
        "-ml",
        "--manual-login",
        action="store_true",
        default=False,
        help="Log in yourself in the browser (email/password and any captcha); "
        "the download starts automatically once you are logged in",
    )
    parser.add_argument(
        "-f",
        "--file",
        required=False,
        help="Path to a text file with one URL per line, or a CSV file with a "
        "url,email,password,login_url header",
    )
    parser.add_argument(
        "--user-agent",
        required=False,
        help="User agent to use when downloading videos",
        default="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/116.0.0.0 Safari/537.36",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        required=False,
        help="Timeout for selenium driver",
        type=int,
        default=10,
    )
    chapter_group = parser.add_mutually_exclusive_group()
    chapter_group.add_argument(
        "--renumber-only",
        action="store_true",
        default=False,
        help="Rename existing chapter folders to match the course's current chapter order, "
        "without downloading anything",
    )
    chapter_group.add_argument(
        "--start-chapter",
        help="Start from the first chapter matching this number or text "
        '(e.g. 3 or "Speaking Tips") and continue to the end of the course',
    )
    chapter_group.add_argument(
        "--only-chapter",
        help="Download only the first chapter matching this number or text "
        '(e.g. 3 or "Speaking Tips")',
    )
    args = parser.parse_args()

    # force=True: on some Python builds hashlib installs a root handler at startup,
    # which makes a plain basicConfig() silently do nothing
    logging.basicConfig(
        level={0: logging.WARNING, 1: logging.INFO}.get(args.verbose, logging.DEBUG),
        format="%(levelname)s: %(message)s",
        force=True,
    )

    # Validate everything before opening the browser
    if args_error := get_args_error(args):
        logger.error(args_error)
        sys.exit(1)

    entries = read_batch_file(args.file) if args.file else None
    if args.file and not entries:
        sys.exit(1)

    downloader = TeachableDownloader(
        verbose_arg=args.verbose >= 2,
        complete_lecture_arg=args.complete_lecture,
        user_agent_arg=args.user_agent,
        timeout_arg=args.timeout,
        start_chapter_arg=args.start_chapter,
        only_chapter_arg=args.only_chapter,
        renumber_only_arg=args.renumber_only,
    )
    exit_code = 0
    try:
        if entries:
            downloader.run_batch(
                entries,
                args.email,
                args.password,
                args.login_url,
                manual_login=args.manual_login,
            )
        else:
            downloader.run(
                course_url=args.url,
                email=args.email,
                password=args.password,
                login_url=args.login_url,
                manual_login=args.manual_login,
            )
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        exit_code = 1
    except Exception as e:
        if downloader.is_dead_session_error(e):
            logger.error("Browser session was lost (crashed or closed), stopping.")
        else:
            logger.error(f"Error: {e}")
        exit_code = 1
    finally:
        downloader.cleanup()
    sys.exit(exit_code)
