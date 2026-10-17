"""`python -m clipper check` — is everything set up? Never prints secret values."""

import os
import shutil
import subprocess
import sys

import httpx

from . import llm
from .config import LLM_MODEL, ROOT, TOKENS, accounts


def run() -> bool:
    ok = True

    def report(name, passed, detail="", required=True):
        nonlocal ok
        ok &= passed or not required
        mark = "✅" if passed else ("❌" if required else "⚪️")
        print(f"{mark} {name}{' — ' + detail if detail else ''}")

    print("Core (needed for clipping)")
    report("FFmpeg", shutil.which("ffmpeg") is not None)
    yt = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True)
    report("yt-dlp", yt.returncode == 0, yt.stdout.strip())

    print("\nOptional (the system runs without these, using free fallbacks)")
    if llm.available():
        try:
            import litellm

            r = litellm.completion(model=LLM_MODEL, messages=[{"role": "user", "content": "Reply with just: ready"}])
            report("AI model", True, f"{LLM_MODEL} says: {(r.choices[0].message.content or '').strip()}")
        except Exception as e:
            report("AI model", False, f"{LLM_MODEL}: {type(e).__name__} — check the key in .env")
    else:
        report("AI model", False, f"no key for {LLM_MODEL} → using the free heuristic picker + title templates", required=False)

    cid, secret = os.getenv("TWITCH_CLIENT_ID"), os.getenv("TWITCH_CLIENT_SECRET")
    if cid and secret:
        r = httpx.post("https://id.twitch.tv/oauth2/token",
                       data={"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
        report("Twitch API", r.status_code == 200, "" if r.status_code == 200 else f"token request failed ({r.status_code})")
    else:
        report("Twitch API", False, "TWITCH_CLIENT_SECRET not in .env → Twitch discovery off", required=False)

    print("\nPublishing (not connected = clips become ready-to-post folders in data/export/)")
    from .publish.base import get

    for acc in accounts():
        report(f"{acc['platform']} · {acc['id']}", get(acc["platform"]).ready(acc),
               "connected" if get(acc["platform"]).ready(acc) else "manual export mode", required=False)

    print("\n" + ("All required pieces work 🎉" if ok else "Fix the ❌ items above, then run again."))
    return ok
