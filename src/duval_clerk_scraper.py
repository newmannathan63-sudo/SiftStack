"""Scraper for or.duvalclerk.com — Duval County Official Records (Lis Pendens).

Public portal — no login required. Searches the OR (Official Records) index
by document type "LIS PENDENS (LP)" and a date range.

Lis Pendens = notice that a lawsuit has been filed against a property (preforeclosure).
The grantor is the mortgagor / property owner — our target contact.

Site: Kendo UI SPA (OnCore Acclaim platform). Verified selectors as of 2026-06-11.
  - Search URL: or.duvalclerk.com/search/SearchTypeDocType
  - Doc Type: Kendo combobox — input name="DocTypesDisplay_input", value "LIS PENDENS (LP)"
  - From date: input name="RecordDateFrom" (Kendo datepicker, M/D/YYYY format)
  - To date:   input name="RecordDateTo"   (Kendo datepicker, M/D/YYYY format)
  - Export CSV button available after search — fetches ALL matching records at once.
    Preferred over HTML table pagination. Falls back to table scraping if CSV fails.
  - Results table columns: First Direct Name | First Indirect Name | Instrument # |
    Record Date | Doc Type | Book Type | Book/Page | Doc Link | Consideration | Legal

Address enrichment strategy for addressless lis pendens:
  The OR index row rarely contains a property street address — only grantor name,
  instrument number, book/page, and a brief legal description (lot/subdivision).
  Enrichment pipeline fills the gap via:
  1. Grantor name → Duval County Property Appraiser name search (TODO)
  2. Grantor name → TruePeopleSearch / FastPeopleSearch (existing deep prospecting)
  3. LLM extraction from legal description text if present in the row
"""

import asyncio
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

import config
from config import REQUEST_DELAY_MAX, REQUEST_DELAY_MIN, SavedSearch
from jdr_scraper import (
    FL_DUVAL_CITIES,
    FL_ZIP_RE,
    _clean,
    _is_office_address,
    _norm_date,
)
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Site constants ────────────────────────────────────────────────────

DUVAL_CLERK_BASE_URL = config.DUVAL_CLERK_URL.rstrip("/")
DUVAL_CLERK_SEARCH_URL = f"{DUVAL_CLERK_BASE_URL}/search/SearchTypeDocType"

# Exact label in the Kendo combobox (name="DocTypesDisplay_input")
LIS_PENDENS_INSTRUMENT_TYPE = "LIS PENDENS (LP)"

# Results per page (site default — may be configurable)
RESULTS_PER_PAGE = 25


# ── Lis Pendens–specific regex patterns ──────────────────────────────

# Grantor name from the OR index row.
# OR index typically shows: GRANTOR | GRANTEE | DATE | BOOK | PAGE | TYPE
# The grantor column contains the mortgagor / property owner.
# Matches "LASTNAME, FIRSTNAME" or "FIRSTNAME LASTNAME" in all-caps index text.
LP_GRANTOR_RE = re.compile(
    r"(?:Grantor|Mortgagor|Defendant)[:\s]+([A-Z][A-Z\s.,'-]+?)(?:\s{2,}|\t|\n|$)",
    re.IGNORECASE,
)

# Case number from the notice text (FL format: 16-2026-CA-001234)
LP_CASE_NO_RE = re.compile(
    r"Case\s+(?:No\.?|Number|#)?\s*[:\s]*"
    r"([0-9]{2}-[0-9]{4}-CA-[0-9]+(?:-[A-Z0-9]+)?)",
    re.IGNORECASE,
)

# Book/Page reference (OR recording index)
LP_BOOK_PAGE_RE = re.compile(
    r"(?:Book|Bk\.?)\s*[:\s]*(\d+)\s*[/,]?\s*(?:Page|Pg\.?)\s*[:\s]*(\d+)",
    re.IGNORECASE,
)

# Recording date from index row ("Recorded: MM/DD/YYYY" or "Date: MM/DD/YYYY")
LP_RECORDED_DATE_RE = re.compile(
    r"(?:Recorded|Instrument\s+Date|Date\s+Recorded)[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE,
)

