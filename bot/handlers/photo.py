import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from bot import state
from bot.caption.generator import CaptionGenerationError, generate_caption
from bot.handlers.review import present_failure, present_result
from bot.utils.image_utils import save_telegram_file

logger = logging.getLogger(__name__)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message.photo:
        tg_file = await message.photo[-1].get_file()
        suffix = ".jpg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        tg_file = await message.document.get_file()
        suffix = Path(message.document.file_name or "image.jpg").suffix or ".jpg"
    else:
        return

    image_path = await save_telegram_file(tg_file, chat.id, suffix)

    draft = state.create_draft(chat_id=chat.id, user_id=user.id, image_path=image_path)
    state.schedule_expiry_cleanup(context.application, draft.id)

    status_msg = await message.reply_text("🤖 Looking at the image and writing a caption...")

    context.application.create_task(_produce_first_caption(context, draft, status_msg.message_id))


async def _produce_first_caption(context, draft: "state.ReviewDraft", status_message_id: int) -> None:
    try:
        result, _angle = await generate_caption(draft.image_path, is_regeneration=False, history=[], used_angles=[])
    except CaptionGenerationError:
        logger.exception("Initial caption generation failed for draft %s", draft.id)
        async with draft.lock:
            draft.status = "failed"
        await present_failure(context, draft, status_message_id)
        return

    async with draft.lock:
        draft.caption = result.caption
        draft.hashtags = result.hashtags
        draft.history.append(result.caption)
        draft.status = "ready"

    await present_result(context, draft, status_message_id)
