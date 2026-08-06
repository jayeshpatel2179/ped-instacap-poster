from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = (
    "Send me a finished PedTalkSports Instagram graphic (as a photo or as a file for full quality) "
    "and I'll look at it, write a caption in our voice, and pick 2-4 SEO hashtags.\n\n"
    "You'll get it back with three buttons:\n"
    "✅ Confirm & Post - publishes it to Instagram\n"
    "🔄 Regenerate - tries a different caption/hashtags for the same image\n"
    "❌ Cancel - drops it, nothing gets posted\n\n"
    "You can tap Regenerate as many times as you like before deciding."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)
