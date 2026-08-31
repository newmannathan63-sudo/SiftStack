"""SmartSkip batch skip trace — additive phones + emails, on top of Tracerfy.

SmartSkip has no public API (dashboard/CSV-upload only), so this automates
their bulk-search UI via Playwright: log in, upload a CSV of contacts, wait
for the job to process, download the results CSV, and merge back into
NoticeData objects.

Unlike Tracerfy (tracerfy_skip_tracer.py), this runs on EVERY contact
regardless of what Tracerfy already found — the point is added phone/email
coverage, not just filling gaps. Any number/email SmartSkip returns that a
notice doesn't already have gets appended into the next empty slot; anything
it returns that duplicates what's already there is dropped. Existing slots
are never overwritten.

Confirmed live 2026-08-30 against the real site (one real $0.50 test charge):
login, upload, column-mapping (drag-and-drop, not dropdowns), payment
(a confirm modal charging a saved card — a "$50.00" balance shown elsewhere
in the UI was not actually drawn down by this charge, so its role is
unconfirmed), async job processing, and the "CRM Format" results download.
See smartskip_core module docstring for context on this app's structure
(Vuetify SPA, app.smartskip.io subdomain).
"""

import csv
import io
import json
import logging
from pathlib import Path

import config as cfg
from notice_parser import NoticeData
from smartskip_core import create_browser, login, screenshot

logger = logging.getLogger(__name__)

# Same NoticeData phone/email slot fields Tracerfy uses.
PHONE_FIELDS = [
    "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
    "mobile_5", "landline_1", "landline_2", "landline_3",
]
EMAIL_FIELDS = ["email_1", "email_2", "email_3", "email_4", "email_5"]

# Confirmed live 2026-08-30 from a real "CRM Format" results download —
# SmartSkip returns up to 15 phones and 20 emails per matched contact.
RESULT_PHONE_COLS = [f"Phone {i} number" for i in range(1, 16)]
RESULT_EMAIL_COLS = [f"Email {i}" for i in range(1, 21)]
RESULT_FIRST_NAME_COL = "First Name"
RESULT_LAST_NAME_COL = "Last Name"


def _split_name(name: str) -> tuple[str, str]:
    """Split a full name into (first, last). Returns ('', '') if unparseable."""
    parts = name.strip().split()
    if len(parts) < 2:
        return ("", "")
    return (parts[0], parts[-1])


def _get_all_contacts(
    notices: list[NoticeData], max_signing_traces: int = 5,
) -> list[tuple[NoticeData, str, str, str, str, str, str]]:
    """Every contact worth tracing — DM #1 + signing heirs for deceased owners,
    the owner for living owners. No "already has a phone" skip: SmartSkip runs
    on everyone, since the goal is additional coverage, not gap-filling.

    Returns list of (notice, first, last, address, city, zip, heir_key).
    """
    contacts: list[tuple[NoticeData, str, str, str, str, str, str]] = []

    for notice in notices:
        if (notice.owner_deceased == "yes"
                and notice.decision_maker_name
                and notice.decision_maker_name.strip()):
            dm_name = notice.decision_maker_name.strip()
            address = notice.decision_maker_street or notice.address or ""
            city_val = notice.decision_maker_city or notice.city or ""
            zip_code = notice.decision_maker_zip or notice.zip or ""
            first, last = _split_name(dm_name)
            if first and last:
                contacts.append((notice, first, last, address, city_val, zip_code, dm_name))

            if notice.heir_map_json:
                try:
                    heirs = json.loads(notice.heir_map_json)
                except (json.JSONDecodeError, TypeError):
                    heirs = []

                seen = {dm_name.lower()}
                signing_count = 0
                for heir in heirs:
                    if signing_count >= max_signing_traces:
                        break
                    heir_name = heir.get("name", "").strip()
                    if not heir_name or heir_name.lower() in seen:
                        continue
                    if not heir.get("signing_authority"):
                        continue
                    if heir.get("status") == "deceased":
                        continue
                    if not heir.get("street"):
                        continue
                    seen.add(heir_name.lower())
                    signing_count += 1
                    h_first, h_last = _split_name(heir_name)
                    if h_first and h_last:
                        contacts.append((
                            notice, h_first, h_last,
                            heir["street"], heir.get("city", ""), heir.get("zip", ""),
                            heir_name,
                        ))
        else:
            name = (notice.owner_name or "").strip()
            if name:
                first, last = _split_name(name)
                if first and last:
                    contacts.append((
                        notice, first, last,
                        notice.address or "", notice.city or "", notice.zip or "",
                        name,
                    ))

    return contacts


