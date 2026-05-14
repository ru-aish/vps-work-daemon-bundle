#!/usr/bin/env python3
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from daemon_v2.scheduler_v2 import SchedulerV2
from dv_verifier import verify_email, ensure_dv_schema, get_mx_for_domain

BASE_DIR = Path(os.getenv("ADV_VERIFIER_HOME", str(Path(__file__).resolve().parents[1])))
DB_PATH = Path(os.getenv("CLOUD_DB_PATH", str(BASE_DIR / "daemon_v2" / "cloud_daemon.db")))
CFG_PATH = Path(os.getenv("CLOUD_CFG_PATH", str(BASE_DIR / "config.json")))
RUNTIME_DV_DB = Path(os.getenv("RUNTIME_DV_DB", str(BASE_DIR / "daemon_v2" / "cloud_dv_runtime.db")))
HOST = os.getenv("CLOUD_DAEMON_HOST", "0.0.0.0")
PORT = int(os.getenv("CLOUD_DAEMON_PORT", "8788"))
SCHEDULER_LOCK = threading.Lock()


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_epoch():
    return int(time.time())


def norm_token(v: str) -> str:
    return "".join(ch for ch in (v or "").lower().strip() if ch.isalnum())


def norm_name(v: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (v or "").lower())).strip()


def norm_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].strip().strip(".")
    if d.startswith("www."):
        d = d[4:]
    d = d.split(":")[0]
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d):
        return ""
    return d


def split_name(full_name: str):
    clean = re.sub(r"[^A-Za-z ]+", " ", full_name or "").strip()
    parts = [p for p in clean.split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].lower(), ""
    return parts[0].lower(), parts[-1].lower()


def gen_candidates(first_name: str, last_name: str, domain: str):
    fn = norm_token(first_name)
    ln = norm_token(last_name)
    fi = fn[:1] if fn else ""
    li = ln[:1] if ln else ""
    patterns = [
        ("first.last", f"{fn}.{ln}"),
        ("firstlast", f"{fn}{ln}"),
        ("f.last", f"{fi}.{ln}"),
        ("flast", f"{fi}{ln}"),
        ("first", f"{fn}"),
        ("first_last", f"{fn}_{ln}"),
        ("first-last", f"{fn}-{ln}"),
        ("first.l", f"{fn}.{li}"),
        ("firstl", f"{fn}{li}"),
        ("fillast", f"{fi}{li}{ln}"),
    ]
    out, seen = [], set()
    rank = 1
    for rule, local in patterns:
        local = local.strip("._-")
        if not local or ".." in local:
            continue
        email = f"{local}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        out.append((email, rule, rank))
        rank += 1
    return out[:10]


def load_cfg():
    defaults = {
        "mode_switch": 1,
        "api_base": "",
        "smtp_timeout_sec": 8,
        "helo_host": "localhost",
        "mail_from": "noreply@localhost",
        "mx_api": {"enabled": False, "provider": "none", "endpoint": "", "api_key": ""},
        "ip_identities": [],
    }
    if not CFG_PATH.exists():
        return defaults
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = defaults.copy()
    cfg.update(raw or {})
    if not isinstance(cfg.get("mx_api"), dict):
        cfg["mx_api"] = defaults["mx_api"].copy()
    else:
        mx = defaults["mx_api"].copy()
        mx.update(cfg["mx_api"])
        cfg["mx_api"] = mx
    if not isinstance(cfg.get("ip_identities"), list):
        cfg["ip_identities"] = []
    return cfg


