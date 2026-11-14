"""Which of English, Finnish or Swedish a short text is in, without a model or a library.

Counts common function words (and a few Finnish case endings, and the Swedish letter å). Built for
questions and short answers: it returns None when there is too little evidence, or the text is in
another language, so callers fall back to "answer in the question's language".
"""
import re

WORDS = {
    "en": set("""the a an and or of in on at to for with by from is are was were be been did does do
        who what when where why how which whom whose during after before between about into than
        that this these those it its they their not also had has have""".split()),
    "fi": set("""ja tai on oli ovat olivat ei mikä mitkä mitä kuka ketkä kenen milloin missä mistä
        mihin miksi miten kuinka paljonko kun joka jotka jonka se sen ne niiden hän he myös sekä
        vuonna aikana jälkeen ennen välillä kanssa että kuin mutta tämä nämä siitä sitä alkoi
        päättyi""".split()),
    "sv": set("""och eller är var varit vem vilka vilken vilket vad när var hur varför som det den
        de en ett i på av för med till från att inte också under efter före mellan om än men
        detta dessa hans hennes deras år blev""".split()),
}
# "var" is Swedish ("was"/"where"); it is not an English word here.
FI_ENDING = re.compile(r"\w{3,}(ssa|ssä|sta|stä|lla|llä|lta|ltä|lle|ksi|nsa|nsä|ssaan|ssään)$")
WORD = re.compile(r"[a-zåäöéü]+")


def detect(text: str) -> str | None:
    """'en', 'fi', 'sv' or None (unsure, or another language)."""
    text = re.sub(r"\[\d+\]", " ", text or "").lower()
    words = WORD.findall(text)
    if not words:
        return None
    score = {lang: sum(1 for w in words if w in vocab) for lang, vocab in WORDS.items()}
    score["fi"] += sum(1 for w in words if FI_ENDING.match(w)) * 0.5
    score["sv"] += text.count("å") * 1.0
    best = max(score, key=score.get)
    ranked = sorted(score.values(), reverse=True)
    # Need some evidence, and a clear lead over the runner-up.
    if ranked[0] < 1 or ranked[0] - ranked[1] < 0.5 or ranked[0] < 0.08 * len(words):
        return None
    return best
