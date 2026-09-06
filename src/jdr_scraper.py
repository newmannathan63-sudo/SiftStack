"""Scraper for legals.jaxdailyrecord.com — Duval County, FL legal notices.

Public portal — no login, no CAPTCHA required.
Handles: foreclosure (Notice of Sale - Foreclosure), probate, tax_sale (Tax Deeds).

Notice format differs from tnpublicnotice.com:
  - Full-text notices are inline on the search results page (no per-notice navigation)
  - Florida uses judicial foreclosure ("NOTICE OF FORECLOSURE SALE"), not trustee deeds
  - Probate uses "IN RE: ESTATE OF [NAME]" + Personal Representative contact block
  - Tax deeds reference parcel IDs and certificate numbers

Selector notes (verified against live site 2026-06-07, re-verified 2026-09-06):
  The JDR site uses standard HTML form elements and these selectors are correct.
  If every selector starts failing at once from the Apify actor while a local run
  against the same URL works fine, it's not a selector problem — see the Cloudflare
  note below. Only suspect selectors if a local Playwright run also fails to find them.

Cloudflare note (added 2026-09-06): the site sits behind Cloudflare. Every Apify
cloud run from 2026-08-16 onward silently got 0 notices — the page hung on
networkidle, then fell back to a DOM with no search form. Root cause: Cloudflare
serves a "Just a moment..." JS challenge (no form at all) to headless Playwright
traffic from Apify. Fixed by combining two things — neither alone was confirmed
sufficient, both are load-bearing:
  1. Route through Apify's residential proxy (proxy_url param below).
  2. JDR_STEALTH_LAUNCH_ARGS + JDR_STEALTH_INIT_SCRIPT, which patch the
     navigator.webdriver / automation fingerprint that Cloudflare also checks.
A plain unproxied run, and a proxied run without the stealth patches, both still
got challenged — confirmed by direct testing against the live site.
"""

import asyncio
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

import config
from config import JDR_SEARCH_URL, REQUEST_DELAY_MAX, REQUEST_DELAY_MIN, SavedSearch
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Category mapping ──────────────────────────────────────────────────

# SiftStack notice_type → JDR search form category label
JDR_CATEGORY_MAP: dict[str, str] = {
    "foreclosure": "Notice of Sale - Foreclosure",
    "probate":     "Probate",
    "tax_sale":    "Tax Deeds",
}

# JDR sits behind Cloudflare, which serves a "Just a moment..." JS challenge page
# (no search form at all) to plain headless Playwright — confirmed 2026-09-06 by
# comparing a raw headless launch (challenged) against one with these two patches
# applied (passed cleanly, both through the same residential proxy exit). Neither
# patch alone was tested in isolation — keep both.
JDR_STEALTH_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
JDR_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""

# JDR's search_date field filters on an internal entry date that lags behind
# when a notice actually becomes visible/searchable — records can appear in a
# search for a given date days after that date has passed. A daily incremental
# scraper using only the prior run's date as the floor permanently misses any
# notice that lags past that point. Re-check this many days back every run;
# hash-based seen_ids dedup (verified collision-free) makes the overlap safe.
JDR_LOOKBACK_BUFFER_DAYS = 7


# ── Florida address + name patterns ───────────────────────────────────

# Duval County cities (longest-first for greedy matching)
FL_DUVAL_CITIES: list[str] = sorted(
    [
        "Jacksonville Beach", "Atlantic Beach", "Neptune Beach",
        "Ponte Vedra Beach", "Fleming Island", "Fernandina Beach",
        "Jacksonville", "Orange Park", "Middleburg", "Macclenny",
        "Ponte Vedra", "Baldwin", "Yulee", "Callahan", "Hilliard",
    ],
    key=len,
    reverse=True,
)

# FL ZIP codes — Duval County is 320xx–322xx
FL_ZIP_RE = re.compile(r"\b(32[012]\d{2})(?:-\d{4})?\b")

_SUFFIX = (
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|"
    r"Pike|Highway|Hwy|Trail|Trl|Terrace|Ter|Parkway|Pkwy|"
    r"Cove|Cv|Loop|Run|Path|Ridge|Rdg|Crossing|Xing|"
    r"Bend|Point|Pt|Pass|Hollow|Holw|Glen|Glenn|View|"
    r"Landing|Lndg|Row|Trace|Walk|Knoll|Overlook|Crest|Spur|Commons)\b"
)

_ADDR_PART = (
    r"(\d{1,5}\s+"
    r"(?:[NSEW]\.?\s+)?"
    r"(?:[\w'-]+\s+)+?"
    + _SUFFIX + r"\.?)"
)

