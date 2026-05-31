"""Tests for app/services/title_utils.py"""
import pytest
from app.services.title_utils import (
    search_variations, stem_from_rom_name, significant_terms, title_is_relevant,
)


# ---------------------------------------------------------------------------
# stem_from_rom_name
# ---------------------------------------------------------------------------

def test_stem_strips_region_tags():
    assert "(USA)" not in stem_from_rom_name("Sonic the Hedgehog (USA, Europe).md")
    assert "(Europe)" not in stem_from_rom_name("Sonic the Hedgehog (USA, Europe).md")


def test_stem_strips_extension():
    result = stem_from_rom_name("Super Mario World.sfc")
    assert ".sfc" not in result


def test_stem_strips_revision_tags():
    result = stem_from_rom_name("Donkey Kong Country (Rev 1).sfc")
    assert "(Rev 1)" not in result
    assert "Donkey Kong Country" in result


def test_stem_handles_none():
    assert stem_from_rom_name(None) == ""  # should not raise


def test_stem_handles_empty():
    assert stem_from_rom_name("") == ""


def test_stem_handles_no_extension():
    result = stem_from_rom_name("Metroid")
    assert result  # should return something, not crash


# ---------------------------------------------------------------------------
# search_variations
# ---------------------------------------------------------------------------

def test_search_variations_returns_list():
    assert isinstance(search_variations("Sonic the Hedgehog"), list)


def test_search_variations_includes_original():
    title = "Sonic the Hedgehog"
    variations = search_variations(title)
    assert any(title.lower() in v.lower() for v in variations)


def test_search_variations_strips_subtitle():
    variations = search_variations("Castlevania: Symphony of the Night")
    # Should include a version without the subtitle
    assert any("Castlevania" in v and "Symphony" not in v for v in variations)


def test_search_variations_handles_empty():
    result = search_variations("")
    assert isinstance(result, list)


def test_search_variations_no_duplicates():
    variations = search_variations("Final Fantasy VII")
    assert len(variations) == len(set(v.lower() for v in variations))


# ---------------------------------------------------------------------------
# significant_terms / title_is_relevant — the "search == hunt" relevance filter
# ---------------------------------------------------------------------------

def test_significant_terms_drops_stopwords_and_short():
    # "don" survives ("Don't" → don/t, t is len 1); "the" is a stopword
    assert significant_terms("Pajama Sam: Don't Fear the Dark") == {
        "pajama", "sam", "don", "fear", "dark",
    }


def test_significant_terms_matches_hunter_kirby():
    assert significant_terms("Kirby's Return to Dream Land") == {
        "kirby", "return", "dream", "land",
    }


_PAJAMA = significant_terms("Pajama Sam: Don't Fear the Dark")


def test_relevant_exact_title():
    assert title_is_relevant("Pajama Sam: Don't Fear the Dark", _PAJAMA)


def test_relevant_rejects_sibling_game():
    # The reported bug: a DIFFERENT 'Pajama Sam' game must not show as a match.
    assert not title_is_relevant(
        "Pajama Sam - No Need to Hide When Its Dark Outside", _PAJAMA
    )


def test_relevant_rejects_unrelated_game():
    assert not title_is_relevant("Super Mario World", _PAJAMA)


def test_relevant_all_but_one_when_three_plus_words():
    kirby = significant_terms("Kirby's Return to Dream Land")  # 4 terms
    # missing only "land" → 3/4 present, accepted
    assert title_is_relevant("Kirby's Epic Return to Dream", kirby)
    # missing "return" AND "land" → 2/4 present, rejected
    assert not title_is_relevant("Kirby Adventure Dream", kirby)


def test_relevant_two_word_title_needs_both():
    terms = significant_terms("Mega Man")  # {mega, man}
    assert title_is_relevant("Mega Man 2", terms)
    assert not title_is_relevant("Mega Drive Collection", terms)


def test_relevant_empty_terms_accepts_anything():
    assert title_is_relevant("literally anything", set())
