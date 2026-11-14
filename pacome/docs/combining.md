# Combining the two tracks

The sponsor's example: "What did Wikipedia say about this in 2015, and what has changed since?"
Neither starter builds it. The SVT README lists it as step 7, "later", with one hint: one vector
per year per article, not per version.

## What lines up

- Same topic and same article list: the Finnish articles of S are the articles of M.
- Finnish `page_id` joins them. `rev_id` identifies a version in both.
- One MariaDB version covers both: vectors need 11.7+, history loading needs 10.11+, and
  `mariadb:lts` is 12.3.
- The two starters use separate databases on ports 3307 and 3308. A combined tool wants one.

## What does not line up

| Mismatch | Consequence |
|---|---|
| S text is Markdown, M text is wikitext | Embedding old versions means cleaning wikitext first, or the vectors carry infobox and reference syntax. `history.py` in the SVT starter has a light cleaner |
| S is August 2025, M ends October 2026 | The S vector for an article does not describe M's newest version |
| S vectors are fi, sv, en; history is fi only | Time travel is Finnish only. Cross-language questions still work for retrieval since bge-m3 is multilingual |
| One vector per article in S | A per-version or per-period vector table has to be designed and filled by us |
| 862,076 versions | Embedding all of them is out of reach. At the starter's 100 articles a minute it would take about 6 days, and most versions are near-duplicates |

## Volume, for sizing a vector-over-time table

- 19,742 articles over roughly 24 years. One vector per article per year is at most about
  470,000 vectors, fewer in practice since many articles are young or unchanged in a given year.
- Skipping years where `sha1` did not change removes the unchanged ones for free.
- With chunking the count multiplies by the chunks per article.

## Unverified

Check these with a mentor or a five-minute test before designing around them:

- Whether a `VECTOR INDEX` can sit on a table declared `WITH SYSTEM VERSIONING`, and what
  `FOR SYSTEM_TIME AS OF` does to an indexed vector search if so.
- If not, the fallback is a plain vector table keyed by `(page_id, rev_id)` or
  `(page_id, period)`, joined to the versioned table.
- How the vector index behaves at several hundred thousand rows (the starter measured 28,345).
- Embedding throughput on our GPU. The starter's figure is an Apple M4 through Ollama.
