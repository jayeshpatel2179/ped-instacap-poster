import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import state
from bot.caption.generator import CaptionGenerationError, generate_caption
from bot.config import CAPTION_HISTORY_LIMIT, POST_ALLOWED_USER_IDS
from bot.posting.uploadpost_client import (
    UploadPostError,
    UploadPostPending,
    check_upload_status,
    upload_photo_to_instagram,
)
from bot.utils.image_utils import cleanup

logger = logging.getLogger(__name__)

_EXPIRED_TEXT = "⌛ This review expired (30 min passed). Send the image again to post it."
_FAILURE_TEXT = "⚠️ Couldn't write a caption for this image (AI error). Tap Retry."


def _is_post_allowed(user_id: int) -> bool:
    # Empty allowlist = unrestricted, see POST_ALLOWED_USER_IDS in config.py.
    return not POST_ALLOWED_USER_IDS or user_id in POST_ALLOWED_USER_IDS


def build_preview_text(caption: str, hashtags: list[str]) -> str:
    hashtag_line = " ".join(f"#{tag}" for tag in hashtags) if hashtags else "(no hashtags generated)"
    return f"{caption}\n\n{hashtag_line}"


def _with_note(text: str, note: str | None) -> str:
    return f"{note}\n\n{text}" if note else text


def _ready_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm & Post", callback_data=f"capbot:confirm:{draft_id}"),
                InlineKeyboardButton("🔄 Regenerate", callback_data=f"capbot:regen:{draft_id}"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"capbot:cancel:{draft_id}")],
        ]
    )


def _failed_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"capbot:regen:{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"capbot:cancel:{draft_id}"),
            ]
        ]
    )


def _busy_keyboard(draft_id: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"capbot:noop:{draft_id}")]])


def _pending_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Check Status", callback_data=f"capbot:checkstatus:{draft_id}")]])


async def _safe_edit_caption(context, draft: "state.ReviewDraft", text: str, reply_markup) -> None:
    try:
        await context.bot.edit_message_caption(
            chat_id=draft.chat_id, message_id=draft.review_message_id, caption=text, reply_markup=reply_markup
        )
    except TelegramError:
        logger.warning("Could not edit review message caption for draft %s", draft.id)


async def present_result(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
    """First successful caption for a fresh draft: replace the '🤖 Looking...' placeholder with the photo+caption+buttons review message."""
    try:
        await context.bot.delete_message(chat_id=draft.chat_id, message_id=placeholder_message_id)
    except TelegramError:
        pass

    with draft.image_path.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=draft.chat_id,
            photo=fh,
            caption=build_preview_text(draft.caption, draft.hashtags),
            reply_markup=_ready_keyboard(draft.id),
            reply_to_message_id=None,
        )
    draft.review_message_id = msg.message_id


async def present_failure(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=draft.chat_id, message_id=placeholder_message_id)
    except TelegramError:
        pass

    with draft.image_path.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=draft.chat_id,
            photo=fh,
            caption=_FAILURE_TEXT,
            reply_markup=_failed_keyboard(draft.id),
        )
    draft.review_message_id = msg.message_id


def _parse_draft_id(callback_data: str) -> str:
    return callback_data.split(":", 2)[2]


async def on_regen_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if state.is_expired(draft):
            await query.answer()
            state.pop_draft(draft.id)
            cleanup(draft.image_path)
            await _safe_edit_caption(context, draft, _EXPIRED_TEXT, None)
            return

        if draft.status not in ("ready", "failed"):
            await query.answer("Hang on, that's already in progress.", show_alert=False)
            return

        was_ready = draft.status == "ready"
        draft.status = "regenerating"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            "🔄 Regenerating caption..." if was_ready else "🔄 Retrying...",
            _busy_keyboard(draft.id, "🔄 Working..."),
        )

    context.application.create_task(_do_regenerate(context, draft, was_ready))


