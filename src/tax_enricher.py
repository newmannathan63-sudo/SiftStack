"""Enrich notices with tax delinquency data from county property tax APIs.

Knox County: https://knox-tn.mygovonline.com/api/v2
Blount County: TBD (placeholder)
"""

import logging
import random
import re
import time
from urllib.parse import quote

import requests

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

KNOX_API_BASE = "https://knox-tn.mygovonline.com/api/v2"
REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0
REQUEST_TIMEOUT = 15


# Business entity pattern — imported from shared config
import config as _cfg
_BUSINESS_RE = _cfg.BUSINESS_RE


def detect_deceased_indicator(owner_name: str) -> str:
    """Detect deceased-owner indicators from a county tax API owner name.

    Returns one of: "life_estate", "personal_rep", "care_of", "et_al",
    "trustee", or "" (no indicator detected).

    Priority order reflects confidence level (highest first).
    """
    if not owner_name or not owner_name.strip():
        return ""

    upper = owner_name.upper()

    # 1. Personal Representative — strongest signal (definite estate)
    if "PERSONAL REPRESENTATIVE" in upper or "PERSONAL REP" in upper:
        return "personal_rep"

    # 2. Life Estate — very strong signal (elderly/deceased holder)
    if "LIFE EST" in upper:
        return "life_estate"

    # 3. Care-of (%) — strong signal for deceased/incapacitated
    if "%" in owner_name:
        return "care_of"

    # 4. Et Al — moderate signal (multiple parties, often heirs)
    if re.search(r"\bET\s+AL\b", upper):
        return "et_al"

    # 5. Trustee — weakest signal; skip business entities
    if re.search(r"\bTRUSTEE\b", upper):
        if not _BUSINESS_RE.search(upper):
            return "trustee"

    return ""


