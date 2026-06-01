"""Auto-hunt wrong-game guard: a hash that matches a DIFFERENT RA game must not
verify the wanted game (the Kirby-hunt-downloads-Solaris bug)."""

from sqlmodel import Session

from app.db.models import LibraryEntry
from app.services.hunter import (
    _match_is_correct_game, _verified_game_id, _file_score, _significant_terms,
    _owned_accepted_copy, _ra_base_id,
)


# --- candidate scoring: "is this the game we want?" --------------------------

_KIRBY = _significant_terms("Kirby's Return to Dream Land")


def test_significant_terms_drops_stopwords():
    assert _significant_terms("Kirby's Return to Dream Land") == {"kirby", "return", "dream", "land"}


def test_ra_stem_exact_match_scores_high():
    stems = {"kirby's return to dream land (usa) (en,fr,es)"}
    assert _file_score("Kirby's Return to Dream Land (USA) (En,Fr,Es).rvz", stems, _KIRBY) >= 100


def test_unrelated_file_scores_zero_with_ra_stems():
    stems = {"kirby's return to dream land (usa)"}
    assert _file_score("Ben 10 - Galactic Racing (USA).nds", stems, _KIRBY) == 0


def test_unrelated_file_scores_zero_without_ra_stems():
    # The key fix: even when RA hashes failed to load (empty stems), an unrelated
    # NDS game for a Kirby hunt must score 0 (no region freebie).
    assert _file_score("Ben 10 - Galactic Racing (USA).nds", set(), _KIRBY) == 0
    assert _file_score("Solaris (USA).zip", set(), _KIRBY) == 0


def test_title_fallback_matches_without_ra_stems():
    # No RA stems, but the filename contains all the title's significant words.
    assert _file_score("Kirbys Return to Dream Land (USA).rvz", set(), _KIRBY) > 0



def test_correct_game_matches():
    assert _match_is_correct_game(matched_id=586, expected_id=586) is True


def test_wrong_game_rejected():
    # Solaris (matched_id) found during a Kirby (expected_id) hunt → reject.
    assert _match_is_correct_game(matched_id=999, expected_id=586) is False


def test_no_match_rejected():
    assert _match_is_correct_game(matched_id=None, expected_id=586) is False
    assert _match_is_correct_game(matched_id=0, expected_id=586) is False


def test_no_expected_id_accepts_any_match():
    # When we don't know the expected RA id, any real match is accepted.
    assert _match_is_correct_game(matched_id=123, expected_id=None) is True
    assert _match_is_correct_game(matched_id=None, expected_id=None) is False


# --- _verified_game_id: normal match + the subset (hash-in-own-list) path -------

def test_verified_normal_lookup_match():
    # RA's hash lookup returns the expected game → verifies as that id.
    assert _verified_game_id(586, 586, "abc", set()) == 586


def test_verified_rejects_wrong_game_not_in_list():
    # Hash matched a DIFFERENT game and isn't in the wanted game's list → None.
    assert _verified_game_id(999, 586, "abc", set()) is None
    assert _verified_game_id(None, 586, "abc", set()) is None


def test_verified_subset_accepts_hash_in_own_list():
    # Subset game 7001: its accepted list contains the base ROM's hash, but RA's
    # lookup resolves that hash to the BASE game (586). It must still verify as the
    # wanted subset id (7001), not the base id and not be rejected as wrong-game.
    accepted = {"deadbeef"}
    assert _verified_game_id(586, 7001, "DEADBEEF", accepted) == 7001
    assert _verified_game_id(None, 7001, "deadbeef", accepted) == 7001


def test_verified_hash_match_is_case_insensitive():
    assert _verified_game_id(None, 42, "AbCdEf", {"abcdef"}) == 42


# --- multiset/hub synthetic id decode (Alien Crush Returns: dump → 1200034728) ----

def test_ra_base_id_decodes_synthetic_multiset_ids():
    assert _ra_base_id(1200034728) == 34728   # "set of" game 34728
    assert _ra_base_id(1100002271) == 2271
    assert _ra_base_id(586) == 586            # real ids (< 1e8) pass through unchanged


def test_synthetic_id_verifies_against_its_base_game():
    # RA resolves the wanted dump to a *set of* game 34728 (synthetic 1200034728);
    # comparing base ids lets it verify, and returns the real base id (34728).
    assert _match_is_correct_game(1200034728, 34728) is True
    assert _verified_game_id(1200034728, 34728, "deadbeef", set()) == 34728
    # A different base game (Alien Crush PC Engine = 2271) must still be rejected.
    assert _match_is_correct_game(1100002271, 34728) is False
    assert _verified_game_id(1100002271, 34728, "x", set()) is None


# --- _owned_accepted_copy: skip hunting a subset you already own (SM64 case) ----

def test_owned_accepted_copy_finds_base_rom_for_subset(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(LibraryEntry(game_title="Super Mario 64", system="Nintendo 64",
                           file_name="Super Mario 64 (USA).z64", file_path="/n64/sm64.z64",
                           file_hash="20b854b239203baf6c961b850a4a51a2"))
        s.add(LibraryEntry(game_title="Other", system="NES", file_name="o.nes",
                           file_path="/nes/o.nes", file_hash="ffffffffffffffffffffffffffffffff"))
        s.commit()
    with Session(fresh_engine) as s:
        # The Coin Collector subset accepts the plain base SM64 hash — owned copy
        # is found even though we pass the hash upper-cased (case-insensitive).
        hit = _owned_accepted_copy(s, {"20B854B239203BAF6C961B850A4A51A2",
                                       "7b3285c7f6aee1f9a3891fd75abe0a39"})
        assert hit is not None and hit.file_name == "Super Mario 64 (USA).z64"


def test_owned_accepted_copy_none_when_not_owned(fresh_engine):
    with Session(fresh_engine) as s:
        s.add(LibraryEntry(game_title="Other", system="NES", file_name="o.nes",
                           file_path="/nes/o.nes", file_hash="ffffffffffffffffffffffffffffffff"))
        s.commit()
    with Session(fresh_engine) as s:
        assert _owned_accepted_copy(s, {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}) is None
        assert _owned_accepted_copy(s, set()) is None
