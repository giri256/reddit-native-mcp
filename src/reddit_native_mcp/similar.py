"""Query planning for find_similar: turn one post into Reddit searches.

Reddit's search does not stem ('startups' misses 'startup') and long
queries return few hits, while short ones are noisy and its results vary
between identical calls. So a post becomes several short queries over its
most telling title terms (singularised, proper nouns first), and candidates
are ranked by how many queries agree. Posts sharing the same external link
are found with the `url:` operator, which is precise.
"""

from __future__ import annotations

import itertools
import re

_STOP = set(
    """a an the and or but if of to in on for with at by from as is are was were be been
    being it its this that these those i me my we our you your he she they them their his her
    what which who whom how why when where there here so than too very can could should would
    will just not no yes do does did done have has had having get gets got getting about into
    over under after before again more most some any all each every other such only own same
    up down out off now new also via vs per amp im ive dont doesnt cant wont isnt youre thats
    anyone someone something anything everyone really still even much many lot lots thing things
    way ways make makes made use using used need needs want wants like know think going giving
    give gives gave got take takes help please ask asks asked asking tell told say says said question thoughts guys today day days
    year years week weeks month months time first one two three""".split()
)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+'.-]*[A-Za-z0-9+]|[A-Za-z]")
_LINK_RE = re.compile(r"https?://[^\s)\]>\"']+")
_REDDIT_HOSTS = ("reddit.com", "redd.it", "redditmedia.com", "reddit.app.link")
MAX_TERMS = 4


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def title_terms(title: str, limit: int = MAX_TERMS) -> list[str]:
    """The most telling title terms: adjacent capitalised words become one
    phrase ('Claude Team'), stopwords and numbers go, plurals are
    singularised; proper nouns rank first, then longer words, in order."""
    words = _WORD_RE.findall(title or "")
    scored: list[tuple[int, int, str]] = []
    i = 0
    while i < len(words):
        w = words[i]
        cap = w[0].isupper()
        if cap and i + 1 < len(words) and words[i + 1][0].isupper() and i > 0:
            phrase = f"{w} {words[i + 1]}".lower()
            if not any(p in _STOP for p in phrase.split()):
                scored.append((3, i, phrase))
                i += 2
                continue
        base = singular(w.lower().strip("'.-").removesuffix("'s"))
        if base not in _STOP and len(base) >= 3 and not base.isdigit():
            score = (2 if cap and i > 0 else 1 if cap else 0) + (1 if len(base) >= 5 else 0)
            scored.append((score, i, base))
        i += 1
    in_phrases = {w for _, _, t in scored if " " in t for w in t.split()}
    seen: dict[str, None] = {}
    for _, _, term in sorted(scored, key=lambda t: (-t[0], t[1])):
        if term not in seen and (" " in term or term not in in_phrases):
            seen[term] = None
        if len(seen) == limit:
            break
    return list(seen)


def post_terms(post: dict, limit: int = MAX_TERMS) -> list[str]:
    """Title terms, topped up from the start of the body when the title says
    little (e.g. "Thank you Anthropic!")."""
    terms = title_terms(post.get("title") or "", limit)
    if len(terms) < 3 and post.get("body"):
        body = _LINK_RE.sub(" ", post["body"])[:400]
        for t in title_terms(body, limit):
            if len(terms) >= limit:
                break
            if t not in terms and not any(t in x.split() or x in t.split() for x in terms):
                terms.append(t)
    return terms


def queries(terms: list[str]) -> list[str]:
    """Every pair of terms, plus the top three together."""
    out = [" ".join(p) for p in itertools.combinations(terms, 2)]
    if len(terms) >= 3:
        out.append(" ".join(terms[:3]))
    return out or terms[:1]


def external_links(post: dict, limit: int = 3) -> list[str]:
    """The post's external link plus links in its body, as `url:` targets
    (scheme and tracking query stripped)."""
    found = [post.get("link_url") or ""] + _LINK_RE.findall(post.get("body") or "")
    out: list[str] = []
    for url in found:
        if not url:
            continue
        bare = re.sub(r"^https?://(www\.)?", "", url).split("?", 1)[0].split("#", 1)[0].rstrip("/.,")
        host = bare.split("/", 1)[0].lower()
        if not bare or any(host == h or host.endswith("." + h) for h in _REDDIT_HOSTS):
            continue
        if bare not in out:
            out.append(bare)
        if len(out) == limit:
            break
    return out
