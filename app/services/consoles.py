"""Console catalog — every platform the app knows about, plus the user's selection.

`CONSOLES` is the master list ("all consoles under the sun"). Each entry carries:
  - `ra_id`: the RetroAchievements console id (rcheevos `rc_consoles.h`), or None when
    RA has no console id for it (or files its games under a parent console).
  - `ra`: True when RA can hash-verify ROMs for this platform. False → the platform lands
    in `ra_client.RA_UNSUPPORTED_SYSTEMS` (shown as "No RA platform", never sent to RA).
    RA-listed consoles we're unsure are active stay True: a wasted lookup is cheaper than
    hiding a real match.
  - `aliases`: lowercase folder names used ONLY by the Settings folder auto-map suggestion
    (never by the scan's `resolve_system`, so existing entries never get re-systemed).

Names of consoles that existed before the catalog are kept byte-identical — they are stored
on LibraryEntry/WantedGame rows.

Users choose which consoles the app offers (`enabled_consoles` setting, JSON list; empty =
the default set) and can add their own (`custom_consoles`). The selection drives the console
pickers (folder mapping, RA game search) — it never hides ROMs already in the library.

Leaf module: imports only `app.db` + `app.services.settings` (ra_client imports this).
"""

import re
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.db.models import LibraryEntry, WantedGame
from app.services import settings as app_settings


@dataclass(frozen=True)
class Console:
    name: str
    maker: str
    ra_id: int | None = None
    ra: bool = True
    aliases: tuple[str, ...] = field(default_factory=tuple)


def _c(name, maker, ra_id=None, ra=True, *aliases):
    return Console(name, maker, ra_id, ra, tuple(aliases))


