# pipeline/ — owner: Backend 2 (offline jobs; the demo never depends on these running)

Planned scripts:
1. `make_map.py` — read S's vectors, run UMAP once, write x,y to `map_point`.
2. `select_showcase.py` — pick 10-20 showcase articles (long histories, well-known topics, ones we can verify).
3. `sample_revisions.py` — for each showcase article pick ~20-40 revisions across time (e.g. yearly + big byte changes);
   clean wikitext (`mwparserfromhell`).
4. `embed_*.py` — embed chunks/revisions. Local via Ollama bge-m3, or `modal_embed.py` on a GPU.
5. `summarise_changes.py` — LLM summary of then-vs-now with a strict citation prompt; store in `change_summary`.

Consistency rule: whatever model embeds the stored vectors must also embed the questions.
S's vectors came from bge-m3 via Ollama. If new tables are embedded with another build of the
model, embed their questions the same way.
