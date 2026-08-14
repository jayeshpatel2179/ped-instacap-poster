import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import state
from bot.caption.generator import CaptionGenerationError, SummaryGenerationError, generate_caption, generate_summary
from bot.config import CAPTION_HISTORY_LIMIT, POST_ALLOWED_USER_IDS, SUMMARY_HISTORY_LIMIT
from bot.posting.uploadpost_client import (
    UploadPostError,
    UploadPostPending,
    check_upload_status,
    upload_photo_to_instagram,
)
from bot.posting.website_client import upload_photo_to_website
from bot.utils.image_utils import cleanup

logger = logging.getLogger(__name__)

_EXPIRED_TEXT = "⌛ This review expired (30 min passed). Send the image again to post it."
_SUMMARY_FAILURE_TEXT = "⚠️ Couldn't rewrite your summary (AI error). Tap Retry."
_CAPTION_FAILURE_TEXT = "⚠️ Couldn't write an Instagram caption for this image (AI error). Tap Retry."


def _is_post_allowed(user_id: int) -> bool:
    # Empty allowlist = unrestricted, see POST_ALLOWED_USER_IDS in config.py.
    return not POST_ALLOWED_USER_IDS or user_id in POST_ALLOWED_USER_IDS


def build_preview_text(caption: str, hashtags: list[str]) -> str:
    hashtag_line = " ".join(f"#{tag}" for tag in hashtags) if hashtags else "(no hashtags generated)"
    return f"{caption}\n\n{hashtag_line}"


def _with_note(text: str, note: str | None) -> str:
    return f"{note}\n\n{text}" if note else text


def _summary_text(draft: "state.ReviewDraft", note: str | None = None) -> str:
    return _with_note(f"📝 Website summary:\n\n{draft.summary}", note)


def _caption_text(draft: "state.ReviewDraft", note: str | None = None) -> str:
    base = (
        f"📝 Website summary (saved):\n{draft.summary}\n\n"
        f"📸 Instagram caption + hashtags:\n{build_preview_text(draft.caption, draft.hashtags)}"
    )
    return _with_note(base, note)


def _summary_ready_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Save", callback_data=f"capbot:save:{draft_id}"),
                InlineKeyboardButton("🔄 Regenerate", callback_data=f"capbot:regen_summary:{draft_id}"),
            ]
        ]
    )


def _summary_failed_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry", callback_data=f"capbot:regen_summary:{draft_id}")]])


def _caption_ready_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 Go Live", callback_data=f"capbot:golive:{draft_id}"),
                InlineKeyboardButton("🔄 Regenerate", callback_data=f"capbot:regen_caption:{draft_id}"),
            ],
            [InlineKeyboardButton("❌ Abort", callback_data=f"capbot:abort:{draft_id}")],
        ]
    )


def _caption_failed_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"capbot:regen_caption:{draft_id}"),
                InlineKeyboardButton("❌ Abort", callback_data=f"capbot:abort:{draft_id}"),
            ]
        ]
    )


def _busy_keyboard(draft_id: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"capbot:noop:{draft_id}")]])


def _keyboard_for_caption_stage(draft: "state.ReviewDraft") -> InlineKeyboardMarkup:
    """What to show while idle in the caption stage - covers a fresh caption, a partial Go Live
    failure, and an outstanding Instagram pending check, purely from the boolean publish flags."""
    if draft.instagram_pending:
        rows = []
        if not draft.website_posted:
            rows.append([InlineKeyboardButton("🚀 Go Live", callback_data=f"capbot:golive:{draft.id}")])
        rows.append([InlineKeyboardButton("🔄 Check Status", callback_data=f"capbot:checkstatus:{draft.id}")])
        return InlineKeyboardMarkup(rows)
    return _caption_ready_keyboard(draft.id)


async def _safe_edit_caption(context, draft: "state.ReviewDraft", text: str, reply_markup) -> None:
    try:
        await context.bot.edit_message_caption(
            chat_id=draft.chat_id, message_id=draft.review_message_id, caption=text, reply_markup=reply_markup
        )
    except TelegramError:
        logger.warning("Could not edit review message caption for draft %s", draft.id)


def _parse_draft_id(callback_data: str) -> str:
    return callback_data.split(":", 2)[2]


# ---------------------------------------------------------------------------
# Summary stage
# ---------------------------------------------------------------------------


