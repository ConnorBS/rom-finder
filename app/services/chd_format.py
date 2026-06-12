"""CHD container-format check + re-encode (the in-app twin of the R: batch scripts).

Why this exists: a CHD whose hunks are compressed with **Zstandard** (`cdzs` for CD
images, `zstd` for DVD/raw) is a perfectly valid, hash-correct dump — it verifies in
RAHasher and plays in a current emulator — but RetroArch's *RetroAchievements* hasher
(and any core/standalone built against an older libchdr) can't DECODE the zstd hunks,
so the game boots yet earns no achievements. The fix is to re-encode the container
off zstd onto the long-standing codecs every libchdr reads (`cdlz,cdzl,cdfl` for CD,
`lzma,zlib,huff,flac` for DVD). The disc *data* is byte-identical, so the RA hash is
unchanged — only the wrapper differs.

Two halves, mirroring the rest of the app:
  * **Detection** — pure, needs no binary: read the CHD v5 header (32 bytes) and look
    at the four compressor FourCCs. Stamped onto `LibraryEntry.chd_codec`
    ("" = not a CHD / unreadable, "ok" = safe, "cdzs"/"zstd" = needs re-encode).
  * **Conversion** — best-effort, needs `chdman` on PATH (Docker image installs it).
    `chdman copy` to a temp file, confirm the Data SHA1 matches the original, then
    atomically swap. NB: a `chdman` too old to *read* zstd input (e.g. the distro
    package) can re-encode non-zstd CHDs but will fail on a cdzs input — surfaced via
    `chd_format_status()` so it's diagnosable, with the R: batch as the reliable path.
"""

import asyncio
import logging
import shutil
from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import LibraryEntry
from app.services import logger as applog
from app.services import settings as app_settings

logger = logging.getLogger(__name__)

# Zstandard codec FourCCs that RetroArch's rcheevos libchdr can't decode → no cheevos.
# cdzs = CD Zstandard, zstd = generic/DVD Zstandard.
BAD_CODECS = {"cdzs", "zstd"}
# What we re-encode onto — universally-read codecs, by CHD type.
CD_CODECS = "cdlz,cdzl,cdfl"
DVD_CODECS = "lzma,zlib,huff,flac"

_CHDMAN_BIN = "chdman"


def _chdman() -> str | None:
    return shutil.which(_CHDMAN_BIN)


def read_chd_codecs(path: Path) -> list[str] | None:
    """The four compressor FourCCs from a CHD v5 header, or None if `path` isn't a
    readable CHD. Pure (32-byte read), no chdman. Pre-v5 CHDs can't be zstd, so they
    return [] (treated as safe)."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if len(head) < 16 or head[:8] != b"MComprHD":
        return None
    version = int.from_bytes(head[12:16], "big")
    if version < 5 or len(head) < 32:
        return []  # pre-v5 header has no FourCC compressor list; can't be zstd
    codecs: list[str] = []
    for i in range(16, 32, 4):
        tag = head[i:i + 4].decode("ascii", "ignore").strip("\x00")
        if tag:
            codecs.append(tag)
    return codecs


def chd_status(path: Path) -> str:
    """"cdzs"/"zstd" (needs re-encode), "ok" (safe CHD), or "" (not a CHD / unreadable)."""
    codecs = read_chd_codecs(path)
    if codecs is None:
        return ""
    for bad in ("cdzs", "zstd"):
        if bad in codecs:
            return bad
    return "ok"


async def _run(*args: str) -> tuple[int, str]:
    """Run a command, return (returncode, combined stdout+stderr text)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)  # 30 min/file
    return proc.returncode, out.decode(errors="replace")


def _data_sha1(info_text: str) -> str:
    for line in info_text.splitlines():
        if "Data SHA1:" in line:
            return line.split("Data SHA1:")[1].strip()
    return ""


async def convert_chd(path: Path) -> tuple[bool, str]:
    """Re-encode `path` off zstd in place. Verifies the new CHD's Data SHA1 matches the
    original (disc data identical) BEFORE atomically replacing the file — so a bad/partial
    convert never destroys the original. Returns (ok, message). Because we exec chdman with
    an argument list (no shell), filenames with spaces/( )/!/' are safe."""
    chdman = _chdman()
    if not chdman:
        return False, "chdman not available"
    try:
        rc, info = await _run(chdman, "info", "-i", str(path))
        if rc != 0:
            return False, f"chdman info failed: {info.strip()[:200]}"
        is_dvd = "Tag='DVD" in info
        old_sha = _data_sha1(info)
        if not old_sha:
            return False, "could not read original Data SHA1"
        codecs = (DVD_CODECS if is_dvd else CD_CODECS).split(",")
        tmp = path.with_name(path.stem + ".rfconv.chd")
        if tmp.exists():
            tmp.unlink()
        rc, out = await _run(chdman, "copy", "-i", str(path), "-o", str(tmp), "-c", *codecs)
        if rc != 0:
            tmp.unlink(missing_ok=True)
            # The common cause on a too-old distro chdman: can't decode zstd input.
            return False, f"chdman copy failed (chdman too old to read zstd?): {out.strip()[-200:]}"
        rc, new_info = await _run(chdman, "info", "-i", str(tmp))
        new_sha = _data_sha1(new_info) if rc == 0 else ""
        if not new_sha or new_sha != old_sha:
            tmp.unlink(missing_ok=True)
            return False, f"Data SHA1 mismatch (old={old_sha} new={new_sha}) — original kept"
        import os
        os.replace(tmp, path)  # atomic swap; data is provably identical
        return True, new_sha
    except asyncio.TimeoutError:
        return False, "chdman timed out"
    except Exception as exc:
        return False, f"convert error: {exc}"


