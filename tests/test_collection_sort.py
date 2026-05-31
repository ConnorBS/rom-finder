"""Collection sort controls (size / date / progress / points / …)."""
from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry


def test_sort_by_file_size(client):
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="TinyGame", system="NES", file_name="t.nes",
                           file_path="/t.nes", file_size=100))
        s.add(LibraryEntry(game_title="HugeGame", system="Wii", file_name="h.iso",
                           file_path="/h.iso", file_size=5_000_000_000))
        s.commit()

    desc = client.get("/collection?sort=size_desc").text
    asc = client.get("/collection?sort=size_asc").text
    assert desc.index("HugeGame") < desc.index("TinyGame")   # largest first
    assert asc.index("TinyGame") < asc.index("HugeGame")     # smallest first
    assert "4.66 GB" in desc                                  # human-readable size shown