CONSOLES: list[Console] = [
    # Nintendo
    _c("NES", "Nintendo", 7, True, "nes", "famicom", "fc", "nintendo entertainment system"),
    _c("Famicom Disk System", "Nintendo", 81, True, "fds", "famicom disk system"),
    _c("SNES", "Nintendo", 3, True, "snes", "sfc", "super nintendo", "super famicom",
       "super nintendo entertainment system"),
    _c("Satellaview", "Nintendo", None, True, "satellaview", "bs-x", "bsx"),
    _c("Sufami Turbo", "Nintendo", None, True, "sufami turbo", "sufami"),
    _c("Nintendo 64", "Nintendo", 2, True, "n64", "nintendo 64"),
    _c("Nintendo 64DD", "Nintendo", None, True, "64dd", "n64dd"),
    _c("GameCube", "Nintendo", 16, True, "gc", "ngc", "gcn", "gamecube", "nintendo gamecube"),
    _c("Wii", "Nintendo", 19, True, "wii", "nintendo wii"),
    _c("Wii U", "Nintendo", 20, True, "wiiu", "wii u", "nintendo wii u"),
    _c("Nintendo Switch", "Nintendo", None, False, "switch", "nsw", "nintendo switch"),
    _c("Nintendo Switch 2", "Nintendo", None, False, "switch 2", "switch2", "ns2"),
    _c("Game & Watch", "Nintendo", 60, True, "game & watch", "game and watch", "gw"),
    _c("Game Boy", "Nintendo", 4, True, "gb", "game boy", "gameboy", "nintendo game boy"),
    _c("Game Boy Color", "Nintendo", 6, True, "gbc", "game boy color", "gameboy color",
       "nintendo game boy color"),
    _c("Game Boy Advance", "Nintendo", 5, True, "gba", "game boy advance", "gameboy advance",
       "nintendo game boy advance"),
    _c("Virtual Boy", "Nintendo", 28, True, "vb", "virtual boy", "virtualboy"),
    _c("Nintendo DS", "Nintendo", 18, True, "nds", "ds", "nintendo ds"),
    _c("Nintendo DSi", "Nintendo", 78, True, "dsi", "nintendo dsi"),
    _c("Nintendo 3DS", "Nintendo", 62, False, "3ds", "n3ds", "new 3ds", "new nintendo 3ds",
       "nintendo 3ds"),
    _c("Pokemon Mini", "Nintendo", 24, True, "pokemon mini", "pokemini", "pokemon-mini"),
    # Sony
    _c("PlayStation", "Sony", 12, True, "ps1", "psx", "psone", "playstation", "sony playstation"),
    _c("PlayStation 2", "Sony", 21, True, "ps2", "playstation 2", "sony playstation 2"),
    _c("PlayStation 3", "Sony", 82, False, "ps3", "playstation 3", "sony playstation 3"),
    _c("PlayStation 4", "Sony", None, False, "ps4", "playstation 4"),
    _c("PlayStation 5", "Sony", None, False, "ps5", "playstation 5"),
    _c("PlayStation Portable", "Sony", 41, True, "psp", "playstation portable"),
    _c("PlayStation Vita", "Sony", None, False, "vita", "psvita", "ps vita", "psv",
       "playstation vita"),
    _c("PocketStation", "Sony", None, False, "pocketstation"),
    # Sega
    _c("SG-1000", "Sega", 33, True, "sg1000", "sg-1000"),
    _c("Master System", "Sega", 11, True, "sms", "master system", "mark iii",
       "sega master system"),
    _c("Sega Genesis / Mega Drive", "Sega", 1, True, "genesis", "megadrive", "mega drive", "md",
       "sega genesis", "sega mega drive"),
    _c("Sega CD", "Sega", 9, True, "segacd", "sega cd", "mega cd", "megacd"),
    _c("Sega 32X", "Sega", 10, True, "32x", "sega 32x"),
    _c("Sega Pico", "Sega", 68, True, "pico", "sega pico"),
    _c("Saturn", "Sega", 39, True, "saturn", "sega saturn"),
    _c("Dreamcast", "Sega", 40, True, "dc", "dreamcast", "sega dreamcast"),
    _c("Game Gear", "Sega", 15, True, "gg", "gamegear", "game gear", "sega game gear"),
    _c("Sega NAOMI", "Sega", None, False, "naomi", "sega naomi"),
    _c("Sega Model 3", "Sega", None, False, "model3", "model 3", "sega model 3"),
    # Microsoft
    _c("Xbox", "Microsoft", 22, False, "xbox", "microsoft xbox"),
    _c("Xbox 360", "Microsoft", None, False, "x360", "xbox360", "xbox 360"),
    _c("Xbox One", "Microsoft", None, False, "xbone", "xbox one"),
    _c("Xbox Series X|S", "Microsoft", None, False, "xbox series", "xsx"),
    _c("MS-DOS", "Microsoft", 26, True, "dos", "msdos", "ms-dos"),
    _c("Windows PC", "Microsoft", None, False, "pc", "windows", "win"),
    _c("MSX", "Microsoft", 29, True, "msx", "msx1", "msx2", "microsoft - msx"),
    # Atari
    _c("Atari 2600", "Atari", 25, True, "a2600", "atari2600", "atari 2600", "vcs"),
    _c("Atari 5200", "Atari", 50, True, "a5200", "atari5200", "atari 5200"),
    _c("Atari 7800", "Atari", 51, True, "a7800", "atari7800", "atari 7800"),
    _c("Atari 8-bit", "Atari", None, False, "atari800", "atari 800", "atari xl", "atari xe",
       "atari 8-bit"),
    _c("Atari Lynx", "Atari", 13, True, "lynx", "atari lynx"),
    _c("Atari Jaguar", "Atari", 17, True, "jaguar", "atari jaguar"),
    _c("Atari Jaguar CD", "Atari", 77, True, "jaguarcd", "jaguar cd", "atari jaguar cd"),
    _c("Atari ST", "Atari", 36, True, "atarist", "atari st"),
    # NEC
    _c("PC Engine / TurboGrafx-16", "NEC", 8, True, "pce", "tg16", "tg-16", "pc engine",
       "pcengine", "turbografx", "turbografx-16", "turbografx 16"),
    _c("PC Engine CD", "NEC", 76, True, "pcecd", "pce-cd", "pc engine cd", "turbografx-cd",
       "turbografx cd", "tg-cd"),
    _c("PC Engine SuperGrafx", "NEC", None, True, "supergrafx", "sgx"),
    _c("PC-FX", "NEC", 49, True, "pcfx", "pc-fx"),
    _c("PC-8000/8800", "NEC", 47, True, "pc88", "pc-88", "pc-8800", "pc8800"),
    _c("PC-9800", "NEC", 48, True, "pc98", "pc-98", "pc-9800", "pc9800"),
    _c("PC-6000", "NEC", 67, True, "pc60", "pc-6000", "pc6000", "pc-6001"),
    # SNK
    _c("Neo Geo", "SNK", None, True, "neogeo", "neo geo", "aes", "mvs"),
    _c("Neo Geo CD", "SNK", 56, True, "neogeocd", "neo geo cd", "ngcd"),
    _c("Neo Geo Pocket", "SNK", 14, True, "ngp", "neo geo pocket", "snk neo geo pocket"),
    _c("Neo Geo Pocket Color", "SNK", None, True, "ngpc", "neo geo pocket color"),
    # Commodore
    _c("Commodore 64", "Commodore", 30, True, "c64", "commodore 64"),
    _c("VIC-20", "Commodore", 34, True, "vic20", "vic-20"),
    _c("Amiga", "Commodore", 35, True, "amiga", "commodore amiga"),
    _c("Amiga CD32", "Commodore", None, False, "cd32", "amiga cd32"),
    # Sinclair / Amstrad / Acorn
    _c("ZX Spectrum", "Sinclair", 59, True, "zxspectrum", "zx spectrum", "spectrum"),
    _c("ZX81", "Sinclair", 31, True, "zx81"),
    _c("Amstrad CPC", "Amstrad", 37, True, "cpc", "amstrad cpc"),
    _c("BBC Micro", "Acorn", None, False, "bbc", "bbc micro", "bbcmicro"),
    # Bandai / Watara / misc handhelds
    _c("WonderSwan", "Bandai", 53, True, "ws", "wonderswan"),
    _c("WonderSwan Color", "Bandai", None, True, "wsc", "wonderswan color"),
    _c("Watara Supervision", "Watara", 63, True, "supervision", "watara supervision"),
    _c("Mega Duck", "Welback", 69, True, "megaduck", "mega duck", "cougar boy"),
    _c("Tiger Game.com", "Tiger", None, False, "game.com", "gamecom"),
    _c("Gamate", "Bit Corporation", None, False, "gamate"),
    _c("Nokia N-Gage", "Nokia", 61, True, "ngage", "n-gage"),
    _c("Zeebo", "Zeebo", 70, True, "zeebo"),
    # Japanese computers / consoles
    _c("Sharp X68000", "Sharp", 52, True, "x68000", "x68k"),
    _c("Sharp X1", "Sharp", 64, True, "x1", "sharp x1"),
    _c("FM Towns", "Fujitsu", 58, True, "fmtowns", "fm towns"),
    _c("Cassette Vision", "Epoch", 54, True, "cassette vision", "cassettevision"),
    _c("Super Cassette Vision", "Epoch", 55, True, "super cassette vision", "scv"),
    _c("Casio Loopy", "Casio", None, False, "loopy", "casio loopy"),
    _c("Casio PV-1000", "Casio", None, False, "pv1000", "pv-1000"),
    _c("Thomson TO8", "Thomson", 66, True, "to8", "thomson to8"),
    _c("Oric", "Tangerine", 32, True, "oric"),
    # Early consoles
    _c("Magnavox Odyssey 2", "Magnavox", 23, True, "odyssey2", "odyssey 2", "videopac"),
    _c("ColecoVision", "Coleco", 44, True, "coleco", "colecovision"),
    _c("Intellivision", "Mattel", 45, True, "intv", "intellivision"),
    _c("Vectrex", "GCE", 46, True, "vectrex"),
    _c("Fairchild Channel F", "Fairchild", 57, True, "channelf", "channel f", "channel-f"),
    _c("Arcadia 2001", "Emerson", 73, True, "arcadia", "arcadia 2001"),
    _c("Interton VC 4000", "Interton", 74, True, "vc4000", "vc 4000"),
    _c("Elektor TV Games Computer", "Elektor", 75, True, "elektor"),
    _c("Bally Astrocade", "Bally", None, False, "astrocade", "bally astrocade"),
    _c("RCA Studio II", "RCA", None, False, "studio2", "studio ii"),
    _c("VTech CreatiVision", "VTech", None, False, "creativision"),
    _c("VTech V.Smile", "VTech", None, False, "vsmile", "v.smile"),
    # Disc-era others
    _c("3DO Interactive Multiplayer", "Panasonic", 43, True, "3do"),
    _c("Philips CD-i", "Philips", 42, True, "cdi", "cd-i", "philips cd-i"),
    _c("Nuon", "VM Labs", None, False, "nuon"),
    # Computers / fantasy consoles / other
    _c("Apple II", "Apple", 38, True, "apple2", "apple ii", "appleii"),
    _c("Arcade", "Arcade", 27, True, "arcade", "mame", "fbneo", "fba", "fbalpha"),
    _c("Arduboy", "Homebrew", 71, True, "arduboy"),
    _c("WASM-4", "Homebrew", 72, True, "wasm4", "wasm-4"),
    _c("Uzebox", "Homebrew", 80, True, "uzebox"),
    _c("TIC-80", "Homebrew", 65, True, "tic80", "tic-80"),
    _c("PICO-8", "Homebrew", None, False, "pico8", "pico-8"),
    _c("TI-83", "Texas Instruments", 79, True, "ti83", "ti-83"),
    _c("Ouya", "Ouya", None, False, "ouya"),
    _c("ScummVM", "Other", None, False, "scummvm"),
    _c("J2ME", "Other", None, False, "j2me", "java"),
]