# Legal description address pattern — appears in some lis pendens documents.
# FL legal notices often contain "commonly known as ADDR, Jacksonville, FL ZIP"
# ── Mortgage preforeclosure filters ──────────────────────────────────

# Plaintiffs that indicate this is NOT a mortgage LP.
# NOTE: DirectName = plaintiff (lender), IndirectName = defendant (property owner).
# Filter out HOA dues disputes and contractor/mechanic's liens.
_NON_MORTGAGE_PLAINTIFF_RE = re.compile(
    r"\b(?:"
    # HOA / condo associations
    r"HOMEOWNERS?\s+ASSOCIATION|CONDOMINIUM|CONDO\s+ASSOC(?:IATION)?|"
    r"COMMUNITY\s+ASSOCIATION|PROPERTY\s+OWNERS?\s+ASSOCIATION|"
    r"MASTER\s+ASSOCIATION|TOWN(?:HOME)?\s+ASSOC(?:IATION)?|"
    r"VILLAS?\s+ASSOC(?:IATION)?|CLUB\s+ESTATES|OWNERS?\s+ASSOC(?:IATION)?|"
    # Contractor / mechanic's lien filers
    r"ROOFING|CONSTRUCTION|PLUMBING|ELECTRIC(?:AL)?|HVAC|FLOORING|"
    r"PAINTING|REMODEL(?:ING)?|CONTRACTOR|BUILDERS?\b|RENOVATION"
    r")\b",
    re.IGNORECASE,
)


def _is_mortgage_preforeclosure(plaintiff: str, defendant: str) -> bool:
    """Return True only if this LP row looks like a mortgage preforeclosure.

    IMPORTANT — OR index column mapping (verified 2026-06-12):
      DirectName   = plaintiff  (lender filing the suit — bank, credit union, etc.)
      IndirectName = defendant  (property owner = our target contact)

    Two rules:
      1. Defendant (IndirectName / property owner) must be a person, not a business.
      2. Plaintiff (DirectName) must not be an HOA, condo association, or contractor.
    """
    from config import BUSINESS_RE
    if BUSINESS_RE.search(defendant):
        return False
    if _NON_MORTGAGE_PLAINTIFF_RE.search(plaintiff):
        return False
    return True


# Reuse jdr_scraper patterns; import added above.
_LP_ADDR_INDICATOR = (
    r"(?:"
    r"commonly\s+known\s+as"
    r"|property\s+(?:address|known\s+as|at)"
    r"|street\s+address"
    r"|located\s+at"
    r"|a/?k/?a"
    r")"
)

_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Rdg|Crossing|Xing|"
    r"Bend|Point|Pt|Pass|Hollow|Holw|Glen|View|Landing|Lndg|"
    r"Row|Trace|Walk|Knoll|Overlook|Crest|Spur|Commons)\b"
)

LP_ADDR_RE = re.compile(
    _LP_ADDR_INDICATOR
    + r"\s*[:.,\s]*"
    + r"(\d{1,5}\s+(?:[NSEW]\.?\s+)?(?:[\w'-]+\s+)+?" + _SUFFIX + r"\.?)"
    + r"(?:\s*[,.]\s*([\w][\w\s]*?))?"     # optional city
    + r"(?:\s*[,.]\s*(?:Florida|Fla\.?|FL))?"
    + r"(?:\s*[,.\s]*(\d{5}(?:-\d{4})?))?",
    re.IGNORECASE,
)


# ── Utilities ─────────────────────────────────────────────────────────


async def _delay() -> None:
    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _notice_hash(grantor: str, book: str, page: str, date: str) -> str:
    """Stable 12-char dedup ID from OR index key fields."""
    key = f"{grantor}|{book}|{page}|{date}"
    return hashlib.md5(key.encode("utf-8", errors="replace")).hexdigest()[:12]


# ── Per-record parser ─────────────────────────────────────────────────


