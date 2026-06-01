"""Torrent / Usenet download-client integration for ROM Finder.

Searches Prowlarr, then submits the chosen release to qBittorrent (torrents) or
SABnzbd (usenet). This is a `download_client` extension, NOT a `rom_source`:
downloads are asynchronous/external, so the hunt submits at HTTP-source exhaustion
(last resort) and `scheduler.run_poll_external` watches each job to completion.

API notes (verified against qBit 5.2.1 / WebAPI 2.15.1, SABnzbd 5.x, Prowlarr v1):
- qBittorrent 5.x renamed resume/pause → start/stop and the add-param `paused` →
  `stopped`. We detect the WebAPI version once and branch. LAN auth-bypass is
  common (the version endpoint answered without a cookie), so username/password
  are OPTIONAL — we only log in if a username is set.
- SABnzbd has no per-file selection, so multi-ROM usenet packs are skipped upstream.
- Prowlarr ROM categorization is unreliable, so we search a broad category set and
  rely on title/file matching (done in app/services/download_clients/selection.py).
"""

EXTENSION_INFO = {
    "id": "download_client",
    "name": "Torrent / Usenet (Prowlarr + qBittorrent + SABnzbd)",
    "version": "1.0.0",
    "type": "download_client",
    "author": "ConnorBS",
    "description": (
        "Last-resort downloader: searches Prowlarr indexers and submits torrents to "
        "qBittorrent or NZBs to SABnzbd. Used only after the direct HTTP sources fail. "
        "qBittorrent packs are trimmed to the wanted ROM via file-priority; usenet "
        "multi-ROM packs are skipped. The ROM is hash-verified against RetroAchievements "
        "after the external client finishes."
    ),
}

EXTENSION_SETTINGS = [
    {"key": "prowlarr_url", "label": "Prowlarr URL", "type": "text",
     "default": "http://192.168.0.82:9696", "description": "Base URL of your Prowlarr instance."},
    {"key": "prowlarr_api_key", "label": "Prowlarr API Key", "type": "text", "default": "",
     "description": "Prowlarr → Settings → General → API Key."},
    {"key": "prowlarr_categories", "label": "Prowlarr Categories", "type": "text",
     "default": "1000,1030,1060,1020,8000,4050",
     "description": "Comma-separated Torznab category IDs to search (Console=1000, Wii=1030, WiiWare=1060, PSP=1020, Other=8000, PC/Games=4050)."},
    {"key": "qbit_url", "label": "qBittorrent URL", "type": "text",
     "default": "http://192.168.0.87:8080", "description": "Base URL of qBittorrent WebUI. Leave blank to disable torrents."},
    {"key": "qbit_username", "label": "qBittorrent Username", "type": "text", "default": "",
     "description": "Leave blank if LAN auth-bypass is enabled."},
    {"key": "qbit_password", "label": "qBittorrent Password", "type": "password", "default": ""},
    {"key": "qbit_category", "label": "qBittorrent Category", "type": "text", "default": "rom-finder",
     "description": "Category assigned to submitted torrents (used to find the just-added torrent)."},
    {"key": "qbit_save_path", "label": "qBittorrent Save Path", "type": "text", "default": "",
     "description": "Optional explicit download path. Must be readable by the ROM Finder container so it can import the file."},
    {"key": "sab_url", "label": "SABnzbd URL", "type": "text",
     "default": "http://192.168.0.97:8080", "description": "Base URL of SABnzbd. Leave blank to disable usenet."},
    {"key": "sab_api_key", "label": "SABnzbd API Key", "type": "text", "default": "",
     "description": "SABnzbd → Config → General → API Key."},
    {"key": "sab_category", "label": "SABnzbd Category", "type": "text", "default": "", "description": "Optional SABnzbd category."},
]

import asyncio
import re

import httpx

from app.services.download_clients.base import DownloadClient
from app.services.sources.errors import SourceNetworkError, SourceForbiddenError

_BTIH_RE = re.compile(r"xt=urn:btih:([0-9A-Za-z]+)", re.IGNORECASE)

# qBit states meaning "still downloading / not yet complete".
_QBIT_ACTIVE = {"downloading", "stalledDL", "metaDL", "queuedDL", "forcedDL",
                "checkingDL", "allocating", "checkingResumeData", "moving"}
