"""RetroAchievements **V2** API client (read-only, minimal).

The V2 API is a JSON:API service on a DIFFERENT host from V1 — `https://api.retroachievements.org/v2`
(V1 is `retroachievements.org/API/*.php`). It exposes data V1 can't: an event's award TIERS
(event → `awards`), per-achievement `points`/`pointsWeighted`, and an achievement's SOURCE game
(achievement → `games`) — the pieces needed for the Goals event header + per-achievement game/console.

Docs source: https://github.com/Chew/RA-api-docs/tree/feat/v2-docs/docs/v2

Auth + Cloudflare reachability are NOT yet confirmed from this codebase's environment — use the
`/api/diag/ra-v2` probe (which calls this) to verify in a deployment that has the API key + network,
THEN wire the UI. We send the web API key as a Bearer token (the documented V2 scheme); the probe
reports the real status/shape so the auth can be corrected if RA expects something else.
"""
import httpx

from app.services.ra_client import _limiter  # share the global 2 req/s limiter

RA_V2_BASE = "https://api.retroachievements.org/v2"


class RAClientV2:
    def __init__(self, api_key: str, username: str = ""):
        self.api_key = api_key
        self.username = username

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        await _limiter.wait()
        async with httpx.AsyncClient() as client:
            return await client.get(f"{RA_V2_BASE}{path}", headers=self._headers(),
                                    params=params or {}, timeout=20)

    async def get_event(self, event_id: int, include: str = "awards") -> dict:
        resp = await self.get(f"/events/{event_id}", params={"include": include} if include else None)
        resp.raise_for_status()
        return resp.json()

    async def get_achievement(self, achievement_id: int, include: str = "games") -> dict:
        resp = await self.get(f"/achievements/{achievement_id}", params={"include": include} if include else None)
        resp.raise_for_status()
        return resp.json()
