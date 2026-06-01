"""selection.classify_release rules: single-game OK, multi-disc kept as one game,
torrent pack → keep only the matching indices, usenet pack → skipped, no-match → none."""
from app.services.download_clients import selection


# RA-accepted stems for "Sly Cooper and the Thievius Raccoonus" + its title terms.
_RA = {"sly cooper and the thievius raccoonus (usa)"}
_TERMS = {"sly", "cooper", "thievius", "raccoonus"}


def _f(i, name):
    return {"index": i, "name": name, "size": 100}


def test_single_game_ok():
    files = [_f(0, "Sly Cooper and the Thievius Raccoonus (USA).iso")]
    r = selection.classify_files(files, _RA, _TERMS)
    assert r["kind"] == "single"
    assert r["keep_indices"] == [0]


def test_multidisc_kept_as_one_game():
    files = [
        _f(0, "Final Fantasy IX (USA) (Disc 1).bin"),
        _f(1, "Final Fantasy IX (USA) (Disc 2).bin"),
        _f(2, "Final Fantasy IX (USA) (Disc 3).bin"),
        _f(3, "Final Fantasy IX (USA) (Disc 4).bin"),
        _f(4, "readme.nfo"),
    ]
    ra = {"final fantasy ix (usa) (disc 1)"}
    terms = {"final", "fantasy"}
    r = selection.classify_files(files, ra, terms)
    assert r["kind"] == "multidisc"
    assert sorted(r["keep_indices"]) == [0, 1, 2, 3]   # all discs, not the .nfo


def test_torrent_pack_keeps_only_matching_indices():
    files = [
        _f(0, "Crash Bandicoot (USA).bin"),
        _f(1, "Sly Cooper and the Thievius Raccoonus (USA).iso"),  # the wanted one
        _f(2, "Spyro the Dragon (USA).bin"),
    ]
    r = selection.classify_files(files, _RA, _TERMS)
    assert r["kind"] == "pack"
    assert r["keep_indices"] == [1]


def test_no_match_returns_none():
    files = [_f(0, "Some Unrelated Game (USA).bin"), _f(1, "cover.jpg")]
    r = selection.classify_files(files, _RA, _TERMS)
    assert r["kind"] == "none"
    assert r["keep_indices"] == []


def test_no_rom_files_returns_none():
    files = [_f(0, "readme.txt"), _f(1, "cover.png")]
    assert selection.classify_files(files, _RA, _TERMS)["kind"] == "none"


def test_looks_like_pack_heuristic():
    assert selection.looks_like_pack("Nintendo Wii No-Intro Collection 2024")
    assert selection.looks_like_pack("PSP Romset (Merged)")
    assert selection.looks_like_pack("500 Games in 1")
    assert not selection.looks_like_pack("Sly Cooper and the Thievius Raccoonus (USA)")


def test_release_is_relevant():
    assert selection.release_is_relevant("Sly Cooper and the Thievius Raccoonus (USA) PS2", _TERMS)
    assert not selection.release_is_relevant("Ratchet & Clank (USA)", _TERMS)
