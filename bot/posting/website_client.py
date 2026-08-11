import mimetypes
from pathlib import Path

import httpx

from bot.config import WEBSITE_UPLOAD_API_KEY, WEBSITE_UPLOAD_URL

_TIMEOUT_SECONDS = 60.0


class WebsiteUploadError(Exception):
    """Raised when publishing to the PedTalks website genuinely failed - nothing was posted, safe to retry."""


async def upload_photo_to_website(image_path: Path, summary: str) -> str:
    """Publish an image (+ optional 50-60 word summary) to the PedTalks website via its upload API.

    POST https://pedtalks.com/api/instagram/upload, multipart/form-data with an `image` file field
    and an optional `summary` text field, authenticated via the `x-api-key` header. Returns the
    published URL on success.
    """
    mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            with image_path.open("rb") as fh:
                response = await client.post(
                    WEBSITE_UPLOAD_URL,
                    headers={"x-api-key": WEBSITE_UPLOAD_API_KEY},
                    data={"summary": summary} if summary else {},
                    files={"image": (image_path.name, fh, mime)},
                )
    except httpx.HTTPError as exc:
        raise WebsiteUploadError(f"Website upload request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.is_success and isinstance(payload, dict) and payload.get("success"):
        url = payload.get("url")
        if not url:
            raise WebsiteUploadError(f"Website didn't return a post URL: {payload}")
        return url

    if isinstance(payload, dict) and payload.get("error"):
        raise WebsiteUploadError(str(payload["error"]))

    raise WebsiteUploadError(f"Website upload failed (HTTP {response.status_code}): {response.text[:300]}")
