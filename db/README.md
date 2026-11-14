# db/ — owner: Backend 1

- `001_schema_rag.sql` search tables (from MariaDB's RAG tutorial) + `chunk` tables
- `002_schema_history.sql` system-versioned article table (design unverified, see spike)
- `003_schema_map_activity.sql` map points, per-year activity, cached change summaries
- `spike_vector_versioning.sql` run first: `make spike`

## Loading data (not in git)

Datasets: https://github.com/MariaDB/mariadb-wikipedia-datasets (releases: S 156 MB, M 315 MB).
Put downloads in `data/`. RAG loader to copy: https://github.com/MariaDB/mariadb-vector-wikipedia-rag
(`load.py --vectors` loads S with ready-made vectors in about 3 minutes).

M is Finnish only: columns `page_id, title, rev_id, parent_id, timestamp, user, anonymous, comment,
minor, sha1, bytes, text`. Join to S on `page_id` (Finnish rows). Revert = same `sha1` as an older
non-adjacent revision.

Rule: schema changes are NEW numbered files. Never edit an applied one.
