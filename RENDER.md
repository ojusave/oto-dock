# Deploy OtoDock on Render

Docs-only packaging of [OtoDock](https://github.com/OtoDock/oto-dock) for Render. This fork does **not** change upstream application code.

Blueprint: [`render.yaml`](./render.yaml)  
Image: `ghcr.io/otodock/otodock-proxy:1.1.0`

## Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://dashboard.render.com/select-repo?type=blueprint)

1. Apply this repo as a Blueprint ([create account](https://dashboard.render.com/register?utm_source=github&utm_medium=referral&utm_campaign=ojus_demos&utm_content=readme_link) if needed).
2. Set `DASHBOARD_PUBLIC_URL` to `https://<service>.onrender.com` when prompted (or right after the URL is assigned).
3. Wait until the web service is **Live**, then open `/health`.

Verified deploy (Ojus render prod): [otodock](https://dashboard.render.com/web/srv-d9bt9fm1a83c73btj2jg) → https://otodock.onrender.com

## Resources

| Resource | Type | Plan | Role |
| --- | --- | --- | --- |
| `otodock` | Web (`runtime: image`) | Standard | Official proxy + dashboard image |
| `otodock-data` | Disk 10 GB at `/var/otodock` | — | Agents / sessions / `config.env` |
| `otodock-db` | Postgres 16 | Basic 1 GB | Platform DB |

## Verified on Render (2026-07-15)

Against `ghcr.io/otodock/otodock-proxy:1.1.0` on Render Standard + managed Postgres + disk:

| Check | Result |
| --- | --- |
| Image pull + process start | Succeeded |
| Schema init + workers | Succeeded |
| `GET /health` | `200` with `"status":"ok"` |
| Service status | **Live** |
| Docker MCP `file-tools` start | Failed: no `/var/run/docker.sock` (expected) |

Notes from the live run:

- Bind `PROXY_PORT=8400` (and `PORT=8400`). Health checks hit `:8400`.
- Leave `dockerCommand` empty so the image `CMD` runs. A custom shell one-liner via the API was mis-parsed and exited 127.
- Pre-set secrets as env vars (`PROXY_API_KEY`, `JWT_SECRET`, …). VAPID keys persist to `/var/otodock/config.env` on the disk.

Local Docker Desktop still blocks `unshare -Urn` under default seccomp; Render’s runtime allowed the stock proxy to complete `netns_preflight` in this deploy. Treat nested-userns behavior as host-dependent, and confirm with a real deploy rather than assuming either outcome.

## Still unavailable vs compose

| Compose piece | On Render |
| --- | --- |
| `docker-socket-proxy` / Docker MCPs | No host Docker socket (see live `file-tools` error above) |
| Shared `file-tools` agents volume | Disks are one service each |
| Collabora (`MKNOD`) | Not in this Blueprint |

Full local agent sandboxes and Docker MCP tooling still want a self-hosted [Docker Compose](https://github.com/OtoDock/oto-dock#quick-start) host (or satellites) for compose-level fidelity.

## Smoke check

```bash
curl -fsS "https://otodock.onrender.com/health"
```

## License

Upstream: **FSL-1.1-Apache-2.0**. This docs packaging does not relicense OtoDock.
