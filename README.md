# vps_worker_bundle

Portable daemon + verifier bundle for VPS deployment.

## Contents
- `app/daemon_v2/cloud_daemon.py` (VPS cloud daemon)
- `app/daemon_v2/local_daemon.py` (optional local daemon)
- `app/dv_verifier.py` (verifier engine)
- `scripts/install.sh` (single entry installer)
- `scripts/setup_vps.sh` (system dependencies + systemd service)

## One-line install (no git login on VPS)
Use a public repo URL:

```bash
curl -fsSL <RAW_INSTALL_SH_URL> | REPO_URL=https://github.com/<org>/<repo>.git BUNDLE_PATH=vps_worker_bundle bash
```

Or use archive URL:

```bash
curl -fsSL <RAW_INSTALL_SH_URL> | ARCHIVE_URL=https://<host>/vps_worker_bundle.tar.gz bash
```

## Local test install
From this folder:

```bash
sudo TARGET_DIR=/opt/vps-work bash scripts/install.sh
```

## Smoke test
After install:

```bash
sudo bash /opt/vps-work/scripts/smoke_test.sh /opt/vps-work/app
```

## Service
- Name: `vps-work-cloud-daemon.service`
- Health: `curl http://127.0.0.1:8788/health`

## Notes
- Configure runtime via env vars in systemd service (`ADV_VERIFIER_HOME`, `CLOUD_DAEMON_PORT`, etc.)
- Default config file: `/opt/vps-work/app/config.json`
