from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = (
    "Send the image (with a 50-60 word summary as the photo's caption) for posting on the website "
    "and Instagram.\n\n"
    "Here's what happens:\n"
    "I polish your summary and send it back: ✅ Save, 🔄 Regenerate.\n"
    "Save locks the summary in (doesn't post yet) and writes the Instagram caption + hashtags from it.\n"
    "Then you'll see: 🚀 Go Live, 🔄 Regenerate, ❌ Abort.\n"
    "Go Live publishes to the website and Instagram at the same time.\n\n"
    "You can tap Regenerate as many times as you like at either step before deciding.\n\n"
    "/cancel drops your most recent pending draft (same as tapping Abort) - works any time before "
    "anything's actually been posted."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)