def _normalize_parcel_id(raw: str) -> str:
    """Normalize parcel ID to the format used by the tax API (with dash).

    KGIS returns e.g. '048DE017' but the tax API uses '048DE-017'.
    TPAD returns e.g. '048DE-017' already.
    """
    raw = raw.strip()
    if not raw:
        return ""
    # Already has a dash — return as-is
    if "-" in raw:
        return raw
    # Insert dash before the last 3 digits (common Knox parcel format)
    m = re.match(r"^(\d{3}[A-Z]{0,2})(\d{2,5})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return raw


def _knox_lookup_by_parcel(parcel_id: str) -> dict | None:
    """Fetch tax bills for a Knox County parcel by account number.

    Returns dict with 'delinquent_amount', 'delinquent_years',
    'latitude', 'longitude', or None on failure.
    """
    normalized = _normalize_parcel_id(parcel_id)
    if not normalized:
        return None

    url = f"{KNOX_API_BASE}/due/PPT/{quote(normalized)}?detail_level=public"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            logger.debug("Tax API: no data for parcel '%s'", normalized)
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Tax API request failed for parcel '%s': %s", normalized, e)
        return None

    try:
        data = resp.json()
        ppt = data.get("due", {}).get("PPT", {})
        bills = ppt.get("bills", [])
        result = _parse_delinquency(bills)

        # Extract lat/lon from parcels array (already in the response)
        parcels = ppt.get("parcels", [])
        if parcels:
            p = parcels[0]
            if p.get("latitude") is not None:
                result["latitude"] = str(p["latitude"])
            if p.get("longitude") is not None:
                result["longitude"] = str(p["longitude"])

        return result
    except (ValueError, KeyError) as e:
        logger.debug("Tax API parse error for parcel '%s': %s", normalized, e)
        return None


def _knox_lookup_by_address(address: str) -> dict | None:
    """Search Knox County tax records by address, then fetch bills.

    Returns dict with 'delinquent_amount', 'delinquent_years', and
    'account_number', or None on failure.
    """
    if not address or not address.strip():
        return None

    search_url = f"{KNOX_API_BASE}/parcels/{quote(address)}?detail_level=public&start=0&length=5"
    try:
        resp = requests.get(search_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Tax API address search failed for '%s': %s", address, e)
        return None

    try:
        data = resp.json()
        parcels = data.get("parcels", [])
        if not parcels:
            logger.debug("Tax API: no parcels found for '%s'", address)
            return None

        # Take the best match (first result, highest score)
        best = parcels[0]
        account = best.get("account_number", "")
        if not account:
            return None

        # Small delay before the bill lookup
        time.sleep(random.uniform(0.5, 1.0))
        result = _knox_lookup_by_parcel(account)
        if result:
            result["account_number"] = account
        return result
    except (ValueError, KeyError) as e:
        logger.debug("Tax API parse error for address '%s': %s", address, e)
        return None


def _parse_delinquency(bills: list[dict]) -> dict:
    """Parse bill list to extract delinquent amount and years count.

    A bill is delinquent if delinquent=True AND paid=False.
    """
    delinquent_amount = 0.0
    delinquent_years = 0

    for bill in bills:
        is_delinquent = bill.get("delinquent", False)
        is_paid = bill.get("paid", True)
        due = bill.get("due", 0) or 0

        if is_delinquent and not is_paid and due > 0:
            delinquent_amount += due
            delinquent_years += 1

    return {
        "delinquent_amount": round(delinquent_amount, 2),
        "delinquent_years": delinquent_years,
    }


def _knox_parcel_address(parcel_id: str) -> dict | None:
    """Look up official street address and owner for a Knox County parcel ID.

    Uses the parcels search endpoint which returns parcel_address and owner.
    Returns dict with 'address' and 'owner', or None if not found.
    """
    normalized = _normalize_parcel_id(parcel_id)
    if not normalized:
        return None

    url = f"{KNOX_API_BASE}/parcels/{quote(normalized)}?detail_level=public&start=0&length=1"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.debug("Parcel address lookup failed for '%s': %s", normalized, e)
        return None

    try:
        data = resp.json()
        parcels = data.get("parcels", [])
        if not parcels:
            return None
        p = parcels[0]
        addr = p.get("parcel_address", "").strip()
        owner = p.get("owner", "").strip()
        return {"address": addr or None, "owner": owner or None}
    except (ValueError, KeyError):
        return None


def lookup_parcel_addresses(notices: list[NoticeData]) -> None:
    """Replace OCR addresses with official county addresses from parcel IDs.

    For Knox County tax_sale notices that have a parcel_id, looks up the
    official parcel_address from the county API. This fixes garbled OCR
    addresses before Smarty standardization runs.

    Updates notices in-place.
    """
    candidates = [
        n for n in notices
        if n.county.lower() == "knox" and n.parcel_id.strip()
    ]

    if not candidates:
        logger.info("No Knox County notices with parcel IDs for address lookup")
        return

    logger.info("Looking up official addresses for %d parcels...", len(candidates))

    updated = 0
    failed = 0

    for i, notice in enumerate(candidates, 1):
        result = _knox_parcel_address(notice.parcel_id)

        if result:
            # Store raw tax API owner name and detect deceased indicators
            raw_owner = result.get("owner", "")
            if raw_owner:
                notice.tax_owner_name = raw_owner.strip()
                notice.deceased_indicator = detect_deceased_indicator(raw_owner)

            # Extract owner name if available and notice doesn't have one
            if raw_owner and not notice.owner_name.strip():
                notice.owner_name = raw_owner.title()

            api_addr = result.get("address")
            if api_addr:
                old_addr = notice.address
                # Title-case the API address (comes back ALL CAPS)
                new_addr = api_addr.title()
                if old_addr.lower().strip() != new_addr.lower().strip():
                    # Don't replace an address that has a house number with one
                    # that doesn't — the OCR version is more useful for Smarty
                    old_has_number = bool(re.match(r"\d", old_addr.strip()))
                    new_has_number = bool(re.match(r"\d", new_addr.strip()))
                    if old_has_number and not new_has_number:
                        logger.debug(
                            "  [%d/%d] %s: keeping OCR address '%s' (API has no house #: '%s')",
                            i, len(candidates), notice.parcel_id, old_addr, new_addr,
                        )
                    else:
                        notice.address = new_addr
                        updated += 1
                        logger.debug(
                            "  [%d/%d] %s: '%s' → '%s'",
                            i, len(candidates), notice.parcel_id, old_addr, new_addr,
                        )
                else:
                    logger.debug("  [%d/%d] %s: address matches", i, len(candidates), notice.parcel_id)
        else:
            failed += 1

        if i % 50 == 0:
            logger.info("Parcel address progress: %d/%d (updated=%d, failed=%d)",
                        i, len(candidates), updated, failed)

        # Rate limit
        time.sleep(random.uniform(0.5, 1.0))

    logger.info(
        "Parcel address lookup complete: %d updated, %d failed, %d unchanged",
        updated, failed, len(candidates) - updated - failed,
    )


def enrich_tax_delinquency(notices: list[NoticeData]) -> None:
    """Enrich notices with tax delinquency data from county APIs.

    Updates notices in-place, setting tax_delinquent_amount and
    tax_delinquent_years fields.

    Currently supports Knox County only. Blount County is a placeholder.
    """
    knox_notices = [
        n for n in notices
        if n.county.lower() == "knox" and (n.parcel_id.strip() or n.address.strip())
    ]

    if not knox_notices:
        logger.info("No Knox County notices with addresses/parcels to enrich")
        return

    logger.info("Enriching tax delinquency for %d Knox County notices...", len(knox_notices))

    enriched = 0
    failed = 0
    skipped = 0

    for i, notice in enumerate(knox_notices, 1):
        result = None

        # Prefer parcel_id lookup (faster, more precise)
        if notice.parcel_id.strip():
            result = _knox_lookup_by_parcel(notice.parcel_id)

        # Fallback to address search
        if result is None and notice.address.strip():
            result = _knox_lookup_by_address(notice.address)
            # Store the discovered parcel_id if we didn't have one
            if result and not notice.parcel_id.strip() and result.get("account_number"):
                notice.parcel_id = result["account_number"]

        if result:
            notice.tax_delinquent_amount = str(result["delinquent_amount"]) if result["delinquent_amount"] > 0 else ""
            notice.tax_delinquent_years = str(result["delinquent_years"]) if result["delinquent_years"] > 0 else ""

            # Store lat/lon from the API (used for reverse geocoding later)
            if result.get("latitude") and not notice.latitude:
                notice.latitude = result["latitude"]
            if result.get("longitude") and not notice.longitude:
                notice.longitude = result["longitude"]

            enriched += 1
            if result["delinquent_years"] > 0:
                logger.debug(
                    "  [%d/%d] %s: $%.2f delinquent, %d years",
                    i, len(knox_notices), notice.address or notice.parcel_id,
                    result["delinquent_amount"], result["delinquent_years"],
                )
        else:
            failed += 1

        if i % 20 == 0:
            logger.info("Tax enrichment progress: %d/%d (enriched=%d, failed=%d)",
                        i, len(knox_notices), enriched, failed)

        # Rate limit
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

    blount_count = sum(1 for n in notices if n.county.lower() == "blount")
    if blount_count:
        logger.info("Skipped %d Blount County notices (tax API not yet implemented)", blount_count)
        skipped = blount_count

    logger.info(
        "Tax delinquency enrichment complete: %d enriched, %d failed, %d skipped",
        enriched, failed, skipped,
    )


def _name_match_score(search_name: str, api_owner: str) -> float:
    """Score how well a search name matches an API owner name.

    Knox Tax API returns "LAST FIRST MIDDLE" format.
    Search name is "FIRST MIDDLE LAST" format.
    Returns 0.0-1.0 based on token overlap.
    """
    search_tokens = set(search_name.upper().split())
    api_tokens = set(api_owner.upper().split())
    # Remove common noise
    noise = {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    search_tokens -= noise
    api_tokens -= noise
    if not search_tokens or not api_tokens:
        return 0.0
    overlap = search_tokens & api_tokens
    return len(overlap) / max(len(search_tokens), len(api_tokens))


def _clean_name_for_search(name: str) -> list[str]:
    """Generate search variations for a name.

    Knox Tax API stores names as "LAST FIRST MIDDLE". We try multiple formats.
    Returns a list of search strings to try in order.
    """
    # Remove suffixes
    suffixes_re = re.compile(r"\b(JR|SR|II|III|IV|ESQ)\b\.?", re.IGNORECASE)
    clean = suffixes_re.sub("", name).strip()
    clean = re.sub(r"\s+", " ", clean)  # collapse whitespace
    clean = re.sub(r",\s*$", "", clean)  # trailing comma

    parts = clean.split()
    if not parts:
        return [name]

    searches = []

    # Original name as-is
    searches.append(name.strip())

    # Without suffixes
    if clean != name.strip():
        searches.append(clean)

    # "LAST FIRST" format (how Knox API stores names)
    if len(parts) >= 2:
        searches.append(f"{parts[-1]} {parts[0]}")

    # "LAST FIRST MIDDLE" if 3+ parts
    if len(parts) >= 3:
        searches.append(f"{parts[-1]} {' '.join(parts[:-1])}")

    # First + Last only (drop middle)
    if len(parts) >= 3:
        searches.append(f"{parts[0]} {parts[-1]}")

    return list(dict.fromkeys(searches))  # dedupe preserving order


def _knox_name_search(name: str, min_score: float = 0.4) -> list[tuple[float, dict]]:
    """Search Knox Tax API by name, return scored results above threshold."""
    search_url = (
        f"{KNOX_API_BASE}/parcels/{quote(name)}"
        f"?detail_level=public&start=0&length=10"
    )
    try:
        resp = requests.get(search_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except requests.RequestException:
        return []

    try:
        data = resp.json()
        parcels = data.get("parcels", [])
        scored = []
        for p in parcels:
            owner = p.get("owner", "")
            score = _name_match_score(name, owner)
            if score >= min_score:
                scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored
    except (ValueError, KeyError):
        return []


def _people_search_property(name: str, city: str = "Knoxville", state_abbr: str = "TN") -> str | None:
    """Search people search sites for a person's property address.

    Uses CyberBackgroundChecks via the obituary enricher's Firecrawl infrastructure.
    Returns address string or None.
    """
    try:
        from obituary_enricher import _build_people_search_urls, _fetch_page_text
        urls = _build_people_search_urls(name, city, state_abbr=state_abbr)
        for url in urls[:3]:
            time.sleep(random.uniform(0.5, 1.0))
            text = _fetch_page_text(url)
            if not text or len(text) < 100:
                continue
            import re as _re
            # Match street addresses followed by the target city or state
            city_pat = _re.escape(city) if city else state_abbr
            addr_pattern = _re.compile(
                r"(\d+\s+[\w\s.]+(?:St|Ave|Rd|Dr|Ln|Ct|Blvd|Way|Pl|Cir|Pike|Trl|Loop|Run|Ter|Pkwy))"
                rf"[,.\s]+(?:{city_pat}|{state_abbr})",
                _re.IGNORECASE,
            )
            matches = addr_pattern.findall(text)
            if matches:
                addr = matches[0].strip()
                logger.info("    People search found address: %s", addr)
                return addr
    except Exception as e:
        logger.debug("  People search property lookup failed: %s", e)
    return None


def _search_dcpa_by_name(last_name: str, first_name: str, page) -> list[dict]:
    """Search Duval County PAO by owner name using a sync_playwright Page.

    Returns list of dicts: {re_number, owner_name, address, city, zip}
    """
    query = f"{last_name.upper()} {first_name.upper()}".strip() if first_name else last_name.upper()
    try:
        page.goto(
            "https://paopropertysearch.coj.net/Basic/Search.aspx",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        page.fill("#ctl00_cphBody_tbName", query)
        page.select_option("#ctl00_cphBody_ddResultsPerPage", value="100")
        page.click("#ctl00_cphBody_bSearch")
        try:
            page.wait_for_url("**/Results.aspx**", timeout=10000)
        except Exception:
            return []  # stayed on Search.aspx — no results

        rows = page.eval_on_selector_all(
            "table tr",
            """rows => rows.map(row => ({
                cells: Array.from(row.querySelectorAll('td,th')).map(c => c.innerText.trim()),
                link:  (row.querySelector('a') || {}).href || ''
            }))""",
        )
        results = []
        for row in rows[1:]:  # skip header
            cells = row.get("cells", [])
            if len(cells) < 9 or not cells[0]:
                continue
            # Cols: RE#, Name, Street#, StreetName, Type, Direction, Unit, City, Zip
            parts = [cells[2], cells[5], cells[3], cells[4], cells[6]]
            address = " ".join(p for p in parts if p.strip())
            zip_code = cells[8].rstrip("-").strip()
            results.append({
                "re_number":  cells[0],
                "owner_name": cells[1],
                "address":    address,
                "city":       cells[7],
                "zip":        zip_code,
            })
        return results
    except Exception as e:
        logger.debug("  DCPA search failed for %s: %s", query, e)
        return []


def _score_dcpa_name(result_name: str, decedent: str) -> float:
    """Token-overlap score for a DCPA owner name vs the expected decedent name (0–1)."""
    _noise = {"JR", "SR", "III", "II", "IV", "ET", "AL"}
    res_tok = set(result_name.upper().split()) - _noise
    dec_tok = set(decedent.upper().split()) - _noise
    # Strip trailing punctuation from tokens
    res_tok = {t.strip(".,") for t in res_tok}
    dec_tok = {t.strip(".,") for t in dec_tok}
    if not res_tok or not dec_tok:
        return 0.0
    overlap = len(res_tok & dec_tok)
    return overlap / max(len(res_tok), len(dec_tok))


def _probate_property_lookup(notices: list[NoticeData]) -> None:
    """Multi-tier property lookup for probate records without addresses.

    Knox (TN):
      Tier 1: Knox Tax API by decedent name (multiple search variations)
      Tier 2: Knox Tax API by executor last name (family property)
      Tier 3: People search for decedent's last known address

    Duval (FL):
      Tier 1: Duval County Property Appraiser (DCPA) owner name search
      Tier 2: Tracerfy instant trace for decedent (state=FL)
      Tier 3: CyberBackgroundChecks via Firecrawl + LLM address extraction
    """
    # Create one browser for all FL notices (avoids per-notice launch overhead)
    fl_needed = any(
        n.county.lower().strip() == "duval"
        and not n.address.strip()
        and n.decedent_name.strip()
        for n in notices
    )
    dcpa_page = None
    dcpa_browser = None
    dcpa_pw_ctx = None
    if fl_needed:
        try:
            from playwright.sync_api import sync_playwright
            dcpa_pw_ctx = sync_playwright().start()
            dcpa_browser = dcpa_pw_ctx.chromium.launch(headless=True)
            dcpa_page = dcpa_browser.new_page()
            logger.debug("  DCPA Playwright browser initialized")
        except Exception as e:
            logger.warning("  Could not start DCPA browser: %s — FL Tier 1 unavailable", e)

    try:
        for notice in notices:
            if notice.address.strip():
                continue
            if not notice.decedent_name.strip():
                continue

            decedent = notice.decedent_name.strip()
            executor = notice.owner_name.strip()
            county = notice.county.lower().strip()
            logger.info("  Looking up property for decedent: %s (%s County)", decedent, notice.county)

            # ── Duval County (FL) path ────────────────────────────────────
            if county == "duval":
                _probate_property_lookup_fl(notice, decedent, dcpa_page=dcpa_page)
                continue

            # ── Knox County (TN) path ────────────────────────────────────

            # Tier 1: Knox Tax API by decedent name variations
            search_names = _clean_name_for_search(decedent)
            best_match = None

            for search_name in search_names:
                time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                results = _knox_name_search(search_name, min_score=0.4)
                if results:
                    if not best_match or results[0][0] > best_match[0]:
                        best_match = results[0]
                    if results[0][0] >= 0.6:
                        break

            if best_match and best_match[0] >= 0.4:
                score, parcel = best_match
                logger.info(
                    "  Tier 1 (Tax API): %s (owner: %s, score: %.2f)",
                    parcel.get("parcel_address", ""), parcel.get("owner", ""), score,
                )
                _apply_parcel_to_notice(notice, parcel)
                continue

            # Tier 2: Knox Tax API by executor name (family property)
            if executor:
                executor_searches = _clean_name_for_search(executor)
                for search_name in executor_searches[:3]:
                    time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
                    results = _knox_name_search(search_name, min_score=0.4)
                    for score, parcel in results:
                        addr = parcel.get("parcel_address", "")
                        owner = parcel.get("owner", "")
                        dec_last = decedent.split()[-1].upper() if decedent.split() else ""
                        if dec_last and dec_last in owner.upper():
                            logger.info(
                                "  Tier 2 (Executor family): %s (owner: %s, score: %.2f)",
                                addr, owner, score,
                            )
                            _apply_parcel_to_notice(notice, parcel)
                            break
                    if notice.address.strip():
                        break
                if notice.address.strip():
                    continue

            # Tier 3: People search for decedent's property address
            logger.info("  Tier 3: People search for %s", decedent)
            people_addr = _people_search_property(decedent, city="Knoxville", state_abbr="TN")
            if people_addr:
                logger.info("  Tier 3 (People Search): %s", people_addr)
                notice.address = people_addr
                notice.city = "Knoxville"
                notice.state = "TN"
                continue

            logger.warning("  No property found for decedent: %s (all tiers exhausted)", decedent)

    finally:
        if dcpa_browser:
            try:
                dcpa_browser.close()
            except Exception:
                pass
        if dcpa_pw_ctx:
            try:
                dcpa_pw_ctx.stop()
            except Exception:
                pass


def _probate_property_lookup_fl(notice: "NoticeData", decedent: str, dcpa_page=None) -> None:
    """Property lookup for Duval County FL probate — DCPA → Tracerfy → People Search."""

    # Tier 1: Duval County Property Appraiser owner name search
    if dcpa_page is not None:
        try:
            parts = decedent.upper().split()
            last = parts[-1] if parts else decedent.upper()
            first = " ".join(parts[:-1]) if len(parts) > 1 else ""
            # Try last+first first, fall back to last only if no results
            results = _search_dcpa_by_name(last, first, dcpa_page)
            if not results and first:
                results = _search_dcpa_by_name(last, "", dcpa_page)
            if results:
                best = max(results, key=lambda r: _score_dcpa_name(r["owner_name"], decedent))
                score = _score_dcpa_name(best["owner_name"], decedent)
                if score >= 0.4:
                    notice.address = best["address"]
                    notice.city = best["city"]
                    notice.state = "FL"
                    if best.get("zip"):
                        notice.zip = best["zip"]
                    if best.get("re_number"):
                        notice.parcel_id = best["re_number"]
                    logger.info(
                        "  Tier 1 FL (DCPA): %s, %s (owner: %s, score=%.2f)",
                        notice.address, notice.city, best["owner_name"], score,
                    )
                    return
        except Exception as e:
            logger.debug("  DCPA Tier 1 failed for %s: %s", decedent, e)

    # Tier 2: Tracerfy instant trace for FL decedent
    if _cfg.TRACERFY_API_KEY:
        try:
            from obituary_enricher import _lookup_dm_address_tracerfy
            result = _lookup_dm_address_tracerfy(
                decedent, "Jacksonville", state_abbr="FL"
            )
            if result and result.get("street"):
                notice.address = result["street"]
                notice.city = result.get("city", "Jacksonville")
                notice.state = "FL"
                if result.get("zip"):
                    notice.zip = result["zip"]
                logger.info(
                    "  Tier 2 FL (Tracerfy): %s, %s", notice.address, notice.city
                )
                return
        except Exception as e:
            logger.debug("  Tracerfy FL lookup failed for %s: %s", decedent, e)

    # Tier 3: CyberBackgroundChecks via Firecrawl + LLM
    try:
        from obituary_enricher import _lookup_dm_address_serper_firecrawl
        result = _lookup_dm_address_serper_firecrawl(
            decedent, "Jacksonville", _cfg.ANTHROPIC_API_KEY,
            state_full="Florida", state_abbr="FL",
        )
        if result and result.get("street"):
            notice.address = result["street"]
            notice.city = result.get("city", "Jacksonville")
            notice.state = "FL"
            if result.get("zip"):
                notice.zip = result["zip"]
            logger.info(
                "  Tier 3 FL (People Search): %s, %s", notice.address, notice.city
            )
            return
    except Exception as e:
        logger.debug("  FL Tier 3 people search failed for %s: %s", decedent, e)

    logger.warning("  No FL property found for decedent: %s (all tiers exhausted)", decedent)


_LP_INSTRUMENT_RE = re.compile(r"Instrument\s+#:\s*(\d+)", re.IGNORECASE)
_FL_STREET_ADDR_RE = re.compile(
    r"(\d{1,5}\s+[\w\s.'-]+?(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|"
    r"Boulevard|Blvd|Way|Circle|Cir|Court|Ct|Place|Pl|Terrace|Ter|Parkway|Pkwy|"
    r"Loop|Run|Trail|Trl|Cove|Cv|Bend|Path|Ridge|Crossing|Xing|Commons)\b\.?)"
    r"\s*[,.]?\s*([\w][\w\s]*?)\s*[,.]\s*(?:Florida|FL)\s*[,\s]*(\d{5})?",
    re.IGNORECASE,
)


_LP_DOC_LINK_RE = re.compile(r"Doc Link:\s*(https?://\S+)", re.IGNORECASE)


def _fetch_or_doc_text(instrument: str, book: str, page: str, or_page, raw_text: str = "") -> str:
    """Navigate to the Duval Clerk OR document viewer and return visible text.

    Priority: stored Doc Link from raw_text → instrument number URL → book/page URL.
    Handles the or.duvalclerk.com disclaimer redirect automatically.
    Uses the Playwright page passed in (caller manages browser lifecycle).
    """
    base = "https://or.duvalclerk.com"
    try:
        from duval_clerk_scraper import DUVAL_CLERK_BASE_URL as _base
        base = _base
    except Exception:
        pass

    # Build URL candidates — DocLink from scraper takes priority
    candidates: list[str] = []
    doc_link_m = _LP_DOC_LINK_RE.search(raw_text or "")
    if doc_link_m:
        candidates.append(doc_link_m.group(1))
    if instrument:
        candidates += [
            f"{base}/Search/Image/DocType/OR/InstrumentNumber/{instrument}",
            f"{base}/Search/Details?InstrumentNumber={instrument}",
        ]
    if book and page:
        candidates.append(f"{base}/Search/Image/DocType/OR/Book/{book}/Page/{page}")

    _DOC_KEYWORDS = ("lis pendens", "mortgage", "property", "parcel", "grantor",
                     "plaintiff", "defendant", "real property", "legal description")

    for url in candidates:
        try:
            or_page.goto(url, wait_until="domcontentloaded", timeout=20_000)

            # Accept disclaimer redirect if the site bounces us to ToS
            if "disclaimer" in or_page.url.lower():
                try:
                    btn = or_page.query_selector("input[type='submit'], button[type='submit']")
                    if btn:
                        btn.click()
                        or_page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        # Navigate to the intended URL now that disclaimer is accepted
                        or_page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                except Exception:
                    pass

            # Wait briefly for JS-rendered content
            try:
                or_page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass

            text    = or_page.inner_text("body") or ""
            stripped = re.sub(r"\s+", " ", text).strip()
            if len(stripped) > 200 and any(kw in stripped.lower() for kw in _DOC_KEYWORDS):
                logger.debug("  OR doc text fetched via %s (%d chars)", url, len(stripped))
                return stripped
            logger.debug("  OR doc URL %s: page too short or no doc keywords (%d chars)", url, len(stripped))
        except Exception as e:
            logger.debug("  OR doc URL %s failed: %s", url, e)

    return ""


def _dcpa_first_name_matches(dcpa_owner: str, expected_first: str) -> bool:
    """Return True if the expected first name token appears in the DCPA owner name.

    Guards against last-name-only matches where the score is artificially inflated
    by a shared middle name (e.g. 'MITCHELL CHERYL ANN' matching 'Mitchell Felecia Ann'
    because both share 'ANN' as a middle name token).
    """
    if not expected_first:
        return True  # no first name to check — trust the score alone
    first_tok = expected_first.upper().split()[0]  # first token of the first name
    return first_tok in dcpa_owner.upper().split()


def _lis_pendens_address_lookup(notices: list["NoticeData"]) -> None:
    """DCPA owner-name lookup for Duval County lis pendens records without addresses.

    Grantor (owner_name) comes from the Duval Clerk index in LAST FIRST format.
    Tier 1 : DCPA name search (Playwright) — requires first-name token match
    Tier 1b: OR document text fetch — parses address from the filed document
    Tier 2 : Tracerfy instant trace
    Tier 3 : People search (Serper + Firecrawl + LLM)
    Updates notices in-place.
    """
    candidates = [
        n for n in notices
        if n.notice_type == "lis_pendens"
        and n.county.lower() == "duval"
        and not n.address.strip()
        and n.owner_name.strip()
    ]
    if not candidates:
        logger.info("  No lis pendens candidates for DCPA address lookup")
        return

    logger.info("  DCPA address lookup for %d lis pendens grantor(s)", len(candidates))

    dcpa_page = None
    dcpa_browser = None
    dcpa_pw_ctx = None
    try:
        from playwright.sync_api import sync_playwright
        dcpa_pw_ctx = sync_playwright().start()
        dcpa_browser = dcpa_pw_ctx.chromium.launch(headless=True)
        dcpa_page = dcpa_browser.new_page()
        # Second page for OR document viewer (same browser, separate tab)
        or_doc_page = dcpa_browser.new_page()
        logger.debug("  DCPA browser ready for lis pendens lookup")
    except Exception as e:
        logger.warning("  Could not start DCPA browser: %s", e)
        or_doc_page = None

    try:
        # Patterns for OR-index name artifacts
        _ESTATE_OF_RE  = re.compile(r"^(?:THE\s+)?ESTATE\s+OF\s+", re.IGNORECASE)
        _DECEASED_RE   = re.compile(r"\bDECEASED\b\.?", re.IGNORECASE)
        _NAME_SFXS_RE  = re.compile(r"\b(?:JR|SR|II|III|IV|V|ESQ|MD|DO|DDS|PHD)\b\.?", re.IGNORECASE)

        for notice in candidates:
            grantor = notice.owner_name.strip()
            logger.info("  LP address lookup: %s", grantor)

            # ── Normalise grantor name from OR-index quirks ───────────
            # Detect "THE ESTATE OF FIRSTNAME LASTNAME" — name is in FIRST LAST order
            is_estate = bool(_ESTATE_OF_RE.match(grantor))
            clean = _ESTATE_OF_RE.sub("", grantor).strip()   # strip estate prefix
            clean = _DECEASED_RE.sub("", clean).strip()       # strip DECEASED suffix
            clean = re.sub(r"\s+", " ", clean).strip()

            if is_estate:
                # After stripping "ESTATE OF", the remaining name is FIRST [MIDDLE] LAST
                estate_parts = clean.upper().split()
                last  = estate_parts[-1] if estate_parts else clean.upper()
                first = " ".join(estate_parts[:-1]) if len(estate_parts) > 1 else ""
            else:
                # Standard OR-index format: LAST FIRST [MIDDLE] [SUFFIX]
                parts = clean.upper().split()
                last  = parts[0] if parts else clean.upper()
                first = " ".join(parts[1:]) if len(parts) > 1 else ""

            # Build FIRST LAST form for people-search — strip trailing suffixes
            first_no_sfx = _NAME_SFXS_RE.sub("", first).strip()
            first_no_sfx = re.sub(r"\s+", " ", first_no_sfx).strip()
            first_last = f"{first_no_sfx} {last}".strip() if first_no_sfx else last

            # Extract instrument # and book/page from raw_text for OR doc lookup
            instr_m = _LP_INSTRUMENT_RE.search(notice.raw_text or "")
            bk_m    = re.search(r"Book/Page:\s*(\d+)/(\d+)", notice.raw_text or "", re.IGNORECASE)
            instrument = instr_m.group(1) if instr_m else ""
            book       = bk_m.group(1) if bk_m else ""
            page       = bk_m.group(2) if bk_m else ""

            # ── Tier 1: DCPA by grantor name (with first-name guard) ──────
            dcpa_results: list[dict] = []
            if dcpa_page is not None:
                try:
                    dcpa_results = _search_dcpa_by_name(last, first_no_sfx, dcpa_page)
                    if not dcpa_results and first_no_sfx != first:
                        dcpa_results = _search_dcpa_by_name(last, first, dcpa_page)
                    if not dcpa_results and first_no_sfx:
                        dcpa_results = _search_dcpa_by_name(last, "", dcpa_page)
                    # Fallback: the LLM sometimes reorders OR-index LAST FIRST names to
                    # FIRST LAST (e.g. "HAROLD CLAUDE NOLAN III").  If the first attempt
                    # with last=parts[0] finds nothing, retry with last=last_word.
                    if not dcpa_results and not is_estate:
                        parts_rev = clean.upper().split()
                        alt_last = _NAME_SFXS_RE.sub("", parts_rev[-1]).strip() if parts_rev else ""
                        if alt_last and alt_last != last:
                            alt_first = " ".join(parts_rev[:-1])
                            dcpa_results = _search_dcpa_by_name(alt_last, alt_first, dcpa_page)
                            if not dcpa_results:
                                dcpa_results = _search_dcpa_by_name(alt_last, "", dcpa_page)
                            if dcpa_results:
                                logger.debug("  DCPA retry with reversed last=%s found %d result(s)", alt_last, len(dcpa_results))
                    if dcpa_results:
                        best  = max(dcpa_results, key=lambda r: _score_dcpa_name(r["owner_name"], grantor))
                        score = _score_dcpa_name(best["owner_name"], grantor)
                        if score >= 0.4 and _dcpa_first_name_matches(best["owner_name"], first):
                            notice.address  = best["address"]
                            notice.city     = best["city"]
                            notice.state    = "FL"
                            if best.get("zip"):
                                notice.zip = best["zip"]
                            if best.get("re_number"):
                                notice.parcel_id = best["re_number"]
                            logger.info(
                                "  Tier 1 (DCPA): %s, %s (matched: %s, score=%.2f)",
                                notice.address, notice.city, best["owner_name"], score,
                            )
                            continue
                        logger.debug(
                            "  DCPA match rejected: score=%.2f first_name_ok=%s owner=%s",
                            score, _dcpa_first_name_matches(best["owner_name"], first),
                            best["owner_name"],
                        )
                except Exception as e:
                    logger.debug("  DCPA failed for %s: %s", grantor, e)

            # ── Tier 1a: DCPA detail page — check for secondary owners ──
            # Handles trust-owned properties and co-owners where the grantor is
            # listed as a secondary (non-primary) owner on the DCPA parcel page.
            if dcpa_results and dcpa_page is not None and not notice.address.strip():
                first_tok = first.split()[0] if first else ""
                for result in dcpa_results[:5]:
                    re_clean = result.get("re_number", "").replace("-", "")
                    if not re_clean:
                        continue
                    try:
                        dcpa_page.goto(
                            f"https://paopropertysearch.coj.net/Basic/Detail.aspx?RE={re_clean}",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        detail_text = (dcpa_page.inner_text("body") or "").upper()
                        last_present  = last in detail_text
                        first_present = not first_tok or first_tok in detail_text
                        if last_present and first_present:
                            notice.address   = result["address"]
                            notice.city      = result.get("city", "Jacksonville")
                            notice.state     = "FL"
                            if result.get("zip"):
                                notice.zip = result["zip"]
                            if result.get("re_number"):
                                notice.parcel_id = result["re_number"]
                            logger.info(
                                "  Tier 1a (DCPA detail): %s, %s (parcel: %s, primary: %s)",
                                notice.address, notice.city, re_clean, result["owner_name"],
                            )
                            break
                    except Exception as e:
                        logger.debug("  DCPA detail check failed for %s (%s): %s",
                                     grantor, re_clean, e)
                if notice.address.strip():
                    continue

            # ── Tier 1b: OR document text — parse address from filed document ──
            if or_doc_page is not None and (instrument or (book and page) or _LP_DOC_LINK_RE.search(notice.raw_text or "")):
                try:
                    doc_text = _fetch_or_doc_text(instrument, book, page, or_doc_page, notice.raw_text or "")
                    if doc_text:
                        m = _FL_STREET_ADDR_RE.search(doc_text)
                        if m:
                            addr = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",.")
                            city = re.sub(r"\s+", " ", m.group(2)).strip().rstrip(",.")
                            zip_ = m.group(3) or ""
                            if addr and not re.search(r"\bSte\b|\bSuite\b", addr, re.I):
                                notice.address = addr
                                notice.city    = city or "Jacksonville"
                                notice.state   = "FL"
                                if zip_:
                                    notice.zip = zip_
                                logger.info(
                                    "  Tier 1b (OR doc): %s, %s", notice.address, notice.city
                                )
                                continue
                except Exception as e:
                    logger.debug("  OR doc text failed for %s: %s", grantor, e)

            # ── Tier 2: People search (Serper + Firecrawl + LLM) ────────
            # Note: Tracerfy requires a known street address as input — not usable here
            # because finding that address is exactly what we are trying to do.
            if _cfg.ANTHROPIC_API_KEY:
                try:
                    from obituary_enricher import _lookup_dm_address_serper_firecrawl
                    result = _lookup_dm_address_serper_firecrawl(
                        first_last, "Jacksonville", _cfg.ANTHROPIC_API_KEY,
                        state_full="Florida", state_abbr="FL",
                    )
                    if result and result.get("street"):
                        notice.address = result["street"]
                        notice.city    = result.get("city", "Jacksonville")
                        notice.state   = "FL"
                        if result.get("zip"):
                            notice.zip = result["zip"]
                        logger.info("  Tier 2 (People Search): %s, %s", notice.address, notice.city)
                        continue
                except Exception as e:
                    logger.debug("  People search failed for %s: %s", grantor, e)

            logger.warning("  No address found for LP grantor: %s (all tiers exhausted)", grantor)

    finally:
        if dcpa_browser:
            try:
                dcpa_browser.close()
            except Exception:
                pass
        if dcpa_pw_ctx:
            try:
                dcpa_pw_ctx.stop()
            except Exception:
                pass


def _apply_parcel_to_notice(notice: NoticeData, parcel: dict) -> None:
    """Apply parcel data from Knox Tax API to a notice."""
    notice.address = parcel.get("parcel_address", "")
    notice.city = "Knoxville"
    notice.state = "TN"
    account = parcel.get("account_number", "")
    if account:
        notice.parcel_id = account
    owner = parcel.get("owner", "")
    if owner and not notice.tax_owner_name:
        notice.tax_owner_name = owner
