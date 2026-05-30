"""Detect which library games have an emulator save file — READ-ONLY.

Saves are matched to ROMs by **filename stem**: emulators (RetroArch and most
others) name a save after the loaded content, so `Super Mario Bros. (USA).nes`
gets `Super Mario Bros. (USA).srm` / `.state` / `.state1` …  We scan the configured
`saves_dir` plus the ROM dirs (saves often sit next to the ROM) for save-type files
and attribute each to the library entry(ies) whose stem matches.

CRITICAL: this module only ever READS the filesystem (`rglob` + `stat`). It never
opens a save for writing, moves, renames, or deletes one. ROM Finder must never
modify a save. The library delete-file action likewise only removes ROM/disc-track
extensions, never save extensions — see `_delete_rom_file` in routers/collection.py.
"""

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.db.models import LibraryEntry
from app.services import settings as app_settings
from app.routers.library import ROM_EXTENSIONS, ARCHIVE_EXTENSIONS

# Battery / SRAM / memory-card saves (persistent in-game progress).
_BATTERY_EXTS = {
    ".srm", ".sav", ".sa1", ".fla", ".eep", ".sra", ".mpk", ".dsv", ".rtc",
    ".mcr", ".mcd", ".gci", ".ps2", ".mc2", ".vmp", ".vmc", ".bcr", ".duck",
}
# Save STATES (snapshots). RetroArch: .state/.state1/.state2…; others: .st0/.ss0…
_STATE_RE = re.compile(r"\.(state\d*|ss\d+|st\d+|pj\d*)$", re.IGNORECASE)


def _classify(path: Path) -> str | None:
    """'battery', 'state', or None if not a save file."""
    suffix = path.suffix.lower()
    if suffix in _BATTERY_EXTS:
        return "battery"
    if _STATE_RE.match(suffix) or suffix == ".bsv":
        return "state"
    return None


def _rom_stem(file_name: str) -> str:
    """Stem an emulator would name a save after — drops the archive + inner ROM ext
    (`game.nes.zip` → `game`), else the single extension (`game.nes` → `game`)."""
    p = Path(file_name)
    stem = p.stem
    if p.suffix.lower() in ARCHIVE_EXTENSIONS and Path(stem).suffix.lower() in ROM_EXTENSIONS:
        stem = Path(stem).stem
    return stem.strip().lower()


def _save_stem(path: Path) -> str:
    """Normalize a save's name to the ROM stem it belongs to. Handles `Game.srm`,
    `Game.state2`, and emulators that keep the ROM ext (`Game.nes.srm` → `Game`)."""
    stem = path.stem                       # drops the save suffix
    inner = Path(stem)
    if inner.suffix.lower() in ROM_EXTENSIONS:
        stem = inner.stem
    return stem.strip().lower()


# ---------------------------------------------------------------------------
# Wii / Dolphin NAND saves — keyed by **title ID low** (4 ASCII chars, e.g. "RKME"),
# not by ROM filename. Dolphin writes per-game saves to
#     User/Wii/title/{type}/<hex(title_id_low)>/data/
# where {type} is "00010000" for disc games or "00010001" for WiiWare/VC channels.
# ---------------------------------------------------------------------------
_WII_TITLE_TYPES = ("00010000", "00010001")
_WII_RA_IDS = {19, 20}   # RA console ids for Wii / Wii U


def _wii_title_id_from_iso(f) -> str | None:
    """First 4 bytes of a Wii disc = the title-ID-low (ASCII)."""
    data = f.read(4)
    if len(data) == 4 and all(0x20 < b < 0x7F for b in data):
        return data.decode("ascii")
    return None


def _wii_title_id_from_wad(path: Path) -> str | None:
    """Parse a WiiWare/VC .wad: header → cert chain → ticket; title-ID-low at ticket+0x1E0.
    All sections are padded to 64 bytes. Fast (reads ~700 bytes)."""
    try:
        with open(path, "rb") as f:
            header = f.read(0x20)
            if len(header) < 0x14:
                return None
            cert_size = int.from_bytes(header[0x08:0x0C], "big")
            # Header (0x20) padded to 64; cert chain padded to 64; then ticket.
            ticket_off = 0x40 + ((cert_size + 0x3F) & ~0x3F)
            f.seek(ticket_off + 0x1E0)        # title ID low (last 4 bytes of the 8-byte TID)
            tid_low = f.read(4)
            if len(tid_low) == 4 and all(0x20 < b < 0x7F for b in tid_low):
                return tid_low.decode("ascii")
    except OSError:
        pass
    return None


def _read_wii_title_id(rom_path: Path) -> str | None:
    """Return the 4-char title ID for a Wii ROM. Handles the fast-path formats; compressed
    disc formats other than RVZ/WIA aren't read here (would need full nodtool decompress)."""
    if not rom_path.exists():
        return None
    suffix = rom_path.suffix.lower()
    try:
        if suffix == ".iso":
            with open(rom_path, "rb") as f:
                return _wii_title_id_from_iso(f)
        if suffix in (".rvz", ".wia"):
            # WIA/RVZ keep the original disc header inside the WIADisc struct at +0x58
            # (WIA_VERSION = 0x48 bytes, then disc_type/compression/chunk_size = 0x10).
            with open(rom_path, "rb") as f:
                f.seek(0x58)
                return _wii_title_id_from_iso(f)
        if suffix == ".wad":
            return _wii_title_id_from_wad(rom_path)
    except OSError:
        return None
    return None


