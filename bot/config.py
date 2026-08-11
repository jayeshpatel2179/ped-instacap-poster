import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = _require_env("OPENAI_API_KEY")
UPLOAD_POST_API_KEY = _require_env("UPLOAD_POST_API_KEY")
UPLOAD_POST_PROFILE = _require_env("UPLOAD_POST_PROFILE")

# PedTalks website upload endpoint - posted to first, before Instagram, in every flow.
WEBSITE_UPLOAD_API_KEY = _require_env("WEBSITE_UPLOAD_API_KEY")
WEBSITE_UPLOAD_URL = os.getenv("WEBSITE_UPLOAD_URL") or "https://pedtalks.com/api/instagram/upload"

# Vision-capable chat model used to read the finished graphic and write the
# caption + hashtags. gpt-4o (not the -mini variant) for the strongest
# read on the image + most on-brand writing. Kept swappable without a code change.
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")


def _parse_user_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(part.strip()) for part in raw.split(",") if part.strip())


# Comma-separated Telegram user IDs allowed to tap "Confirm & Post" (actually
# publishes to Instagram). Empty/unset = unrestricted - anyone in the group
# can post, same posture as the sibling PedTalks Extra bot's Go Live gate.
POST_ALLOWED_USER_IDS = _parse_user_ids(os.getenv("POST_ALLOWED_USER_IDS"))

# Hardcoded by design - not meant to vary at runtime.
DRAFT_EXPIRY_SECONDS = 30 * 60
TEMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "temp"
LOG_LEVEL = "INFO"

CAPTION_TEMPERATURE_INITIAL = 0.9
CAPTION_TEMPERATURE_REGEN = 1.1
CAPTION_HISTORY_LIMIT = 3  # how many previous captions get fed back in to steer regenerations away from repeats
MIN_HASHTAGS = 2
MAX_HASHTAGS = 4

SUMMARY_TEMPERATURE_INITIAL = 0.7
SUMMARY_TEMPERATURE_REGEN = 0.9
SUMMARY_HISTORY_LIMIT = 3  # how many previous rewrites get fed back in to steer regenerations away from repeats
SUMMARY_WORD_MIN = 50
SUMMARY_WORD_MAX = 60

# The website endpoint only accepts these formats (max 10MB), independent of
# what Upload-Post accepts for Instagram.
WEBSITE_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
WEBSITE_IMAGE_MAX_BYTES = 10 * 1024 * 1024

TEMP_DIR.mkdir(parents=True, exist_ok=True)
