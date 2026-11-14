# SVT track: system-versioned tables

Starter: https://github.com/MariaDB/mariadb-system-versioning-wikipedia (edit history, Finnish)

Needs MariaDB 10.11 or later for `system_versioning_insert_history`. Developed on 12.2.2, tested
in Docker with `mariadb:lts` (12.3.3).

## The idea

A table declared `WITH SYSTEM VERSIONING` keeps every earlier version of each row by itself. The
application writes one row per article; history rows appear without a revision table, triggers or
application code. `FOR SYSTEM_TIME AS OF '2015-06-01'` returns the table as it was then.

## What the starter is

A "time machine" Flask page for 50 articles (31,848 versions, 2003 to 2026): pick an article and
a date, read it as it stood, see a timeline of edits per month with reverts marked, and diff any
two moments. Server-rendered SVG, no JavaScript.

| File | Role |
|---|---|
| `schema.sql`, `create_tables.py` | Tables |
| `download.py` | Fetches the Wikimedia `pages-meta-history` dump files (3.4 GB for the 50, 4.5 GB for all) |
| `load.py` | Streams dumps through 7-Zip and writes versions with real timestamps |
| `asof.py` | CLI: an article as of a date, or all its versions (`--versions`, `--sql`) |
| `history.py` | Wikitext cleaner, revert detection, diff |
| `app.py` | The page, port 8000 |
| `measure.py` | Growth, partitions, size, "as of" speed (`--compare`) |
| `test_history.py` | 5 unit tests |
| `data/articles.tsv` | The 50 articles and why each was chosen |
| `data/articles-all.tsv` | All 19,754 |

Docker database: name, user and password all `wikihistory`, port 3308.

The starter loads from the raw dumps, not from the dataset M Parquet files. Loading M's Parquet
instead is described as "the same few lines" and marked to do. Loading all articles
(`load.py --list data/articles-all.tsv`) is also marked to do: only the 50 have been run.

## Schema

```sql
CREATE TABLE article (
  page_id     INT UNSIGNED NOT NULL,
  title       VARCHAR(255) NOT NULL,
  rev_id      INT UNSIGNED NOT NULL,
  user        VARCHAR(255),
  comment     TEXT,
  minor       BOOL NOT NULL DEFAULT 0,
  sha1        CHAR(31),
  len         INT UNSIGNED,
  text        MEDIUMTEXT COMPRESSED NOT NULL,
  row_start   TIMESTAMP(6) GENERATED ALWAYS AS ROW START INVISIBLE,
  row_end     TIMESTAMP(6) GENERATED ALWAYS AS ROW END INVISIBLE,
  PERIOD FOR SYSTEM_TIME (row_start, row_end),
  PRIMARY KEY (page_id),
  KEY (sha1),
  KEY start_key (page_id, row_start)
) CHARACTER SET utf8mb4
  WITH SYSTEM VERSIONING
  PARTITION BY SYSTEM_TIME INTERVAL 1 YEAR STARTS '2002-01-01 00:00:00' AUTO PARTITIONS 26;
```

- One row per article, not per version. Versions are history rows.
- `INVISIBLE` period columns stay out of `SELECT *`; name them to read them.
- `KEY (page_id, row_start)` is what makes old dates fast (see Performance).
- Partitions: a version is filed under the year it stopped being current (`row_end`). Current
  rows sit in the last partition, `pn`. `AUTO` adds partitions as years pass.
- `MEDIUMTEXT COMPRESSED` is per-value zlib. Page compression saved nothing on macOS; column
  compression behaves the same on every system.

## Loading history with real timestamps

System time normally means when the row changed in this database. Wikipedia's edits happened
years before the load. The starter considered three options:

| Option | How | Result |
|---|---|---|
| Replay | Ordinary `INSERT` then `UPDATE` per version | Every version gets today's time; "as of 2015" means nothing |
| True timestamps (chosen) | `system_versioning_insert_history`, writing `row_start` and `row_end` directly | `AS OF '2015-06-01'` returns what Wikipedia showed that day |
| Application time | A separate `PERIOD FOR valid_time` next to system time | Correct and also records load time, but every query must say which time it means |

```sql
SET time_zone = '+00:00';
SET system_versioning_insert_history = 1;
-- every earlier version, both ends given
INSERT INTO article (page_id, title, rev_id, ..., text, row_start, row_end) VALUES (...);
-- the current version, start only
INSERT INTO article (page_id, title, rev_id, ..., text, row_start) VALUES (...);
```

Details that bite:

- Works for an ordinary user with rights on the database, as long as `secure_timestamp` allows it
  (the default does).
- The loader sets both ends of each version: a version ends when the next starts.
- Two edits in the same second would give a zero-length version. The loader shifts the second by
  one microsecond.