def discover_ipv4():
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show", "scope", "global"], text=True, timeout=5)
    except Exception:
        return []
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        cidr = parts[3]
        ip = cidr.split("/")[0]
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              total_items INTEGER NOT NULL DEFAULT 0,
              done_items INTEGER NOT NULL DEFAULT 0,
              success_items INTEGER NOT NULL DEFAULT 0,
              fail_items INTEGER NOT NULL DEFAULT 0,
              temp_fail_items INTEGER NOT NULL DEFAULT 0,
              notes_json TEXT
            );

            CREATE TABLE IF NOT EXISTS job_items (
              item_id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              group_key TEXT NOT NULL,
              firm_name TEXT,
              person_name TEXT,
              domain TEXT NOT NULL,
              provider TEXT NOT NULL,
              candidate_email TEXT NOT NULL,
              perm_rule TEXT,
              candidate_rank INTEGER,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_verdict TEXT,
              last_error TEXT,
              last_checked_at TEXT,
              source_ip TEXT,
              helo_host TEXT
            );

            CREATE TABLE IF NOT EXISTS group_state (
              job_id TEXT NOT NULL,
              group_key TEXT NOT NULL,
              status TEXT NOT NULL,
              winner_email TEXT,
              winner_verdict TEXT,
              PRIMARY KEY(job_id, group_key)
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
              scope TEXT NOT NULL,
              k TEXT NOT NULL,
              ready_at_epoch INTEGER NOT NULL,
              PRIMARY KEY(scope, k)
            );

            CREATE TABLE IF NOT EXISTS domain_policy (
              domain TEXT PRIMARY KEY,
              policy TEXT NOT NULL,
              source_verdict TEXT,
              updated_at TEXT NOT NULL,
              notes TEXT
            );
            """
        )


def recover_stale_running_state():
    """On daemon restart, re-queue any in-flight work instead of losing it."""
    with db() as c:
        c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now_iso(),))
        c.execute("UPDATE job_items SET status='pending' WHERE status='running'")


def set_domain_policy(domain: str, policy: str, source_verdict: str, notes: str = ""):
    with db() as c:
        c.execute(
            """
            INSERT INTO domain_policy(domain, policy, source_verdict, updated_at, notes)
            VALUES(?,?,?,?,?)
            ON CONFLICT(domain) DO UPDATE SET
              policy=excluded.policy,
              source_verdict=excluded.source_verdict,
              updated_at=excluded.updated_at,
              notes=excluded.notes
            """,
            (domain, policy, source_verdict, now_iso(), notes),
        )


def get_domain_policy(domain: str):
    with db() as c:
        row = c.execute("SELECT policy, source_verdict FROM domain_policy WHERE domain=?", (domain,)).fetchone()
        return (row[0], row[1]) if row else (None, None)


def apply_domain_policies_for_job(job_id: str):
    """Fast-path pending items using learned domain policy to avoid useless probes."""
    with db() as c:
        domains = c.execute(
            "SELECT DISTINCT domain FROM job_items WHERE job_id=? AND status='pending'",
            (job_id,),
        ).fetchall()

        for drow in domains:
            domain = drow[0]
            policy, source = get_domain_policy(domain)
            if not policy:
                continue

            if policy == "catch_all":
                winner_verdict = "catch_all_probable"
            elif policy == "reject_all":
                winner_verdict = "reject_all_or_invalid"
            elif policy == "hopeless_unknown":
                winner_verdict = "unknown"
            else:
                continue

            groups = c.execute(
                "SELECT DISTINCT group_key FROM job_items WHERE job_id=? AND domain=? AND status='pending'",
                (job_id, domain),
            ).fetchall()
            for grow in groups:
                gk = grow[0]
                winner = c.execute(
                    "SELECT candidate_email FROM job_items WHERE job_id=? AND group_key=? ORDER BY candidate_rank ASC, item_id ASC LIMIT 1",
                    (job_id, gk),
                ).fetchone()
                wemail = winner[0] if winner else ""
                c.execute(
                    "UPDATE group_state SET status='done', winner_email=?, winner_verdict=? WHERE job_id=? AND group_key=?",
                    (wemail, winner_verdict, job_id, gk),
                )
                c.execute(
                    "UPDATE job_items SET status='canceled', last_verdict=?, last_error=?, last_checked_at=? WHERE job_id=? AND group_key=? AND status='pending'",
                    (winner_verdict, f"domain_policy:{policy}:{source or ''}", now_iso(), job_id, gk),
                )


def cooldown_ready(scope: str, key: str) -> bool:
    with db() as c:
        row = c.execute("SELECT ready_at_epoch FROM cooldowns WHERE scope=? AND k=?", (scope, key)).fetchone()
        return (not row) or int(row[0]) <= now_epoch()


def cooldown_set(scope: str, key: str, seconds: int):
    ready = now_epoch() + max(0, int(seconds))
    with db() as c:
        c.execute(
            """
            INSERT INTO cooldowns(scope,k,ready_at_epoch) VALUES(?,?,?)
            ON CONFLICT(scope,k) DO UPDATE SET ready_at_epoch=excluded.ready_at_epoch
            """,
            (scope, key, ready),
        )


def enqueue_rows(rows: list[dict]) -> dict:
    cfg = load_cfg()
    providers_order = cfg.get("providers_order", ["gmail", "outlook", "custom"])
    job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    added = 0

    conn = sqlite3.connect(str(RUNTIME_DV_DB))
    ensure_dv_schema(conn)

    with db() as c:
        c.execute(
            "INSERT INTO jobs(job_id,status,created_at,updated_at,notes_json) VALUES(?,?,?,?,?)",
            (job_id, "queued", now_iso(), now_iso(), json.dumps({"providers_order": providers_order})),
        )

        for row in rows:
            firm = (row.get("firm_name") or "").strip()
            person = (row.get("person_full_name") or row.get("name") or "").strip()
            domain = norm_domain(row.get("website") or row.get("domain") or "")
            if not firm or not person or not domain:
                continue
            fn, ln = split_name(person)
            perms = gen_candidates(fn, ln, domain)

            mx, provider, _, _ = get_mx_for_domain(conn, domain, mode_switch=2, api_base=cfg["mx_api"]["base_url"], timeout_s=cfg["mx_api"].get("timeout_sec", 12))
            if provider not in providers_order:
                provider = "custom"

            group_key = f"{norm_name(firm)}|{norm_name(person)}|{domain}"
            c.execute(
                "INSERT OR IGNORE INTO group_state(job_id,group_key,status) VALUES(?,?,?)",
                (job_id, group_key, "pending"),
            )

            for email, rule, rank in perms:
                c.execute(
                    """
                    INSERT INTO job_items(job_id,group_key,firm_name,person_name,domain,provider,candidate_email,perm_rule,candidate_rank,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (job_id, group_key, firm, person, domain, provider, email, rule, rank, "pending"),
                )
                added += 1

        c.execute("UPDATE jobs SET total_items=?, updated_at=? WHERE job_id=?", (added, now_iso(), job_id))

    conn.close()
    return {"job_id": job_id, "total_items": added}


