# Tier B tools: what was built and how well it works (2026-11-14)

Pages: `/gaps` (B1), `/refs` (B2), `/links` (B3), `/consistency` (B4), all in `rag-demo/`, one Blueprint each, registered in `app.py`, linked from `hub.BANNER`, SQL shown on every page. The checks below are reproducible with `scripts/eval_tier_b.py`. Labelled samples and raw results are in `data/tier_b_*.json` (git-ignored).

All hand labelling was done by the model that wrote the tools, reading the passages. It was not done by a Wikipedia editor.

## Shared groundwork (new tables only)

| Table | Content | Size |
|---|---|---|
| `chunk`, `chunk_vector` | S Markdown cut on `##` (sections over 2,500 chars cut by paragraph, reference lists skipped), bge-m3, VECTOR INDEX M=16 cosine. `rag-demo/chunks.py`, resumable | 91,686 chunks, 29,455 articles (fi 19,282, sv 4,920, en 5,253) |
| `para`, `para_vector`, `para_ref`, `article_link` | Newest M wikitext per Finnish article, cleaned paragraphs, their `<ref>` text, outgoing links. `rag-demo/paras.py`, resumable | 132,446 paragraphs (97,763 with a ref), 191,421 refs, 550,412 links; all 19,285 Finnish articles (category root "Suomen historia") |
| `pair_check`, `consistency_finding` | Cache of the yes/no checks and the B4 pairs | grows with use |

Embedding ran on the local GPU (RTX 4060, about 45 chunks/s). The Verda instance was not needed and was deleted (billing stopped).

## Vector index use

The brief's query shape (`ORDER BY VEC_DISTANCE_COSINE ... LIMIT` in a subquery, join afterwards, `mhnsw_ef_search = 80`) is used for B2, B3 and B4. Every page shows `EXPLAIN` and the `Handler_read%` difference (`tb.run_checked`).

- Index path: `EXPLAIN` shows key `embedding` on the vector table, `Handler_read_last` 1, and no `Handler_read_rnd_next` on the vector table (a scan of 8,261 rows shows 8,261 there).
- **A `WHERE lang = ...` inside the subquery makes MariaDB drop the vector index** (it picks the primary key and a filesort). So B3 and B4 take a pool of 200 and 150 nearest rows and filter the language afterwards.
- **B1 does not use the vector index**, on purpose: "nearest chunk inside one given article" is an exact distance over that article's tens of rows (plan: `ref` on the primary key). An HNSW index answers "nearest in the whole table" and cannot be pointed at one article.

**Recall caveat (found late):** `mhnsw_max_cache_size` is 16 MB while the vector indexes are far larger, so the HNSW search reads a partly different graph on different connections: the same query can return different neighbour sets, and recall@10 against an exact scan measured 0.94 to 0.96 on average (minimum 0.4, 40 article queries). The B2, B3 and B4 numbers below carry that noise. A larger cache should fix it; not changed here because it is a server setting.

## B1 Cross-language gap finder

Tested on 50 random sv/en sections compared with the fi article of the same topic (a section is "missing" if the nearest fi section does not cover its content): 11 were missing.

| Threshold | Flagged | Missing among them | Precision | Recall |
|---|---|---|---|---|
| > 0.30 | 19 | 11 | 58% | 100% |
| > 0.32 | 16 | 10 | 62% | 91% |
| > 0.36 | 11 | 7 | 64% | 64% |
| > 0.40 | 3 | 3 | 100% | 27% |

The sample is mostly short biographies and places (median 3 sections per article). On a long article pair (Vinterkriget to Talvisota, 14 sections) 12 sections were above 0.32 and only 3 were really missing (children sent abroad, the Kvarken trade route, Finnish volunteers in Russia), but those 3 were among the 4 highest distances. Over the 64 labelled sections (50 + 14): above 0.40, 6 of 7 were missing; between 0.32 and 0.40, about one in three. A threshold relative to each article pair's median did worse on the short-article sample (it is not decisive: few sections per pair).

Decision: two levels in the page. "Probably missing" above 0.40, "worth a look" between 0.32 and 0.40, folded. English reference lists ("Citations") were removed from the comparison because they came out as "missing". Small sample: 64 sections, 14 of them missing.

