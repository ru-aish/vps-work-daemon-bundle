# daemon_v2

Two-daemon async verification system.

## Components
- `cloud_daemon.py` (VPS): queue + worker + scheduler v2 + status API.
- `local_daemon.py` (PC): submit CSV tasks, poll status, write output CSV/TSV.
- `scheduler_v2.py`: provider/IP round-robin with provider and provider+ip cooldown.

## Cloud API
- `GET /health`
- `POST /enqueue` body: `{ "rows": [{"firm_name","website","person_full_name"}, ...] }`
- `GET /status?job_id=...`

## Local API
- `GET /health`
- `GET /submit?input=/abs/path/file.tsv&output=/abs/path/out.tsv`
- `GET /tasks`
