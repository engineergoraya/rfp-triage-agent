"""The only module that talks to Gemini.

One public function, `generate_json`, which:
  1. sends a prompt with a Pydantic schema as `response_schema` (Gemini then must answer in that JSON shape),
  2. retries with backoff on temporary errors (503 busy, per-minute 429), then moves to the next model,
  3. validates the JSON with Pydantic and retries once if it is invalid,
  4. turns every failure into an LLMError with a message fit to show in the UI.
"""

import os
import time

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

# Free-tier models checked against ai.google.dev and our own key (Nov 2026). Override with env vars.
# - gemini-3.6-flash gave the most complete extraction in our tests.
# - The free tier allows only 20 requests per day PER MODEL, and one triage uses 3-4 requests,
#   so the fallback is a short list: each extra model adds its own daily quota.
DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_FALLBACK_MODELS = "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.8-flash"

# "minimal" thinking was several times faster than the default on our samples with no loss of
# recall (the task is careful reading, not multi-step reasoning). Override with GEMINI_THINKING_LEVEL.
DEFAULT_THINKING_LEVEL = "minimal"

BACKOFF_SECONDS = [2, 5]  # waits between retries on temporary errors, per model


class LLMError(Exception):
    """A model failure. The message is shown to the user as-is."""


def models_to_try() -> list[str]:
    """Primary model first, then the comma-separated fallbacks, without duplicates."""
    primary = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODELS
    models = [primary] + [m.strip() for m in fallbacks.split(",") if m.strip()]
    return list(dict.fromkeys(models))


def _call(client: genai.Client, model: str, prompt: str, schema: type[BaseModel], system: str,
          set_thinking: bool = True) -> str:
    """One raw API call. Returns the response text (which should be JSON)."""
    thinking = types.ThinkingConfig(thinking_level=os.environ.get("GEMINI_THINKING_LEVEL") or DEFAULT_THINKING_LEVEL)
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0,  # same document in, same report out (as far as the model allows)
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=thinking if set_thinking else None,  # None = the model's own default
    )
    response = client.models.generate_content(model=model, contents=prompt, config=config)
    return response.text or ""


def _is_too_long(exc: errors.APIError) -> bool:
    message = str(exc).lower()
    return exc.code == 400 and "token" in message and any(w in message for w in ("exceed", "limit", "long"))


def _is_daily_quota(exc: errors.APIError) -> bool:
    # Retrying cannot help until the quota resets at midnight Pacific, so skip to the next model.
    return exc.code == 429 and "perday" in str(exc).lower()


def generate_json(api_key: str, prompt: str, schema: type[BaseModel], system: str):
    """Return (validated schema instance, model name used)."""
    client = genai.Client(api_key=api_key)
    errors_seen = []  # one line per model, so the final message explains every failure

    for model in models_to_try():
        json_retry_used = False
        set_thinking = True
        attempt = 0
        while True:
            try:
                text = _call(client, model, prompt, schema, system, set_thinking)
                return schema.model_validate_json(text), model
            except ValidationError as exc:
                # The model returned malformed or incomplete JSON. Retry once on the same model.
                if json_retry_used:
                    errors_seen.append(f"{model}: invalid JSON twice ({exc.errors()[0]['msg']})")
                    break  # give up on this model, move to the next
                json_retry_used = True
            except errors.APIError as exc:
                if _is_too_long(exc):
                    raise LLMError("The document is too long for the model's context window.") from exc
                if exc.code in (401, 403) or (exc.code == 400 and "api key" in str(exc).lower()):
                    raise LLMError("The Gemini API key is missing or invalid. Check GEMINI_API_KEY.") from exc
                if exc.code == 400 and "thinking" in str(exc).lower() and set_thinking:
                    set_thinking = False  # some models reject our thinking level: retry with their default
                    continue
                if exc.code == 404:
                    errors_seen.append(f"{model}: model not found")
                    break
                if _is_daily_quota(exc):
                    errors_seen.append(f"{model}: daily free quota used up")
                    break
                if exc.code == 429 or (exc.code or 0) >= 500:
                    if attempt >= len(BACKOFF_SECONDS):
                        errors_seen.append(f"{model}: busy or rate-limited (HTTP {exc.code})")
                        break
                    time.sleep(BACKOFF_SECONDS[attempt])
                    attempt += 1
                    continue
                # Any other error (e.g. a model that rejects a setting): note it and try the next model.
                errors_seen.append(f"{model}: HTTP {exc.code} {str(exc)[:120]}")
                break

    hint = ("The free tier allows 20 requests per model per day (reset at midnight Pacific). "
            if any("daily" in e for e in errors_seen) else
            "Google's free tier is busy; please try again in a minute. ")
    raise LLMError(f"The model could not produce a report ({'; '.join(errors_seen)}). {hint}"
                   "Results for PDFs already analysed are cached and still available.")
