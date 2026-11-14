"""Site map and the shared banner: two areas (Readers, Editors), the menu, and the "What is this page?" drawer.

Every page puts hub.BANNER right after <body>, so nothing in the pages themselves changes: the banner carries the text
for every page, and a small script puts a "What is this page?" button next to the page's <h1> and shows the matching text.
"""
import html
import json

SECTIONS = {  # key: (name, landing page, one-line description)
    "read": ("Readers", "/readers", "Explore what Wikipedia says about the history of Finland, and how the three languages and the years differ."),
    "edit": ("Editors", "/editors", "Find work to do on the Finnish Wikipedia: missing sections, missing references, missing links, conflicting facts."),
}

# path, section, nav label, card title, card text, guide
# guide = (in short, [how to use it], [what you see], how it works, [keep in mind])
PAGES = [
    ("/ask", "read", "Ask", "Ask Wikipedia",
     "Ask a question in English, Finnish or Swedish and get a short answer that cites the articles it comes from.",
     ("Ask a question in English, Finnish or Swedish. The page finds the articles that are closest in meaning and "
      "writes a short answer from them, with a numbered citation after each fact.",
      ["Type a question in any of the three languages, or click one of the examples.",
       "Optionally limit the sources to one language with the drop-down.",
       "Press <b>Ask</b>. The three steps tick off as they finish; the answer appears word by word."],
      ["<b>The answer</b>, with numbers like [2]. Click one to jump to that source.",
       "<b>Sources</b> (right): the five articles found. Bold ones are cited; the others were found but not used.",
       "<b>Timings</b> and, folded, <b>the SQL MariaDB ran</b>."],
      "The question is turned into a vector (a list of 1,024 numbers that captures its meaning) with bge-m3, the same model "
      "that made the stored article vectors. One SQL query asks MariaDB's vector index for the closest articles; versions of the "
      "same topic in several languages count once. The best five go to Gemini, which may only use those texts.",
      ["Each article has one vector, so the search finds the right <em>topic</em>, not a fact buried deep inside it.",
       "Only the first 12,000 characters of each article reach the AI.",
       "The answer is written by an AI. Check the sources before you rely on it."])),

    ("/drift", "read", "Opinion drift", "Opinion drift",
     "Travel through time: see how the closest Finnish articles changed their position on the same claims, edit by edit.",
     ("See how the Finnish Wikipedia's account of a subject changed over the years: which claims were stated as fact, "
      "hedged, disputed or denied, and the exact edit where that changed.",
      ["Ask a question, or click <b>replay the demo</b> to see a recorded run straight away.",
       "A live run takes a few minutes: it reads the full edit history of five articles and scores many old versions. "
       "The step list shows progress.",
       "Click a tick on a ribbon, a ribbon cell, or drag the vertical cursor."],
      ["<b>Ribbons</b>: one per claim and article, from the oldest version (left) to today (right). "
       "Colour = how the article treats the claim (see the legend); faint = the claim is barely there.",
       "<b>Ticks |</b> mark the exact edit where the position changed. Click one and the <b>Proof</b> panel shows the diff.",
       "<b>The cursor</b> shows the article text as it stood on that day."],
      "An AI writes a few claims that the five closest Finnish articles share. Each article is scored on every claim at nine "
      "points in its history; where the score changes between two points, a binary search over MariaDB's versioned history "
      "narrows it down to the one edit that did it.",
      ["Finnish articles only; a topic with no Finnish article is left out.",
       "Stances are judged by an AI. Open the linked diff before you quote it.",
       "A claim that appears and disappears between two checkpoints can be missed."])),

    ("/mirror", "read", "Language mirror", "Language mirror",
     "A map of how differently two languages tell the same topic. Click a line to read both articles with a note on what each leaves out.",
     ("Compare how two Wikipedias tell the same topics. The topics where the Finnish and English (or Swedish) articles "
      "differ most stand out, and you can read both side by side.",
      ["Pick the two languages at the top.",
       "Hover over the map, or scroll the <b>Widest gaps</b> list on the right (you can filter it by title).",
       "Click a line or a list entry to open both articles."],
      ["<b>The map</b>: every topic is two dots, one per language, joined by a line. A longer, darker line means the two "
       "articles are further apart in meaning.",
       "<b>The list</b>: topics ranked by gap. The number is the real gap; the map is a flattened picture of it.",
       "<b>The comparison</b>: both texts, and an AI note on what each says that the other leaves out."],
      "Topics are matched across languages by their Wikidata id. The gap is the distance between the two articles' vectors, "
      "computed for every topic in one SQL query. The dot positions come from squeezing all vectors down to two dimensions.",
      ["A big gap often means a different focus or length, not an error.",
       "The note is written by an AI from the two texts only."])),

    ("/neighbors", "read", "Neighbours", "Closest neighbours",
     "Pick an article and see its 10 nearest articles by meaning, in any language. Click one to hop onward.",
     ("Start from one article and walk through the ones closest to it in meaning, in any of the three languages, "
      "building a map of related topics as you go.",
      ["Type an article title in the box and pick it from the suggestions.",
       "Click any dot to centre it and add its own ten neighbours.",
       "Drag to move, scroll to zoom. Use the drop-down to stay in one language, and <b>Clear</b> to start again."],
      ["<b>Dots</b> are articles, coloured by language (blue Finnish, yellow Swedish, red English).",
       "<b>The right panel</b>: the selected article, its ten neighbours with their distance (smaller = closer), and the SQL."],
      "Each article has a vector that captures its meaning. For the article you pick, MariaDB's vector index returns the ten "
      "articles whose vectors are closest, computed live.",
      ["Close in meaning is not the same as linked, or about the same event: articles on similar kinds of topics also end up close."])),

    ("/stats", "read", "Numbers", "The numbers",
     "How many articles we have, per language, and how many topics exist in two or three languages.",
     ("What is in the database: how many articles per language, how big they are, how recent, and how many topics "
      "exist in more than one language.",
      ["Nothing to do: read the tables. The counts are live from MariaDB."],
      ["<b>Big numbers</b>: articles, vectors, distinct topics, categories.",
       "<b>Per language</b>: article count, total characters, oldest and newest edit.",
       "<b>Shared topics</b>: how many topics exist in each pair of languages, and in all three."],
      "A topic is one Wikidata id; its Finnish, Swedish and English articles share it.",
      ["The collection covers the history of Finland, not the whole Wikipedia."])),

    ("/gaps", "edit", "Gaps", "Cross-language gaps",
     "Which sections of the Swedish or English article have nothing like them in the Finnish one, and the reverse. No translation, no AI.",
     ("Find sections that one language's article has and another's lacks: ready-made ideas for expanding an article, "
      "with the text to start from.",
      ["Type a topic in any language and press <b>Find topics</b>.",
       "Click a topic, then choose which language to compare with which (for example Swedish → Finnish).",
       "Press <b>Show missing sections</b>."],
      ["<b>Pairs of boxes</b>: on the left a section of the first article; on the right the closest thing the second article has.",
       "<b>Red distance</b> (above 0.40): very probably missing; 6 of 7 such sections were in a hand check.",
       "Folded: sections that <b>may be missing</b> (0.32 to 0.40, about one in three is) and sections that <b>do have a counterpart</b>."],
      "Both articles are cut into sections, each with its own vector. The vector model reads all three languages, so nothing "
      "is translated: each section is matched with the closest section of the other article, and a large distance means there "
      "is nothing like it. No AI writes or judges anything here.",
      ["The content may exist in another Finnish article. Search before you write.",
       "Bring sources: the other language's references are a good place to start."])),

    ("/refs", "edit", "References", "Reference borrower",
     "A Finnish paragraph without a source: find the same fact in another article and offer the reference it uses.",
     ("For a Finnish paragraph with no reference, find a paragraph in another article that states the same fact "
      "<em>and</em> has a reference, and show that reference as a lead.",
      ["Type a Finnish article title and press <b>Find</b>. Each result says how many of its paragraphs lack a reference.",
       "Open an article to list its unreferenced paragraphs.",
       "Press <b>Find a reference</b> under one of them."],
      ["<b>Candidates</b>: paragraphs from other articles, each with the reference it uses.",
       "<b>same fact</b> (green): an AI yes/no check agreed the two say the same thing. <b>not confirmed</b>: it did not."],
      "Every paragraph has a vector. MariaDB finds the closest paragraphs in other articles that carry a reference; an AI then "
      "answers one question for each: do these two paragraphs state the same fact? Wikipedia cannot cite itself, so the page "
      "borrows the source the other article already uses.",
      ["This is a lead, not a citation: open the source and check that it really supports the sentence.",
       "Scope for now: Finnish articles in the category tree “Suomen sodat”."])),

    ("/links", "edit", "Links", "Link suggester",
     "Articles that are close in meaning but not linked, the paragraph where the link fits, and the least-linked articles.",
     ("Find links that are missing between Finnish articles, and the paragraph where each link would fit.",
      ["<b>Close but unlinked</b>: type an article title, press <b>Find</b>, click the article.",
       "<b>Fewest inbound links</b>: see the articles that almost nothing links to; click one."],
      ["<b>Close but unlinked</b>: articles close in meaning with no link either way, and for each the best paragraph "
       "on both sides to add the link.",
       "<b>Fewest inbound links</b>: five paragraphs in other articles that could link to it.",
       "<b>names it</b> (green): the paragraph already mentions the other article's title, so the link is one edit away."],
      "Close articles come from MariaDB's vector index; existing links were read from each article's newest wikitext; "
      "the best paragraph is the one whose vector is closest to the other article.",
      ["Scope: Finnish articles in the category tree “Suomen sodat”. Links from articles outside it are unknown, "
       "so “inbound” means inbound from within that set.",
       "Close in meaning does not always mean a link helps the reader. Use judgement."])),

    ("/consistency", "edit", "Consistency", "Consistency checker",
     "Two articles, one fact, two values? Both passages side by side, never a verdict.",
     ("Find places where two articles give different values for the same fact (a date, a number, a name), so an editor "
      "can check which is right.",
      ["Choose the language, type an article title and press <b>Find</b>, then click the article.",
       "Press <b>Check this section</b> under a section.",
       "Open <b>Flagged pairs found so far</b> to see what every check has turned up."],
      ["<b>Pairs</b>: the section you chose, and each close section from another article.",
       "<b>may differ</b> (red): the AI thinks they disagree on a fact. <b>worth a look</b>: less sure. <b>no conflict found</b>: nothing spotted."],
      "MariaDB finds the sections of other articles, in the same language, that are closest in meaning. An AI answers one "
      "question for each pair: do they give incompatible values for the same fact? Answers are kept, so a pair is never checked twice.",
      ["The page never says which side is right. Settle it with sources.",
       "“may differ” is right about as often as not, and most real differences score lower. Treat both levels as leads."])),
]

