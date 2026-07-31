"""The description contract, and the scrubber that enforces it. Stdlib only.

A node `description` says **what the component is and what it does** — present
tense, source-derived, one or two clauses. That is genuinely useful and cannot
be recovered by grepping.

What it must *not* carry is the content that made the old `summary` field a
maintenance liability: ticket ids, dates, and change narrative. All three
duplicate git and the issue tracker, and all three go stale the moment anyone
touches the code — which is exactly what happened (the live Android graph
carried 171 ticket refs and sentences like "ACME-433 removed the feed's
lifecycle ON_PAUSE pause entirely").

`check()` is the gate — the writer refuses a description that violates it.
`scrub()` is the best-effort repair used by migration, which strips what it can
prove is history and reports whatever it cannot fix so an agent rewrites it.
"""

from __future__ import annotations

import re

from .schema import MAX_DESCRIPTION

# A tracker id: ACME-431, PROJ-1, ABC-1234. Two-plus uppercase alnum, a dash, digits.
# The trailing `(?:/\d+)*` catches the range shorthand real graphs use —
# `ACME-305/306/308` is three tickets, and matching only the first left `(/306/308)`
# behind as debris.
_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+(?:/\d+)*\b")

# Dates in the shapes that show up in hand-written prose.
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})\b"
)

# Change narrative — a description is a statement about the present, so anything
# that positions the code against its own past belongs in the commit, not here.
_HISTORY = re.compile(
    r"(?i)\b("
    r"live-verified|verified live|now\s+(?:sends?|sent|uses?|lives?|returns?|handles?)"
    r"|previously|used to|no longer|formerly|as of\b"
    r"|was\s+(?:renamed|moved|replaced|removed|added)"
    r"|(?:re)?named\s+from|migrated\s+from"
    r"|(?:newly|recently)\s+added|added\s+in\b|removed\s+in\b|introduced\s+in\b"
    r"|fixes?\s+the\b|fixed\s+(?:a|the)\b|regression"
    r")"
)

# Sentence split that keeps abbreviations and decimals intact well enough for
# prose repair. Splits on . ! ? followed by whitespace + a capital or backtick.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[`A-Z*])")

_WS = re.compile(r"\s+")
# Punctuation left dangling after a mid-sentence excision, e.g. "(, ACME-1)".
# The `(?<!\w)` guard on the empty-parens case matters: `enableEdgeToEdge()` is
# a symbol name, not debris, and must survive intact.
_ORPHAN_PUNCT = re.compile(
    r"(?<!\w)\(\s*[,;]?\s*\)|\[\s*\]|\s+([,;.:!?])|(?<=\()\s*[,;]\s*|,\s*(?=[,;)])"
)


class DescriptionError(ValueError):
    """A description violates the contract and the writer must not store it."""


def problems(text: str) -> list[str]:
    """Every contract violation in `text`, as human-readable reasons.

    Empty list == the description is acceptable. Used by `check()` and reported
    verbatim by migration and `kg_validate`.
    """
    issues: list[str] = []
    if len(text) > MAX_DESCRIPTION:
        issues.append(f"too long: {len(text)} chars (max {MAX_DESCRIPTION})")
    for rx, label in (
        (_TICKET, "ticket refs (belongs in git/tracker)"),
        (_DATE, "dates (belongs in git)"),
        (_HISTORY, "change narrative (belongs in git)"),
    ):
        hits = sorted({m.lower() if rx is _HISTORY else m for m in rx.findall(text)})
        if hits:
            issues.append(f"{label}: " + ", ".join(hits))
    return issues


def check(text: str) -> None:
    """Raise `DescriptionError` if `text` violates the contract."""
    issues = problems(text)
    if issues:
        raise DescriptionError("; ".join(issues))


def node_problems(node: object) -> list[str]:
    """Every reason `node` could not be stored, as human-readable strings.

    The single Python expression of the node contract. Three callers need the
    same rules for different purposes and must not drift:

    - `writer._validate` raises, naming the node — so an authoring agent gets
      "node 'a': parity=matched requires a counterpart" instead of a bare
      `sqlite3.IntegrityError` from a CHECK constraint it cannot see.
    - `migrate` normalizes and reports.
    - `kg_validate` reports.

    The DDL keeps the same rules as constraints. That is deliberate: the schema
    is the backstop that makes bad data unwritable even by hand, and this is the
    layer that explains *why* before the write is attempted.
    """
    issues: list[str] = []
    node_id = getattr(node, "id", "")
    if not str(node_id).strip():
        issues.append("empty id")
    if not str(getattr(node, "kind", "")).strip():
        issues.append("no kind")
    issues += problems(str(getattr(node, "description", "")))

    for anchor in getattr(node, "anchors", []):
        if not anchor.path:
            issues.append("an anchor has no path")
        elif anchor.symbol and anchor.symbol[:1].isdigit():
            issues.append(
                f"anchor '{anchor}' looks like a line number — SCHEMA.md §4.1 requires a symbol"
            )

    issues += parity_problems(
        getattr(node, "parity", None),
        getattr(node, "counterpart", None),
        getattr(node, "divergence", None),
    )
    return issues