def _scan_wii_nand(root: Path) -> dict[str, list[Path]]:
    """Walk root for the Dolphin NAND save layout. Returns {title_id_low: [data dirs]}."""
    found: dict[str, list[Path]] = defaultdict(list)
    for ttype in _WII_TITLE_TYPES:
        try:
            for d in root.rglob(f"title/{ttype}/*/data"):
                if not d.is_dir():
                    continue
                try:
                    if not any(d.iterdir()):    # empty data/ = no save
                        continue
                except OSError:
                    continue
                tid_hex = d.parent.name
                if len(tid_hex) != 8:
                    continue
                try:
                    tid_bytes = bytes.fromhex(tid_hex)
                except ValueError:
                    continue
                if all(0x20 < b < 0x7F for b in tid_bytes):
                    found[tid_bytes.decode("ascii")].append(d)
        except OSError:
            continue
    return found


def scan_saves(session: Session) -> dict:
    """Refresh save info on every LibraryEntry. Full rebuild (clear then re-derive)."""
    entries = session.exec(select(LibraryEntry)).all()
    for e in entries:
        e.save_count = 0
        e.save_files = ""
        e.save_updated_at = None

    # Scan the dedicated saves dir + the ROM roots (saves often sit beside the ROM).
    roots: list[Path] = []
    seen: set[str] = set()
    for key in ("saves_dir", "download_dir", "check_dir"):
        val = app_settings.get(session, key, "")
        if not val:
            continue
        p = Path(val)
        if p.exists() and str(p) not in seen:
            seen.add(str(p))
            roots.append(p)
    if not roots:
        session.commit()
        return {"roots": [], "found": 0, "matched": 0, "games": 0}

    index: dict[str, list[LibraryEntry]] = defaultdict(list)
    for e in entries:
        index[_rom_stem(e.file_name)].append(e)

    found = matched = 0
    by_entry: dict[int, list[dict]] = defaultdict(list)
    for root in roots:
        for f in root.rglob("*"):
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            kind = _classify(f)
            if not kind:
                continue
            found += 1
            targets = index.get(_save_stem(f))
            if not targets:
                continue
            try:
                st = f.stat()
                info = {"name": f.name, "kind": kind, "size": st.st_size,
                        "mtime": datetime.utcfromtimestamp(st.st_mtime).isoformat()}
            except OSError:
                continue
            for e in targets:
                matched += 1
                by_entry[e.id].append(info)

    # ------------------------------------------------------------------
    # Wii / Dolphin NAND saves (per-title save data, not stem-named).
    # ------------------------------------------------------------------
    from app.services.rahasher import get_ra_system_id   # lazy: avoid import cycle
    wii_entries = [e for e in entries if get_ra_system_id(e.system) in _WII_RA_IDS]
    # Lazy populate disc_id (4-char title-ID-low) from the ROM header — fast paths only.
    for e in wii_entries:
        if not e.disc_id and e.file_path:
            tid = _read_wii_title_id(Path(e.file_path))
            if tid:
                e.disc_id = tid
    disc_idx: dict[str, list[LibraryEntry]] = defaultdict(list)
    for e in wii_entries:
        if e.disc_id:
            disc_idx[e.disc_id].append(e)

    wii_nand_found = 0
    seen_data_dirs: set[str] = set()           # dedup across nested roots
    for root in roots:
        nand = _scan_wii_nand(root)
        for tid, data_dirs in nand.items():
            targets = disc_idx.get(tid)
            if not targets:
                continue
            for data_dir in data_dirs:
                try:
                    key = str(data_dir.resolve())
                except OSError:
                    key = str(data_dir)
                if key in seen_data_dirs:
                    continue
                seen_data_dirs.add(key)
                try:
                    files = [f for f in data_dir.iterdir() if f.is_file()]
                except OSError:
                    continue
                if not files:
                    continue
                try:
                    total_size = sum(f.stat().st_size for f in files)
                    latest_mtime = max(f.stat().st_mtime for f in files)
                except OSError:
                    continue
                wii_nand_found += 1
                info = {"name": f"Wii NAND save ({tid})", "kind": "wii-nand",
                        "size": total_size,
                        "mtime": datetime.utcfromtimestamp(latest_mtime).isoformat()}
                for e in targets:
                    matched += 1
                    by_entry[e.id].append(info)
    found += wii_nand_found

    games = 0
    for e in entries:
        saves = by_entry.get(e.id)
        if not saves:
            continue
        games += 1
        saves.sort(key=lambda s: s["name"])
        e.save_count = len(saves)
        e.save_files = json.dumps(saves)
        e.save_updated_at = max(datetime.fromisoformat(s["mtime"]) for s in saves)

    session.commit()
    return {"roots": [str(r) for r in roots], "found": found, "matched": matched, "games": games}
