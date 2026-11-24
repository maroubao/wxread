"""Local console fixture: only fake accounts, QR and notifications; no external side effects."""
"""本机前端管线验收。GitHub、扫码和发送为测试替身，页面明确标注测试数据。"""
import argparse
import base64
import json
import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
from support import MemoryContents
from wxread import control, crypto, notify
from wxread.state import StateStore, utcnow_iso

backend = MemoryContents()
key = b"k" * 32
store = StateStore(backend, key, "fixture/wxread")
public, secret = crypto.new_keypair()
store.save_control(public)
runs = []
sent = []
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j9z0AAAAASUVORK5CYII=")

def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def seed():
    now = datetime.now(timezone.utc)
    payload = {"appId": "fixture-app", "ps": "fixture-ps", "pc": "fixture-pc", "b": "book", "c": "chapter", "ci": 1, "co": 0, "pr": 0}
    for aid, label, status in (("0001", "我", "ok"), ("0002", "家人", "needs_login"), ("0003", "测试账户", "ok")):
        data = {"id": aid, "label": label, "enabled": True, "status": status, "target_count": 40, "cookies": {"wr_vid": "fixture-" + aid, "wr_skey": "fixture-session"}, "read_payload": payload, "last_ok_at": iso(now - timedelta(days=3) if aid == "0002" else now - timedelta(minutes=22)), "renewed_at": iso(now - timedelta(days=31) if aid == "0002" else now - timedelta(hours=2)), "note": "续期连续2次确认登录失效" if aid == "0002" else ""}
        if aid == "0003":
            data["progress"] = {"run_id": "running-demo", "status": "running", "started_at": iso(now - timedelta(minutes=10)), "updated_at": iso(now), "read_count": 18, "target_count": 40}
        store.save_account(data)
    for days in range(6, 0, -1):
        moment = now - timedelta(days=days, hours=1)
        for aid in ("0001", "0002"):
            failed = days == 1 and aid == "0002"
            store.append_run({"run_id": aid + str(days), "account_id": aid, "label": "我" if aid == "0001" else "家人", "started_at": iso(moment), "finished_at": iso(moment + timedelta(minutes=20)), "read_count": 0 if failed else 40, "target_count": 40, "exit_code": 3 if failed else 0, "level": "L3" if failed else "L0", "reason": "renew_failed" if failed else "ok"})
    for aid, days in (("0003", 1), ("0001", 0)):
        moment = now - timedelta(days=days, minutes=23)
        store.append_run({"run_id": aid + "extra", "account_id": aid, "label": "测试账户" if aid == "0003" else "我", "started_at": iso(moment), "finished_at": iso(moment + timedelta(minutes=20)), "read_count": 40, "target_count": 40, "exit_code": 0, "level": "L0", "reason": "ok"})
    store.save_settings({"schedule": {"enabled": True, "daily": "01:07", "warm_hours": 6}, "notifications": {"enabled": True, "channels": ["feishu", "pushplus"], "feishu": {"webhook": "fixture-only"}, "pushplus": {"token": "fixture-only"}}})

seed()

class Page:
    def __init__(self, run): self.run = run; self.opened = 0
    def open(self): self.opened = time.monotonic()
    def cookies(self):
        if self.run["conclusion"] == "cancelled": return {}
        return {"wr_vid": "fixture-new", "wr_skey": "fixture-new-session"} if time.monotonic() - self.opened > 7 else {}
    def qr_png(self):
        if self.run["conclusion"] == "cancelled": raise RuntimeError("fixture cancelled")
        return png
    def user_agent(self): return "FixtureBrowser"
    def close(self): pass
    def capture_read_payload(self):
        time.sleep(5)
        return {"appId": "fixture-app", "ps": "p", "pc": "c", "b": "book", "c": "chapter", "ci": 1, "co": 0}, {}

def fake_send(channel, cfg, title, text):
    sent.append({"channel": channel, "cfg": cfg, "title": title, "text": text})
    return notify.NotifyResult(channel, True)

def execute_instruction(instruction, run):
    def frame(shot):
        store.put_raw("responses/qr-" + instruction.session_id + "-001-" + shot.fingerprint + ".png", shot.png, "fixture qr")
    context = control.ControlContext(store, qr_page_factory=lambda: Page(run), qr_out=frame, notify_send=fake_send)
    try:
        control.execute(context, instruction)
    finally:
        run.update({"status": "completed", "conclusion": run["conclusion"] or "success"})

