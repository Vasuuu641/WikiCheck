-- Cross-language consistency cache & paragraph vectors.
-- Stores paragraph-level embeddings for semantic gap detection and precomputed
-- verified conflicts/gaps so the browser extension gets instant responses.

CREATE TABLE IF NOT EXISTS passage_vector (
  lang        CHAR(2) NOT NULL,
  page_id     INT UNSIGNED NOT NULL,
  para_idx    SMALLINT UNSIGNED NOT NULL,
  wikidata_id VARCHAR(20),
  text        TEXT NOT NULL,
  embedding   VECTOR(1024) NOT NULL,
  PRIMARY KEY (lang, page_id, para_idx),
  INDEX idx_pv_wikidata (wikidata_id),
  VECTOR INDEX idx_pv_embedding (embedding)
);

CREATE TABLE IF NOT EXISTS article_conflict (
  id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  wikidata_id     VARCHAR(20) NOT NULL,
  lang_a          CHAR(2) NOT NULL,
  page_id_a       INT UNSIGNED NOT NULL,
  title_a         VARCHAR(255) NOT NULL,
  url_a           VARCHAR(500) NOT NULL,
  lang_b          CHAR(2) NOT NULL,
  page_id_b       INT UNSIGNED NOT NULL,
  title_b         VARCHAR(255) NOT NULL,
  url_b           VARCHAR(500) NOT NULL,
  conflict_type   VARCHAR(40) NOT NULL,
  claim_a         TEXT NOT NULL,
  quote_a         TEXT NOT NULL,
  claim_b         TEXT NOT NULL,
  quote_b         TEXT NOT NULL,
  explanation     TEXT NOT NULL,
  confidence      FLOAT NOT NULL,
  verifier_reason TEXT,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_ac_a (lang_a, page_id_a),
  INDEX idx_ac_b (lang_b, page_id_b),
  INDEX idx_ac_wid (wikidata_id)
);

CREATE TABLE IF NOT EXISTS article_gap (
  id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  wikidata_id VARCHAR(20) NOT NULL,
  src_lang    CHAR(2) NOT NULL,
  src_page_id INT UNSIGNED NOT NULL,
  src_title   VARCHAR(255) NOT NULL,
  src_url     VARCHAR(500) NOT NULL,
  dst_lang    CHAR(2) NOT NULL,
  dst_page_id INT UNSIGNED NOT NULL,
  dst_title   VARCHAR(255) NOT NULL,
  dst_url     VARCHAR(500) NOT NULL,
  importance  VARCHAR(16) NOT NULL,
  sim         FLOAT NOT NULL,
  summary     TEXT NOT NULL,
  paragraph   TEXT NOT NULL,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_ag_dst (dst_lang, dst_page_id),
  INDEX idx_ag_wid (wikidata_id)
);

CREATE TABLE IF NOT EXISTS consistency_status (
  wikidata_id VARCHAR(20) PRIMARY KEY,
  analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