CONSOLE_BY_NAME: dict[str, Console] = {c.name: c for c in CONSOLES}

# Every RA console id → name (incl. ones RA lists but can't verify, e.g. 3DS) — so an id
# posted by the Chrome extension always resolves to a clean name.
RA_ID_TO_NAME: dict[int, str] = {c.ra_id: c.name for c in CONSOLES if c.ra_id}

# RA consoles we can hash-verify against (the RA game-search pickers + autodiscover).
RA_VERIFIABLE: dict[int, str] = dict(sorted(
    (c.ra_id, c.name) for c in CONSOLES if c.ra_id and c.ra
))

NON_RA_NAMES: frozenset[str] = frozenset(c.name for c in CONSOLES if not c.ra)

# Default selection when the user hasn't chosen: the RA-verifiable set (what the app
# offered before the catalog existed) — plus anything already in the library/wanted pool.
DEFAULT_ENABLED: tuple[str, ...] = tuple(c.name for c in CONSOLES if c.ra_id and c.ra)


# --- User selection ----------------------------------------------------------

def custom_consoles(session: Session) -> list[str]:
    raw = app_settings.get_json(session, "custom_consoles", [])
    if not isinstance(raw, list):
        return []
    return [n for n in (str(x).strip() for x in raw) if n and n not in CONSOLE_BY_NAME]


