"""Hunt History view: renders, shows the resolved URL, and marks failed
attempts as blocked for that specific game."""

from sqlmodel import Session

from app.db.database import engine
from app.db.models import WantedGame, HuntAttempt


def test_history_marks_blocked_per_game_and_shows_url(client):
    with Session(engine) as s:
        g = WantedGame(game_title="Super Mario Bros", system="NES", ra_game_id=10)
        s.add(g)
        s.commit()
        s.refresh(g)
        # An SMB2 file tried during the SMB hunt and rejected → blocked for SMB.
        s.add(HuntAttempt(
            wanted_game_id=g.id, source_id="archive_org", identifier="coll",
            file_name="Super Mario Bros 2 (USA).nes",
            source_url="https://archive.org/download/coll/smb2.nes",
            result="bad_hash",
        ))
        s.commit()

    r = client.get("/downloads")
    assert r.status_code == 200
    body = r.text
    assert "Super Mario Bros 2 (USA).nes" in body
    assert "blocked for this game" in body                 # per-game block indicator
    assert "https://archive.org/download/coll/smb2.nes" in body  # the resolved URL is shown
