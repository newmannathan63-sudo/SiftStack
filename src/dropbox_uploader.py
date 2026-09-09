"""Upload output files to Dropbox and return public share links.

Used by the daily pipeline to attach CSV + deep-prospecting PDF links into
Slack run summaries. Reuses the same Dropbox credentials that the courthouse
photo ingestion (dropbox_watcher.py) already relies on.
"""

from __future__ import annotations

import logging
from pathlib import Path

import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode
from dropbox.sharing import RequestedVisibility, SharedLinkSettings

import config

logger = logging.getLogger(__name__)


def _get_client() -> dropbox.Dropbox:
    # Prefer a short-lived DROPBOX_ACCESS_TOKEN when present (useful for
    # one-off runs before a refresh token is provisioned). Falls back to
    # the refresh-token flow for long-lived scheduled runs.
    import os
    access_token = os.environ.get("DROPBOX_ACCESS_TOKEN", "").strip()
    if access_token:
        return dropbox.Dropbox(oauth2_access_token=access_token)

    if not config.DROPBOX_REFRESH_TOKEN:
        raise ValueError(
            "No Dropbox credentials — set DROPBOX_ACCESS_TOKEN (short-lived) "
            "or DROPBOX_REFRESH_TOKEN (permanent) in .env"
        )
    if not config.DROPBOX_APP_KEY:
        raise ValueError("DROPBOX_APP_KEY not set in .env")
    return dropbox.Dropbox(
        oauth2_refresh_token=config.DROPBOX_REFRESH_TOKEN,
        app_key=config.DROPBOX_APP_KEY,
        app_secret=config.DROPBOX_APP_SECRET or None,
    )


def _ensure_shared_link(dbx: dropbox.Dropbox, path: str) -> str:
    """Return a public share URL for path, creating one if it doesn't exist yet."""
    try:
        link = dbx.sharing_create_shared_link_with_settings(
            path,
            settings=SharedLinkSettings(requested_visibility=RequestedVisibility.public),
        )
        return link.url
    except ApiError as e:
        # If a link already exists, Dropbox returns shared_link_already_exists.
        # Fetch the existing one instead of failing.
        err = getattr(e, "error", None)
        if err is not None and getattr(err, "is_shared_link_already_exists", lambda: False)():
            existing = dbx.sharing_list_shared_links(path=path, direct_only=True).links
            if existing:
                return existing[0].url
        raise


def upload_bytes_and_share(
    data: bytes,
    dropbox_dest_path: str,
    dbx: dropbox.Dropbox | None = None,
) -> str | None:
    """Upload in-memory bytes to Dropbox and return a public share URL.

    Args:
        data: file content to upload.
        dropbox_dest_path: absolute path in Dropbox (must start with "/").
        dbx: optional preexisting client (lets callers upload many files
            in a single session without re-authenticating).

    Returns:
        Public share URL, or None on any failure (logged with context).
    """
    close_after = False
    if dbx is None:
        try:
            dbx = _get_client()
            close_after = True
        except Exception:
            logger.exception("Dropbox client init failed")
            return None

    try:
        dbx.files_upload(
            data, dropbox_dest_path, mode=WriteMode.overwrite, mute=True,
        )
        url = _ensure_shared_link(dbx, dropbox_dest_path)
        logger.info("Dropbox uploaded: %s (%d bytes) → %s", dropbox_dest_path, len(data), url)
        return url
    except Exception:
        logger.exception("Dropbox upload failed: %s", dropbox_dest_path)
        return None
    finally:
        if close_after:
            try:
                dbx.close()
            except Exception:
                pass


def upload_and_share(
    local_path: Path,
    dropbox_dest_path: str,
    dbx: dropbox.Dropbox | None = None,
) -> str | None:
    """Upload a local file to Dropbox and return a public share URL.

    Args:
        local_path: file on disk to upload.
        dropbox_dest_path: absolute path in Dropbox (must start with "/").
        dbx: optional preexisting client (lets callers upload many files
            in a single session without re-authenticating).

    Returns:
        Public share URL, or None on any failure (logged with context).
    """
    local_path = Path(local_path)
    if not local_path.exists():
        logger.warning("Dropbox upload skipped — file missing: %s", local_path)
        return None

    with open(local_path, "rb") as f:
        data = f.read()
    return upload_bytes_and_share(data, dropbox_dest_path, dbx=dbx)


def upload_batch(
    files: list[tuple[Path, str]],
) -> list[tuple[Path, str | None]]:
    """Upload multiple files in one session. Returns list of (path, url_or_None)."""
    try:
        dbx = _get_client()
    except Exception:
        logger.exception("Dropbox client init failed — skipping batch upload")
        return [(p, None) for p, _ in files]

    results: list[tuple[Path, str | None]] = []
    try:
        for local_path, dest in files:
            url = upload_and_share(local_path, dest, dbx=dbx)
            results.append((local_path, url))
    finally:
        try:
            dbx.close()
        except Exception:
            pass
    return results