def _systems_in_use(session: Session) -> set[str]:
    lib = session.exec(select(LibraryEntry.system).distinct()).all()
    wanted = session.exec(select(WantedGame.system).distinct()).all()
    return {s for s in (*lib, *wanted) if s}


def enabled_names(session: Session) -> set[str]:
    """Names of the consoles the user has switched on (catalog + custom)."""
    raw = app_settings.get(session, "enabled_consoles", "")
    customs = custom_consoles(session)
    if raw.strip():
        chosen = app_settings.get_json(session, "enabled_consoles", None)
        if isinstance(chosen, list):
            known = set(CONSOLE_BY_NAME) | set(customs)
            return {n for n in chosen if n in known}
    in_use = {s for s in _systems_in_use(session) if s in CONSOLE_BY_NAME}
    return set(DEFAULT_ENABLED) | in_use | set(customs)


def picker_names(session: Session, include: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Sorted console names for a folder→console dropdown: the enabled set, plus any
    `include`d value (a folder's current mapping) so a saved choice never vanishes."""
    names = enabled_names(session) | {n for n in include if n}
    return sorted(names, key=str.lower)


def ra_picker(session: Session) -> list[tuple[int, str]]:
    """(ra_id, name) for the RA game-search console dropdowns, limited to the enabled set.
    Falls back to every RA console if none are enabled (never an empty picker)."""
    enabled = enabled_names(session)
    pairs = [(i, n) for i, n in RA_VERIFIABLE.items() if n in enabled] or list(RA_VERIFIABLE.items())
    return sorted(pairs, key=lambda x: x[1].lower())


def grouped(session: Session) -> list[tuple[str, list[dict]]]:
    """Catalog grouped by maker (catalog order) for the Settings checklist."""
    enabled = enabled_names(session)
    groups: dict[str, list[dict]] = {}
    for c in CONSOLES:
        groups.setdefault(c.maker, []).append(
            {"name": c.name, "ra_id": c.ra_id, "ra": c.ra, "enabled": c.name in enabled, "custom": False}
        )
    customs = custom_consoles(session)
    if customs:
        groups["Custom"] = [
            {"name": n, "ra_id": None, "ra": True, "enabled": n in enabled, "custom": True}
            for n in customs
        ]
    return list(groups.items())


# --- Folder auto-mapping (Settings suggestion only) ---------------------------

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-\.]", " ", s.lower())).strip()


def automap_folder(folder_name: str, allowed: set[str], extra: dict[str, str] | None = None) -> str:
    """Best-match console name for a folder, restricted to `allowed` names; '' if none.
    `extra` is an additional {lowercase folder: system} table (the legacy DEFAULT_FOLDER_MAP).
    Exact name/alias first, then the noise-stripped form, then a substring match over
    full names only (longest first — short aliases like "gb"/"ds" would mis-hit "gba")."""
    exact: dict[str, str] = {}
    for c in CONSOLES:
        if c.name not in allowed:
            continue
        exact.setdefault(c.name.lower(), c.name)
        for a in c.aliases:
            exact.setdefault(a, c.name)
    for n in allowed:
        exact.setdefault(n.lower(), n)
    for folder, system in (extra or {}).items():
        if system in allowed:
            exact.setdefault(folder.lower(), system)

    normalized = folder_name.lower().strip()
    if normalized in exact:
        return exact[normalized]
    stripped = _clean(normalized)
    for key, system in exact.items():
        if _clean(key) == stripped:
            return system

    long_keys = sorted((k for k in exact if len(_clean(k)) >= 4), key=len, reverse=True)
    for key in long_keys:
        ck = _clean(key)
        if ck in stripped or (len(stripped) >= 3 and stripped in ck):
            return exact[key]
    return ""
