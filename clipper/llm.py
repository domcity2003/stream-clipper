"""One door to every AI provider (via LiteLLM). Callers get validated Pydantic objects back.

If no key is configured, `available()` is False and callers use their heuristic fallback,
so the whole pipeline still runs for free with zero keys.
"""

import json
import logging
import os
import re
import time

from pydantic import BaseModel, ValidationError

from .config import LLM_FALLBACK_MODELS, LLM_MODEL

log = logging.getLogger(__name__)

PROVIDER_KEYS = {
    "anthropic/": "ANTHROPIC_API_KEY",
    "gemini/": "GEMINI_API_KEY",
    "groq/": "GROQ_API_KEY",
    "ollama/": None,  # local model, no key
}


def available() -> bool:
    for prefix, env in PROVIDER_KEYS.items():
        if LLM_MODEL.startswith(prefix):
            return env is None or bool(os.getenv(env))
    return bool(os.getenv("OPENAI_API_KEY"))


def _extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ```fences``` or prose; pull out the outermost object."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end != -1 else text


def _complete(messages: list[dict]):
    """Call the main model, retrying outages/rate limits with backoff, then try the backup models."""
    import litellm

    transient = (litellm.ServiceUnavailableError, litellm.RateLimitError, litellm.InternalServerError,
                 litellm.Timeout, litellm.APIConnectionError)
    models = [LLM_MODEL] + [m for m in LLM_FALLBACK_MODELS if m != LLM_MODEL]
    for i, model in enumerate(models):
        for attempt in range(3):
            try:
                return litellm.completion(model=model, messages=messages, temperature=0.7)
            except Exception as e:
                quota = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)  # sometimes surfaces as BadRequestError
                if not (isinstance(e, transient) or quota):
                    raise
                if quota and i < len(models) - 1:  # daily quota: retrying the same model won't help
                    log.warning("%s quota exhausted; trying %s", model, models[i + 1])
                    break
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                elif i == len(models) - 1:
                    raise
                else:
                    log.warning("%s unavailable (%s); trying %s", model, type(e).__name__, models[i + 1])


def ask_json[T: BaseModel](prompt: str, schema: type[T], system: str = "", retries: int = 2) -> T:
    import litellm

    litellm.suppress_debug_info = True
    messages = [{"role": "system", "content": system}] if system else []
    messages.append(
        {
            "role": "user",
            "content": f"{prompt}\n\nRespond with ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema.model_json_schema())}",
        }
    )
    last_error = None
    for attempt in range(retries + 1):
        resp = _complete(messages)
        text = resp.choices[0].message.content or ""
        try:
            return schema.model_validate_json(_extract_json(text))
        except ValidationError as e:
            last_error = e
            log.warning("LLM returned invalid JSON (attempt %d): %s", attempt + 1, e)
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"That JSON was invalid: {e}. Return corrected JSON only."},
            ]
    raise last_error
