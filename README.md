# ped-instacap-poster

Telegram bot for PedTalkSports. Send it an already-finished Instagram post
graphic (headline, player photo, footer already designed) **with a 50-60 word
summary attached as the caption on the photo message** and it walks through a
single review-then-publish flow, reusing that one image for both platforms:

1. Reads your draft summary and rewrites it into a clean 50-60 word version
   (same facts/names, just polished wording), using the image only to sanity-
   check what's already in your draft. Sends it back with two buttons:
   **✅ Save**, **🔄 Regenerate**.
2. Save locks the summary in (doesn't post anywhere yet) and immediately
   writes an Instagram caption + 2-4 lowercase SEO hashtags in PedTalks'
   voice, using that saved summary as the primary source of truth for
   who/what/why (the image is only supporting context).
3. Shows the summary (saved) alongside the caption + hashtags for a final
   look, with three buttons: **🚀 Go Live**, **🔄 Regenerate**, **❌ Abort**.
   At this stage Regenerate only rewrites the caption/hashtags - the summary
   is already locked in.
4. Go Live publishes to the PedTalks website and the PedTalkSports Instagram
   account (via [Upload-Post](https://www.upload-post.com/), the same service
   the sibling `pedtalks-insta-image` bot uses) **at the same time**. If one
   side fails while the other succeeds, tapping Go Live again only retries
   whichever side is still outstanding - it never double-posts a platform
   that already went live. Abort is only available before anything has
   posted; once either platform is live (or Instagram is mid-processing),
   only retry/Check Status remain. `/cancel` does the same thing as Abort
   but as a text command - drops your own most recent pending draft in that
   chat, at either stage, with the same "nothing posted yet" restriction.
5. Regenerate can be tapped repeatedly at either stage for a genuinely
   different rewrite/caption each time, not a reworded copy.

This bot does not generate or design images - it only writes the copy and
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
- `WEBSITE_UPLOAD_API_KEY` - the `x-api-key` value provided by the PedTalks
  website team for `POST https://www.pedtalkssports.com/api/instagram/upload`.
- `WEBSITE_UPLOAD_URL` - optional, defaults to
  `https://www.pedtalkssports.com/api/instagram/upload`.
- `POST_ALLOWED_USER_IDS` - optional. Leave blank to let anyone in the chat
  tap Go Live; set it to restrict who can actually publish.

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
| `WEBSITE_UPLOAD_API_KEY` | yes | `x-api-key` for the PedTalks website upload endpoint |
| `WEBSITE_UPLOAD_URL` | no | defaults to `https://www.pedtalkssports.com/api/instagram/upload` |
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
(aborted, posted, or expired).

A draft always carries the user's original 50-60 word draft summary
(`raw_summary`, required on the photo's caption) and the AI-rewritten version
(`summary`). Once Saved, `summary` is reused as the primary source of truth
for the Instagram caption too, so the story stays consistent from the saved
summary through to the Instagram caption - only the wording/angle changes on
Regenerate.

Publish state is tracked as independent per-platform flags
(`website_posted`, `instagram_posted`, `instagram_pending`), not a single
status - so if Go Live partially fails (one platform succeeds, the other
errors or Instagram's async worker is still processing), tapping Go Live
again only retries whatever's still outstanding. It never double-posts a
platform that already went live, and Instagram's "still processing"
(`instagram_pending`) case surfaces a **🔄 Check Status** button instead of
being retried blindly. Abort is only available before anything has posted or
gone pending.

## Project layout

```
bot/
├── main.py                     # Application builder, registers handlers, starts polling
├── config.py                   # env vars + hardcoded constants
├── state.py                    # in-memory draft store, keyed by random id, one asyncio.Lock per draft
├── caption/
│   └── generator.py            # vision calls -> summary rewrite JSON and {caption, hashtags} JSON
├── posting/
│   ├── website_client.py       # PedTalks website upload API wrapper (image + summary, x-api-key auth)
│   └── uploadpost_client.py    # Upload-Post SDK wrapper (same pattern as pedtalks-insta-image's go_live client)
├── handlers/
│   ├── start.py                # /start, /help
│   ├── photo.py                # entry point: photo/image-document + required summary -> create draft -> first rewrite
│   └── publish.py              # merged review flow (Save/Regenerate -> Go Live/Regenerate/Abort -> concurrent publish) + /cancel
└── utils/
    └── image_utils.py          # download Telegram photo/document, temp-file cleanup
```
