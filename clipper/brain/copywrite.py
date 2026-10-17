"""Titles, on-screen hook, description and hashtags for a clip.

LLM path writes 3 title variants in different styles (see docs/VIRALITY_PLAYBOOK.md).
Free fallback builds them from templates + the most quotable line in the clip.
"""

import logging
import random
import re

from pydantic import BaseModel, Field

from .. import llm
from ..config import hashtags as hashtag_config
from .signals import clean_line, text_features

log = logging.getLogger(__name__)


class CopyOut(BaseModel):
    hook_text: str = Field(description="on-screen text for the first 3 seconds, max 8 words, no hashtags")
    titles: list[str] = Field(description="exactly 3 titles, max 70 chars, styles: curiosity gap / direct quote / stakes-reaction")
    description: str = Field(description="1-2 sentence caption that adds context and invites comments")
    hashtags: list[str] = Field(description="5-8 hashtags starting with #, mix of broad, niche and creator-specific")


SYSTEM = "You write short-form video copy that gets clicks without lying. Curiosity + accuracy."

PROMPT = """Write copy for a vertical short clipped from "{title}" by {creator}.
Moment type: {moment_type}. Why it's good: {reason}
Clip transcript:
\"\"\"{text}\"\"\"
{rules}
Guidelines:
- hook_text: the reason to keep watching, ≤8 words, punchy, can tease the payoff but must be TRUE to the clip.
- titles: 3 variants — (1) curiosity gap, (2) the best direct quote in quotes, (3) stakes/reaction. ≤70 chars, 0-1 emoji.
- description: 1-2 sentences, ends with a question to drive comments.
- hashtags: 5-8 total. Use these where relevant: {tag_hint}. Never use: {blocklist}.
- WHO IS SPEAKING: {creator} runs the show, but the speaker may be a guest. Only name a person as the speaker if
  the transcript or campaign notes make it unambiguous (addressed by name, describing their own known career).
  Otherwise write around it ("this QB", "he", the show name). A wrong name is worse than no name.
- Never mention gambling or betting."""


def _niche_tags(niche: str, creator: str) -> tuple[list[str], list[str]]:
    cfg = hashtag_config()
    bank = cfg.get("niches", {}).get(niche) or cfg.get("niches", {}).get("default", {})
    creator_tags = []
    for name, tags in (cfg.get("creators") or {}).items():
        if name.lower() in (creator or "").lower():
            creator_tags += tags
    return bank.get("broad", [])[:2] + bank.get("niche", [])[:3] + creator_tags, cfg.get("blocklist", [])


def _clean_tags(tags: list[str], required: list[str], blocklist: list[str]) -> list[str]:
    out, seen = [], set()
    for t in [*required, *tags]:
        t = "#" + re.sub(r"[^\w]", "", t.lstrip("#"))
        if len(t) > 1 and t.lower() not in seen and t.lower() not in {b.lower() for b in blocklist}:
            seen.add(t.lower())
            out.append(t)
    return out[:10]


def _best_quote(text: str) -> str:
    sentences = [clean_line(s) for s in re.split(r"(?<=[.?!])\s+", text)]
    sentences = [s for s in sentences if 3 <= len(s.split()) <= 14]
    if not sentences:
        return " ".join(text.split()[:10])
    return max(sentences, key=lambda s: sum(v for k, v in text_features(s).items() if k != "you"))


def fallback(text: str, creator: str, hook_line: str, tags: list[str]) -> CopyOut:
    quote = _best_quote(text).rstrip(".")
    who = creator or "He"
    # The most quotable line beats the first line (which is often "Um, so…")
    hook_src = quote if len(quote.split()) <= 9 else clean_line(hook_line)
    hook = " ".join(hook_src.split()[:8]).rstrip(",.")
    titles = [
        random.choice([f"{who} didn't expect this…", f"Wait for {who}'s answer 😳", f"Nobody talks about this"]),
        f"\"{quote[:64]}\"",
        random.choice([f"{who} was dead serious 💀", f"This changed how I see {who}", f"The room went silent"]),
    ]
    return CopyOut(hook_text=hook, titles=titles,
                   description=f"{quote}. Would you have said the same? 👇", hashtags=tags)


def write_copy(text: str, title: str, creator: str, moment_type: str, reason: str, hook_line: str,
               niche: str = "podcast_entertainment", campaign: dict | None = None) -> CopyOut:
    tags, blocklist = _niche_tags(niche, creator)
    rules = campaign.get("rules", {}) if campaign else {}
    required = list(rules.get("required_hashtags", []))
    rule_text = ""
    if campaign:
        rule_text = (f"Campaign rules (MUST follow): required tags/mentions {rules.get('required_tags', [])}, "
                     f"required hashtags {required}. Notes: {rules.get('notes', '')}")
    # If the clip never names the creator, don't hand the model a name to pin on whoever is talking
    # (guests are often the speaker; a wrong name is worse than none).
    surname = (creator or "").split()[-1].lower() if creator else ""
    named = bool(surname) and surname in f"{text} {title}".lower()
    if llm.available():
        try:
            out = llm.ask_json(
                PROMPT.format(title=title, creator=(creator if named else "the show's host (not necessarily the speaker)"), moment_type=moment_type, reason=reason,
                              text=text[:4000], rules=rule_text, tag_hint=", ".join(tags), blocklist=", ".join(blocklist)),
                CopyOut, SYSTEM,
            )
            out.hashtags = _clean_tags(out.hashtags, required, blocklist)
            out.titles = (out.titles + out.titles[:1] * 3)[:3]
            return out
        except Exception as e:
            log.warning("LLM copywriting failed (%s); using templates", e)
    out = fallback(text, creator, hook_line, tags)
    out.hashtags = _clean_tags(out.hashtags, required, blocklist)
    return out


def format_for(platform: str, title: str, description: str, hashtags: list[str], creator: str,
               source_url: str = "", campaign: dict | None = None) -> dict:
    """Platform-specific title/caption. Always credits the creator."""
    mentions = " ".join((campaign or {}).get("rules", {}).get("required_tags", []))
    credit = f"🎥 {creator}" if creator else ""
    tags = " ".join(hashtags)
    if platform == "youtube":
        yt_title = title if "#shorts" in title.lower() else f"{title} #shorts"
        body = "\n".join(x for x in [description, credit, source_url and f"Full video: {source_url}", mentions, tags] if x)
        return {"title": yt_title[:100], "description": body[:4900], "tags": [h.lstrip("#") for h in hashtags]}
    caption = " ".join(x for x in [title, "—", description, credit, mentions, tags] if x)
    limit = 2200 if platform in ("tiktok", "instagram") else 5000
    return {"title": title, "caption": caption[:limit]}
