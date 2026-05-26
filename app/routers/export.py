"""Hash export — CSV/JSON of the library for emulator frontends / external tools."""

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse, JSONResponse
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import LibraryEntry

router = APIRouter(prefix="/export")

_FIELDS = ["game_title", "system", "file_name", "file_hash",
           "ra_game_id", "ra_matched", "hash_verified"]


@router.get("/hashes")
async def export_hashes(
    format: str = Query("csv", pattern="^(csv|json)$"),
    verified_only: bool = Query(False, description="Only RA-matched entries"),
    session: Session = Depends(get_session),
):
    q = select(LibraryEntry).order_by(LibraryEntry.system, LibraryEntry.game_title)
    if verified_only:
        q = q.where(LibraryEntry.ra_matched == True)  # noqa: E712
    entries = session.exec(q).all()

    if format == "json":
        return JSONResponse([{f: getattr(e, f) for f in _FIELDS} for e in entries])

    def _stream():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_FIELDS)
        yield buf.getvalue()
        for e in entries:
            buf.seek(0); buf.truncate(0)
            writer.writerow([getattr(e, f) for f in _FIELDS])
            yield buf.getvalue()

    return StreamingResponse(
        _stream(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rom-finder-hashes.csv"},
    )