_PROP_INDICATOR = (
    r"(?:"
    r"commonly\s+known\s+as"
    r"|property\s+(?:known\s+as|address\s*(?:is|of|:)|at)"
    r"|(?:real\s+)?property\s+(?:located|situated)\s+at"
    r"|street\s+address\s*(?:is|of|:)?"
    r"|having\s+(?:the\s+)?address\s+(?:of\s+)?"
    r"|bearing\s+the\s+address"
    r"|also\s+known\s+as"
    r"|a/?k/?a"
    r")"
)

# Full FL address: indicator phrase + addr + city + FL + zip
FL_FULL_ADDR_RE = re.compile(
    _PROP_INDICATOR
    + r"\s*[:.,\s]*"
    + _ADDR_PART
    + r"(?:\s*[,.]?\s*(?:Suite|Ste|Apt|Unit|#)\s*\w+)?"
    + r"\s*[,.]\s*([\w][\w\s]*?)"           # city
    + r"\s*[,.]\s*(?:Florida|Fla\.?|FL)"
    + r"\s*[,.\s]*(\d{5}(?:-\d{4})?)?",     # zip
    re.IGNORECASE,
)

# Address-only (indicator + addr, city/state elsewhere)
FL_ADDR_ONLY_RE = re.compile(
    _PROP_INDICATOR + r"\s*[:.,\s]*" + _ADDR_PART,
    re.IGNORECASE,
)

# Standalone: "NUMBER STREET, CITY, FL ZIP" — no indicator phrase needed
FL_STANDALONE_RE = re.compile(
    _ADDR_PART
    + r"\s*[,.]\s*([\w][\w\s]*?)"
    + r"\s*[,.]\s*(?:Florida|Fla\.?|FL)"
    + r"\s*[,.\s]*(\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

# FL judicial foreclosure — defendant is the property owner
FL_DEFENDANT_RE = re.compile(
    r"Defendant\(?s?\)?\s*[:\-]\s*([A-Z][A-Za-z\s.,'-]+?)"
    r"(?:\s*[,;]\s*(?:et\s+al\.?|Case|Plaintiff|case\s+no)|\s*\n|\.$)",
    re.IGNORECASE,
)

# Probate — decedent
FL_DECEDENT_RE = re.compile(
    r"(?:IN\s+RE[:\s]*)?ESTATE\s+OF\s+([A-Z][A-Za-z\s.,'\-]+?)"
    r"(?:\s*,?\s*(?:Deceased|Dec['’.]?\s*d))",
    re.IGNORECASE,
)

# Probate — personal representative name
FL_PR_NAME_RE = re.compile(
    r"Personal\s+Representative\s*[:\s]+([A-Z][A-Za-z\s.,'-]+?)"
    r"(?:\s*[,\n]|\s+Attorney|\s+\d|\s*$)",
    re.IGNORECASE | re.MULTILINE,
)

# Probate — PR mailing address block following "Personal Representative"
FL_PR_ADDR_RE = re.compile(
    r"Personal\s+Representative"
    r"[^0-9]{3,150}"                            # skip over name
    r"(\d{1,5}\s+[\w\s.,'#-]+?" + _SUFFIX + r"\.?)"
    r"\s*[,.]+\s*([\w][\w\s]*?)"                # city
    r"\s*[,.]\s*(?:Florida|Fla\.?|FL)"
    r"\s*[,.\s]*(\d{5})",                       # zip
    re.IGNORECASE,
)

# Tax deed / general owner label
FL_OWNER_RE = re.compile(
    r"(?:owner(?:\s+of\s+record)?|grantor|taxpayer)\s*[:\-]\s*([A-Z][A-Za-z\s.,'-]+?)"
    r"(?:\s*[,\n]|\s+(?:of|at|in)\b|\s*$)",
    re.IGNORECASE,
)

# Case number (FL format: 16-2026-CA-001234 or 16-2026-CP-001234)
CASE_NO_RE = re.compile(
    r"Case\s+(?:No\.?|Number|#)\s*[:\s]*"
    r"([0-9]{2}-[0-9]{4}-[A-Z]{2}-[0-9]+(?:-[0-9A-Z]+)?)",
    re.IGNORECASE,
)

# Parcel / certificate number for tax deeds
PARCEL_RE = re.compile(
    r"(?:parcel\s+(?:ID|number|no\.?)|certificate\s+no\.?|tax\s+ID)\s*[:\s#]*([0-9][0-9-]{4,})",
    re.IGNORECASE,
)

# Sale / auction date (FL patterns)
_DATE_FRAG = (
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*)?"
    r"("
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}\s*,?\s*\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")"
)

