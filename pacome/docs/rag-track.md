# RAG track: MariaDB Vector

Starter: https://github.com/MariaDB/mariadb-vector-wikipedia-rag (dataset S)

Vector search needs MariaDB 11.7 or later. The starter was developed on 12.2.2 and tested in
Docker with `mariadb:lts` (12.3.3 in October 2026).

## What the starter is

A Flask search page over the 29,460 articles of S. A question in English, Finnish or Swedish is
embedded with bge-m3 and one SQL statement returns the closest articles, with a toggle to compare
against `FULLTEXT` keyword search. It is retrieval only: the "G" of RAG is not built.

| File | Role |
|---|---|
| `schema.sql`, `create_tables.py` | Tables |
| `load.py` | Loads articles and categories; `--vectors` loads the ready-made vectors |
| `embed.py` | Resumable embedding through Ollama |
| `common.py` | DB connection, Ollama call, `search()` |
| `search.py` | CLI search (`--lang`, `--category`, `--keyword`, `--sql`) |
| `app.py` | The page, port 8000 |
| `evaluate.py` | hit@1/5/10, MRR and latency on the 37 test questions |
| `index_recall.py` | Index recall against exact search |
| `setup.sh` | Everything above in one go, about 3 minutes with ready-made vectors |

Docker database: name, user and password all `wikirag`, port 3307.

## Schema

```sql
CREATE TABLE article (
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
) CHARACTER SET utf8mb4;

CREATE TABLE article_vector (
  lang       CHAR(2)      NOT NULL,
  page_id    INT UNSIGNED NOT NULL,
  model      VARCHAR(50)  NOT NULL,
  truncated  BOOL         NOT NULL,
  embedding  VECTOR(1024) NOT NULL,
  PRIMARY KEY (lang, page_id),
  VECTOR INDEX (embedding) M=16 DISTANCE=cosine
);

CREATE TABLE category_link (parent VARCHAR(255), child VARCHAR(255), PRIMARY KEY (parent, child), KEY (child));
CREATE TABLE article_category (lang CHAR(2), page_id INT UNSIGNED, category VARCHAR(255),
                               PRIMARY KEY (lang, page_id, category), KEY (category));
```

- A column with a `VECTOR INDEX` must be `NOT NULL`. That is why vectors live in their own table:
  a row exists only once the vector is computed, which makes embedding resumable.
- `DISTANCE=cosine` because bge-m3 is trained for cosine similarity.
- Chunks would be one more table of the same shape.

## Query shapes

Top 10 by meaning. The subquery matters:

```sql
SET mhnsw_ef_search = 80;

SELECT a.lang, a.title, a.url, v.distance
FROM (SELECT lang, page_id, VEC_DISTANCE_COSINE(embedding, VEC_FromText(?)) AS distance
      FROM article_vector
      ORDER BY distance
      LIMIT 10) AS v
JOIN article a USING (lang, page_id)
ORDER BY v.distance;
```

MariaDB uses the vector index when `ORDER BY VEC_DISTANCE_...(column, vector) LIMIT n` stands
directly on the table that holds the index. The flat join version (`FROM article_vector v JOIN
article a ... ORDER BY distance LIMIT 10`) read about 22,000 rows and took 50 ms against 1 ms.
`EXPLAIN` named the index in both cases; only `SHOW SESSION STATUS LIKE 'Handler_read%'` showed
the difference.

A language filter can go inside the subquery (`WHERE lang = 'sv'`): 8 ms, results identical to
exact search.

With a category and all its subcategories:

```sql
WITH RECURSIVE cats AS (
  SELECT CAST('Suomen sisällissota' AS VARCHAR(255)) AS name
  UNION
  SELECT l.child FROM category_link l JOIN cats ON l.parent = cats.name)
SELECT a.lang, a.title, VEC_DISTANCE_COSINE(v.embedding, VEC_FromText(?)) AS distance
FROM article_vector v
JOIN article a USING (lang, page_id)
WHERE EXISTS (SELECT 1 FROM article_category c JOIN cats ON c.category = cats.name
              WHERE c.lang = a.lang AND c.page_id = a.page_id)
ORDER BY distance
LIMIT 10;
```

- The `CAST` is required: the first row of a recursive query fixes the column width, and a longer
  subcategory name otherwise fails with "Data too long for column 'name'".
- This runs category first, then compares that category's articles one by one. Exact, but the
  cost grows with the category: 14 ms for 104 articles, 280 ms for 4,675, 2,100 ms for all
  28,345.

