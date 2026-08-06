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

# Case number from the notice text (FL UCN format: 16-2026-CA-004689-AXXX-MA —
# county-2-digit prefix, year, case type, sequence, then division/subtype
# suffixes that can repeat, e.g. "-AXXX-MA"). The Clerk's recording stamp puts
# the UCN on its own line near the top with NO "Case No." label at all — the
# actual "Case No." line later in the document body is usually a blank
# template field — so the label prefix must be optional, not required.
# The leading "16-" (county prefix) hyphen is a common OCR dropout at the
# very start of the stamped line (e.g. "162026-CA-004751-AXXX-MA") — captured
# as a separate group so the caller can always reassemble a normalized UCN
# regardless of whether OCR preserved that hyphen.
LP_CASE_NO_RE = re.compile(
    r"(?:Case\s+(?:No\.?|Number|#)?\s*[:\s]*)?"
    r"\b([0-9]{2})-?([0-9]{4}-CA-[0-9]+(?:-[A-Z0-9]+)*)\b",
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

# Generic "<Development Name> Association, Inc." catch-all — FL HOAs/condo
# associations are almost universally incorporated this way (e.g. "Sandpiper
# Association, Inc."), but the name itself is the development's name, not a
# recognizable HOA keyword, so the enumerated regex above misses it. Real
# mortgage lenders are never registered simply as "___ Association, Inc."
_GENERIC_ASSOCIATION_INC_RE = re.compile(
    r"\bASSOCIATION,?\s+INC\b", re.IGNORECASE,
)


def _plaintiff_is_mortgage_lender(plaintiff: str) -> bool:
    """Cheap pre-filter on the plaintiff (DirectName) alone: True unless the
    plaintiff itself is an HOA/condo association or contractor lien-filer.

    Deliberately does NOT check the defendant name — used to gate the OCR
    document fetch, before doc_fields exists. The defendant-business check
    lives in `_is_mortgage_preforeclosure` and is checked separately (with
    OCR ground truth available to override it) once the document has been
    fetched.
    """
    if _NON_MORTGAGE_PLAINTIFF_RE.search(plaintiff):
        return False
    if _GENERIC_ASSOCIATION_INC_RE.search(plaintiff):
        return False
    return True


def _is_mortgage_preforeclosure(plaintiff: str, defendant: str, doc_fields: dict | None = None) -> bool:
    """Return True only if this LP row looks like a mortgage preforeclosure.

    IMPORTANT — OR index column mapping (verified 2026-06-12):
      DirectName   = plaintiff  (lender filing the suit — bank, credit union, etc.)
      IndirectName = defendant  (property owner = our target contact)

    Two rules:
      1. Defendant (IndirectName / property owner) must be a person, not a business.
      2. Plaintiff (DirectName) must not be an HOA, condo association, or contractor.

    Rule 1 has a documented blind spot: the OR index only exposes a single
    "First Indirect Name" per filing, which for multi-defendant mortgage
    cases can be a co-defendant lienholder (HOA, hospital, etc.) rather than
    the actual homeowner. If `doc_fields` (OCR of the actual recorded
    document) confirms mortgage-foreclosure language, that overrides the
    business-name-defendant rejection.
    """
    from config import BUSINESS_RE
    if not _plaintiff_is_mortgage_lender(plaintiff):
        return False
    if BUSINESS_RE.search(defendant):
        if doc_fields and doc_fields.get("mortgage_confirmed"):
            return True
        return False
    return True


# ── Ground-truth document fetch + OCR ────────────────────────────────
#
# The OR index row never contains a property address (verified live). The
# actual recorded lis pendens document does — but it's a scanned image PDF
# behind an opaque `docId` token that only exists once you click the row in
# the live grid (it is NOT derivable from instrument #/book/page). So this
# must run while the row's DOM element is still valid, during the same grid
# page as `_extract_index_rows` — not as a later enrichment-tier fallback.

_LP_DOC_PARCEL_RE = re.compile(
    r"Parcel\s+(?:Identification\s+Number|ID)\s*:?\s*([\d]{3,}-?[\d]{2,})", re.IGNORECASE
)
# Real Duval lis pendens filings label the property line "COMMONLY KNOWN AS:",
# not "Property Address:" — verified against live OCR output (2026-07-08).
# Keep both since a "Property Address:" phrasing may appear in other filers'
# templates.
_LP_DOC_ADDRESS_RE = re.compile(
    r"(?:Property\s+Address|Commonly\s+Known\s+As|a/?k/?a)\s*:?\s*(.+?)(?:\n|$)", re.IGNORECASE
)
# Nature-of-action language that marks an HOA/condo LIEN case, not a mortgage
# preforeclosure — a defense-in-depth check for plaintiff names the pre-filter
# regexes above don't catch (e.g. a development named "Foo Association, Inc."
# with unusual formatting, or a differently-worded HOA plaintiff).
#
# Deliberately does NOT include a bare "HOMEOWNERS ASSOCIATION" / "CONDOMINIUM
# ASSOCIATION" phrase — FL mortgage foreclosures routinely join the property's
# HOA/condo association as a co-DEFENDANT (to subordinate its lien in the
# sale), so that phrase alone appears in plenty of legitimate mortgage cases
# and produced false positives (e.g. "PEREGRINE MEADOWS HOMEOWNERS
# ASSOCIATION, INC." as a defendant, verified 2026-07-08). Only match language
# that specifically describes the ASSOCIATION bringing its own lien claim.
_LP_DOC_HOA_RE = re.compile(
    r"CLAIM\s+OF\s+LIEN|OWNER.?S\s+ASSESSMENTS|ASSESSMENTS?\s+AND\s+COLLECTION\s+COSTS",
    re.IGNORECASE,
)

# Positive confirmation the recorded document is a mortgage foreclosure.
# FL lis pendens notices for mortgage foreclosures use this statutory
# phrasing ("...seeking to foreclose a mortgage on the following real
# property..."). Used to override the defendant-business-name heuristic:
# the county's OR index only exposes a single "First Indirect Name" per
# filing, which can land on a co-defendant lienholder (HOA, hospital, etc.)
# instead of the actual homeowner when a case has multiple defendants
# (verified 2026-07-14 — "Lakeview Loan Servicing, LLC v. Servis" case had
# Shands Jacksonville Medical Center Inc. and Wells Creek West Homeowners
# Association, Inc. as co-defendants alongside the actual homeowners).
_LP_DOC_MORTGAGE_RE = re.compile(r"foreclose\s+a\s+mortgage", re.IGNORECASE)


def _parse_lp_document_text(ocr_text: str) -> dict:
    """Extract property address / parcel ID / case number / HOA-lien signal
    from OCR'd LP document text.

    Returns a dict with any of: address, city, zip, parcel_id, case_number,
    hoa_lien. Missing/unparseable fields are simply omitted — caller merges
    what it can.
    """
    result: dict = {}

    cm = LP_CASE_NO_RE.search(ocr_text)
    if cm:
        result["case_number"] = f"{cm.group(1)}-{cm.group(2)}".strip().upper()

    pm = _LP_DOC_PARCEL_RE.search(ocr_text)
    if pm:
        result["parcel_id"] = pm.group(1).strip()

    am = _LP_DOC_ADDRESS_RE.search(ocr_text)
    if am:
        addr_line = re.sub(r"\s+", " ", am.group(1)).strip().rstrip(".")
        # Common OCR confusion: "1st" often reads as "Ist"/"lst".
        addr_line = re.sub(r"\b[Il]st\b", "1st", addr_line)
        m2 = re.match(
            r"(.+?),\s*([\w\s]+?),\s*(?:FL|Florida)\.?\s*(\d{5}(?:-\d{4})?)?",
            addr_line, re.IGNORECASE,
        )
        if m2:
            result["address"] = m2.group(1).strip()
            result["city"] = m2.group(2).strip()
            if m2.group(3):
                result["zip"] = m2.group(3)
        elif addr_line:
            result["address"] = addr_line

    if _LP_DOC_HOA_RE.search(ocr_text):
        result["hoa_lien"] = True

    if _LP_DOC_MORTGAGE_RE.search(ocr_text):
        result["mortgage_confirmed"] = True

    return result


async def _fetch_lp_details(page: Page, instrument_cell) -> dict:
    """Click an OR index row's instrument-number cell to open its Details tab.

    Returns {"pdf_bytes": bytes|None, "case_number": str}. The case number is
    read directly from the Details page's own structured metadata panel
    (a "CaseNumber:" label/value pair, verified 2026-07-08) — that field is
    populated even when the document image itself shows "Image Not
    Available", so it's available strictly more often than OCR and doesn't
    depend on image quality at all.
    """
    result: dict = {"pdf_bytes": None, "case_number": ""}
    new_page = None
    try:
        async with page.context.expect_page(timeout=8_000) as new_page_info:
            await instrument_cell.click()
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded", timeout=15_000)
        await new_page.wait_for_timeout(2_000)

        case_no_cell = new_page.locator(
            "div.docDetailRow:has(div.detailLabel:text-is('CaseNumber:')) div.listDocDetails"
        )
        if await case_no_cell.count() > 0:
            case_text = (await case_no_cell.first.inner_text()).strip().splitlines()[0].strip()
            if case_text:
                result["case_number"] = case_text.upper()

        iframe = new_page.locator("iframe").first
        if await iframe.count() == 0:
            return result
        iframe_src = await iframe.get_attribute("src")
        if not iframe_src or "DocumentImage1" not in iframe_src:
            return result
        pdf_url = iframe_src.replace("DocumentImage1", "DocumentPdfAllPages")

        resp = await new_page.request.get(pdf_url)
        if resp.status == 200:
            result["pdf_bytes"] = await resp.body()
        return result
    except Exception as e:
        logger.debug("  LP details fetch failed: %s", e)
        return result
    finally:
        if new_page is not None:
            try:
                await new_page.close()
            except Exception:
                pass


async def _fetch_and_ocr_lp_document(page: Page, instrument_cell) -> dict:
    """Fetch the actual recorded LP document's details + image, and OCR the
    image for ground-truth property address / parcel ID. Returns whatever it
    could get — at minimum the Details-page case_number even if the image
    is unavailable or OCR fails entirely; callers fall back to the existing
    DCPA name-lookup tier for anything still missing.
    """
    details = await _fetch_lp_details(page, instrument_cell)
    base: dict = {}
    if details["case_number"]:
        base["case_number"] = details["case_number"]

    pdf_bytes = details["pdf_bytes"]
    if not pdf_bytes:
        return base
    try:
        from image_utils import fix_rotation, ocr_page, render_pdf_bytes
        images = render_pdf_bytes(pdf_bytes, dpi=300)
        if not images:
            return base
        img = fix_rotation(images[0])
        ocr_text = ocr_page(img, psm=3)
        parsed = _parse_lp_document_text(ocr_text)
        # The Details-page case number is structured data, not an OCR guess —
        # prefer it over whatever (possibly OCR-garbled) case number the text
        # scan found.
        if base.get("case_number"):
            parsed["case_number"] = base["case_number"]
        return parsed
    except Exception as e:
        logger.debug("  LP document OCR failed: %s", e)
        return base


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


async def _extract_index_rows(page: Page) -> list[tuple[str, str, dict]]:
    """Extract (recorded_date, row_text, doc_fields) tuples from the OR results page.

    Uses HTML table scraping as primary. The CSV export is limited to records
    at or below the "Released through Instrument Number" shown in the site banner,
    which lags 3-4 days behind current filings. The HTML table returns ALL records
    including those past the release threshold, so recently-filed LP records are
    captured without waiting for the official release cycle.

    Falls back to CSV export only if the HTML table yields nothing.

    Verified table column order (2026-06-11):
      R# | First Direct Name | First Indirect Name | Instrument # |
      Record Date | Doc Type | Book Type | Book/Page | Doc Link |
      Consideration | Legal | DeletedAfter

    For each row that looks like a real mortgage preforeclosure, also clicks
    through to the actual recorded document and OCRs it for ground-truth
    property address / parcel ID / HOA-lien signal (doc_fields — see
    `_fetch_and_ocr_lp_document`). Only possible from this HTML-table path:
    the CSV export has no interactive rows to click, so those get {}.
    """
    # Primary: scrape the Kendo grid HTML table (includes unreleased records)
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
        logger.info("DuvalClerk: HTML table not found — falling back to CSV export")
        csv_rows = await _try_export_csv(page)
        return [(d, t, {}) for d, t in (csv_rows or [])]

    rows = await result_tbl.query_selector_all("tr")
    results: list[tuple[str, str, dict]] = []
    doc_fetch_count = 0
    # NOTE: `.k-grid-content table` is Kendo's scrollable grid-BODY table —
    # its header lives in a separate `.k-grid-header` table, so every <tr>
    # here is already a data row (verified live 2026-07-14: a 5-row search
    # returned exactly 5 <tr>, tr[0] being real data, not a header). A prior
    # version of this code did `rows[1:]` assuming row 0 was a header, which
    # silently dropped the first record of every single search. Any genuine
    # <th>-based header row (e.g. if a fallback `table` selector above ever
    # matches a full grid incl. header) is already filtered out below by the
    # `len(cells) < 5` check, since `query_selector_all("td")` won't match
    # <th> cells — so no manual row-skip is needed here.
    for row in rows:
        try:
            cells = await row.query_selector_all("td")
            if len(cells) < 5:
                continue
            texts = [(await c.inner_text()).replace("\xa0", " ").strip() for c in cells]

            # Kendo grid inserts a hidden checkbox/select cell at index 0, shifting
            # all visible columns right by 1. Detect this by checking whether texts[4]
            # matches a date (M/D/YYYY). If not, assume the +1 offset applies.
            # Visual column order: R# | DirectName | IndirectName | Instrument# | RecordDate | ...
            _DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}")
            _offset = 0
            if len(texts) > 4 and not _DATE_RE.match(texts[4]):
                # texts[4] is not a date — hidden column present, shift indices +1
                _offset = 1

            plaintiff    = texts[1 + _offset] if len(texts) > 1 + _offset else ""
            defendant    = texts[2 + _offset] if len(texts) > 2 + _offset else ""
            instrument   = texts[3 + _offset] if len(texts) > 3 + _offset else ""
            rec_date_raw = texts[4 + _offset].split(" ")[0] if len(texts) > 4 + _offset else ""
            book_page    = texts[7 + _offset] if len(texts) > 7 + _offset else ""
            legal        = texts[10 + _offset] if len(texts) > 10 + _offset else ""

            # Capture the actual href from the DocLink cell's <a> tag
            doc_link = ""
            if len(cells) > 8 + _offset:
                link_el = await cells[8 + _offset].query_selector("a")
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

            # Ground-truth document fetch — gated on the plaintiff alone (cheap
            # pre-filter avoids wasting a click+download+OCR round-trip on
            # obvious HOA/contractor lien rows, which get dropped later in the
            # caller anyway). Deliberately NOT gated on the defendant-business
            # check too: the index's single "First Indirect Name" can be a
            # co-defendant lienholder rather than the actual homeowner on
            # multi-defendant mortgage cases, so those rows still need OCR to
            # get a fair shot at the mortgage_confirmed override below.
            doc_fields: dict = {}
            if _plaintiff_is_mortgage_lender(plaintiff) and len(cells) > 3 + _offset:
                doc_fields = await _fetch_and_ocr_lp_document(page, cells[3 + _offset])
                if doc_fields:
                    doc_fetch_count += 1

            results.append((rec_date, row_text, doc_fields))
        except Exception:
            continue

    logger.info("DuvalClerk: extracted %d rows from HTML table (includes unreleased records)", len(results))
    if doc_fetch_count:
        logger.info("DuvalClerk: fetched+OCR'd %d/%d recorded documents for ground-truth address/parcel", doc_fetch_count, len(results))
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


