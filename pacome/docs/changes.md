# What was changed (2026-11-14)

## Demo page (`rag-demo/app.py`, `rag-demo/rag.py`)
- **Live progress:** `/stream` sends server-sent events (embedded, sources, token, done, problem). The page shows a checklist with timings, then the sources and SQL, then the answer word by word.
- **Markdown:** the prompt asks Gemini for plain short paragraphs (bold and `- ` lists only). The page renders bold, italic, code, lists and `#` headings safely (HTML escaped first). `[n]` citations link to the source list.
- **Faster Gemini:** `thinkingLevel` is `low` (`minimal` is rejected by the model; override with `GEMINI_THINKING`), `MAX_CHARS` 12000 to 6000, `maxOutputTokens` 600. Bge-m3 is warmed at startup and kept loaded 2 h. Measured: first token about 3 s, total about 4 s (was 7-11 s).
- **Layout:** wide two-column page (answer left, sticky sources panel right), one column under 900 px.
- **Look:** Wikipedia-style header, serif headings, blue links, infobox-style sources panel, dark mode. Drawn logo, not the real one.

## Verda GPU embeddings (`scripts/verda.py`)
- `up` creates a 1xA100 80GB on-demand instance (FIN-01, image `24.04.cuda12.9`), installs Ollama, pulls bge-m3. `tunnel` opens `localhost:11435` to it over SSH. `status` and `down` (deletes instance and disk, stops billing).
- Dedicated key `~/.ssh/verda_ed25519`, uploaded as `wikirag`. State in `data/verda.json`.
- `OLLAMA_URL=http://localhost:11435` in `.env` points the app at it. `embed()` falls back to local Ollama if the tunnel is down.
- `scripts/ollama.sh` no longer overrides `OLLAMA_URL`; it uses its own `LOCAL_OLLAMA_URL`.
- Checked: GPU and local embeddings match (cosine 1.00000, 1024 dims). Latency is about 240-360 ms through the tunnel vs about 10 ms locally.

## State and open items
- The instance is running and billing about 2.39 EUR/h. Stop it with `scripts/verda.py down`.
- The balance is a hackathon coupon; Verda required a payment method before allowing compute.
- Not done: a live inference status page.

## Opinion drift improvements (`rag-demo/drift.py`)
- **Stream:** the engine runs in a thread; the SSE stream sends a keep-alive comment every 10 s (Gemini calls and history loads used to leave it silent) and stops the engine at the next event if the reader leaves. A question over 300 characters is refused with a clear message.
- **Evidence:** `/api/drift/evidence` is cached in memory (each lookup costs several Decisions requests) and rejects a missing claim. The old fixture-only claim lookup is gone.
- **Page:** example questions and a demo link under the search box; `?fixture=1&fast=1` replays the demo without pauses; the time axis follows the articles' histories (it was fixed at 2002 to 2026) with year steps that adapt; lane numbers 1 to 5 in every row match the source list; a filter "only claims with a change point"; change-point titles sorted by date and prefixed with the article, the selected one is highlighted on the ribbons; a message when no change point exists; "Download results (JSON)" and "Copy link"; change-point dots are keyboard-focusable (Tab, Enter) and carry aria-labels; the ribbons scroll sideways on a phone instead of shrinking to unreadable text.
- **Checked:** the page script was run in QuickJS against a DOM stub and the 18 events of `drift_fixture.json` (12 change points, 70 lane numbers, no exception); no real browser was available, so the visual result has not been looked at.
- **AI summary:** at the end of a run `stance.summarize` makes one Gemini call (current article openings, each claim's current position, the change points) and the page shows two short paragraphs above the change list: ANSWER (what the current articles say, with [n] citations, or that they do not address the question) and DRIFT (whether the position moved, the biggest change with date and article, or that none was found). Event `summary` comes before `done`; if Gemini fails the page is unaffected. The demo replay has no summary.
- **Empty versions:** `decisions.probabilities` scores a version with no readable text as "not addressed" instead of sending empty input (the API answers 400).
- **Relevance filter (two steps):** `stance.pick_articles` (1) stops with a clear message when the best article is farther than 0.48 (cosine) from the question and drops articles farther than the best plus 0.07; (2) asks a yes/no check per remaining candidate (at most 10, cached in `pair_check`): "is this article about the subject the question asks about, a sensible place to look for the answer?", keeping those at 0.5 or above. If none passes, the page says so and lists the scores. Checked on seven questions: an off-topic one (1998 football world cup) and a mixed one (what the Finns thought of Napoleon and the 1763 treaty) are refused; on-topic questions keep 1 to 5 articles with scores 0.57 to 0.99 (the French question kept "Ranskan ja Suomen suhteet" at 0.94 after the first wording of the check had scored it 0.26 and was loosened). End-to-end run on the French question: 5 sources, 24 change points, summary, 16 s. Each source shows its distance and the number left out.
- **Vector search flicker:** the same query returned different article sets on different connections, and the nearest article (0.448) was sometimes missing (best 0.488). Cause: `mhnsw_max_cache_size` is 16 MB, much smaller than the vector indexes now in the database, so each connection reads a partly different graph. Measured recall@10 against an exact scan: 0.94 to 0.96 on average, minimum 0.4, over 40 article queries. `stance.retrieve_stable` merges three searches and, only when the page is about to refuse a question, confirms with an exact scan (about 3 s). The real fix is a bigger cache (`SET GLOBAL mhnsw_max_cache_size = ...` or the server config); this was not changed because it is a server setting.
