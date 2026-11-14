"""OpenAI-compatible chat clients. Works with any provider that speaks the OpenAI API.

Two roles, each configured in .env (never commit a key):

  answer  (writes the answers)    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
          we use Verda's managed inference: LLM_BASE_URL=https://inference.verda.com/v1
  review  (neutrality review,     NEUTRALITY_BASE_URL, NEUTRALITY_API_KEY, NEUTRALITY_MODEL
           api/review.py)         e.g. Gemini's OpenAI-compatible endpoint:
                                  https://generativelanguage.googleapis.com/v1beta/openai/
          If any of the three is unset, review falls back to the answer settings.

  LLM_REASONING_EFFORT        none | low | medium | high (default none). Reasoning models such as
                              kimi-k3-1-pro otherwise "think" in hidden tokens before answering, which
                              costs latency. Set it empty to not send the option at all (for models
                              or providers that reject it).
  NEUTRALITY_REASONING_EFFORT the same for the review model; default empty (not sent). For a
                              Gemini Flash model "none" (or "low") keeps it fast.
  LLM_MAX_TOKENS              cap on the answer length (default 300)
A model or effort set in the process environment wins over .env, e.g. for a one-off comparison:
  $env:LLM_MODEL = "other-model"; .venv\\Scripts\\python ...
"""
import os
from functools import lru_cache

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()


def settings(role: str = "answer") -> dict:
    """base_url, api_key, model, effort, and which settings were used ("LLM" or "NEUTRALITY")."""
    if role == "review":
        base, key, model = (os.getenv(f"NEUTRALITY_{k}", "").strip() for k in ("BASE_URL", "API_KEY", "MODEL"))
        if base and key and model:
            return {"base_url": base, "api_key": key, "model": model, "source": "NEUTRALITY",
                    "effort": os.getenv("NEUTRALITY_REASONING_EFFORT", "").strip()}
    base, key, model = (os.getenv(f"LLM_{k}", "").strip() for k in ("BASE_URL", "API_KEY", "MODEL"))
    if not base or not key:
        raise RuntimeError("LLM_BASE_URL and LLM_API_KEY must be set in .env")
    if not model:
        raise RuntimeError("LLM_MODEL must be set in .env")
    return {"base_url": base, "api_key": key, "model": model, "source": "LLM",
            "effort": os.getenv("LLM_REASONING_EFFORT", "none").strip()}


@lru_cache(maxsize=4)
def _client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, timeout=120, max_retries=2)


def client(role: str = "answer") -> OpenAI:
    s = settings(role)
    return _client(s["base_url"], s["api_key"])


def model_name(role: str = "answer") -> str:
    return settings(role)["model"]


def chat(messages: list[dict], temperature: float = 0.0, max_tokens: int | None = None,
         role: str = "answer") -> str:
    """Send chat messages, return the assistant's visible text (never the reasoning)."""
    s = settings(role)
    kwargs = {"reasoning_effort": s["effort"]} if s["effort"] else {}
    resp = _client(s["base_url"], s["api_key"]).chat.completions.create(
        model=s["model"],
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens or int(os.getenv("LLM_MAX_TOKENS", "300")),
        **kwargs,
    )
    return (resp.choices[0].message.content or "").strip()