async def _parse_released_through_date(page: Page) -> str | None:
    """Extract 'Released through date' from the OR index page banner.

    The banner reads: "Released through date: MM/DD/YYYY | ..."
    Returns ISO date string (YYYY-MM-DD) or None if not found.
    """
    try:
        body = await page.inner_text("body")
        m = re.search(r"Released through date:\s*(\d{1,2}/\d{1,2}/\d{4})", body)
        if m:
            return _norm_date(m.group(1))
    except Exception:
        pass
    return None


async def _set_kendo_date_via_js(page: Page, name: str, value_mdy: str) -> bool:
    """Set a Kendo DatePicker via its JavaScript widget API.

    Kendo DatePicker stores its internal state in a JS widget object separate
    from the HTML input value.  Plain Playwright fill() + Tab updates the visible
    input but may not trigger the widget's internal state update, so a subsequent
    Search click uses the old (or empty) date.  This function uses the Kendo JS
    API directly to set the widget value and fires the change event so the Search
    button picks up the new date.
    """
    result = await page.evaluate("""
        (args) => {
            const input = document.querySelector(args.sel);
            if (!input) return 'no-input';

            // Try Kendo widget instance via jQuery .data() (most reliable)
            if (window.$ && $(input).data) {
                try {
                    const kw = $(input).data('kendoDatePicker');
                    if (kw) {
                        kw.value(new Date(args.val));
                        kw.trigger('change');
                        return 'kendo-api-' + (kw.value() ? kw.value().toLocaleDateString() : 'null');
                    }
                } catch(e) {}
            }

            // Try kendo.widgetInstance() fallback
            if (window.kendo && kendo.widgetInstance) {
                try {
                    const kw = kendo.widgetInstance(input);
                    if (kw) {
                        kw.value(new Date(args.val));
                        kw.trigger('change');
                        return 'kendo-instance-ok';
                    }
                } catch(e) {}
            }

            // Last resort: native setter + bubbling events (React/Kendo hybrid)
            try {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(input, args.val);
                input.dispatchEvent(new Event('input',  {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                return 'native-setter';
            } catch(e) {
                return 'error: ' + e.message;
            }
        }
    """, {"sel": f"input[name='{name}']", "val": value_mdy})
    logger.debug("DuvalClerk: _set_kendo_date_via_js name=%s val=%s → %s", name, value_mdy, result)
    return not (result or "").startswith("error") and result != "no-input"