def _pick_header(headers, aliases):
    hmap = {re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower()): h for h in headers}
    for a in aliases:
        k = re.sub(r"[^a-z0-9]+", "", a.strip().lower())
        if k in hmap:
            return hmap[k]
    return ""


def enqueue_csv_text(csv_text: str, delimiter: str | None = None) -> dict:
    import csv
    import io

    if not csv_text.strip():
        return {"job_id": "", "total_items": 0}
    if delimiter is None:
        delimiter = "\t" if "\t" in csv_text[:4000] else ","

    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    headers = list(reader.fieldnames or [])
    firm_col = _pick_header(headers, ["Firm Name", "Firm", "Company", "Business Name"])
    web_col = _pick_header(headers, ["Website", "Web", "Domain", "URL", "Site"])
    name_col = _pick_header(headers, ["Main Person Full Name", "Full Name", "Name", "Contact Name", "Main Person"])

    rows = []
    for r in reader:
        rows.append(
            {
                "firm_name": (r.get(firm_col) or "").strip() if firm_col else "",
                "website": (r.get(web_col) or "").strip() if web_col else "",
                "person_full_name": (r.get(name_col) or "").strip() if name_col else "",
            }
        )
    return enqueue_rows(rows)


def fetch_job_status(job_id: str) -> dict:
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not j:
            return {"error": "not_found"}
        best = c.execute(
            """
            SELECT g.group_key, g.winner_email, g.winner_verdict, i.firm_name, i.person_name, i.domain
            FROM group_state g
            LEFT JOIN job_items i ON i.job_id=g.job_id AND i.group_key=g.group_key
            WHERE g.job_id=? AND g.status='done'
            GROUP BY g.group_key
            """,
            (job_id,),
        ).fetchall()

        return {
            "job_id": job_id,
            "status": j["status"],
            "total_items": j["total_items"],
            "done_items": j["done_items"],
            "success_items": j["success_items"],
            "fail_items": j["fail_items"],
            "temp_fail_items": j["temp_fail_items"],
            "results": [
                {
                    "firm_name": r[3],
                    "person_name": r[4],
                    "domain": r[5],
                    "verified_email": r[1] or "",
                    "verification_status": r[2] or "",
                }
                for r in best
            ],
        }


