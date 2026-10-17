# Architecture

## 1. Pipeline overview

```
 ┌────────────┐   ┌──────────┐   ┌────────────┐   ┌───────────────┐   ┌──────────┐
 │ 1 DISCOVER │ → │ 2 FETCH  │ → │3 TRANSCRIBE│ → │ 4 SCORE+TRIM  │ → │ 5 RENDER │
 │ find hot   │   │ download │   │ faster-    │   │ LLM picks exact│   │ FFmpeg   │
 │ moments    │   │ ~3 min   │   │ whisper    │   │ in/out + score │   │ 9:16 +   │
 └────────────┘   └──────────┘   └────────────┘   └───────────────┘   │ captions │
                                                                       └────┬─────┘
 ┌────────────┐   ┌──────────┐   ┌────────────┐   ┌───────────────┐        │
 │ 9 MEASURE  │ ← │8 PUBLISH │ ← │ 7 SCHEDULE │ ← │ 6 COPYWRITE   │ ←──────┘
 │ pull stats │   │ official │   │ review UI +│   │ titles, hooks, │
 │ feed back  │   │ APIs     │   │ time slots │   │ hashtags       │
 └─────┬──────┘   └──────────┘   └────────────┘   └───────────────┘
       └──────────── learnings adjust scoring prompts + streamer priority ───────────┘
```

Each stage is a **worker function** that reads jobs with status `X` from SQLite, does its
work, and sets status `Y`. APScheduler runs each worker on an interval. If anything crashes,
the job just stays at its last status and gets retried. No complex queue infrastructure is needed.

## 2. Stages in detail

### 1. Discover (find candidate moments)
Three signals get merged into one "moment candidates" list:

| Signal | How | Why it works |
|---|---|---|
| **Twitch viewer clips** | Twitch Helix `GET /clips?broadcaster_id=…&started_at=…` (free) | Viewers already voted with their clicks. Several clips in the same 60s window = a guaranteed moment |
| **Chat velocity** | Listen to chat live over IRC (`twitchio`), or pull VOD chat afterward (`chat-downloader`). Flag windows where messages/sec > 3× the rolling average, or spikes in `KEKW`, `LUL`, `OMEGALUL`, `W`, `L`, `Clip it` | Chat reacts 2–10s after the moment happens |
| **Audio spikes** | FFmpeg `ebur128` loudness on the VOD audio; flag screams/laughter peaks | Catches moments chat misses |

Candidates within 30s of each other get merged. Each gets a `signal_score`. The top N per
stream (default 15) move to Fetch.

### 2. Fetch
- Download **only the window** (moment −60s to +30s), not the whole VOD: `yt-dlp --download-sections "*start-end"` on the VOD URL.
- For live streams: `streamlink` records a rolling buffer, or wait for the VOD (simpler; the default).
- Store under `data/raw/{stream_id}/{candidate_id}.mp4`.

### 3. Transcribe
- `faster-whisper` (model `small` or `medium`, int8 on CPU; Apple Silicon handles it fine), with **word-level timestamps**.
- Output: `transcript.json` with words and their start/end times. Reused later for captions.

### 4. Score + Trim (the "brain")
Send the transcript plus signal info to **Claude Haiku 4.5** with a structured-output prompt:

```json
{
  "keep": true,
  "virality_score": 82,
  "moment_type": "rage | funny | clutch | wholesome | drama | hot_take | fail",
  "start_sec": 41.2,
  "end_sec": 68.9,
  "hook_line": "the exact quote that should open the video",
  "why": "one sentence"
}
```
Rules baked into the prompt: the clip starts **at or right before** the most arresting line
(no build-up), lasts 15–45s (the sweet spot for Shorts/TikTok), and ends on the payoff or reaction.
Anything under `min_score` (default 60) gets dropped. The prompt also gets "past winners" examples
from the analytics loop, so the picker improves over time.

Cost: roughly 1.5k tokens in and 200 out per candidate, which is about **$0.002 each**.

### 5. Render (FFmpeg)
Layout templates in `config/layouts.yaml`, one per streamer:
- **Split (default):** facecam crop scaled to the top 35%, gameplay center-crop on the bottom 65%. The facecam box coordinates are set once per streamer (or auto-detected with MediaPipe face detection on a sample frame).
- **Full-face / IRL:** MediaPipe tracks the face and a smoothed crop window follows it.
- **Blur-fill:** the original 16:9 in the middle over a blurred, zoomed copy (fallback for anything).

Then:
- **Captions:** generate an `.ass` subtitle file from the word timestamps. Word-by-word pop, bold white with a black outline, current word highlighted in yellow, placed in the center-lower third (clear of the platform UI).
- **Hook text:** a big on-screen text box for the first 2–3 seconds (from the copywriter).
- **Polish:** `loudnorm` to −14 LUFS, small zoom punch-ins on high-energy words (optional), and a trim of any dead air in the first 0.5s.
- Output: 1080×1920, H.264, 30/60fps, under 60s → `data/out/{clip_id}.mp4` plus a thumbnail frame.

