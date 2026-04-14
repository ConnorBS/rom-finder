from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import LibraryEntry

router = APIRouter(prefix="/library")
templates = Jinja2Templates(directory="app/templates")


@router.get("", response_class=HTMLResponse)
async def library_page(
    request: Request,
    system: str = Query(default=""),
    q: str = Query(default=""),
    session: Session = Depends(get_session),
):
    statement = select(LibraryEntry)

    if system:
        statement = statement.where(LibraryEntry.system == system)
    if q:
        statement = statement.where(LibraryEntry.game_title.contains(q))

    entries = session.exec(statement.order_by(LibraryEntry.game_title)).all()
    systems = session.exec(
        select(LibraryEntry.system).distinct()
    ).all()

    return templates.TemplateResponse(
        request, "library.html",
        {"entries": entries, "systems": systems, "selected_system": system, "query": q},
    )
