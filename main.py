import argparse
import csv
import json
import logging
import os
import re
import string
import sys
import time
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import selenium.webdriver.support.expected_conditions as EC
import wget
import yt_dlp
from selenium.common import TimeoutException
from selenium.webdriver.remote.webdriver import By
from selenium.webdriver.support.wait import WebDriverWait
from seleniumbase import Driver

logger = logging.getLogger(__name__)

# Elements that download_videos_from_links() downloads from on a lecture page
LECTURE_CONTENT_SELECTOR = (
    "[class*='lecture-attachment-type-'], iframe[data-testid^='embed-player']"
)


def create_folder(course_title):
    root_path = os.path.abspath(os.getcwd())
    course_path = os.path.join(root_path, "courses", course_title)
    os.makedirs(course_path, exist_ok=True)
    return course_path


def clean_string(data):
    logger.debug("Cleaning string: " + data)
    # Remove all non-ASCII characters (including emojis)
    data = data.encode("ascii", "ignore").decode("ascii")
    # Replace specific characters with "-"
    return (
        data.replace("\n", "-")
        .replace(" ", "-")
        .replace(":", "-")
        .replace("/", "-")
        .replace("|", "-")
        .replace("*", "")
        .replace("?", "-")
        .replace("<", "-")
        .replace(">", "-")
        .replace('"', "-")
        .replace("\\", "-")
    )


def truncate_title_to_fit_filename(title, max_filename_length=250):
    # the file name length should not be too long
    # truncate the title to accommodate the max used file extension length and lecture index prefix
    max_title_length = max_filename_length - len(".mp4.part-Frag0000.part") - 3
    if len(title) > max_title_length:
        truncated_title = title[:max_title_length]
        logger.warning("Truncating title: " + truncated_title)
        return truncated_title
    return title


