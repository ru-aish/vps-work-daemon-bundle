import json
import random
import re
import smtplib
import sqlite3
import string
import subprocess
import time
import urllib.parse
import urllib.request
from email.utils import parseaddr

def now_ts() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

def ensure_dv_schema(conn: sqlite3.Connection):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS dv_runs (dv_run_id TEXT PRIMARY KEY, mode_switch INTEGER, created_at TEXT, notes_json TEXT);
        CREATE TABLE IF NOT EXISTS dv_jobs (dv_job_id INTEGER PRIMARY KEY AUTOINCREMENT, dv_run_id TEXT, row_link TEXT, input_email TEXT, normalized_email TEXT, domain TEXT, provider_bucket TEXT, mx_json TEXT, source TEXT, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS dv_results (dv_result_id INTEGER PRIMARY KEY AUTOINCREMENT, dv_job_id INTEGER, rcpt_code INTEGER, rcpt_reply TEXT, catchall_code INTEGER, catchall_reply TEXT, mailbox_verdict TEXT, confidence REAL, details_json TEXT, checked_at TEXT);
        CREATE TABLE IF NOT EXISTS mx_results (domain TEXT PRIMARY KEY, mx_json TEXT, provider_bucket TEXT, source TEXT, error TEXT, checked_at TEXT);
    ''')

def normalize_email(raw: str) -> str | None:
    _, addr = parseaddr(raw or '')
    addr = (addr or '').strip().lower()
    if not addr or '@' not in addr: return None
    return addr

def _local_dig_mx(domain: str, timeout_s: int):
    try:
        out = subprocess.check_output(['dig', '+short', 'mx', domain], text=True, timeout=timeout_s)
        mx = []
        for ln in out.splitlines():
            parts = ln.split()
            if parts: mx.append(parts[-1].rstrip('.'))
        return mx
    except: return []

def get_mx_for_domain(conn, domain, mode_switch, api_base, timeout_s):
    # Forced Local DNS to avoid 401 API
    mx = _local_dig_mx(domain, timeout_s)
    return mx, 'custom', 'local_dns', ''

def _smtp_probe(mx_host, rcpt_email, mail_from, helo_host, timeout_s, source_ip):
    try:
        smtp = smtplib.SMTP(host=mx_host, port=25, timeout=timeout_s, local_hostname=helo_host, source_address=(source_ip, 0))
        smtp.ehlo_or_helo_if_needed()
        smtp.mail(mail_from)
        code, reply = smtp.rcpt(rcpt_email)
        smtp.quit()
        return int(code), str(reply)
    except Exception as e:
        return 0, str(e)

def verify_email(conn, email, row_link, mode_switch, api_base, timeout_s, helo_host, mail_from, source_ip=None):
    norm = normalize_email(email)
    if not norm: return {'mailbox_verdict': 'invalid_syntax'}
    domain = norm.split('@')[-1]
    mx, provider, src, err = get_mx_for_domain(conn, domain, mode_switch, api_base, timeout_s)
    if not mx: return {'mailbox_verdict': 'unknown'}
    
    code, reply = _smtp_probe(mx[0], norm, mail_from, helo_host, timeout_s, source_ip)
    
    # Catchall check
    rand_email = f"test_{int(time.time())}@{domain}"
    c_code, c_reply = _smtp_probe(mx[0], rand_email, mail_from, helo_host, timeout_s, source_ip)
    
    verdict = 'unknown'
    if 200 <= code <= 299:
        if 200 <= c_code <= 299: verdict = 'catch_all_probable'
        else: verdict = 'valid'
    elif 500 <= code <= 599: verdict = 'invalid_mailbox'
    elif 400 <= code <= 499: verdict = 'temp_fail_retry'
    
    return {
        'input_email': email, 'normalized_email': norm, 'domain': domain,
        'mailbox_verdict': verdict, 'rcpt_code': code, 'rcpt_reply': reply,
        'mx': mx, 'mx_source': src
    }

def persist_job_and_result(conn, dv_run_id, result):
    pass

def provider_from_mx(mx):
    return 'custom'