"""Collection shows base achievements earned/total (from the RA mirror) so the user can
see which games they've started. earned = RAGameProgress.num_awarded, total = max_possible
for the matched (base) game id."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry, RAGameProgress


def _add_game(ra_game_id: int, title: str, total: int, earned: int):
    with Session(engine) as s:
        s.add(LibraryEntry(game_title=title, system="NES", file_name=f"{title}.nes",
                           file_path=f"/roms/NES/{title}.nes", file_hash="h",
                           ra_game_id=ra_game_id, ra_matched=True))
        if total:
            s.add(RAGameProgress(game_id=ra_game_id, title=title, max_possible=total,
                                 num_awarded=earned, owned=True))
        s.commit()


def test_started_game_shows_earned_over_total(client):
    _add_game(5001, "Started Game", total=50, earned=3)
    html = client.get("/collection?view=cards").text
    assert "3/50" in html                       # earned/total base achievements


def test_unstarted_but_known_game_shows_zero_over_total(client):
    """A game in the RA completion mirror with 0 earned still shows 0/total — the
    user can see it's owned + tracked but untouched."""
    _add_game(5002, "Untouched Game", total=20, earned=0)
    html = client.get("/collection?view=cards").text
    assert "0/20" in html


def test_game_with_no_progress_row_shows_no_count(client):
    """No RAGameProgress (never played / not in the mirror) → no achievement count,
    so the cards stay clean and 'has a count' itself means 'started'."""
    _add_game(5003, "No Mirror Game", total=0, earned=0)
    html = client.get("/collection?view=list").text
    # The list cell renders an em-dash placeholder, never a "/0" count.
    assert "/0" not in html