class TeachableDownloader:
    def __init__(
        self,
        verbose_arg=False,
        complete_lecture_arg=False,
        user_agent_arg="",
        origin_arg="",
        referer_arg="",
        timeout_arg=10,
    ):
        self.driver = Driver(uc=True, headed=True)
        self.headers = {
            "User-Agent": user_agent_arg,
            "Origin": origin_arg or "https://player.hotmart.com",
            "Referer": referer_arg or "https://player.hotmart.com",
        }
        self.verbose = verbose_arg
        self._complete_lecture = complete_lecture_arg
        self.global_timeout = timeout_arg

    def bypass_cloudflare_if_present(self):
        if self.driver.find_elements(By.ID, "challenge-stage"):
            self.bypass_cloudflare()
        else:
            logger.info("No need to bypass cloudflare")

    def bypass_cloudflare(self):
        if self.driver.capabilities["browserVersion"].split(".")[0] < "115":
            return
        logger.info("Bypassing cloudflare")
        time.sleep(1)

        try:
            self.driver.find_element(
                By.ID, "challenge-stage"
            ).click()  # make sure the challenge is focused
            self.driver.execute_script(
                '''window.open("''' + self.driver.current_url + """","_blank");"""
            )  # open page in new tab
            input(
                "\033[93mWarning: Bypassing Cloudflare\nplease click on the captcha checkbox if not done already "
                "and press enter to continue (do not close any of the tabs)\033[0m"
            )
            self.driver.switch_to.window(
                window_name=self.driver.window_handles[0]
            )  # switch to first tab
            self.driver.close()  # close first tab
            self.driver.switch_to.window(
                window_name=self.driver.window_handles[0]
            )  # switch back to new tab
        except Exception as e:
            logger.error("Could not bypass cloudflare: " + str(e))
            return

    def construct_sign_in_url(self, course_url):
        parsed_url = urlparse(course_url)
        # Replace the path with '/sign_in'
        sign_in_path = "/sign_in"
        fallback_url = urlunparse(
            (parsed_url.scheme, parsed_url.netloc, sign_in_path, "", "", "")
        )
        return fallback_url

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
                match.group(1) + "/password",
                "",
                "force=true",
                "",
            )
        )
        logger.info(f"Switching from OTP to password login: {password_login_url}")
        self.driver.get(password_login_url)

    def follow_link(self, by, selector, timeout=None):
        element = WebDriverWait(self.driver, timeout or self.global_timeout).until(
            EC.presence_of_element_located((by, selector))
        )
        href = element.get_attribute("href")
        if href and not href.endswith("#"):
            self.driver.get(href)
        else:
            element.click()

    def login(self, email, password):
        logger.info("Logging in")

        self.bypass_cloudflare_if_present()

        WebDriverWait(self.driver, timeout=15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        email_element = WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.ID, "email"))
        )
        password_element = WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.ID, "password"))
        )
        commit_element = WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.NAME, "commit"))
        )

        logger.debug("Filling in login form")
        email_element.click()
        email_element.clear()
        self.driver.execute_script(
            "arguments[0].value = arguments[1]", email_element, email
        )

        password_element.click()
        password_element.clear()
        self.driver.execute_script(
            "arguments[0].value = arguments[1]", password_element, password
        )

        commit_element.click()

        # Wait for whichever outcome comes first: redirect away from login, error toast, or new device challenge
        logger.debug("Waiting for login result")
        WebDriverWait(self.driver, self.global_timeout).until(
            EC.any_of(
                EC.none_of(EC.url_contains("/identity/login")),
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.toast, span.text-with-icon")
                ),
                EC.presence_of_element_located((By.NAME, "otp_code")),
            )
        )

        # Check for login error due to incorrect credentials
        for element in self.driver.find_elements(
            By.CSS_SELECTOR, "div.toast, span.text-with-icon"
        ):
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

    def logged_out_reason(self):
        # A sign-in form (email + password together, not just a stray "email"
        # input elsewhere on the page, e.g. a newsletter signup), a login/OTP
        # URL, a "Login" link on the course page, or the "content locked"
        # message all mean we're not authenticated yet. Returns why, or None
        # if none of that applies.
        try:
            current_url = self.driver.current_url
            if self.driver.find_elements(By.ID, "email") and self.driver.find_elements(
                By.ID, "password"
            ):
                return "sign-in form (email + password fields) present"
            if re.search(r"/sign_in|/identity/login", current_url):
                return "current URL looks like a sign-in/OTP page"
            if self.driver.find_elements(By.LINK_TEXT, "Login"):
                return "'Login' link present on the page"
            if "content locked" in self.driver.page_source.lower():
                return "'content locked' message present"
        except Exception as e:
            logger.warning("logged_out_reason: could not inspect page: " + str(e))
            return "could not read browser state (" + str(e) + ")"
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
        while True:
            logger.debug(f"wait_for_manual_login: poll #{poll_count + 1}")
            reason = self.logged_out_reason()
            if not reason:
                break
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
        WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

    def run(self, course_url, email, password, login_url, manual_login=False):
        logger.info("Starting login")

        if manual_login:
            self.wait_for_manual_login(course_url)
        else:
            try:
                if login_url:
                    self.driver.get(login_url)
                else:
                    self.go_to_login_page(course_url)
                self.login(email, password)
            except Exception as e:
                logger.error("Could not login: " + str(e), exc_info=self.verbose)
                return

        logger.info("Starting download of course: " + course_url)
        try:
            self.pick_course_downloader(course_url)
        except Exception as e:
            logger.error(
                "Could not download course: " + course_url + " cause: " + str(e)
            )

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
        if not entries:
            logger.error("No courses to download")
            return

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

            try:
                if manual_login:
                    self.wait_for_manual_login(urls[0])
                else:
                    self.login_if_needed(
                        urls[0], group_email, group["password"], group["login_url"]
                    )
            except Exception as e:
                logger.error(
                    f"Could not login to {domain}: " + str(e), exc_info=self.verbose
                )
                continue

            logger.info(f"Running batch download of courses for {domain}")
            for url in urls:
                try:
                    self.pick_course_downloader(url)
                except Exception as e:
                    logger.error(
                        "Could not download course: " + url + " cause: " + str(e)
                    )

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

    def pick_course_downloader(self, course_url):
        # Check if we are already on the course page
        if self.driver.current_url != course_url:
            logger.info("Switching to course page")
            self.driver.get(course_url)
            self.bypass_cloudflare_if_present()

        WebDriverWait(self.driver, timeout=self.global_timeout).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

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
            return

    def download_course_colossal(self, course_url):
        logger.info("Detected block course format")
        course_title = self.get_course_title(
            course_url, By.CSS_SELECTOR, ".course__title"
        )
        course_path = create_folder(course_title)

        self.save_course_html(course_path)

        # Unhide all elements
        logger.info("Unhiding all elements")
        self.driver.execute_script(
            '[...document.querySelectorAll(".hidden")].map(e=>e.classList.remove("hidden"))'
        )

        chapter_idx = 1
        video_list = []
        wait = WebDriverWait(self.driver, timeout=self.global_timeout)
        sections = wait.until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, ".block__curriculum__section")
            )
        )
        for section in sections:
            chapter_title = section.find_element(
                By.CSS_SELECTOR, ".block__curriculum__section__title"
            ).text
            chapter_title = clean_string(chapter_title)
            chapter_title = f"{chapter_idx:02d}-{chapter_title}"
            logger.info("Found chapter: " + chapter_title)

            download_path = os.path.join(course_path, chapter_title)
            os.makedirs(download_path, exist_ok=True)

            chapter_idx += 1
            idx = 1

            section_items = section.find_elements(
                By.CSS_SELECTOR, ".block__curriculum__section__list__item__link"
            )
            for item in section_items:
                lecture_link = item.get_attribute("href")
                lecture_title = item.find_element(
                    By.CSS_SELECTOR,
                    ".block__curriculum__section__list__item__lecture-name",
                ).text
                lecture_title = clean_string(lecture_title)
                lecture_title = "".join(
                    char for char in lecture_title if char in string.printable
                )
                logger.info("Found lecture: " + lecture_title)

                truncated_lecture_title = truncate_title_to_fit_filename(lecture_title)
                video_entity = {
                    "link": lecture_link,
                    "title": truncated_lecture_title,
                    "download_path": download_path,
                    "idx": idx,
                }
                video_list.append(video_entity)
                idx += 1

        self.download_videos_from_links(video_list)

    def download_course_classic(self, course_url):
        logger.info("Detected _mainbar course format")
        course_title = self.get_course_title(
            course_url,
            By.CSS_SELECTOR,
            "body > section > div.course-sidebar > div > h2",
        )
        course_path = create_folder(course_title)

        self.save_course_html(course_path)
        self.get_course_image(course_path, By.CLASS_NAME, "course-image")

        chapter_idx = 1
        video_list = []
        sections = WebDriverWait(self.driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".course-section"))
        )
        for section in sections:
            chapter_title = section.find_element(By.CSS_SELECTOR, ".section-title").text
            chapter_title = clean_string(chapter_title)
            chapter_title = f"{chapter_idx:02d}-{chapter_title}"
            logger.info("Found chapter: " + chapter_title)

            download_path = os.path.join(course_path, chapter_title)
            os.makedirs(download_path, exist_ok=True)

            chapter_idx += 1
            idx = 1

            section_items = section.find_elements(By.CSS_SELECTOR, ".section-item")
            for item in section_items:
                lecture_link = item.find_element(By.CLASS_NAME, "item").get_attribute(
                    "href"
                )
                lecture_title = item.find_element(By.CLASS_NAME, "lecture-name").text
                lecture_title = clean_string(lecture_title)
                logger.info("Found lecture: " + lecture_title)

                truncated_lecture_title = truncate_title_to_fit_filename(lecture_title)

                video_entity = {
                    "link": lecture_link,
                    "title": truncated_lecture_title,
                    "download_path": download_path,
                    "idx": idx,
                }
                video_list.append(video_entity)
                idx += 1

        self.download_videos_from_links(video_list)

    def download_course_simple(self, course_url):
        self.driver.implicitly_wait(2)
        logger.info("Detected next course format")
        course_title = self.get_course_title_simple(course_url)
        course_path = create_folder(course_title)

        self.save_course_html(course_path)
        self.get_course_image(
            course_path, By.XPATH, '//*[@id="__next"]/div/div/div[2]/div/div[1]/img'
        )

        chapter_idx = 0
        video_list = []
        slim_sections = self.driver.find_elements(By.CSS_SELECTOR, ".slim-section")
        for section in slim_sections:
            chapter_idx += 1
            bars = section.find_elements(By.CSS_SELECTOR, ".bar")
            chapter_title = section.find_element(By.CSS_SELECTOR, ".heading").text
            chapter_title = clean_string(chapter_title)
            chapter_title = f"{chapter_idx:02d}-{chapter_title}"
            logger.info("Found chapter: " + chapter_title)

            try:
                WebDriverWait(section, self.global_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".drip-tag"))
                )
                logger.warning('Chapter "%s" not available, skipping', chapter_title)
                continue
            except TimeoutException:
                logger.info("Chapter is available")
                # Element wasn't found so the chapter is available

            download_path = os.path.join(course_path, chapter_title)
            os.makedirs(download_path, exist_ok=True)

            idx = 1
            for bar in bars:
                video = bar.find_element(By.CSS_SELECTOR, ".text")
                link = video.get_attribute("href")
                # Remove new line characters from the title and replace spaces with -
                title = clean_string(video.text)
                logger.info("Found lecture: " + title)
                truncated_title = truncate_title_to_fit_filename(title)
                video_entity = {
                    "link": link,
                    "title": truncated_title,
                    "download_path": download_path,
                    "idx": idx,
                }
                video_list.append(video_entity)
                idx += 1

        self.download_videos_from_links(video_list)

    def get_course_title(self, course_url, by, selector):
        if self.driver.current_url != course_url:
            self.driver.get(course_url)
        try:
            logger.info("Getting course title")
            course_title = (
                WebDriverWait(self.driver, self.global_timeout)
                .until(EC.presence_of_element_located((by, selector)))
                .text
            )
        except Exception:
            logger.warning("Could not get course title, using tab title instead")
            course_title = self.driver.title
        return clean_string(course_title)

    def get_course_title_simple(self, course_url):
        if self.driver.current_url != course_url:
            self.driver.get(course_url)
        WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".wrap"))
        )
        heading = WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".heading"))
        )
        course_title = heading.text
        return clean_string(course_title)

    def save_course_image(self, course_path, image_src):
        image_path = os.path.join(course_path, "course-image.jpg")

        # Skip existing image if it already exists
        if os.path.exists(image_path):
            logger.info("Image already exists, skipping download.")
            return True

        try:
            response = requests.get(image_src, timeout=30)
        except requests.RequestException as e:
            logger.warning("Could not download course image: " + str(e))
            return False

        if not response.ok:
            logger.warning(
                f"Failed to download image (HTTP {response.status_code}): {image_src}"
            )
            return False

        # Write to a temporary file first so an interrupted write never leaves a
        # partial image at image_path, which the existence check would skip
        part_path = image_path + ".part"
        with open(part_path, "wb") as f:
            f.write(response.content)
        os.replace(part_path, image_path)

        logger.info("Image downloaded successfully.")
        return True

    def get_course_image(self, course_path, by, selector):
        image_element = self.driver.find_elements(by, selector)

        if not image_element:
            logger.warning("No course image element found")
            return

        try:
            logger.info("Found course image")
            image_src = image_element[0].get_attribute("src")
            # Try the high-definition version first, falling back to the
            # original link if that fails for any reason.
            image_src_hd = re.sub(r"/resize=.+?/", "/", image_src)
            if self.save_course_image(course_path, image_src_hd):
                return
            self.save_course_image(course_path, image_src)
        except Exception as e:
            logger.warning("Could not find course image: " + str(e))

    def save_course_html(self, course_path):
        try:
            logger.debug("Saving course html")
            output_file = os.path.join(course_path, "course.html")
            with open(output_file, "w+", encoding="utf-8") as f:
                f.write(self.driver.page_source)
        except Exception as e:
            logger.error("Could not save course html: " + str(e), exc_info=self.verbose)

    def find_video_widgets(self):
        # Most templates wrap each video in a widget that mounts its player iframe later via JS;
        # for templates without those widgets, fall back to the player iframes on the page
        widgets = self.driver.find_elements(
            By.CLASS_NAME, "lecture-attachment-type-video"
        )
        if widgets:
            return widgets
        return self.driver.find_elements(
            By.XPATH, "//iframe[starts-with(@data-testid, 'embed-player')]"
        )

    def download_video_widget(
        self, widget, widget_index, total_widgets, title, idx, download_path
    ):
        # Later widgets can take much longer to mount their iframe, so wait well past
        # global_timeout. Scroll once, then poll: re-scrolling can reset the lazy-loader's
        # visibility timer and stop the widget from mounting at all.
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", widget
        )

        iframe = None
        max_wait_seconds = max(self.global_timeout * 3, 30)
        deadline = time.time() + max_wait_seconds
        attempt_num = 0

        while time.time() < deadline:
            attempt_num += 1
            # descendant-or-self: a widget may be the iframe itself (see find_video_widgets)
            frames = widget.find_elements(
                By.XPATH,
                "descendant-or-self::iframe[starts-with(@data-testid, 'embed-player')]",
            )
            if frames:
                iframe = frames[0]
                break
            if attempt_num % 4 == 0:
                logger.debug(
                    f"Still waiting for video {widget_index + 1}/{total_widgets} to mount its player ({title})"
                )
            time.sleep(1)  # Wait a bit before retrying

        if iframe is None:
            raise TimeoutException(
                f"video {widget_index + 1}/{total_widgets} never mounted its player within {max_wait_seconds}s"
            )

        logger.info("Switching to video frame")
        self.driver.switch_to.frame(iframe)

        # Lazy-loaded iframes may not have rendered the video data
        # yet, so wait for it rather than failing immediately.
        script_text = WebDriverWait(self.driver, self.global_timeout).until(
            EC.presence_of_element_located((By.ID, "__NEXT_DATA__"))
        )
        json_text = json.loads(script_text.get_attribute("innerHTML"))
        link = json_text["props"]["pageProps"]["applicationData"]["mediaAssets"][0][
            "urlEncrypted"
        ]

        # Append -n to the video title if there are multiple widgets
        video_title = title + ("-" + str(widget_index + 1) if total_widgets > 1 else "")

        try:
            logger.info("Downloading subtitle")
            self.download_subtitle(link, video_title, idx, download_path)
        except Exception as e:
            logger.warning(
                "Could not download subtitle: " + video_title + " cause: " + str(e)
            )

        logger.info("Downloading video")
        return self.download_video(link, video_title, idx, download_path)

    def download_videos_from_links(self, video_list):
        for video in video_list:
            link = video["link"]
            title = video["title"]
            download_path = video["download_path"]
            idx = video["idx"]

            if self.driver.current_url != link:
                logger.info("Navigating to lecture: " + title)
                self.driver.get(link)

                # Wait for the lecture content the steps below look for
                # attachments or a video player to render
                try:
                    WebDriverWait(self.driver, self.global_timeout).until(
                        EC.presence_of_element_located(
                            (By.CSS_SELECTOR, LECTURE_CONTENT_SELECTOR)
                        )
                    )
                except TimeoutException:
                    logger.warning(
                        "No attachments or video found for lecture: " + title
                    )

            logger.info("Downloading lecture: " + title)
            try:
                logger.info("Saving html")
                self.save_webpage_as_html(title, idx, download_path)
            except Exception as e:
                logger.error(
                    "Could not save html: " + title + " cause: " + str(e),
                    exc_info=self.verbose,
                )

            try:
                logger.info("Downloading attachments")
                self.download_attachments(link, title, idx, download_path)
            except Exception as e:
                logger.warning(
                    "Could not download attachments: " + title + " cause: " + str(e)
                )

            try:
                logger.debug("Trying to download video as an attachment")
                if self.download_video_file(title, idx, download_path):
                    continue
            except Exception as e:
                logger.debug(
                    "Could not download video as an attachment: "
                    + title
                    + " cause: "
                    + str(e)
                )

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
                            self.driver.get(link)
                            WebDriverWait(self.driver, self.global_timeout).until(
                                EC.presence_of_element_located(
                                    (By.CSS_SELECTOR, LECTURE_CONTENT_SELECTOR)
                                )
                            )
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
                        logger.warning(
                            "Could not find video: " + title + " cause: " + str(e)
                        )
                    finally:
                        # Reset top-level context so the next widget (or next lecture)
                        # doesn't inherit a broken frame context
                        self.driver.switch_to.default_content()

            logger.info("Downloaded video: " + title)

            if self._complete_lecture:
                try:
                    logger.info("Completing lecture")
                    self.complete_lecture()
                except Exception as e:
                    logger.warning(
                        "Could not complete lecture: "
                        + video["title"]
                        + " cause: "
                        + str(e)
                    )

    def save_webpage_as_html(self, title, video_index, output_path):
        output_file = os.path.join(output_path, f"{video_index:02d}-{title}.html")
        with open(output_file, "w+", encoding="utf-8") as f:
            f.write(self.driver.page_source)
        logger.info("Saved webpage as html: " + output_file)

    def download_attachments(self, link, title, video_index, output_path):
        video_title = f"{video_index:02d}-{title}"

        # Grab the video attachments type file
        video_attachments = self.driver.find_elements(
            By.CLASS_NAME, "lecture-attachment-type-file"
        )
        if video_attachments:
            # Get all links from the video attachments
            video_links = video_attachments[0].find_elements(By.TAG_NAME, "a")
            output_path = os.path.join(output_path, video_title)
            os.makedirs(output_path, exist_ok=True)

            # Get href attribute from the first link
            if video_links:
                for video_link in video_links:
                    link = video_link.get_attribute("href")
                    filename = video_link.text
                    logger.info(
                        "Downloading attachment: " + filename + " for video: " + title
                    )
                    # Download file and save the file in output_path directory
                    wget.download(link, out=output_path)
        else:
            logger.warning("No attachments found for video: " + title)

    def download_video_file(self, title, video_index, output_path, timeout=60):
        video_title = f"{video_index:02d}-{title}"

        # Grab the video attachments type video
        video_attachment = self.driver.find_element(
            By.CLASS_NAME, "lecture-attachment-type-video"
        )
        if not video_attachment:
            logger.debug(f"No video attachment found for lecture: {title}")
            return False

        video_link = video_attachment.find_element(By.TAG_NAME, "a")
        if not video_link:
            logger.debug(f"No video link found for lecture: {title}")
            return False

        # Set the download directory for this file
        self.driver.execute_cdp_cmd(
            "Page.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": output_path,
            },
        )
        # Get list of files before download
        files_before_download = set(os.listdir(output_path))

        # Click the link to trigger download
        video_link.click()

        # Wait for download to complete
        start_time = time.time()
        while True:
            files_after_download = set(os.listdir(output_path))

            # Find new files
            new_files = files_after_download - files_before_download

            if len(new_files) > 0 and all(
                os.path.splitext(f)[1] != ".crdownload" for f in new_files
            ):
                break

            if timeout > 0 and (time.time() - start_time) > timeout:
                logger.warning(f"Download timeout for lecture: {title}")
                return False

            time.sleep(1)

        latest_file = os.path.join(output_path, next(iter(new_files)))

        # Determine the file extension
        _, extension = os.path.splitext(latest_file)

        # Create the new filename
        new_filename = f"{video_title}{extension}"
        new_filepath = os.path.join(output_path, new_filename)

        # Rename the file
        os.rename(latest_file, new_filepath)
        logger.info(f"Downloaded video file {new_filename}")
        return True

    # This function is needed because yt-dlp subtitle downloader is not working
    def download_subtitle(self, link, title, video_index, output_path):
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                },
            ],
            "http_headers": self.headers,
            "allsubtitles": True,
            "subtitleslangs": ["all"],
            "concurrentfragments": 10,
            "writesubtitles": True,
            "outtmpl": os.path.join(output_path, title),
            "verbose": self.verbose,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(link, download=False)
                info_json = ydl.sanitize_info(info)
        except Exception as e:
            logger.warning(
                "Could not download subtitle: " + title + " cause: " + str(e)
            )
            return

        subtitle_links = {}
        for lang, sub_info in info_json["requested_subtitles"].items():
            subtitle_links[lang] = {
                "url": sub_info["url"],
                "ext": sub_info["ext"],
            }

        # Print the subtitle links and language names
        for lang, sub in subtitle_links.items():
            subtitle_filename = "{:02d}-{}.{}.{}".format(
                video_index, title, lang, sub["ext"]
            )
            file_path = os.path.join(output_path, subtitle_filename)
            if os.path.isfile(file_path):
                logger.info("Skipping existing subtitle: " + subtitle_filename)
            else:
                base_url = sub["url"]
                try:
                    req = requests.get(sub["url"], headers=self.headers, timeout=30)
                except Exception as e:
                    logger.warning(
                        "Could not download subtitle: " + title + " cause: " + str(e)
                    )
                    continue

                relative_path = req.text.split("\n")[5]
                full_url = urljoin(base_url, relative_path)
                try:
                    response = requests.get(full_url, headers=self.headers, timeout=30)
                    with open(file_path, "wb") as f:
                        f.write(response.content)
                except Exception as e:
                    logger.warning(
                        "Could not download subtitle: " + title + " cause: " + str(e)
                    )
                    continue

                logger.info("Downloaded subtitle: " + subtitle_filename)

    def download_video(self, link, title, video_index, output_path):
        output_file = os.path.join(output_path, f"{video_index:02d}-{title}.mp4")
        if os.path.isfile(output_file):
            logger.info("Skipping existing video: " + output_file)
            return

        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                },
                {
                    "key": "FFmpegMetadata",
                },
            ],
            "http_headers": self.headers,
            "concurrentfragments": 15,
            "outtmpl": os.path.join(output_path, f"{video_index:02d}-{title}.mp4"),
            "verbose": self.verbose,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([link])
            return True
        except Exception as e:
            logger.error("Could not download video: " + title + " cause: " + str(e))
            return False

    def complete_lecture(self):
        self.driver.switch_to.default_content()
        complete_button = self.driver.find_element(By.ID, "lecture_complete_button")
        if complete_button:
            logger.info("Found complete button")
            complete_button.click()
            logger.info("Completed lecture")
            time.sleep(3)

    def cleanup(self):
        logger.info("Cleaning up")
        self.driver.quit()
        # Delete cookies.txt
        if os.path.exists("cookies.txt"):
            os.remove("cookies.txt")


