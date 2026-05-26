# Deploy mechanism (Unraid)

Push to `main` → GitHub Actions builds `ghcr.io/connorbs/rom-finder:latest` and
POSTs the Unraid webhook → the webhook runs `redeploy.sh` which pulls the new
image and restarts the `rom-finder` container.

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
container's logs for the `fork/exec` error above:

```
docker logs rom-finder-webhook --tail 40
```
