"""Cross-language conflict detection over the 100 most content-rich trilingual Wikipedia topics.

Reads the FineWiki parquet dataset directly (no database needed), compares the Finnish,
English and Swedish versions of each topic with Gemini, and reports genuine contradictions
(incompatible facts and opposing interpretations) -- not coverage gaps or wording differences.

Resumable: each topic's result is cached under data/conflicts/<wikidata_id>.json, so a rerun
skips finished topics. Config (API key, model) is reused from rag-demo/rag.py.

    python scripts/crosslang_conflicts.py             # run all 100 (skips cached)
    python scripts/crosslang_conflicts.py --limit 3   # pilot on the first 3
    python scripts/crosslang_conflicts.py --report    # rebuild the summary from cache only
    python scripts/crosslang_conflicts.py --workers 8 # concurrency (default 8)
"""
import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import duckdb
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rag-demo"))
import rag  # noqa: E402  (loads .env on import; provides _key(), GEMINI_MODEL, THINKING)

DATA = ROOT / "reference" / "mariadb-vector-wikipedia-rag" / "data"
OUT = ROOT / "data" / "conflicts"
CAP = 60_000            # chars per language sent to the model
N = 100                 # topics to compare
API = f"https://generativelanguage.googleapis.com/v1beta/models/{rag.GEMINI_MODEL}:generateContent"

SYSTEM = (
    "You are a meticulous cross-language Wikipedia fact-checker. You compare the Finnish, English and "
    "Swedish versions of the SAME article (one Wikidata topic) and find genuine contradictions between "
    "them. A conflict is when two or more versions make incompatible claims about the same fact: different "
    "dates, numbers, names, outcomes, causes, or opposing interpretations presented as fact. A conflict may "
    "involve just two of the three languages.\n"
    "Do NOT report mere differences in coverage or detail: one version mentioning something another omits "
    "is a gap, NOT a conflict. Do NOT report wording or translation differences, or differences only in "
    "article structure, length or sources.\n"
    "For each conflict give a faithful English translation of what EACH version says (use 'not addressed' "
    "if a version is silent on that point), classify it, and rate severity: high = a major contradiction "
    "(outcome, cause, key date or number); medium = a notable but secondary contradiction; low = a minor "
    "detail. Be conservative: only report real contradictions you can point to in the texts."
)
SCHEMA = (
    'Return JSON: {"conflicts":[{"type":"factual|numerical|chronological|interpretive",'
    '"severity":"high|medium|low","fi":"what the Finnish version says (English)",'
    '"en":"what the English version says (English)","sv":"what the Swedish version says (English)",'
    '"explanation":"why these conflict"}]}. If there are no genuine conflicts, return {"conflicts":[]}.'
)

_print_lock = threading.Lock()
_done = 0


def select_topics():
    """The 100 most content-rich trilingual topics (reproducible rule from the plan)."""
    d = duckdb.connect()
    for lang in ("fi", "en", "sv"):
        d.sql(f"CREATE VIEW {lang} AS SELECT * FROM '{DATA}/articles-{lang}.parquet'")
    rows = d.sql(f"""
        SELECT f.wikidata_id, f.title fi_title, e.title en_title, s.title sv_title,
               f.text fi_text, e.text en_text, s.text sv_text,
               length(f.text) fi_len, length(e.text) en_len, length(s.text) sv_len
        FROM fi f JOIN en e USING(wikidata_id) JOIN sv s USING(wikidata_id)
        WHERE f.wikidata_id IS NOT NULL
          AND length(f.text) >= 1500 AND length(e.text) >= 1500 AND length(s.text) >= 1500
          AND length(f.text)+length(e.text)+length(s.text) <= 400000
          AND e.title NOT LIKE 'List of%' AND f.title NOT LIKE 'Luettelo%' AND s.title NOT LIKE 'Lista%'
        ORDER BY length(f.text)+length(e.text)+length(s.text) DESC
        LIMIT {N}""").fetchall()
    topics = []
    for qid, fi_t, en_t, sv_t, fi_x, en_x, sv_x, fi_l, en_l, sv_l in rows:
        topics.append(dict(
            wikidata_id=qid, fi_title=fi_t, en_title=en_t, sv_title=sv_t,
            fi_text=fi_x[:CAP], en_text=en_x[:CAP], sv_text=sv_x[:CAP],
            fi_len=fi_l, en_len=en_l, sv_len=sv_l,
            truncated=(fi_l > CAP or en_l > CAP or sv_l > CAP),
        ))
    return topics


def _call(prompt):
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32768,
                             "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingLevel": "medium"}},
    }
    last = ""
    for attempt in range(6):
        try:
            r = requests.post(API, headers={"x-goog-api-key": rag._key()}, json=body, timeout=240)
        except requests.RequestException as e:
            last = type(e).__name__
            time.sleep(2.0 * (attempt + 1))
            continue
        if r.status_code == 200:
            j = r.json()
            txt = "".join(p.get("text", "") for p in j["candidates"][0]["content"]["parts"])
            return json.loads(txt), j.get("usageMetadata", {})
        last = f"{r.status_code}: {r.text[:200]}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2.0 * (attempt + 1))
            continue
        break
    raise RuntimeError(f"Gemini failed ({last})")


