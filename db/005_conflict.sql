-- Possible factual conflicts between two Finnish paragraphs (ask_chunk rows) of DIFFERENT articles,
-- found offline by eval/scan_conflicts.py and served to editors by GET /api/conflicts.
--
-- One row per pair. The pair is stored in a fixed order, side a = the lower page_id (the CHECK),
-- so (a, b) and (b, a) are the same row and the UNIQUE key makes reruns skip it.
-- phrase_a / phrase_b are exact substrings of ask_chunk.text at the time of the scan; a later
-- rebuild of an article's chunks can renumber them, so match on the phrase, not only chunk_no.
-- status: open (default) | fixed | dismissed, set by whoever handles the row.

CREATE TABLE IF NOT EXISTS possible_conflict (
  id            INT UNSIGNED      NOT NULL AUTO_INCREMENT PRIMARY KEY,
  about         VARCHAR(500),
  kind          ENUM('contradiction', 'sources may differ') NOT NULL,
  page_id_a     INT UNSIGNED      NOT NULL,
  chunk_no_a    SMALLINT UNSIGNED NOT NULL,
  phrase_a      TEXT              NOT NULL,
  page_id_b     INT UNSIGNED      NOT NULL,
  chunk_no_b    SMALLINT UNSIGNED NOT NULL,
  phrase_b      TEXT              NOT NULL,
  distance      FLOAT             NOT NULL,      -- cosine distance of the two chunk vectors
  seed_category VARCHAR(255),                    -- the --category the seed paragraph came from
  model         VARCHAR(100)      NOT NULL,      -- the review LLM that confirmed it
  created_at    TIMESTAMP         NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status        VARCHAR(20)       NOT NULL DEFAULT 'open',
  UNIQUE KEY pair (page_id_a, chunk_no_a, page_id_b, chunk_no_b),
  KEY by_category (seed_category, created_at),
  KEY by_created (created_at),
  CONSTRAINT pair_order CHECK (page_id_a < page_id_b)
);
