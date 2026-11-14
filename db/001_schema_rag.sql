-- Search layer (S dataset). Adapted from the schema in MariaDB's mariadb-vector-wikipedia-rag tutorial.
-- Vectors: BAAI/bge-m3, 1024 dims, cosine. M=16 chosen there after measuring recall.

CREATE TABLE IF NOT EXISTS article (
  lang          CHAR(2)      NOT NULL,
  page_id       INT UNSIGNED NOT NULL,
  rev_id        INT UNSIGNED NOT NULL,
  wikidata_id   VARCHAR(20),
  title         VARCHAR(255) NOT NULL,
  url           VARCHAR(500) NOT NULL,
  date_modified DATETIME,
  text          MEDIUMTEXT   NOT NULL,
  PRIMARY KEY (lang, page_id),
  KEY (wikidata_id),
  FULLTEXT (title, text)
);

CREATE TABLE IF NOT EXISTS article_vector (
  lang       CHAR(2)      NOT NULL,
  page_id    INT UNSIGNED NOT NULL,
  model      VARCHAR(50)  NOT NULL,
  truncated  BOOL         NOT NULL,
  embedding  VECTOR(1024) NOT NULL,
  PRIMARY KEY (lang, page_id),
  VECTOR INDEX (embedding) M=16 DISTANCE=cosine
);

CREATE TABLE IF NOT EXISTS category_link (
  parent VARCHAR(255) NOT NULL,
  child  VARCHAR(255) NOT NULL,
  PRIMARY KEY (parent, child),
  KEY (child)
);

CREATE TABLE IF NOT EXISTS article_category (
  lang     CHAR(2)      NOT NULL,
  page_id  INT UNSIGNED NOT NULL,
  category VARCHAR(255) NOT NULL,
  PRIMARY KEY (lang, page_id, category),
  KEY (category)
);

-- The paragraph cache for /api/ask used to be defined here as `chunk` / `chunk_vector`. It now
-- lives in db/006_ask_chunk.sql as `ask_chunk` / `ask_chunk_vector`, because the site in pacome/
-- owns `chunk` / `chunk_vector` with another shape (sections in fi/sv/en). See db/README.md.
