"""Translate a source passage for the reader (the passages are mostly Finnish). Used by the test page.

Same LLM as /api/ask (api/llm.py). Translation only: no answering, no added facts.
"""
LANGUAGES = {"en": "English", "fi": "Finnish", "sv": "Swedish"}
MAX_CHARS = 4000

PROMPT = ("Translate the user's text from Wikipedia into {lang}. Output only the translation: "
          "no notes, no quotes, nothing added or left out. Keep names, dates and numbers exactly.")


def translate(text: str, target: str = "en") -> str:
    from .llm import chat  # lazily, so MOCK=1 needs no LLM settings

    if target not in LANGUAGES:
        raise ValueError(f"target must be one of {', '.join(LANGUAGES)}")
    text = text.strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > MAX_CHARS:
        raise ValueError(f"text is longer than {MAX_CHARS} characters")
    return chat([{"role": "system", "content": PROMPT.format(lang=LANGUAGES[target])},
                 {"role": "user", "content": text}], max_tokens=1500)