def active_ips() -> list[dict]:
    cfg = load_cfg()
    current = set(discover_ipv4())
    configured = cfg.get("ip_identities", []) or []
    out = []
    for x in configured:
        if x.get("enabled", True) and x.get("ip") in current:
            out.append({"id": x.get("id"), "ip": x.get("ip"), "subdomain": x.get("subdomain")})
    # Fallback for fresh installs: if no identities configured, use all discovered global IPv4.
    if not out and not configured:
        for idx, ip in enumerate(sorted(current)):
            out.append({"id": f"auto-{idx+1}", "ip": ip, "subdomain": "localhost"})
    return out


def process_one(item: sqlite3.Row, ip_obj: dict, cfg: dict, timeout_s: int):
    dv = sqlite3.connect(str(RUNTIME_DV_DB))
    ensure_dv_schema(dv)
    res = verify_email(
        conn=dv,
        email=item["candidate_email"],
        row_link=f"daemon:{item['item_id']}",
        mode_switch=2,
        api_base=cfg["mx_api"]["base_url"],
        timeout_s=timeout_s,
        helo_host=ip_obj.get("subdomain") or "mail.advancedverifier.com",
        mail_from="verify@clarvoc.org",
        source_ip=ip_obj.get("ip"),
    )
    dv.close()
    return res


def pick_next_job_id() -> str:
    with db() as c:
        row = c.execute(
            "SELECT job_id FROM jobs WHERE status IN ('queued','running') ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""


def mark_job_running(job_id: str):
    with db() as c:
        c.execute("UPDATE jobs SET status='running', updated_at=? WHERE job_id=?", (now_iso(), job_id))


def get_pending_rows(job_id: str):
    with db() as c:
        return c.execute(
            """
            SELECT * FROM job_items
            WHERE job_id=? AND status='pending'
            ORDER BY provider, domain, group_key, candidate_rank, item_id
            """,
            (job_id,),
        ).fetchall()


def maybe_finish_job(job_id: str):
    with db() as c:
        done_items = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND status IN ('done','canceled')",
            (job_id,),
        ).fetchone()[0]
        succ = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict IN ('valid','catch_all_probable','accept_all_policy')",
            (job_id,),
        ).fetchone()[0]
        temp = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict='temp_fail_retry'",
            (job_id,),
        ).fetchone()[0]
        fail = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict IN ('invalid_mailbox','reject_all_or_invalid','invalid_syntax')",
            (job_id,),
        ).fetchone()[0]
        left = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND status IN ('pending','running')",
            (job_id,),
        ).fetchone()[0]
        c.execute(
            "UPDATE jobs SET done_items=?, success_items=?, temp_fail_items=?, fail_items=?, updated_at=? WHERE job_id=?",
            (done_items, succ, temp, fail, now_iso(), job_id),
        )
        if left == 0:
            c.execute("UPDATE jobs SET status='done', updated_at=? WHERE job_id=?", (now_iso(), job_id))


def provider_pressure(job_id: str, provider: str) -> float:
    with db() as c:
        total = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND status='pending'",
            (job_id,),
        ).fetchone()[0]
        if total == 0:
            return 0.0
        p = c.execute(
            "SELECT count(*) FROM job_items WHERE job_id=? AND status='pending' AND provider=?",
            (job_id, provider),
        ).fetchone()[0]
        return p / float(total)


def try_claim_item(ip: dict, scheduler: SchedulerV2, providers_order: list[str]):
    ip_id = ip.get("id") or ip.get("ip")
    job_id = pick_next_job_id()
    if not job_id:
        return "", None, None
    mark_job_running(job_id)
    apply_domain_policies_for_job(job_id)
    pending = get_pending_rows(job_id)
    if not pending:
        maybe_finish_job(job_id)
        return "", None, None

    by_provider = defaultdict(list)
    for r in pending:
        by_provider[r["provider"]].append(dict(r))

    def prov_ready(provider):
        return cooldown_ready("provider", provider)

    def prov_ip_ready(provider, iid):
        return cooldown_ready("provider_ip", f"{provider}|{iid}")

    def dom_ip_ready(domain, iid):
        return cooldown_ready("domain_ip", f"{domain}|{iid}")

    with SCHEDULER_LOCK:
        provider, chosen = scheduler.pick_for_ip(ip_id, by_provider, prov_ready, prov_ip_ready, dom_ip_ready)
    if not chosen:
        return job_id, None, None

    item_id = chosen["item_id"]
    with db() as c:
        updated = c.execute(
            "UPDATE job_items SET status='running', source_ip=?, helo_host=? WHERE item_id=? AND status='pending'",
            (ip.get("ip"), ip.get("subdomain"), item_id),
        ).rowcount
        if updated != 1:
            return job_id, None, None
        row = c.execute("SELECT * FROM job_items WHERE item_id=?", (item_id,)).fetchone()
        return job_id, provider, row


