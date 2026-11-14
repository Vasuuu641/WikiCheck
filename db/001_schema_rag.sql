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

-- Chunks for precise questions (librarian owns; filled by pipeline/). Same shape as article_vector.
CREATE TABLE IF NOT EXISTS chunk (
  page_id   INT UNSIGNED NOT NULL,
  chunk_no  SMALLINT UNSIGNED NOT NULL,
  rev_id    INT UNSIGNED NOT NULL,
  text      TEXT NOT NULL,
  PRIMARY KEY (page_id, chunk_no)
);

CREATE TABLE IF NOT EXISTS chunk_vector (
  page_id   INT UNSIGNED NOT NULL,
  chunk_no  SMALLINT UNSIGNED NOT NULL,
  model     VARCHAR(50) NOT NULL,
  embedding VECTOR(1024) NOT NULL,
  PRIMARY KEY (page_id, chunk_no),
  VECTOR INDEX (embedding) M=16 DISTANCE=cosine
);
