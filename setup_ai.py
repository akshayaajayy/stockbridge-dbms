"""
One-time AI setup for StockBridge (free Gemini key).

Run:   python setup_ai.py

It asks for your key, lists the models your key can use, tests the one you
pick, and writes the .env file next to this script.

Get a free key (no card needed): https://aistudio.google.com/apikey
"""

import os
import secrets
import sys

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
API = "https://generativelanguage.googleapis.com/v1beta"

MAIN_DEFAULT = "gemini-flash-latest"
BACKUP_DEFAULT = "gemini-flash-lite-latest"


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip().strip("\"'").strip()
    return value or (default or "")


def list_models(key):
    """Returns the models this key can use for text, newest names first."""

    response = requests.get(
        API + "/models?pageSize=200",
        headers={"x-goog-api-key": key},
        timeout=30,
    )

    if response.status_code in (400, 401, 403):
        raise PermissionError

    response.raise_for_status()

    names = sorted(
        m["name"][len("models/"):]
        for m in response.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
        and "flash" in m["name"]
        and not any(w in m["name"] for w in
                    ("image", "tts", "live", "audio", "embedding", "robotics"))
    )

    return names


def test_call(key, model):
    """Returns (ok, message)."""

    try:
        response = requests.post(
            f"{API}/models/{model}:generateContent",
            headers={"x-goog-api-key": key, "content-type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Reply OK"}]}],
                "generationConfig": {"maxOutputTokens": 200},
            },
            timeout=40,
        )
    except requests.RequestException:
        return False, "Could not reach the internet or Google."

    if response.status_code == 200:
        return True, "Key and model work."

    if response.status_code in (401, 403) or (
        response.status_code == 400 and "api key" in response.text.lower()
    ):
        return False, "Google rejected this key (wrong, expired or restricted)."

    if response.status_code == 404:
        return False, f"Google does not know the model '{model}'."

    if response.status_code == 429:
        return True, ("The free limit was hit just now. The key looks fine; "
                      "it will work again in a minute.")

    return False, f"Google returned HTTP {response.status_code}."


def pick_model(models, title, default):

    print(f"\n{title}")

    if models:
        for number, name in enumerate(models, 1):
            print(f"  {number}. {name}")
        print("(Tip: names ending in -latest always follow Google's newest "
              "Flash / Flash-Lite.)")

    choice = ask("Pick a number or type a model name", default)

    if choice.isdigit() and models and 1 <= int(choice) <= len(models):
        return models[int(choice) - 1]

    return choice


def main():

    print("\nStockBridge AI setup (free Gemini key)")
    print("Get a key at https://aistudio.google.com/apikey\n")

    key = ask("Paste your Gemini API key")

    if not key:
        print("\nNothing saved.")
        return 1

    try:
        models = list_models(key)
    except PermissionError:
        print("\nGoogle rejected this key. Check that you copied all of it.")
        return 1
    except requests.RequestException:
        models = []
        print("(Could not fetch the model list; you can type a model name.)")

    model = pick_model(models, "Main model", MAIN_DEFAULT)

    print(f"\nTesting {model} ...")
    ok, message = test_call(key, model)
    print(message)

    if not ok and ask("Use these settings anyway? (y/n)", "n").lower() != "y":
        return 1

    backup = pick_model(models, "Backup model (used if the main one is "
                        "busy or rate-limited; type none to skip)",
                        BACKUP_DEFAULT)

    secret = secrets.token_hex(24)

    # Keep an existing SECRET_KEY so users stay logged in.
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("SECRET_KEY=") and line.strip() != "SECRET_KEY=":
                    secret = line.split("=", 1)[1].strip()

    lines = [
        f"GEMINI_API_KEY={key}",
        f"AI_MODEL={model}",
        f"AI_FALLBACK_MODEL={backup or 'none'}",
        "LLM_SEND_ROWS=1",
        f"SECRET_KEY={secret}",
    ]

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\nSaved to {ENV_PATH}")
    print("Now start the app:  python app.py")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(1)
