import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import state
from bot.caption.generator import SummaryGenerationError, generate_summary
from bot.config import POST_ALLOWED_USER_IDS, SUMMARY_HISTORY_LIMIT
from bot.handlers.review import begin_instagram_phase
from bot.posting.website_client import WebsiteUploadError, upload_photo_to_website
from bot.utils.image_utils import cleanup

logger = logging.getLogger(__name__)

_EXPIRED_TEXT = "⌛ This review expired (30 min passed). Send the image again to post it."
_FAILURE_TEXT = "⚠️ Couldn't rewrite your summary (AI error). Tap Retry."


def _is_post_allowed(user_id: int) -> bool:
    # Empty allowlist = unrestricted, see POST_ALLOWED_USER_IDS in config.py.
    return not POST_ALLOWED_USER_IDS or user_id in POST_ALLOWED_USER_IDS


def _with_note(text: str, note: str | None) -> str:
    return f"{note}\n\n{text}" if note else text


def _for_review(draft: "state.ReviewDraft", note: str | None = None) -> str:
    return _with_note(draft.summary, note)


def _ready_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🌐 Go Live Website", callback_data=f"capbot:site_golive:{draft_id}"),
                InlineKeyboardButton("🔄 Regenerate", callback_data=f"capbot:site_regen:{draft_id}"),
            ],
            [InlineKeyboardButton("❌ Abort", callback_data=f"capbot:site_abort:{draft_id}")],
        ]
    )


def _failed_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Retry", callback_data=f"capbot:site_regen:{draft_id}"),
                InlineKeyboardButton("❌ Abort", callback_data=f"capbot:site_abort:{draft_id}"),
            ]
        ]
    )


def _busy_keyboard(draft_id: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"capbot:noop:{draft_id}")]])


async def _safe_edit_caption(context, draft: "state.ReviewDraft", text: str, reply_markup) -> None:
    try:
        await context.bot.edit_message_caption(
            chat_id=draft.chat_id, message_id=draft.review_message_id, caption=text, reply_markup=reply_markup
        )
    except TelegramError:
        logger.warning("Could not edit review message caption for draft %s", draft.id)


async def present_website_result(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
    """First successful summary rewrite for a fresh draft: replace the '🤖 Reading...' placeholder with the photo+summary+buttons review message."""
    try:
        await context.bot.delete_message(chat_id=draft.chat_id, message_id=placeholder_message_id)
    except TelegramError:
        pass

    with draft.image_path.open("rb") as fh:
        msg = await context.bot.send_photo(
            chat_id=draft.chat_id,
            photo=fh,
            caption=_for_review(draft),
            reply_markup=_ready_keyboard(draft.id),
        )
    draft.review_message_id = msg.message_id


async def present_website_failure(context, draft: "state.ReviewDraft", placeholder_message_id: int) -> None:
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


async def on_website_regen_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

        if draft.status not in ("website_ready", "website_failed"):
            await query.answer("Hang on, that's already in progress.", show_alert=False)
            return

        was_ready = draft.status == "website_ready"
        draft.status = "website_regenerating"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            "🔄 Rewriting summary..." if was_ready else "🔄 Retrying...",
            _busy_keyboard(draft.id, "🔄 Working..."),
        )

    context.application.create_task(_do_regenerate(context, draft, was_ready))


async def _do_regenerate(context, draft: "state.ReviewDraft", had_previous: bool) -> None:
    try:
        summary = await generate_summary(
            draft.image_path,
            draft.raw_summary,
            is_regeneration=True,
            history=draft.summary_history[-SUMMARY_HISTORY_LIMIT:],
        )
    except SummaryGenerationError:
        logger.exception("Summary regeneration failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "website_ready" if had_previous else "website_failed"
        if had_previous:
            await _safe_edit_caption(
                context,
                draft,
                _for_review(draft, "⚠️ Regeneration failed - showing the previous summary."),
                _ready_keyboard(draft.id),
            )
        else:
            await _safe_edit_caption(context, draft, _FAILURE_TEXT, _failed_keyboard(draft.id))
        return

    async with draft.lock:
        draft.summary = summary
        draft.summary_history.append(summary)
        draft.status = "website_ready"

    await _safe_edit_caption(context, draft, _for_review(draft), _ready_keyboard(draft.id))


async def on_website_golive_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    draft_id = _parse_draft_id(query.data)

    if not _is_post_allowed(user.id):
        await query.answer("You're not authorized to post to the website.", show_alert=True)
        return

    draft = state.get_draft(draft_id)
    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status == "website_posting":
            await query.answer("Already posting, hang tight...", show_alert=False)
            return
        if state.is_expired(draft):
            await query.answer()
            state.pop_draft(draft.id)
            cleanup(draft.image_path)
            await _safe_edit_caption(context, draft, _EXPIRED_TEXT, None)
            return
        if draft.status != "website_ready":
            await query.answer("Hang on, still preparing the summary.", show_alert=False)
            return

        draft.status = "website_posting"
        await query.answer()
        await _safe_edit_caption(
            context,
            draft,
            _for_review(draft, "⏳ Posting to the website..."),
            _busy_keyboard(draft.id, "⏳ Posting..."),
        )

    context.application.create_task(_do_post(context, draft))


async def _do_post(context, draft: "state.ReviewDraft") -> None:
    try:
        url = await upload_photo_to_website(draft.image_path, draft.summary)
    except WebsiteUploadError as exc:
        logger.exception("Website upload failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "website_ready"
        await _safe_edit_caption(
            context,
            draft,
            _for_review(draft, f"⚠️ Posting to website failed: {exc}\nYour summary is unchanged - tap Go Live Website to try again."),
            _ready_keyboard(draft.id),
        )
        return

    async with draft.lock:
        draft.website_url = url
        draft.status = "website_posted"

    await begin_instagram_phase(context, draft)


async def on_website_abort_tap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    draft_id = _parse_draft_id(query.data)
    draft = state.get_draft(draft_id)

    if draft is None:
        await query.answer(_EXPIRED_TEXT, show_alert=True)
        return

    async with draft.lock:
        if draft.status == "website_posting":
            await query.answer("Already posting - can't abort now.", show_alert=False)
            return
        if draft.status == "website_regenerating":
            await query.answer("Hang on, still working on that.", show_alert=False)
            return

        state.pop_draft(draft.id)
        cleanup(draft.image_path)
        await query.answer()
        await _safe_edit_caption(context, draft, "❌ Aborted - nothing was posted.", None)
