"""Tests for app/services/title_utils.py"""
import pytest
from app.services.title_utils import search_variations, stem_from_rom_name


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
