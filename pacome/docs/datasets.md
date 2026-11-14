# Datasets

Repo: https://github.com/MariaDB/mariadb-wikipedia-datasets

The three are not one dataset in three sizes. Each is built for a different kind of project.

| | S: History of Finland, now | M: History of Finland, every version | L: all of Finnish Wikipedia, now |
|---|---|---|---|
| Made for | RAG, vector search | System-versioned tables | Showing an idea scales |
| Articles | 19,285 fi, 4,921 sv, 5,254 en | 19,742 fi | 568,416 fi |
| Versions | current only | every saved version, 2002 to 1 Oct 2026: 862,076 (12.2 GB of text) | current only |
| Text | clean Markdown | raw wikitext | clean Markdown (1.4 GB) |
| Snapshot | August 2025 (FineWiki) | 1 October 2026 (Wikimedia dumps) | August 2025 (FineWiki) |
| Vectors | yes: bge-m3, 1024 dims, one per article, all three languages | no | no |
| Extras | categories, category tree | editor, edit summary, text hash | none |
| Download | 156 MB | 315 MB | 495 MB |

Downloads (each with `SHA256SUMS`):

- S: https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/tag/s-2026-10
- M: https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/tag/m-2026-10
- L: https://github.com/MariaDB/mariadb-wikipedia-datasets/releases/tag/l-2026-10

No account or API key is needed for any of it.

## S

| File | Rows | Columns |
|---|---|---|
| `articles-fi.parquet`, `-sv`, `-en` | 19,285 / 4,921 / 5,254 | `lang`, `page_id`, `rev_id`, `wikidata_id`, `title`, `url`, `date_modified`, `text` |
| `vectors-bge-m3-fi.parquet`, `-sv`, `-en` | one per article | `lang`, `page_id`, `model`, `truncated`, `embedding` (1024 x float32) |
| `category-links-fi.tsv` | 1,760 | `parent`, `child` |
| `article-categories-fi.tsv` | 25,720 | `page_id`, `category` |

- Articles: everything under the Finnish category `Luokka:Suomen historia`, three levels of
  subcategories down (19,296), plus the main article of each named category one level deeper
  (458, which is how the presidents get in).
- Swedish and English: articles on the same topics, matched by `wikidata_id`. 2,919 topics exist
  in all three languages.
- Text comes from FineWiki (Hugging Face): rendered pages turned into Markdown, templates filled
  in, no wiki markup.
- Vectors are made from title + text with BAAI/bge-m3 through Ollama. The model reads 8,192
  tokens; 290 longer articles (1%) were cut and flagged `truncated`.
- Questions must be embedded with the same model as the vectors.
- Categories are Finnish only. Swedish and English articles inherit the categories of the
  Finnish article with the same `wikidata_id`.
- Also in the repo: `lists/s-test-questions.tsv`, 37 questions (13 en, 11 sv, 13 fi) each with
  the Finnish title of the article that answers it. Columns: `lang`, `question`,
  `expected_fi_title`, `article_exists_in`.

## M

| File | Rows | Columns |
|---|---|---|
| `versions-01.parquet` to `versions-16.parquet` | 862,076 versions of 19,742 articles | `page_id`, `title`, `rev_id`, `parent_id`, `timestamp` (UTC), `user`, `anonymous`, `comment`, `minor`, `sha1`, `bytes`, `text` |

- Same article list as the Finnish part of S. M has slightly more articles (19,742 against
  19,285) because FineWiki's snapshot is older.
- Versions are ordered oldest first per article.
- Text is raw wikitext. Old versions exist in no other form, so templates, infoboxes and
  reference syntax are all in there.
- `anonymous`: 19% of versions. An IP address, or since 2025 a temporary account named like
  `~2026-33532-4`.
- `sha1`: same hash, same text. A version whose text equals an earlier one but not the one just
  before it is a revert.
- Versions hidden by an administrator are absent.
- Size once loaded: Parquet compresses 12.2 GB to 315 MB because consecutive versions are nearly
  identical. In a MariaDB table with `COMPRESSED` columns the README estimates 5 to 7 GB for all
  of M.

## L

| File | Rows | Columns |
|---|---|---|
| `articles-fi.parquet` | 568,416 | same as S |

- Same columns as S, so code written against S runs on L.
- All 19,285 Finnish articles of S are in L, same version.
- No vectors. At about 100 articles a minute on the author's laptop, embedding all of it takes
  days. The README calls it a GPU job, or a job for a sample.
- No categories. They can be pulled from the Wikimedia SQL dumps (`categorylinks`, `linktarget`,
  `page`) at https://dumps.wikimedia.org/fiwiki/20261001/.
- Ready-made vectors at this scale exist from Cohere (2.4 million paragraphs of Finnish
  Wikipedia), but they date from November 2023 and querying them needs a Cohere API key.

## Keys and links

- `page_id` is unique only within one language. S uses `(lang, page_id)`.
- `wikidata_id` ties the same topic across languages (Talvisota, Winter War and Vinterkriget are
  all `Q134949`).
- Finnish `page_id` is the join between S, M and L.
- Link to the live article: `https://fi.wikipedia.org/?curid=<page_id>`.
- Link to one version: `https://fi.wikipedia.org/?oldid=<rev_id>`.

## Mismatches to keep in mind

- S and L are from August 2025, M runs to October 2026. The newest version in M is usually not
  the version in S.
- S and L are Markdown, M is wikitext. Comparing or embedding across them needs a wikitext
  cleaner. The SVT starter has a light one in `history.py` (drops templates, tables and
  references, turns links into words).
- Edit history exists for Finnish only. English full history is about 300 GB compressed.

## Building from scratch

Only needed for another topic or a fresher snapshot.

| Script | Time | Needs |
|---|---|---|
| `dataset_s.py` | about 2 h, plus about 5 h with `--vectors embed` | Ollama + bge-m3 only for embedding |
| `dataset_m.py` | about 25 min plus a 4.5 GB download | 7-Zip (`7zz`) |
| `dataset_l.py` | about 4 min | nothing extra |

All need Python 3.11+ with `duckdb`, `pyarrow`, `requests`. All are resumable.