FL_SALE_DATE_RE = re.compile(
    r"(?:sale\s+(?:will\s+be\s+)?(?:held\s+)?on"
    r"|scheduled\s+for"
    r"|at\s+public\s+(?:auction|sale)\s+on"
    r"|sell\s+at\s+public\s+(?:auction|sale)\s+on"
    r"|foreclosure\s+sale\s+(?:scheduled\s+for|on)"
    r"|will,?\s+on"
    r")\s+" + _DATE_FRAG,
    re.IGNORECASE,
)

# "date of first publication … is DATE" (probate)
FIRST_PUB_RE = re.compile(
    r"date\s+of\s+first\s+publication\s+(?:of\s+this\s+notice\s+)?is\s+" + _DATE_FRAG,
    re.IGNORECASE,
)


# ── Utilities ─────────────────────────────────────────────────────────


async def _delay() -> None:
    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))


def _notice_hash(text: str) -> str:
    """Stable 12-char ID from the first 500 chars of notice text (for dedup)."""
    return hashlib.md5(text[:500].encode("utf-8", errors="replace")).hexdigest()[:12]


def _norm_date(raw: str) -> str:
    """Parse various date formats → YYYY-MM-DD."""
    raw = raw.strip().rstrip(".")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(",. ")


# Matches "Suite NNN" or "Ste NNN" — indicates a commercial office address, not a property
_OFFICE_SUITE_RE = re.compile(r"\bStes?\b\s*\d+|\bSuite\s*\d+", re.IGNORECASE)


def _is_office_address(addr: str, city: str = "") -> bool:
    """Return True if addr/city looks like a law firm / commercial office, not a property."""
    return bool(_OFFICE_SUITE_RE.search(addr)) or bool(_OFFICE_SUITE_RE.search(city))


# ── Per-type notice parsers ────────────────────────────────────────────


def _parse_fl_address(notice: NoticeData, text: str) -> None:
    """Extract FL property address, city, and zip — three-strategy fallback."""
    # 1. Full context: indicator + addr + city + FL + zip
    m = FL_FULL_ADDR_RE.search(text)
    if m:
        addr = _clean(m.group(1))
        city = _clean(m.group(2)) if m.group(2) else ""
        if not _is_office_address(addr, city):
            notice.address = addr
            notice.city    = city
            notice.zip     = m.group(3) or ""
            return

    # 2. Address-only context: find city/zip in the next 200 chars
    m = FL_ADDR_ONLY_RE.search(text)
    if m:
        addr = _clean(m.group(1))
        window = text[m.end(): m.end() + 200]
        city_m = re.search(
            r"[,.\s]+([\w][\w\s]*?)\s*[,.]\s*(?:Florida|Fla\.?|FL)"
            r"\s*[,.\s]*(\d{5})?",
            window, re.IGNORECASE,
        )
        city = _clean(city_m.group(1)) if city_m else ""
        if not _is_office_address(addr, city):
            notice.address = addr
            if city_m:
                notice.city = city
                if city_m.group(2):
                    notice.zip = city_m.group(2)
            else:
                w_low = window.lower()
                for c in FL_DUVAL_CITIES:
                    if c.lower() in w_low:
                        notice.city = c
                        break
                z = FL_ZIP_RE.search(window)
                if z:
                    notice.zip = z.group(1)
            return

    # 3. Standalone "ADDR, CITY, FL ZIP" — only accept Duval-area addresses
    for m in FL_STANDALONE_RE.finditer(text):
        addr = _clean(m.group(1))
        city = _clean(m.group(2)) if m.group(2) else ""
        zipcode = m.group(3) or ""
        if _is_office_address(addr, city):
            continue
        # Only accept when city is a known Duval area city OR zip starts with 320/321/322
        city_ok = any(c.lower() == city.lower() for c in FL_DUVAL_CITIES)
        zip_ok  = bool(re.match(r"32[012]", zipcode))
        if city_ok or zip_ok:
            notice.address = addr
            notice.city    = city
            notice.zip     = zipcode
            return

    # 4. Last resort — find any FL zip and look back for a known city
    for z in FL_ZIP_RE.finditer(text):
        ctx = text[max(0, z.start() - 120): z.start()]
        for city in FL_DUVAL_CITIES:
            if city.lower() in ctx.lower():
                notice.city = city
                break
        notice.zip = z.group(1)
        break


def _parse_jdr_foreclosure(notice: NoticeData, text: str) -> None:
    """Extract fields from a FL judicial foreclosure notice."""
    _parse_fl_address(notice, text)

    # Defendant name = property owner in FL judicial foreclosure
    m = FL_DEFENDANT_RE.search(text)
    if m:
        name = _clean(m.group(1)).title()
        if 3 <= len(name) <= 80:
            notice.owner_name = name

    # Sale date
    m = FL_SALE_DATE_RE.search(text)
    if m:
        notice.auction_date = _norm_date(m.group(1))

    # Append case number to source_url for uniqueness
    m = CASE_NO_RE.search(text)
    if m:
        notice.source_url += f"&case={m.group(1)}"


