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
        # V2 is JSON:API and ONLY produces application/vnd.api+json — Accept: application/json got HTTP 406.
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/vnd.api+json"}

    async def get(self, path: str, params: dict | None = None) -> httpx.Response:
        await _limiter.wait()
        async with httpx.AsyncClient() as client:
            return await client.get(f"{RA_V2_BASE}{path}", headers=self._headers(),
                                    params=params or {}, timeout=20)

    async def get_event(self, event_id: int, include: str = "awards") -> dict:
        resp = await self.get(f"/events/{event_id}", params={"include": include} if include else None)
        resp.raise_for_status()
        return resp.json()

    async def get_achievement(self, achievement_id: int, include: str = "games.system") -> dict:
        resp = await self.get(f"/achievements/{achievement_id}", params={"include": include} if include else None)
        resp.raise_for_status()
        return resp.json()

    async def get_game(self, game_id: int, include: str = "") -> dict:
        """`/games/{id}` → box art (`imageBoxArtUrl`), `medianTimeToBeatMinutes`,
        `releasedAt`, points, etc. `include=achievementSets,hashes` for set-aware data."""
        resp = await self.get(f"/games/{game_id}", params={"include": include} if include else None)
        resp.raise_for_status()
        return resp.json()

    # --- JSON:API payload parsers (static; tolerant of missing fields) ---------

    @staticmethod
    def tiers_from_event(payload: dict) -> list[dict]:
        """Award tiers from an /events/{id}?include=awards payload →
        [{title, kind, points_required, badge_url}] sorted by threshold."""
        out = []
        for inc in (payload.get("included") or []):
            if inc.get("type") not in ("user-awards", "awards", "event-awards"):
                continue
            a = inc.get("attributes", {}) or {}
            pr = a.get("pointsRequired", a.get("points_required"))
            out.append({
                "title": a.get("title", ""),
                "kind": a.get("kind", ""),
                "points_required": pr if isinstance(pr, int) else None,
                "badge_url": a.get("badgeUrl", a.get("badge_url", "")),
            })
        out.sort(key=lambda t: (t["points_required"] is None, t["points_required"] or 0))
        return out

    @staticmethod
    def source_game_from_achievement(payload: dict) -> dict | None:
        """The achievement's SOURCE game + console from
        /achievements/{id}?include=games.system → {game_id, title, console}."""
        inc = payload.get("included") or []
        games = [i for i in inc if i.get("type") == "games"]
        if not games:
            return None
        g = games[0]
        ga = g.get("attributes", {}) or {}
        # console: the game's system, resolved from the included systems (nested include)
        console = ""
        sys_id = (((g.get("relationships") or {}).get("system") or {}).get("data") or {}).get("id")
        if sys_id:
            for i in inc:
                if i.get("type") == "systems" and str(i.get("id")) == str(sys_id):
                    console = (i.get("attributes") or {}).get("name", "")
                    break
        return {"game_id": g.get("id"), "title": ga.get("title", ""), "console": console}
