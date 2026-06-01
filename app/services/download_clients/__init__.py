"""Download-client integrations (torrent/usenet) — a third extension type beside
ROM sources and cover sources.

Unlike a `RomSource` (whose `download_file` streams to disk synchronously within
the hunt's 5-min window), a download client SUBMITS a job to an external app
(qBittorrent / SABnzbd via a Prowlarr search) that downloads asynchronously over
minutes-to-hours. So these are NOT part of the in-hunt source loop; the hunt
submits at HTTP-source exhaustion (last resort) and a scheduler poll task
(`scheduler.run_poll_external`) watches each job to completion, then ingests +
RA-verifies the file the same way a normal download is.
"""
from app.services.download_clients import registry  # noqa: F401
