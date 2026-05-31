"""Auto-hunt service: search → download → RA-verify → retry on bad hash.

Mirrors Sonarr/Radarr's grab logic:
  1. Search all enabled sources using RA ROM names + title variations
  2. Expand each result to individual files, score by match quality
  3. For each candidate (best score first):
     a. Skip if already attempted (HuntAttempt record exists)
     b. Download to a temp path in _hunt/ subfolder
     c. Hash with RA hasher (platform-specific) + MD5 fallback
     d. Look up hash against RetroAchievements API
     e. Verified → move to staging, create Download(pending_approval), done
     f. Bad hash → record HuntAttempt(bad_hash), delete file, continue
     g. Error → record HuntAttempt(download_failed), continue
  4. All candidates tried with no match → mark WantedGame.status = exhausted
"""

import asyncio
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import (
    AppSetting, Download, DownloadStatus, HuntAttempt, HuntStatus,
    LibraryEntry, WantedGame,
)
from app.services import activity as activity_store
from app.services import logger as applog
from app.services import sources as source_registry
from app.services.hasher import extract_rom_from_zip, hash_rom
from app.services.ra_client import DEFAULT_FOLDER_MAP, RAClient
from app.services.rahasher import compute_ra_hash, ra_hash_or_fallback
from app.services.title_utils import search_variations, significant_terms, title_is_relevant
from app.services import settings as app_settings


def _gs(session: Session, key: str, default: str = "") -> str:
    s = session.get(AppSetting, key)
    return s.value if s else default


def _enabled_srcs(session: Session) -> list:
    enabled = {
        src.source_id
        for src in source_registry.all_sources()
        if _gs(session, f"source_{src.source_id}_enabled", "false") == "true"
    }
    return source_registry.enabled_sources(enabled)


# Cap on candidate files actually downloaded per hunt — stops a loose source
# match (e.g. a whole NDS romset) from triggering hundreds of download attempts.
_MAX_CANDIDATES = 20

# Significant title words + the result-relevance predicate live in title_utils
# now, so the Wanted-page source search and this hunt agree on what's a match
# ("search == hunt"). Kept under the old name for the existing import/test.
_significant_terms = significant_terms


def _file_score(file_name: str, ra_stems: set[str], title_terms: set[str]) -> int:
    """Score a candidate by likelihood of being the right dump.

    Returns 0 when the filename matches NEITHER an RA-accepted ROM name NOR the
    game title — so score 0 reliably means "unrelated game" even when RA hashes
    couldn't be loaded (previously a region freebie gave every file a nonzero
    score, so unrelated NDS files for a Wii hunt slipped through)."""
    stem = Path(file_name).stem.lower()
    name_score = 0

    if stem in ra_stems:
        name_score = 100
    else:
        for rs in ra_stems:
            if rs and (rs in stem or stem in rs):
                name_score = 20
                break

    # Fallback: require the game title's significant words to appear in the name.
    if name_score == 0 and title_terms:
        present = sum(1 for t in title_terms if t in stem)
        if present == len(title_terms):
            name_score = 30
        elif len(title_terms) >= 3 and present >= len(title_terms) - 1:
            name_score = 10

    if name_score == 0:
        return 0  # unrelated — never download

    low = file_name.lower()
    if "(usa)" in low or "(world)" in low:
        name_score += 10
    elif "(europe)" in low:
        name_score += 3
    elif "(japan)" in low:
        name_score += 2
    return name_score


def _cleanup(*paths: Path) -> None:
    seen: set[Path] = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _mark_exhausted(wanted_id: int) -> None:
    with Session(engine) as session:
        g = session.get(WantedGame, wanted_id)
        if g and g.status == HuntStatus.hunting:
            g.status = HuntStatus.exhausted
            g.last_hunt_at = datetime.utcnow()
            session.add(g)
            session.commit()


def _match_is_correct_game(matched_id, expected_id) -> bool:
    """True only when the RA hash matched the EXPECTED game (or no expected id is
    known). A match to a DIFFERENT game means the wrong dump was downloaded
    (e.g. a Solaris ROM matching RA during a Kirby hunt) and must NOT verify."""
    return bool(matched_id) and (expected_id is None or matched_id == expected_id)