def compare(topic):
    """One topic -> result dict. Cached on disk; raises on failure."""
    cache = OUT / f"{topic['wikidata_id']}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    prompt = (
        f"ARTICLE: {topic['en_title']} (fi: {topic['fi_title']} / sv: {topic['sv_title']})\n\n"
        f"=== FINNISH VERSION ===\n{topic['fi_text']}\n\n"
        f"=== ENGLISH VERSION ===\n{topic['en_text']}\n\n"
        f"=== SWEDISH VERSION ===\n{topic['sv_text']}\n\n{SCHEMA}"
    )
    try:
        data, usage = _call(prompt)
    except (json.JSONDecodeError, RuntimeError):
        data, usage = _call(prompt)        # one retry on parse/API failure
    conflicts = data.get("conflicts", []) if isinstance(data, dict) else []
    result = dict(
        wikidata_id=topic["wikidata_id"], fi_title=topic["fi_title"],
        en_title=topic["en_title"], sv_title=topic["sv_title"],
        truncated=topic["truncated"], conflicts=conflicts,
        usage={k: usage.get(k) for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")},
    )
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def _progress(result, total):
    global _done
    with _print_lock:
        _done += 1
        n = len(result.get("conflicts", []))
        err = result.get("error")
        tag = f"ERROR {err}" if err else f"{n} conflict(s)"
        print(f"[{_done}/{total}] {result['en_title'][:50]:50} {tag}", flush=True)


def run(topics, workers):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "selected.json").write_text(
        json.dumps([{k: t[k] for k in ("wikidata_id", "fi_title", "en_title", "sv_title",
                                       "fi_len", "en_len", "sv_len", "truncated")} for t in topics],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    results, errors = [], []
    total = len(topics)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(compare, t): t for t in topics}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                res = fut.result()
            except Exception as e:               # noqa: BLE001 - record and continue
                res = dict(wikidata_id=t["wikidata_id"], en_title=t["en_title"],
                           fi_title=t["fi_title"], sv_title=t["sv_title"],
                           truncated=t["truncated"], conflicts=[], error=str(e)[:200])
                errors.append(res)
            results.append(res)
            _progress(res, total)
    results.sort(key=lambda r: next(i for i, t in enumerate(topics) if t["wikidata_id"] == r["wikidata_id"]))
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return results, errors


def build_report(results):
    total_conf = sum(len(r.get("conflicts", [])) for r in results)
    with_conf = [r for r in results if r.get("conflicts")]
    errors = [r for r in results if r.get("error")]
    by_sev, by_type = {}, {}
    for r in results:
        for c in r.get("conflicts", []):
            by_sev[c.get("severity", "?")] = by_sev.get(c.get("severity", "?"), 0) + 1
            by_type[c.get("type", "?")] = by_type.get(c.get("type", "?"), 0) + 1
    toks = sum((r.get("usage") or {}).get("totalTokenCount", 0) or 0 for r in results)
    L = ["# Cross-language conflicts: English vs Finnish vs Swedish Wikipedia", "",
         f"Compared **{len(results)}** trilingual topics (the most content-rich, matched by Wikidata ID).",
         f"Found **{total_conf} conflicts** across **{len(with_conf)} articles**; "
         f"{len(results) - len(with_conf) - len(errors)} articles had none.",
         f"By severity: {by_sev}. By type: {by_type}.",
         f"Tokens used: {toks:,}. Errors: {len(errors)}.", "",
         "A conflict = incompatible facts or opposing interpretations presented as fact. "
         "Coverage gaps and wording differences are excluded. Each statement below is an English "
         "translation of what that language version says.", "",
         "> Caveats: LLM-based detection (spot-checked, not exhaustive). The longest articles are "
         "compared on their first 60,000 characters per language; conflicts in the truncated tail are "
         "out of scope. 'not addressed' = that version is silent on the point.", ""]
    if errors:
        L += ["## Errors", ""] + [f"- {r['en_title']} ({r['wikidata_id']}): {r['error']}" for r in errors] + [""]
    L += ["## Conflicts by article", ""]
    for r in with_conf:
        trunc = " _(truncated)_" if r.get("truncated") else ""
        L += [f"### {r['en_title']}{trunc}",
              f"fi: {r['fi_title']} · sv: {r['sv_title']} · {r['wikidata_id']}", ""]
        for c in r["conflicts"]:
            L += [f"- **[{c.get('severity','?')}/{c.get('type','?')}]** {c.get('explanation','')}",
                  f"  - fi: {c.get('fi','')}", f"  - en: {c.get('en','')}", f"  - sv: {c.get('sv','')}"]
        L += [""]
    none = [r["en_title"] for r in results if not r.get("conflicts") and not r.get("error")]
    L += ["## Articles with no detected conflict", "", ", ".join(none), ""]
    (OUT / "summary.md").write_text("\n".join(L), encoding="utf-8")
    return total_conf, len(with_conf), by_sev, by_type, len(errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="only the first N topics (pilot)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--report", action="store_true", help="rebuild summary from cache, no API calls")
    args = ap.parse_args()

    topics = select_topics()
    if args.limit:
        topics = topics[:args.limit]
    if args.report:
        results = [json.loads((OUT / f"{t['wikidata_id']}.json").read_text(encoding="utf-8"))
                   for t in topics if (OUT / f"{t['wikidata_id']}.json").exists()]
    else:
        print(f"Comparing {len(topics)} topics, {args.workers} workers, model {rag.GEMINI_MODEL}", flush=True)
        t0 = time.time()
        results, _ = run(topics, args.workers)
        print(f"Done in {time.time()-t0:.0f}s", flush=True)
    tc, wc, sev, typ, errs = build_report(results)
    print(f"\n{tc} conflicts in {wc}/{len(results)} articles. severity={sev} type={typ} errors={errs}")
    print(f"Report: {OUT/'summary.md'}")


if __name__ == "__main__":
    main()