def _parse_lp_row(row_text: str, search: SavedSearch, recorded_date: str, seq: int) -> NoticeData:
    """Parse one OR index row → NoticeData.

    The OR index row typically contains: grantor name, date, book/page,
    and sometimes a partial legal description. Street address is rarely
    present — enrichment pipeline fills the gap via property appraiser lookup.
    """
    source_url = (
        f"{DUVAL_CLERK_SEARCH_URL}?type={LIS_PENDENS_INSTRUMENT_TYPE}"
        f"&date={recorded_date}&seq={seq}"
    )

    # Append book/page to URL for uniqueness
    bk_m = LP_BOOK_PAGE_RE.search(row_text)
    if bk_m:
        source_url += f"&bk={bk_m.group(1)}&pg={bk_m.group(2)}"

    notice = NoticeData(
        county=search.county,
        notice_type="lis_pendens",
        state="FL",
        date_added=recorded_date,
        source_url=source_url,
        raw_text=row_text,
    )

    # Grantor = property owner (mortgagor)
    gm = LP_GRANTOR_RE.search(row_text)
    if gm:
        raw_name = _clean(gm.group(1))
        # OR index uses "LAST, FIRST MIDDLE" — convert to "First Last"
        if "," in raw_name:
            parts = [p.strip() for p in raw_name.split(",", 1)]
            raw_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else raw_name
        notice.owner_name = raw_name.title()

    # Property address (may not be present in index — that's OK).
    # OR index rows rarely include street addresses; enrichment fills via
    # owner name lookup after DataSift upload.
    am = LP_ADDR_RE.search(row_text)
    if am:
        addr = _clean(am.group(1))
        city = _clean(am.group(2)) if am.group(2) else ""
        if addr and not _is_office_address(addr, city):
            notice.address = addr
            notice.city    = city
            notice.zip     = am.group(3) or ""
    if not notice.city:
        # Default to Jacksonville — covers ~95% of Duval County residential.
        # DataSift enrichment overrides with actual city once address is found.
        notice.city = "Jacksonville"

    # Case number → append to source_url
    cm = LP_CASE_NO_RE.search(row_text)
    if cm:
        notice.source_url += f"&case={cm.group(1)}"

    return notice


# ── Playwright form interaction ───────────────────────────────────────


def _to_mdy(iso: str) -> str:
    """Convert YYYY-MM-DD → M/D/YYYY (Kendo datepicker format on this site)."""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{d.month}/{d.day}/{d.year}"
    except ValueError:
        return iso


async def _set_kendo_datepicker(page: Page, name: str, value_mdy: str) -> bool:
    """Set a Kendo datepicker input by name. Returns True on success.

    Kendo datepickers ignore a plain Playwright fill() — the widget re-validates
    on blur. We triple-fill and fire change events to force the widget to accept.
    """
    sel = f"input[name='{name}']"
    # Use locator (not query_selector/ElementHandle) — triple_click() is Locator-only
    el = page.locator(sel).first
    if not await el.count():
        logger.warning("DuvalClerk: datepicker input[name='%s'] not found", name)
        return False
    await el.click(click_count=3)
    await el.fill(value_mdy)
    await el.press("Tab")   # triggers Kendo blur/change validation
    logger.debug("Set datepicker %s = %s", name, value_mdy)
    return True


async def _set_kendo_combobox(page: Page, input_name: str, value: str) -> bool:
    """Set a Kendo combobox by typing and selecting the matching dropdown item.

    Kendo comboboxes (role="combobox") autocomplete from a list — we clear,
    type, wait for the listbox to appear, then click the first matching option.
    """
    sel = f"input[name='{input_name}']"
    # Use locator (not query_selector/ElementHandle) — triple_click() is Locator-only
    el = page.locator(sel).first
    if not await el.count():
        logger.warning("DuvalClerk: combobox input[name='%s'] not found", input_name)
        return False

    await el.click(click_count=3)
    await el.fill("")
    await el.type(value, delay=50)

    # Wait for Kendo listbox popup
    try:
        await page.wait_for_selector(
            "[role='listbox'] [role='option']", timeout=6_000
        )
        option = await page.query_selector(
            f"[role='listbox'] [role='option']:has-text('{value.split()[0]}')"
        )
        if option:
            await option.click()
            logger.info("DuvalClerk: combobox %s set to '%s' via listbox", input_name, value)
            return True
        logger.warning("DuvalClerk: listbox appeared but no option matched '%s'", value.split()[0])
    except PwTimeout:
        logger.warning("DuvalClerk: listbox did not appear for combobox %s — falling back to typed value", input_name)

    # Fallback: value may already be accepted by the input alone
    logger.info(
        "DuvalClerk: combobox %s — accepting typed value '%s' without listbox selection", input_name, value
    )
    await el.press("Tab")
    return True


