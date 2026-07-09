"""Duval Clerk of Circuit Court CORE ePortal (core.duvalclerk.com) — case
lookup by case number, used to find the correct defendant/property address
for lis pendens records.

Why this exists: the OR index (duval_clerk_scraper.py) and the recorded LP
document's OCR text sometimes yield the wrong property address (mismatched
name, misread OCR, etc.). The court case itself lists each party's address
in its Parties table, keyed to the exact lawsuit — far fewer candidates to
disambiguate than a county-wide name search, so it's a stronger source of
truth. Only usable when a case_number was extracted from the LP document OCR
(see LP_CASE_NO_RE / _parse_lp_document_text in duval_clerk_scraper.py).

Requires a CORE account (config.DUVAL_CORE_EMAIL / DUVAL_CORE_PASSWORD).

Site: ASP.NET WebForms tabbed SPA. Verified selectors as of 2026-07-08.
  - Login: a modal appears on page load —
    input#c_UsernameTextBox / input#c_PasswordTextBox /
    input[type='submit'][value='Login to CORE']
  - Case Number Entry: input[id^='c_UcnEntryBox_']. A client-side
    onkeyup="parseUcn(...)" handler validates/formats the UCN and enables
    the "Open Case" button — Playwright's `.fill()` does not fire keyup
    events, so the case number must be typed via `press_sequentially`.
  - Open Case button: input[id^='c_SubmitCaseLookupButton_'], matched by the
    same GUID suffix as the UcnEntryBox just typed into (each open "Case
    Search" tab has its own hidden copy of the form, all sharing the same
    ID prefixes with different GUIDs).
  - Clicking "Case Search" in the left nav opens a new blank tab each time,
    and each lookup's result opens in yet another new tab — tabs accumulate
    within a page and #c_PartiesPanel stops being unique to the active tab,
    causing duplicate/garbled rows to leak in from earlier lookups. Fix: open
    a fresh `context.new_page()` per case lookup instead of reusing tabs
    within one page — the login session (cookies) carries over on the shared
    context, so no re-login is needed, and each lookup starts from a single
    clean tab.
  - Parties table: div#c_PartiesPanel table, one <tbody> per party:
    name (<span>), party type (text before <br>), address (<address> tag,
    "STREET<br>CITY, STATEZIP" with no space between state and zip). Filter
    to `:visible` rows — a stale hidden copy of the panel can otherwise
    contribute duplicate, unparsed rows.
"""

import asyncio
import logging
import random
import re

from playwright.async_api import Page, async_playwright

import config
from config import BUSINESS_RE, REQUEST_DELAY_MAX, REQUEST_DELAY_MIN
from notice_parser import NoticeData
from tax_enricher import _score_dcpa_name

logger = logging.getLogger(__name__)

