# Stream Clipper

The whole pipeline runs end to end with zero API keys. Every paid piece has a free fallback, and nothing is posted until a person approves it in a review app.

Stream Clipper turns long videos (podcasts, streams) into vertical short-form clips: face-tracked 9:16 framing, word-by-word captions, a hook banner, titles and hashtags. It then schedules the approved clips and publishes them to YouTube Shorts, TikTok and Instagram Reels through the platforms' official APIs, or exports a ready-to-post folder when an account is not connected.

`Python 3.12` · `yt-dlp` · `faster-whisper` · `FFmpeg` · `MediaPipe` · `LiteLLM` · `FastAPI` · `SQLite`

## How it works

```
discover ─▶ fetch ─▶ transcribe ─▶ score + trim ─▶ render
                                                     │
measure ◀─ publish ◀─ schedule (review app) ◀─ copywrite
```

Each stage is a worker that reads jobs with one status from SQLite, does its work and sets the next status. If a stage crashes, the job stays where it was and is retried, so there is no queue to run. Details are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

- **Picking moments.** An LLM proposes in and out points and a score. Without a key, a heuristic picker takes over, built from the transcript's word timestamps and a small set of signal words.
- **Rendering.** Face tracking keeps the speaker framed in vertical layouts, and captions pop word by word from the transcript's timings.
- **Copy.** Titles, hooks and hashtags come from the LLM, or from templates when no key is set.
- **Review.** A local web app shows each candidate clip with title options. Approving one schedules it into an account's posting slots.
- **Measure.** Views and likes are pulled back and summarised, and the results adjust scoring priorities.

## What each key unlocks

| You add | Unlocks |
|---|---|
| Nothing | Clipping, the review app, and export folders (video plus caption) for manual posting |
| An LLM key in `.env` (`GEMINI_API_KEY` has a free tier) | LLM moment picking and three title variants, hooks and hashtags |
| `TWITCH_CLIENT_SECRET` and `config/streamers.yaml` | Discovery of top viewer clips from streamers who allow clipping |
| `config/accounts.yaml` and `./clip auth ...` | Automatic posting and view tracking |

## Use it

```bash
uv sync
./clip clip "https://www.youtube.com/watch?v=..." --start 20:00 --end 45:00   # clip one video now
./clip run                       # autopilot plus the review app at http://localhost:8000
./clip check                     # what is connected and what is running on fallbacks
python -m pytest                 # 12 tests
```

All commands: `./clip --help`. Copy `config/.env.example` to `.env` and the `*.example.yaml` files to their real names to configure keys and accounts. Neither `.env` nor the real configs are committed.

## Rules the code follows

- Official platform APIs only. There is no browser-automation posting.
- Only sources that allow it: streamers are skipped unless their config entry has `allowed_to_clip: true`.
- A person approves every clip before it is scheduled.

## Code map

```
clipper/
  __main__.py      CLI                          autopilot.py  background schedule for `run`
  config.py        paths, settings, YAML        db.py         SQLite (sources, moments, clips, posts, metrics)
  fetch.py         yt-dlp downloads             transcribe.py faster-whisper with word timestamps
  pipeline.py      the stages                   discover.py   campaigns and Twitch top clips
  brain/           moments.py, copywrite.py, signals.py
  render/          render.py, facetrack.py, captions.py, tighten.py
  publish/         platform publishers          web/          review app (FastAPI)
tests/             core tests
```
