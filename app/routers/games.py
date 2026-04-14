from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.services.archive_client import ArchiveClient
from app.services.ra_client import SYSTEMS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"systems": SYSTEMS})


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = Query(default=""),
    system: str = Query(default=""),
):
    """HTMX endpoint: search archive.org and return a results partial."""
    results = []
    error = None

    if q:
        try:
            client = ArchiveClient()
            results = await client.search_collections(q, system)
        except Exception as exc:
            error = str(exc)

    return templates.TemplateResponse(
        request, "partials/search_results.html",
        {"results": results, "query": q, "error": error},
    )


@router.get("/games/{identifier}/files", response_class=HTMLResponse)
async def browse_files(
    request: Request,
    identifier: str,
    q: str = Query(default=""),
):
    """HTMX endpoint: list ROM files inside an archive.org item."""
    files = []
    error = None

    try:
        client = ArchiveClient()
        files = await client.get_files(identifier, name_filter=q)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request, "partials/file_list.html",
        {"files": files, "identifier": identifier, "query": q, "error": error},
    )