async def _do_regenerate(context, draft: "state.ReviewDraft", had_previous: bool) -> None:
    try:
        result, angle = await generate_caption(
            draft.image_path,
            is_regeneration=True,
            history=draft.history[-CAPTION_HISTORY_LIMIT:],
            used_angles=draft.used_angles,
        )
    except CaptionGenerationError:
        logger.exception("Caption regeneration failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "ready" if had_previous else "failed"
        if had_previous:
            await _safe_edit_caption(
                context,
                draft,
                _with_note(build_preview_text(draft.caption, draft.hashtags), "⚠️ Regeneration failed - showing the previous caption."),
                _ready_keyboard(draft.id),
            )
        else:
            await _safe_edit_caption(context, draft, _FAILURE_TEXT, _failed_keyboard(draft.id))
        return

    async with draft.lock:
        draft.caption = result.caption
        draft.hashtags = result.hashtags
        draft.history.append(result.caption)
        if angle:
            draft.used_angles.append(angle)
        draft.status = "ready"

    await _safe_edit_caption(context, draft, build_preview_text(draft.caption, draft.hashtags), _ready_keyboard(draft.id))


async def on_confirm_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    draft_id = _parse_draft_id(query.data)

    if not _is_post_allowed(user.id):
        await query.answer("You're not authorized to post to Instagram.", show_alert=True)
        return

    draft = state.get_draft(draft_id)
    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status == "posting":
            await query.answer("Already posting, hang tight...", show_alert=False)
            return
        if draft.status == "posted":
            await query.answer("Already posted.", show_alert=False)
            return
        if draft.status == "posting_pending":
            await query.answer("Still processing the last attempt - tap Check Status.", show_alert=False)
            return
        if state.is_expired(draft):
            await query.answer()
            state.pop_draft(draft.id)
            cleanup(draft.image_path)
            await _safe_edit_caption(context, draft, _EXPIRED_TEXT, None)
            return
        if draft.status != "ready":
            await query.answer("Hang on, still preparing the caption.", show_alert=False)
            return

        draft.status = "posting"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            _with_note(build_preview_text(draft.caption, draft.hashtags), "⏳ Posting to Instagram..."),
            _busy_keyboard(draft.id, "⏳ Posting..."),
        )

    context.application.create_task(_do_post(context, draft))


async def _do_post(context, draft: "state.ReviewDraft") -> None:
    final_caption = build_preview_text(draft.caption, draft.hashtags)
    try:
        url = await upload_photo_to_instagram(draft.image_path, final_caption)
    except UploadPostPending as exc:
        logger.info("Instagram upload for draft %s still processing (request_id=%s)", draft.id, exc.request_id)
        async with draft.lock:
            draft.status = "posting_pending"
            draft.upload_request_id = exc.request_id
        await _safe_edit_caption(
            context,
            draft,
            _with_note(
                final_caption,
                "⏳ Instagram is taking longer than usual to process this post. It may still go through on "
                "their end - tap Check Status to look. I won't post it again automatically, to avoid a duplicate.",
            ),
            _pending_keyboard(draft.id),
        )
        return
    except UploadPostError as exc:
        logger.exception("Instagram upload failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "ready"
        await _safe_edit_caption(
            context,
            draft,
            _with_note(final_caption, f"⚠️ Posting failed: {exc}\nYour caption and hashtags are unchanged - tap Confirm & Post to try again."),
            _ready_keyboard(draft.id),
        )
        return

    async with draft.lock:
        draft.status = "posted"

    await _safe_edit_caption(context, draft, f"✅ Posted to Instagram!\n{url}\n\n{final_caption}", None)
    cleanup(draft.image_path)
    state.pop_draft(draft.id)


async def on_checkstatus_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status != "posting_pending":
            await query.answer("Nothing pending to check.", show_alert=False)
            return
        request_id = draft.upload_request_id
        await query.answer("Checking...")

    final_caption = build_preview_text(draft.caption, draft.hashtags)
    try:
        result_state, url, error = await check_upload_status(request_id)
    except UploadPostError as exc:
        await _safe_edit_caption(
            context,
            draft,
            _with_note(final_caption, f"⚠️ Couldn't check status right now: {exc}"),
            _pending_keyboard(draft.id),
        )
        return

    if result_state == "done_success":
        async with draft.lock:
            draft.status = "posted"
        display_url = url or f"(no URL returned; request_id={request_id})"
        await _safe_edit_caption(context, draft, f"✅ Posted to Instagram!\n{display_url}\n\n{final_caption}", None)
        cleanup(draft.image_path)
        state.pop_draft(draft.id)
        return

    if result_state == "done_failed":
        async with draft.lock:
            draft.status = "ready"
        await _safe_edit_caption(
            context,
            draft,
            _with_note(final_caption, f"⚠️ Posting failed: {error}\nYour caption and hashtags are unchanged - tap Confirm & Post to try again."),
            _ready_keyboard(draft.id),
        )
        return

    # still pending
    await _safe_edit_caption(
        context,
        draft,
        _with_note(final_caption, "⏳ Still processing on Instagram's end. Tap Check Status again in a bit."),
        _pending_keyboard(draft.id),
    )


async def on_cancel_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status == "posting":
            await query.answer("Already posting - can't cancel now.", show_alert=False)
            return
        if draft.status == "posted":
            await query.answer("Already posted.", show_alert=False)
            return
        if draft.status == "posting_pending":
            await query.answer("Already sent to Instagram's queue - can't cancel now. Tap Check Status.", show_alert=False)
            return
        if draft.status == "regenerating":
            await query.answer("Hang on, still working on that.", show_alert=False)
            return

        state.pop_draft(draft.id)
        cleanup(draft.image_path)
        await query.answer()
        await _safe_edit_caption(context, draft, "❌ Cancelled - nothing was posted.", None)


async def on_noop_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
