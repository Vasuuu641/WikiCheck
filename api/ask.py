"""Ask: question -> top passages from MariaDB -> Verda LLM -> answer with numbered citations."""
import os
import re
import time

from openai import OpenAI

from api.search import search

TOP_K = 8
CHARS_PER_SOURCE = 1500   # only the start of each article for now; chunks will improve this

PROMPT = """You answer questions about the history of Finland using ONLY the numbered sources below.
- Cite sources like [1] or [2] right after each claim.
- If the sources do not contain the answer, say you cannot find it in the sources.
- Answer in the same language as the question, in 2-4 sentences.

Sources:
{sources}

Question: {question}"""


def _client():
    return OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        timeout=60,
    )


def answer_with_citations(question: str, top_k: int = TOP_K):
    t0 = time.time()
    hits, sql = search(question, limit=top_k)

    sources = "\n\n".join(
        f"[{i + 1}] {h['title']}: {h['text'][:CHARS_PER_SOURCE]}" for i, h in enumerate(hits)
    )
    r = _client().chat.completions.create(
        model=os.environ["LLM_MODEL"],
        messages=[{"role": "user", "content": PROMPT.format(sources=sources, question=question)}],
        max_tokens=2048,
    )
    msg = r.choices[0].message
    # Kimi may be a reasoning model: the answer can come back in a different field.
    answer = msg.content or getattr(msg, "reasoning_content", "") or ""

    cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    return {
        "answer": answer,
        "citations": [
            {
                "n": i + 1,
                "page_id": h["page_id"],
                "rev_id": h["rev_id"],
                "title": h["title"],
                "url": h["url"],
                "passage": h["text"][:300],
                "cited": (i + 1) in cited,
            }
            for i, h in enumerate(hits)
        ],
        "sql": sql,
        "took_ms": int((time.time() - t0) * 1000),
    }