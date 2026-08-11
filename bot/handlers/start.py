from telegram import Update
from telegram.ext import ContextTypes

HELP_TEXT = (
    "Send the image (with a 50-60 word summary as the photo's caption) for posting on the website "
    "and Instagram.\n\n"
    "Here's what happens:\n"
    "I polish your summary and send it back: 🌐 Go Live Website, 🔄 Regenerate, ❌ Abort.\n"
    "Go Live Website posts the image + summary to the PedTalks website.\n"
    "Once that's live, I write an Instagram caption + hashtags from that summary and ask again: "
    "🚀 Go Live Instagram, 🔄 Regenerate, ❌ Abort.\n"
    "Go Live Instagram publishes to the PedTalkSports Instagram account.\n\n"
    "You can tap Regenerate as many times as you like before deciding."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)