async def present_summary_result(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
    """First successful summary rewrite for a fresh draft: replace the '🤖 Reading...' placeholder with the photo+summary+buttons review message."""
    try:
        await context.bot.delete_message(chat_id=draft.chat_id, message_id=placeholder_message_id)
    except TelegramError:
        pass

    with draft.image_path.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=draft.chat_id,
            photo=fh,
            caption=_summary_text(draft),
            reply_markup=_summary_ready_keyboard(draft.id),
        )
    draft.review_message_id = msg.message_id


async def present_summary_failure(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=draft.chat_id, message_id=placeholder_message_id)
    except TelegramError:
        pass

    with draft.image_path.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=draft.chat_id,
            photo=fh,
            caption=_SUMMARY_FAILURE_TEXT,
            reply_markup=_summary_failed_keyboard(draft.id),
        )
    draft.review_message_id = msg.message_id


async def on_regen_summary_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        if draft.stage != "summary" or draft.status not in ("summary_ready", "summary_failed"):
            await query.answer("Hang on, that's already in progress.", show_alert=False)
            return

        was_ready = draft.status == "summary_ready"
        draft.status = "summary_regenerating"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            "🔄 Rewriting summary..." if was_ready else "🔄 Retrying...",
            _busy_keyboard(draft.id, "🔄 Working..."),
        )

    context.application.create_task(_produce_summary(context, draft, was_ready))


async def _produce_summary(context, draft: "state.ReviewDraft", had_previous: bool) -> None:
    try:
        summary = await generate_summary(
            draft.image_path,
            draft.raw_summary,
            is_regeneration=had_previous,
            history=draft.summary_history[-SUMMARY_HISTORY_LIMIT:],
        )
    except SummaryGenerationError:
        logger.exception("Summary generation failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "summary_ready" if had_previous else "summary_failed"
        if had_previous:
            await _safe_edit_caption(
                context,
                draft,
                _summary_text(draft, "⚠️ Regeneration failed - showing the previous summary."),
                _summary_ready_keyboard(draft.id),
            )
        else:
            await _safe_edit_caption(context, draft, _SUMMARY_FAILURE_TEXT, _summary_failed_keyboard(draft.id))
        return

    async with draft.lock:
        draft.summary = summary
        draft.summary_history.append(summary)
        draft.status = "summary_ready"

    await _safe_edit_caption(context, draft, _summary_text(draft), _summary_ready_keyboard(draft.id))


async def on_save_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        if draft.stage != "summary" or draft.status != "summary_ready":
            await query.answer("Hang on, still preparing the summary.", show_alert=False)
            return

        draft.stage = "caption"
        draft.status = "generating_caption"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            f"✅ Summary saved:\n{draft.summary}\n\n🤖 Now writing the Instagram caption...",
            _busy_keyboard(draft.id, "🤖 Writing caption..."),
        )

    context.application.create_task(_produce_caption(context, draft, had_previous=False))


# ---------------------------------------------------------------------------
# Caption stage
# ---------------------------------------------------------------------------


async def on_regen_caption_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        if draft.stage != "caption" or draft.status not in ("caption_ready", "caption_failed"):
            await query.answer("Hang on, that's already in progress.", show_alert=False)
            return

        was_ready = draft.status == "caption_ready"
        draft.status = "caption_regenerating"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            "🔄 Regenerating caption..." if was_ready else "🔄 Retrying...",
            _busy_keyboard(draft.id, "🔄 Working..."),
        )

    context.application.create_task(_produce_caption(context, draft, had_previous=was_ready))


