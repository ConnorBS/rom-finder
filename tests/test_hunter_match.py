"""Auto-hunt wrong-game guard: a hash that matches a DIFFERENT RA game must not
verify the wanted game (the Kirby-hunt-downloads-Solaris bug)."""

from app.services.hunter import _match_is_correct_game


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