def _parse_jdr_probate(notice: NoticeData, text: str) -> None:
    """Extract fields from a FL Notice to Creditors (probate)."""
    # Decedent name
    m = FL_DECEDENT_RE.search(text)
    if m:
        notice.decedent_name = _clean(m.group(1)).title()

    # Personal representative name
    m = FL_PR_NAME_RE.search(text)
    if m:
        notice.owner_name = _clean(m.group(1)).title()

    # PR mailing address
    m = FL_PR_ADDR_RE.search(text)
    if m:
        street = _clean(m.group(1))
        notice.owner_street = street.title() if street.isupper() else street
        notice.owner_city   = _clean(m.group(2))
        notice.owner_state  = "FL"
        notice.owner_zip    = m.group(3)

    # Date of first publication (may override the page-level date)
    m = FIRST_PUB_RE.search(text)
    if m:
        d = _norm_date(m.group(1))
        if d:
            notice.date_added = d

    # Case number
    m = CASE_NO_RE.search(text)
    if m:
        notice.source_url += f"&case={m.group(1)}"


def _parse_jdr_tax_deed(notice: NoticeData, text: str) -> None:
    """Extract fields from a FL tax deed sale notice."""
    _parse_fl_address(notice, text)

    # Owner name — try labeled field first, then defendant pattern
    m = FL_OWNER_RE.search(text)
    if m:
        notice.owner_name = _clean(m.group(1)).title()
    if not notice.owner_name:
        m = FL_DEFENDANT_RE.search(text)
        if m:
            notice.owner_name = _clean(m.group(1)).title()

    # Sale date
    m = FL_SALE_DATE_RE.search(text)
    if m:
        notice.auction_date = _norm_date(m.group(1))

    # Parcel ID
    m = PARCEL_RE.search(text)
    if m:
        notice.parcel_id = _clean(m.group(1))


def _parse_pub_date_from_block(text: str) -> str:
    """Best-effort publication date from a raw notice text block."""
    # "date of first publication … is DATE"
    m = FIRST_PUB_RE.search(text)
    if m:
        return _norm_date(m.group(1))
    # "Published: DATE" or "Date: DATE"
    m = re.search(
        r"(?:Published|Date)\s*[:\-]\s*"
        r"(\w+\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        text, re.IGNORECASE,
    )
    if m:
        return _norm_date(m.group(1))
    return ""


def _parse_jdr_notice(raw_text: str, search: SavedSearch, pub_date: str, seq: int) -> NoticeData:
    """Parse one JDR notice text block → NoticeData."""
    source_url = (
        f"{JDR_SEARCH_URL}?county={search.county}"
        f"&category={search.notice_type}&date={pub_date}&seq={seq}"
    )
    notice = NoticeData(
        county=search.county,
        notice_type=search.notice_type,
        state="FL",
        date_added=pub_date,
        source_url=source_url,
        raw_text=raw_text,
    )

    if search.notice_type == "foreclosure":
        _parse_jdr_foreclosure(notice, raw_text)
    elif search.notice_type == "probate":
        _parse_jdr_probate(notice, raw_text)
    elif search.notice_type == "tax_sale":
        _parse_jdr_tax_deed(notice, raw_text)

    return notice


# ── LLM fallback helpers ──────────────────────────────────────────────


def _needs_llm(notice: NoticeData) -> bool:
    if notice.notice_type == "probate":
        # Always run LLM for probate — FL probate format is complex and regex
        # often captures attorney attribution text ("Attorney And Personal") as
        # the PR name. LLM is authoritative for probate field extraction.
        return True
    return not notice.address or not notice.owner_name


# Known bad patterns that regex produces from FL probate notices.
# LLM results override regex for probate when regex captured these.
_PROBATE_REGEX_GARBAGE = re.compile(
    r"^(?:attorney\s+(?:and|for|at)\b|florida\s+bar\s+no|personal\s+rep)",
    re.IGNORECASE,
)


