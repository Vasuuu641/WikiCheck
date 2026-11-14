-- Map-maker and history owners fill these. Precomputed so the slider never runs heavy live queries.

-- 2D position per article, from a one-off UMAP over S's vectors (pipeline/make_map.py).
CREATE TABLE IF NOT EXISTS map_point (
  page_id INT UNSIGNED NOT NULL PRIMARY KEY,
  x FLOAT NOT NULL,
  y FLOAT NOT NULL
);

-- Edits and reverts per article per year, from M's metadata (a revert = same sha1 as an older,
-- non-adjacent revision).
CREATE TABLE IF NOT EXISTS activity_year (
  page_id INT UNSIGNED NOT NULL,
  year    SMALLINT NOT NULL,
  edits   INT UNSIGNED NOT NULL,
  reverts INT UNSIGNED NOT NULL,
  bytes_end INT UNSIGNED,
  PRIMARY KEY (page_id, year)
);

-- Cached then-vs-now summaries for showcase articles (generated offline, shown instantly).
CREATE TABLE IF NOT EXISTS change_summary (
  page_id   INT UNSIGNED NOT NULL,
  from_date DATE NOT NULL,
  to_date   DATE NOT NULL,
  summary   TEXT NOT NULL,
  diff_json JSON,
  PRIMARY KEY (page_id, from_date, to_date)
);