async def _produce_caption(context, draft: "state.ReviewDraft", *, had_previous: bool) -> None:
    try:
        result, angle = await generate_caption(
            draft.image_path,
            is_regeneration=had_previous,
            history=draft.history[-CAPTION_HISTORY_LIMIT:],
            used_angles=draft.used_angles,
            user_context=draft.summary,
        )
    except CaptionGenerationError:
        logger.exception("Caption generation failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "caption_ready" if had_previous else "caption_failed"
        if had_previous:
            await _safe_edit_caption(
                context,
                draft,
                _caption_text(draft, "⚠️ Regeneration failed - showing the previous caption."),
                _keyboard_for_caption_stage(draft),
            )
        else:
            await _safe_edit_caption(context, draft, _CAPTION_FAILURE_TEXT, _caption_failed_keyboard(draft.id))
        return

    async with draft.lock:
        draft.caption = result.caption
        draft.hashtags = result.hashtags
        draft.history.append(result.caption)
        if angle:
            draft.used_angles.append(angle)
        draft.status = "caption_ready"

    await _safe_edit_caption(context, draft, _caption_text(draft), _keyboard_for_caption_stage(draft))


async def on_golive_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    draft_id = _parse_draft_id(query.data)

    if not _is_post_allowed(user.id):
        await query.answer("You're not authorized to publish.", show_alert=True)
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
        if state.is_expired(draft):
            await query.answer()
            state.pop_draft(draft.id)
            cleanup(draft.image_path)
            await _safe_edit_caption(context, draft, _EXPIRED_TEXT, None)
            return
        if draft.stage != "caption" or draft.status != "caption_ready":
            await query.answer("Hang on, still preparing the caption.", show_alert=False)
            return
        if draft.instagram_pending and draft.website_posted:
            await query.answer("Instagram is still processing - tap Check Status instead.", show_alert=False)
            return

        draft.status = "posting"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            _caption_text(draft, "⏳ Posting to the website + Instagram..."),
            _busy_keyboard(draft.id, "⏳ Posting..."),
        )

    context.application.create_task(_do_publish(context, draft))


async def _do_publish(context, draft: "state.ReviewDraft") -> None:
    """Publishes to whichever platform(s) haven't succeeded (or aren't already pending) yet,
    concurrently. Safe to call repeatedly after a partial failure - never double-posts a platform
    that already succeeded, and never re-fires Instagram while a prior attempt is still pending."""
    jobs: dict[str, asyncio.Task] = {}
    if not draft.website_posted:
        jobs["website"] = asyncio.create_task(upload_photo_to_website(draft.image_path, draft.summary))
    if not draft.instagram_posted and not draft.instagram_pending:
        final_caption = build_preview_text(draft.caption, draft.hashtags)
        jobs["instagram"] = asyncio.create_task(upload_photo_to_instagram(draft.image_path, final_caption))

    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    outcomes = dict(zip(jobs.keys(), results))

    errors: list[str] = []
    async with draft.lock:
        if "website" in outcomes:
            result = outcomes["website"]
            if isinstance(result, BaseException):
                logger.error("Website publish failed for draft %s: %s", draft.id, result)
                errors.append(f"Website: {result}")
            else:
                draft.website_posted = True
                draft.website_url = result

        if "instagram" in outcomes:
            result = outcomes["instagram"]
            if isinstance(result, UploadPostPending):
                logger.info("Instagram upload for draft %s still processing (request_id=%s)", draft.id, result.request_id)
                draft.instagram_pending = True
                draft.upload_request_id = result.request_id
            elif isinstance(result, BaseException):
                logger.error("Instagram publish failed for draft %s: %s", draft.id, result)
                errors.append(f"Instagram: {result}")
            else:
                draft.instagram_posted = True
                draft.instagram_url = result

        fully_done = draft.website_posted and draft.instagram_posted
        draft.status = "posted" if fully_done else "caption_ready"

    if fully_done:
        lines = ["✅ Posted to the website and Instagram!"]
        if draft.website_url:
            lines.append(f"Website: {draft.website_url}")
        if draft.instagram_url:
            lines.append(f"Instagram: {draft.instagram_url}")
        await _safe_edit_caption(context, draft, "\n".join(lines), None)
        cleanup(draft.image_path)
        state.pop_draft(draft.id)
        return

    note_lines = []
    if errors:
        note_lines.append("⚠️ Posting failed for: " + "; ".join(errors))
    if draft.instagram_pending:
        note_lines.append("⏳ Instagram is still processing on Upload-Post's async worker - tap Check Status.")
    if draft.website_posted:
        note_lines.append("✅ Website already posted - Go Live again will only retry Instagram.")
    if draft.instagram_posted:
        note_lines.append("✅ Instagram already posted - Go Live again will only retry the website.")

    await _safe_edit_caption(context, draft, _caption_text(draft, "\n".join(note_lines)), _keyboard_for_caption_stage(draft))