READERS = [p for p in PAGES if p[1] == "read"]
EDITORS = [p for p in PAGES if p[1] == "edit"]
BY_SECTION = {"read": READERS, "edit": EDITORS}


def _guide(path, title, g):
    short, use, see, how, mind = g
    li = lambda xs: "".join(f"<li>{x}</li>" for x in xs)
    return (f'<div class="gd-p" data-p="{path}" hidden><h2>{html.escape(title)}</h2><p class="gd-lead">{short}</p>'
            f'<h3>How to use it</h3><ol>{li(use)}</ol><h3>What you see</h3><ul>{li(see)}</ul>'
            f'<h3>How it works</h3><p>{how}</p><h3>Good to know</h3><ul>{li(mind)}</ul></div>')


def _subnav(key):
    return (f'<div class="bn-sub" data-s="{key}" hidden><div class="in">'
            + "".join(f'<a href="{p[0]}">{p[3]}</a>' for p in BY_SECTION[key]) + "</div></div>")


SECTION_OF = {p[0]: p[1] for p in PAGES} | {v[1]: k for k, v in SECTIONS.items()}

BANNER = """<style>
.bn,.gd,.gd-btn{--bn-bg:#fff;--bn-fg:#202122;--bn-muted:#54595d;--bn-line:#a2a9b1;--bn-soft:#eaecf0;--bn-card:#f8f9fa;--bn-link:#3366cc;
 font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;box-sizing:border-box}
@media(prefers-color-scheme:dark){.bn,.gd,.gd-btn{--bn-bg:#101418;--bn-fg:#eaecf0;--bn-muted:#a2a9b1;--bn-line:#54595d;--bn-soft:#27292d;--bn-card:#1b1f23;--bn-link:#88a3e8}}
.bn{border-bottom:1px solid var(--bn-line);background:var(--bn-bg);color:var(--bn-fg)}
.bn .in{max-width:1400px;margin:0 auto;padding:10px clamp(16px,3vw,48px);display:flex;align-items:center;gap:14px 28px;flex-wrap:wrap}
.bn a{color:var(--bn-link);text-decoration:none}.bn a:hover{text-decoration:underline}
.bn .home{display:flex;align-items:center;gap:12px;color:var(--bn-fg)}.bn .home:hover{text-decoration:none}
.bn .logo{width:44px;height:44px;flex:none;border-radius:50%;background:radial-gradient(circle at 50% 50%,var(--bn-bg) 0 28%,transparent 29%),conic-gradient(var(--bn-fg) 0 12%,var(--bn-soft) 0 25%,var(--bn-fg) 0 37%,var(--bn-soft) 0 50%,var(--bn-fg) 0 62%,var(--bn-soft) 0 75%,var(--bn-fg) 0 87%,var(--bn-soft) 0)}
.bn b{display:block;font:400 1.55rem/1.1 Georgia,serif}.bn small{font-size:.72rem;color:var(--bn-muted)}
.bn-top{margin-left:auto;display:flex;gap:26px;font-size:15px}
.bn-top a[aria-current],.bn-sub a[aria-current]{color:var(--bn-fg);font-weight:600;box-shadow:inset 0 -2px 0 currentColor}
.bn-sub{border-top:1px solid var(--bn-soft);background:var(--bn-card)}
.bn-sub .in{padding-top:6px;padding-bottom:6px;gap:6px 22px;font-size:13.5px}
.gd-btn{display:inline-block;vertical-align:middle;margin-left:14px;padding:2px 10px;font-size:13px;font-weight:400;line-height:1.5;color:var(--bn-link);
 background:var(--bn-bg);border:1px solid var(--bn-line);border-radius:12px;cursor:pointer;white-space:nowrap}
.gd-btn:hover{border-color:var(--bn-link)}
.gd-bg{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.25);opacity:0;visibility:hidden;transition:opacity .2s,visibility .2s}
.gd{position:fixed;top:0;right:0;bottom:0;z-index:21;width:min(420px,100vw);overflow-y:auto;background:var(--bn-bg);color:var(--bn-fg);
 border-left:1px solid var(--bn-line);box-shadow:-4px 0 18px rgba(0,0,0,.15);padding:22px 26px 32px;transform:translateX(100%);visibility:hidden;transition:transform .22s ease,visibility .22s}
html.gd-open .gd{transform:none;visibility:visible}html.gd-open .gd-bg{opacity:1;visibility:visible}
.gd-x{position:absolute;top:12px;right:14px;font:20px/1 sans-serif;color:var(--bn-muted);background:none;border:0;padding:4px 8px;cursor:pointer}
.gd h2{font:400 1.5rem/1.25 Georgia,serif;margin:0 30px 10px 0;padding:0;border:0;background:none;text-align:left;color:var(--bn-fg)}
.gd h3{font-size:14px;font-weight:600;margin:20px 0 6px;color:var(--bn-fg)}
.gd-lead{font-size:15px;margin:0}.gd p{margin:0}.gd ol,.gd ul{margin:0;padding-left:1.3em}.gd li{margin:5px 0;padding:0;border:0;list-style:revert}
.gd b{display:inline;font:inherit;font-weight:600}
</style><header class="bn"><div class="in"><a class="home" href="/" title="Home"><div class="logo" aria-hidden="true"></div>
<div><b>Wikipedia</b><small>History of Finland · powered by MariaDB Vector</small></div></a>
<nav class="bn-top" aria-label="Sections"><a href="/">Home</a>""" + "".join(f'<a href="{v[1]}">{v[0]}</a>' for v in SECTIONS.values()) + \
    "</nav></div>" + _subnav("read") + _subnav("edit") + """</header>
<div class="gd-bg" id="gd-bg"></div><div class="gd" id="gd" role="dialog" aria-modal="true" aria-label="What is this page?">
<button type="button" class="gd-x" id="gd-x" title="Close (Esc)">&times;</button>""" + \
    "".join(_guide(p[0], p[3], p[5]) for p in PAGES) + """</div>
<script>(function(){var d=document,h=d.documentElement,p=location.pathname.replace(/\\/+$/,'')||'/',S=""" + \
    json.dumps(SECTION_OF) + """,s=S[p];
d.querySelectorAll('.bn a').forEach(function(a){var x=a.getAttribute('href');if(x===p||(s&&x===({read:'/readers',edit:'/editors'})[s]))a.setAttribute('aria-current','page')});
if(s)d.querySelector('.bn-sub[data-s="'+s+'"]').hidden=false;
var box=d.querySelector('.gd-p[data-p="'+p+'"]');if(!box)return;box.hidden=false;
function open(o){h.classList.toggle('gd-open',o)}
d.getElementById('gd-x').onclick=d.getElementById('gd-bg').onclick=function(){open(false)};
addEventListener('keydown',function(e){if(e.key==='Escape')open(false)});
d.addEventListener('DOMContentLoaded',function(){var t=d.querySelector('main h1, .tool h1, .bar h1, h1');if(!t)return;
var b=d.createElement('button');b.type='button';b.className='gd-btn';b.textContent='? What is this page?';b.onclick=function(){open(true)};t.appendChild(b);if(location.hash==='#about')open(true)});})();</script>"""


def cards(pages):
    return "".join(f'<a class="card" href="{p[0]}"><h3>{p[3]}</h3>{p[4]}</a>' for p in pages)
