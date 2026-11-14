# The challenge

Sponsor: MariaDB (https://mariadb.org/). Event: Junction.

> Build the tools Wikipedia is missing, powered by MariaDB's vector search (RAG) and
> system-versioned tables.

The mission: a tool for Wikipedia that does not exist yet, or exists but is badly underused, with
MariaDB at the core of the stack.

## Tracks

| Track | For | What to build | Sponsor's example |
|---|---|---|---|
| RAG | most teams | Retrieval-augmented generation on MariaDB native vector search: plain-language questions, grounded answers with cited sources | A research assistant for the history of Finland |
| SVT | advanced teams | Tools on system-versioned tables that query how knowledge changes over time | What an article said on a given date, surfacing edit wars, tracing how the story of an event was rewritten |
| Both | very advanced teams | The two combined | "What did Wikipedia say about this in 2015, and what has changed since?" |

## Judging criteria

Five criteria, 20% each:

| Criterion | Wording |
|---|---|
| Use of MariaDB | Effective and creative use of MariaDB RAG and/or system-versioned tables |
| Implementation | Technical execution and scalability across the provided datasets |
| Usefulness | Real-world usefulness for Wikipedia readers or editors |
| Design and demo | Design and demo quality |
| Originality | The most original and unique implementation |

Bonus (weighted 0%): added technical ambition for teams taking the SVT track or combining both.
The text says these teams "will be recognised", but the bonus carries no points on the scoresheet.

What follows from the wording:

- "Scalability across the provided datasets" means the demo should run on more than the small
  set. The datasets README says L exists for exactly this: show the idea scales.
- 40% of the score (usefulness, design and demo) has nothing to do with the database.
- MariaDB must be doing real work, not sitting behind an app that would work the same on any
  store.

## What the sponsor says it wants to see

- LLMs carry a world view from their training data, which may differ from Wikipedia's, which
  strives for NPOV (neutral point of view).
- Two user groups: Wikipedia readers, and active Wikipedians who edit. Pick either or both.
- Tools that make Wikipedia's knowledge easier to digest, verify and trust: source-grounded Q&A,
  fact-checking assistants, detecting gaps and weak spots in article coverage.
- Comparing the same article between languages.
- On the SVT side, "time-travel" tools: point-in-time views, change visualisations, edit wars or
  bias detection, edit-history analytics.

## Constraints and freedoms

- Source articles are in Finnish. A multilingual embedding model is recommended. No Finnish
  skills needed.
- Any LLM, embedding model or frontend framework is allowed.
- Three datasets (small, medium, large), Finnish Wikipedia, history of Finland. The brief calls
  them "preliminary". See [datasets.md](datasets.md).

## Provided

- MariaDB Server (open source) with native vector search and system-versioned tables
- The three datasets
- Starter documentation and two example apps
- Mentors on site throughout

## Mentors

- Kaj Arnö, MariaDB Foundation, Executive Chairman
- Robert Silén, MariaDB Foundation, AI Lead (author of all three starter repos)

## Prizes

| Place | Prize |
|---|---|
| 1st | EUR 1,000 cash, EUR 1,000 UpCloud credits, a MariaDB certification per team member (5 vouchers at learn.mariadb.com, value USD 750) |
| 2nd | EUR 500 cash |
| 3rd | EUR 250 cash |

All winning teams are credited in a blog post on mariadb.org.

## Background

MariaDB is the database behind Wikipedia. It was created in Finland by Michael "Monty" Widenius,
the original author of MySQL. Wikipedia search is still largely keyword-based, and the edit
history of articles is rarely explored: those are the two gaps the challenge points at.