async def _resubmit_search_dates(page: Page, start: str, end: str) -> bool:
    """Update date inputs on the current results page and re-click Search.

    Uses Kendo's JS widget API to update DatePicker internal state so the
    Search button uses the new dates, not just the visible input value.
    """
    try:
        start_mdy = _to_mdy(start)
        end_mdy   = _to_mdy(end)

        # Set dates via Kendo JS API (most reliable) + visible input fallback
        for name, val in [("RecordDateFrom", start_mdy), ("RecordDateTo", end_mdy)]:
            js_ok = await _set_kendo_date_via_js(page, name, val)
            if not js_ok:
                # Fallback: Playwright fill + Tab
                el = page.locator(f"input[name='{name}']").first
                await el.click(click_count=3)
                await el.fill(val)
                await el.press("Tab")
            logger.debug("DuvalClerk: set %s = %s (js_ok=%s)", name, val, js_ok)

        await page.wait_for_timeout(300)

        # Re-click Search
        for btn_sel in ["button:has-text('Search')", "input[type='submit'][value='Search']"]:
            try:
                await page.click(btn_sel, timeout=3_000)
                break
            except Exception:
                continue

        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PwTimeout:
            pass
        await _delay()

        # Log page snippet so we can diagnose whether results changed
        try:
            snippet = (await page.inner_text("body"))[:300].replace("\n", " ").strip()
            logger.info("DuvalClerk: resubmit %s→%s page text: %s", start, end, snippet)
        except Exception:
            pass

        return True
    except Exception as exc:
        logger.warning("DuvalClerk: resubmit failed: %s", exc)
        return False