def worker_loop(stop_evt: threading.Event, ip: dict):
    cfg = load_cfg()
    providers_order = cfg.get("providers_order", ["gmail", "outlook", "custom"])
    scheduler = SchedulerV2(providers_order)
    first_attempt_timeout = int(cfg.get("first_attempt_timeout_sec", 7))
    later_attempt_timeout = int(cfg.get("mx_api", {}).get("timeout_sec", 12))
    max_temp_retry = int(cfg.get("max_temp_retry", 2))

    while not stop_evt.is_set():
        ip_id = ip.get("id") or ip.get("ip")
        job_id, provider, row = try_claim_item(ip, scheduler, providers_order)
        if not job_id:
            time.sleep(1)
            continue
        if not row:
            time.sleep(0.4)
            continue

        timeout_s = first_attempt_timeout if int(row["attempts"]) <= 0 else later_attempt_timeout
        result = process_one(row, ip, cfg, timeout_s=timeout_s)
        verdict = result.get("mailbox_verdict") or "unknown"
        rcpt_code = result.get("rcpt_code")

        retryable = verdict == "temp_fail_retry"
        attempts_after = int(row["attempts"]) + 1
        will_retry = retryable and attempts_after < max_temp_retry

        with db() as c:
            if will_retry:
                c.execute(
                    """
                    UPDATE job_items
                    SET status='pending', attempts=?, last_verdict=?, last_error=?, last_checked_at=?
                    WHERE item_id=?
                    """,
                    (attempts_after, verdict, result.get("rcpt_reply", ""), now_iso(), row["item_id"]),
                )
            else:
                c.execute(
                    """
                    UPDATE job_items
                    SET status='done', attempts=?, last_verdict=?, last_error=?, last_checked_at=?
                    WHERE item_id=?
                    """,
                    (attempts_after, verdict, result.get("rcpt_reply", ""), now_iso(), row["item_id"]),
                )

                if verdict in ("valid", "catch_all_probable", "accept_all_policy"):
                    c.execute(
                        "UPDATE group_state SET status='done', winner_email=?, winner_verdict=? WHERE job_id=? AND group_key=?",
                        (row["candidate_email"], verdict, row["job_id"], row["group_key"]),
                    )
                    c.execute(
                        "UPDATE job_items SET status='canceled' WHERE job_id=? AND group_key=? AND status='pending'",
                        (row["job_id"], row["group_key"]),
                    )

                gleft = c.execute(
                    "SELECT count(*) FROM job_items WHERE job_id=? AND group_key=? AND status IN ('pending','running')",
                    (row["job_id"], row["group_key"]),
                ).fetchone()[0]
                gdone = c.execute(
                    "SELECT status FROM group_state WHERE job_id=? AND group_key=?",
                    (row["job_id"], row["group_key"]),
                ).fetchone()[0]
                if gleft == 0 and gdone != "done":
                    best = c.execute(
                        """
                        SELECT candidate_email, last_verdict
                        FROM job_items
                        WHERE job_id=? AND group_key=?
                        ORDER BY CASE last_verdict
                          WHEN 'valid' THEN 0
                          WHEN 'catch_all_probable' THEN 1
                          WHEN 'accept_all_policy' THEN 2
                          WHEN 'temp_fail_retry' THEN 3
                          WHEN 'unknown' THEN 4
                          WHEN 'invalid_mailbox' THEN 5
                          WHEN 'reject_all_or_invalid' THEN 6
                          ELSE 99 END, candidate_rank ASC
                        LIMIT 1
                        """,
                        (row["job_id"], row["group_key"]),
                    ).fetchone()
                    if best:
                        c.execute(
                            "UPDATE group_state SET status='done', winner_email=?, winner_verdict=? WHERE job_id=? AND group_key=?",
                            (best[0], best[1], row["job_id"], row["group_key"]),
                        )

            done_items = c.execute(
                "SELECT count(*) FROM job_items WHERE job_id=? AND status IN ('done','canceled')",
                (row["job_id"],),
            ).fetchone()[0]
            succ = c.execute(
                "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict IN ('valid','catch_all_probable','accept_all_policy')",
                (row["job_id"],),
            ).fetchone()[0]
            temp = c.execute(
                "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict='temp_fail_retry'",
                (row["job_id"],),
            ).fetchone()[0]
            fail = c.execute(
                "SELECT count(*) FROM job_items WHERE job_id=? AND last_verdict IN ('invalid_mailbox','reject_all_or_invalid','invalid_syntax')",
                (row["job_id"],),
            ).fetchone()[0]
            c.execute(
                "UPDATE jobs SET done_items=?, success_items=?, temp_fail_items=?, fail_items=?, updated_at=? WHERE job_id=?",
                (done_items, succ, temp, fail, now_iso(), row["job_id"]),
            )

        if verdict in ("catch_all_probable", "accept_all_policy"):
            set_domain_policy(row["domain"], "catch_all", verdict, "accept-all behavior observed")
        elif verdict in ("reject_all_or_invalid", "invalid_mailbox") and rcpt_code in (550, 551, 553):
            set_domain_policy(row["domain"], "reject_all", verdict, f"rcpt_code={rcpt_code}")
        elif verdict == "unknown":
            with db() as c:
                ucnt = c.execute(
                    "SELECT count(*) FROM job_items WHERE domain=? AND last_verdict='unknown'",
                    (row["domain"],),
                ).fetchone()[0]
            if ucnt >= 8:
                set_domain_policy(row["domain"], "hopeless_unknown", verdict, f"unknown_count={ucnt}")

        base_lo_hi = cfg.get("provider_delays_sec", {}).get(
            provider, cfg.get("provider_delays_sec", {}).get("default", [3, 8])
        )
        normal_delay = int(sum(base_lo_hi) / 2)
        pressure = provider_pressure(row["job_id"], provider)
        pressure_mult = 1.0 if pressure < 0.7 else (1.3 if pressure < 0.85 else 1.6)

        cooldown_set("provider", provider, int(normal_delay * pressure_mult))
        if will_retry or verdict == "temp_fail_retry" or rcpt_code in (421, 451):
            retry_delay = SchedulerV2.backoff_seconds(attempts_after, base=20, cap=240)
            cooldown_set("provider_ip", f"{provider}|{ip_id}", max(int(normal_delay * 3), retry_delay))
            cooldown_set("domain_ip", f"{row['domain']}|{ip_id}", max(int(normal_delay * 2), retry_delay // 2))
        else:
            cooldown_set("provider_ip", f"{provider}|{ip_id}", max(8, int(normal_delay * pressure_mult)))
            cooldown_set("domain_ip", f"{row['domain']}|{ip_id}", max(5, normal_delay // 2))

        maybe_finish_job(row["job_id"])


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._json(200, {"ok": True, "ts": now_iso()})
        if u.path == "/status":
            q = parse_qs(u.query)
            job_id = (q.get("job_id") or [""])[0]
            if not job_id:
                return self._json(400, {"error": "job_id required"})
            return self._json(200, fetch_job_status(job_id))
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        if self.path not in ("/enqueue", "/enqueue_csv"):
            return self._json(404, {"error": "not_found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(n)
            payload = json.loads(raw.decode("utf-8"))
            if self.path == "/enqueue_csv":
                csv_text = payload.get("csv_text") or ""
                delimiter = payload.get("delimiter")
                result = enqueue_csv_text(csv_text, delimiter)
            else:
                rows = payload.get("rows") or []
                if not isinstance(rows, list):
                    return self._json(400, {"error": "rows must be list"})
                result = enqueue_rows(rows)
            return self._json(200, result)
        except Exception as e:
            return self._json(500, {"error": str(e)})


def main():
    init_db()
    recover_stale_running_state()
    stop_evt = threading.Event()
    workers = []
    for ip in active_ips():
        t = threading.Thread(target=worker_loop, args=(stop_evt, ip), daemon=True, name=f"worker-{ip.get('id')}")
        t.start()
        workers.append(t)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"cloud_daemon listening on {HOST}:{PORT}")
    try:
        srv.serve_forever()
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
