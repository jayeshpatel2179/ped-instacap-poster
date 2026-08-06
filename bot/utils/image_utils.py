import uuid
from pathlib import Path

from telegram import File

from bot.config import TEMP_DIR


async def save_telegram_file(file: File, chat_id: int, suffix: str) -> Path:
    dest = TEMP_DIR / f"{chat_id}_{uuid.uuid4().hex}{suffix}"
    await file.download_to_drive(custom_path=str(dest))
    return dest


def cleanup(*paths: Path) -> None:
    for path in paths:
        if path is not None:
            Path(path).unlink(missing_ok=True)
