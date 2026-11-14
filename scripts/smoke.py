"""End-to-end check of what the extension and the drift page use, against a running server.

    python3 scripts/smoke.py                      # http://127.0.0.1:8020
    python3 scripts/smoke.py --base URL --quick   # --quick skips Ask (an LLM call, ~10 s)

Needs only the standard library. Exit code 1 if any check fails. Expects the demo data: the articles
below must be in the database (Talvisota with its history loaded, as eval/load_history.py does).
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

FAILED = []


def get(base, path, **params):
    url = f"{base}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=180) as r:
        return r.status, r.read().decode()


def check(name, fn):
    t0 = time.perf_counter()
    try:
        detail = fn()
        print(f"  ok    {name}  ({time.perf_counter() - t0:.1f} s)  {detail}")
    except Exception as e:
        FAILED.append(name)
        print(f"  FAIL  {name}  ({time.perf_counter() - t0:.1f} s)  {type(e).__name__}: {e}")


def sse_events(base, **params):
    """Read a drift stream to its end: {event name: count}, plus the summary text."""
    counts, summary, event = {}, "", None
    url = f"{base}/api/drift/stream?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=600) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
                counts[event] = counts.get(event, 0) + 1
            elif line.startswith("data: ") and event == "problem":
                raise AssertionError(f"problem event: {line[6:200]}")
            elif line.startswith("data: ") and event == "summary":
                summary = json.loads(line[6:]).get("text", "")
    return counts, summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8020")
    p.add_argument("--quick", action="store_true")
    a = p.parse_args()
    base = a.base.rstrip("/")
    print(f"smoke test against {base}")

    def health():
        s, body = get(base, "/api/health")
        assert s == 200 and json.loads(body)["ok"], body
        return ""
    check("health", health)

    def conflicts():
        d = json.loads(get(base, "/api/consistency", lang="sv", title="Regeringen_Aura_II", compute="true")[1])
        assert d["found"] and len(d["conflicts"]) >= 1, "no conflicts for Regeringen Aura II"
        assert all("current_is_a" in c for c in d["conflicts"]), "conflicts lack current_is_a"
        return f"{len(d['conflicts'])} conflicts"
    check("Conflicts tab (sv: Regeringen Aura II)", conflicts)

    def gaps():
        d = json.loads(get(base, "/api/consistency", lang="en", title="Hauho", compute="true")[1])
        assert d["found"] and len(d["gaps"]) >= 1, "no missing content for Hauho"
        return f"{len(d['gaps'])} missing, {len(d['conflicts'])} conflicts"
    check("Missing here tab (en: Hauho)", gaps)

    def search():
        d = json.loads(get(base, "/api/consistency/search", q="Aura")[1])
        n = len(d.get("conflicts", [])) + len(d.get("gaps", [])) + len(d.get("articles", []))
        assert n, "empty search"
        return f"{n} results"
    check("Search (Aura)", search)

    if not a.quick:
        def ask():
            req = urllib.request.Request(f"{base}/api/ask", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps({"question": "Which peace treaty ended the Winter War?",
                                                          "mode": "editor"}).encode())
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            assert d.get("answer") and d.get("citations"), str(d)[:200]
            assert "[1]" in d["answer"], "answer has no citation mark"
            return f"{len(d['citations'])} citations"
        check("Ask tab", ask)

    def page():
        s, body = get(base, "/drift", q="Talvisota", lang="fi")
        assert s == 200 and 'id="strips"' in body, "drift page did not render"
        others = [h for h in ("/mirror", "/gaps", "/refs", "/links", "/neighbors", "/stats") if f'href="{h}' in body]
        assert not others, f"links to other pages: {others}"
        return ""
    check("Drift page renders", page)

    def drift_live():
        counts, summary = sse_events(base, q="Talvisota", lang="fi")
        assert counts.get("done") == 1, counts
        assert counts.get("change", 0) >= 1, counts
        return f"{counts.get('change', 0)} changes, summary {len(summary)} chars"
    check("Drift stream (fi: Talvisota)", drift_live)

    def drift_fixture():
        counts, _ = sse_events(base, fixture=1, fast=1)
        assert counts.get("done") == 1 and counts.get("change", 0) >= 1, counts
        return f"{counts.get('change')} changes"
    check("Drift recorded run", drift_fixture)

    def asof():
        sources = json.loads(get(base, "/api/drift/asof", page_id=0, date="2010-01-01")[1])
        # page_id 0 is not an article: the call must answer (rev_id None or an error), not crash the server
        assert isinstance(sources, dict), sources
        s, body = get(base, "/api/consistency", lang="fi", title="Talvisota", compute="false")
        pid = json.loads(body)["article"]["page_id"]
        d = json.loads(get(base, "/api/drift/asof", page_id=pid, date="2010-06-01")[1])
        assert d.get("rev_id") and len(d.get("text", "")) > 1000, str(d)[:200]
        return f"rev {d['rev_id']}, {len(d['text'])} chars"
    check("As-of reader (Talvisota, 2010-06-01)", asof)

    print("all passed" if not FAILED else f"{len(FAILED)} failed: {', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
