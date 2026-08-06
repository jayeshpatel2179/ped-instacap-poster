import asyncio
from pathlib import Path
from typing import Optional

from upload_post import UploadPostClient
from upload_post import UploadPostError as _SDKUploadPostError

from bot.config import UPLOAD_POST_API_KEY, UPLOAD_POST_PROFILE

# How long we wait, polling, for an async-handed-off job before giving up and
# telling the caller to fall back to a manual "Check Status" tap instead of
# blocking the Telegram flow indefinitely.
_POLL_INTERVAL_SECONDS = 5
_POLL_ATTEMPTS = 8  # ~40s on top of however long the initial request itself took

# Upload-Post's async status entries don't have a single documented field name
# for the published post URL (see docs.upload-post.com/api/upload-status) -
# try the plausible candidates in order rather than assuming one.
_URL_KEYS = ("url", "post_url", "link", "permalink", "post_link")


class UploadPostError(Exception):
    """Raised when publishing to Instagram via Upload-Post genuinely failed - nothing was posted, safe to retry."""


class UploadPostPending(UploadPostError):
    """Raised when Upload-Post handed the request to an async worker and it hadn't finished within our poll budget.

    NOT a failure - the post may still land on Instagram after we stop watching.
    Callers must not treat this as "safe to just tap Confirm & Post again",
    since that could double-post; use request_id with check_upload_status
    later instead.
    """

    def __init__(self, request_id: str):
        super().__init__(f"Still processing on Upload-Post's side (request_id={request_id})")
        self.request_id = request_id


def _upload_sync(image_path: Path, caption: str) -> dict:
    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    return client.upload_photos(
        [str(image_path)],
        title=caption,
        user=UPLOAD_POST_PROFILE,
        platforms=["instagram"],
        media_type="IMAGE",
    )


def _extract_url_from_entry(entry: dict) -> Optional[str]:
    for key in _URL_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_sync_response(response: dict) -> str:
    """Handle the immediate/synchronous response shape: results = {"instagram": {...}}."""
    instagram_result = response["results"]["instagram"]

    if instagram_result.get("success") is False:
        error = instagram_result.get("error") or "unknown error"
        raise UploadPostError(f"Instagram publish failed: {error}")

    url = instagram_result.get("url")
    if not url:
        raise UploadPostError(f"Upload-Post didn't return a post URL: {response}")

    return url


def _parse_status_response(status_resp: dict) -> tuple[str, Optional[str], Optional[str]]:
    """Parse a GET /uploadposts/status response into (state, url, error).

    state is one of "done_success", "done_failed", "pending".
    """
    top_status = status_resp.get("status")
    results = status_resp.get("results")

    entry = None
    if isinstance(results, list):
        entry = next((r for r in results if isinstance(r, dict) and r.get("platform") == "instagram"), None)
    elif isinstance(results, dict):
        entry = results.get("instagram")

    if top_status == "completed":
        if entry and entry.get("success"):
            return "done_success", _extract_url_from_entry(entry), None
        message = (entry or {}).get("message") or (entry or {}).get("error") or "Instagram reported failure with no message"
        return "done_failed", None, message

    if top_status in ("failed", "not_found"):
        message = status_resp.get("message") or (entry or {}).get("message") or "Upload failed"
        return "done_failed", None, message

    # Defensive: a per-platform entry can occasionally resolve before the
    # top-level status catches up on a multi-platform job.
    if entry and entry.get("success") is True:
        return "done_success", _extract_url_from_entry(entry), None
    if entry and entry.get("success") is False and entry.get("status") in ("failed", "error"):
        message = entry.get("message") or entry.get("error") or "Instagram reported failure"
        return "done_failed", None, message

    return "pending", None, None  # queued / processing / pending / in_progress


async def check_upload_status(request_id: str) -> tuple[str, Optional[str], Optional[str]]:
    """One-shot status check for the manual 'Check Status' button. Returns (state, url, error)."""
    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    try:
        status_resp = await asyncio.to_thread(client.get_status, request_id)
    except Exception as exc:
        raise UploadPostError(f"Couldn't check Upload-Post status: {exc}") from exc
    return _parse_status_response(status_resp)


async def upload_photo_to_instagram(image_path: Path, caption: str) -> str:
    """Publish an already-finished image to the PedTalkSports Instagram feed.

    Runs the (blocking, requests-based) SDK call in a thread so the bot's
    event loop keeps serving other chats while it's in flight. If Upload-Post
    hands the request off to its async worker (it does this itself when a
    synchronous attempt is taking too long), polls for a bounded window and
    raises UploadPostPending if it still hasn't resolved - the caller should
    then fall back to a manual status check rather than re-posting.
    """
    try:
        response = await asyncio.to_thread(_upload_sync, image_path, caption)
    except _SDKUploadPostError as exc:
        raise UploadPostError(str(exc)) from exc
    except Exception as exc:
        raise UploadPostError(f"Upload-Post request failed: {exc}") from exc

    if isinstance(response.get("results"), dict):
        return _parse_sync_response(response)

    request_id = response.get("request_id") if isinstance(response, dict) else None
    if not request_id:
        raise UploadPostError(f"Unexpected response from Upload-Post: {response}")

    client = UploadPostClient(api_key=UPLOAD_POST_API_KEY)
    for _ in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            status_resp = await asyncio.to_thread(client.get_status, request_id)
        except Exception:
            continue  # transient - keep trying within the budget
        state, url, error = _parse_status_response(status_resp)
        if state == "done_success":
            return url or f"(posted - Upload-Post didn't return a URL; request_id={request_id})"
        if state == "done_failed":
            raise UploadPostError(f"Instagram publish failed: {error}")

    raise UploadPostPending(request_id)