def parity_problems(
    parity: str | None, counterpart: str | None, divergence: str | None
) -> list[str]:
    """The three legal parity shapes (SCHEMA.md §9), as reasons.

    Mirrors the CHECK constraints in `schema.py` exactly — see `node_problems`
    for why both exist.
    """
    issues: list[str] = []
    if parity is None:
        if counterpart:
            issues.append("counterpart set with no parity flag")
        if divergence:
            issues.append("divergence set with no parity flag")
        return issues

    if parity not in {"matched", "divergent"} and not parity.endswith("-only"):
        return [f"unrecognized parity '{parity}' (expected matched, divergent, or <codebase>-only)"]

    if parity.endswith("-only"):
        if counterpart:
            issues.append(f"parity={parity} must not have a counterpart")
        if divergence:
            issues.append(f"parity={parity} must not have a divergence line")
    elif parity == "matched":
        if not counterpart:
            issues.append("parity=matched requires a counterpart")
        if divergence:
            issues.append("parity=matched must not have a divergence line")
    else:  # divergent
        if not counterpart:
            issues.append("parity=divergent requires a counterpart")
        if not divergence:
            issues.append("parity=divergent requires a divergence line")
    return issues


def _tidy(text: str) -> str:
    # Excisions leave debris that itself creates new debris — removing a ticket
    # ref from "wiring (ACME-432)." leaves "wiring ().", and dropping the empty
    # parens then leaves a floating space before the period. One pass cannot see
    # the second problem, so run to a fixed point.
    for _ in range(4):
        cleaned = _ORPHAN_PUNCT.sub(lambda m: m.group(1) or "", text)
        cleaned = _WS.sub(" ", cleaned).strip()
        if cleaned == text:
            break
        text = cleaned
    text = re.sub(r"^[,;:.\-–—]\s*", "", text)
    return text.strip(" ,;:")


def scrub(text: str, *, max_len: int = MAX_DESCRIPTION) -> tuple[str, list[str]]:
    """Best-effort repair. Returns `(cleaned, unresolved_problems)`.

    Removes what can be proven to be history — ticket refs, dates, and whole
    sentences whose subject is a change rather than the code — then trims to the
    leading sentences that fit `max_len`. It never invents text: if what remains
    still violates the contract, the reasons come back in the second element and
    the caller is expected to have an agent rewrite the description from source.
    """
    if not text.strip():
        return "", []

    # Sentences that exist to narrate a change go entirely; a sentence that
    # merely mentions a ticket keeps its content minus the ref.
    kept = [s for s in _SENTENCE.split(text) if not _HISTORY.search(s)]
    cleaned = " ".join(kept) if kept else text

    cleaned = _TICKET.sub("", cleaned)
    cleaned = _DATE.sub("", cleaned)
    cleaned = _tidy(cleaned)

    if len(cleaned) > max_len:
        cleaned = _truncate_to_sentence(cleaned, max_len)

    return cleaned, problems(cleaned)


def _word_cut(text: str, budget: int) -> str:
    """`text` cut to `budget` chars on a word boundary, ellipsized."""
    if budget < 8:
        return ""
    cut = text[: budget - 1].rsplit(" ", 1)[0].rstrip(" ,;:—–-")
    return (cut + "…") if cut else ""


def _truncate_to_sentence(text: str, max_len: int) -> str:
    """Fit `text` into `max_len`, preferring whole sentences.

    Keeps complete leading sentences while they fit, then spends whatever budget
    is left on a word-boundary cut of the next one. Stopping at the last whole
    sentence alone is not good enough: these summaries often open with a short
    label and carry the actual content in a long second sentence, so a
    sentence-only rule can discard nearly everything.
    """
    sentences = _SENTENCE.split(text)
    out = ""
    idx = 0
    for i, sentence in enumerate(sentences):
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > max_len:
            break
        out, idx = candidate, i + 1

    remainder = " ".join(sentences[idx:]).strip()
    if not remainder:
        return out
    # -1 leaves room for the space joining the kept prefix to the fragment.
    budget = max_len - len(out) - (1 if out else 0)
    fragment = _word_cut(remainder, budget)
    if not fragment:
        return out or _word_cut(text, max_len)
    return f"{out} {fragment}".strip()
