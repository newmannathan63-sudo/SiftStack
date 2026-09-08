"""Track property address stage progression (lis_pendens -> foreclosure).

Duval lis pendens filings (duval_clerk_scraper.py) and Jacksonville Daily
Record foreclosure sale notices (jdr_scraper.py, category "Notice of Sale -
Foreclosure") are scraped independently with no shared dedup key. A property
that already came through as a lis_pendens/pre-foreclosure lead therefore
shows up later as an unrelated "new" foreclosure record, with no signal that
it's the same deal progressing.

This module persists a lightweight address -> last-seen-stage registry so
that transition can be detected and tagged in the DataSift upload rather than
relying on DataSift's own (unreliable — see the Deerwood Lake phantom-
duplicate case) address dedup on bulk import.
"""

import json
import re
from pathlib import Path

from notice_parser import NoticeData

# Lower rank = earlier in the foreclosure timeline. Only notice types in this
# map participate in conversion tracking.
STAGE_RANK = {
    "lis_pendens": 1,
    "foreclosure": 2,
}

CONVERSION_TAG = "Preforeclosure_to_Foreclosure"

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "address_stage_history.json"


def normalize_address(address: str) -> str:
    """Loose key for matching the same property across scrapers/runs.

    Duval Clerk and JDR format the same address slightly differently, so this
    is intentionally forgiving (case, punctuation, whitespace) rather than a
    strict canonical form.
    """
    if not address:
        return ""
    addr = address.upper().strip()
    addr = re.sub(r"[.,#]", "", addr)
    addr = re.sub(r"\s+", " ", addr)
    return addr


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_registry(registry: dict, path: Path = REGISTRY_PATH) -> None:
    path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def check_and_record(notice: NoticeData, registry: dict) -> str | None:
    """Check whether `notice` is a stage-up conversion for its address.

    Updates `registry` in place with the notice's stage if it's new-to-us or
    higher than what's recorded. Returns the conversion tag to add to the
    notice's Tags column when a higher stage is reached for an address we've
    already seen at a lower stage, else None.
    """
    stage = STAGE_RANK.get(notice.notice_type)
    if stage is None:
        return None

    key = normalize_address(notice.address)
    if not key:
        return None

    prior = registry.get(key)
    conversion_tag = None
    if prior and stage > prior.get("stage_rank", 0):
        conversion_tag = CONVERSION_TAG

    if not prior or stage >= prior.get("stage_rank", 0):
        registry[key] = {
            "stage_rank": stage,
            "notice_type": notice.notice_type,
            "date_added": notice.date_added,
            "case_number": notice.case_number,
        }

    return conversion_tag
