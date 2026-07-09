"""Sync the daily Apify Actor run's CSV output back to the local hard drive.

The 6am scheduled Apify Actor run writes output.csv and the DataSift split
CSVs into the persistent "siftstack-state" key-value store on Apify's side —
that store never reaches this machine on its own (the Actor's local
filesystem is an ephemeral cloud container). This module polls the Apify
API for the most recent successful scheduled run and downloads those CSVs
into OUTPUT_DIR, so a local copy exists after every daily run, not just
manual/CLI ones.
"""

import logging
from datetime import datetime, timezone

import requests

import config
from config import OUTPUT_DIR, PROJECT_ROOT, load_state, save_state

logger = logging.getLogger(__name__)

APIFY_ACTOR_ID = "newman_n~tn-public-notice-scraper"
APIFY_KVS_STORE_NAME = "siftstack-state"
SYNC_STATE_FILE = PROJECT_ROOT / "apify_sync_state.json"


def _api_get(path: str, **params) -> dict:
    params["token"] = config.APIFY_TOKEN
    resp = requests.get(f"https://api.apify.com/v2{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"]


def _latest_scheduled_run() -> dict | None:
    """Return the most recent SCHEDULER-triggered run, or None."""
    # requests serializes Python bools as "True"/"False" (capitalized), which
    # Apify's API doesn't recognize as truthy — must pass lowercase strings.
    data = _api_get(f"/acts/{APIFY_ACTOR_ID}/runs", desc="true", limit=10)
    for run in data["items"]:
        if run.get("meta", {}).get("origin") == "SCHEDULER":
            return run
    return None


def _kvs_store_id() -> str:
    data = _api_get("/key-value-stores", unnamed="false")
    for store in data["items"]:
        if store["name"] == APIFY_KVS_STORE_NAME:
            return store["id"]
    raise RuntimeError(f"Key-value store '{APIFY_KVS_STORE_NAME}' not found")


def _download_record(store_id: str, key: str) -> bytes:
    url = f"https://api.apify.com/v2/key-value-stores/{store_id}/records/{key}"
    resp = requests.get(url, params={"token": config.APIFY_TOKEN}, timeout=60)
    resp.raise_for_status()
    return resp.content


def sync_latest_output(force: bool = False) -> dict:
    """Download the latest scheduled run's CSVs to OUTPUT_DIR if not already synced.

    Idempotent: safe to call repeatedly (e.g. from a polling scheduled task) —
    skips if the latest scheduled run was already synced, unless force=True.
    """
    if not config.APIFY_TOKEN:
        return {"synced": False, "reason": "APIFY_TOKEN not set"}

    run = _latest_scheduled_run()
    if not run:
        return {"synced": False, "reason": "No scheduled runs found"}

    if run["status"] != "SUCCEEDED":
        return {
            "synced": False,
            "reason": f"Latest scheduled run status={run['status']}",
            "run_id": run["id"],
        }

    state = load_state(SYNC_STATE_FILE)
    if not force and state.get("last_synced_run_id") == run["id"]:
        return {"synced": False, "reason": "Already synced", "run_id": run["id"]}

    store_id = _kvs_store_id()
    keys_data = _api_get(f"/key-value-stores/{store_id}/keys", limit=1000)
    csv_keys = [item["key"] for item in keys_data["items"] if item["key"].endswith(".csv")]

    if not csv_keys:
        return {"synced": False, "reason": "No CSV keys in KVS store", "run_id": run["id"]}

    started = run["startedAt"]  # e.g. "2026-07-07T10:00:02.418Z"
    ts = datetime.strptime(started, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%d_%H%M%S")

    saved = []
    for key in csv_keys:
        content = _download_record(store_id, key)
        stem = key[:-4]  # strip ".csv"
        path = OUTPUT_DIR / f"apify_{stem}_{ts}.csv"
        path.write_bytes(content)
        saved.append(str(path))
        logger.info("Synced %s -> %s (%d bytes)", key, path, len(content))

    state["last_synced_run_id"] = run["id"]
    state["last_synced_at"] = datetime.now(timezone.utc).isoformat()
    save_state(SYNC_STATE_FILE, state)

    return {"synced": True, "run_id": run["id"], "files": saved}