def complete_read(inputs, run):
    time.sleep(2)
    selected = (inputs.get("account_ids") or "").split(",")
    for aid, data in store.load_accounts().items():
        if inputs.get("account_ids") and aid not in selected: continue
        if not data.get("enabled") or data.get("status") != "ok": continue
        count = int(data.get("target_count", 40))
        started = utcnow_iso()
        data["progress"] = {"run_id": run["request_id"] + aid, "status": "completed", "started_at": started, "updated_at": started, "read_count": count, "target_count": count}
        store.save_account(data)
        store.append_run({"run_id": data["progress"]["run_id"], "account_id": aid, "label": data["label"], "started_at": started, "finished_at": started, "read_count": count, "target_count": count, "exit_code": 0, "level": "L0", "reason": "ok"})
    run.update({"status": "completed", "conclusion": "success"})

def complete_warm(run):
    for data in store.load_accounts().values():
        if data.get("enabled") and data.get("status") == "ok":
            data["renewed_at"] = utcnow_iso()
            store.save_account(data)
    run.update({"status": "completed", "conclusion": "success"})

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(root), **kwargs)
    def log_message(self, *args): pass
    def json(self, data, status=200):
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.end_headers()
        if data is not None: self.wfile.write(json.dumps(data).encode())
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/_test/snapshot":
            self.json({"accounts": store.load_accounts(), "settings": store.load_settings(), "sent": sent, "runs": runs}); return
        if path.startswith("/api/"):
            if "/branches/" in path: self.json({"commit": {"sha": str(len(backend.commits))}}); return
            if "/contents/" in path:
                target = path.split("/contents/", 1)[1]
                raw, sha = backend.get(target)
                if raw is not None: self.json({"content": base64.b64encode(raw).decode(), "sha": sha}); return
                names = backend.list(target)
                if names: self.json([{"name": name, "type": "file"} for name in names]); return
                self.json({"message": "Not Found"}, 404); return
            if "/actions/workflows/" in path:
                workflow = path.split("/actions/workflows/", 1)[1].split("/")[0]
                self.json({"workflow_runs": [run for run in reversed(runs) if run["workflow"] == workflow]}); return
            self.json({"message": "unknown fixture endpoint"}, 404); return
        if path in ("/web/gh.js", "/web/index.html", "/web/"):
            data = (root / ("web/index.html" if path == "/web/" else path.lstrip("/"))).read_text(encoding="utf-8")
            if path.endswith("gh.js"): data = data.replace('const API = "https://api.github.com";', 'const API = "http://127.0.0.1:' + str(self.server.server_port) + '/api";')
            else: data = data.replace("<body>", '<body><p style="padding:8px 24px;color:var(--text-3);background:var(--surface-2)">本机测试数据</p>')
            self.send_response(200); self.send_header("Content-Type", "text/javascript" if path.endswith(".js") else "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data.encode()); return
        super().do_GET()
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if path == "/_test/mutate":
            raw = store.load_account(body["id"]); raw.update(body.get("changes", {})); store.save_account(raw); self.json({"ok": True}); return
        if path.endswith("/cancel"):
            run_id = int(path.split("/")[-2])
            for run in runs:
                if run["id"] == run_id: run.update({"status": "completed", "conclusion": "cancelled"})
            self.json(None, 202); return
        if path.endswith("/dispatches"):
            workflow = path.split("/actions/workflows/", 1)[1].split("/")[0]
            inputs = body.get("inputs", {})
            request_id = inputs.get("session_id") or inputs.get("request_id") or "fixture"
            run = {"id": len(runs) + 100, "run_number": len(runs) + 1, "workflow": workflow, "request_id": request_id, "display_title": workflow + " " + request_id, "status": "in_progress", "conclusion": None, "created_at": utcnow_iso(), "html_url": "http://127.0.0.1:" + str(self.server.server_port) + "/_test/snapshot"}
            runs.append(run)
            if workflow == "control.yml":
                instruction = control.decode(json.loads(inputs["instruction"]), secret)
                threading.Thread(target=execute_instruction, args=(instruction, run), daemon=True).start()
            elif workflow == "warm.yml": threading.Thread(target=complete_warm, args=(run,), daemon=True).start()
            else: threading.Thread(target=complete_read, args=(inputs, run), daemon=True).start()
            if workflow == "control.yml" and instruction.action == "qr" and instruction.payload.get("label") == "cancel-delay": time.sleep(8)
            self.json(None, 204); return
        self.json({"message": "unknown fixture post"}, 404)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Console test fixture; use only the printed fake credentials")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    info = {"url": "http://127.0.0.1:" + str(server.server_port) + "/web/", "repo": "fixture/wxread", "token": "fixture-token", "stateKey": crypto.b64e(key)}
    print(json.dumps(info), flush=True)
    server.serve_forever()