def _apply_llm(notice: NoticeData, result: dict) -> None:
    """Merge LLM-extracted fields into notice.

    For probate: LLM results override regex results because FL probate has
    complex structure that often causes regex to capture attorney text as the
    PR name. LLM is authoritative; only skip if LLM returned empty string.
    For other types: only fill empty slots (regex is reliable for addresses).
    """
    if notice.notice_type == "probate":
        # Always prefer LLM decedent name (regex may miss A/K/A variants)
        if result.get("decedent_name"):
            notice.decedent_name = result["decedent_name"]
        # Override regex PR name if LLM found one OR regex produced garbage
        if result.get("owner_name"):
            notice.owner_name = result["owner_name"]
        elif _PROBATE_REGEX_GARBAGE.match(notice.owner_name or ""):
            notice.owner_name = ""
        # Override PR address from LLM (regex often grabs attorney address instead)
        if result.get("owner_street"):
            notice.owner_street = result["owner_street"]
            notice.owner_city   = result.get("owner_city", "")
            notice.owner_state  = result.get("owner_state", "FL")
            notice.owner_zip    = result.get("owner_zip", "")
    else:
        if not notice.address and result.get("address"):
            addr = result["address"]
            city = result.get("city", "")
            if not _is_office_address(addr, city):
                notice.address = addr
                notice.city    = city
                notice.zip     = result.get("zip", "")
        if not notice.owner_name and result.get("owner_name"):
            notice.owner_name = result["owner_name"]
        if not notice.auction_date and result.get("auction_date"):
            notice.auction_date = result["auction_date"]
        if result.get("plaintiff"):
            notice.plaintiff = result["plaintiff"]


# ── Playwright form interaction ───────────────────────────────────────


