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
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select, func

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
from app.services.title_utils import search_title, search_variations, significant_terms, title_is_relevant
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
# Kept as a flood-guard but raised so an exhaustive multi-source/multi-region
# candidate list isn't truncated before a verified dump is reached.
_MAX_CANDIDATES = 40

# A *download* failure (timeout / CDN 403 / network) is transient — token CDNs
# (ROMsFun, WowROMs) 403 intermittently even on a good file — so allow this many
# attempts across hunts before giving up on a file. A `bad_hash` is NOT counted
# here: a wrong dump re-downloads to the same hash, so it stays blocked at once.
_MAX_DOWNLOAD_RETRIES = 3

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


def _ra_base_id(x):
    """RA hub/multiset synthetic ids encode the base game id in the low 8 digits:
    `pseudo = type * 10^8 + base_game_id` (e.g. 1200034728 → 34728, 1100002271 → 2271;
    real game ids are < 10^8). A ROM that RA resolves to a *set of* game N hashes to
    such a synthetic id; decoding it lets the wanted base game (N) verify. Real ids
    pass through unchanged, so the wrong-game guard still rejects a genuinely
    different base."""
    return x % 100000000 if x and x >= 100000000 else x


def _match_is_correct_game(matched_id, expected_id) -> bool:
    """True only when the RA hash matched the EXPECTED game — comparing BASE game ids
    so a multiset/hub synthetic id (1200034728) matches its base game (34728). A match
    to a different base (a Solaris ROM during a Kirby hunt) must NOT verify."""
    return bool(matched_id) and (expected_id is None or _ra_base_id(matched_id) == _ra_base_id(expected_id))


def _owned_accepted_copy(session: Session, accepted_hashes: set[str]):
    """An owned LibraryEntry whose hash is in this game's accepted-hash list, or
    None. Used to skip hunting a game the user already owns a satisfying copy of.

    This is the SUBSET case: a Subset game's ra_game_id differs from the base
    game's, and the subset reuses the base ROM (e.g. SM64 'Coin Collector'
    accepts the plain Super Mario 64 (USA) dump). The user owns that ROM under
    the BASE game's id, so add-wanted's `ra_game_id` ownership check can't see it
    and the hunt would download a redundant, byte-identical duplicate."""
    lowered = [h.lower() for h in accepted_hashes if h]
    if not lowered:
        return None
    return session.exec(
        select(LibraryEntry)
        .where(LibraryEntry.file_hash.isnot(None))
        .where(func.lower(LibraryEntry.file_hash).in_(lowered))
    ).first()


