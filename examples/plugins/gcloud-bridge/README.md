# gcloud bridge — Google Cloud SDK from the container

Gives the disposable Docker container access to a host-authenticated Google Cloud
SDK (`gcloud` / `gsutil` / `bq`) over a token-gated HTTP bridge — same pattern as
the qmd / transcribe / voice / browser bridges.

> **Getting cloud access (teammates):** cloud credentials are **not self-service**.
> Request access from the **DevOps team**, who provision a **scoped, read-only identity**
> (least-privilege / impersonated service account) for your agent and hand back the bridge
> config. Don't reuse a personal full-access login — the whole point is a read-only boundary
> at the source. See `SECURITY.md` → "Cloud credentials are provisioned by DevOps".

- **Account:** `<your-google-account>` (the host's gcloud login)
- **Port:** `host.docker.internal:18187` (sibling to qmd 18181, slot3d 18183,
  browser 18184, transcribe 18185, voice 18186)
- **Auth lives on the host**, never in the container. The container holds only a
  bearer token (`.secrets/gcloud-bridge.env`) and proxies commands.

## Why a bridge (not gcloud-in-the-container)
`gcloud auth login` is interactive (browser) and credentials are long-lived —
keeping both on the Mac means the container stays disposable and never holds
Google credentials. The container sends an argv array; the host runs the SDK
binary **without a shell** (no injection) and relays stdout/stderr/exit-code.

## One-time setup

### On the Mac (operator, once)
```bash
bash tools/gcloud/host-bridge-setup.sh     # installs SDK if missing + launchd agent on :18187
gcloud auth login                          # opens browser -> <your-google-account>
gcloud config set project <PROJECT_ID>
```
Installs a `KeepAlive` launchd agent (`org.enclave.gcloud-bridge`) so it survives
reboots. Logs: `.gcloud-bridge.log`.

### In the container (once per fresh container)
```bash
bash tools/gcloud/container-setup.sh       # symlinks gcloud/gsutil/bq shims into ~/.local/bin
```

## Usage (from the container)
After container-setup, the CLIs work transparently via PATH shims:
```bash
gcloud projects list
gcloud config get-value account
gsutil ls gs://my-bucket
bq query --use_legacy_sql=false 'SELECT 1'
```
Or call the client directly (no PATH shim needed):
```bash
python3 tools/gcloud/bridge.py --health
python3 tools/gcloud/bridge.py projects list
python3 tools/gcloud/bridge.py --tool gsutil ls gs://my-bucket
```

## Security model
- Bearer token (`GCLOUD_BRIDGE_TOKEN` in `.secrets/gcloud-bridge.env`, gitignored).
- Only `{gcloud, gsutil, bq}` may be invoked; argv runs without a shell.
- This grants the container the **same GCP power** the host login has — it is a
  trust bridge, **not** a sandbox against destructive commands. Treat container
  gcloud calls with the same care as running them locally.

## Operate
- Health (container): `python3 tools/gcloud/bridge.py --health`
- Logs (Mac):    `tail -f .gcloud-bridge.log`
- Restart (Mac): `launchctl kickstart -k gui/$(id -u)/org.enclave.gcloud-bridge`
- Stop (Mac):    `launchctl bootout gui/$(id -u)/org.enclave.gcloud-bridge`
