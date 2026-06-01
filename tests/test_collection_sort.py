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


def test_sort_controls_carry_the_active_status_filter(client):
    """Regression: changing Sort/System/Per-page must PRESERVE the status filter.
    The controls pull it via hx-include="#col-status"; that only works if a real
    form field carries the value. Before the fix the status pills were <a> links
    sharing id="col-status" (no value), so sorting dropped the filter (e.g.
    Duplicate → sort by size cleared it)."""
    html = client.get("/collection?status=duplicate&sort=size_desc").text
    # The hidden input must carry the active filter so hx-include sends it.
    assert '<input type="hidden" id="col-status" name="status" value="duplicate">' in html
    # …and that id must be unique — the pills must NOT also use it.
    assert html.count('id="col-status"') == 1
