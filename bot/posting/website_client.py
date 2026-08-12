import logging
import mimetypes
from pathlib import Path

import httpx

from bot.config import WEBSITE_UPLOAD_API_KEY, WEBSITE_UPLOAD_URL

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 60.0


class WebsiteUploadError(Exception):
    """Raised when publishing to the PedTalks website genuinely failed - nothing was posted, safe to retry."""


def _describe_exception_chain(exc: BaseException) -> str:
    """httpx/httpcore transport errors are often raised with an empty message, with the actually
    useful detail (e.g. the underlying ssl.SSLError or OSError) several `__cause__` levels down.
    Walk the whole chain so that detail always ends up in the Telegram-facing error text."""
    parts = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current)
        parts.append(f"{type(current).__name__}({text})" if text else type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


async def upload_photo_to_website(image_path: Path, summary: str) -> str:
    """Publish an image (+ optional 50-60 word summary) to the PedTalks website via its upload API.

    POST https://www.pedtalkssports.com/api/instagram/upload, multipart/form-data with an `image` file field
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
        # httpx/httpcore transport errors (timeouts, connect/SSL failures) often carry no message on
        # the top-level exception - the real detail is several __cause__ levels down. Walk the whole
        # chain so this is diagnosable from the Telegram error text alone, no log cross-referencing needed.
        logger.exception("Website upload request to %s failed", WEBSITE_UPLOAD_URL)
        raise WebsiteUploadError(f"calling {WEBSITE_UPLOAD_URL}: {_describe_exception_chain(exc)}") from exc

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
