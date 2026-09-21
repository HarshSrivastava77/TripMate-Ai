"""
Central configuration for the project.

Loads environment variables once, sets up SSL certs once, and creates the
shared LLM client(s). Both backend.py and mcp_client.py import from here
instead of each repeating this setup - change the model in ONE place.

FALLBACK KEYS
-------------
Every external API used by this project (Groq, Tavily, AviationStack,
OpenWeather) supports an optional *_FALLBACK key in .env. If the primary
key is missing, invalid, or rate-limited, the code automatically retries
once with the fallback key before giving up. This is entirely optional -
leave a *_FALLBACK value blank and that service just behaves as before
(one key, no retry).

This is useful for students on free tiers: keep two free accounts for a
service (e.g. two separate Groq accounts) and put the second one in the
_FALLBACK slot, so hitting one account's rate limit doesn't stop the app.
"""

import os
from pathlib import Path

import certifi
from dotenv import load_dotenv
from langchain_groq import ChatGroq

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

os.environ["SSL_CERT_FILE"] = certifi.where()
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} is missing. Please add it to your .env file.")
    return value


# =========================
# API keys / connection strings
# =========================
GROQ_API_KEY = _require_env("GROQ_API_KEY")
GROQ_API_KEY_FALLBACK = os.getenv("GROQ_API_KEY_FALLBACK")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_API_KEY_FALLBACK = os.getenv("TAVILY_API_KEY_FALLBACK")

AVIATION_STACK_API_KEY = os.getenv("AVIATION_STACK_API_KEY") or os.getenv(
    "AVIATIONSTACK_API_KEY"
)
AVIATION_STACK_API_KEY_FALLBACK = os.getenv(
    "AVIATION_STACK_API_KEY_FALLBACK"
) or os.getenv("AVIATIONSTACK_API_KEY_FALLBACK")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENWEATHER_API_KEY_FALLBACK = os.getenv("OPENWEATHER_API_KEY_FALLBACK")


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError(
            "DATABASE_URL is missing. "
            "Please add your PostgreSQL connection string to .env"
        )

    if "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{separator}sslmode=require"

    return database_url


# =========================
# Shared LLM (with automatic fallback-key retry)
# =========================
# Single source of truth for the model name - change it here only.
MODEL_NAME = "openai/gpt-oss-120b"

# Caps how many tokens each response is allowed to use. Groq's free tier
# limits total tokens-per-minute (prompt + response combined), so keeping
# responses reasonably sized leaves more room for the prompt itself and
# helps avoid hitting that limit.
_MAX_TOKENS = 1200

_primary_llm = ChatGroq(
    model=MODEL_NAME,
    api_key=GROQ_API_KEY,
    max_tokens=_MAX_TOKENS,
)

_fallback_llm = (
    ChatGroq(
        model=MODEL_NAME,
        api_key=GROQ_API_KEY_FALLBACK,
        max_tokens=_MAX_TOKENS,
    )
    if GROQ_API_KEY_FALLBACK
    else None
)

# Kept for backwards compatibility with any code that wants the raw
# client. Prefer invoke_llm() below wherever possible - it adds the
# automatic fallback-key retry that calling llm.invoke() directly skips.
llm = _primary_llm


def invoke_llm(messages):
    """
    Call the LLM using GROQ_API_KEY. If that call fails for any reason
    (rate limit, invalid/expired key, etc.) and GROQ_API_KEY_FALLBACK is
    set in .env, automatically retry once with the fallback key before
    raising the error.
    """
    try:
        return _primary_llm.invoke(messages)
    except Exception as exc:
        if _fallback_llm is None:
            raise

        print(
            f"GROQ_API_KEY call failed ({type(exc).__name__}: {exc}). "
            "Retrying once with GROQ_API_KEY_FALLBACK..."
        )
        return _fallback_llm.invoke(messages)