_QBIT_PAUSED = {"pausedDL", "stoppedDL"}
_QBIT_DONE = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP",
              "forcedUP", "checkingUP"}
_QBIT_FAILED = {"error", "missingFiles"}


def _b32_to_hex(s: str) -> str:
    import base64
    try:
        return base64.b32decode(s.upper()).hex()
    except Exception:
        return s.lower()


def _infohash_from_magnet(magnet: str) -> str:
    m = _BTIH_RE.search(magnet or "")
    if not m:
        return ""
    h = m.group(1)
    return h.lower() if len(h) == 40 else _b32_to_hex(h)


class TorrentUsenetClient(DownloadClient):
    client_id = "download_client"
    name = "Torrent / Usenet (Prowlarr + qBittorrent + SABnzbd)"

    def configure(self, config: dict) -> None:
        g = lambda k, d="": (config.get(k) or d).strip()  # noqa: E731
        self.prowlarr_url = g("prowlarr_url").rstrip("/")
        self.prowlarr_key = g("prowlarr_api_key")
        self.categories = [c.strip() for c in g("prowlarr_categories", "1000,8000").split(",") if c.strip()]
        self.qbit_url = g("qbit_url").rstrip("/")
        self.qbit_user = g("qbit_username")
        self.qbit_pass = config.get("qbit_password") or ""
        self.qbit_category = g("qbit_category", "rom-finder")
        self.qbit_save_path = g("qbit_save_path")
        self.sab_url = g("sab_url").rstrip("/")
        self.sab_key = g("sab_api_key")
        self.sab_category = g("sab_category")
        self._qbit_v5: bool | None = None
        # Which protocols are actually usable given what's configured.
        self.protocols = set()
        if self.qbit_url:
            self.protocols.add("torrent")
        if self.sab_url and self.sab_key:
            self.protocols.add("usenet")

    # Default attrs so the instance is valid before configure() (registry/UI).
    prowlarr_url = ""; prowlarr_key = ""; categories = ["1000", "8000"]
    qbit_url = ""; qbit_user = ""; qbit_pass = ""; qbit_category = "rom-finder"; qbit_save_path = ""
    sab_url = ""; sab_key = ""; sab_category = ""
    _qbit_v5 = None

    # ------------------------------------------------------------------ Prowlarr
    async def search(self, query: str, system: str = "") -> list[dict]:
        if not (self.prowlarr_url and self.prowlarr_key):
            return []
        params = [("query", query), ("type", "search"), ("limit", "100")]
        params += [("categories", c) for c in self.categories]
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(f"{self.prowlarr_url}/api/v1/search",
                                params=params, headers={"X-Api-Key": self.prowlarr_key})
                if r.status_code in (401, 403):
                    raise SourceForbiddenError(f"Prowlarr auth failed (HTTP {r.status_code})")
                r.raise_for_status()
                rows = r.json()
        except (SourceForbiddenError, SourceNetworkError):
            raise
        except Exception as exc:
            raise SourceNetworkError(f"Prowlarr search failed: {exc}")

        out: list[dict] = []
        for x in rows if isinstance(rows, list) else []:
            proto = (x.get("protocol") or "").lower()
            if proto not in self.protocols:
                continue
            out.append({
                "title": x.get("title", ""),
                "download_url": x.get("downloadUrl") or x.get("magnetUrl") or "",
                "magnet_url": x.get("magnetUrl") or "",
                "info_hash": (x.get("infoHash") or "").lower(),
                "size": x.get("size") or 0,
                "seeders": x.get("seeders"),
                "protocol": proto,
                "indexer": x.get("indexer", ""),
            })
        return out

    # ------------------------------------------------------------------ submit
    async def submit(self, release: dict, save_path: str) -> dict:
        proto = release.get("protocol")
        if proto == "torrent":
            return await self._qbit_add(release, save_path or self.qbit_save_path)
        if proto == "usenet":
            return await self._sab_add(release)
        raise SourceNetworkError(f"Unsupported protocol: {proto}")

    # ------------------------------------------------------------------ qBittorrent
    def _qbit(self) -> httpx.AsyncClient:
        # One client per op-group; cookie persists across calls on the same client.
        return httpx.AsyncClient(base_url=self.qbit_url, timeout=30,
                                 headers={"Referer": self.qbit_url, "Origin": self.qbit_url})

    async def _qbit_login(self, c: httpx.AsyncClient) -> None:
        if not self.qbit_user:
            return  # rely on LAN auth-bypass
        r = await c.post("/api/v2/auth/login", data={"username": self.qbit_user, "password": self.qbit_pass})
        if r.text.strip() != "Ok.":
            raise SourceForbiddenError("qBittorrent login failed")

    async def _qbit_is_v5(self, c: httpx.AsyncClient) -> bool:
        if self._qbit_v5 is None:
            try:
                v = (await c.get("/api/v2/app/webapiVersion")).text.strip()
                parts = [int(p) for p in v.split(".")[:2]]
                self._qbit_v5 = parts >= [2, 11]   # WebAPI 2.11 ships with qBit 5.0
            except Exception:
                self._qbit_v5 = False
        return self._qbit_v5

    async def _qbit_add(self, release: dict, save_path: str) -> dict:
        url = release.get("download_url") or release.get("magnet_url")
        if not url:
            raise SourceNetworkError("Release has no download URL")
        c = self._qbit()
        try:
            await self._qbit_login(c)
            v5 = await self._qbit_is_v5(c)
            before = await self._qbit_hashes(c)
            data = {"urls": url, "category": self.qbit_category,
                    "stopped" if v5 else "paused": "true", "autoTMM": "false"}
            if save_path:
                data["savepath"] = save_path
            r = await c.post("/api/v2/torrents/add", data=data)
            if r.status_code == 415:
                raise SourceNetworkError("qBittorrent rejected the torrent (invalid file)")
            r.raise_for_status()
            # Identify the infohash: prefer Prowlarr's infoHash / the magnet btih,
            # else diff the torrent list (added paused under our category).
            handle = release.get("info_hash") or _infohash_from_magnet(url)
            if not handle:
                handle = await self._qbit_new_hash(c, before)
            return {"job_handle": handle, "protocol": "torrent", "needs_file_selection": True}
        finally:
            await c.aclose()

    async def _qbit_hashes(self, c: httpx.AsyncClient) -> set[str]:
        try:
            rows = (await c.get("/api/v2/torrents/info", params={"category": self.qbit_category})).json()
            return {t.get("hash", "") for t in rows}
        except Exception:
            return set()

    async def _qbit_new_hash(self, c: httpx.AsyncClient, before: set[str]) -> str:
        # Poll briefly for the newly-added torrent to appear.
        for _ in range(10):
            await asyncio.sleep(1)
            try:
                rows = (await c.get("/api/v2/torrents/info", params={"category": self.qbit_category})).json()
            except Exception:
                continue
            new = [t for t in rows if t.get("hash", "") not in before]
            if new:
                new.sort(key=lambda t: t.get("added_on", 0), reverse=True)
                return new[0].get("hash", "")
        return ""

    async def list_files(self, job_handle: str) -> list[dict]:
        if not job_handle:
            return []
        c = self._qbit()
        try:
            await self._qbit_login(c)
            r = await c.get("/api/v2/torrents/files", params={"hash": job_handle})
            if r.status_code != 200:
                return []
            rows = r.json()
            return [{"index": f.get("index"), "name": f.get("name", ""),
                     "size": f.get("size", 0), "priority": f.get("priority", 1)} for f in rows]
        except Exception:
            return []
        finally:
            await c.aclose()

    async def set_wanted_files(self, job_handle: str, keep_indices: list[int]) -> None:
        c = self._qbit()
        try:
            await self._qbit_login(c)
            files = await self._raw_files(c, job_handle)
            keep = set(keep_indices)
            unwanted = [str(f["index"]) for f in files if f.get("index") not in keep]
            if unwanted:
                await c.post("/api/v2/torrents/filePrio",
                             data={"hash": job_handle, "id": "|".join(unwanted), "priority": "0"})
            # Start the (paused) torrent — verb differs across qBit versions.
            v5 = await self._qbit_is_v5(c)
            r = await c.post(f"/api/v2/torrents/{'start' if v5 else 'resume'}", data={"hashes": job_handle})
            if r.status_code in (404, 405):  # version guess wrong — try the other verb
                await c.post(f"/api/v2/torrents/{'resume' if v5 else 'start'}", data={"hashes": job_handle})
        finally:
            await c.aclose()

    async def _raw_files(self, c: httpx.AsyncClient, h: str) -> list[dict]:
        try:
            return (await c.get("/api/v2/torrents/files", params={"hash": h})).json()
        except Exception:
            return []

    # ------------------------------------------------------------------ SABnzbd
    async def _sab_add(self, release: dict) -> dict:
        url = release.get("download_url")
        params = {"output": "json", "apikey": self.sab_key, "mode": "addurl", "name": url}
        if self.sab_category:
            params["cat"] = self.sab_category
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                data = (await c.get(f"{self.sab_url}/api", params=params)).json()
        except Exception as exc:
            raise SourceNetworkError(f"SABnzbd add failed: {exc}")
        if not data.get("status"):
            raise SourceNetworkError(f"SABnzbd add rejected: {data.get('error', 'unknown')}")
        ids = data.get("nzo_ids") or []
        handle = ids[0] if ids else ""
        # nzo_ids can be empty (NZB fetched async) — the poller then matches by title.
        return {"job_handle": handle, "protocol": "usenet", "needs_file_selection": False,
                "release_title": release.get("title", "")}

    # ------------------------------------------------------------------ status
    async def status(self, job_handle: str, protocol: str) -> dict:
        if protocol == "torrent":
            return await self._qbit_status(job_handle)
        return await self._sab_status(job_handle)

    async def _qbit_status(self, h: str) -> dict:
        c = self._qbit()
        try:
            await self._qbit_login(c)
            rows = (await c.get("/api/v2/torrents/info", params={"hashes": h})).json()
            if not rows:
                return {"state": "unknown", "progress": 0.0, "completed": False, "failed": True,
                        "error": "torrent not found in qBittorrent"}
            t = rows[0]
            st = t.get("state", "unknown")
            return {
                "state": st,
                "progress": float(t.get("progress", 0.0)),
                "completed": st in _QBIT_DONE or float(t.get("progress", 0)) >= 1.0,
                "failed": st in _QBIT_FAILED,
                "content_path": t.get("content_path") or t.get("save_path", ""),
                "save_path": t.get("save_path", ""),
                "error": st if st in _QBIT_FAILED else "",
            }
        except Exception as exc:
            return {"state": "error", "progress": 0.0, "completed": False, "failed": False, "error": str(exc)}
        finally:
            await c.aclose()

    async def _sab_status(self, h: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                base = {"output": "json", "apikey": self.sab_key}
                hist = (await c.get(f"{self.sab_url}/api", params={**base, "mode": "history"})).json()
                for s in hist.get("history", {}).get("slots", []):
                    if s.get("nzo_id") == h:
                        done = s.get("status") == "Completed"
                        return {"state": s.get("status", ""), "progress": 1.0 if done else 0.99,
                                "completed": done, "failed": s.get("status") == "Failed",
                                "content_path": s.get("storage", ""), "save_path": s.get("storage", ""),
                                "error": s.get("fail_message", "")}
                q = (await c.get(f"{self.sab_url}/api", params={**base, "mode": "queue"})).json()
                for s in q.get("queue", {}).get("slots", []):
                    if s.get("nzo_id") == h:
                        return {"state": s.get("status", ""), "progress": float(s.get("percentage", 0)) / 100.0,
                                "completed": False, "failed": False, "content_path": "", "save_path": "", "error": ""}
        except Exception as exc:
            return {"state": "error", "progress": 0.0, "completed": False, "failed": False, "error": str(exc)}
        return {"state": "missing", "progress": 0.0, "completed": False, "failed": True,
                "error": "nzo not found in SABnzbd queue/history"}

    # ------------------------------------------------------------------ cleanup
    async def cleanup(self, job_handle: str, protocol: str, delete_files: bool = False) -> None:
        try:
            if protocol == "torrent" and self.qbit_url:
                c = self._qbit()
                try:
                    await self._qbit_login(c)
                    await c.post("/api/v2/torrents/delete",
                                 data={"hashes": job_handle, "deleteFiles": "true" if delete_files else "false"})
                finally:
                    await c.aclose()
            elif protocol == "usenet" and self.sab_url:
                async with httpx.AsyncClient(timeout=30) as c:
                    await c.get(f"{self.sab_url}/api", params={
                        "output": "json", "apikey": self.sab_key, "mode": "history",
                        "name": "delete", "value": job_handle,
                        "del_files": "1" if delete_files else "0"})
        except Exception:
            pass


CLIENT_CLASS = TorrentUsenetClient
