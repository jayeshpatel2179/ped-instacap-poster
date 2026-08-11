import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import LOG_LEVEL, TELEGRAM_BOT_TOKEN
from bot.handlers.photo import on_photo
from bot.handlers.review import on_cancel_tap, on_checkstatus_tap, on_confirm_tap, on_noop_tap, on_regen_tap
from bot.handlers.start import help_command, start
from bot.handlers.website import on_website_abort_tap, on_website_golive_tap, on_website_regen_tap

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=LOG_LEVEL)
logger = logging.getLogger(__name__)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing an update", exc_info=context.error)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND, on_photo))
    application.add_handler(CallbackQueryHandler(on_website_golive_tap, pattern=r"^capbot:site_golive:"))
    application.add_handler(CallbackQueryHandler(on_website_regen_tap, pattern=r"^capbot:site_regen:"))
    application.add_handler(CallbackQueryHandler(on_website_abort_tap, pattern=r"^capbot:site_abort:"))
    application.add_handler(CallbackQueryHandler(on_confirm_tap, pattern=r"^capbot:confirm:"))
    application.add_handler(CallbackQueryHandler(on_regen_tap, pattern=r"^capbot:regen:"))
    application.add_handler(CallbackQueryHandler(on_cancel_tap, pattern=r"^capbot:cancel:"))
    application.add_handler(CallbackQueryHandler(on_checkstatus_tap, pattern=r"^capbot:checkstatus:"))
    application.add_handler(CallbackQueryHandler(on_noop_tap, pattern=r"^capbot:noop:"))
    application.add_error_handler(_error_handler)

    return application


def main() -> None:
    application = build_application()
    logger.info("Starting PedTalks caption/poster bot (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