BATCH_FILE_FIELDS = ("url", "email", "password", "login_url")


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
        logger.error(f"Could not read file: {file_path}. Error: {e!s}")
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


def check_required_args(args):
    # A batch file can carry its own credentials per course
    if args.manual_login or args.file:
        return True
    return bool(args.email and args.password)


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
    args = parser.parse_args()
    verbose = False
    if args.verbose == 0:
        log_level = logging.WARNING
    elif args.verbose == 1:
        log_level = logging.INFO
    else:
        verbose = True
        log_level = logging.DEBUG

    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    if not check_required_args(args):
        logger.error(
            "Required arguments are missing. Choose email/password or --manual-login."
        )
        sys.exit(1)

    downloader = TeachableDownloader(
        verbose_arg=verbose,
        complete_lecture_arg=args.complete_lecture,
        user_agent_arg=args.user_agent,
        timeout_arg=args.timeout,
    )
    if args.file:
        entries = read_batch_file(args.file)
        try:
            downloader.run_batch(
                entries,
                args.email,
                args.password,
                args.login_url,
                manual_login=args.manual_login,
            )
            downloader.cleanup()
            sys.exit(0)
        except KeyboardInterrupt:
            logger.error("Interrupted by user")
            downloader.cleanup()
            sys.exit(1)
        except Exception as e:
            logger.error("Error: " + str(e))
            downloader.cleanup()
            sys.exit(1)
    else:
        # Check if url argument is passed
        if not args.url:
            logger.error("URL is required")
            sys.exit(1)
        try:
            downloader.run(
                course_url=args.url,
                email=args.email,
                password=args.password,
                login_url=args.login_url,
                manual_login=args.manual_login,
            )
            downloader.cleanup()
            sys.exit(0)
        except KeyboardInterrupt:
            logger.error("Interrupted by user")
            downloader.cleanup()
            sys.exit(1)
        except Exception as e:
            logger.error("Error: " + str(e))
            downloader.cleanup()
            sys.exit(1)