CORE_URL = config.DUVAL_CORE_URL


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _parse_address_block(text: str) -> tuple[str, str, str, str]:
    """Parse an <address> block's inner text into (street, city, state, zip)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return ("", "", "", "")
    street = lines[0]
    if len(lines) < 2:
        return (street, "", "", "")
    m = re.match(r"^(.*?),\s*([A-Z]{2})(\d{5}(?:-\d{4})?)$", lines[1])
    if m:
        return (street, m.group(1).strip(), m.group(2), m.group(3))
    return (street, lines[1], "", "")


async def _login(page: Page) -> bool:
    await page.goto(CORE_URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_selector("#c_UsernameTextBox", timeout=15_000)
    except Exception:
        logger.error("CORE: login form never appeared at %s", CORE_URL)
        return False

    await page.fill("#c_UsernameTextBox", config.DUVAL_CORE_EMAIL)
    await page.fill("#c_PasswordTextBox", config.DUVAL_CORE_PASSWORD)
    await page.click("input[type='submit'][value='Login to CORE']")
    await page.wait_for_timeout(3_000)

    logged_in = await page.locator(f"text={config.DUVAL_CORE_EMAIL}").count() > 0
    if not logged_in:
        logger.error("CORE: login did not succeed — check DUVAL_CORE_EMAIL/DUVAL_CORE_PASSWORD")
    return logged_in


async def _lookup_one_case(page: Page, case_number: str) -> list[dict]:
    """Look up one case number and return its Parties-table rows.

    Each row: {"name", "party_type", "address", "city", "state", "zip"}.
    Returns [] on any failure — caller falls back to other address sources.
    """
    await page.click("text=Case Search")
    try:
        case_input = page.locator("input[id^='c_UcnEntryBox_']:visible")
        await case_input.first.wait_for(state="visible", timeout=10_000)
    except Exception:
        logger.warning("CORE: case number field never appeared for %s", case_number)
        return []

    await case_input.first.click()
    await case_input.first.press_sequentially(case_number, delay=30)

    input_id = await case_input.first.get_attribute("id")
    guid = input_id[len("c_UcnEntryBox_"):]
    open_btn = page.locator(f"#c_SubmitCaseLookupButton_{guid}:visible")

    # The button's "aspNetDisabled" class is stale — it never gets removed —
    # but Playwright's own actionability check polls the real `disabled` DOM
    # property before clicking, so a plain (non-force) click already waits
    # correctly for parseUcn() to enable it.
    try:
        await open_btn.first.click(timeout=10_000)
    except Exception:
        logger.warning(
            "CORE: Open Case button never enabled for %s (invalid/unrecognized case number?)",
            case_number,
        )
        return []

    try:
        await page.locator("#c_PartiesPanel table tbody tr:visible").first.wait_for(
            state="visible", timeout=15_000
        )
    except Exception:
        logger.warning("CORE: no Parties table appeared for case %s", case_number)
        return []

    rows = page.locator("#c_PartiesPanel table tbody tr:visible")
    parties = []
    for i in range(await rows.count()):
        row = rows.nth(i)
        cells = row.locator("td")
        name = _clean_text(await cells.nth(0).inner_text())
        party_type = (await cells.nth(1).inner_text()).splitlines()[0].strip()
        addr_locator = cells.nth(2).locator("address")
        if await addr_locator.count() == 0:
            continue
        street, city, state, zip_code = _parse_address_block(await addr_locator.inner_text())
        if not street:
            continue
        parties.append({
            "name": name,
            "party_type": party_type,
            "address": street,
            "city": city,
            "state": state,
            "zip": zip_code,
        })
    return parties


def _best_defendant_address(parties: list[dict], owner_name: str) -> dict | None:
    """Pick the Parties-table row that best matches the known lis pendens
    owner name — skips the plaintiff (lender) and prefers a named person
    over entities/HOAs/unknown-tenant placeholders, since those can be
    co-defendants at a different address than the actual owner.
    """
    defendants = [p for p in parties if p["party_type"].upper() == "DEFENDANT" and p["address"]]
    if not defendants:
        return None

    named = [
        p for p in defendants
        if not BUSINESS_RE.search(p["name"]) and "UNKNOWN TENANT" not in p["name"].upper()
    ]

    scored = sorted(
        ((_score_dcpa_name(p["name"], owner_name), p) for p in named),
        key=lambda t: t[0],
        reverse=True,
    )
    if scored and scored[0][0] >= 0.4:
        return scored[0][1]

    # No confident name match — co-defendants at a foreclosed property
    # overwhelmingly share the same address, so fall back to the first
    # named person rather than an HOA/unknown-tenant placeholder.
    if named:
        return named[0]
    return defendants[0]


async def lookup_case_addresses(notices: list[NoticeData]) -> int:
    """For each notice with a case_number, look up its CORE case and set
    address/city/state/zip from the best-matching defendant. Mutates
    notices in place. Logs in once and reuses the session for the batch.

    Returns the number of notices whose address was set/corrected.
    """
    targets = [n for n in notices if n.case_number.strip()]
    if not targets:
        return 0
    if not config.DUVAL_CORE_EMAIL or not config.DUVAL_CORE_PASSWORD:
        logger.warning("CORE: DUVAL_CORE_EMAIL/DUVAL_CORE_PASSWORD not set — skipping case lookup")
        return 0

    matched = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        context.set_default_timeout(30_000)
        login_page = await context.new_page()
        logged_in = await _login(login_page)
        await login_page.close()
        if not logged_in:
            await browser.close()
            return 0

        for notice in targets:
            page = await context.new_page()
            try:
                # Fresh page per lookup — already authenticated via the
                # context's session cookies, so this lands straight on a
                # single clean "Case Search" tab (see module docstring).
                await page.goto(CORE_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(1_500)

                parties = await _lookup_one_case(page, notice.case_number)
                best = _best_defendant_address(parties, notice.owner_name)
                if best:
                    notice.address = best["address"]
                    notice.city = best["city"] or notice.city
                    notice.zip = best["zip"] or notice.zip
                    matched += 1
                    logger.debug(
                        "  CORE case %s -> %s, %s (matched party: %s)",
                        notice.case_number, best["address"], best["city"], best["name"],
                    )
                else:
                    logger.debug("  CORE case %s: no usable defendant address", notice.case_number)
            except Exception as e:
                logger.warning("  CORE lookup failed for case %s: %s", notice.case_number, e)
            finally:
                await page.close()
            await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

        await browser.close()

    logger.info("CORE case lookup: %d/%d addresses matched", matched, len(targets))
    return matched
