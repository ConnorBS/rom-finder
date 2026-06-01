"""In-memory registry of download-client integrations (mirrors sources/registry)."""
from app.services.download_clients.base import DownloadClient

_clients: dict[str, DownloadClient] = {}


def register(client: DownloadClient) -> None:
    _clients[client.client_id] = client


def unregister(client_id: str) -> None:
    _clients.pop(client_id, None)


def get(client_id: str) -> DownloadClient | None:
    return _clients.get(client_id)


def all_clients() -> list[DownloadClient]:
    return list(_clients.values())