async def run_chd_check(convert: bool | None = None) -> dict:
    """Walk every owned `.chd`, stamp `LibraryEntry.chd_codec`, and (when enabled +
    chdman present) re-encode the zstd ones. Gated on the `chd_format_check_enabled`
    opt-in setting. `convert=None` → convert when chdman is available; pass False to
    flag-only. Fresh session per file write (never held across the chdman await)."""
    from app.services import activity as activity_store

    with Session(engine) as session:
        if not app_settings.get_bool(session, "chd_format_check_enabled", False):
            return {"status": "disabled"}
        entries = [
            (e.id, e.file_path)
            for e in session.exec(select(LibraryEntry)).all()
            if (e.file_path or "").lower().endswith(".chd")
        ]

    chdman_ok = _chdman() is not None
    do_convert = chdman_ok if convert is None else (convert and chdman_ok)

    checked = flagged = converted = failed = missing = 0
    bad_ids = [eid for eid, _ in entries]  # overlay scope: all CHDs (cheap header read each)
    batch_id = "chd-check-batch"
    if entries:
        activity_store.start_batch(
            batch_id, f"Checking {len(entries)} CHD{'s' if len(entries) != 1 else ''}",
            len(entries), "rehash", entry_ids=bad_ids,
        )

    loop = asyncio.get_event_loop()
    for eid, fpath in entries:
        p = Path(fpath)
        if not p.exists():
            missing += 1
            activity_store.complete_entry(batch_id, eid)
            continue
        st = await loop.run_in_executor(None, chd_status, p)
        checked += 1
        new_size = None
        if st in BAD_CODECS:
            flagged += 1
            if do_convert:
                ok, msg = await convert_chd(p)
                if ok:
                    converted += 1
                    st = "ok"
                    try:
                        new_size = p.stat().st_size
                    except OSError:
                        new_size = None
                else:
                    failed += 1
                    applog.warning("library", f"CHD re-encode failed for {p.name}: {msg}",
                                   {"file": p.name})
        with Session(engine) as session:
            e = session.get(LibraryEntry, eid)
            if e:
                e.chd_codec = st
                if new_size is not None:
                    e.file_size = new_size
                session.add(e)
                session.commit()
        activity_store.complete_entry(batch_id, eid)

    with Session(engine) as session:
        app_settings.set(session, "sched_chdcheck_last_run", __import__("datetime").datetime.utcnow().isoformat())

    still_bad = flagged - converted
    applog.info(
        "library",
        f"CHD format check: {checked} checked, {flagged} on zstd, "
        f"{converted} re-encoded, {failed} failed, {still_bad} still need fixing"
        + ("" if chdman_ok else " (chdman unavailable — flag-only)"),
    )
    return {
        "status": "ok",
        "checked": checked,
        "flagged": flagged,
        "converted": converted,
        "failed": failed,
        "still_bad": still_bad,
        "missing": missing,
        "chdman_available": chdman_ok,
        "converted_in_app": do_convert,
    }


def chd_format_status(session: Session) -> dict:
    """DB-only snapshot for /api/status.chd (no file I/O) — counts read off the stamped
    `chd_codec` column, plus chdman availability + the opt-in flag + last run."""
    from sqlalchemy import func, select as sa_select

    total_chd = session.scalar(
        sa_select(func.count()).select_from(LibraryEntry).where(LibraryEntry.chd_codec != "")
    ) or 0
    bad = session.scalar(
        sa_select(func.count()).select_from(LibraryEntry).where(LibraryEntry.chd_codec.in_(list(BAD_CODECS)))
    ) or 0
    return {
        "enabled": app_settings.get_bool(session, "chd_format_check_enabled", False),
        "chdman_available": _chdman() is not None,
        "checked_chd": int(total_chd),
        "bad": int(bad),
        "last_run": app_settings.get(session, "sched_chdcheck_last_run", ""),
    }
