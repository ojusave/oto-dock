# Deploy OtoDock on Render

Docs-only packaging of [OtoDock](https://github.com/OtoDock/oto-dock) for Render. This fork does **not** change upstream application code.

Blueprint: [`render.yaml`](./render.yaml)  
Image: `ghcr.io/otodock/otodock-proxy:1.1.0`

## Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://dashboard.render.com/select-repo?type=blueprint)

1. Apply this repo as a Blueprint ([create account](https://dashboard.render.com/register?utm_source=github&utm_medium=referral&utm_campaign=ojus_demos&utm_content=readme_link) if needed).
2. Set `DASHBOARD_PUBLIC_URL` to `https://<service>.onrender.com` when prompted (or after first URL is assigned).
3. Watch the web service deploy and open `/health`.

## Resources

| Resource | Type | Plan | Role |
| --- | --- | --- | --- |
| `otodock` | Web (`runtime: image`) | Standard | Official proxy + dashboard image |
| `otodock-data` | Disk 10 GB at `/var/otodock` | — | Agents / sessions / config persist |
| `otodock-db` | Postgres 16 | Basic 1 GB | Platform DB |

## Verified Render fit (do not skip)

Upstream boots only when nested user namespaces work. Against the official image:

| Container profile | `unshare -Urn true` | Proxy boot |
| --- | --- | --- |
| Default Docker seccomp (Render-like) | Fails: `Operation not permitted` | Stock `netns_preflight` **hard-fails** |
| Compose with `seccomp=unconfined` | Succeeds | Boots |

Render Blueprints cannot set `security_opt`, `/dev/net/tun`, or a Docker daemon socket. Expect **deploy failure** on `/health` unless Render's runtime later allows nested userns. Prefer full [Docker Compose self-host](https://github.com/OtoDock/oto-dock#quick-start) for a working agent platform.

Also unavailable on Render vs compose:

| Compose piece | Render |
| --- | --- |
| `docker-socket-proxy` / Docker MCPs | No host Docker socket |
| Shared `file-tools` agents volume | Disks are one service each |
| Collabora (`MKNOD`) | Omitted from this Blueprint |

## Smoke check

```bash
curl -fsS "https://<your-service>.onrender.com/health"
```

## License

Upstream: **FSL-1.1-Apache-2.0**. This docs packaging does not relicense OtoDock.
