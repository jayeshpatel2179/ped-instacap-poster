import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import LOG_LEVEL, TELEGRAM_BOT_TOKEN
from bot.handlers.photo import on_photo
from bot.handlers.publish import (
    on_abort_tap,
    on_cancel_command,
    on_checkstatus_tap,
    on_golive_tap,
    on_noop_tap,
    on_regen_caption_tap,
    on_regen_summary_tap,
    on_save_tap,
)
from bot.handlers.start import help_command, start

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=LOG_LEVEL)
# httpx logs every request URL at INFO, and Telegram URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", on_cancel_command))
    application.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, on_photo))
    application.add_handler(CallbackQueryHandler(on_save_tap, pattern=r"^capbot:save:"))
    application.add_handler(CallbackQueryHandler(on_regen_summary_tap, pattern=r"^capbot:regen_summary:"))
    application.add_handler(CallbackQueryHandler(on_regen_caption_tap, pattern=r"^capbot:regen_caption:"))
    application.add_handler(CallbackQueryHandler(on_golive_tap, pattern=r"^capbot:golive:"))
    application.add_handler(CallbackQueryHandler(on_checkstatus_tap, pattern=r"^capbot:checkstatus:"))
    application.add_handler(CallbackQueryHandler(on_abort_tap, pattern=r"^capbot:abort:"))
    application.add_handler(CallbackQueryHandler(on_noop_tap, pattern=r"^capbot:noop:"))
    application.add_error_handler(_error_handler)

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting PedTalks caption/poster bot (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
