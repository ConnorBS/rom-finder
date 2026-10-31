"""Console catalog + the user's enabled-console selection."""

import json

from sqlmodel import Session

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import consoles
from app.services import settings as app_settings
from app.services.ra_client import SYSTEMS, RA_UNSUPPORTED_SYSTEMS
from app.services.rahasher import get_ra_system_id
from app.services.title_utils import canonical_system


# Names that were in SYSTEMS before the catalog — stored on existing rows, must not change.
_LEGACY_NAMES = {
    "Sega Genesis / Mega Drive", "Nintendo 64", "SNES", "Game Boy", "Game Boy Advance",
    "Game Boy Color", "NES", "PC Engine / TurboGrafx-16", "Sega CD", "Sega 32X",
    "Master System", "PlayStation", "Atari Lynx", "Neo Geo Pocket", "Game Gear",
    "Atari Jaguar", "Nintendo DS", "Wii", "Wii U", "PlayStation 2", "Magnavox Odyssey 2",
    "Pokemon Mini", "Atari 2600", "Arcade", "Virtual Boy", "MSX", "SG-1000", "Amstrad CPC",
    "Apple II", "Saturn", "Dreamcast", "PlayStation Portable", "3DO Interactive Multiplayer",
    "ColecoVision", "Intellivision", "Vectrex", "PC-8000/8800", "PC-FX", "Atari 7800",
    "WonderSwan", "Fairchild Channel F", "Philips CD-i", "Watara Supervision", "Mega Duck",
    "Arduboy", "WASM-4", "PC Engine CD", "Nintendo DSi", "GameCube", "Uzebox",
}


def test_catalog_unique():
    names = [c.name for c in consoles.CONSOLES]
    ids = [c.ra_id for c in consoles.CONSOLES if c.ra_id]
    assert len(names) == len(set(names))
    assert len(ids) == len(set(ids))


def test_legacy_names_still_verifiable():
    assert _LEGACY_NAMES <= set(SYSTEMS.values())


def test_ra_ids_match_rcheevos():
    # These three were wrong before (56/57/89); rc_consoles.h is the source of truth.
    assert SYSTEMS[42] == "Philips CD-i"
    assert SYSTEMS[57] == "Fairchild Channel F"
    assert SYSTEMS[80] == "Uzebox"
    assert get_ra_system_id("Philips CD-i") == 42
    assert get_ra_system_id("Fairchild Channel F") == 57


def test_non_ra_consoles_unsupported_not_in_systems():
    for name in ("Nintendo 3DS", "Nintendo Switch", "PlayStation Vita", "PlayStation 3", "Xbox"):
        assert name in RA_UNSUPPORTED_SYSTEMS
        assert name not in SYSTEMS.values()
    assert "Archipelago" in RA_UNSUPPORTED_SYSTEMS
    assert not (_LEGACY_NAMES & RA_UNSUPPORTED_SYSTEMS)


def test_canonical_system_resolves_inactive_ra_id():
    assert canonical_system("", 62) == "Nintendo 3DS"


def test_automap_aliases_and_restriction():
    allowed = {c.name for c in consoles.CONSOLES}
    assert consoles.automap_folder("N3DS", allowed) == "Nintendo 3DS"
    assert consoles.automap_folder("3ds", allowed) == "Nintendo 3DS"
    assert consoles.automap_folder("Nintendo - Nintendo 3DS", allowed) == "Nintendo 3DS"
    assert consoles.automap_folder("gba", allowed) == "Game Boy Advance"
    assert consoles.automap_folder("Neo Geo Pocket Color", allowed) == "Neo Geo Pocket Color"
    assert consoles.automap_folder("random-stuff", allowed) == ""
    # A disabled console is never suggested.
    assert consoles.automap_folder("3ds", allowed - {"Nintendo 3DS"}) == ""


def test_default_enabled_includes_ra_and_in_use(fresh_engine):
    with Session(engine) as s:
        s.add(LibraryEntry(game_title="Z", system="Nintendo 3DS", file_name="z.3ds", file_path="/z.3ds"))
        s.commit()
        enabled = consoles.enabled_names(s)
    assert set(consoles.DEFAULT_ENABLED) <= enabled
    assert "Nintendo 3DS" in enabled          # already in the library
    assert "Nintendo Switch" not in enabled   # not RA, not in use


def test_explicit_selection_and_ra_picker(fresh_engine):
    with Session(engine) as s:
        app_settings.set(s, "enabled_consoles", json.dumps(["SNES", "Nintendo 3DS", "Bogus"]))
        assert consoles.enabled_names(s) == {"SNES", "Nintendo 3DS"}
        assert consoles.ra_picker(s) == [(3, "SNES")]      # 3DS has no RA game list
        assert "Nintendo 3DS" in consoles.picker_names(s)
        assert "Wii" in consoles.picker_names(s, {"Wii"})  # a saved mapping never vanishes
        app_settings.set(s, "enabled_consoles", "[]")
        assert len(consoles.ra_picker(s)) == len(SYSTEMS)  # never an empty picker
        app_settings.set(s, "enabled_consoles", "not json")
        assert set(consoles.DEFAULT_ENABLED) <= consoles.enabled_names(s)


def test_settings_page_and_save(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'value="Nintendo 3DS"' in r.text
    r = client.post("/settings/consoles", data={"consoles": ["Nintendo 3DS", "SNES"]})
    assert "2 console(s) enabled" in r.text
    with Session(engine) as s:
        assert consoles.enabled_names(s) == {"Nintendo 3DS", "SNES"}
    r = client.get("/wanted")
    assert r.status_code == 200
    assert '<option value="3">SNES</option>' in r.text
    assert "Nintendo 64</option>" not in r.text


def test_custom_console_add_remove(client):
    r = client.post("/settings/consoles/custom", data={"name": "  Sega   <Model 2> "})
    assert r.headers.get("HX-Refresh") == "true"
    with Session(engine) as s:
        assert consoles.custom_consoles(s) == ["Sega <Model 2>"]
        assert "Sega <Model 2>" in consoles.enabled_names(s)
    page = client.get("/settings").text
    assert "Sega &lt;Model 2&gt;" in page and "Sega <Model 2>" not in page
    # Re-adding (any case) doesn't duplicate.
    r = client.post("/settings/consoles/custom", data={"name": "sega <model 2>"})
    assert "already" in r.text
    # Adding a catalog name just enables it.
    client.post("/settings/consoles", data={"consoles": ["SNES"]})
    client.post("/settings/consoles/custom", data={"name": "nintendo switch"})
    with Session(engine) as s:
        assert "Nintendo Switch" in consoles.enabled_names(s)
        assert consoles.custom_consoles(s) == ["Sega <Model 2>"]
    r = client.post("/settings/consoles/custom/remove", data={"name": "Sega <Model 2>"})
    assert r.headers.get("HX-Refresh") == "true"
    with Session(engine) as s:
        assert consoles.custom_consoles(s) == []
        assert "Sega <Model 2>" not in consoles.enabled_names(s)
