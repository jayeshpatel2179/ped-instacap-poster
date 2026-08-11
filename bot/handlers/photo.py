import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot import state
from bot.caption.generator import SummaryGenerationError, generate_summary
from bot.config import WEBSITE_IMAGE_MAX_BYTES, WEBSITE_IMAGE_SUFFIXES
from bot.handlers.website import present_website_failure, present_website_result
from bot.utils.image_utils import save_telegram_file

logger = logging.getLogger(__name__)

_NO_SUMMARY_TEXT = (
    "📝 Send the photo again with a 50-60 word summary attached as the caption on the photo message "
    "itself - I need that to post to the website and Instagram."
)
_BAD_FORMAT_TEXT = "⚠️ The website only accepts jpg, jpeg, png, or webp images. Please resend in one of those formats."
_TOO_LARGE_TEXT = "⚠️ That image is over the website's 10MB limit. Please resend a smaller file."


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message.photo:
        tg_file = await message.photo[-1].get_file()
        suffix = ".jpg"
        file_size = message.photo[-1].file_size
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        tg_file = await message.document.get_file()
        suffix = Path(message.document.file_name or "image.jpg").suffix.lower() or ".jpg"
        file_size = message.document.file_size
    else:
        return

    raw_summary = (message.caption or "").strip()
    if not raw_summary:
        await message.reply_text(_NO_SUMMARY_TEXT)
        return

    if suffix not in WEBSITE_IMAGE_SUFFIXES:
        await message.reply_text(_BAD_FORMAT_TEXT)
        return

    if file_size and file_size > WEBSITE_IMAGE_MAX_BYTES:
        await message.reply_text(_TOO_LARGE_TEXT)
        return

    image_path = await save_telegram_file(tg_file, chat.id, suffix)

    draft = state.create_draft(chat_id=chat.id, user_id=user.id, image_path=image_path, raw_summary=raw_summary)
    state.schedule_expiry_cleanup(context.application, draft.id)

    status_msg = await message.reply_text("🤖 Reading your summary and polishing it for the website...")

    context.application.create_task(_produce_first_summary(context, draft, status_msg.message_id))


async def _produce_first_summary(context, draft: "state.ReviewDraft", status_message_id: int) -> None:
    try:
        summary = await generate_summary(draft.image_path, draft.raw_summary, is_regeneration=False, history=[])
    except SummaryGenerationError:
        logger.exception("Initial summary rewrite failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "website_failed"
        await present_website_failure(context, draft, status_message_id)
        return

    async with draft.lock:
        draft.summary = summary
        draft.summary_history.append(summary)
        draft.status = "website_ready"

    await present_website_result(context, draft, status_message_id)
