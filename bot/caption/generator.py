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
    SUMMARY_TEMPERATURE_INITIAL,
    SUMMARY_TEMPERATURE_REGEN,
    SUMMARY_WORD_MAX,
    SUMMARY_WORD_MIN,
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

Sometimes the user sending the image will also give you a short context note (e.g. who's pictured, \
what just happened, why this is being posted). When that note is present, treat it as the primary \
source of truth about the story - the image is only supporting visual context. Never contradict the \
note, and don't fall back to guessing at the image alone when it's given. When no note is given, work \
from the image alone as before.

CRITICAL - match the note's tone and opinion, not just its facts: the note is also the source of \
truth for the ATTITUDE to take, not only the who/what/why. If the note is critical, sarcastic, \
mocking, angry, or otherwise negative about the subject, the caption must land the same way - keep \
the sting, don't file it down. If it's hyped, proud, or celebratory, match that instead. Do not \
default to a positive/supportive spin just because that's the safer or more typical sports-page \
tone - inventing enthusiasm the note doesn't contain is as wrong as inventing a fact it doesn't \
contain. Example: a note mocking a player's ego over a transfer should produce a caption that mocks \
that ego, in PedTalkSports' voice - not one that celebrates the move.

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


class SummaryGenerationError(Exception):
    """Raised when the website-summary rewrite call fails or returns something unusable."""


SUMMARY_SYSTEM_PROMPT = f"""You are a copy editor for PedTalkSports, a sports news/commentary brand. You'll be \
shown a finished Instagram post graphic and a draft summary the user wrote about it. Your job is to \
rewrite that draft into a clean, {SUMMARY_WORD_MIN}-{SUMMARY_WORD_MAX} word summary for the PedTalks \
website - a straightforward, informative recap of the story, not a social caption.

Rules:
- Preserve every fact, name, number, and claim from the user's draft - do not invent, drop, or change \
what happened. You are tightening and polishing wording/grammar/flow, not reporting new details.
- Preserve the user's tone and opinion just as strictly as the facts. If their draft is critical, \
sarcastic, mocking, angry, or negative about the subject, the rewrite must read the same way - clean \
up the grammar and flow, but do not launder out the attitude, and do not soften criticism into \
neutrality or flip it into praise. If their draft is positive, hyped, or celebratory, match that \
instead. Never default to a "safe" neutral news tone when the user's draft wasn't neutral - matching \
their actual stance matters more than sounding like a wire report.
- Use the image only to sanity-check names/context already implied by the draft - never contradict the \
draft with something you infer from the image alone.
- Target length is {SUMMARY_WORD_MIN}-{SUMMARY_WORD_MAX} words. Stay in that range.
- Third-person, no emoji, no hashtags, no quotation marks around the whole thing. Structurally still \
reads like a recap, not a social caption - but the attitude in the wording should match the user's \
draft, not a generic neutral register.
- Plain prose, 1-3 sentences.

Respond with STRICT JSON only, no markdown fences, no commentary, exactly this shape:
{{"summary": "..."}}
"""


_EMPTY_RESPONSE_ATTEMPTS = 2


async def _complete_json(
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    error_cls: type[Exception],
) -> str:
    """Run a JSON-mode chat completion, retrying once if the model comes back with no content.

    An empty content with a 200 OK is usually a refusal (message.refusal is set) or a cut-off
    (finish_reason == "length"); both are logged so the failure is self-explanatory.
    """
    reason = "unknown"
    for attempt in range(1, _EMPTY_RESPONSE_ATTEMPTS + 1):
        try:
            response = await _client.chat.completions.create(
                model=OPENAI_VISION_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise error_cls(f"Vision API call failed: {exc}") from exc

        choice = response.choices[0]
        if choice.message.content:
            return choice.message.content

        refusal = getattr(choice.message, "refusal", None)
        reason = f"refusal={refusal!r}" if refusal else f"finish_reason={choice.finish_reason!r}"
        logger.warning("Model returned empty content (attempt %d/%d): %s", attempt, _EMPTY_RESPONSE_ATTEMPTS, reason)

    raise error_cls(f"Model returned an empty response ({reason})")


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
    user_context: str | None = None,
) -> tuple[CaptionResult, str | None]:
    """Look at the image (and optional user-provided context) and produce a caption + hashtags.

    user_context is free text the user attached to the image (who/what/why) - when present it's
    the primary source of truth, the image is only supporting. When absent, behavior is unchanged
    from image-only captioning.

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
    else:
        instruction = "Write the caption and hashtags for this image."

    if user_context:
        instruction += (
            f"\n\nContext provided by the user about this post (this is the primary source of "
            f"truth for both the facts AND the tone/opinion to take - match their stance, whether "
            f"critical, sarcastic, mocking, angry, hyped, or celebratory; do not soften it or flip "
            f"it positive; the image is only supporting visual context):\n{user_context.strip()}"
        )

    user_parts.append({"type": "text", "text": instruction})
    user_parts.append({"type": "image_url", "image_url": {"url": data_url}})

    temperature = CAPTION_TEMPERATURE_REGEN if is_regeneration else CAPTION_TEMPERATURE_INITIAL

    content = await _complete_json(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_parts},
        ],
        temperature=temperature,
        max_tokens=400,
        error_cls=CaptionGenerationError,
    )

    return _parse_response(content), angle


def _parse_summary_response(content: str) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise SummaryGenerationError(f"Model returned non-JSON output: {content[:200]!r}") from exc

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SummaryGenerationError(f"Model response missing a usable summary: {data!r}")

    summary = summary.strip()
    word_count = len(summary.split())
    if not (SUMMARY_WORD_MIN <= word_count <= SUMMARY_WORD_MAX):
        logger.warning("Summary rewrite came back at %d words (target %d-%d): %r", word_count, SUMMARY_WORD_MIN, SUMMARY_WORD_MAX, summary)

    return summary


async def generate_summary(
    image_path: Path,
    raw_summary: str,
    *,
    is_regeneration: bool,
    history: list[str],
) -> str:
    """Rewrite the user's draft summary into a {SUMMARY_WORD_MIN}-{SUMMARY_WORD_MAX} word website summary.

    raw_summary is the user's own text, always the primary source of truth for facts; the image is
    only used to sanity-check it. Returns the rewritten summary text.
    """
    data_url = _encode_image(image_path)

    if is_regeneration:
        instruction = (
            "Rewrite the draft summary again - genuinely different wording/structure from your previous "
            f"attempt(s) below, while preserving the same facts AND the same tone/opinion (if the draft is "
            f"critical, sarcastic, or negative, stay critical/sarcastic/negative - don't drift positive or "
            f"neutral on a reroll), staying {SUMMARY_WORD_MIN}-{SUMMARY_WORD_MAX} words.\n\n"
            "Your previous rewrite(s) (do not repeat this exact wording):\n"
            + "\n".join(f"- {c}" for c in history)
            + f"\n\nUser's original draft summary:\n{raw_summary.strip()}"
        )
    else:
        instruction = f"User's draft summary to rewrite:\n{raw_summary.strip()}"

    user_parts = [
        {"type": "text", "text": instruction},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]

    temperature = SUMMARY_TEMPERATURE_REGEN if is_regeneration else SUMMARY_TEMPERATURE_INITIAL

    content = await _complete_json(
        [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_parts},
        ],
        temperature=temperature,
        max_tokens=250,
        error_cls=SummaryGenerationError,
    )

    return _parse_summary_response(content)
