"""Opinion drift engine: how the top articles for a question changed their position on a set of claims, edit by edit.

  run(question)                      generator of (event, data) following the /api/drift/stream contract
  evidence(page_id, rev_id, claim)   the paragraph that carries a claim, found by binary descent over paragraph spans

Method. Texts are never compared with each other. Each version is scored against the same K claims (one Decisions
request per version, K questions). A coarse grid of versions is scored first; wherever a claim's stance differs between
two neighbouring probes, the midpoint version is scored for that claim only, until the two versions are consecutive.
That edit is the one that changed the position: about log2(gap) requests per change instead of one per version.
"""
import hashlib
import json
import time

import requests

import decisions
import history
import rag
import tb

RELEVANT = ("The question is: \"{b}\"\n\nThe text is the opening of a Wikipedia article. Is this article about the subject that the question "
            "asks about (the same events, people, places or relations), so that it is a sensible place to look for the answer, even if the "
            "opening does not give the answer itself? Answer no if the article only shares the country or the period with the question's "
            "subject. Judge only what the text says.")
RELEVANT_MIN = 0.5       # P(relevant) from the yes/no check below which an article is dropped
NO_MATCH = 0.48          # cosine distance of the best article above which nothing is close enough to the question
MARGIN = 0.07            # an article farther than best + MARGIN is dropped as off-topic
GRID = 9                 # probes per article in round 0
MAX_ROUNDS = 14
MIN_DELTA = 0.35         # stance change that counts
N_CLAIMS = 14
LEAVES = 64              # evidence tree: paragraphs are merged down to at most this many spans
WORDS = {2: "stated as fact", 1: "stated with attribution", 0: "not addressed", -1: "presented as disputed", -2: "denied"}
MONTHS = "January February March April May June July August September October November December".split()

CLAIMS_SYSTEM = (
    "You prepare a study of how Wikipedia articles changed their position over time. You get a reader's question, and for "
    "several Finnish Wikipedia articles an early version, the current version and passages that editors fought over. "
    f"Write {N_CLAIMS} claims to track. Each claim is one short, self-contained declarative sentence in English that an "
    "article version can state as fact, attribute to someone, present as disputed, deny, or not mention. Choose claims where "
    "the versions or the fought-over passages show a difference in substance, interpretation, blame, naming, numbers or "
    "emphasis, and that matter for the question. Prefer contested interpretations over trivia. Do not write claims about the "
    "form of the article (length, sources, structure). Do not write two claims that say the same thing; a claim and its "
    "opposite count as the same thing. Most claims should be answerable from several of the articles. "
    'Return JSON: {"claims": [{"id": "c1", "label": "at most 6 words", "text": "the claim", "theme": "one or two words"}]}'
)


def _bucket(v):
    s, p = v
    if p < 0.5:
        return 0
    return 2 if s > 0.6 else 1 if s > 0.15 else -2 if s < -0.6 else -1 if s < -0.15 else 0


def _differs(a, b):
    return _bucket(a) != _bucket(b) and abs(a[0] - b[0]) >= MIN_DELTA


def _month(iso):
    return f"{MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"


def _headline(label, before, after, t):
    a, b = WORDS[_bucket(before)], WORDS[_bucket(after)]
    return f"{label}: {a} until {_month(t)}, {b} since"


def _trim(paragraphs, n=3, chars=700):
    out = [p if len(p) <= chars else p[:chars] + "…" for p in paragraphs[:n]]
    if len(paragraphs) > n:
        out.append(f"(+{len(paragraphs) - n} more paragraphs)")
    return out


def retrieve_stable(question, tries=3, sources=12):
    """rag.retrieve, but the nearest articles of several fresh connections are merged. With the graph cache of MariaDB's vector
    index smaller than the index (mhnsw_max_cache_size, 16 MB), each connection reads a slightly different graph and the very
    nearest article is sometimes missing; the best distance then decides whether the question is on topic, so it must not flicker."""
    vector, t_embed = rag.timed_embed(question)
    merged, sql, t_search = {}, "", 0.0
    for _ in range(tries):
        arts, ms, sql = rag.search(vector, None, sources)
        t_search += ms
        for a in arts:
            k = a.get("wikidata_id") or (a["lang"], a["title"])
            if k not in merged or a["distance"] < merged[k]["distance"]:
                merged[k] = a
    found = sorted(merged.values(), key=lambda a: a["distance"])[:sources]
    return found, dict(embed_ms=t_embed, search_ms=t_search, searches=tries), sql


