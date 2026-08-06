# ped-instacap-poster

Telegram bot for PedTalkSports. Send it an already-finished Instagram post
graphic (headline, player photo, footer already designed) and it:

1. Looks at the image with a vision model and writes an Instagram caption in
   PedTalks' voice.
2. Picks 2-4 lowercase SEO hashtags based on what's actually in the image.
3. Sends the image back with the caption + hashtags and three buttons:
   **✅ Confirm & Post**, **🔄 Regenerate**, **❌ Cancel**.
4. Regenerate can be tapped repeatedly for a genuinely different caption/
   hashtag pick each time (not a reworded copy).
5. Confirm & Post publishes to the PedTalkSports Instagram account via
   [Upload-Post](https://www.upload-post.com/), the same service the sibling
   `pedtalks-insta-image` bot uses for its "Go Live" feature.

This bot does not generate or design images - it only writes the caption and
posts an image you already finished elsewhere.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` - create a bot via [@BotFather](https://t.me/BotFather).
  If this bot will live in the same company group as `pedtalks-insta-image`,
  use a **new** bot (don't reuse that token) so the two don't fight over
  updates; you can reuse the same `OPENAI_API_KEY` and
  `UPLOAD_POST_API_KEY`/`UPLOAD_POST_PROFILE` values from that project's
  `.env` since they hit the same OpenAI account and the same Instagram
  profile.
- `OPENAI_API_KEY` - needs access to a vision-capable chat model.
- `UPLOAD_POST_API_KEY` / `UPLOAD_POST_PROFILE` - from upload-post.com,
  pointed at the PedTalkSports Instagram profile.
- `POST_ALLOWED_USER_IDS` - optional. Leave blank to let anyone in the chat
  tap Confirm & Post; set it to restrict who can actually publish.

**Group chat privacy mode:** if this bot runs in a group (not a 1:1 DM),
disable privacy mode via BotFather (`/mybots` → bot → Bot Settings → Group
Privacy → Turn off), or promote the bot to admin - otherwise Telegram won't
deliver plain photo messages to it. Remove and re-add the bot after changing
this setting.

Run it:

```
python run.py
```

## Deploying on Railway

`railway.json` pins the start command to `python run.py` and `.python-version`
pins Python 3.12, so Railway/Nixpacks doesn't need to guess either. There's
no HTTP server here (the bot runs a Telegram long-poll loop), so don't
generate a public domain for the service - it doesn't serve anything.

Add these variables in the Railway service's **Variables** tab (same values
as your local `.env`):

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from BotFather |
| `OPENAI_API_KEY` | yes | needs access to a vision-capable model |
| `OPENAI_VISION_MODEL` | no | defaults to `gpt-4o` |
| `UPLOAD_POST_API_KEY` | yes | from upload-post.com |
| `UPLOAD_POST_PROFILE` | yes | the Upload-Post profile to publish through |
| `POST_ALLOWED_USER_IDS` | no | comma-separated Telegram user ids; blank = unrestricted |

Deploy, then check the service logs for `Application started` to confirm it
connected. Since Telegram only allows one process to poll a given bot token
at a time, make sure no local `python run.py` is still running against the
same `TELEGRAM_BOT_TOKEN` once the Railway deployment is live.

## How review works

Every image gets its own in-memory "draft" identified by a random id baked
into that message's button `callback_data` - not by chat/user - so several
people posting images in the same group at the same moment never cross wires,
even if they're all mid-review simultaneously. A draft expires after 30
minutes if nobody taps a button; the temp image file is cleaned up either way
(cancelled, posted, or expired).

If posting to Instagram fails (network/API error), the bot edits the message
to show the error and restores the Confirm/Regenerate/Cancel buttons with the
**same caption and hashtags already approved** - nothing is lost, no crash,
just tap Confirm & Post again once whatever was wrong is fixed.

## Project layout

```
bot/
├── main.py                     # Application builder, registers handlers, starts polling
├── config.py                   # env vars + hardcoded constants
├── state.py                    # in-memory draft store, keyed by random id, one asyncio.Lock per draft
├── caption/
│   └── generator.py            # vision call -> {caption, hashtags} JSON, regen diversity via angle hints + history
├── posting/
│   └── uploadpost_client.py    # Upload-Post SDK wrapper (same pattern as pedtalks-insta-image's go_live client)
├── handlers/
│   ├── start.py                # /start, /help
│   ├── photo.py                # entry point: photo/image-document received -> create draft -> first caption
│   └── review.py               # Confirm/Regenerate/Cancel callback handlers + message rendering
└── utils/
    └── image_utils.py          # download Telegram photo/document, temp-file cleanup
```
