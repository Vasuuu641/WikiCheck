# db/

Schema files, applied in order (`make db` with Docker, or the `mariadb` client as below):

- `001_schema_rag.sql` articles and their vectors (from MariaDB's RAG tutorial), categories
- `004_schema_consistency.sql` the extension's cross-language check: paragraph vectors, stored conflicts and gaps
- `006_ask_chunk.sql` paragraph cache for `/api/ask`

The drift tables (`article_version`, `drift_claims`, `stance_cache`, `pair_check`) are created by
`api/drift/` on first use. Numbers 002, 003 and 005 were early designs that nothing uses; they were
removed, and databases that still have their tables can keep or drop them.

## Two things that changed in the merge

1. The `/api/ask` paragraph cache is `ask_chunk` / `ask_chunk_vector` (`chunk` / `chunk_vector` in a
   database are the old site's section tables, no longer used). `001` was edited once to drop the old definitions (the only exception to the
   rule below). If your database still has the old paragraph tables under the old names:
   `RENAME TABLE chunk TO ask_chunk, chunk_vector TO ask_chunk_vector;`
2. One history table, `article_version` (columns `user, comment, len, text`; `text` is wikitext).
   Tables of the old site pages (`para*`, `article_link`, `mirror_point`, `consistency_finding`) are
   no longer used.

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
