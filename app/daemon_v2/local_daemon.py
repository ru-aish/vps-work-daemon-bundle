#!/usr/bin/env python3
import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import shutil

DB_PATH = Path("./daemon_v2/local_daemon.db")
UPLOAD_DIR = Path("./daemon_v2/uploads")
OUTPUT_DIR = Path("./daemon_v2/outputs")
HOST = os.getenv("LOCAL_DAEMON_HOST", "127.0.0.1")
PORT = int(os.getenv("LOCAL_DAEMON_PORT", "8799"))
CLOUD_BASE = os.getenv("CLOUD_BASE", "http://127.0.0.1:8788")


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
          local_task_id TEXT PRIMARY KEY,
          cloud_job_id TEXT,
          input_path TEXT NOT NULL,
          output_path TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          priority INTEGER DEFAULT 0,
          note TEXT
        );
        """
    )
    c.commit()
    # lightweight migration for existing DBs created before priority/status changes
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
    if "priority" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 0")
    if "note" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN note TEXT")
    c.commit()
    c.close()

def norm_key(v: str):
    return re.sub(r"[^a-z0-9]+", "", (v or "").strip().lower())

def pick_header(headers, aliases):
    d = {norm_key(h): h for h in headers}
    for a in aliases:
        k = norm_key(a)
        if k in d:
            return d[k]
    return ""

def read_rows(path: str):
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    delim = "\t" if "\t" in raw[:4000] else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    headers = list(reader.fieldnames or [])
    firm_col = pick_header(headers, ["Firm Name", "Firm", "Company"])
    web_col = pick_header(headers, ["Website", "Web", "Domain", "URL"])
    name_col = pick_header(headers, ["Main Person Full Name", "Full Name", "Name", "Contact Name"])
    out = []
    for r in reader:
        out.append(
            {
                "firm_name": (r.get(firm_col) or "").strip(),
                "website": (r.get(web_col) or "").strip(),
                "person_full_name": (r.get(name_col) or "").strip(),
            }
        )
    return out, delim, headers

def http_json(method: str, url: str, payload=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)

def submit_task(input_path: str, output_path: str):
    # Queue locally first; cloud upload happens in poll_loop with retries.
    _, delim, _ = read_rows(input_path)
    local_task_id = f"local_{int(time.time())}"

    c = sqlite3.connect(DB_PATH)
    # Get max priority
    max_p = c.execute("SELECT MAX(priority) FROM tasks").fetchone()[0] or 0
    c.execute(
        "INSERT INTO tasks(local_task_id,cloud_job_id,input_path,output_path,status,created_at,updated_at,priority) VALUES(?,?,?,?,?,?,?,?)",
        (local_task_id, "", str(input_path), str(output_path), "queued_upload", now_iso(), now_iso(), max_p + 1),
    )
    c.commit()
    c.close()
    return {"local_task_id": local_task_id, "status": "queued_upload", "delimiter": delim}

def export_with_results(task):
    input_path = task["input_path"]
    output_path = task["output_path"]
    cloud_job_id = task["cloud_job_id"]
    try:
        status_data = http_json("GET", f"{CLOUD_BASE}/status?job_id={cloud_job_id}")
    except Exception as e:
        print(f"Failed to fetch status for {cloud_job_id}: {e}")
        return

    results = status_data.get("results", [])
    idx = {}
    for r in results:
        k = f"{(r.get('firm_name') or '').strip().lower()}|{(r.get('person_name') or '').strip().lower()}|{(r.get('domain') or '').strip().lower()}"
        idx[k] = r

    with open(input_path, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    delim = "\t" if "\t" in raw[:4000] else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows = list(reader)
    headers = list(reader.fieldnames or [])
    if "Verified Email" not in headers:
        headers.append("Verified Email")
    if "Verification Status" not in headers:
        headers.append("Verification Status")

    firm_col = pick_header(headers, ["Firm Name", "Firm", "Company"])
    web_col = pick_header(headers, ["Website", "Web", "Domain", "URL"])
    name_col = pick_header(headers, ["Main Person Full Name", "Full Name", "Name", "Contact Name"])

    def norm_domain(x):
        x = (x or "").lower().strip()
        x = re.sub(r"^https?://", "", x).split("/")[0].strip().strip(".")
        if x.startswith("www."):
            x = x[4:]
        return x

    for r in rows:
        k = f"{(r.get(firm_col) or '').strip().lower()}|{(r.get(name_col) or '').strip().lower()}|{norm_domain(r.get(web_col) or '')}"
        hit = idx.get(k)
        r["Verified Email"] = (hit or {}).get("verified_email", "")
        r["Verification Status"] = (hit or {}).get("verification_status", "")

    with open(output_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers, delimiter=delim)
        w.writeheader()
        w.writerows(rows)

def poll_loop(stop_evt: threading.Event):
    while not stop_evt.is_set():
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        tasks = c.execute("SELECT * FROM tasks WHERE status IN ('queued_upload','submitted','running') ORDER BY priority DESC, created_at ASC").fetchall()
        for t in tasks:
            try:
                if not t["cloud_job_id"]:
                    csv_text = Path(t["input_path"]).read_text(encoding="utf-8-sig")
                    delim = "\t" if "\t" in csv_text[:4000] else ","
                    enq = http_json("POST", f"{CLOUD_BASE}/enqueue_csv", {"csv_text": csv_text, "delimiter": delim})
                    c.execute(
                        "UPDATE tasks SET cloud_job_id=?, status='submitted', note=?, updated_at=? WHERE local_task_id=?",
                        (enq.get("job_id", ""), f"enqueued_items={enq.get('total_items', 0)}", now_iso(), t["local_task_id"]),
                    )
                else:
                    st = http_json("GET", f"{CLOUD_BASE}/status?job_id={t['cloud_job_id']}")
                    cloud_status = st.get("status")
                    if cloud_status == "done":
                        export_with_results(t)
                        c.execute("UPDATE tasks SET status='done', updated_at=? WHERE local_task_id=?", (now_iso(), t["local_task_id"]))
                    else:
                        c.execute(
                            "UPDATE tasks SET status='running', note=?, updated_at=? WHERE local_task_id=?",
                            (f"done_items={st.get('done_items',0)}/{st.get('total_items',0)}", now_iso(), t["local_task_id"]),
                        )
            except Exception as e:
                c.execute(
                    "UPDATE tasks SET status='queued_upload', note=?, updated_at=? WHERE local_task_id=?",
                    (str(e), now_iso(), t["local_task_id"]),
                )
        c.commit()
        c.close()
        time.sleep(5)

class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE, PATCH")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE, PATCH")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._json(200, {"ok": True, "ts": now_iso()})
        if u.path == "/tasks":
            c = sqlite3.connect(DB_PATH)
            c.row_factory = sqlite3.Row
            rows = [dict(x) for x in c.execute("SELECT * FROM tasks ORDER BY status='done' ASC, priority DESC, created_at DESC").fetchall()]
            c.close()
            return self._json(200, {"tasks": rows})
        if u.path == "/download":
            q = parse_qs(u.query)
            tid = (q.get("task_id") or [""])[0]
            c = sqlite3.connect(DB_PATH)
            c.row_factory = sqlite3.Row
            task = c.execute("SELECT * FROM tasks WHERE local_task_id=?", (tid,)).fetchone()
            c.close()
            if not task or not os.path.exists(task["output_path"]):
                return self._json(404, {"error": "file not found"})
            
            self.send_response(200)
            self.send_header("Content-Type", "text/tab-separated-values")
            self.send_header("Content-Disposition", f"attachment; filename=\"{os.path.basename(task['output_path'])}\"")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(task["output_path"], "rb") as f:
                self.wfile.write(f.read())
            return
            
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/upload":
            content_type = self.headers.get("Content-Type")
            if not content_type or "multipart/form-data" not in content_type:
                return self._json(400, {"error": "multipart/form-data required"})
            
            # Simple multipart parser (lazy but works for single file)
            length = int(self.headers.get("Content-Length"))
            body = self.rfile.read(length)
            
            boundary = content_type.split("boundary=")[1].encode()
            parts = body.split(b"--" + boundary)
            
            filename = f"upload_{int(time.time())}.csv"
            file_content = b""
            
            for part in parts:
                if b"filename=" in part:
                    match = re.search(b'filename="([^"]+)"', part)
                    if match:
                        filename = match.group(1).decode()
                    header_end = part.find(b"\r\n\r\n")
                    file_content = part[header_end+4:].rstrip(b"\r\n")
                    break
            
            if not file_content:
                return self._json(400, {"error": "no file content"})

            # Clean filename
            filename = re.sub(r"[^a-zA-Z0-9.-]", "_", filename)
            input_path = UPLOAD_DIR / filename
            output_filename = filename.rsplit(".", 1)[0] + "_verified.tsv"
            output_path = OUTPUT_DIR / output_filename
            
            with open(input_path, "wb") as f:
                f.write(file_content)
                
            try:
                r = submit_task(str(input_path), str(output_path))
                return self._json(200, r)
            except Exception as e:
                return self._json(500, {"error": str(e)})

        return self._json(404, {"error": "not_found"})

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path == "/tasks":
            q = parse_qs(u.query)
            tid = (q.get("task_id") or [""])[0]
            if not tid: return self._json(400, {"error": "task_id required"})
            
            c = sqlite3.connect(DB_PATH)
            c.execute("DELETE FROM tasks WHERE local_task_id=?", (tid,))
            c.commit()
            c.close()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not_found"})

    def do_PATCH(self):
        u = urlparse(self.path)
        if u.path == "/tasks/reorder":
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode())
            # payload: { "orders": [ {"task_id": "...", "priority": 10}, ... ] }
            c = sqlite3.connect(DB_PATH)
            for item in payload.get("orders", []):
                c.execute("UPDATE tasks SET priority=? WHERE local_task_id=?", (item["priority"], item["task_id"]))
            c.commit()
            c.close()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not_found"})

def main():
    init_db()
    stop_evt = threading.Event()
    t = threading.Thread(target=poll_loop, args=(stop_evt,), daemon=True)
    t.start()
    srv = ReusableThreadingHTTPServer((HOST, PORT), Handler)
    print(f"local_daemon listening on {HOST}:{PORT}")
    try:
        srv.serve_forever()
    finally:
        stop_evt.set()

if __name__ == "__main__":
    main()
