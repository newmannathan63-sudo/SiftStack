"""SmartSkip.io shared automation primitives — login, cookies, UI helpers.

SmartSkip has no public API (dashboard/CSV-upload only), so this mirrors the
shape of datasift_core.py: browser automation via Playwright instead of a
requests-based API client.

IMPORTANT — selectors below are best-guess placeholders, not verified against
the live site. Before this is used for real, run this module's __main__ debug
harness with headless=False and real SMARTSKIP_EMAIL/SMARTSKIP_PASSWORD to
confirm the login flow, then fix up any selectors that don't match (same
live-debug approach used for the DataSift automation — see
feedback_datasift_live_debug memory).
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# ── URLs ──────────────────────────────────────────────────────────────
# Confirmed 2026-08-30: the marketing site (smartskip.io) 404s on /login —
# the actual app lives on a separate subdomain.
SMARTSKIP_LOGIN_URL = "https://app.smartskip.io/login"
SMARTSKIP_DASHBOARD_URL = "https://app.smartskip.io/dashboard"
# Confirmed 2026-08-30: post-login redirect lands here, so this is real.
SMARTSKIP_BULK_URL = "https://app.smartskip.io/bulk-skip"

# ── Browser Defaults ──────────────────────────────────────────────────
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ── Credentials ───────────────────────────────────────────────────────

def get_credentials() -> tuple[str, str]:
    """Get SmartSkip email and password from environment or .env file.

    Returns (email, password). Raises ValueError if not found.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    email = os.getenv("SMARTSKIP_EMAIL", "")
    password = os.getenv("SMARTSKIP_PASSWORD", "")

    if not email or not password:
        raise ValueError(
            "SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD must be set in .env or environment"
        )
    return email, password


# ── Cookie / State Persistence ────────────────────────────────────────

def save_state(path: Path, data) -> None:
    """Write JSON state to disk with .bak backup."""
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path):
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read %s: %s", candidate, e)
    return {}


COOKIES_FILE = Path("smartskip_cookies.json")


async def save_cookies(page) -> None:
    """Save browser cookies for session reuse."""
    cookies = await page.context.cookies()
    save_state(COOKIES_FILE, cookies)
    logger.debug("Saved %d SmartSkip cookies", len(cookies))


async def load_cookies(context) -> bool:
    """Load saved cookies into browser context. Returns True if loaded."""
    cookies = load_state(COOKIES_FILE)
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        logger.debug("Loaded %d SmartSkip cookies", len(cookies))
        return True
    except Exception as e:
        logger.debug("Failed to load cookies: %s", e)
        return False


# ── Authentication ────────────────────────────────────────────────────

async def login(page, email: str = None, password: str = None) -> bool:
    """Log in to SmartSkip.io. Returns True on success.

    Tries saved cookies first, falls back to fresh login. Selectors are
    best-guess (generic email/password/submit patterns) — verify live before
    relying on this.
    """
    from playwright.async_api import TimeoutError as PwTimeout

    if not email or not password:
        email, password = get_credentials()

    has_cookies = await load_cookies(page.context)
    if has_cookies:
        await page.goto(SMARTSKIP_DASHBOARD_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        if "/login" not in page.url:
            logger.info("SmartSkip session restored from cookies")
            return True
        logger.info("SmartSkip cookies expired (url=%s), doing fresh login", page.url)

    await page.context.clear_cookies()
    await page.goto(SMARTSKIP_LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    await screenshot(page, "login_page_loaded")
    logger.info("Login page loaded — url=%s title=%s", page.url, await page.title())

    email_field = page.locator(
        'input[type="email"], input[name="email"], input[id*="email" i], '
        'input[placeholder*="email" i]'
    ).first
    try:
        await email_field.wait_for(state="visible", timeout=10000)
    except Exception:
        await screenshot(page, "login_no_email_field")
        body_text = (await page.inner_text("body"))[:500]
        logger.error("No email field found. Page text snippet: %s", body_text)
        raise
    await email_field.click()
    await email_field.fill(email)

    password_field = page.locator(
        'input[type="password"], input[name="password"], input[id*="password" i]'
    ).first
    await password_field.click()
    await password_field.fill(password)

    await screenshot(page, "login_before_submit")

    submit_btn = page.locator('button[type="submit"]')
    if await submit_btn.count() == 0:
        submit_btn = page.get_by_role("button", name="Log In")
    if await submit_btn.count() == 0:
        submit_btn = page.get_by_role("button", name="Sign In")
    await submit_btn.first.click()

    try:
        await page.wait_for_function(
            "() => !window.location.pathname.startsWith('/login')",
            timeout=20000,
        )
    except PwTimeout:
        await screenshot(page, "login_failed")
        logger.error("SmartSkip login failed — still on login page (url=%s)", page.url)
        return False

    await page.wait_for_timeout(2000)
    await screenshot(page, "login_post_submit")
    logger.info("Post-login URL: %s", page.url)

    await save_cookies(page)
    logger.info("SmartSkip login successful (url=%s)", page.url)
    return True


# ── UI Primitives ─────────────────────────────────────────────────────

async def screenshot(page, name: str) -> None:
    """Take a debug screenshot (saved to working directory)."""
    try:
        await page.screenshot(path=f"smartskip_{name}.png")
        logger.debug("Screenshot: smartskip_%s.png", name)
    except Exception as e:
        logger.debug("Screenshot failed (%s): %s", name, e)


# ── Browser Lifecycle ─────────────────────────────────────────────────

@asynccontextmanager
async def create_browser(headless: bool = False, viewport: dict = None):
    """Create a Playwright browser context. Yields (browser, context, page).

    Usage:
        async with create_browser(headless=False) as (browser, context, page):
            await login(page)
            # ... do work ...
    """
    from playwright.async_api import async_playwright

    vp = viewport or DEFAULT_VIEWPORT

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport=vp,
            user_agent=DEFAULT_USER_AGENT,
        )
        page = await context.new_page()
        try:
            yield browser, context, page
        finally:
            await browser.close()