def exact_nearest(question, sources=12):
    """The nearest articles by an exact scan of article_vector (no index): slow but not subject to the graph-cache effect."""
    vector = rag.embed(question)
    sql = rag.SEARCH_SQL.format(where="").replace("FROM article_vector", "FROM article_vector IGNORE INDEX (embedding)")
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, [str(vector), rag.CANDIDATES])
        rows = cur.fetchall()
    finally:
        conn.close()
    seen, out = set(), []
    for l, title, url, wid, d, text, pid in rows:
        k = wid or (l, title)
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(lang=l, title=title, url=url, distance=float(d), text=text, page_id=pid, wikidata_id=wid))
    return out[:sources]


def pick_articles(question, want=5):
    """The usual retrieval, then keep the topics that have a Finnish edit history. Returns (articles, timings, sql)."""
    found, timings, sql = retrieve_stable(question)
    if not found:
        raise RuntimeError("no article was found")
    best = found[0]["distance"]
    if best > NO_MATCH:         # about to refuse: make sure the index did not simply miss the nearest article (an exact scan takes ~3 s)
        exact = exact_nearest(question)
        if exact and exact[0]["distance"] < best:
            found, best = exact, exact[0]["distance"]
            timings["exact_scan"] = True
    if best > NO_MATCH:
        raise RuntimeError(f"None of the articles is close enough to this question (closest distance {best:.2f}, limit {NO_MATCH}). "
                           "The stored articles are about the history of Finland: try a question about that.")
    near = [a for a in found if a["distance"] <= best + MARGIN]
    timings["dropped_far"] = len(found) - len(near)
    near = near[:10]
    try:        # a yes/no check per candidate (cached): distance alone keeps articles that only share the country or the period
        ps = tb.pair_check("relevant", RELEVANT, [(a["text"][:1500], question) for a in near])
        kept = [a for a, p in zip(near, ps) if p >= RELEVANT_MIN]
        for a, p in zip(near, ps):
            a["p_relevant"] = round(p, 3)
        if not kept:
            raise RuntimeError("None of the closest articles helps answer this question (each was checked: "
                               + ", ".join(f"{a['title']} {a['p_relevant']:.2f}" for a in near[:4]) + "). Try another wording or a question about the history of Finland.")
        timings["dropped_far"] += len(near) - len(kept)
        near = kept
    except RuntimeError:
        raise
    except Exception:       # the check failing must not break the page: fall back on the distance filter
        pass
    out, seen = [], set()
    for a in near:
        pid = history.resolve_fi(a)
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        out.append(dict(a, fi_page_id=pid))
        if len(out) == want:
            break
    return out, timings, sql


TITLE_SQL = ("SELECT lang, page_id, wikidata_id, title, url FROM article WHERE title = ? {lang}"
             "ORDER BY FIELD(lang, 'fi', 'sv', 'en') LIMIT 1")


def exact_article(q, lang=None):
    """When q is the exact title of a stored article (what the browser extension sends for the page in the tab), drift is run on
    that one article instead of searching: a bare title is too short to be close to any article vector.
    Returns (articles, timings, sql) like pick_articles, or None when q is not a title."""
    sql = TITLE_SQL.format(lang="AND lang = ? " if lang else "")
    t0 = time.perf_counter()
    conn = rag.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, (q, lang) if lang else (q,))
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    a = dict(zip(("lang", "page_id", "wikidata_id", "title", "url"), row), distance=0.0)
    pid = history.resolve_fi(a)
    if pid is None:
        raise RuntimeError(f"\"{a['title']}\" has no Finnish edit history in the dataset. Opinion drift reads the versions of the "
                           "Finnish article; try a question instead of a title.")
    return [dict(a, fi_page_id=pid)], dict(embed_ms=0, search_ms=(time.perf_counter() - t0) * 1000, by_title=True), sql


def _stable(page_id):
    """Versions worth probing: not undone within a day, and with some text."""
    return [r for r in history.revisions(page_id) if not r["reverted"] and r["bytes"] > 200]


def _grid(revs, n=GRID):
    """Indexes of n versions: first, last, and the rest at equal shares of cumulative edit volume."""
    if len(revs) <= n:
        return list(range(len(revs)))
    cum, total = [0], 0
    for a, b in zip(revs, revs[1:]):
        total += abs(b["bytes"] - a["bytes"]) + 20
        cum.append(total)
    picks, j = {0, len(revs) - 1}, 0
    for k in range(1, n - 1):
        target = total * k / (n - 1)
        while cum[j] < target:
            j += 1
        picks.add(j)
    return sorted(picks)


_claims_ready = False