- Wikipedia times are UTC and `TIMESTAMP` displays in the session zone, so every script sets
  `time_zone = '+00:00'`.
- A batch of long versions exceeds the default 16 MB `max_allowed_packet`. Batch by bytes (the
  starter uses 8 MB), not by row count.
- One transaction per article; already loaded articles are skipped, so the load is resumable.

## Time-travel queries

```sql
-- the article as it stood on 1 June 2015
SELECT title, rev_id, user, text
FROM article FOR SYSTEM_TIME AS OF TIMESTAMP '2015-06-01 00:00:00'
WHERE page_id = 1256;

-- every version, oldest first, with reverts flagged
SELECT rev_id, row_start, user, comment,
       sha1 <> LAG(sha1) OVER (ORDER BY row_start)
         AND MIN(row_start) OVER (PARTITION BY sha1) < row_start AS is_revert
FROM article FOR SYSTEM_TIME ALL
WHERE page_id = 1256
ORDER BY row_start;
```

- `AS OF` returns the version with `row_start <= moment < row_end`.
- `ALL` returns the current row and every history row.
- A revert is a version whose text differs from the previous one but equals an earlier one (same
  `sha1`).
- In a prepared statement `TIMESTAMP ?` needs a full date and time. A bare date is refused.
- System versioning also offers `FOR SYSTEM_TIME BETWEEN ... AND ...` and `FROM ... TO ...` for
  ranges. The starter does not use them.

## Reverts and edit wars

- Most reverts are cleanup. Talvisota has 328 reverts, 240 of them undoing an anonymous edit.
- The starter splits reverts in two: undoing an anonymous edit (vandalism cleanup) and named
  users undoing each other. Only the second kind counts toward an edit war, defined as a month
  with 3 or more such reverts.
- Anonymous means an IP address or, since 2025, a temporary account (`~2026-...`).
- Dataset M ships an `anonymous` column, so this split does not need to be re-derived from user
  names there.

## Performance

"As of" for 50 articles, median ms per query:

| Table | 2005 | 2010 | 2015 | 2020 | 2025 | now |
|---|---|---|---|---|---|---|
| versioned, partitioned, without the extra key | 0.65 | 0.52 | 0.37 | 0.24 | 0.19 | 0.14 |
| versioned, partitioned, with `KEY (page_id, row_start)` | 0.23 | 0.15 | 0.13 | 0.18 | 0.16 | 0.15 |
| versioned, not partitioned, with the key | 0.05 | 0.18 | 0.20 | 0.28 | 0.19 | 0.15 |
| plain `revision` table with `KEY (page_id, rev_time)` | 0.04 | 0.06 | 0.07 | 0.09 | 0.13 | 0.13 |

- MariaDB's own history index is ordered by `row_end`, so "as of 2005" walked every version that
  ended later. The extra key fixes that.
- System versioning is not faster than a plain revision table. Its case is simplicity and the
  impossibility of forgetting to record history.
- Yearly partitioning gave no speedup at this size and cost 25% more disk (1,196 MB against
  956 MB). "As of 2005" still reads 23 of 25 partitions; only recent dates read few. Its use is
  dropping or archiving old years.
- Whole page for Talvisota (2,143 versions, article plus full history): 42 ms.

## Size

- Every version is a full copy of the article, so the table grows with edits x article length.
- 50 articles: 2,049 MB of wikitext, 1,202 MB on disk with `COMPRESSED`.
- Suomen sisällissota alone has 3,357 versions.
- All of M (862,076 versions, 12.2 GB of text): estimated 5 to 7 GB on disk. Not yet loaded by
  anyone in the starter repos.
- Load time for the 50 from dumps: about 20 minutes, mostly 7-Zip and XML parsing. Loading from
  M's Parquet skips both.

## Limits

- No `CREATE TEMPORARY TABLE` with system versioning.
- Editing history goes against the grain: `DELETE HISTORY`, or rewriting through
  `system_versioning_insert_history`.
- `ALTER TABLE` on a versioned table needs `system_versioning_alter_history = KEEP`.
- Full copies get expensive at Wikipedia scale. MediaWiki itself uses an ordinary revision table
  with text stored separately.

## What the starter left undone

| Gap | Notes |
|---|---|
| All 19,742 articles | Only 50 loaded and measured |
| Loading from dataset M Parquet | Marked to do |
| Combination with RAG | Step 7, "later". Suggestion: one vector per year per article, not per version |
| Analytics across articles | The page is one article at a time. Nothing ranks articles by edit wars, churn or contested periods |
| Semantic change | Diffs are textual. Nothing says what a change means |
| Other languages | Finnish history only |
