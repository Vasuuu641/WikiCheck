# db/ — owner: Backend 1

- `001_schema_rag.sql` search tables (from MariaDB's RAG tutorial) + `chunk` tables
- `002_schema_history.sql` system-versioned article table (design unverified, see spike)
- `003_schema_map_activity.sql` map points, per-year activity, cached change summaries
- `004_schema_consistency.sql` extension backend: paragraph vectors, cached conflicts and gaps
- `004_history_index.sql`, `002`: `article_state`, the first history design. **Not used any more:**
  history lives in `article_version`, created and filled by `pacome/rag-demo/history.py`
- `005_conflict.sql` possible conflicts between Finnish paragraphs (`eval/scan_conflicts.py`)
- `006_ask_chunk.sql` paragraph cache for `/api/ask`
- `spike_vector_versioning.sql` run first: `make spike`

## Two things that changed in the merge

1. `chunk` / `chunk_vector` are the **site's** section tables (`lang, page_id, chunk_no, heading, text`),
   created by `pacome/rag-demo/chunks.py`. The `/api/ask` paragraph cache is `ask_chunk` /
   `ask_chunk_vector`. `001` was edited once to drop the old definitions (the only exception to the
   rule below). If your database still has the old paragraph tables under the old names:
   `RENAME TABLE chunk TO ask_chunk, chunk_vector TO ask_chunk_vector;`
2. One history table, `article_version` (columns `user, comment, len, text`; `text` is wikitext).
   The other site tables (`para*`, `article_link`, `mirror_point`, `pair_check`, `stance_cache`,
   `drift_claims`, `consistency_finding`) are created by the site's own code on first use.

On a machine without Docker, apply files with the client instead of `make db`:
`mariadb -uwikirag -pwikirag -h127.0.0.1 -P3306 wikirag < db/006_ask_chunk.sql`

## Loading data (not in git)

Datasets: https://github.com/MariaDB/mariadb-wikipedia-datasets (releases: S 156 MB, M 315 MB).
Put downloads in `data/`. RAG loader to copy: https://github.com/MariaDB/mariadb-vector-wikipedia-rag
(`load.py --vectors` loads S with ready-made vectors in about 3 minutes).

M is Finnish only: columns `page_id, title, rev_id, parent_id, timestamp, user, anonymous, comment,
minor, sha1, bytes, text`. Join to S on `page_id` (Finnish rows). Revert = same `sha1` as an older
non-adjacent revision.

Rule: schema changes are NEW numbered files. Never edit an applied one.
