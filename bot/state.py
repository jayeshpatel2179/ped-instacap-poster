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
    """One finished graphic awaiting a caption decision.

    Keyed by a random id embedded in every button's callback_data, not by
    (chat_id, user_id) - that's what lets N people in the same group post
    images at the same second without their drafts crossing wires. Lost on
    redeploy by design; the image just needs to be re-sent if that happens
    mid-review.
    """

    id: str
    chat_id: int
    user_id: int
    image_path: Path
    user_context: Optional[str] = None  # optional free-text context the user attached to the image
    review_message_id: Optional[int] = None
    caption: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)  # prior captions, used to steer regenerations away from repeats
    used_angles: list[str] = field(default_factory=list)
    upload_request_id: Optional[str] = None  # set when Upload-Post hands a post off to its async worker
    status: str = "generating"  # generating | ready | regenerating | posting | posting_pending | posted
    created_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_drafts: dict[str, ReviewDraft] = {}


def create_draft(chat_id: int, user_id: int, image_path: Path, user_context: Optional[str] = None) -> ReviewDraft:
    draft_id = uuid.uuid4().hex[:10]
    while draft_id in _drafts:
        draft_id = uuid.uuid4().hex[:10]
    draft = ReviewDraft(id=draft_id, chat_id=chat_id, user_id=user_id, image_path=image_path, user_context=user_context)
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