## B2 Reference borrower

50 sourced paragraphs chosen at random (seed 7), their ref hidden, their own article excluded:

- 13 of 50 have exactly the same ref text in a paragraph of another article (ceiling for "same source").
- Retrieval (5 nearest paragraphs with a ref): the same source is among them for 11 of those 13.
- After the yes/no "same fact" check (confirmed at 0.5): 3 of 13. 11 of 50 paragraphs get a proposal at all.
- Counting "same work, page numbers ignored": retrieval 16/50, after the check 4/50.

The check removes most correct sources because another article often cites the same book for a different statement (different page). So the page shows unconfirmed candidates too, marked "not confirmed". The result is a lead: many refs are short forms ("Jakobsson 2002, s. 121") that mean nothing without the other article's bibliography, and the editor still has to open the source.

## B3 Link suggester

100 existing links hidden in memory (links between articles of the whole Finnish set with no link back; seed 11), top-10 unlinked neighbours by vector, Finnish only:

| | Subtree "Suomen sodat" (917 articles) | All 19,285 articles |
|---|---|---|
| Suggested back in top 10 | 29% | 8% |
| Top 25 | 42% | 16% |
| Found anywhere in the 200 nearest | 49% | 28% |
| Median rank when found | 6 | 23.5 |

With the full set there are many more near articles that are unlinked, and one hidden link is only one of the tens an article has. A larger pool (500, 1000) did not improve the subtree result. Link targets are matched by exact title, so redirects count as "not linked". Inbound counts are counts within the parsed set.

## B4 Consistency checker

Sample: 400 random Finnish sections of the full set (seed 3), their neighbours in other Finnish articles within distance 0.35, 800 pairs. Three versions, each re-run on the same pairs:

| Version | Same-subject pairs | Flagged at p >= 0.5 | True among the flagged |
|---|---|---|---|
| 1. one question, "incompatible values for the same fact" (first 60 sections only) | not asked | 33 of 124 | 0 of the 17 highest (two people, elections, factories, churches with similar wording) |
| 2. plus "same specific subject?" (both must hold), table-heavy chunks skipped | 231 of 808 | 8 | 0 of 8 (the true differences scored 0.47 and 0.48) |
| 3. each passage prefixed with its article title and section heading (**current**) | 56 of 799 | **1** | **1 of 1** |

With titles the check stops confusing two relatives with one surname, or two forts built the same summer. The one flagged pair is a real difference: Tele Finland is said to have merged into its parent on 31 Dec 2005 in the Sonera article and in spring 2017 in its own article.

Current levels in the page (product of P(same subject) and P(incompatible)):

- **0.5 and above, "may differ":** 1 pair, true.
- **0.3 to 0.5, "worth a look":** 2 pairs: Lappeenrannan linnoitus / vankileiri ("about 500 executed" against "almost 700 died": a real figure difference, though executed and died are not the same count) is a lead; Mäkiluoto / Pietari Suuren merilinnoitus is consistent text (false).
- So at 0.3 and above: 2 of 3 are real leads. Very small numbers: this is not a precision estimate, it says the tool surfaces few pairs and they are worth reading.

Recall, planted changes (one year moved by 7 in passage B of 48 same-subject pairs): 8 of 48 flagged at 0.5, 16 of 48 at 0.3. This is a lower bound because the changed year is often not a fact that passage A states. In practice the tool finds few contradictions: most pairs of near passages simply agree, and a check this conservative misses subtle ones.

Findings are cached in `pair_check` (every answer) and `consistency_finding` (pairs shown). The page shows both passages side by side and never says which is right.

## Not done / open

- B1 on more long article pairs (its labelled sample is mostly short biographies); B4 on Swedish and English sections (only Finnish was sampled) and on a real second labeller.
- B3 pool across languages: the neighbour search takes the 200 nearest articles in any language and keeps the Finnish ones.
- The pages have been run through the Flask server and checked through their JSON APIs; the JavaScript of the pages was syntax-checked, but no browser rendering of B1 to B4 was inspected.