async def _submit_search_form(
    page: Page,
    search: SavedSearch,
    start_date: str,
    end_date: str,
) -> bool:
    """Navigate to JDR and submit the search form for a given category/county/date range.

    Returns True once the form has been submitted (results may be empty — caller checks).
    Logs a warning for each field it cannot find rather than raising, so the test run
    log clearly shows which selectors need adjustment.
    """
    category = JDR_CATEGORY_MAP.get(search.notice_type, "")
    if not category:
        logger.error("No JDR category mapping for notice_type=%s", search.notice_type)
        return False

    logger.info("JDR: loading search page %s", JDR_SEARCH_URL)
    try:
        await page.goto(JDR_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PwTimeout:
        logger.warning("JDR search page load timed out — retrying once")
        try:
            await page.goto(JDR_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
        except PwTimeout:
            logger.error("JDR search page unreachable")
            return False

    logger.debug("JDR page loaded: %s", page.url)

    # ── Date range fields ────────────────────────────────────────────
    # JDR uses MM/DD/YYYY format in inputs named search_date / through_date.
    # Convert from the ISO YYYY-MM-DD format we store internally.
    def _to_jdr_date(iso: str) -> str:
        try:
            from datetime import datetime as _dt
            return _dt.strptime(iso, "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            return iso

    # (selector_start, selector_end, use_mmddyyyy)
    date_pairs = [
        ("input[name='search_date']",   "input[name='through_date']", True),   # JDR (verified)
        ("input[name='start_date']",    "input[name='end_date']",     False),
        ("input[name='startDate']",     "input[name='endDate']",      False),
        ("input[id*='start_date']",     "input[id*='end_date']",      False),
        ("input[placeholder*='Start']", "input[placeholder*='End']",  False),
        ("input[type='date']:first-of-type", "input[type='date']:last-of-type", False),
    ]
    date_filled = False
    for s_sel, e_sel, use_mdy in date_pairs:
        s_el = await page.query_selector(s_sel)
        e_el = await page.query_selector(e_sel)
        if s_el and e_el:
            fmt_s = _to_jdr_date(start_date) if use_mdy else start_date
            fmt_e = _to_jdr_date(end_date)   if use_mdy else end_date
            await s_el.fill(fmt_s)
            await e_el.fill(fmt_e)
            date_filled = True
            logger.debug("Date fields filled using %s / %s (%s – %s)", s_sel, e_sel, fmt_s, fmt_e)
            break
    if not date_filled:
        logger.warning(
            "JDR: could not find date input fields — tried %d selector pairs. "
            "Results may not be date-filtered. Check page HTML.",
            len(date_pairs),
        )

    # ── Category dropdown ────────────────────────────────────────────
    cat_selectors = [
        "select[name='category']", "select[name='Category']",
        "select[name='cat']",      "select[id*='category']",
        "select[id*='Category']",
    ]
    cat_filled = False
    for sel in cat_selectors:
        el = await page.query_selector(sel)
        if el:
            try:
                await page.select_option(sel, label=category)
                cat_filled = True
                logger.debug("Category set to '%s' via %s", category, sel)
                break
            except Exception:
                try:
                    await page.select_option(sel, value=category)
                    cat_filled = True
                    break
                except Exception:
                    continue
    if not cat_filled:
        logger.warning(
            "JDR: could not set category '%s' — tried %d selectors. "
            "Results may include all categories.",
            category, len(cat_selectors),
        )

    # ── County dropdown ──────────────────────────────────────────────
    county_selectors = [
        "select[name='county']", "select[name='County']",
        "select[id*='county']",  "select[id*='County']",
    ]
    county_filled = False
    for sel in county_selectors:
        el = await page.query_selector(sel)
        if el:
            try:
                await page.select_option(sel, label=search.county)
                county_filled = True
                logger.debug("County set to '%s' via %s", search.county, sel)
                break
            except Exception:
                continue
    if not county_filled:
        logger.warning(
            "JDR: could not set county '%s' — results may include all counties.",
            search.county,
        )

    # ── Submit ───────────────────────────────────────────────────────
    # JDR has multiple submit buttons (Drupal search block + the legal notices form).
    # input[name='submit'] targets the legal notices form specifically.
    submit_selectors = [
        "input[name='submit']",         # JDR legal notices form (verified)
        "button[type='submit']",
        "input[value='Search']",
        "button:has-text('Search')",
        "input[type='submit']",         # generic fallback (may match wrong button)
    ]
    submitted = False
    for sel in submit_selectors:
        el = await page.query_selector(sel)
        if el:
            try:
                await page.click(sel, timeout=5_000)
                submitted = True
                logger.debug("Form submitted via %s", sel)
                break
            except Exception:
                continue
    if not submitted:
        logger.warning("JDR: submit button not found — pressing Enter as fallback")
        await page.keyboard.press("Enter")

    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PwTimeout:
        pass  # Results may still be available even if networkidle times out
    await _delay()
    return True


# ── Notice block extraction ───────────────────────────────────────────


_JDR_SERIAL_RE = re.compile(
    r"^(?:Notice of Sale - Foreclosure|Notice of Action - Foreclosure|"
    r"Notice of Sale - Sheriff|Tax Deeds|Probate|Miscellaneous Public Notices|"
    r"[\w\s\-]+?)\s+\d{2}-\d{5}[A-Z]\s*\n",
    re.IGNORECASE,
)


async def _wait_for_stable_row_count(result_tbl, max_wait_s: float = 6.0) -> None:
    """Poll a results table's row count until it stops growing.

    `networkidle` can resolve before JDR's results table has finished
    populating all rows (e.g. progressive/AJAX rendering) — extracting
    immediately after can silently capture only a partial page (seen live:
    22 of 53 real rows on 2026-07-02), with no error or warning to flag it.
    Requires two consecutive identical counts (~400ms apart) before treating
    the table as settled.
    """
    prev_count = -1
    stable_checks = 0
    elapsed = 0.0
    interval = 0.4
    while elapsed < max_wait_s:
        try:
            rows = await result_tbl.query_selector_all("tr")
        except Exception:
            return
        count = len(rows)
        if count == prev_count:
            stable_checks += 1
            if stable_checks >= 2:
                return
        else:
            stable_checks = 0
        prev_count = count
        await asyncio.sleep(interval)
        elapsed += interval


async def _extract_notice_blocks(page: Page) -> list[tuple[str, str]]:
    """Extract (pub_date, text) tuples from the current results page.

    JDR results are in a <table bgcolor="#000000"> — each <tr> after the first
    (header) row is one complete notice.  Falls back to full-page text splitting
    if the table is not found (e.g. after a site redesign).

    Returns an empty list if no notices are present on this page.
    """
    # ── JDR-specific: results table identified by bgcolor attribute ──
    # Verified 2026-06-07: results live in the second <table> which has
    # bgcolor="#000000"; the first <table> is the search form.
    result_tbl = await page.query_selector("table[bgcolor='#000000']")
    if not result_tbl:
        # bgcolor attribute may have been removed — try the second table generically
        all_tables = await page.query_selector_all("table")
        if len(all_tables) >= 2:
            result_tbl = all_tables[-1]   # last table = results (form is first)

    if result_tbl:
        await _wait_for_stable_row_count(result_tbl)
        rows = await result_tbl.query_selector_all("tr")
        logger.debug("JDR results table: %d rows (skipping row 0 = category header)", len(rows))
        results: list[tuple[str, str]] = []
        for row in rows[1:]:    # row 0 is the category/column header
            try:
                raw = (await row.inner_text()).replace("\xa0", " ").strip()
                if len(raw) < 80:
                    continue
                # Skip the "No Results Found" page content that can appear as a table row
                if "No Results Found" in raw or "no results found" in raw.lower():
                    continue
                # Strip leading "Category SerialNumber\n" prefix (e.g. "Notice of Sale - Foreclosure 26-03229D\n")
                text = _JDR_SERIAL_RE.sub("", raw, count=1).strip()
                if not text:
                    text = raw      # keep original if stripping removed everything
                results.append((_parse_pub_date_from_block(text), text))
            except Exception:
                continue
        if results:
            return results

    # ── Fallback: split full page text by notice header patterns ────
    logger.warning(
        "JDR: results table not found — falling back to page-text splitting. "
        "If this persists, verify the table selector in _extract_notice_blocks."
    )
    content_text = ""
    for area_sel in ("#content", ".content", "main", "#main", ".results", "#results", "body"):
        el = await page.query_selector(area_sel)
        if el:
            content_text = (await el.inner_text()).replace("\xa0", " ")
            if len(content_text) > 200:
                break

    return _split_notice_text(content_text)


def _split_notice_text(full_text: str) -> list[tuple[str, str]]:
    """Split full-page text into notice blocks by common FL notice header patterns."""
    split_re = re.compile(
        r"(?=(?:"
        r"IN\s+THE\s+CIRCUIT\s+COURT"
        r"|NOTICE\s+OF\s+(?:FORECLOSURE|TAX\s+DEED|SALE)"
        r"|IN\s+RE:\s+ESTATE\s+OF"
        r"|NOTICE\s+TO\s+CREDITORS"
        r"|TAX\s+DEED\s+SALE\s+NOTICE"
        r"))",
        re.IGNORECASE,
    )
    parts = split_re.split(full_text)
    results: list[tuple[str, str]] = []
    for i, part in enumerate(parts):
        part = part.strip()
        if len(part) <= 100:
            continue
        # parts[0] is whatever precedes the first real match (site nav/boilerplate
        # when the page has no notices at all, e.g. a "Next" link past the last
        # real page) — only keep it if it actually starts with a notice header.
        if i == 0 and not split_re.match(part):
            continue
        results.append((_parse_pub_date_from_block(part), part))
    return results


async def _get_result_count(page: Page) -> int | None:
    """Extract total result count from the page text (e.g. 'Found 40 Records')."""
    try:
        body = await page.inner_text("body")
        # JDR's actual format: "Displaying Records 1 to 68" — total is the
        # second number, not the first (verified live 2026-07-09).
        m = re.search(r"Displaying\s+Records?\s+\d+\s+to\s+(\d+)", body, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(?:Found|Total)\s+(\d+)\s+Record", body, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s+(?:result|record|notice)s?\s+found", body, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"1\s*[-–]\s*\d+\s+of\s+(\d+)", body, re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


async def _click_next_page(page: Page) -> bool:
    """Click the Next pagination control. Returns True on success."""
    next_selectors = [
        "a:has-text('Next')",
        "input[title='Next page']",
        "input[value='Next']",
        "a[title='Next']",
        "a[rel='next']",
        ".pagination a:last-child",
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


async def _scrape_jdr_search(
    page: Page,
    search: SavedSearch,
    since_date: str | None,
    seen_ids: dict[str, str],
    llm_api_key: str | None,
) -> list[NoticeData]:
    """Run one JDR category search and return all matching NoticeData."""
    logger.info(
        "JDR scraping: county=%s type=%s since=%s",
        search.county, search.notice_type, since_date or "all",
    )

    today = datetime.now().strftime("%Y-%m-%d")
    # date_added tracks when *we* first captured the notice, not the widened
    # search floor below — keep it as the caller's since_date (or today).
    date_added_default = since_date or today
    if since_date:
        start = (
            datetime.strptime(since_date, "%Y-%m-%d") - timedelta(days=JDR_LOOKBACK_BUFFER_DAYS)
        ).strftime("%Y-%m-%d")
    else:
        start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    ok = await _submit_search_form(page, search, start, today)
    if not ok:
        logger.error("JDR form submission failed for %s / %s", search.county, search.notice_type)
        return []

    notices: list[NoticeData] = []
    page_num = 0

    while True:
        page_num += 1
        total = await _get_result_count(page)
        logger.info(
            "  JDR page %d — total results: %s",
            page_num, str(total) if total is not None else "?",
        )

        blocks = await _extract_notice_blocks(page)
        if not blocks:
            if page_num == 1:
                logger.info(
                    "  JDR %s/%s: no notice blocks on page 1 — "
                    "either no results for this date range or selector needs updating",
                    search.county, search.notice_type,
                )
            break

        logger.info("  Parsing %d notice blocks from page %d", len(blocks), page_num)
        seq_base = (page_num - 1) * 50

        # First pass: regex-parse all new notices on this page
        pending: list[tuple[str, "NoticeData", str]] = []  # (nhash, notice, block_text)
        for i, (pub_date, block_text) in enumerate(blocks):
            nhash = _notice_hash(block_text)
            if nhash in seen_ids:
                logger.debug("  Skipping seen notice hash=%s", nhash)
                continue
            # Use date_added_default (the un-widened since_date), not the
            # lookback-buffered `start` used only to query JDR. Parsed notice
            # text dates (FIRST_PUB_RE) reflect the *first* publication date,
            # which can be earlier for two-run notices (e.g. Jun 22 + Jun 29).
            # Skipping on that parsed date would drop valid second-publication records.
            notice = _parse_jdr_notice(block_text, search, date_added_default, seq_base + i + 1)
            pending.append((nhash, notice, block_text))

        # Second pass: parallel LLM for all notices that need it
        if llm_api_key and pending:
            from llm_parser import extract_with_llm
            _sem = asyncio.Semaphore(10)

            async def _llm_one(block_text: str, notice: "NoticeData") -> dict:
                if not _needs_llm(notice):
                    return {}
                async with _sem:
                    try:
                        return await extract_with_llm(
                            block_text, search.notice_type, search.county,
                            llm_api_key, state="FL",
                        )
                    except Exception as exc:
                        logger.debug("  LLM fallback failed: %s", exc)
                        return {}

            llm_results = await asyncio.gather(
                *[_llm_one(bt, n) for _, n, bt in pending]
            )
            for (nhash, notice, _), llm_result in zip(pending, llm_results):
                if llm_result:
                    _apply_llm(notice, llm_result)
                seen_ids[nhash] = notice.date_added or today
                logger.debug(
                    "  Parsed: type=%s owner=%s addr=%s",
                    notice.notice_type,
                    (notice.owner_name or "?")[:35],
                    (notice.address or "?")[:45],
                )
                notices.append(notice)
        else:
            for nhash, notice, _ in pending:
                seen_ids[nhash] = notice.date_added or today
                notices.append(notice)

        # Pagination — JDR always renders a "Next" link even when every result
        # already fits on the current page (it just points back to start_round=0,
        # not a real next page), so only follow it if there's actually more to see.
        if total is not None and len(blocks) >= total:
            break
        if not await _click_next_page(page):
            break

    logger.info(
        "  JDR %s/%s: %d notices collected",
        search.county, search.notice_type, len(notices),
    )
    return notices


# ── Main entry point ──────────────────────────────────────────────────


async def scrape_jdr_all(
    searches: list[SavedSearch],
    since_date: str | None = None,
    seen_ids: dict[str, str] | None = None,
    llm_api_key: str | None = None,
    failures: list[str] | None = None,
    proxy_url: str | None = None,
) -> list[NoticeData]:
    """Scrape all JDR-sourced saved searches and return combined NoticeData.

    Args:
        searches:    SavedSearch entries with source="jdr".
        since_date:  ISO date string (YYYY-MM-DD); only notices on/after this date.
        seen_ids:    Cross-run dedup dict {notice_hash: date}; updated in-place.
        llm_api_key: Anthropic API key for LLM fallback on missing fields.
        proxy_url:   Optional proxy URL (e.g. Apify residential proxy). JDR sits
                     behind Cloudflare, which throttles/degrades responses to
                     datacenter IPs (confirmed 2026-09: every field selector fails
                     when loaded from Apify's cloud IP, all pass from a residential
                     IP) — route through a residential proxy to match.

    Returns:
        List of NoticeData for Duval County, FL with state="FL".
    """
    if seen_ids is None:
        seen_ids = {}

    jdr_only = [s for s in searches if s.source == "jdr"]
    if not jdr_only:
        return []

    logger.info(
        "JDR: starting %d search(es): %s",
        len(jdr_only),
        ", ".join(f"{s.county}/{s.notice_type}" for s in jdr_only),
    )

    all_notices: list[NoticeData] = []

    launch_opts: dict = {"headless": True, "args": JDR_STEALTH_LAUNCH_ARGS}
    if proxy_url:
        from urllib.parse import urlparse
        parsed = urlparse(proxy_url)
        proxy_cfg: dict = {
            "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
        }
        if parsed.username:
            proxy_cfg["username"] = parsed.username
        if parsed.password:
            proxy_cfg["password"] = parsed.password
        launch_opts["proxy"] = proxy_cfg
        logger.info("JDR: using proxy %s:%s", parsed.hostname, parsed.port)

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_opts)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(JDR_STEALTH_INIT_SCRIPT)
        context.set_default_timeout(30_000)
        page = await context.new_page()

        for search in jdr_only:
            try:
                batch = await _scrape_jdr_search(page, search, since_date, seen_ids, llm_api_key)
                all_notices.extend(batch)
            except Exception as exc:
                logger.exception(
                    "JDR scrape failed for %s/%s", search.county, search.notice_type
                )
                if failures is not None:
                    failures.append(f"JDR {search.county}/{search.notice_type}: {exc}")

        await browser.close()

    logger.info(
        "JDR complete: %d total notices across %d search(es)",
        len(all_notices), len(jdr_only),
    )
    return all_notices