async def _collect_rows_from_search(
    page: Page, start: str, end: str, navigate: bool = True
) -> list[tuple[str, str, dict]]:
    """Submit a search for [start, end] and return all (rec_date, row_text, doc_fields) tuples.

    navigate=True (default): calls _submit_search_form which does page.goto first.
    navigate=False: stays on the current results page and just updates date inputs.
    """
    if navigate:
        ok = await _submit_search_form(page, start, end)
    else:
        ok = await _resubmit_search_dates(page, start, end)
    if not ok:
        return []
    all_rows: list[tuple[str, str, dict]] = []
    page_num = 0
    while True:
        page_num += 1
        rows = await _extract_index_rows(page)
        if not rows:
            break
        all_rows.extend(rows)
        if not await _click_next_page(page):
            break
    return all_rows


async def _scrape_duval_clerk_search(
    page: Page,
    search: SavedSearch,
    since_date: str | None,
    seen_ids: dict[str, str],
    llm_api_key: str | None,
    last_released_through: str | None = None,
) -> tuple[list[NoticeData], str | None]:
    """Run one lis pendens date-range search and return (notices, released_through).

    Strategy: the Duval Clerk's date-range search only returns officially
    "released" records (up to the Released-through Instrument Number). Records
    filed in the last 3-5 days are visible on the site but excluded from searches
    until the Clerk processes them and advances the released-through date.

    Catch-up logic: if `last_released_through` is supplied and the current
    released-through date has advanced, we run an additional search for the
    newly-released period (last_released_through+1 → current_released_through).
    This ensures records that became released since the last run are captured.
    """
    logger.info(
        "DuvalClerk scraping: county=%s type=%s since=%s",
        search.county, search.notice_type, since_date or "all",
    )

    today = datetime.now().strftime("%Y-%m-%d")
    start = since_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    # ── Main range search (released records in [start, today]) ───────
    all_rows: list[tuple[str, str, dict]] = await _collect_rows_from_search(page, start, today)

    # Parse the released-through date so we can persist it and detect changes.
    released_through = await _parse_released_through_date(page)
    if released_through:
        logger.info("  DuvalClerk: Released-through date = %s", released_through)

    # ── Catch-up: newly-released records from the previous gap ───────
    # If the released-through date has advanced since our last run, records
    # that were previously unreleased (and thus invisible to date-range
    # searches) may now be accessible.  Run a supplemental search for the
    # newly-released window as long as it doesn't overlap our main range
    # (which already covers start→today).
    if released_through and last_released_through and released_through > last_released_through:
        catch_start = (
            datetime.strptime(last_released_through, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        catch_end = released_through
        # Both ends of the catch-up window must precede the main range's start —
        # if catch_end reaches into [start, today], the main search (which already
        # includes unreleased records via the HTML table) has covered that overlap
        # already, and re-fetching it here would duplicate those rows.
        if catch_start < start and catch_end < start:
            logger.info(
                "  DuvalClerk: catch-up search for newly-released records %s → %s",
                catch_start, catch_end,
            )
            catch_rows = await _collect_rows_from_search(
                page, catch_start, catch_end, navigate=False
            )
            if catch_rows:
                sample_dates = sorted({r[0] for r in catch_rows if r[0]})
                logger.info(
                    "  DuvalClerk: catch-up returned %d rows (dates: %s)",
                    len(catch_rows), sample_dates[:5],
                )
                all_rows.extend(catch_rows)

    notices: list[NoticeData] = []

    if not all_rows:
        logger.info(
            "  DuvalClerk %s/%s: no rows found — "
            "no results for this date range or selectors need updating",
            search.county, search.notice_type,
        )

    logger.info("  Parsing %d total rows", len(all_rows))

    # First pass: filter + parse all rows
    pending: list[tuple[str, NoticeData, str]] = []
    skipped_non_mortgage = 0
    skipped_hoa_doc = 0
    doc_matched = 0
    # Tracks hashes already queued in `pending` during this loop. `seen_ids`
    # only gets its entries added *after* this whole pass finishes (below), so
    # two identical rows within the same `all_rows` batch (e.g. from an
    # overlapping catch-up search or a pagination re-fetch) would otherwise
    # both pass the `nhash in seen_ids` check and both end up in the output.
    batch_seen: set[str] = set()
    for i, (rec_date, row_text, doc_fields) in enumerate(all_rows):
        gm = LP_GRANTOR_RE.search(row_text)
        bm = LP_BOOK_PAGE_RE.search(row_text)
        defendant = _clean(gm.group(1)) if gm else row_text[:40]

        plaintiff_m = re.search(r"^Grantee:\s*(.+)$", row_text, re.MULTILINE)
        plaintiff = _clean(plaintiff_m.group(1)) if plaintiff_m else ""

        if not _is_mortgage_preforeclosure(plaintiff, defendant, doc_fields):
            skipped_non_mortgage += 1
            logger.debug(
                "  Skipping non-mortgage LP: defendant=%s plaintiff=%s",
                defendant[:40], plaintiff[:40],
            )
            continue

        # Defense-in-depth: the recorded document itself states the nature of
        # the action. If OCR found HOA/condo-lien language, drop the record
        # even though the plaintiff-name pre-filter let it through (catches
        # generically-named associations the name regex can't recognize).
        if doc_fields.get("hoa_lien"):
            skipped_hoa_doc += 1
            logger.info(
                "  Skipping HOA/condo lien LP (doc text confirms non-mortgage): defendant=%s",
                defendant[:40],
            )
            continue

        book    = bm.group(1) if bm else ""
        page_no = bm.group(2) if bm else ""
        nhash   = _notice_hash(defendant, book, page_no, rec_date)

        if nhash in seen_ids or nhash in batch_seen:
            logger.info("  Skipping seen: defendant=%s date=%s hash=%s", defendant[:30], rec_date, nhash[:8])
            continue
        batch_seen.add(nhash)

        effective_date = rec_date or start
        if since_date and effective_date and effective_date < since_date:
            logger.info(
                "  Skipping old record: defendant=%s date=%s < since=%s", defendant[:30], effective_date, since_date
            )
            continue

        notice = _parse_lp_row(row_text, search, effective_date or today, i + 1)

        # Ground-truth address/parcel from the actual recorded document (OCR)
        # takes priority over anything the index-row regex parser found.
        if doc_fields.get("address"):
            notice.address = doc_fields["address"]
            notice.city    = doc_fields.get("city") or notice.city
            if doc_fields.get("zip"):
                notice.zip = doc_fields["zip"]
            doc_matched += 1
        if doc_fields.get("parcel_id"):
            notice.parcel_id = doc_fields["parcel_id"]
        if doc_fields.get("case_number"):
            notice.case_number = doc_fields["case_number"]

        pending.append((nhash, notice, row_text))

    if skipped_non_mortgage:
        logger.info(
            "  Filtered out %d non-mortgage LP rows (HOA/business grantor)",
            skipped_non_mortgage,
        )
    if skipped_hoa_doc:
        logger.info(
            "  Filtered out %d additional HOA/condo lien LP rows (confirmed via document OCR)",
            skipped_hoa_doc,
        )
    if doc_matched:
        logger.info(
            "  Ground-truth address/parcel extracted from recorded document for %d/%d records",
            doc_matched, len(pending),
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

    logger.info(
        "  DuvalClerk %s/%s: %d records collected",
        search.county, search.notice_type, len(notices),
    )
    return notices, released_through


# ── Main entry point ──────────────────────────────────────────────────


async def scrape_duval_clerk_all(
    searches: list[SavedSearch],
    since_date: str | None = None,
    seen_ids: dict[str, str] | None = None,
    llm_api_key: str | None = None,
    proxy_url: str | None = None,
    last_released_through: str | None = None,
) -> tuple[list[NoticeData], str | None]:
    """Scrape all duval_clerk saved searches and return (notices, released_through).

    Args:
        searches:               SavedSearch entries with source="duval_clerk".
        since_date:             ISO date string (YYYY-MM-DD); only records on/after.
        seen_ids:               Cross-run dedup dict {hash: date}; updated in-place.
        llm_api_key:            Anthropic API key for LLM address fallback.
        proxy_url:              Optional proxy URL (unused — see note in code).
        last_released_through:  Released-through date from the previous run (KVS).
                                When set and the current released-through has advanced,
                                a catch-up search is run for the newly-released window.

    Returns:
        (notices, current_released_through) — caller should persist
        current_released_through to KVS as "last_released_through_date".
    """
    if seen_ids is None:
        seen_ids = {}

    dc_only = [s for s in searches if s.source == "duval_clerk"]
    if not dc_only:
        return [], None

    logger.info(
        "DuvalClerk: starting %d search(es): %s",
        len(dc_only),
        ", ".join(f"{s.county}/{s.notice_type}" for s in dc_only),
    )

    all_notices: list[NoticeData] = []
    current_released_through: str | None = None

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
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        context.set_default_timeout(30_000)
        page = await context.new_page()

        for search in dc_only:
            try:
                batch, rt = await _scrape_duval_clerk_search(
                    page, search, since_date, seen_ids, llm_api_key,
                    last_released_through=last_released_through,
                )
                all_notices.extend(batch)
                if rt:
                    current_released_through = rt
            except Exception:
                logger.exception(
                    "DuvalClerk scrape failed for %s/%s", search.county, search.notice_type
                )

        await browser.close()

    logger.info(
        "DuvalClerk complete: %d total records across %d search(es)",
        len(all_notices), len(dc_only),
    )
    return all_notices, current_released_through