async def auto_hunt(wanted_id: int) -> None:
    """Run the full auto-hunt pipeline for a single wanted game."""
    task_id = f"hunt-{wanted_id}"

    with Session(engine) as session:
        game = session.get(WantedGame, wanted_id)
        if not game:
            return
        ra_username = _gs(session, "ra_username")
        ra_api_key = _gs(session, "ra_api_key")
        check_dir = _gs(session, "check_dir", "/rom-check")
        download_dir = _gs(session, "download_dir", "/roms")
        use_review = _gs(session, "use_review_dir", "true") == "true"
        folder_map = app_settings.get_json(session, "folder_map", {})
        srcs = _enabled_srcs(session)
        game_title = game.game_title
        system = game.system
        ra_game_id = game.ra_game_id

    if not ra_username or not ra_api_key:
        applog.warning("hunt", "Auto-hunt skipped — RA credentials not configured", {"game": game_title})
        return

    system_folder = folder_map.get(system) or DEFAULT_FOLDER_MAP.get(system, system)
    base_dir = check_dir if use_review else download_dir

    activity_store.start(task_id, f"Hunting: {game_title}", task_type="hunt")
    applog.info("hunt", f"Auto-hunt started: {game_title}", {"wanted_id": wanted_id, "system": system})

    try:
        ra = RAClient(ra_username, ra_api_key)

        # Fetch RA-accepted hashes and ROM name stems for ranking candidates
        ra_hashes: set[str] = set()
        ra_stems: set[str] = set()
        try:
            for h in await ra.get_game_hashes_full(ra_game_id):
                if h.get("MD5"):
                    ra_hashes.add(h["MD5"].lower())
                if h.get("Name"):
                    ra_stems.add(Path(h["Name"]).stem.lower())
        except Exception as exc:
            applog.warning("hunt", f"Could not fetch RA hashes: {exc}", {"wanted_id": wanted_id})

        # Build ordered search queries: RA ROM name stems first, then title variants
        queries: list[str] = []
        seen_q: set[str] = set()
        for stem in list(ra_stems)[:3]:
            if stem and stem not in seen_q:
                queries.append(stem)
                seen_q.add(stem)
        for v in search_variations(game_title):
            if v not in seen_q:
                queries.append(v)
                seen_q.add(v)

        # Search each source. Stop at the first query that yields a plausibly
        # matching title — a junk-only early hit (a sibling 'Pajama Sam' game)
        # must not short-circuit a better later query (mirrors the Wanted-page
        # search). Unrelated hits are still collected and later dropped by
        # _file_score == 0, so a generically-titled archive.org collection that
        # actually holds the file isn't lost.
        title_terms = _significant_terms(game_title)
        search_results: list[tuple] = []  # (src, result_dict)
        for src in srcs:
            src_hits: list[tuple] = []
            for query in queries:
                try:
                    found = await src.search(query, system)
                except Exception as exc:
                    applog.warning("hunt", f"Search error ({src.source_id}): {exc}")
                    continue
                src_hits.extend((src, r) for r in found)
                if any(title_is_relevant(r.get("title") or r.get("identifier", ""), title_terms)
                       for r in found):
                    break
            search_results.extend(src_hits)

        # Expand to individual files and score
        candidates: list[tuple[int, object, str, dict]] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for src, result in search_results:
            identifier = result.get("identifier", "")
            if not identifier:
                continue
            try:
                for f in await src.get_files(identifier):
                    fname = f.get("name", "")
                    key = (src.source_id, identifier, fname)
                    if key not in seen_keys:
                        score = _file_score(fname, ra_stems, title_terms)
                        # score 0 = matches neither an RA ROM name nor the game
                        # title → unrelated game from a loose/collection search.
                        # Always skip (works even when RA stems failed to load).
                        if score == 0:
                            continue
                        seen_keys.add(key)
                        candidates.append((score, src, identifier, f))
            except Exception as exc:
                applog.warning("hunt", f"get_files error ({src.source_id}): {exc}", {"identifier": identifier})
                continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        # Hard cap: only attempt the best N. Stops a loose collection match from
        # triggering hundreds of downloads (the "insane amount of downloads").
        if len(candidates) > _MAX_CANDIDATES:
            applog.info("hunt", f"Capping candidates: {len(candidates)} → {_MAX_CANDIDATES}",
                        {"wanted_id": wanted_id})
            candidates = candidates[:_MAX_CANDIDATES]

        if not candidates:
            applog.info("hunt", f"No downloadable files found: {game_title}", {"wanted_id": wanted_id})
            _mark_exhausted(wanted_id)
            return

        # Load previously attempted (source, identifier, file) combos to skip
        with Session(engine) as session:
            prior = session.exec(
                select(HuntAttempt).where(HuntAttempt.wanted_game_id == wanted_id)
            ).all()
            past: set[tuple[str, str, str]] = {
                (a.source_id, a.identifier, a.file_name) for a in prior
            }
            past_urls: set[str] = {a.source_url for a in prior if a.source_url}

        tried = 0
        for score, src, identifier, file_info in candidates:
            if activity_store.is_cancelled(task_id):
                applog.info("hunt", f"Hunt cancelled by user: {game_title}", {"wanted_id": wanted_id})
                break

            file_name = file_info.get("name", f"rom_{tried}.zip")
            # Resolve the actual download URL — a stable per-file identity used for
            # dedup (skip re-downloading the same file) and recorded for audit.
            # Extension sources store the CDN/mirror URL in the file's own identifier.
            file_identifier = file_info.get("identifier") or identifier
            source_url = src.get_download_url(file_identifier, file_name)
            key = (src.source_id, identifier, file_name)
            if key in past or (source_url and source_url in past_urls):
                continue

            tried += 1
            activity_store.update_label(task_id, f"Hunting: {game_title} (attempt {tried})")

            hunt_dir = Path(base_dir) / "_hunt" / system_folder
            hunt_dir.mkdir(parents=True, exist_ok=True)
            dest = hunt_dir / file_name
            rom_path = dest
            result_code = "download_failed"
            file_hash: str | None = None

            try:
                applog.info("hunt", f"Trying: {file_name}", {
                    "wanted_id": wanted_id, "source": src.source_id,
                    "identifier": identifier, "score": score, "url": source_url,
                })
                try:
                    await asyncio.wait_for(
                        src.download_file(source_url, dest, None),
                        timeout=300,  # 5 min max per attempt
                    )
                except asyncio.TimeoutError:
                    applog.warning("hunt", f"Download timed out (5 min): {file_name}", {"wanted_id": wanted_id})
                    raise RuntimeError("Download timed out after 5 minutes")

                rom_path = dest
                if dest.suffix.lower() in (".zip", ".7z"):
                    try:
                        rom_path = extract_rom_from_zip(dest, prefer_name=file_name)
                    except zipfile.BadZipFile:
                        real = dest.with_suffix("")
                        dest.rename(real)
                        rom_path = real

                file_hash, _ = await ra_hash_or_fallback(rom_path, system)

                match = await ra.lookup_hash(file_hash)
                matched_id = match.get("ID") if match else None
                # Only accept when the hash matches the EXPECTED game. A file whose
                # hash matches a DIFFERENT RA game (e.g. a Solaris ROM downloaded
                # during a Kirby hunt) is the wrong dump — must NOT verify the wanted
                # game. Mirrors the downloads.py approval fix.
                if _match_is_correct_game(matched_id, ra_game_id):
                    # Move verified file to normal staging dir
                    stage_dir = Path(base_dir) / system_folder
                    stage_dir.mkdir(parents=True, exist_ok=True)
                    final_path = stage_dir / rom_path.name
                    shutil.move(str(rom_path), str(final_path))
                    _cleanup(dest, rom_path)

                    matched_ra_id = matched_id
                    dl_status = DownloadStatus.pending_approval if use_review else DownloadStatus.completed

                    with Session(engine) as session:
                        session.add(Download(
                            game_title=game_title, system=system,
                            file_name=final_path.name, file_path=str(final_path),
                            source_url=source_url, source_id=src.source_id,
                            archive_identifier=identifier, status=dl_status,
                            progress=1.0, file_hash=file_hash, hash_verified=True,
                            ra_game_id=matched_ra_id,
                        ))
                        session.add(HuntAttempt(
                            wanted_game_id=wanted_id, source_id=src.source_id,
                            identifier=identifier, file_name=final_path.name,
                            source_url=source_url, file_hash=file_hash, result="verified",
                        ))
                        g = session.get(WantedGame, wanted_id)
                        if g:
                            g.status = HuntStatus.verified
                            g.last_hunt_at = datetime.utcnow()
                            session.add(g)
                        if not use_review:
                            session.add(LibraryEntry(
                                game_title=game_title, system=system,
                                file_name=final_path.name, file_path=str(final_path),
                                file_hash=file_hash, hash_verified=True,
                                ra_game_id=matched_ra_id, ra_matched=True,
                            ))
                        session.commit()

                    applog.info("hunt", f"Verified: {final_path.name} [{file_hash}]", {
                        "wanted_id": wanted_id, "source": src.source_id, "attempts": tried,
                        "url": source_url,
                    })
                    return  # success

                else:
                    if matched_id:
                        applog.warning(
                            "hunt",
                            f"Hash matched RA game {matched_id}, expected {ra_game_id} — wrong dump, skipping {file_name}",
                            {"wanted_id": wanted_id, "matched_id": matched_id,
                             "expected_id": ra_game_id, "hash": file_hash},
                        )
                    result_code = "bad_hash"
                    applog.info("hunt", f"Bad hash: {file_name} [{file_hash}]",
                                {"wanted_id": wanted_id, "url": source_url})
                    _cleanup(dest, rom_path)

            except Exception as exc:
                result_code = "download_failed"
                applog.warning("hunt", f"Attempt failed ({src.source_id}): {exc}", {
                    "wanted_id": wanted_id, "file": file_name, "url": source_url,
                })
                _cleanup(dest, rom_path)

            with Session(engine) as session:
                session.add(HuntAttempt(
                    wanted_game_id=wanted_id, source_id=src.source_id,
                    identifier=identifier, file_name=file_name,
                    source_url=source_url, file_hash=file_hash, result=result_code,
                ))
                g = session.get(WantedGame, wanted_id)
                if g:
                    g.last_hunt_at = datetime.utcnow()
                    session.add(g)
                session.commit()
            past.add(key)
            if source_url:
                past_urls.add(source_url)

        if tried == 0:
            applog.info("hunt", f"All candidates already attempted: {game_title}", {"wanted_id": wanted_id})
        _mark_exhausted(wanted_id)
        applog.info("hunt", f"Auto-hunt exhausted all candidates: {game_title}", {
            "wanted_id": wanted_id, "tried": tried,
        })

    except Exception as exc:
        applog.error("hunt", f"Auto-hunt crashed: {exc}", {"wanted_id": wanted_id})
    finally:
        activity_store.finish(task_id)
