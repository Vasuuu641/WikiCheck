# Data state (2026-11-14)

Snapshot of what exists where. Counts were measured, not read from logs.

## Wikipedia history (files on disk)

Source: `enwiki-20261001` full-history dump, fetched by `scripts/enwiki/fetch_history.py`.

| Where | What | Count |
|---|---|---|
| `data/enwiki/versions/*.parquet` | converted history chunks (1, 9, 20, 22) | **4,150 articles**, 465,456 revisions |
| `data/enwiki/raw/*.7z` | downloaded chunks (about 3.4 GB) | not converted yet, article count unknown |
| `data/enwiki/sample_pages.parquet` | ranked page list (`page_id, rank, title, len`) | 1,000,000 rows, no article content |

Columns in `versions/`: `page_id, title, rev_id, parent_id, timestamp, user, anonymous, ...`

The download was not running at the time of the snapshot. `fetch.log` ended with a leaked-semaphore warning, so the
workers were probably killed. `raw/.dl0.lock` and `raw/.dl1.lock` may be stale.

## MariaDB (system service, `127.0.0.1:3306`)

Connection (from `docs/running.md` and `scripts/setup.sh`): user `wikirag`, password `wikirag`, database `wikirag`.

```
mariadb -uwikirag -pwikirag -h127.0.0.1 -P3306 wikirag
```

| Table | Rows (exact) |
|---|---|
| `article` | **29,460** |
| `article_vector` | 29,460 |
| `mirror_point` | 29,460 |
| `article_category` | 39,037 |
| `category_link` | 1,760 |
| `spike_vec` / `spike_hist` | 3 / 2 (spike tests) |

- `article` columns: `lang, page_id, rev_id, wikidata_id, title, url, date_modified, text`.
- Tables from `db/001`, `db/002` and `db/003` are only partly applied. `chunk` and `article_state` do not exist.
- The MariaDB articles did not come from the history download. Only **2 of the 4,150** history articles are also in
  `article`. They are two separate sets.

## Not related to this repo

An ollama and llama-server from `small-projects/Junction/mariadb-wikipedia` was running and was stopped on 2026-11-14.
`scripts/ollama.sh` starts the project's own copy when needed.