### 6. Copywrite
A second Haiku call, given the transcript, moment type, streamer, and the platform's recent winning titles:
- 3 title/hook variants in different styles (curiosity gap, quote, reaction/stakes). See VIRALITY_PLAYBOOK.md.
- A caption with credit (`🎥 twitch.tv/{streamer}`).
- Hashtags: 2 broad + 3 niche + 2 streamer/game tags, drawn from a per-niche tag bank in config and updated by analytics.
- Output stored in the `copy` table per platform (TikTok, YT and IG each get tuned text).

### 7. Review & Schedule
- **FastAPI** app serving one simple mobile-first page: a video player, score, title picker, and approve/reject buttons. Reach it from your phone over your home Wi-Fi, or through a free Tailscale/Cloudflare tunnel.
- Approved clips get placed in the next open **time slot** for each account (slots in `config/accounts.yaml`, e.g. 4/day at peak hours). Each account has a max posts/day cap.

### 8. Publish
One adapter per platform, all with the same interface: `publish(clip, copy, account) -> post_id`.

| Platform | API | Notes |
|---|---|---|
| YouTube Shorts | YouTube Data API v3 `videos.insert` | Free. The default quota fits roughly 6 uploads/day per project. Vertical + under 60s = a Short |
| TikTok | Content Posting API (Direct Post) | Needs a TikTok developer app. **Until the app passes TikTok's audit, posts are private-only.** Fallback: upload as a draft to your inbox and tap publish on your phone |
| Instagram Reels | Instagram Graph API (container → publish) | Needs an IG Business/Creator account linked to a FB Page. Video must be at a public URL (serve it briefly through the tunnel, or use free Cloudflare R2) |

### 9. Measure
A daily job pulls views, likes, comments and shares per post (YouTube Analytics, TikTok Display API,
IG Insights) into the `metrics` table. A weekly report plus the dashboard show:
- views by streamer, by moment_type, by hook style, by posting hour
- The top 10 clips get fed back as few-shot examples into the Score and Copywrite prompts.

## 3. Tech stack & why

| Layer | Choice | Why / free alternative |
|---|---|---|
| Language | Python 3.12 | Best ecosystem for video + AI; easy to maintain with Claude Code |
| Video | FFmpeg | Free industry standard; does all cutting, cropping and captions |
| Download | yt-dlp, streamlink | Free, well maintained |
| Transcription | faster-whisper | Free, local, word timestamps. (Paid alt: Deepgram ~$0.004/min) |
| Face tracking | MediaPipe | Free, fast on CPU |
| LLM | **Any provider via LiteLLM** (default: Claude Haiku 4.5) | One interface for Claude / OpenAI / Gemini / Groq / local Ollama. Switch with `LLM_MODEL` in `.env`. Structured JSON output is validated with Pydantic so any model works |
| DB | SQLite | Zero setup, one file. Swap to Postgres only if it ever becomes a SaaS |
| Scheduler | APScheduler (in-process) | No Redis/Celery needed |
| Web UI | FastAPI + HTMX + Tailwind (CDN) | One small server; no build step |
| Storage | Local disk; auto-delete raw files after 3 days | Cloudflare R2 free tier (10GB) if IG needs public URLs |
| Remote access | Tailscale (free) | Review queue on your phone, safely |

## 4. Data model (SQLite)

```
streamers   (id, platform, handle, broadcaster_id, niche, layout, priority, allowed_to_clip, notes)
streams     (id, streamer_id, vod_url, started_at, ended_at, status)
candidates  (id, stream_id, t_start, t_end, signal_score, signals_json, status)
clips       (id, candidate_id, start_sec, end_sec, virality_score, moment_type, hook_line,
             transcript_path, video_path, thumb_path, status)   -- status: rendered|approved|rejected|posted
copy        (id, clip_id, platform, title, caption, hashtags, hook_text, variant, chosen)
accounts    (id, platform, handle, niche, daily_cap, slots_json, auth_ref)
posts       (id, clip_id, account_id, platform_post_id, url, scheduled_at, posted_at, status, error)
metrics     (post_id, captured_at, views, likes, comments, shares, watch_time)
```

## 5. Planned folder layout

```
stream-clipper/
├── README.md
├── CLAUDE.md                 # instructions for future Claude Code sessions
├── pyproject.toml
├── .env                      # secrets (never committed)
├── config/
│   ├── streamers.yaml
│   ├── accounts.yaml
│   ├── layouts.yaml
│   └── hashtags.yaml
├── clipper/
│   ├── main.py               # starts scheduler + web UI
│   ├── db.py                 # SQLite models
│   ├── discover/             # twitch_clips.py, chat_velocity.py, audio_peaks.py
│   ├── fetch.py
│   ├── transcribe.py
│   ├── brain/                # score.py, copywrite.py, prompts/
│   ├── render/               # layouts.py, captions_ass.py, ffmpeg.py
│   ├── publish/              # base.py, youtube.py, tiktok.py, instagram.py
│   ├── metrics.py
│   └── web/                  # FastAPI app + templates (review queue, dashboard)
├── data/                     # raw/, work/, out/  (gitignored)
└── tests/
```

## 6. Deployment path

1. **Now:** run on your Mac with `python -m clipper.main`, and keep it awake while it's running.
2. **24/7:** a Hetzner CX22 VPS (~€4/mo, 2 vCPU/4GB) handles about 30 clips/day with whisper `small` on CPU. Docker Compose, one container.
3. **Scale:** only if needed, move rendering to a GPU spot box or a serverless GPU (Modal/RunPod, pay per second).
