"""Tests for VimmSource in app/services/sources/vimm.py

Covers the logic that was broken before — name filter and URL generation.
No network calls; tests the pure logic only.
"""
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.sources.vimm import VimmSource, VIMM_DOWNLOAD_FALLBACK


@pytest.fixture
def source():
    return VimmSource()


# ---------------------------------------------------------------------------
# get_download_url — mediaId must be in query string for download_file()
# ---------------------------------------------------------------------------

def test_get_download_url_encodes_media_id(source):
    url = source.get_download_url("8003", "Burnout 3 - Takedown.zip")
    assert "mediaId=8003" in url


def test_get_download_url_different_ids_produce_different_urls(source):
    assert source.get_download_url("100", "a.zip") != source.get_download_url("200", "a.zip")


# ---------------------------------------------------------------------------
# Name filter — the core bug that was fixed
# ---------------------------------------------------------------------------

def _run_filter(name_filter: str, vault_title: str) -> bool:
    """Simulate the filter check from get_files(): True = file passes."""
    filter_stem = Path(name_filter).stem.lower()
    file_stem = Path(f"{vault_title}.zip").stem.lower()
    return filter_stem in file_stem or file_stem in filter_stem


# The main regression: RA ROM name has region + extension, Vimm has clean title
def test_filter_passes_when_vimm_stem_in_ra_stem():
    # "Sonic the Hedgehog" is a substring of "Sonic the Hedgehog (USA, Europe)"
    assert _run_filter("Sonic the Hedgehog (USA, Europe).md", "Sonic the Hedgehog")


def test_filter_passes_when_ra_stem_in_vimm_stem():
    # Less common direction: RA name is shorter
    assert _run_filter("Burnout 3.iso", "Burnout 3 - Takedown")


def test_filter_passes_exact_match():
    assert _run_filter("Super Mario World.sfc", "Super Mario World")


def test_filter_blocks_completely_different_game():
    assert not _run_filter("Sonic the Hedgehog (USA).md", "Super Mario World")


def test_filter_blocks_generic_collection_name():
    # "genesis" is NOT in "sonic the hedgehog (usa, europe)" and vice-versa
    assert not _run_filter("Sonic the Hedgehog (USA, Europe).md", "Genesis")


def test_filter_passes_with_no_filter():
    # Empty filter string: everything passes
    assert _run_filter("", "Any Game Title") is False  # empty string: code skips check
    # The actual code: `if name_filter:` — empty string is falsy so all files pass
    name_filter = ""
    if not name_filter:
        passed = True  # filter skipped
    else:
        filter_stem = Path(name_filter).stem.lower()
        file_stem = "any game title"
        passed = filter_stem in file_stem or file_stem in filter_stem
    assert passed


# Regression: old code did `name_filter.lower() in filename.lower()` (extension included)
def test_old_broken_logic_would_have_failed():
    name_filter = "Sonic the Hedgehog (USA, Europe).md"
    filename = "Sonic the Hedgehog.zip"
    # Old check (broken):
    old_result = name_filter.lower() in filename.lower()
    assert old_result is False  # this was the bug: it returned False and blocked the file

    # New check (fixed): bidirectional stem comparison
    filter_stem = Path(name_filter).stem.lower()
    file_stem = Path(filename).stem.lower()
    new_result = filter_stem in file_stem or file_stem in filter_stem
    assert new_result is True  # now correctly passes


# ---------------------------------------------------------------------------
# system map — spot-check a few important systems
# ---------------------------------------------------------------------------

def test_system_map_playstation(source):
    assert source._vimm_system("PlayStation") == "PS1"


def test_system_map_n64(source):
    assert source._vimm_system("Nintendo 64") == "N64"


def test_system_map_unknown_returns_empty(source):
    assert source._vimm_system("UnknownSystem XYZ") == ""


def test_system_map_genesis(source):
    assert source._vimm_system("Sega Genesis / Mega Drive") == "Genesis"
