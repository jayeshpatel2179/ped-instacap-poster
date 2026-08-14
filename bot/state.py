import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bot.config import DRAFT_EXPIRY_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class ReviewDraft:
    """One finished graphic moving through the merged review-then-publish pipeline:
    summary rewrite -> Save -> caption+hashtags generation -> single Go Live that publishes to
    the website and Instagram concurrently.

    Keyed by a random id embedded in every button's callback_data, not by
    (chat_id, user_id) - that's what lets N people in the same group post
    images at the same second without their drafts crossing wires. Lost on
    redeploy by design; the image just needs to be re-sent if that happens
    mid-review. The same image is reused for both publish targets throughout -
    the user never has to resend it.
    """

    id: str
    chat_id: int
    user_id: int
    image_path: Path
    raw_summary: str  # the user's own 50-60 word draft, required, primary source of truth throughout
    review_message_id: Optional[int] = None
    stage: str = "summary"  # summary | caption

    # Summary stage (website copy) - see bot/handlers/publish.py
    summary: Optional[str] = None  # AI-rewritten 50-60 word summary, also reused as IG caption context
    summary_history: list[str] = field(default_factory=list)  # prior rewrites, steers regens away from repeats

    # Caption stage (Instagram copy)
    caption: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)  # prior captions, used to steer regenerations away from repeats
    used_angles: list[str] = field(default_factory=list)

    # Publish outcome tracking - independent per-platform flags (not a single phase/status) so a
    # partial Go Live failure can be retried without double-posting whichever platform already
    # succeeded. instagram_pending covers Upload-Post's async hand-off case, distinct from a hard
    # failure: it must not be retried automatically, only resolved via Check Status.
    website_posted: bool = False
    website_url: Optional[str] = None
    instagram_posted: bool = False
    instagram_url: Optional[str] = None
    instagram_pending: bool = False
    upload_request_id: Optional[str] = None  # set when Upload-Post hands a post off to its async worker

    # status is stage-relative:
    #   stage=summary: summarizing | summary_ready | summary_regenerating | summary_failed
    #   stage=caption: generating_caption | caption_ready | caption_regenerating | caption_failed |
    #                  posting | posted
    # caption_ready doubles as the idle/safe-to-act state both for a fresh caption and for any
    # post-Go-Live state that still needs follow-up (partial failure or instagram_pending) - what
    # to show is derived from the boolean flags above at render time, not encoded into status.
    status: str = "summarizing"
    created_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_drafts: dict[str, ReviewDraft] = {}


def create_draft(chat_id: int, user_id: int, image_path: Path, raw_summary: str) -> ReviewDraft:
    draft_id = uuid.uuid4().hex[:10]
    while draft_id in _drafts:
        draft_id = uuid.uuid4().hex[:10]
    draft = ReviewDraft(id=draft_id, chat_id=chat_id, user_id=user_id, image_path=image_path, raw_summary=raw_summary)
    _drafts[draft_id] = draft
    return draft


def get_draft(draft_id: str) -> Optional[ReviewDraft]:
    return _drafts.get(draft_id)


def pop_draft(draft_id: str) -> Optional[ReviewDraft]:
    return _drafts.pop(draft_id, None)


def is_expired(draft: ReviewDraft) -> bool:
    return (time.monotonic() - draft.created_at) > DRAFT_EXPIRY_SECONDS


def schedule_expiry_cleanup(application, draft_id: str) -> None:
    application.create_task(_expire_after_timeout(draft_id))


async def _expire_after_timeout(draft_id: str) -> None:
    await asyncio.sleep(DRAFT_EXPIRY_SECONDS)
    draft = _drafts.get(draft_id)
    if draft is None or draft.status == "posted":
        return
    logger.info("Draft %s expired without resolving; cleaning up temp file", draft_id)
    pop_draft(draft_id)
    draft.image_path.unlink(missing_ok=True)