def _existing_values(notice: NoticeData, heir_key: str, fields: list[str]) -> set[str]:
    """Values already present for this contact — DM #1 flat fields, or a
    heir's phones/emails inside heir_map_json — used to dedupe SmartSkip's
    results against what's already there."""
    is_primary = (
        notice.decision_maker_name
        and heir_key.lower() == notice.decision_maker_name.strip().lower()
    ) or notice.owner_deceased != "yes"

    if is_primary:
        return {(getattr(notice, f, "") or "").strip() for f in fields if getattr(notice, f, "")}

    if not notice.heir_map_json:
        return set()
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("name", "").lower() == heir_key.lower():
                key = "phones" if fields is PHONE_FIELDS else "emails"
                return set(h.get(key, []) or [])
    except (json.JSONDecodeError, TypeError):
        pass
    return set()


def _append_additive(
    notice: NoticeData, heir_key: str,
    new_phones: list[str], new_emails: list[str],
) -> tuple[int, int]:
    """Append genuinely new phones/emails into the next empty slot, without
    overwriting anything already there. Returns (phones_added, emails_added)."""
    is_primary = (
        notice.decision_maker_name
        and heir_key.lower() == notice.decision_maker_name.strip().lower()
    ) or notice.owner_deceased != "yes"

    if is_primary:
        added_phones = 0
        for phone in new_phones:
            for field in PHONE_FIELDS:
                if not getattr(notice, field, ""):
                    setattr(notice, field, phone)
                    added_phones += 1
                    break
        added_emails = 0
        for email in new_emails:
            for field in EMAIL_FIELDS:
                if not getattr(notice, field, ""):
                    setattr(notice, field, email)
                    added_emails += 1
                    break
        return added_phones, added_emails

    # Heir — append into their heir_map_json entry's phones/emails lists.
    if not notice.heir_map_json:
        return 0, 0
    try:
        heirs = json.loads(notice.heir_map_json)
    except (json.JSONDecodeError, TypeError):
        return 0, 0

    added_phones = added_emails = 0
    for h in heirs:
        if h.get("name", "").lower() != heir_key.lower():
            continue
        phones = list(h.get("phones", []) or [])
        emails = list(h.get("emails", []) or [])
        for phone in new_phones:
            if phone not in phones:
                phones.append(phone)
                added_phones += 1
        for email in new_emails:
            if email not in emails:
                emails.append(email)
                added_emails += 1
        h["phones"] = phones
        h["emails"] = emails
        break

    if added_phones or added_emails:
        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)
    return added_phones, added_emails


