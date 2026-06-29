#!/usr/bin/env python3
"""Create a Shopee Playwright session from credentials or exported cookies."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from getpass import getpass
from pathlib import Path

from playwright.async_api import Browser, Error, Page, TimeoutError, async_playwright


LOGIN_URL = "https://shopee.co.id/buyer/login"
HOME_URL = "https://shopee.co.id/"
LOGOUT_URLS = [
    "https://shopee.co.id/buyer/logout",
    "https://shopee.co.id/logout",
]
DEFAULT_STATE_PATH = "output/shopee-state.json"
DEFAULT_COOKIES_PATH = "output/shopee-cookies.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Log in to Shopee using Chromium and save Playwright storage state."
    )
    parser.add_argument(
        "--identifier",
        default=os.getenv("SHOPEE_IDENTIFIER"),
        help="Shopee email, phone number, or username. Defaults to SHOPEE_IDENTIFIER.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SHOPEE_PASSWORD"),
        help="Shopee password. Defaults to SHOPEE_PASSWORD.",
    )
    parser.add_argument(
        "--otp",
        default=os.getenv("SHOPEE_OTP"),
        help="One-time code if Shopee asks for it. Defaults to SHOPEE_OTP.",
    )
    parser.add_argument(
        "--cookies-txt",
        default=os.getenv("SHOPEE_COOKIES_TXT"),
        help=(
            "Import a Netscape cookies.txt export instead of logging in with Chromium. "
            f"Defaults to SHOPEE_COOKIES_TXT when set."
        ),
    )
    parser.add_argument(
        "--print-login-url",
        action="store_true",
        help="Print the current Shopee login page URL after it opens.",
    )
    parser.add_argument(
        "--state",
        default=os.getenv("SHOPEE_STORAGE_STATE", DEFAULT_STATE_PATH),
        help=f"Path for saved storage state. Defaults to {DEFAULT_STATE_PATH}.",
    )
    parser.add_argument(
        "--cookies",
        default=os.getenv("SHOPEE_COOKIES_PATH", DEFAULT_COOKIES_PATH),
        help=f"Path for saved cookies JSON. Defaults to {DEFAULT_COOKIES_PATH}.",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Log out the saved Shopee session and remove local state/cookie files.",
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="With --logout, keep local state/cookie files after the remote logout attempt.",
    )
    parser.add_argument("--headful", action="store_true", help="Run with a visible browser.")
    parser.add_argument(
        "--allow-http2",
        action="store_true",
        help="Do not pass Chromium's --disable-http2 workaround.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("SHOPEE_TIMEOUT_MS", "30000")),
        help="Default Playwright timeout in milliseconds.",
    )
    parser.add_argument(
        "--login-timeout",
        type=int,
        default=int(os.getenv("SHOPEE_LOGIN_TIMEOUT_MS", "120000")),
        help="How long to wait for login completion in milliseconds.",
    )
    return parser.parse_args()


def chromium_args(args: argparse.Namespace) -> list[str]:
    launch_args = [
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    if not args.allow_http2:
        launch_args.append("--disable-http2")
    return launch_args


async def first_visible(page: Page, selectors: list[str], timeout: int = 3000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            return locator
        except TimeoutError:
            continue
    return None


async def click_first(page: Page, selectors: list[str], timeout: int = 2500) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click()
            return True
        except (TimeoutError, Error):
            continue
    return False


async def dismiss_language_modal(page: Page) -> None:
    await click_first(
        page,
        [
            "button:has-text('Bahasa Indonesia')",
            "button:has-text('English')",
            "text='Bahasa Indonesia'",
            "text='English'",
        ],
        timeout=2500,
    )
    await page.wait_for_timeout(500)
    try:
        modal_visible = await page.locator("text=/Pilih bahasa Anda/i").first.is_visible(timeout=1000)
    except Error:
        modal_visible = False
    if modal_visible:
        try:
            await page.evaluate(
                """() => {
                    const elements = [...document.querySelectorAll("button, div, span")];
                    const button = elements.find((element) =>
                        element.textContent && element.textContent.trim() === "Bahasa Indonesia"
                    );
                    if (button) button.click();
                }"""
            )
        except Error:
            pass
    await page.wait_for_timeout(500)
    try:
        modal_visible = await page.locator("text=/Pilih bahasa Anda/i").first.is_visible(timeout=1000)
    except Error:
        modal_visible = False
    if modal_visible:
        await page.mouse.click(682, 508)
    await page.wait_for_timeout(1500)


async def goto_login(page: Page) -> None:
    errors = []
    for wait_until, url in [("domcontentloaded", LOGIN_URL), ("commit", LOGIN_URL), ("domcontentloaded", HOME_URL)]:
        try:
            await page.goto(url, wait_until=wait_until)
            return
        except Error as exc:
            errors.append(str(exc).splitlines()[0])
            await page.wait_for_timeout(1500)
    raise RuntimeError("Could not open Shopee login. Chromium reported: " + "; ".join(errors))


async def fill_identifier(page: Page, identifier: str) -> None:
    identifier_input = await first_visible(
        page,
        [
            "input[name='loginKey']",
            "input[name='login']",
            "input[name='username']",
            "input[type='text']",
            "input[type='email']",
            "input[type='tel']",
            "input[placeholder*='email' i]",
            "input[placeholder*='phone' i]",
            "input[placeholder*='nomor' i]",
            "input[placeholder*='username' i]",
        ],
    )
    if identifier_input is None:
        raise RuntimeError("Could not find Shopee email/phone/username input.")
    await identifier_input.fill(identifier)


async def submit_login_form(page: Page) -> bool:
    return await click_first(
        page,
        [
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Masuk')",
            "button:has-text('Lanjut')",
            "button:has-text('Berikutnya')",
            "button:has-text('Next')",
            "button[type='submit']",
        ],
    )


async def fill_password_if_present(page: Page, password: str | None) -> bool:
    password_input = await first_visible(
        page,
        [
            "input[name='password']",
            "input[type='password']",
            "input[placeholder*='password' i]",
            "input[placeholder*='kata sandi' i]",
        ],
        timeout=8000,
    )
    if password_input is None:
        return False
    if not password:
        password = getpass("Shopee password: ")
    await password_input.fill(password)
    await submit_login_form(page)
    return True


async def login_with_credentials(page: Page, args: argparse.Namespace) -> None:
    await dismiss_language_modal(page)
    if args.print_login_url or args.headful:
        print("Login page URL:")
        print(page.url)

    await fill_identifier(page, args.identifier)
    if not await fill_password_if_present(page, args.password):
        if not await submit_login_form(page):
            raise RuntimeError("Could not submit Shopee login identifier.")
        if not await fill_password_if_present(page, args.password):
            raise RuntimeError("Could not find Shopee password input after submitting identifier.")
    await fill_otp_if_present(page, args.otp)
    await wait_until_logged_in(page, args.login_timeout)


async def fill_otp_if_present(page: Page, otp: str | None) -> None:
    otp_input = await first_visible(
        page,
        [
            "input[name*='otp' i]",
            "input[name*='code' i]",
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[placeholder*='kode' i]",
            "input[placeholder*='code' i]",
        ],
        timeout=10000,
    )
    if otp_input is None:
        return
    if not otp:
        otp = input("Shopee OTP/code: ").strip()
    await otp_input.fill(otp)
    await click_first(
        page,
        [
            "button:has-text('Verifikasi')",
            "button:has-text('Verify')",
            "button:has-text('Konfirmasi')",
            "button:has-text('Lanjut')",
            "button[type='submit']",
        ],
    )


async def wait_until_logged_in(page: Page, timeout_ms: int) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        lowered = page.url.lower()
        if "/buyer/login" not in lowered and "/login" not in lowered:
            return
        await page.wait_for_timeout(500)

    challenge = await first_visible(
        page,
        [
            "iframe[src*='captcha' i]",
            "text=/captcha/i",
            "text=/verifikasi keamanan/i",
            "text=/security verification/i",
        ],
        timeout=1500,
    )
    if challenge is not None:
        raise RuntimeError(
            "Shopee is showing a CAPTCHA or security check. Re-run with --headful "
            "to complete that step manually, then save the session."
        )
    raise RuntimeError("Login did not finish before the timeout.")


async def save_cookies(context, cookies_path: str) -> Path:
    path = Path(cookies_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = await context.cookies()
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    return path


def parse_netscape_cookies_txt(cookies_txt_path: str) -> list[dict]:
    path = Path(cookies_txt_path).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"cookies.txt not found at {path}")

    cookies: list[dict] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("# Netscape") or line.startswith("# This file"):
            continue

        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)
        if len(parts) != 7:
            raise RuntimeError(f"Invalid cookies.txt line {line_number}: expected 7 fields.")

        domain, _include_subdomains, cookie_path, secure, expires, name, value = parts
        if not name:
            continue

        try:
            expires_value = int(expires)
        except ValueError as exc:
            raise RuntimeError(f"Invalid cookie expiry on line {line_number}: {expires}") from exc

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": cookie_path or "/",
            "expires": expires_value if expires_value > 0 else -1,
            "httpOnly": http_only,
            "secure": secure.upper() == "TRUE",
            "sameSite": "Lax",
        }
        cookies.append(cookie)

    shopee_cookies = [
        cookie
        for cookie in cookies
        if cookie["domain"].lstrip(".").endswith("shopee.co.id")
    ]
    if not shopee_cookies:
        raise RuntimeError(f"No shopee.co.id cookies found in {path}")
    return shopee_cookies


def validate_imported_login_cookies(cookies: list[dict]) -> None:
    by_name = {cookie["name"]: cookie.get("value", "") for cookie in cookies}
    missing_or_guest = [
        name
        for name in ["SPC_U", "SPC_EC"]
        if by_name.get(name) in (None, "", "-")
    ]
    if missing_or_guest:
        raise RuntimeError(
            "cookies.txt does not contain an authenticated Shopee session. "
            f"Missing or guest auth cookie(s): {', '.join(missing_or_guest)}. "
            "Open Shopee in your normal browser, confirm the account is logged in, "
            "then export cookies for shopee.co.id again."
        )


def import_cookies_txt(args: argparse.Namespace) -> tuple[Path, Path, int]:
    cookies = parse_netscape_cookies_txt(args.cookies_txt)
    validate_imported_login_cookies(cookies)

    state_path = Path(args.state).expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2),
        encoding="utf-8",
    )

    cookies_path = Path(args.cookies).expanduser().resolve()
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")

    return state_path, cookies_path, len(cookies)


async def logout(args: argparse.Namespace) -> tuple[bool, list[Path]]:
    state_path = Path(args.state).expanduser().resolve()
    cookies_path = Path(args.cookies).expanduser().resolve()
    removed_paths: list[Path] = []
    remote_attempted = False

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=not args.headful and env_bool("SHOPEE_HEADLESS", True),
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH")
            or os.environ.get("PUPPETEER_EXECUTABLE_PATH"),
            args=chromium_args(args),
        )
        try:
            context_kwargs = {
                "locale": "id-ID",
                "timezone_id": "Asia/Jakarta",
                "user_agent": DEFAULT_USER_AGENT,
                "viewport": {"width": 1366, "height": 768},
            }
            if state_path.exists():
                context_kwargs["storage_state"] = str(state_path)
            context = await browser.new_context(**context_kwargs)
            context.set_default_timeout(args.timeout)
            page = await context.new_page()
            for url in LOGOUT_URLS:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
                    await page.wait_for_timeout(2000)
                    remote_attempted = True
                    break
                except Error:
                    continue
            await context.clear_cookies()
        finally:
            await browser.close()

    if not args.keep_files:
        for path in [state_path, cookies_path]:
            if path.exists():
                path.unlink()
                removed_paths.append(path)
    return remote_attempted, removed_paths


async def login(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.identifier:
        raise RuntimeError(
            "Missing login identifier. Pass --identifier, set SHOPEE_IDENTIFIER, "
            "or import exported browser cookies with --cookies-txt."
        )

    state_path = Path(args.state).expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    cookies_path = Path(args.cookies).expanduser().resolve()

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=not args.headful and env_bool("SHOPEE_HEADLESS", True),
            executable_path=os.environ.get("CHROMIUM_EXECUTABLE_PATH")
            or os.environ.get("PUPPETEER_EXECUTABLE_PATH"),
            args=chromium_args(args),
        )
        try:
            context = await browser.new_context(
                locale="id-ID",
                timezone_id="Asia/Jakarta",
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1366, "height": 768},
            )
            context.set_default_timeout(args.timeout)
            page = await context.new_page()
            await goto_login(page)
            await login_with_credentials(page, args)

            await context.storage_state(path=str(state_path))
            cookies_path = await save_cookies(context, args.cookies)
        finally:
            await browser.close()
    return state_path, cookies_path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    if args.logout:
        try:
            remote_attempted, removed_paths = asyncio.run(logout(args))
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"Logout failed: {exc}", file=sys.stderr)
            return 1

        if remote_attempted:
            print("Shopee remote logout was requested.")
        else:
            print("Shopee remote logout URL could not be reached; local cleanup continued.")
        if args.keep_files:
            print("Kept local Shopee state/cookie files.")
        elif removed_paths:
            for path in removed_paths:
                print(f"Removed {path}")
        else:
            print("No local Shopee state/cookie files to remove.")
        return 0

    if args.cookies_txt:
        try:
            state_path, cookies_path, cookie_count = import_cookies_txt(args)
        except Exception as exc:
            print(f"Cookie import failed: {exc}", file=sys.stderr)
            return 1

        print(f"Imported {cookie_count} Shopee cookies from {Path(args.cookies_txt).expanduser().resolve()}")
        print(f"Shopee session saved to {state_path}")
        print(f"Shopee cookies saved to {cookies_path}")
        return 0

    try:
        state_path, cookies_path = asyncio.run(login(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 1

    print(f"Shopee session saved to {state_path}")
    print(f"Shopee cookies saved to {cookies_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