## Index settings

Recall@10 of the index against exact search, 32 questions, 28,345 vectors:

| Index | `mhnsw_ef_search` | recall@10 | worst question | median time |
|---|---|---|---|---|
| none (exact) | n/a | 100% | 100% | 1,800 to 2,100 ms |
| `M=6` (default) | 20 (default) | 79% | 0% | 0.6 ms |
| `M=6` | 160 | 94% | 60% | 2.5 ms |
| `M=16` | 20 | 97% | 70% | 2.5 ms |
| `M=16` | 80 | 100% | 90% | 5.4 ms |
| `M=16` | 640 | 100% | 100% | 22.8 ms |

- `M` is links per node, fixed at index build. Rebuilding with `M=16` took 3.5 minutes.
- `mhnsw_ef_search` is candidates examined per search, set per session.
- The defaults are too low for 1024-dimensional vectors. The starter uses `M=16`, `ef_search=80`.
- The graph depends on insertion order, so recall varies slightly between builds.
- `mhnsw_max_cache_size` was left at 16 MB and its effect is unmeasured.

## Retrieval quality

37 test questions, `M=16`, `ef_search=80`. A hit is the expected article in any language.

| | Questions | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|---|
| Vector | all 37 | 57% | 76% | 86% | 0.65 |
| | English (13) | 54% | 69% | 77% | 0.61 |
| | Swedish (11) | 82% | 82% | 100% | 0.84 |
| | Finnish (13) | 38% | 77% | 85% | 0.52 |
| | answer only in Finnish (4) | 100% | 100% | 100% | 1.00 |
| Keyword (`FULLTEXT`) | all 37 | 11% | 16% | 41% | 0.15 |

Latency: embedding the question 28 ms median, the vector SQL 16 ms, keyword SQL 26 ms.

Where it fails:

- Facts inside long biographies. "Who was the president of Finland from 1956 to 1982?" does not
  find Urho Kekkonen in the top 10. Three of five president questions miss.
- One vector per article finds the topic, not the fact: "who led Finland in the Winter War"
  returns Winter War articles, not the leaders.
- Finnish questions have the lowest hit@1, attributed to long Finnish articles.
- Results lean toward the question's own language.
- `FULLTEXT` has no Finnish stemming: Mannerheim, Mannerheimin and Mannerheimille are three
  separate searches (649, 305 and 43 articles).
- 37 questions is small: one question is about 3 percentage points.

## Embedding

- Model: BAAI/bge-m3, 1024 dims, 8,192 token input, multilingual, MIT, via `ollama pull bge-m3`.
- Ollama trap: set both `num_ctx` and `num_batch` to 8192. With only `num_ctx`, texts over 2,048
  tokens are refused or silently cut.
- Speed on a MacBook Air M4: about 100 articles a minute, about 5 hours for all of S.
- Alternatives the starter weighed: multilingual-e5-large (512 tokens, fine with chunks),
  EmbeddingGemma-300m (2,048 tokens, as good as bge-m3 in a mariadb.org test, fine with chunks),
  Qwen3-Embedding-0.6B (newer, higher on MTEB, untested here). English-only models fail at
  cross-language search.
- Changing model means re-embedding everything and changing `VECTOR(1024)` to the new dimension.

## What the starter left undone

Each of these is marked to do, later, or to try in its README:

| Gap | Notes from the starter |
|---|---|
| Ask (generation with cited sources) | Top 3 to 5 whole articles is 30,000 to 50,000 tokens of context. Model choice undecided |
| Chunking | 200 to 400 word pieces or one per Markdown section, about 150,000 vectors, in a separate `chunk_vector` table. Expected to fix the fact-in-biography misses |
| Hybrid search | `MATCH ... AGAINST` and vector ranks fused with Reciprocal Rank Fusion, score = 1/(60 + rank_fulltext) + 1/(60 + rank_vector), in one statement. MariaDB doc: https://mariadb.com/docs/server/reference/sql-structure/vectors/optimizing-hybrid-search-query-with-reciprocal-rank-fusion-rrf |
| One result per topic | Group by `wikidata_id` so fi, en and sv versions count once |
| Large-category filtering | Take the index's top few hundred, then filter, instead of category first |
| Vectors for L | None provided for the 568,416 articles |
| Multi-turn chat | Deliberately left out |
| Where it would live | Standalone page, Wikimedia Toolforge (ToolsDB vector support unverified), or a MediaWiki gadget |
