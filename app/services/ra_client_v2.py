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

    async def get_hub_games(self, hub_id: int, page: int = 1, size: int = 100) -> dict:
        """`/hubs/{id}/games` — the games in an RA hub (series/theme), one page.
        `include=system` so each game carries its console."""
        resp = await self.get(f"/hubs/{hub_id}/games",
                              params={"include": "system", "page[number]": page, "page[size]": size})
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def games_from_payload(payload: dict) -> list[dict]:
        """Games from a `/hubs/{id}/games?include=system` page →
        [{game_id, title, console, achievements, points, players}]. The achievement
        count drives the hub-import "has achievements" filter; V2 exposes no game-type
        (retail/hack/…) field, so those RA filters aren't reproducible here."""
        inc = {(i.get("type"), str(i.get("id"))): i for i in (payload.get("included") or [])}
        out = []
        for d in (payload.get("data") or []):
            a = d.get("attributes", {}) or {}
            console = ""
            sid = (((d.get("relationships") or {}).get("system") or {}).get("data") or {}).get("id")
            if sid:
                sysrec = inc.get(("systems", str(sid)))
                if sysrec:
                    console = (sysrec.get("attributes") or {}).get("name", "")
            gid = d.get("id")
            out.append({"game_id": int(gid) if str(gid).isdigit() else 0,
                        "title": a.get("title", ""), "console": console,
                        "achievements": int(a.get("achievementsPublished") or 0),
                        "points": int(a.get("pointsTotal") or 0),
                        "players": int(a.get("playersTotal") or 0)})
        return out

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
    def _types_set(types) -> set:
        t = types if isinstance(types, list) else [types]
        return {str(x).lower() for x in t if x}

    @staticmethod
    def sets_from_game(payload: dict) -> list[dict]:
        """Multiset achievement sets from /games/{id}?include=achievementSets,hashes →
        [{set_id, title, set_type, compatibility, patch_url, points_total}], EXCLUDING the
        core set. compatibility: specialty/exclusive sets need a patch (patch-required,
        with the game's patch_url); bonus/challenge play on the base ROM (compatible)."""
        inc = payload.get("included") or []
        patch_url = ""
        for i in inc:
            if i.get("type") == "game-hashes":
                a = i.get("attributes", {}) or {}
                comp = (a.get("compatibility") or "").lower().replace("_", "-")
                if comp == "patch-required" and a.get("patchUrl"):
                    patch_url = a["patchUrl"]
                    break
        out = []
        for i in inc:
            if i.get("type") != "achievement-sets":
                continue
            a = i.get("attributes", {}) or {}
            types = RAClientV2._types_set(a.get("types"))
            if "core" in types:
                continue   # the base set, not a subset
            patch_required = bool(types & {"specialty", "exclusive"})
            sid = i.get("id")
            out.append({
                "set_id": int(sid) if str(sid).isdigit() else 0,
                "title": a.get("title", ""),
                "set_type": ",".join(sorted(types)),
                "compatibility": "patch-required" if patch_required else "compatible",
                "patch_url": patch_url if patch_required else "",
                "points_total": a.get("pointsTotal", 0) or 0,
            })
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
