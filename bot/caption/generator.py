import base64
import json
import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from bot.config import (
    CAPTION_TEMPERATURE_INITIAL,
    CAPTION_TEMPERATURE_REGEN,
    MAX_HASHTAGS,
    MIN_HASHTAGS,
    OPENAI_API_KEY,
    OPENAI_VISION_MODEL,
)

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

_MIME_BY_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

SYSTEM_PROMPT = f"""You are the Instagram voice of PedTalkSports, a sports hot-take/commentary brand.

You will be shown a finished, fully-designed Instagram post graphic - it already has its headline \
text, player photo, and PedTalkSports footer branding baked in. Your only job is to look at it and \
write the caption that goes in the Instagram post text box alongside it, plus SEO hashtags. You are \
NOT designing or describing the graphic itself.

Voice: confident, punchy, first-person as the page itself (use "we"/"I" as PedTalkSports), like a hot \
take dropped in the group chat - not a press release, not a generic sports-blog caption.

Caption rules:
- 1-3 short sentences, under ~280 characters.
- React to what's actually shown in the image (the headline/quote, the player, the moment) - be \
specific, not generic.
- Do not restate the on-image headline verbatim; riff on it in the page's voice.
- At most one emoji, only if it fits naturally - no emoji spam.
- Do not include hashtags inside the caption text itself; they go in a separate field.
- Do not wrap the caption in quotation marks.

Hashtag rules:
- Choose between {MIN_HASHTAGS} and {MAX_HASHTAGS} hashtags, never more than {MAX_HASHTAGS}.
- Base them specifically on what's identifiable in the image (player name, team, club, league, \
competition, rivalry, etc.). Avoid generic filler (#sports, #football) unless nothing more specific \
genuinely applies.
- Strictly lowercase, single word or runtogether phrase, no spaces, no punctuation, no "#" character \
in the value itself (it gets added when displayed).
- No duplicate or near-duplicate tags.

Respond with STRICT JSON only, no markdown fences, no commentary, exactly this shape:
{{"caption": "...", "hashtags": ["tag1", "tag2"]}}
"""

_ANGLE_HINTS = [
    "Lead with the emotional reaction a fan would have seeing this.",
    "Lead with the stat, achievement, or record implied by the image.",
    "Lead with a bold prediction or challenge to critics/doubters.",
    "Lead with the rivalry or opponent angle.",
    "Lead with a short, witty one-liner rather than analysis.",
    "Lead with why this moment matters right now.",
]


@dataclass
class CaptionResult:
    caption: str
    hashtags: list[str]


class CaptionGenerationError(Exception):
    """Raised when the vision model call fails or returns something unusable."""


def _encode_image(image_path: Path) -> str:
    mime = _MIME_BY_SUFFIX.get(image_path.suffix.lower(), "image/jpeg")
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _normalize_hashtags(raw: list) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in raw:
        if not isinstance(tag, str):
            continue
        tag = tag.strip().lstrip("#").lower()
        tag = re.sub(r"[^a-z0-9]", "", tag)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= MAX_HASHTAGS:
            break
    return cleaned


def _parse_response(content: str) -> CaptionResult:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CaptionGenerationError(f"Model returned non-JSON output: {content[:200]!r}") from exc

    caption = data.get("caption")
    hashtags = data.get("hashtags")
    if not isinstance(caption, str) or not caption.strip():
        raise CaptionGenerationError(f"Model response missing a usable caption: {data!r}")
    if not isinstance(hashtags, list):
        raise CaptionGenerationError(f"Model response missing a usable hashtag list: {data!r}")

    normalized = _normalize_hashtags(hashtags)
    if len(normalized) < MIN_HASHTAGS:
        logger.warning("Model returned only %d usable hashtag(s): %r", len(normalized), hashtags)

    return CaptionResult(caption=caption.strip(), hashtags=normalized)


async def generate_caption(
    image_path: Path,
    *,
    is_regeneration: bool,
    history: list[str],
    used_angles: list[str],
) -> tuple[CaptionResult, str | None]:
    """Look at the image and produce a caption + hashtags.

    Returns (result, angle_used). angle_used is None on the first (non-regen)
    call; on a regeneration it's the framing hint that was picked, so the
    caller can track it in used_angles and avoid repeating it next time.
    """
    data_url = _encode_image(image_path)

    user_parts: list[dict] = []
    angle: str | None = None

    if is_regeneration:
        remaining = [a for a in _ANGLE_HINTS if a not in used_angles] or _ANGLE_HINTS
        angle = random.choice(remaining)
        instruction = (
            "Write a NEW caption for the same image - genuinely different from your previous "
            "attempt(s) below, not a reworded version of the same idea. Change the angle, structure, "
            "and wording. Feel free to re-pick different hashtags too if better ones fit.\n\n"
            f"Framing to use this time: {angle}\n\n"
            "Caption(s) already shown to the user (do not repeat these ideas):\n"
            + "\n".join(f"- {c}" for c in history)
        )
        user_parts.append({"type": "text", "text": instruction})
    else:
        user_parts.append({"type": "text", "text": "Write the caption and hashtags for this image."})

    user_parts.append({"type": "image_url", "image_url": {"url": data_url}})

    temperature = CAPTION_TEMPERATURE_REGEN if is_regeneration else CAPTION_TEMPERATURE_INITIAL

    try:
        response = await _client.chat.completions.create(
            model=OPENAI_VISION_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_parts},
            ],
            temperature=temperature,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        raise CaptionGenerationError(f"Vision API call failed: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise CaptionGenerationError("Model returned an empty response")

    return _parse_response(content), angle
