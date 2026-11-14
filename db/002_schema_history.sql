-- History layer (M dataset). UNVERIFIED DESIGN: confirm with the spike and MariaDB's
-- mariadb-system-versioning-wikipedia demo before loading real data.
--
-- Idea: one row per article = its CURRENT state. Every historical revision becomes a past row
-- version whose [row_start, row_end) is the time that revision was live on Wikipedia.
-- Historical rows are written with real edit times using @@system_versioning_insert_history=1.
-- TIMESTAMP ends at 2038-01-19 03:14:07 UTC, which is also the row_end of a "current" row.
--
-- Metadata for ALL ~862k revisions is small. Load full wikitext only for showcase articles
-- (full M text is ~12 GB raw; plan 5-7 GB in MariaDB).

CREATE TABLE IF NOT EXISTS article_state (
  page_id    INT UNSIGNED NOT NULL,
  rev_id     INT UNSIGNED NOT NULL,
  title      VARCHAR(255) NOT NULL,
  editor     VARCHAR(255),
  anonymous  BOOL NOT NULL DEFAULT 0,
  edit_comment TEXT,
  minor      BOOL NOT NULL DEFAULT 0,
  sha1       CHAR(40),
  bytes      INT UNSIGNED,
  wikitext   MEDIUMTEXT NULL,            -- only for showcase articles
  row_start  TIMESTAMP(6) GENERATED ALWAYS AS ROW START,
  row_end    TIMESTAMP(6) GENERATED ALWAYS AS ROW END,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id)
) WITH SYSTEM VERSIONING;

-- Example (after loading):  how many articles existed on a date?
--   SELECT COUNT(*) FROM article_state FOR SYSTEM_TIME AS OF '2010-01-01 00:00:00';
