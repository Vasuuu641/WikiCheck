-- Paragraph cache for /api/ask (api/ask.py, eval/build_chunks.py): Finnish paragraphs of the
-- articles an answer was written from, embedded with bge-m3 as "<title>: <paragraph>".
-- Reused while rev_id matches the article row. Finnish only, so page_id alone is the key.
--
-- Named ask_chunk, not chunk: `chunk` / `chunk_vector` were the old site's section tables
-- and hold section-level chunks keyed by (lang, page_id, chunk_no).

CREATE TABLE IF NOT EXISTS ask_chunk (
  page_id   INT UNSIGNED NOT NULL,
  chunk_no  SMALLINT UNSIGNED NOT NULL,
  rev_id    INT UNSIGNED NOT NULL,
  text      TEXT NOT NULL,
  PRIMARY KEY (page_id, chunk_no)
);

CREATE TABLE IF NOT EXISTS ask_chunk_vector (
  page_id   INT UNSIGNED NOT NULL,
  chunk_no  SMALLINT UNSIGNED NOT NULL,
  model     VARCHAR(50) NOT NULL,
  embedding VECTOR(1024) NOT NULL,
  PRIMARY KEY (page_id, chunk_no),
  VECTOR INDEX (embedding) M=16 DISTANCE=cosine
);