async def _submit_search_form(
    page: Page,
    start_date: str,
    end_date: str,
) -> bool:
    """Navigate to the OR search page and submit a lis pendens date-range search.

    Selectors verified against or.duvalclerk.com on 2026-06-11 via DevTools:
      - Doc type:  input[name="DocTypesDisplay_input"]  (Kendo combobox)
      - From date: input[name="RecordDateFrom"]          (Kendo datepicker)
      - To date:   input[name="RecordDateTo"]            (Kendo datepicker)
      - Submit:    button:has-text("Search")
    """
    logger.info("DuvalClerk: loading search page %s", DUVAL_CLERK_SEARCH_URL)
    try:
        await page.goto(DUVAL_CLERK_SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except PwTimeout:
        logger.warning("DuvalClerk search page load timed out — retrying once")
        try:
            await page.goto(DUVAL_CLERK_SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
        except PwTimeout:
            logger.error("DuvalClerk search page unreachable")
            return False

    # or.duvalclerk.com shows a Disclaimer/ToS page on fresh sessions.
    # Detect the redirect and click Accept before the Kendo form is accessible.
    if "disclaimer" in page.url.lower():
        logger.info("DuvalClerk: disclaimer page detected (%s) — accepting terms", page.url)
        accepted = False
        for sel in [
            "button:has-text('Accept')",
            "button:has-text('Agree')",
            "button:has-text('I Agree')",
            "button:has-text('Continue')",
            "a:has-text('Accept')",
            "a:has-text('I Agree')",
            "input[type='submit']",
        ]:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                logger.debug("DuvalClerk: clicked disclaimer button '%s'", sel)
                accepted = True
                break
        if not accepted:
            logger.error("DuvalClerk: disclaimer page found but no Accept button matched — check selectors")
            return False
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except PwTimeout:
            pass

        # After disclaimer acceptance the st= param should redirect us to the
        # search form, but verify and re-navigate if needed.
        logger.info("DuvalClerk: URL after disclaimer acceptance = %s", page.url)
        if "disclaimer" in page.url.lower() or "SearchTypeDocType" not in page.url:
            logger.info("DuvalClerk: re-navigating to search form after disclaimer")
            try:
                await page.goto(DUVAL_CLERK_SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except PwTimeout:
                pass

    logger.info("DuvalClerk: on search page: %s", page.url)

    # Give Kendo UI extra time to finish JS initialization after networkidle.
    # Government court sites can be slow to bootstrap Kendo widgets.
    await page.wait_for_timeout(4_000)

    try:
        await page.wait_for_selector(
            "input[name='DocTypesDisplay_input']", timeout=30_000
        )
        logger.debug("DuvalClerk: Kendo widgets ready")
    except PwTimeout:
        # Capture what the page actually shows so we can diagnose the failure.
        try:
            screenshot_path = "/tmp/dc_kendo_failure.png"
            await page.screenshot(path=screenshot_path, full_page=False)
            logger.error(
                "DuvalClerk: Kendo combobox never appeared after 30s — screenshot saved to %s",
                screenshot_path,
            )
        except Exception as sc_err:
            logger.error(
                "DuvalClerk: Kendo combobox never appeared after 30s (screenshot failed: %s)",
                sc_err,
            )
        logger.error("DuvalClerk: page URL at timeout: %s", page.url)
        return False

    # ── Doc type (Kendo combobox) ────────────────────────────────────
    await _set_kendo_combobox(page, "DocTypesDisplay_input", LIS_PENDENS_INSTRUMENT_TYPE)

    # ── Date range (Kendo datepickers) ───────────────────────────────
    await _set_kendo_datepicker(page, "RecordDateFrom", _to_mdy(start_date))
    await _set_kendo_datepicker(page, "RecordDateTo",   _to_mdy(end_date))

    # ── Submit ───────────────────────────────────────────────────────
    submitted = False
    for btn_sel in [
        "button:has-text('Search')",
        "input[type='submit'][value='Search']",
        "button[type='submit']",
        "input[type='submit']",
    ]:
        try:
            await page.click(btn_sel, timeout=3_000)
            logger.info("DuvalClerk: form submitted via '%s'", btn_sel)
            submitted = True
            break
        except Exception:
            continue

    if not submitted:
        # JS fallback: click the first submit-like element in the form
        clicked = await page.evaluate("""() => {
            const btn = document.querySelector(
                'button[type="submit"], input[type="submit"], button:not([type])'
            );
            if (btn) { btn.click(); return btn.textContent || btn.value || 'clicked'; }
            return null;
        }""")
        if clicked:
            logger.info("DuvalClerk: form submitted via JS click ('%s')", str(clicked).strip()[:40])
        else:
            logger.warning("DuvalClerk: no submit button found — pressing Enter as last resort")
            await page.keyboard.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PwTimeout:
        pass
    await _delay()
    logger.info("DuvalClerk: URL after search submit = %s", page.url)
    # Log a snippet of page text to help diagnose results
    try:
        snippet = (await page.inner_text("body"))[:400].replace("\n", " ").strip()
        logger.info("DuvalClerk: page text after search = %s", snippet)
    except Exception:
        pass
    return True


# ── Results extraction ────────────────────────────────────────────────


async def _try_export_csv(page: Page) -> list[tuple[str, str]] | None:
    """Click 'Export to CSV' and parse the downloaded file.

    Returns list of (recorded_date, row_text) if successful, None if the
    button is absent or the download fails (caller falls back to table scraping).

    Verified: 'Export to CSV' link is present at the bottom-left of the
    results area after a search on or.duvalclerk.com.
    Column order in the CSV (verified 2026-06-11):
      R# | First Direct Name | First Indirect Name | Instrument # |
      Record Date | Doc Type | Book Type | Book/Page | Doc Link |
      Consideration | Legal | DeletedAfter
    """
    export_sel = "a:has-text('Export to CSV'), button:has-text('Export to CSV')"
    el = await page.query_selector(export_sel)
    if not el:
        return None

    try:
        async with page.expect_download(timeout=20_000) as dl_info:
            await el.click()
        download = await dl_info.value
        path = await download.path()
        if not path:
            return None

        import csv
        results: list[tuple[str, str]] = []
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Verified column names from live CSV export (2026-06-11):
                #   DirectName   = plaintiff (lender filing the suit)
                #   IndirectName = defendant (property owner = our target contact)
                plaintiff  = (row.get("DirectName") or "").strip()
                defendant  = (row.get("IndirectName") or "").strip()
                instrument = (row.get("InstrumentNumber") or "").strip()
                book_page  = (row.get("BookPage") or "").strip()
                legal      = (row.get("DocLegalDescription") or "").strip()

                # RecordDate includes a timestamp: "6/11/2026 9:13:44 AM"
                # Strip the time portion before normalising to YYYY-MM-DD.
                rec_date_raw = (row.get("RecordDate") or "").strip()
                rec_date_day = rec_date_raw.split(" ")[0] if rec_date_raw else ""
                rec_date = _norm_date(rec_date_day) if rec_date_day else ""

                if not defendant:
                    continue

                # DocLink may be a relative or absolute URL to the document viewer
                doc_link = (row.get("DocLink") or "").strip()
                if doc_link and not doc_link.startswith("http"):
                    doc_link = f"{DUVAL_CLERK_BASE_URL}/{doc_link.lstrip('/')}"

                # Grantor = defendant (property owner), Grantee = plaintiff (lender)
                row_text = (
                    f"Grantor: {defendant}\n"
                    f"Grantee: {plaintiff}\n"
                    f"Instrument #: {instrument}\n"
                    f"Record Date: {rec_date_raw}\n"
                    f"Book/Page: {book_page}\n"
                    f"Legal: {legal}"
                )
                if doc_link:
                    row_text += f"\nDoc Link: {doc_link}"
                results.append((rec_date, row_text))

        logger.info("DuvalClerk: CSV export yielded %d records", len(results))
        return results

    except Exception as exc:
        logger.warning("DuvalClerk: CSV export failed (%s) — falling back to table", exc)
        return None


async def _extract_index_rows(page: Page) -> list[tuple[str, str]]:
    """Extract (recorded_date, row_text) tuples from the OR results page.

    Tries CSV export first (gets all records at once). Falls back to HTML
    table scraping if the export button is absent or fails.

    Verified table column order (2026-06-11):
      R# | First Direct Name | First Indirect Name | Instrument # |
      Record Date | Doc Type | Book Type | Book/Page | Doc Link |
      Consideration | Legal | DeletedAfter
    """
    # Prefer CSV — avoids pagination entirely
    csv_rows = await _try_export_csv(page)
    if csv_rows is not None:
        return csv_rows

    # Fallback: scrape the Kendo grid HTML table
    # The Kendo grid renders as a <table> inside a div.k-grid-content
    table_selectors = [
        ".k-grid-content table",
        ".k-grid table",
        "table[role='grid']",
        "table",
    ]
    result_tbl = None
    for sel in table_selectors:
        tbl = await page.query_selector(sel)
        if tbl:
            result_tbl = tbl
            logger.debug("DuvalClerk: found results table via '%s'", sel)
            break

    if not result_tbl:
        logger.warning("DuvalClerk: results table not found and CSV export unavailable")
        return []

    rows = await result_tbl.query_selector_all("tr")
    results: list[tuple[str, str]] = []
    for row in rows[1:]:  # row 0 is the header
        try:
            cells = await row.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [(await c.inner_text()).replace("\xa0", " ").strip() for c in cells]

            # Column order (verified 2026-06-12):
            # 0:R# | 1:DirectName (plaintiff) | 2:IndirectName (defendant/owner) |
            # 3:InstrumentNumber | 4:RecordDate | 5:DocTypeDescription | 6:BookType |
            # 7:BookPage | 8:DocLink | 9:Consideration | 10:DocLegalDescription | 11:DeletedAfterVerify
            plaintiff    = texts[1] if len(texts) > 1 else ""
            defendant    = texts[2] if len(texts) > 2 else ""
            instrument   = texts[3] if len(texts) > 3 else ""
            rec_date_raw = texts[4].split(" ")[0] if len(texts) > 4 else ""  # strip time
            book_page    = texts[7] if len(texts) > 7 else ""
            legal        = texts[10] if len(texts) > 10 else ""

            # Capture the actual href from the DocLink cell's <a> tag
            doc_link = ""
            if len(cells) > 8:
                link_el = await cells[8].query_selector("a")
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    if href:
                        if not href.startswith("http"):
                            href = f"{DUVAL_CLERK_BASE_URL}/{href.lstrip('/')}"
                        doc_link = href

            if not defendant or "no results" in defendant.lower():
                continue

            rec_date = _norm_date(rec_date_raw) if rec_date_raw else ""
            row_text = (
                f"Grantor: {defendant}\n"
                f"Grantee: {plaintiff}\n"
                f"Instrument #: {instrument}\n"
                f"Record Date: {rec_date_raw}\n"
                f"Book/Page: {book_page}\n"
                f"Legal: {legal}"
            )
            if doc_link:
                row_text += f"\nDoc Link: {doc_link}"
            results.append((rec_date, row_text))
        except Exception:
            continue

    logger.debug("DuvalClerk: extracted %d rows from HTML table", len(results))
    return results


async def _get_result_count(page: Page) -> int | None:
    """Extract total result count from the page (e.g. 'Showing 1-25 of 142')."""
    try:
        body = await page.inner_text("body")
        for pat in [
            r"(?:Showing|Displaying)\s+\d+\s*[-–]\s*\d+\s+of\s+(\d+)",
            r"(\d+)\s+(?:result|record|instrument)s?\s+found",
            r"Total\s+(?:Records|Results)\s*[:\s]+(\d+)",
        ]:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


async def _click_next_page(page: Page) -> bool:
    """Click the Next pagination control. Returns True on success."""
    next_selectors = [
        "a:has-text('Next')",
        "a[title='Next Page']",
        "input[value='Next']",
        "button:has-text('Next')",
        ".pagination a:last-child",
        "a[rel='next']",
    ]
    for sel in next_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                disabled = await el.get_attribute("disabled")
                aria_disabled = await el.get_attribute("aria-disabled")
                if disabled or aria_disabled == "true":
                    return False
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=15_000)
                await _delay()
                return True
        except Exception:
            continue
    return False


# ── Per-search scraping ───────────────────────────────────────────────


async def _scrape_duval_clerk_search(
    page: Page,
    search: SavedSearch,
    since_date: str | None,
    seen_ids: dict[str, str],
    llm_api_key: str | None,
) -> list[NoticeData]:
    """Run one lis pendens date-range search and return all NoticeData."""
    logger.info(
        "DuvalClerk scraping: county=%s type=%s since=%s",
        search.county, search.notice_type, since_date or "all",
    )

    today = datetime.now().strftime("%Y-%m-%d")
    start = since_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    ok = await _submit_search_form(page, start, today)
    if not ok:
        logger.error("DuvalClerk form submission failed")
        return []

    notices: list[NoticeData] = []
    page_num = 0

    while True:
        page_num += 1
        total = await _get_result_count(page)
        logger.info(
            "  DuvalClerk page %d — total results: %s",
            page_num, str(total) if total is not None else "?",
        )

        rows = await _extract_index_rows(page)
        if not rows:
            if page_num == 1:
                logger.info(
                    "  DuvalClerk %s/%s: no rows on page 1 — "
                    "no results for this date range or selectors need updating",
                    search.county, search.notice_type,
                )
            break

        logger.info("  Parsing %d rows from page %d", len(rows), page_num)
        seq_base = (page_num - 1) * RESULTS_PER_PAGE

        # First pass: filter + parse all new rows
        pending: list[tuple[str, NoticeData, str]] = []
        skipped_non_mortgage = 0
        for i, (rec_date, row_text) in enumerate(rows):
            # Extract defendant (property owner) and plaintiff (lender) for filter + dedup
            # Grantor: line = defendant (property owner); Grantee: line = plaintiff (lender)
            gm = LP_GRANTOR_RE.search(row_text)
            bm = LP_BOOK_PAGE_RE.search(row_text)
            defendant = _clean(gm.group(1)) if gm else row_text[:40]

            plaintiff_m = re.search(r"^Grantee:\s*(.+)$", row_text, re.MULTILINE)
            plaintiff = _clean(plaintiff_m.group(1)) if plaintiff_m else ""

            if not _is_mortgage_preforeclosure(plaintiff, defendant):
                skipped_non_mortgage += 1
                logger.debug(
                    "  Skipping non-mortgage LP: defendant=%s plaintiff=%s",
                    defendant[:40], plaintiff[:40],
                )
                continue

            book    = bm.group(1) if bm else ""
            page_no = bm.group(2) if bm else ""
            nhash   = _notice_hash(defendant, book, page_no, rec_date)

            if nhash in seen_ids:
                logger.debug("  Skipping seen record hash=%s", nhash)
                continue

            effective_date = rec_date or start
            if since_date and effective_date and effective_date < since_date:
                logger.debug(
                    "  Skipping old record (date=%s < since=%s)", effective_date, since_date
                )
                continue

            notice = _parse_lp_row(row_text, search, effective_date or today, seq_base + i + 1)
            pending.append((nhash, notice, row_text))

        if skipped_non_mortgage:
            logger.info(
                "  Filtered out %d non-mortgage LP rows (HOA/business grantor)",
                skipped_non_mortgage,
            )

        # Second pass: LLM for rows missing address
        if llm_api_key and pending:
            from llm_parser import extract_with_llm
            _sem = asyncio.Semaphore(10)

            async def _llm_one(row_text: str, notice: NoticeData) -> dict:
                if notice.address:
                    return {}
                async with _sem:
                    try:
                        return await extract_with_llm(
                            row_text, "lis_pendens", search.county, llm_api_key, state="FL"
                        )
                    except Exception as exc:
                        logger.debug("  LLM fallback failed: %s", exc)
                        return {}

            llm_results = await asyncio.gather(
                *[_llm_one(rt, n) for _, n, rt in pending]
            )
            for (nhash, notice, _), result in zip(pending, llm_results):
                if result and not notice.address and result.get("address"):
                    city = result.get("city", "")
                    if not _is_office_address(result["address"], city):
                        notice.address = result["address"]
                        notice.city    = city
                        notice.zip     = result.get("zip", "")
                seen_ids[nhash] = notice.date_added or today
                logger.debug(
                    "  Parsed: owner=%s addr=%s",
                    (notice.owner_name or "?")[:35],
                    (notice.address or "NO ADDR — needs PA lookup")[:45],
                )
                notices.append(notice)
        else:
            for nhash, notice, _ in pending:
                seen_ids[nhash] = notice.date_added or today
                notices.append(notice)

        if not await _click_next_page(page):
            break

    logger.info(
        "  DuvalClerk %s/%s: %d records collected",
        search.county, search.notice_type, len(notices),
    )
    return notices


# ── Main entry point ──────────────────────────────────────────────────


async def scrape_duval_clerk_all(
    searches: list[SavedSearch],
    since_date: str | None = None,
    seen_ids: dict[str, str] | None = None,
    llm_api_key: str | None = None,
    proxy_url: str | None = None,
) -> list[NoticeData]:
    """Scrape all duval_clerk saved searches and return combined NoticeData.

    Args:
        searches:    SavedSearch entries with source="duval_clerk".
        since_date:  ISO date string (YYYY-MM-DD); only records on/after this date.
        seen_ids:    Cross-run dedup dict {record_hash: date}; updated in-place.
        llm_api_key: Anthropic API key for LLM fallback on missing address fields.
        proxy_url:   Optional proxy server URL (e.g. Apify residential proxy).

    Returns:
        List of NoticeData for Duval County lis pendens with state="FL",
        notice_type="lis_pendens". Property address may be empty for records
        with no street address in the OR index — enrichment pipeline fills
        via Duval County Property Appraiser name lookup.
    """
    if seen_ids is None:
        seen_ids = {}

    dc_only = [s for s in searches if s.source == "duval_clerk"]
    if not dc_only:
        return []

    logger.info(
        "DuvalClerk: starting %d search(es): %s",
        len(dc_only),
        ", ".join(f"{s.county}/{s.notice_type}" for s in dc_only),
    )

    all_notices: list[NoticeData] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                # Prevent sites from detecting headless Chrome via automation flags
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        # No proxy for Duval Clerk — or.duvalclerk.com is a public government
        # portal that loads fine from datacenter IPs. Routing through the
        # residential proxy caused domcontentloaded to time out (30s+).
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # Remove navigator.webdriver flag so Kendo UI doesn't see a bot
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        context.set_default_timeout(30_000)
        page = await context.new_page()

        for search in dc_only:
            try:
                batch = await _scrape_duval_clerk_search(
                    page, search, since_date, seen_ids, llm_api_key
                )
                all_notices.extend(batch)
            except Exception:
                logger.exception(
                    "DuvalClerk scrape failed for %s/%s", search.county, search.notice_type
                )

        await browser.close()

    logger.info(
        "DuvalClerk complete: %d total records across %d search(es)",
        len(all_notices), len(dc_only),
    )
    return all_notices
