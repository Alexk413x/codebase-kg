"""The description contract: what it rejects, and what scrubbing recovers."""

from __future__ import annotations

import pytest

from codebase_kg import clean
from codebase_kg.schema import MAX_DESCRIPTION


# --- problems --------------------------------------------------------
def test_clean_description_has_no_problems() -> None:
    assert clean.problems("Foreground Media3 MediaSessionService owning the player.") == []


@pytest.mark.parametrize(
    "text, expect",
    [
        ("Wires the feed (ACME-431).", "ticket refs"),
        ("Added 2026-07-12 for the digest.", "dates"),
        ("Live-verified: the tile rotates.", "change narrative"),
        ("The player no longer pauses on background.", "change narrative"),
        ("x" * (MAX_DESCRIPTION + 1), "too long"),
    ],
)
def test_problems_flags_each_violation(text: str, expect: str) -> None:
    assert any(expect in p for p in clean.problems(text))


def test_lowercase_dash_number_is_not_a_ticket() -> None:
    # `utf-8`, `media3-session` and friends must not read as tracker ids.
    assert clean.problems("Decodes utf-8 via media3-1 helpers.") == []


# --- scrub -------------------------------------------------------------------
def test_scrub_removes_ticket_ref_and_tidies_parens() -> None:
    out, left = clean.scrub("Picture-in-Picture wiring (ACME-432). Provided by the activity.")
    assert out == "Picture-in-Picture wiring. Provided by the activity."
    assert left == []


def test_scrub_keeps_function_call_parens() -> None:
    # `enableEdgeToEdge()` is a symbol, not debris left by an excision.
    out, _ = clean.scrub("`enableEdgeToEdge()`, collects theme prefs.")
    assert "enableEdgeToEdge()" in out


def test_scrub_drops_whole_history_sentences() -> None:
    out, _ = clean.scrub("Owns the player. Live-verified: screen-off keeps playing. Releases on idle.")
    assert out == "Owns the player. Releases on idle."


def test_scrub_removes_a_ticket_range_whole() -> None:
    # `ACME-305/306/308` is three tickets in the shorthand real graphs use.
    # Matching only the first left "(/306/308)" behind as debris.
    out, left = clean.scrub("Full video backend (ACME-305/306/308): presigned PUT URL.")
    assert out == "Full video backend: presigned PUT URL."
    assert left == []


def test_scrub_keeps_a_path_that_looks_like_a_range() -> None:
    # `v1/videos/306` is a route, not a ticket — no uppercase key in front of it.
    out, _ = clean.scrub("Calls `@POST v1/videos/306` on the backend.")
    assert "v1/videos/306" in out


def test_scrub_cleans_orphaned_comma_inside_parens() -> None:
    out, _ = clean.scrub("Video check (3 h, CONNECTED, ACME-431) runs periodically.")
    assert out == "Video check (3 h, CONNECTED) runs periodically."


def test_scrub_result_always_satisfies_the_contract() -> None:
    messy = (
        "Foreground service (ACME-433) added 2026-07-12. " + "Owns a single player. " * 40
    )
    out, left = clean.scrub(messy)
    assert left == []
    assert clean.problems(out) == []
    assert len(out) <= MAX_DESCRIPTION


def test_scrub_fills_the_budget_past_a_short_leading_sentence() -> None:
    # A bare label followed by the real content must not collapse to the label:
    # sentence-only truncation would throw away everything that matters.
    text = "Picture-in-Picture wiring. " + "The controller is provided by the activity " * 10
    out, _ = clean.scrub(text)
    assert out.startswith("Picture-in-Picture wiring.")
    assert "controller" in out
    assert len(out) <= MAX_DESCRIPTION


def test_scrub_empty_is_empty() -> None:
    assert clean.scrub("   ") == ("", [])


def test_scrub_leaves_already_clean_text_untouched() -> None:
    text = "Ranks the feed by freshness and per-source weight."
    assert clean.scrub(text) == (text, [])
