# RAG demo

Ask a question in English, Finnish or Swedish. MariaDB finds the closest Wikipedia articles by
meaning, and an LLM writes a short answer that cites them. Built on the starter's data (dataset S,
29,460 articles with ready-made bge-m3 vectors) and its query shape; the answer step is ours.

```
question -> bge-m3 (Ollama) -> 1 SQL statement (MariaDB Vector) -> top 5 topics -> Gemini -> answer with [n]
```

- `rag.py`: retrieval and generation; also a CLI: `python rag.py "question" [--lang fi|sv|en]`
- `app.py`: web page on http://localhost:8000, shows answer, sources, timings and the SQL

## What it needs running

| Part | Where | Notes |
|---|---|---|
| MariaDB 11.7+ | system service, port 3306 | database and user `wikirag` / `wikirag`, created and loaded by `setup.sh` |
| Ollama + bge-m3 | `data/tools/ollama`, port 11434 | started by `run.sh` |
| Gemini API | `GOOGLE_API_KEY` in `../.env` | git-ignored. Default model `gemini-3.8-flash`; `gemini-2.5-flash` is closed to new keys |

Start everything with `scripts/run.sh` from the project root; first-time setup is `scripts/setup.sh`.
Full guide: [../docs/running.md](../docs/running.md).

## Choices

- Vector search sits in a subquery, then joins to `article`, so MariaDB uses the vector index
  (the starter measured 1 ms against 50 ms for a flat join).
- `mhnsw_ef_search = 80`, the starter's measured setting for the `M=16` index.
- 12 candidates are fetched; versions of one topic in several languages count once (`wikidata_id`),
  and the best 5 topics go to the LLM.
- Each article is cut to its first 12,000 characters before it goes to the LLM.
- The prompt allows only the numbered sources and tells the model to say when they do not answer.

## Known limits

- One vector per article finds the topic, not a fact deep inside it. The starter's test: "who was
  president 1956 to 1982" does not find Kekkonen in the top 10. Chunking is the fix, not built.
- The 12,000-character cut means a fact late in a long article never reaches the LLM.
- No hybrid (keyword + vector) search, no multi-turn chat.
- Answer time is dominated by the LLM: 5 to 9 s, against about 20 ms for embedding plus MariaDB.
- Not evaluated: the starter's `evaluate.py` measures retrieval only, nothing here measures whether
  the answers are correct.