def _verified_game_id(matched_id, expected_id, file_hash, accepted_hashes) -> int | None:
    """The RA game id this dump verifies as, or None if it doesn't belong to the
    wanted game. Accepts two ways:
      • the dump's hash is in the EXPECTED game's own accepted-hash list — the
        authoritative per-game set. This is the **subset** path: a Subset game's
        ra_game_id differs from the base game's, so RA's hash lookup returns the
        BASE id, but the subset reuses the base ROM whose hash IS in the subset's
        list, so it verifies as the wanted (subset) id; or
      • RA's hash lookup returns the expected id (the normal path).
    A hash matching a DIFFERENT game that is NOT in the accepted list (a Solaris
    ROM during a Kirby hunt) returns None — the wrong dump."""
    if file_hash and file_hash.lower() in accepted_hashes:
        return expected_id
    if _match_is_correct_game(matched_id, expected_id):
        # Return the wanted (base) id — never a multiset/hub synthetic id — so the
        # Download/LibraryEntry carries the real game id for covers + collection links.
        return expected_id if expected_id is not None else matched_id
    return None


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
        use_review = _gs(session, "use_review_dir", "true") == "true"
        srcs = _enabled_srcs(session)
        game_title = game.game_title
        system = game.system
        ra_game_id = game.ra_game_id
        # Final target = the primary root (or its legacy download_dir fallback).
        from app.services import library_roots
        target_base, system_folder, primary_root_id = library_roots.download_target(session, system)

    if not ra_username or not ra_api_key:
        applog.warning("hunt", "Auto-hunt skipped — RA credentials not configured", {"game": game_title})
        return

    base_dir = check_dir if use_review else target_base
    target_root_id = None if use_review else primary_root_id

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

        # Already own a satisfying copy? Then there's nothing to hunt — downloading
        # another copy just makes a byte-identical duplicate. Common for SUBSETs:
        # the subset reuses the base ROM, owned under the BASE game's id, so the
        # add-wanted ownership check (by ra_game_id) can't see it. Mark verified
        # and stop. (SM64 'Coin Collector' accepts plain Super Mario 64 (USA),
        # which the user already owns.)
        owned_info: tuple | None = None
        if ra_hashes:
            with Session(engine) as session:
                owned = _owned_accepted_copy(session, ra_hashes)
                if owned:
                    owned_info = (owned.file_name, owned.file_hash, owned.id)
                    g = session.get(WantedGame, wanted_id)
                    if g:
                        g.status = HuntStatus.verified
                        g.last_hunt_at = datetime.utcnow()
                        session.add(g)
                        session.commit()
        if owned_info:
            applog.info("hunt", f"Already owned — no download needed: {game_title}", {
                "wanted_id": wanted_id, "owned_file": owned_info[0],
                "owned_hash": owned_info[1], "owned_library_id": owned_info[2],
            })
            return

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
        # Derive match terms from the SEARCH title — a platform suffix
        # ("Ristar (Genesis/Mega Drive)") or "[Subset …]" tag otherwise poisons the
        # term set so the real ROM is judged irrelevant and a whole-system romset
        # (which happens to contain "genesis/mega/drive") outscores it. search_title
        # drops both (clean_title keeps [Subset] for the stored title).
        title_terms = _significant_terms(search_title(game_title))

        async def _try_external() -> bool:
            """LAST RESORT: when the HTTP sources can't produce a verified dump,
            hand the game to a configured torrent/usenet download client (it then
            downloads async and the scheduler poll ingests + RA-verifies it)."""
            try:
                from app.services.external_hunt import submit_external
                return await submit_external(wanted_id, ra_stems, title_terms, ra_hashes, system, game_title)
            except Exception as exc:
                applog.warning("hunt", f"External fallback error: {exc}", {"wanted_id": wanted_id})
                return False

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
            if await _try_external():
                return
            _mark_exhausted(wanted_id)
            return

        # Load previously attempted (source, identifier, file) combos to skip.
        # A `verified`/`bad_hash` attempt blocks the file permanently (re-trying
        # yields the same hash). A `download_failed` is transient — a token CDN
        # 403s intermittently even on a good file — so it only blocks once it has
        # failed _MAX_DOWNLOAD_RETRIES times, letting a fixed download path or a
        # passing rate-limit recover a file that merely 403'd before.
        with Session(engine) as session:
            prior = session.exec(
                select(HuntAttempt).where(HuntAttempt.wanted_game_id == wanted_id)
            ).all()
            past: set[tuple[str, str, str]] = set()
            past_urls: set[str] = set()
            dl_fail_counts: Counter = Counter()
            for a in prior:
                akey = (a.source_id, a.identifier, a.file_name)
                if a.result == "download_failed":
                    dl_fail_counts[akey] += 1
                    if dl_fail_counts[akey] < _MAX_DOWNLOAD_RETRIES:
                        continue  # retryable — don't block yet
                past.add(akey)
                if a.source_url:
                    past_urls.add(a.source_url)

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

            # Create a transient Download row so this attempt shows a live progress
            # card + Cancel in the UI (hunt downloads previously had no visible
            # progress — only the tray label). file_path stays None until the
            # verified file is staged (the ux_download_path partial-unique index
            # forbids duplicate non-null paths). On verify we reuse this row; on
            # bad_hash/failure we delete it (the HuntAttempt is the durable audit).
            with Session(engine) as session:
                dl = Download(
                    game_title=game_title, system=system, file_name=file_name,
                    file_path=None, source_url=source_url, source_id=src.source_id,
                    archive_identifier=identifier, status=DownloadStatus.downloading,
                    progress=0.0, ra_game_id=ra_game_id, hunt_task_id=task_id,
                )
                session.add(dl)
                session.commit()
                session.refresh(dl)
                dl_id = dl.id

            async def on_progress(fraction: float, _dl_id=dl_id):
                with Session(engine) as s:
                    d = s.get(Download, _dl_id)
                    if d:
                        d.progress = fraction
                        d.updated_at = datetime.utcnow()
                        s.add(d)
                        s.commit()

            def _set_dl_status(st, _dl_id=dl_id):
                with Session(engine) as s:
                    d = s.get(Download, _dl_id)
                    if d:
                        d.status = st
                        d.updated_at = datetime.utcnow()
                        s.add(d)
                        s.commit()

            try:
                applog.info("hunt", f"Trying: {file_name}", {
                    "wanted_id": wanted_id, "source": src.source_id,
                    "identifier": identifier, "score": score, "url": source_url,
                })
                try:
                    await asyncio.wait_for(
                        src.download_file(source_url, dest, on_progress),
                        timeout=300,  # 5 min max per attempt
                    )
                except asyncio.TimeoutError:
                    applog.warning("hunt", f"Download timed out (5 min): {file_name}", {"wanted_id": wanted_id})
                    raise RuntimeError("Download timed out after 5 minutes")

                _set_dl_status(DownloadStatus.hashing)
                rom_path = dest
                if dest.suffix.lower() in (".zip", ".7z"):
                    try:
                        rom_path = extract_rom_from_zip(dest, prefer_name=file_name)
                    except zipfile.BadZipFile:
                        real = dest.with_suffix("")
                        dest.rename(real)
                        rom_path = real

                file_hash, _ = await ra_hash_or_fallback(rom_path, system)

                _set_dl_status(DownloadStatus.verifying)
                match = await ra.lookup_hash(file_hash)
                matched_id = match.get("ID") if match else None
                # Accept when the dump belongs to the EXPECTED game, either way:
                #  • RA's hash lookup returns the expected id, OR
                #  • the dump's hash is in the wanted game's OWN accepted-hash list
                #    (ra_hashes = get_game_hashes_full(ra_game_id)).
                # The second path is how a "Subset" game verifies: its ra_game_id
                # differs from the base game's, so lookup_hash returns the BASE id —
                # but the subset reuses the base ROM whose hash IS in the subset's
                # accepted list. A hash matching a DIFFERENT game NOT in that list
                # (a Solaris ROM during a Kirby hunt) is still the wrong dump.
                verified_id = _verified_game_id(matched_id, ra_game_id, file_hash, ra_hashes)
                if verified_id is not None:
                    # Move verified file to normal staging dir
                    stage_dir = Path(base_dir) / system_folder
                    stage_dir.mkdir(parents=True, exist_ok=True)
                    final_path = stage_dir / rom_path.name
                    shutil.move(str(rom_path), str(final_path))
                    _cleanup(dest, rom_path)

                    matched_ra_id = verified_id
                    dl_status = DownloadStatus.pending_approval if use_review else DownloadStatus.completed

                    with Session(engine) as session:
                        # Reuse the transient progress row created for this attempt
                        # (don't create a second Download) — promote it to the
                        # verified/staged file.
                        d = session.get(Download, dl_id)
                        if d is None:
                            d = Download(source_id=src.source_id, source_url=source_url)
                            session.add(d)
                        d.game_title = game_title
                        d.system = system
                        d.file_name = final_path.name
                        d.file_path = str(final_path)
                        d.source_url = source_url
                        d.source_id = src.source_id
                        d.archive_identifier = identifier
                        d.status = dl_status
                        d.progress = 1.0
                        d.file_hash = file_hash
                        d.hash_verified = True
                        d.ra_game_id = matched_ra_id
                        d.hunt_task_id = None  # terminal — no longer a cancellable in-flight hunt row
                        d.updated_at = datetime.utcnow()
                        session.add(d)
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
                                root_id=target_root_id,
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

            # This block runs only for bad_hash / download_failed (success returns
            # above). Delete the transient progress card — the HuntAttempt below is
            # the durable record — so a failed attempt leaves no orphaned download.
            with Session(engine) as session:
                d = session.get(Download, dl_id)
                if d is not None:
                    session.delete(d)
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
        if await _try_external():
            return
        _mark_exhausted(wanted_id)
        applog.info("hunt", f"Auto-hunt exhausted all candidates: {game_title}", {
            "wanted_id": wanted_id, "tried": tried,
        })

    except Exception as exc:
        applog.error("hunt", f"Auto-hunt crashed: {exc}", {"wanted_id": wanted_id})
    finally:
        activity_store.finish(task_id)
        # Safety net: drop any transient hunt Download row that never reached a
        # terminal state (e.g. a crash mid-attempt or a user cancel), so the
        # downloads page isn't left with a stuck "downloading" card.
        try:
            with Session(engine) as s:
                stale = s.exec(
                    select(Download).where(
                        Download.hunt_task_id == task_id,
                        Download.status.in_([
                            DownloadStatus.downloading,
                            DownloadStatus.hashing,
                            DownloadStatus.verifying,
                        ]),
                    )
                ).all()
                for d in stale:
                    s.delete(d)
                if stale:
                    s.commit()
        except Exception:
            pass