async def on_checkstatus_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if not draft.instagram_pending:
            await query.answer("Nothing pending to check.", show_alert=False)
            return
        request_id = draft.upload_request_id
        await query.answer("Checking...")

    try:
        result_state, url, error = await check_upload_status(request_id)
    except UploadPostError as exc:
        await _safe_edit_caption(
            context, draft, _caption_text(draft, f"⚠️ Couldn't check status right now: {exc}"), _keyboard_for_caption_stage(draft)
        )
        return

    if result_state == "done_success":
        async with draft.lock:
            draft.instagram_pending = False
            draft.instagram_posted = True
            draft.instagram_url = url
            fully_done = draft.website_posted
            draft.status = "posted" if fully_done else "caption_ready"

        if fully_done:
            lines = ["✅ Posted to the website and Instagram!", f"Website: {draft.website_url}"]
            lines.append(f"Instagram: {url}" if url else "Instagram: (no URL returned)")
            await _safe_edit_caption(context, draft, "\n".join(lines), None)
            cleanup(draft.image_path)
            state.pop_draft(draft.id)
        else:
            await _safe_edit_caption(
                context,
                draft,
                _caption_text(draft, "✅ Instagram posted - Go Live again to retry the website."),
                _keyboard_for_caption_stage(draft),
            )
        return

    if result_state == "done_failed":
        async with draft.lock:
            draft.instagram_pending = False
            draft.status = "caption_ready"
        await _safe_edit_caption(
            context,
            draft,
            _caption_text(draft, f"⚠️ Instagram posting failed: {error}\nTap Go Live to retry."),
            _keyboard_for_caption_stage(draft),
        )
        return

    # still pending
    await _safe_edit_caption(
        context,
        draft,
        _caption_text(draft, "⏳ Still processing on Instagram's end. Tap Check Status again in a bit."),
        _keyboard_for_caption_stage(draft),
    )


async def on_abort_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status == "posting":
            await query.answer("Already posting - can't abort now.", show_alert=False)
            return
        if draft.status == "posted":
            await query.answer("Already posted.", show_alert=False)
            return
        if draft.website_posted or draft.instagram_posted or draft.instagram_pending:
            posted_where = " and ".join(
                p
                for p, ok in (("the website", draft.website_posted), ("Instagram", draft.instagram_posted or draft.instagram_pending))
                if ok
            )
            await query.answer(f"Already posted to {posted_where} - can't abort, only retry what's left.", show_alert=True)
            return
        if draft.status not in ("caption_ready", "caption_failed"):
            await query.answer("Hang on, still working on that.", show_alert=False)
            return

        state.pop_draft(draft.id)
        cleanup(draft.image_path)
        await query.answer()
        await _safe_edit_caption(context, draft, "❌ Aborted - nothing was posted.", None)


async def on_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel - text-command equivalent of the ❌ Abort button, for whoever sent the photo.

    No draft_id to work from (unlike every button, which carries it in callback_data), so this
    looks up the sender's own most recent draft in this chat via state.get_latest_draft_for.
    Works at either stage (summary or caption review) - the Abort button only appears once the
    caption stage is reached, but there's no reason a plain command should share that limitation.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    draft = state.get_latest_draft_for(chat.id, user.id)
    if draft is None:
        await message.reply_text("Nothing to cancel right now.")
        return

    async with draft.lock:
        if state.is_expired(draft):
            state.pop_draft(draft.id)
            cleanup(draft.image_path)
            await message.reply_text("That draft already expired - nothing to cancel.")
            return
        if draft.status == "posting":
            await message.reply_text("Already posting - can't cancel now.")
            return
        if draft.status == "posted":
            await message.reply_text("Already posted - nothing left to cancel.")
            return
        if draft.website_posted or draft.instagram_posted or draft.instagram_pending:
            posted_where = " and ".join(
                p
                for p, ok in (("the website", draft.website_posted), ("Instagram", draft.instagram_posted or draft.instagram_pending))
                if ok
            )
            await message.reply_text(f"Already posted to {posted_where} - can't cancel, only retry what's left via the buttons.")
            return
        if draft.status not in ("summary_ready", "summary_failed", "caption_ready", "caption_failed"):
            await message.reply_text("Hang on, still working on that - try again in a moment.")
            return

        state.pop_draft(draft.id)
        cleanup(draft.image_path)

    await _safe_edit_caption(context, draft, "❌ Aborted via /cancel - nothing was posted.", None)
    await message.reply_text("Cancelled.")


async def on_noop_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
