#!/usr/bin/env python3
"""Log in to Shopee with Chromium and persist the browser session."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from getpass import getpass
from pathlib import Path
from urllib.parse import quote, urljoin

from playwright.async_api import Browser, Error, Locator, Page, TimeoutError, async_playwright


LOGIN_URL = "https://shopee.co.id/buyer/login"
HOME_URL = "https://shopee.co.id/"
LOGOUT_URLS = [
    "https://shopee.co.id/buyer/logout",
    "https://shopee.co.id/logout",
]
DEFAULT_STATE_PATH = "output/shopee-state.json"
DEFAULT_COOKIES_PATH = "output/shopee-cookies.json"
DEFAULT_QR_PATH = "output/shopee-qr.png"
DEFAULT_CLI_QR_WIDTH = 64
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
    parser.add_argument("--qr", action="store_true", help="Use Shopee QR login.")
    parser.add_argument(
        "--print-qr-url",
        action="store_true",
        help="Print the QR image URL/data URL so it can be copied from a VM.",
    )
    parser.add_argument(
        "--print-login-url",
        action="store_true",
        help="Print the current Shopee login page URL after it opens.",
    )
    parser.add_argument(
        "--qr-path",
        default=os.getenv("SHOPEE_QR_PATH", DEFAULT_QR_PATH),
        help=f"Where to save the QR screenshot. Defaults to {DEFAULT_QR_PATH}.",
    )
    parser.add_argument("--no-cli-qr", action="store_true", help="Do not print the QR in the terminal.")
    parser.add_argument(
        "--cli-qr-width",
        type=int,
        default=int(os.getenv("SHOPEE_CLI_QR_WIDTH", str(DEFAULT_CLI_QR_WIDTH))),
        help=f"Terminal QR width in character cells. Defaults to {DEFAULT_CLI_QR_WIDTH}.",
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


def print_qr_to_terminal(path: Path, max_width: int = DEFAULT_CLI_QR_WIDTH) -> None:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "Printing QR to the CLI requires Pillow. Run: python -m pip install -r requirements.txt"
        ) from exc

    image = Image.open(path).convert("L")
    mask = image.point(lambda pixel: 0 if pixel > 245 else 255, mode="1")
    box = mask.getbbox()
    if box:
        image = image.crop(box)
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height), Image.Resampling.NEAREST)

    image = ImageOps.expand(image, border=4, fill=255)
    pixels = image.point(lambda pixel: 0 if pixel < 160 else 255, mode="1")
    print()
    print("Scan this QR with the Shopee mobile app:")
    for y in range(0, pixels.height, 2):
        row = []
        for x in range(pixels.width):
            top_dark = pixels.getpixel((x, y)) == 0
            bottom_dark = y + 1 < pixels.height and pixels.getpixel((x, y + 1)) == 0
            if top_dark and bottom_dark:
                row.append("█")
            elif top_dark:
                row.append("▀")
            elif bottom_dark:
                row.append("▄")
            else:
                row.append(" ")
        print("".join(row))
    print()


def image_path_to_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


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


async def fill_password_if_present(page: Page, password: str | None) -> None:
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
        return
    if not password:
        password = getpass("Shopee password: ")
    await password_input.fill(password)
    await click_first(
        page,
        [
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "button:has-text('Masuk')",
            "button[type='submit']",
        ],
    )


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


async def open_qr_login(page: Page) -> None:
    try:
        await page.goto("https://shopee.co.id/buyer/login/qr", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if await find_qr_locator(page) is not None:
            return
    except Error:
        pass

    for x, y in [(1155, 155), (1165, 150), (1145, 165)]:
        await page.mouse.click(x, y)
        await page.wait_for_timeout(1200)
        if await find_qr_locator(page) is not None:
            return

    for selector in [
        "text=/log\\s*in\\s*dengan\\s*qr/i",
        "text=/login\\s*dengan\\s*qr/i",
        "text=/kode\\s*qr/i",
        "text=/qr\\s*code/i",
        "text=/scan.*qr/i",
    ]:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=3000)
            box = await locator.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await page.wait_for_timeout(1500)
                if await find_qr_locator(page) is not None:
                    return
        except (TimeoutError, Error):
            continue

    raise RuntimeError("Could not switch Shopee login page to QR mode.")


async def find_qr_locator(page: Page) -> Locator | None:
    for selector in [
        "img[alt*='qr' i]",
        "img[src*='qr' i]",
        "canvas",
        "svg",
        "[data-testid*='qr' i]",
        "[class*='qr' i]",
    ]:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=3000)
            box = await locator.bounding_box()
            if (
                box
                and box["width"] >= 100
                and box["height"] >= 100
                and box["x"] >= 760
                and box["y"] <= 650
            ):
                return locator
        except (TimeoutError, Error):
            continue
    return None


async def extract_qr_url(page: Page, qr: Locator | None) -> str | None:
    if qr is None:
        return None
    try:
        tag_name = await qr.evaluate("element => element.tagName.toLowerCase()")
        if tag_name == "img":
            src = await qr.get_attribute("src")
            if src:
                return urljoin(page.url, src)
        if tag_name == "canvas":
            return await qr.evaluate("element => element.toDataURL('image/png')")
        if tag_name == "svg":
            svg = await qr.evaluate("element => element.outerHTML")
            return "data:image/svg+xml;charset=utf-8," + quote(svg)
    except Error:
        return None
    return None


async def save_qr_screenshot(page: Page, qr_path: str) -> tuple[Path, Locator | None]:
    path = Path(qr_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    qr = await find_qr_locator(page)
    if qr is None:
        await page.screenshot(path=str(path), full_page=True)
        raise RuntimeError(f"Could not find Shopee login QR. Saved page screenshot to {path}")
    await qr.screenshot(path=str(path))
    return path, qr


async def login_with_qr(
    page: Page,
    qr_path: str,
    login_timeout_ms: int,
    print_cli_qr: bool,
    print_qr_url: bool,
    print_login_url: bool,
    cli_qr_width: int,
) -> Path:
    await dismiss_language_modal(page)
    await open_qr_login(page)
    await dismiss_language_modal(page)
    saved_qr_path, qr = await save_qr_screenshot(page, qr_path)
    print(f"QR screenshot saved to {saved_qr_path}")
    if print_login_url:
        print("Login page URL:")
        print(page.url)
    if print_qr_url:
        qr_url = await extract_qr_url(page, qr)
        if not qr_url or qr_url.startswith("blob:"):
            qr_url = image_path_to_data_url(saved_qr_path)
        print("QR URL/data URL:")
        print(qr_url)
    if print_cli_qr:
        print_qr_to_terminal(saved_qr_path, max_width=cli_qr_width)
    print("Scan it with the Shopee mobile app, then approve the login request.")
    await wait_until_logged_in(page, login_timeout_ms)
    return saved_qr_path


async def save_cookies(context, cookies_path: str) -> Path:
    path = Path(cookies_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = await context.cookies()
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
    return path


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
    if not args.qr and not args.identifier:
        raise RuntimeError(
            "Missing login identifier. Pass --identifier, set SHOPEE_IDENTIFIER, or use --qr."
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
            if args.qr:
                await login_with_qr(
                    page,
                    args.qr_path,
                    args.login_timeout,
                    print_cli_qr=not args.no_cli_qr,
                    print_qr_url=args.print_qr_url,
                    print_login_url=args.print_login_url or args.headful,
                    cli_qr_width=args.cli_qr_width,
                )
            else:
                if args.print_login_url or args.headful:
                    print("Login page URL:")
                    print(page.url)
                await fill_identifier(page, args.identifier)
                await fill_password_if_present(page, args.password)
                await fill_otp_if_present(page, args.otp)
                await wait_until_logged_in(page, args.login_timeout)

            await context.storage_state(path=str(state_path))
            cookies_path = await save_cookies(context, args.cookies)
        finally:
            await browser.close()
    return state_path, cookies_path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parse_args()
    if args.cli_qr_width < 48:
        print("--cli-qr-width must be at least 48 for a scannable QR.", file=sys.stderr)
        return 2
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