async def _run_bulk_search(
    contacts: list[tuple[NoticeData, str, str, str, str, str, str]],
    headless: bool = False,
) -> list[dict]:
    """Log in to SmartSkip, upload the contacts as a bulk-search CSV, pay the
    per-row charge, wait for the job to finish, and return the parsed
    "CRM Format" results rows.

    Confirmed live 2026-08-30 (one real $0.50 test charge) — see module
    docstring.
    """
    import time as _time

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["first_name", "last_name", "address", "city", "state", "zip"])
    for notice, first, last, address, city, zip_code, _ in contacts:
        writer.writerow([first, last, address, city, notice.state or "TN", zip_code])
    csv_content = csv_buffer.getvalue()
    csv_buffer.close()

    # Timestamped so we can find *this* job's row in Skips History even if an
    # older job with a generic name is still listed there.
    upload_filename = f"siftstack_smartskip_{int(_time.time())}.csv"

    async with create_browser(headless=headless) as (browser, context, page):
        logged_in = await login(page)
        if not logged_in:
            logger.error("SmartSkip login failed — aborting batch")
            return []

        from smartskip_core import SMARTSKIP_BULK_URL
        await page.goto(SMARTSKIP_BULK_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Step 1: Upload .csv — Vuetify SPA, no static <input type="file">;
        # clicking "Choose .csv file" opens a native OS file-chooser dialog.
        tmp_path = Path("output") / upload_filename
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(csv_content, encoding="utf-8")

        choose_btn = page.locator('button.file-btn')
        if await choose_btn.count() == 0:
            body_text = (await page.inner_text("body"))[:800]
            logger.error(
                "SmartSkip: no 'Choose .csv file' button found. Page text: %s", body_text,
            )
            return []
        async with page.expect_file_chooser() as fc_info:
            await choose_btn.first.click()
        chooser = await fc_info.value
        await chooser.set_files(str(tmp_path.resolve()))
        await page.wait_for_timeout(2000)
        await screenshot(page, "bulk_file_selected")

        # Step 2: Map the columns — a drag-and-drop widget, not dropdowns.
        # Each CSV column is a chip (.start-zone .item); each target field is
        # a drop zone (.final-zone), in fixed DOM order: 0 First Name,
        # 1 Last Name, 2 Mailing Address, 3 Middle Name, 4 Mailing City,
        # 5 Mailing State, 6 Mailing Zip, 7-10 Property Address/City/State/Zip.
        zones = page.locator(".final-zone")
        column_to_zone = {
            "first_name": 0, "last_name": 1, "address": 2,
            "city": 4, "state": 5, "zip": 6,
        }
        for col, zone_idx in column_to_zone.items():
            chip = page.locator(".start-zone .item", has_text=col).first
            if await chip.count() == 0:
                continue
            await chip.drag_to(zones.nth(zone_idx))
            await page.wait_for_timeout(500)
        await screenshot(page, "bulk_mapped")

        # Vuetify keeps prior steps' buttons in the DOM but hidden — always
        # target the currently-visible "Next", not .first in DOM order.
        await page.locator('button:has-text("Next"):visible').click()  # mapping -> preview
        await page.wait_for_timeout(1500)
        await page.locator('button:has-text("Next"):visible').click()  # preview -> payment
        await page.wait_for_timeout(1500)
        await screenshot(page, "bulk_payment")

        # Step 4: Payment — "Pay and get results" opens a payment-method
        # modal (saved card + a final "Pay $X.XX" confirm). Minimum $0.50
        # charge per submission regardless of row count/results.
        pay_btn = page.locator('button:has-text("Pay and get results"):visible')
        if await pay_btn.count() == 0:
            logger.error("SmartSkip: no 'Pay and get results' button on payment step")
            return []
        await pay_btn.click()
        await page.wait_for_timeout(1500)

        saved_card = page.locator(r"text=/\*\*\*\* \d{4}/").first
        if await saved_card.count() > 0:
            await saved_card.click()
            await page.wait_for_timeout(500)
        confirm_btn = page.locator('button:has-text("Pay $"):visible')
        if await confirm_btn.count() == 0:
            logger.error("SmartSkip: no payment-confirm button found — aborting before charging")
            return []
        await confirm_btn.click()
        await page.wait_for_timeout(3000)
        await screenshot(page, "bulk_submitted")
        logger.info("SmartSkip bulk search paid and submitted — waiting for results...")

        # Job runs async ("may take some time" per SmartSkip's own copy).
        # Poll the Skips History table on the bulk-skip page for our upload's
        # row to show "Completed", then download the CRM-format results.
        download_path = None
        for attempt in range(60):
            await page.goto(SMARTSKIP_BULK_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            row = page.locator("tr", has_text=upload_filename)
            if await row.count() > 0:
                status_text = await row.first.locator(".status").inner_text()
                if "Completed" in status_text:
                    await row.first.locator("button.download-button").click()
                    await page.wait_for_timeout(1000)
                    crm_option = page.locator('text="Download CRM Format"')
                    try:
                        async with page.expect_download(timeout=15000) as dl_info:
                            await crm_option.first.click()
                        download = await dl_info.value
                        download_path = Path("output") / f"{upload_filename}_results.csv"
                        await download.save_as(str(download_path))
                        logger.info("SmartSkip results downloaded: %s", download_path)
                    except Exception as e:
                        logger.warning("SmartSkip download failed: %s", e)
                    break
            if attempt % 6 == 5:
                logger.info("  SmartSkip bulk job still processing (%ds)...", (attempt + 1) * 10)
            await page.wait_for_timeout(7000)

        if not download_path or not download_path.exists():
            logger.warning("SmartSkip bulk job did not produce a downloadable result in time")
            return []

        with open(download_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        return rows


def _match_and_merge(
    rows: list[dict],
    contacts: list[tuple[NoticeData, str, str, str, str, str, str]],
    stats: dict,
) -> None:
    """Match SmartSkip result rows back to contacts by name, and merge
    additively — new values appended to empty slots, duplicates dropped."""
    for row in rows:
        rec_first = (row.get(RESULT_FIRST_NAME_COL) or "").strip().lower()
        rec_last = (row.get(RESULT_LAST_NAME_COL) or "").strip().lower()
        if not rec_first or not rec_last:
            continue

        for notice, first, last, _addr, _city, _zip, heir_key in contacts:
            if first.lower() != rec_first or last.lower() != rec_last:
                continue

            found_phones = [
                (row.get(c) or "").strip() for c in RESULT_PHONE_COLS if (row.get(c) or "").strip()
            ]
            found_emails = [
                (row.get(c) or "").strip() for c in RESULT_EMAIL_COLS if (row.get(c) or "").strip()
            ]
            if not found_phones and not found_emails:
                break

            existing_phones = _existing_values(notice, heir_key, PHONE_FIELDS)
            existing_emails = _existing_values(notice, heir_key, EMAIL_FIELDS)
            new_phones = [p for p in found_phones if p not in existing_phones]
            new_emails = [e for e in found_emails if e not in existing_emails]

            added_p, added_e = _append_additive(notice, heir_key, new_phones, new_emails)
            if added_p or added_e:
                stats["matched"] += 1
                stats["phones_found"] += added_p
                stats["emails_found"] += added_e
                logger.info("    %s %s: +%d new phones, +%d new emails",
                            first, last, added_p, added_e)
            break


def batch_skip_trace(notices: list[NoticeData], max_signing_traces: int = 5) -> dict:
    """Run SmartSkip on every contact and merge new phones/emails additively.

    Same stats shape as tracerfy_skip_tracer.batch_skip_trace so call sites can
    log/summarize both the same way. Synchronous wrapper — internally runs the
    Playwright automation via asyncio.run.
    """
    import asyncio

    stats = {
        "total": len(notices),
        "submitted": 0,
        "matched": 0,
        "phones_found": 0,
        "emails_found": 0,
        "cost": 0.0,
        "credits_exhausted": False,
    }

    if not cfg.SMARTSKIP_EMAIL or not cfg.SMARTSKIP_PASSWORD:
        logger.warning("SmartSkip credentials not set — skipping batch skip trace")
        return stats

    contacts = _get_all_contacts(notices, max_signing_traces)
    if not contacts:
        logger.info("SmartSkip: no contacts to trace (no valid names)")
        return stats

    stats["submitted"] = len(contacts)
    logger.info("SmartSkip batch: submitting %d contacts (%d notices) — ~$%.2f",
                len(contacts), len(notices), len(contacts) * 0.15)

    try:
        rows = asyncio.run(_run_bulk_search(contacts))
    except Exception as e:
        logger.warning("SmartSkip batch skip trace failed: %s", e)
        return stats

    if not rows:
        return stats

    _match_and_merge(rows, contacts, stats)
    stats["cost"] = stats["submitted"] * 0.15
    logger.info("SmartSkip batch complete: %d/%d matched, %d new phones, %d new emails, $%.2f",
                stats["matched"], stats["submitted"],
                stats["phones_found"], stats["emails_found"], stats["cost"])
    return stats


if __name__ == "__main__":
    # Standalone live-debug harness — run with real credentials and a couple
    # of test contacts, headless=False, to discover/verify real selectors
    # before trusting this against production data. Mirrors the approach in
    # feedback_datasift_live_debug memory.
    logging.basicConfig(level=logging.INFO)

    test_notice = NoticeData(
        owner_name="John Smith",
        address="123 Main St",
        city="Knoxville",
        state="TN",
        zip="37902",
    )
    result = batch_skip_trace([test_notice])
    print(json.dumps(result, indent=2))
    print("primary_phone:", test_notice.primary_phone)
    print("mobile_1:", test_notice.mobile_1)
