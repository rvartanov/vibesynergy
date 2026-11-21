"""#138: notice and push texts are cut cleanly, on a sentence or word boundary, with "…"."""

from __future__ import annotations

from vibesynergy.core.text import ELLIPSIS, clip

TEXT = (
    "Oranje: je herstel is nog niet terug. Morgen de rustige rit van 1u15 in zone 2. "
    "Je HRV ligt nog onder normaal, dus houd het rustig."
)


def test_a_text_that_fits_stays_whole():
    assert clip("Groen:  rustig   aan.", 240) == "Groen: rustig aan."


def test_cut_after_the_last_whole_sentence_that_fits():
    out = clip(TEXT, 100)
    kept = "Oranje: je herstel is nog niet terug. Morgen de rustige rit van 1u15 in zone 2."
    assert out == f"{kept} {ELLIPSIS}"
    assert len(out) <= 100


def test_cut_after_the_last_whole_word_when_no_sentence_fits():
    out = clip("onder normaal " * 30, 50)
    assert out.endswith(ELLIPSIS) and len(out) <= 50
    assert out.removesuffix(ELLIPSIS).split()[-1] in ("onder", "normaal")


def test_never_ends_on_a_comma_or_dash():
    out = clip("een, twee, drie, vier, vijf, zes, zeven, acht, negen, tien", 30)
    assert not out.removesuffix(ELLIPSIS).endswith((",", " "))
    assert len(out) <= 30


def test_one_word_longer_than_the_room_is_cut_inside():
    assert clip("x" * 20, 10) == "x" * 9 + ELLIPSIS
