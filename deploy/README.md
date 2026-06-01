# Deploy mechanism (Unraid)

Push to `main` → GitHub Actions runs the test gate, builds + pushes
`ghcr.io/connorbs/rom-finder:latest` (git SHA baked in as `APP_VERSION`). Something
on Unraid then pulls that image and recreates the `rom-finder` container.

## ✅ Recommended: Watchtower (auto-pull from the LAN) — `docker-compose.watchtower.yml`

Watchtower polls GHCR from **inside the LAN** and pulls+restarts only containers
labelled `com.centurylinklabs.watchtower.enable=true` (so the *arr stack is
untouched). After a push, the new build is live within the poll interval (5 min) —
fully automatic, **no public endpoint and no webhook**, which sidesteps both webhook
failure modes below.

1. Add the Watchtower container (`containrrr/watchtower:latest`) with the
   `docker.sock` mount + env in `docker-compose.watchtower.yml`.
2. Label the `rom-finder` container `com.centurylinklabs.watchtower.enable=true`
   (`redeploy.sh` already sets this on the container it creates).
3. Private GHCR? Also mount the host's `~/.docker/config.json` (see the compose).

Verify: `curl http://192.168.0.100:19846/api/status` → `version` matches the pushed
SHA within ~5 min of the CI build.

## Legacy: the webhook path (`almir/webhook` + `hooks.json` + `redeploy.sh`)

GitHub Actions → a **public** `DEPLOY_WEBHOOK_URL` → the webhook container →
`redeploy.sh`. Kept for reference, but **deprecated** in favour of Watchtower because
it broke on BOTH ends (see *Failure modes* below).

## Host setup (one-time, on Unraid)

The `rom-finder-webhook` container (`almir/webhook`) bind-mounts two files from
`/mnt/ssd_cache/appdata/rom-finder-webhook/`:

| Host file | Mounted at | Purpose |
|---|---|---|
| `redeploy.sh` | `/scripts/redeploy.sh` (ro) | pulls + restarts the container |
| `hooks.json`  | `/hooks/hooks.json` (ro)   | webhook definition + secret token |

Copy `deploy/redeploy.sh` and `deploy/hooks.json` from this repo to that host
folder, set the real token in `hooks.json`, and `chmod +x redeploy.sh`.

## ⚠️ These files MUST have LF (Unix) line endings

`redeploy.sh` is executed by its shebang (`#!/bin/sh`). If the file has CRLF
(Windows) line endings, the kernel looks for the interpreter `/bin/sh\r`, which
doesn't exist, and the webhook fails with:

```
error occurred: fork/exec /scripts/redeploy.sh: no such file or directory
```

The webhook still returns HTTP 200 (the hook *matched*), so **CI looks green
while the deploy silently no-ops and prod stays frozen on the old image.** This
happened in practice — prod sat on a 3-week-old commit until it was caught.

The repo now enforces LF on `*.sh` via `.gitattributes`, but a file copied to
the host from an older/Windows checkout can still have CRLF. To fix or verify
the host copy:

```sh
# On Unraid — strip CR and any UTF-8 BOM, then make executable
sed -i '1s/^\xEF\xBB\xBF//; s/\r$//' /mnt/ssd_cache/appdata/rom-finder-webhook/redeploy.sh
chmod +x /mnt/ssd_cache/appdata/rom-finder-webhook/redeploy.sh
```

## Verifying a deploy

The app reports its own deployed SHA — no host access needed:

```sh
curl -s http://192.168.0.100:19846/api/status | jq .version   # == pushed git SHA
```

If `.version` lags the pushed SHA while CI is green, check the webhook
container's logs:

```
docker logs rom-finder-webhook --tail 40
```

## Webhook failure modes (why Watchtower is recommended)

The deploy job is `continue-on-error: true`, so the GitHub run shows **green even
when the redeploy never happened**. Three ways the webhook path has failed:

1. **CRLF in `redeploy.sh`** → `fork/exec … no such file or directory` (see above).
2. **HTTP 502 from the public route** (diagnosed 2026-06-01) — GitHub's cloud runner
   can only reach the LAN through the tunnel/reverse-proxy in front of
   `DEPLOY_WEBHOOK_URL`, and it was down. The deploy step logs
   `Webhook response: HTTP 502`. Fix: restore the tunnel/proxy mapping to the webhook
   container's `:9000`.
3. **`permission denied … /var/run/docker.sock`** (diagnosed 2026-06-01) — the
   `rom-finder-webhook` container runs as the non-root **`webhook`** user, so
   `redeploy.sh`'s `docker pull/stop/run` is denied. Fix: run that container as
   **root** (Unraid template → Extra Parameters `--user 0:0`) or add the docker-socket
   group (`--group-add <gid>`).

Watchtower has none of these failure modes (LAN-local, runs as root, no public
route), so prefer it. If you keep the webhook, you must fix BOTH #2 and #3.
