"""
Gemini client for StockBridge (free tier friendly).

Settings (in the .env file next to app.py):

    GEMINI_API_KEY      your key from https://aistudio.google.com/apikey
    AI_MODEL            main model            (default: gemini-flash-latest)
    AI_FALLBACK_MODEL   backup model          (default: gemini-flash-lite-latest)
                        used automatically if the main model is rate-limited,
                        overloaded or unavailable. Set to "none" to disable.

The two "-latest" names are Google's own aliases: they always point to the
current Flash / Flash-Lite model, so the code keeps working when Google
retires older versions. Free-tier limits are counted per model, so the
backup model gives you a second allowance.
"""

import os
import time

import requests

try:
    from dotenv import load_dotenv

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except ImportError:
    pass


class LLMError(Exception):
    """The AI could not answer. fallback_ok says whether trying the
    backup model could help (it cannot help with a bad API key)."""

    def __init__(self, message, fallback_ok=False):
        super().__init__(message)
        self.fallback_ok = fallback_ok


class LLMNotConfigured(LLMError):
    pass


GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_FALLBACK = "gemini-flash-lite-latest"

# Thinking models spend part of the output budget on hidden reasoning.
# Without extra room a short answer can come back empty.
TOKEN_HEADROOM = 2048


def _clean(value):
    return (value or "").strip().strip("\"'").strip()


def _model_name(value):
    value = _clean(value)
    return value[7:] if value.startswith("models/") else value


def _key():
    return _clean(os.environ.get("GEMINI_API_KEY")) or _clean(
        os.environ.get("LLM_API_KEY")
    )


def config():
    """Main model settings, or None if there is no key."""

    key = _key()

    if not key or key.lower() in ("your-key-here", "your_key_here"):
        return None

    model = _model_name(
        os.environ.get("AI_MODEL") or os.environ.get("LLM_MODEL")
    ) or DEFAULT_MODEL

    return {"provider": "gemini", "key": key, "model": model}


def fallback_config():
    """Backup model (same key), or None."""

    main = config()

    if main is None:
        return None

    raw = os.environ.get("AI_FALLBACK_MODEL")
    model = _model_name(raw) if raw is not None else DEFAULT_FALLBACK

    if not model or model.lower() == "none" or model == main["model"]:
        return None

    return dict(main, model=model)


def problem():
    if config() is None:
        return ("No Gemini API key found. Run  python setup_ai.py  or add "
                "GEMINI_API_KEY to the .env file, then restart.")
    return None


def is_configured():
    return config() is not None


def _error_detail(response):
    try:
        data = response.json()
    except ValueError:
        return None

    if isinstance(data, list) and data:
        data = data[0]

    if not isinstance(data, dict):
        return None

    error = data.get("error", data)

    if isinstance(error, dict):
        error = error.get("message")

    return str(error)[:250] if error else None


def _thinking_config(model):
    """Ask the model to think as little as possible (faster, cheaper)."""

    name = model.lower()

    if "2.5" in name and "pro" not in name:
        return {"thinkingBudget": 0}

    if "pro" in name:
        return None

    return {"thinkingLevel": "low"}


def _post(cfg, payload):
    """POST with one retry on temporary server trouble (500/503)."""

    url = f"{GEMINI_API}/models/{cfg['model']}:generateContent"

    for attempt in range(2):

        try:
            response = requests.post(
                url,
                headers={"x-goog-api-key": cfg["key"],
                         "Content-Type": "application/json"},
                json=payload,
                timeout=60,
            )
        except requests.RequestException as error:
            raise LLMError(
                "Could not reach Gemini. Check your internet connection.",
                fallback_ok=True,
            ) from error

        if response.status_code in (500, 503) and attempt == 0:
            time.sleep(2)
            continue

        return response

    return response


def _call(cfg, system, messages, max_tokens, json_mode):
    """One request to one model. Raises LLMError."""

    contents = []

    for m in messages or []:

        text = str(m.get("content", ""))

        if text:
            contents.append({
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": text}],
            })

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    generation = {
        "maxOutputTokens": max_tokens + TOKEN_HEADROOM,
        "temperature": 0.2,
    }

    if json_mode:
        generation["responseMimeType"] = "application/json"

    thinking = _thinking_config(cfg["model"])

    if thinking:
        generation["thinkingConfig"] = thinking

    payload = {
        "systemInstruction": {"parts": [{"text": system or ""}]},
        "contents": contents,
        "generationConfig": generation,
    }

    response = _post(cfg, payload)

    # Some models reject the thinking setting: retry once without it.
    if response.status_code == 400 and thinking:

        detail = (_error_detail(response) or "").lower()

        if "thinking" in detail:
            generation.pop("thinkingConfig", None)
            response = _post(cfg, payload)

    if response.status_code != 200:
        _raise_for_status(cfg, response)

    try:
        data = response.json()
    except ValueError as error:
        raise LLMError("Gemini sent an unreadable reply.",
                       fallback_ok=True) from error

    candidates = data.get("candidates") or []

    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise LLMError(
            "Gemini gave no answer" + (f" (blocked: {reason})." if reason else "."),
            fallback_ok=True,
        )

    parts = (candidates[0].get("content") or {}).get("parts") or []

    text = "".join(
        p.get("text", "") for p in parts
        if isinstance(p, dict) and not p.get("thought")
    ).strip()

    if not text:
        raise LLMError(
            "Gemini returned an empty answer"
            f" ({candidates[0].get('finishReason', 'no reason given')}).",
            fallback_ok=True,
        )

    return text


def _raise_for_status(cfg, response):

    code = response.status_code
    detail = _error_detail(response)
    low = (detail or "").lower()

    if code in (401, 403) or (code == 400 and "api key" in low):
        raise LLMError(
            f"Gemini rejected your API key (HTTP {code}). Run  python "
            f"setup_ai.py  and paste the key again. Make sure it is still "
            f"active in Google AI Studio."
        )

    if code == 404:
        raise LLMError(
            f"Gemini does not know the model '{cfg['model']}'. Run  python "
            f"setup_ai.py  to pick one from your list.",
            fallback_ok=True,
        )

    if code == 429:
        raise LLMError(
            "Gemini's free limit was reached for this model. Wait a minute "
            "and try again.",
            fallback_ok=True,
        )

    if code in (500, 503):
        raise LLMError(
            "Gemini is busy right now. Please try again in a moment.",
            fallback_ok=True,
        )

    raise LLMError(
        f"Gemini returned an error (HTTP {code})"
        + (f": {detail}" if detail else "."),
        fallback_ok=(code != 400),
    )


def complete(system, messages, max_tokens=800, json_mode=False):
    """
    Send a system prompt and a list of {"role", "content"} messages.
    Tries the main model, then the backup model when that could help.
    Returns the reply text.
    """

    main = config()

    if main is None:
        raise LLMNotConfigured(problem() or "Gemini is not configured.")

    try:
        return _call(main, system, messages, max_tokens, json_mode)

    except LLMError as first:

        backup = fallback_config()

        if backup is None or not first.fallback_ok:
            raise

        try:
            return _call(backup, system, messages, max_tokens, json_mode)

        except LLMError as second:
            raise LLMError(
                f"{first}  Backup model also failed: {second}"
            ) from second
