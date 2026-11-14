import os
from openai import OpenAI

PROMPT = """You help a search engine over Finnish-history Wikipedia articles.
Reply with ONE short line: the names, events or terms of the Wikipedia article that would answer
the question (e.g. a person's full name, or the name of the war or treaty). No explanation.

Question: {q}
Line:"""


def rewrite(question: str) -> str:
    """Question + the model's guess at the answer's article name. Falls back to the question."""
    try:
        r = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"],
                   timeout=60).chat.completions.create(
            model=os.environ["LLM_MODEL"],
            messages=[{"role": "user", "content": PROMPT.format(q=question)}],
            max_tokens=1024)  # large because the model may spend tokens reasoning first
        lines = (r.choices[0].message.content or "").strip().splitlines()
    except Exception:
        return question
    return f"{question}\n{lines[0][:200]}" if lines else question