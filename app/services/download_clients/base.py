"""Contract for a download-client integration (torrent/usenet).

One concrete client exists today (`extensions/download_client.py`, Prowlarr search
+ qBittorrent + SABnzbd), so this stays a single thin ABC — no premature split
into per-backend classes.

Search returns *releases*; `submit` hands a release to the right backend by its
`protocol` and returns an opaque `job_handle` (qBit infohash or SAB nzo_id). The
poller calls `status(handle, protocol)` until terminal, then ingests the file at
the returned `content_path`/`save_path`. Torrents can be added paused and trimmed
to specific files via `list_files`/`set_wanted_files` (qBittorrent file-priority);
usenet cannot select per file.
"""
from abc import ABC, abstractmethod


class DownloadClient(ABC):
    client_id: str = ""
    name: str = ""
    available: bool = True
    # Which release protocols this client can handle.
    protocols: set[str] = {"torrent", "usenet"}

    def configure(self, config: dict) -> None:  # optional override
        """Apply ext_{id}_* settings (host/port/api_key/category/save_path)."""
        return None

    @abstractmethod
    async def search(self, query: str, system: str = "") -> list[dict]:
        """Search Prowlarr. Each release dict:
        {title, download_url, magnet_url, info_hash, size, seeders, protocol, indexer}."""

    @abstractmethod
    async def submit(self, release: dict, save_path: str) -> dict:
        """Submit a release to qBit (paused, for file selection) or SAB. Returns
        {job_handle, protocol, needs_file_selection: bool}."""

    async def list_files(self, job_handle: str) -> list[dict]:
        """Torrent only — files once metadata is present: [{index,name,size,priority}].
        Empty list = metadata not downloaded yet (poll again)."""
        return []

    async def set_wanted_files(self, job_handle: str, keep_indices: list[int]) -> None:
        """Torrent only — deselect every file except keep_indices (qBit filePrio 0),
        then start the (paused) torrent."""
        return None

    @abstractmethod
    async def status(self, job_handle: str, protocol: str) -> dict:
        """{state, progress(0..1), completed, failed, content_path, save_path, error}."""

    async def cleanup(self, job_handle: str, protocol: str, delete_files: bool = False) -> None:
        """Remove the job from the client (and optionally its files)."""
        return None