def cached_claims(question, arts):
    """make_claims keyed by the exact question in MariaDB, so a repeated question is fully cached."""
    global _claims_ready
    conn = rag.connect()
    try:
        cur = conn.cursor()
        if not _claims_ready:
            cur.execute("CREATE TABLE IF NOT EXISTS drift_claims (question_sha CHAR(40) NOT NULL PRIMARY KEY, "
                        "question TEXT NOT NULL, claims JSON NOT NULL, updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            _claims_ready = True
        sha = hashlib.sha1(question.strip().lower().encode()).hexdigest()
        cur.execute("SELECT claims FROM drift_claims WHERE question_sha = ?", (sha,))
        row = cur.fetchone()
        if row:
            return json.loads(row[0] if isinstance(row[0], str) else json.dumps(row[0]))
        claims = make_claims(question, arts)
        cur.execute("REPLACE drift_claims (question_sha, question, claims) VALUES (?,?,?)",
                    (sha, question.strip().lower(), json.dumps(claims, ensure_ascii=False)))
        conn.commit()
        return claims
    finally:
        conn.close()


def make_claims(question, arts):
    """One generative call: claims shared by all the articles. arts: [{title, page_id, revs}]."""
    parts = [f"QUESTION\n{question}"]
    for a in arts:
        revs = a["revs"]
        early = next((r for r in revs if r["bytes"] >= 2500), revs[0])
        block = [f"ARTICLE: {a['title']}",
                 f"EARLY VERSION ({early['t'][:10]})\n{history.text(a['page_id'], early['rev_id'])[:4000]}",
                 f"CURRENT VERSION ({revs[-1]['t'][:10]})\n{history.text(a['page_id'], revs[-1]['rev_id'])[:7000]}"]
        fights, all_revs = [], history.revisions(a["page_id"])
        for j, r in enumerate(all_revs):
            if r["is_revert"] and not r["anonymous"] and not all_revs[j - 1]["anonymous"] and len(fights) < 6:
                h = history.hunks(a["page_id"], r["rev_id"], all_revs[j - 1]["rev_id"])   # what the revert threw out
                gone = " / ".join(h["add"])[:400]
                if len(gone) > 60:
                    fights.append(f"({r['t'][:10]}) reverted text: {gone}")
        if fights:
            block.append("PASSAGES EDITORS FOUGHT OVER\n" + "\n".join(fights))
        parts.append("\n\n".join(block))
    body = {"systemInstruction": {"parts": [{"text": CLAIMS_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "\n\n=====\n\n".join(parts)}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4000, "responseMimeType": "application/json",
                                 "thinkingConfig": {"thinkingLevel": rag.THINKING}}}
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{rag.GEMINI_MODEL}:generateContent",
                      headers={"x-goog-api-key": rag._key()}, json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini returned {r.status_code}: {r.text[:300]}")
    raw = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    data = json.loads(raw)
    claims, seen = [], set()
    for i, c in enumerate(data.get("claims", data if isinstance(data, list) else [])):
        text = str(c.get("text", "")).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            claims.append(dict(id=f"c{i + 1}", label=str(c.get("label", text))[:60], text=text, theme=str(c.get("theme", ""))[:30]))
    if not claims:
        raise RuntimeError("no claims were produced")
    return claims[:N_CLAIMS + 2]


SUMMARY_SYSTEM = (
    "You write the short summary at the top of an 'opinion drift' page. A reader asked a question; the page traced how several "
    "Finnish Wikipedia articles changed their position on shared claims over their edit history. You get the question, the "
    "opening of each article as it stands now, each claim's current position per article, and the change points found "
    "(date, article, what changed). Write plain text in the language of the question, at most 110 words, in exactly two "
    "paragraphs. The first starts with 'ANSWER:' and says what the current articles say in answer to the question, citing "
    "articles as [1], [2]; if they do not really address the question, say so plainly and do not guess. The second starts "
    "with 'DRIFT:' and says whether the position on this has drifted: name the biggest change with its date and article, "
    "or say that no change point was found (and that this may be because the articles are only loosely related to the "
    "question). Use only the material given. Do not say who is right; report what the articles say."
)


def summarize(question, arts, claims, values, changes):
    """One Gemini call: what the current articles answer, and whether the position drifted. Returns text."""
    parts = [f"QUESTION\n{question}", "ARTICLES NOW"]
    for a in arts:
        parts.append(f"[{a['i'] + 1}] {a['title']} ({len(history.revisions(a['page_id']))} versions)\n"
                     f"{history.text(a['page_id'], a['revs'][-1]['rev_id'])[:1500]}")
    lines = []
    for c in claims:
        now = []
        for a in arts:
            vals = values[a["i"]][c["id"]]
            if vals:
                v = vals[max(vals)]
                if _bucket(v) != 0:
                    now.append(f"[{a['i'] + 1}] {WORDS[_bucket(v)]}")
        if now:
            lines.append(f"- {c['text']} -> " + "; ".join(now))
    parts.append("CURRENT POSITIONS ON CLAIMS\n" + ("\n".join(lines) or "(no article takes a position on any claim)"))
    ch = [f"- {c['t'][:10]} [{arts[c['a']]['i'] + 1}] {c['headline']}; edit by {c['user']}: {c['comment'][:100]}" for c in changes[:12]]
    parts.append("CHANGE POINTS FOUND (" + str(len(changes)) + ")\n" + ("\n".join(ch) or "(none)"))
    body = {"systemInstruction": {"parts": [{"text": SUMMARY_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": "\n\n".join(parts)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700, "thinkingConfig": {"thinkingLevel": rag.THINKING}}}
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{rag.GEMINI_MODEL}:generateContent",
                      headers={"x-goog-api-key": rag._key()}, json=body, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini returned {r.status_code}: {r.text[:200]}")
    return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()


def run(question, lang=None):
    """Yield (event, data) pairs: sources, claims, probes (per round), change, done. Raises on failure.
    A question that is exactly an article title (optionally in `lang`) runs on that article alone."""
    t0 = time.perf_counter()
    decisions.reset_stats()
    found, timings, sql = exact_article(question.strip(), lang) or pick_articles(question)
    if not found:
        raise RuntimeError("none of the closest articles has a Finnish edit history")
    arts = []
    for i, a in enumerate(found):
        pid = a["fi_page_id"]
        history.ensure_loaded(pid)
        revs = _stable(pid)
        if len(revs) < 2:
            continue
        arts.append(dict(i=len(arts), page_id=pid, title=revs[-1].get("title") or a["title"], revs=revs, found=a))
    if not arts:
        raise RuntimeError("the closest articles have too little edit history")
    for a in arts:      # Finnish title of the topic, from the history table
        conn = rag.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT title FROM article_version WHERE page_id = ?", (a["page_id"],))
            row = cur.fetchone()
            a["title"] = row[0] if row else a["found"]["title"]
        finally:
            conn.close()
    yield "sources", dict(sql=sql, timings=timings, articles=[
        dict(i=a["i"], page_id=a["page_id"], title=a["title"], url=f"https://fi.wikipedia.org/?curid={a['page_id']}",
             found_as=f"{a['found']['title']} ({a['found']['lang']})", distance=round(a["found"]["distance"], 3), p_relevant=a["found"].get("p_relevant"), n_revs=len(history.revisions(a["page_id"])),
             first=a["revs"][0]["t"], last=a["revs"][-1]["t"], activity=history.activity(a["page_id"]),
             wars=history.wars(a["page_id"])) for a in arts])

    claims = cached_claims(question, arts)
    by_id = {c["id"]: c for c in claims}
    yield "claims", dict(claims=claims)

    # values[article][claim] = {version index: [stance, presence]}
    values = [{c["id"]: {} for c in claims} for _ in arts]
    reported = set()
    found_changes = []

    def probe(jobs, rnd):
        """jobs: {(article, index): set of claim ids}. Scores them, stores the values, returns the probes event."""
        keys = sorted(jobs)
        work = []
        for ai, idx in keys:
            a = arts[ai]
            work.append((history.text(a["page_id"], a["revs"][idx]["rev_id"]), [by_id[c] for c in sorted(jobs[(ai, idx)])]))
        items = []
        for (ai, idx), res in zip(keys, decisions.score_many(work)):
            s = {}
            for cid, v in res.items():
                values[ai][cid][idx] = [v["stance"], v["presence"]]
                s[cid] = [v["stance"], v["presence"]]
            rev = arts[ai]["revs"][idx]
            items.append(dict(a=ai, rev_id=rev["rev_id"], t=rev["t"], s=s))
        return dict(round=rnd, items=items)

    all_ids = {c["id"] for c in claims}
    yield "probes", probe({(a["i"], idx): set(all_ids) for a in arts for idx in _grid(a["revs"])}, 0)

    for rnd in range(1, MAX_ROUNDS + 1):
        jobs, changes = {}, []
        for a in arts:
            ai, revs = a["i"], a["revs"]
            if rnd == 1:        # edit wars can hide a change that was undone: look inside them with every claim
                done = sorted(values[ai][claims[0]["id"]])
                for t0w, t1w in history.wars(a["page_id"]):
                    inside = [k for k, r in enumerate(revs) if t0w <= r["t"] < t1w]
                    if inside:
                        mid = inside[len(inside) // 2]
                        if mid not in done:
                            jobs.setdefault((ai, mid), set()).update(all_ids)
            for cid, vals in values[ai].items():
                idxs = sorted(vals)
                for lo, hi in zip(idxs, idxs[1:]):
                    if not _differs(vals[lo], vals[hi]):
                        continue
                    if hi - lo > 1:
                        jobs.setdefault((ai, (lo + hi) // 2), set()).add(cid)
                    elif (ai, cid, hi) not in reported:
                        reported.add((ai, cid, hi))
                        changes.append((ai, cid, lo, hi))
        for ai, cid, lo, hi in changes:
            a = arts[ai]
            before, after = a["revs"][lo], a["revs"][hi]
            h = history.hunks(a["page_id"], before["rev_id"], after["rev_id"])
            v0, v1 = values[ai][cid][lo], values[ai][cid][hi]
            change = {"a": ai, "claim": cid, "rev_before": before["rev_id"], "rev_after": after["rev_id"], "t": after["t"],
                      "from": v0, "to": v1, "user": after["user"], "comment": after["comment"],
                      "hunk": {"del": _trim(h["del"]), "add": _trim(h["add"])},
                      "headline": _headline(by_id[cid]["label"], v0, v1, after["t"])}
            found_changes.append(change)
            yield "change", change
        if not jobs:
            break
        yield "probes", probe(jobs, rnd)

    try:        # the summary is a bonus: if Gemini fails the page still has everything else
        yield "summary", dict(text=summarize(question, arts, claims, values, found_changes))
    except Exception as e:
        yield "summary", dict(text=None, error=f"{type(e).__name__}: {e}")
    st = decisions.stats()
    yield "done", dict(requests=st["requests"], tokens=st["tokens"], cache_hits=st["cache_hits"],
                       ms=round((time.perf_counter() - t0) * 1000))


# ---------------------------------------------------------------- evidence: where in the text does a claim sit?

def evidence(page_id, rev_id, claim):
    """Binary descent over paragraph spans. Each level is one request; only spans that answer yes are split further."""
    paras = [p for p in history.text(page_id, rev_id).split("\n\n") if p.strip()]
    if not paras:
        return dict(paragraph="", path=[], questions=0)
    step = -(-len(paras) // LEAVES)
    leaves = [paras[i:i + step] for i in range(0, len(paras), step)]
    numbered = "\n\n".join(f"[P{i + 1}] " + "\n".join(leaf) for i, leaf in enumerate(leaves))
    spans, path, asked = [(0, len(leaves))], [], 0
    while True:
        kids = []
        for lo, hi in spans:
            if hi - lo > 1:
                mid = (lo + hi) // 2
                kids += [(lo, mid), (mid, hi)]
        if not kids:
            break
        qs = {f"s{lo}_{hi}": {"type": "noul", "instructions":
                              f"Look only at the passages numbered [P{lo + 1}] to [P{hi}]. Do these passages state, attribute, "
                              f"dispute or deny the following claim, or give the facts it is about? Claim: {claim}"}
              for lo, hi in kids}
        ans = decisions.ask(numbered, qs)
        asked += len(qs)
        scored = sorted(((ans[f"s{lo}_{hi}"]["noul"], lo, hi) for lo, hi in kids), reverse=True)
        keep = [x for x in scored if x[0] >= 0.5][:2] or scored[:1]
        path.append([dict(span=[lo + 1, hi], p=round(p, 3)) for p, lo, hi in scored])
        done = [(lo, hi) for _, lo, hi in keep if hi - lo == 1]
        spans = [(lo, hi) for _, lo, hi in keep]
        if done and all(hi - lo == 1 for lo, hi in spans):
            break
    best = spans[0][0]
    return dict(paragraph="\n".join(leaves[best]), others=["\n".join(leaves[lo]) for lo, hi in spans[1:] if hi - lo == 1],
                path=path, questions=asked)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Why did the Finnish Civil War start in 1918?"
    for event, data in run(q):
        if event == "sources":
            for a in data["articles"]:
                print(f"[{a['i']}] {a['title']}  ({a['n_revs']} versions, {a['first'][:10]} to {a['last'][:10]}, wars {len(a['wars'])})")
        elif event == "claims":
            for c in data["claims"]:
                print(f"  {c['id']:4} {c['label']:40} {c['text']}")
        elif event == "probes":
            print(f"round {data['round']}: {len(data['items'])} probes, {sum(len(i['s']) for i in data['items'])} answers")
        elif event == "change":
            print(f"  CHANGE [{data['a']}] {data['headline']}  (rev {data['rev_after']} by {data['user']}: {data['comment'][:60]})")
        else:
            print(event, data)
