# API contract (frontend builds against this; mocks in /mocks follow it)

Base: `http://localhost:8000`. JSON. Years are ints, dates `YYYY-MM-DD`.

## GET /api/map
All articles as points. Positions are fixed (computed once).
```json
{"points":[{"page_id":123,"title":"Talvisota","x":0.41,"y":-0.12,"first_year":2003,"edits_total":5120}]}
```

## GET /api/timeline?year=2010
State of the "encyclopedia" for a year (from versioned table + activity_year).
```json
{"year":2010,"articles_existing":14200,
 "activity":[{"page_id":123,"edits":41,"reverts":6}],
 "existing_page_ids":[123,124]}
```
`activity` lists only articles with edits that year.

## GET /api/hot?year=2010&limit=10
Most contested articles that year (reverts).
```json
{"year":2010,"items":[{"page_id":123,"title":"Talvisota","edits":41,"reverts":6}]}
```

## POST /api/ask
Body: `{"question":"Why did Finland join NATO?","as_of":"2015-06-01"}` (`as_of` optional)
```json
{"answer":"Short answer with [1] numbered citations [2].",
 "citations":[{"n":1,"page_id":123,"rev_id":456,"title":"Talvisota","url":"https://fi.wikipedia.org/?curid=123","passage":"..."}],
 "sql":"SELECT ...","took_ms":42}
```

## GET /api/article/{page_id}/changes?from=2015-01-01&to=2026-10-01
Then-versus-now for showcase articles (cached).
```json
{"page_id":123,"title":"Talvisota","from":"2015-01-01","to":"2026-10-01",
 "summary":"What changed, in 2-3 sentences.",
 "then":"older text...","now":"newer text...",
 "diff":[{"op":"equal|add|del","text":"..."}]}
```

## Errors
`{"error":"message"}` with a normal HTTP status.
