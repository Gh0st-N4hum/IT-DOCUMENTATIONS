"""
scan_sorter_ui.py - the window version of scan_sorter.

Double-click "Scan Sorter.bat" (or run:  python scan_sorter_ui.py).
It opens a page in your browser where you can:
  - drag and drop scanned PDFs to sort them,
  - watch what is happening step by step,
  - check the documents the sorter was not sure about (Review) and file them,
  - copy or move sorted files to another folder on this PC.

It runs only on this computer (127.0.0.1); nothing is sent over the network.
Needs only scan_sorter.py next to it - no extra installs.
"""

import io
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scan_sorter as ss

if sys.stdout is None:          # started without a console window (pythonw)
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

STAGES = [
    ("read", "Read the pages"),
    ("split", "Find the documents"),
    ("identify", "Read names and dates"),
    ("check", "Check every page is accounted for"),
    ("save", "Save and check the files"),
    ("finish", "Completeness and tidy up"),
]
MAX_UPLOAD = 4 * 1024 * 1024 * 1024      # 4 GB


def fresh_stages():
    return [{"key": k, "label": label, "status": "waiting", "detail": "", "done": 0, "total": 0}
            for k, label in STAGES]


# ======================================================================= runner
class Runner:
    """Works through the queue on one background thread and keeps what the page shows."""

    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}                  # id -> item dict
        self.next_id = 1
        self.pending = deque()           # item ids waiting
        self.seen = set()                # (name, size, mtime) of Inbox files already picked up
        self.candidates = {}             # Inbox files still being written: name -> (size, since)
        self.log = deque(maxlen=3000)    # (seq, text)
        self.seq = 0
        self.stages = fresh_stages()
        self.plan = []
        self.current = None
        self.watch = True
        self.dry = False
        self.busy = False
        self.last = None
        self.attention = {"count": 0, "lines": []}
        self.quit = threading.Event()
        self.wake = threading.Event()

    # ---- events coming from the sorter
    def on_event(self, kind, data):
        with self.lock:
            if kind == "log":
                self.seq += 1
                self.log.append((self.seq, data["text"].rstrip()))
            elif kind == "batch_start":
                self.stages = fresh_stages()
                self.plan = []
            elif kind == "stage":
                st = self._stage(data["key"])
                if st:
                    st["status"], st["detail"] = data["status"], data.get("detail", "")
                    if data["status"] == "done" and st["total"]:
                        st["done"] = st["total"]
            elif kind == "progress":
                st = self._stage(data["key"])
                if st:
                    st["done"], st["total"] = data["done"], data["total"]
            elif kind == "plan":
                self.plan = data["items"]

    def _stage(self, key):
        return next((s for s in self.stages if s["key"] == key), None)

    # ---- queue
    def _new_item(self, path, uploaded=False):
        item = {"id": self.next_id, "name": path.name, "path": str(path), "status": "waiting",
                "note": "", "added": time.strftime("%H:%M:%S"), "uploaded": uploaded}
        self.next_id += 1
        self.items[item["id"]] = item
        return item

    def _remember(self, path):
        try:
            st = path.stat()
            self.seen.add((path.name, st.st_size, st.st_mtime_ns))
        except OSError:
            pass

    def enqueue(self, path, uploaded=False):
        with self.lock:
            item = self._new_item(path, uploaded)
            self._remember(path)
            self.pending.append(item["id"])
        self.wake.set()
        return item

    def sort_inbox_now(self):
        """Queue every PDF in the Inbox that is not already waiting or being sorted."""
        added = 0
        with self.lock:
            active = {i["path"] for i in self.items.values() if i["status"] in ("waiting", "processing")}
            for f in sorted(ss.INBOX.glob("*.pdf")):
                if str(f) not in active:
                    self.enqueue(f)
                    added += 1
        return added

    def stop(self):
        """Stop the batch that is running and forget the ones waiting (the files stay in the Inbox)."""
        with self.lock:
            for i in self.pending:
                self.items[i]["status"], self.items[i]["note"] = "stopped", "not started"
            self.pending.clear()
            if self.busy:
                ss.CANCEL.set()

    def _watch_pick(self):
        """A new PDF in the Inbox (NAPS2 saved it): wait until its size stops changing, then queue it."""
        now = time.time()
        for f in sorted(ss.INBOX.glob("*.pdf")):
            try:
                st = f.stat()
            except OSError:
                continue
            key = (f.name, st.st_size, st.st_mtime_ns)
            if key in self.seen or st.st_size == 0:
                continue
            size, since = self.candidates.get(f.name, (None, now))
            if size != st.st_size:
                self.candidates[f.name] = (st.st_size, now)
                continue
            if now - since >= 2.0:
                self.candidates.pop(f.name, None)
                return self.enqueue(f)
        return None

    def _next(self):
        with self.lock:
            while self.pending:
                item = self.items[self.pending.popleft()]
                if item["status"] == "waiting":
                    return item
            if self.watch:
                return self._watch_pick()
        return None

    def _run(self, item):
        path, dry = Path(item["path"]), self.dry
        with self.lock:
            item["status"], self.busy, self.current = "processing", True, item["id"]
            self.stages, self.plan = fresh_stages(), []
        note, status = "", "failed"
        try:
            if not path.exists():
                raise FileNotFoundError("the file is no longer in the Inbox")
            ok = ss.process(path, dry)
            summary = dict(ss.LAST_SUMMARY)
            if ok:
                bits = [f"{summary.get('sorted', 0)} sorted"]
                if summary.get("review"):
                    bits.append(f"{summary['review']} to Review")
                if summary.get("pdfa_failed"):
                    bits.append(f"{summary['pdfa_failed']} failed the PDF/A check")
                if summary.get("issues"):
                    bits.append(f"{summary['issues']} patient(s) incomplete")
                note = ", ".join(bits)
                if dry:
                    status = "tested"
                elif summary.get("review") or summary.get("pdfa_failed") or summary.get("issues"):
                    status = "attention"
                else:
                    status = "done"
            else:
                status, note = "failed", "not finished - see the log; the original is still in the Inbox"
        except ss.Cancelled:
            status, note = "stopped", "stopped by you"
        except Exception as e:
            status, note = "failed", str(e)
            ss.log(f"  ! could not process {path.name}: {e}")
        with self.lock:
            item["status"], item["note"] = status, note
            self.busy, self.current = False, None
            self.last = {"name": item["name"], "status": status, "note": note, "at": time.strftime("%H:%M:%S")}
        if not dry:
            self.refresh_attention()

    def refresh_attention(self):
        try:
            rows = ss.scan_completeness()
            lines = []
            for key in sorted(rows, key=ss._date_key):
                missing, extra = ss.completeness_issues(rows[key])
                if missing or extra:
                    lines.append(ss.describe_issue(key, missing, extra))
            with self.lock:
                self.attention = {"count": len(lines), "lines": lines[:200], "patients": len(rows)}
        except Exception:
            pass

    def loop(self):
        self.refresh_attention()
        while not self.quit.is_set():
            item = self._next()
            if item is None:
                self.wake.wait(0.7)
                self.wake.clear()
                continue
            self._run(item)

    # ---- what the page asks for
    def snapshot(self, since):
        try:
            review_count = sum(1 for _ in ss.REVIEW.glob("*.pdf")) if ss.REVIEW.exists() else 0
        except OSError:
            review_count = 0
        with self.lock:
            reset = since > self.seq
            lines = [{"seq": s, "text": t} for s, t in self.log if reset or s > since]
            items = sorted(self.items.values(), key=lambda i: i["id"], reverse=True)[:60]
            cur = self.items.get(self.current)
            return {
                "busy": self.busy, "current_name": cur["name"] if cur else "",
                "watch": self.watch, "dry": self.dry, "reset": reset,
                "stages": self.stages, "plan": self.plan, "last": self.last,
                "items": [{k: i[k] for k in ("id", "name", "status", "note", "added")} for i in items],
                "waiting": len(self.pending), "log": lines, "log_seq": self.seq,
                "attention": self.attention, "review_count": review_count,
                "inbox": str(ss.INBOX),
            }


# ====================================================================== helpers
def clean_upload_name(name):
    name = Path(str(name or "")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name.lower().endswith(".pdf"):
        raise ValueError("Only PDF files can be added.")
    return name[:120]


def list_dir(path, files=False):
    """Folders (and, with files=True, PDFs) inside `path` for the pickers ('' = the drives)."""
    if not path:
        if os.name == "nt":
            drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        else:
            drives = ["/"]
        return {"path": "", "parent": None, "dirs": [{"name": d, "path": d} for d in drives], "files": []}
    p = Path(path)
    if not p.is_dir():
        raise ValueError("That folder does not exist.")
    dirs, pdfs = [], []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            try:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(("$", ".")):
                    dirs.append({"name": e.name, "path": str(Path(e.path))})
                elif files and e.is_file() and e.name.lower().endswith(".pdf"):
                    pdfs.append({"name": e.name, "path": str(Path(e.path)),
                                 "kb": max(1, round(e.stat().st_size / 1024))})
            except OSError:
                pass
    except OSError as e:
        raise ValueError(f"Cannot open that folder ({e.strerror or e}).")
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else "", "dirs": dirs, "files": pdfs}


def make_folder(path, name):
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in (".", ".."):
        raise ValueError("A folder name cannot contain  \\ / : * ? \" < > |")
    parent = Path(path)
    if not parent.is_dir():
        raise ValueError("That folder does not exist.")
    new = parent / name.strip()
    new.mkdir(exist_ok=True)
    return str(new)


def open_in_viewer(path):
    """Open a file or folder with the program Windows uses for it."""
    path = str(path)
    if hasattr(os, "startfile"):
        os.startfile(path)                      # Windows
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ======================================================================= server
class Handler(BaseHTTPRequestHandler):
    server_version = "ScanSorterUI"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---- plumbing
    @property
    def runner(self):
        return self.server.runner

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message):
        self._send(code, {"error": message})

    def _guard(self, query):
        """Only this computer's own page may use the API: right Host, right Origin, right token."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            self._fail(403, "Wrong address. Open the page from the Scan Sorter window.")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in allowed}:
            self._fail(403, "Request from another site was refused.")
            return False
        token = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        if not secrets.compare_digest(token, self.server.token):
            self._fail(403, "This page was opened from an old address. Open Scan Sorter again.")
            return False
        return True

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ValueError("Request too large.")
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _dispatch(self, method):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._guard(query):
            self.close_connection = True
            return
        route = url.path
        try:
            if method == "GET" and route == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not route.startswith("/api/"):
                self._fail(404, "Not found.")
                return
            fn = getattr(self, "api_" + method.lower() + "_" + route[5:].replace("/", "_"), None)
            if fn is None:
                self._fail(404, "Not found.")
                return
            fn(query)
        except (ValueError, json.JSONDecodeError) as e:
            self._fail(400, str(e))
        except FileNotFoundError as e:
            self._fail(404, str(e))
        except Exception as e:                      # never leave the page hanging
            self._fail(500, f"{type(e).__name__}: {e}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---- state
    def api_get_state(self, q):
        since = int((q.get("since") or ["0"])[0] or 0)
        self._send(200, self.runner.snapshot(since))

    def api_post_options(self, q):
        body = self._json_body()
        with self.runner.lock:
            if "watch" in body:
                self.runner.watch = bool(body["watch"])
            if "dry" in body:
                self.runner.dry = bool(body["dry"])
        self.runner.wake.set()
        self._send(200, {"ok": True})

    def api_post_process(self, q):
        self._send(200, {"added": self.runner.sort_inbox_now()})

    def api_post_stop(self, q):
        self.runner.stop()
        self._send(200, {"ok": True})

    def api_post_quit(self, q):
        self._send(200, {"ok": True})
        self.runner.quit.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    # ---- upload (drag and drop)
    def api_post_upload(self, q):
        try:
            name = clean_upload_name((q.get("name") or [""])[0])
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError(f"{name} is empty.")
            if length > MAX_UPLOAD:
                raise ValueError(f"{name} is too large.")
            ss.INBOX.mkdir(parents=True, exist_ok=True)
        except ValueError:
            self.close_connection = True       # the body was not read
            raise
        with self.runner.lock:
            dest = ss.unique_path(ss.INBOX, Path(name).stem)
        part = dest.with_name(dest.stem + ".uploading")
        try:
            with open(part, "wb") as f:
                left, first = length, True
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise ValueError("The upload was interrupted.")
                    if first:
                        if b"%PDF" not in chunk[:1024]:
                            raise ValueError(f"{name} is not a PDF file.")
                        first = False
                    f.write(chunk)
                    left -= len(chunk)
            with self.runner.lock:              # rename + queue together, so the Inbox watcher cannot grab it twice
                os.replace(part, dest)
                item = self.runner.enqueue(dest, uploaded=True)
        except Exception:
            part.unlink(missing_ok=True)
            self.close_connection = True
            raise
        self._send(200, {"name": dest.name, "id": item["id"]})

    def api_post_addfiles(self, q):
        """Sort PDFs chosen anywhere on this PC: each is COPIED into the Inbox (the original is not touched)."""
        paths = self._json_body().get("paths") or []
        if not paths:
            raise ValueError("Tick at least one PDF.")
        ss.INBOX.mkdir(parents=True, exist_ok=True)
        inbox, sorted_dir = ss.INBOX.resolve(), ss.SORTED.resolve()
        results = []
        for raw in paths[:500]:
            try:
                src = Path(str(raw)).resolve()
                if not src.is_file() or src.suffix.lower() != ".pdf":
                    raise ValueError("not a PDF file")
                if sorted_dir in src.parents:
                    raise ValueError("this is already a sorted file")
                with open(src, "rb") as f:
                    if b"%PDF" not in f.read(1024):
                        raise ValueError("not a PDF file")
                if src.parent == inbox:
                    item = self.runner.enqueue(src)           # already in the Inbox: just sort it
                    results.append({"name": src.name, "status": "queued", "detail": "already in the Inbox"})
                    continue
                with self.runner.lock:
                    dest = ss.unique_path(ss.INBOX, clean_upload_name(src.name)[:-4])
                part = dest.with_name(dest.stem + ".uploading")
                try:
                    shutil.copyfile(src, part)
                    if part.stat().st_size != src.stat().st_size:
                        raise OSError("the copy is a different size")
                    with self.runner.lock:
                        os.replace(part, dest)
                        self.runner.enqueue(dest, uploaded=True)
                finally:
                    part.unlink(missing_ok=True)
                results.append({"name": src.name, "status": "queued", "detail": "copied to the Inbox"})
            except Exception as e:
                results.append({"name": Path(str(raw)).name, "status": "error", "detail": str(e)})
        self._send(200, {"results": results})

    # ---- review
    def api_get_review(self, q):
        items = ss.list_review_items()
        for it in items:
            try:
                it["page_count"] = len(ss.PdfReader(str(ss.REVIEW / it["file"])).pages)
            except Exception:
                it["page_count"] = 0
        self._send(200, {"items": items})

    def api_get_review_preview(self, q):
        src = ss._review_file((q.get("file") or [""])[0])
        page = max(0, int((q.get("page") or ["0"])[0] or 0))
        total = len(ss.PdfReader(str(src)).pages)
        if page >= total:
            raise ValueError("That page does not exist.")
        img = ss.convert_from_path(str(src), dpi=100, first_page=page + 1, last_page=page + 1)[0]
        buf = io.BytesIO()
        img.save(buf, "PNG")
        self._send(200, buf.getvalue(), "image/png", {"X-Pages": str(total)})

    def api_post_review_file(self, q):
        b = self._json_body()
        self._send(200, ss.file_review_item(b.get("file"), b.get("doc"), b.get("name"), b.get("date")))

    def api_post_review_dismiss(self, q):
        self._send(200, ss.dismiss_review_item(self._json_body().get("file")))

    # ---- moving files
    def api_get_tree(self, q):
        self._send(200, {"tree": ss.list_sorted_tree()})

    def api_post_transfer(self, q):
        b = self._json_body()
        paths = b.get("paths") or []
        if not paths:
            raise ValueError("Tick the files you want to copy or move.")
        if not b.get("dest"):
            raise ValueError("Choose the folder to copy or move them to.")
        results = ss.transfer_files(paths, b["dest"], move=bool(b.get("move")),
                                    keep_structure=bool(b.get("keep_structure", True)))
        if b.get("move"):
            self.runner.refresh_attention()
        self._send(200, {"results": results})

    def api_get_fs(self, q):
        self._send(200, list_dir((q.get("path") or [""])[0], files=(q.get("files") or ["0"])[0] == "1"))

    def api_post_mkdir(self, q):
        b = self._json_body()
        self._send(200, {"path": make_folder(b.get("path", ""), b.get("name", ""))})

    def api_post_open(self, q):
        b = self._json_body()
        what = b.get("what")
        folders = {"inbox": ss.INBOX, "sorted": ss.SORTED, "review": ss.REVIEW,
                   "processed": ss.PROCESSED, "manifests": ss.MANIFEST_DIR}
        if what in folders:
            target = folders[what]
            target.mkdir(parents=True, exist_ok=True)
        elif what == "report":
            target = ss.REPORT_FILE
        elif what == "review_file":
            target = ss._review_file(b.get("file"))
        elif what == "sorted_file":
            target = SORTED_FILE(b.get("rel"))
        else:
            raise ValueError("Unknown item.")
        if not Path(target).exists():
            raise FileNotFoundError("That file or folder does not exist yet.")
        self.server.opener(target)
        self._send(200, {"ok": True})

    # ---- completeness and settings
    def api_get_completeness(self, q):
        self.runner.refresh_attention()
        self._send(200, self.runner.attention)

    def api_get_settings(self, q):
        self._send(200, {"values": ss.current_settings(), "file": str(ss.SETTINGS_FILE)})

    def api_post_settings(self, q):
        if self.runner.busy:
            raise ValueError("A batch is being sorted. Change the settings when it has finished.")
        values = self._json_body().get("values") or {}
        problems = ss.apply_settings(values)
        if problems:
            ss.apply_settings(self.server.last_good)       # put back what worked
            raise ValueError("; ".join(problems))
        ss.save_settings(values)
        for folder in (ss.INBOX, ss.SORTED, ss.REVIEW, ss.PROCESSED):
            folder.mkdir(parents=True, exist_ok=True)
        self.server.last_good = ss.current_settings()
        self.runner.refresh_attention()
        self._send(200, {"values": ss.current_settings(), "file": str(ss.SETTINGS_FILE)})


def SORTED_FILE(rel):
    """A file inside the Sorted folder, from a 'date/NAME/DOC.pdf' path (nothing outside it)."""
    root = ss.SORTED.resolve()
    p = ss.SORTED.joinpath(*Path(str(rel or "").replace("\\", "/")).parts).resolve()
    if root not in p.parents or p.suffix.lower() != ".pdf":
        raise ValueError("Not a sorted PDF.")
    return p


def make_server(port=0, opener=open_in_viewer):
    """Create the server and the background worker. Returns (server, runner)."""
    runner = Runner()
    ss.EVENT_HOOK = runner.on_event
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.runner, server.token, server.opener = runner, secrets.token_urlsafe(18), opener
    server.last_good = ss.current_settings()
    threading.Thread(target=runner.loop, daemon=True, name="sorter").start()
    return server, runner


def open_app_window(url):
    """Open the page like a program (Edge or Chrome without tabs or address bar).
    Falls back to the normal browser if neither is found."""
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for sub in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            for root in filter(None, roots):
                exe = Path(root) / sub
                if exe.exists():
                    try:
                        subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,860"])
                        return
                    except OSError:
                        pass
    webbrowser.open(url)


def free_port(start=8765, tries=30):
    for port in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main():
    for problem in ss.apply_settings(ss.load_settings()):
        print(f"settings.json: {problem} (ignored)")
    ss.housekeeping()
    for folder in (ss.INBOX, ss.SORTED, ss.REVIEW, ss.PROCESSED):
        folder.mkdir(parents=True, exist_ok=True)
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else free_port()
    server, runner = make_server(port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={server.token}"
    print("Scan Sorter is running. Your browser should open by itself.")
    print(f"If it does not, open this address:\n  {url}\n")
    print("Keep this window open while you work. Close it (or press Ctrl+C) to quit.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: open_app_window(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.quit.set()
        server.server_close()


# ========================================================================== page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scan Sorter</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%230e6b66'/%3E%3Cpath d='M9 8h10l4 4v12H9z' fill='none' stroke='white' stroke-width='2'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#e8edeb; --surface:#f7f9f8; --raised:#ffffff; --ink:#10262b; --muted:#566b6f; --line:#c9d5d2;
  --accent:#0e6b66; --accent-ink:#ffffff; --accent-soft:#d5e9e6;
  --ok:#1b7a4c; --ok-soft:#d6eedf; --warn:#9a5f00; --warn-soft:#f6e6c4; --bad:#b3261e; --bad-soft:#f6d9d6;
  --info:#2a5a9c; --info-soft:#d9e5f5; --idle-soft:#e1e8e6;
  --head:Bahnschrift,"Segoe UI Variable Display","Segoe UI",system-ui,sans-serif;
  --body:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
  --mono:"Cascadia Mono",Consolas,"SFMono-Regular",monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#0e181b; --surface:#152226; --raised:#1a2a2f; --ink:#e2ecea; --muted:#92a8ab; --line:#2a3d42;
    --accent:#4fc1b6; --accent-ink:#06201e; --accent-soft:#17383a;
    --ok:#62d093; --ok-soft:#163626; --warn:#e5b04c; --warn-soft:#3a2d12; --bad:#ff8a80; --bad-soft:#401d1a;
    --info:#8db8f2; --info-soft:#172e4a; --idle-soft:#1e2e33;
  }
}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--body)}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--accent);outline-offset:2px;border-radius:4px}
h1,h2,h3{font-family:var(--head);font-weight:600;margin:0;letter-spacing:.01em}
h2{font-size:1.05rem}
.shell{max-width:1240px;margin:0 auto;padding:0 16px 40px}
header.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:18px 0 10px}
header.top h1{font-size:1.5rem;letter-spacing:.02em}
.pill{display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:999px;background:var(--idle-soft);font-size:.9rem;max-width:100%}
.pill .dot{width:9px;height:9px;border-radius:50%;background:var(--muted);flex:none}
.pill.working{background:var(--accent-soft)} .pill.working .dot{background:var(--accent);animation:pulse 1.2s ease-in-out infinite}
.pill.off{background:var(--bad-soft)} .pill.off .dot{background:var(--bad)}
@keyframes pulse{50%{opacity:.25}}
.spacer{flex:1}
nav.tabs{display:flex;gap:4px;border-bottom:2px solid var(--line);margin-bottom:18px;overflow-x:auto}
nav.tabs button{background:none;border:0;padding:10px 16px;cursor:pointer;font-family:var(--head);font-size:1rem;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px;white-space:nowrap}
nav.tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
nav.tabs .badge{display:inline-block;min-width:1.4em;padding:0 6px;margin-left:6px;border-radius:999px;background:var(--warn);color:#fff;font-size:.8rem;text-align:center}
.panel[hidden],.badge[hidden]{display:none}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.card>h2{margin-bottom:10px;display:flex;align-items:center;gap:10px}
.card>h2 small{font-family:var(--body);font-weight:400;color:var(--muted);font-size:.85rem}
.grid{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:16px;align-items:start}
@media (max-width:960px){.grid{grid-template-columns:minmax(0,1fr)}}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:8px;border:1px solid var(--line);background:var(--raised);cursor:pointer}
.btn:hover:not(:disabled){border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
.btn.danger{border-color:var(--bad);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.muted{color:var(--muted)} .small{font-size:.88rem}
/* drop zone */
#drop{border:2px dashed var(--accent);border-radius:12px;padding:30px 20px;text-align:center;background:var(--raised);transition:background .15s,border-color .15s}
#drop.over{background:var(--accent-soft);border-style:solid}
#drop .big{font-family:var(--head);font-size:1.35rem;margin-bottom:4px}
#drop .row{justify-content:center;margin-top:14px}
.toggles{display:flex;gap:20px;flex-wrap:wrap;margin-top:14px}
.toggles label{display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=checkbox],input[type=radio]{width:18px;height:18px;accent-color:var(--accent)}
/* pipeline stepper */
ol.steps{list-style:none;margin:0;padding:0}
ol.steps li{position:relative;display:grid;grid-template-columns:28px 1fr;gap:12px;padding:0 0 18px}
ol.steps li:last-child{padding-bottom:0}
ol.steps li::before{content:"";position:absolute;left:13px;top:28px;bottom:0;width:2px;background:var(--line)}
ol.steps li:last-child::before{display:none}
ol.steps li.done::before{background:var(--ok)}
.mark{width:28px;height:28px;border-radius:50%;border:2px solid var(--line);background:var(--raised);display:grid;place-items:center;font-size:.85rem;font-weight:700;z-index:1}
li.running .mark{border-color:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
li.running .mark::after{content:"";width:10px;height:10px;border-radius:50%;background:var(--accent);animation:pulse 1.1s ease-in-out infinite}
li.done .mark{background:var(--ok);border-color:var(--ok);color:#fff}
li.failed .mark{background:var(--bad);border-color:var(--bad);color:#fff}
li.skipped .mark{border-style:dashed;color:var(--muted)}
ol.steps .lbl{font-weight:600;line-height:28px}
li.waiting .lbl{color:var(--muted);font-weight:400}
ol.steps .detail{color:var(--muted);font-size:.9rem;margin-top:-2px;overflow-wrap:anywhere}
li.failed .detail{color:var(--bad)}
.bar{height:8px;border-radius:99px;background:var(--idle-soft);margin-top:6px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent);transition:width .25s}
.nowtitle{font-family:var(--head);font-size:1.15rem;overflow-wrap:anywhere}
/* chips */
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:.82rem;font-weight:600;background:var(--idle-soft);white-space:nowrap}
.chip.ok,.chip.done,.chip.sorted{background:var(--ok-soft);color:var(--ok)}
.chip.warn,.chip.attention,.chip.review{background:var(--warn-soft);color:var(--warn)}
.chip.bad,.chip.failed{background:var(--bad-soft);color:var(--bad)}
.chip.info,.chip.processing,.chip.tested{background:var(--info-soft);color:var(--info)}
.chip.ignored,.chip.waiting,.chip.stopped{background:var(--idle-soft);color:var(--muted)}
.chip.doc{font-family:var(--mono);background:var(--raised);border:1px solid var(--line);font-weight:500}
/* tables / lists */
.scroll{overflow:auto;max-height:420px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:.92rem}
th{font-family:var(--head);font-weight:600;color:var(--muted);font-size:.85rem;position:sticky;top:0;background:var(--surface)}
td.mono,.mono{font-family:var(--mono);font-size:.85rem}
td.nw{white-space:nowrap}
td .why{display:block;color:var(--muted);font-size:.85rem}
.empty{color:var(--muted);padding:10px 0}
ul.plain{list-style:none;margin:0;padding:0}
ul.plain li{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line)}
ul.plain li:last-child{border-bottom:0}
ul.plain .nm{flex:1;overflow-wrap:anywhere}
pre#log{margin:0;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:10px;height:260px;overflow:auto;font:12.5px/1.45 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere}
/* review */
.rv{display:grid;grid-template-columns:minmax(260px,4fr) minmax(0,8fr);gap:16px}
@media (max-width:960px){.rv{grid-template-columns:minmax(0,1fr)}}
.rvlist button{display:block;width:100%;text-align:left;background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:pointer}
.rvlist button[aria-current=true]{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
.rvlist .f{font-size:.85rem;overflow-wrap:anywhere}
.rvlist .reasons{margin-top:4px;color:var(--warn);font-size:.85rem}
.preview{background:var(--raised);border:1px solid var(--line);border-radius:8px;min-height:260px;display:grid;place-items:center;overflow:auto;max-height:560px}
.preview img{max-width:100%;height:auto;display:block}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:14px 0}
.field label{display:block;font-size:.88rem;color:var(--muted);margin-bottom:3px}
input[type=text],select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--raised)}
.err{color:var(--bad);margin-top:8px;min-height:1.4em}
/* move files */
.mv{display:grid;grid-template-columns:minmax(0,6fr) minmax(0,5fr);gap:16px}
@media (max-width:960px){.mv{grid-template-columns:minmax(0,1fr)}}
.tree{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:8px;background:var(--raised);padding:8px 10px}
.tree details{margin-left:4px} .tree summary{cursor:pointer;padding:3px 0;display:flex;align-items:center;gap:8px}
.tree .file{display:flex;align-items:center;gap:8px;padding:2px 0 2px 26px}
.tree .file .grow{flex:1}
.link{background:none;border:0;color:var(--accent);cursor:pointer;padding:0;text-decoration:underline}
.dest{display:flex;gap:8px} .dest input{flex:1}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
/* modal + toast */
dialog{border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink);padding:18px;width:min(560px,92vw)}
dialog::backdrop{background:rgba(0,0,0,.45)}
.dirs{border:1px solid var(--line);border-radius:8px;background:var(--raised);max-height:300px;overflow:auto;margin:10px 0}
.dirs button{display:block;width:100%;text-align:left;background:none;border:0;border-bottom:1px solid var(--line);padding:8px 12px;cursor:pointer}
.dirs button:hover{background:var(--accent-soft)}
#toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:20;max-width:min(420px,90vw)}
.toast{background:var(--ink);color:var(--bg);padding:10px 14px;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.25)}
.toast.bad{background:var(--bad);color:#fff}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="shell">
  <header class="top">
    <h1>Scan Sorter</h1>
    <span id="status" class="pill"><span class="dot"></span><span id="statusText">Connecting…</span></span>
    <span class="spacer"></span>
    <button class="btn" id="btnQuit" title="Stop Scan Sorter and close it">Quit Scan Sorter</button>
  </header>
  <nav class="tabs" role="tablist" aria-label="Sections">
    <button role="tab" id="tab-sort" aria-selected="true" aria-controls="p-sort">Sort</button>
    <button role="tab" id="tab-review" aria-selected="false" aria-controls="p-review">Review <span id="reviewBadge" class="badge" hidden>0</span></button>
    <button role="tab" id="tab-move" aria-selected="false" aria-controls="p-move">Move files</button>
    <button role="tab" id="tab-settings" aria-selected="false" aria-controls="p-settings">Settings</button>
  </nav>

  <!-- ============ SORT ============ -->
  <section class="panel" id="p-sort" role="tabpanel" aria-labelledby="tab-sort">
    <div class="grid">
      <div>
        <div class="card">
          <div id="drop" tabindex="0">
            <div class="big">Drop scanned PDFs here</div>
            <div class="muted">or choose the files yourself. They are copied to the Inbox and sorted; your originals stay where they are.</div>
            <div class="row">
              <button class="btn primary" id="btnPick">Find PDFs on this PC…</button>
              <button class="btn" id="btnChoose">Use the standard file picker</button>
              <input type="file" id="fileInput" accept=".pdf,application/pdf" multiple hidden>
            </div>
          </div>
          <div class="toggles">
            <label><input type="checkbox" id="optWatch" checked> Sort new scans in the Inbox automatically</label>
            <label><input type="checkbox" id="optDry"> Test only (save nothing)</label>
          </div>
          <div class="row" style="margin-top:14px">
            <button class="btn" id="btnInbox">Sort what is in the Inbox now</button>
            <button class="btn danger" id="btnStop" disabled>Stop</button>
            <button class="btn" data-open="inbox">Open Inbox folder</button>
          </div>
        </div>

        <div class="card">
          <h2>What is happening <small id="nowSmall"></small></h2>
          <div class="nowtitle" id="nowTitle">Nothing is being sorted.</div>
          <ol class="steps" id="steps" style="margin-top:14px"></ol>
        </div>

        <div class="card">
          <h2>Queue</h2>
          <ul class="plain" id="queue"></ul>
          <div class="empty" id="queueEmpty">Nothing waiting. Drop a PDF above.</div>
        </div>
      </div>

      <div>
        <div class="card">
          <h2>Documents in this batch <small id="planNote"></small></h2>
          <div class="scroll"><table id="planTable" hidden>
            <thead><tr><th>Pages</th><th>Document</th><th>Result</th><th>Patient</th><th>Date</th><th>Saved as / why</th></tr></thead>
            <tbody></tbody></table></div>
          <div class="empty" id="planEmpty">Documents appear here as soon as the pages are read.</div>
        </div>
        <div class="card" id="attentionCard">
          <h2>Needs attention <small id="attnNote"></small></h2>
          <div id="attnBody" class="muted">Nothing to check.</div>
          <div class="row" style="margin-top:10px">
            <button class="btn" id="btnGoReview">Go to Review</button>
            <button class="btn" data-open="report">Open completeness report</button>
            <button class="btn" data-open="manifests">Open batch records</button>
          </div>
        </div>
        <div class="card">
          <h2>Log <small>every step is also saved in sorter_log.txt</small></h2>
          <pre id="log" aria-live="off"></pre>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ REVIEW ============ -->
  <section class="panel" id="p-review" role="tabpanel" aria-labelledby="tab-review" hidden>
    <div class="row" style="margin-bottom:12px">
      <button class="btn" id="btnReviewReload">Refresh</button>
      <button class="btn" data-open="review">Open Review folder</button>
      <span class="muted small">Pages the sorter was not sure about. Read the page, type the name and date, and file it.</span>
    </div>
    <div class="rv">
      <div class="rvlist" id="rvList"></div>
      <div class="card" id="rvDetail" hidden>
        <h2 id="rvTitle"></h2>
        <div class="muted small" id="rvWhy" style="margin-bottom:10px"></div>
        <div class="row" style="margin-bottom:8px">
          <button class="btn" id="rvPrev" aria-label="Previous page">◀</button>
          <span id="rvPageNo" class="small">Page 1</span>
          <button class="btn" id="rvNext" aria-label="Next page">▶</button>
          <span class="spacer"></span>
          <button class="btn" id="rvOpen">Open the PDF</button>
        </div>
        <div class="preview"><img id="rvImg" alt="Preview of the page"><span id="rvImgMsg" class="muted" hidden>Preview not available.</span></div>
        <div class="form">
          <div class="field"><label for="rvDoc">Document</label>
            <select id="rvDoc"><option value="">Choose…</option><option>CF2</option><option>SOA</option><option>CSF</option><option>HDA</option><option>LAB</option></select></div>
          <div class="field"><label for="rvName">Patient's last name</label><input type="text" id="rvName" autocomplete="off" placeholder="ARRUEJO"></div>
          <div class="field"><label for="rvDate">Date (month-day-year)</label><input type="text" id="rvDate" autocomplete="off" placeholder="10-19-2026"></div>
        </div>
        <div class="row">
          <button class="btn primary" id="rvFile">File it</button>
          <button class="btn" id="rvDismiss">Not needed</button>
        </div>
        <div class="err" id="rvErr" role="alert"></div>
      </div>
      <div class="empty" id="rvEmpty" hidden>Nothing to review.</div>
    </div>
  </section>

  <!-- ============ MOVE ============ -->
  <section class="panel" id="p-move" role="tabpanel" aria-labelledby="tab-move" hidden>
    <div class="mv">
      <div class="card">
        <h2>Sorted files <small id="treeNote"></small></h2>
        <div class="row" style="margin-bottom:10px">
          <input type="text" id="treeFilter" placeholder="Find a patient or date" aria-label="Find a patient or date" style="max-width:260px">
          <button class="btn" id="btnTreeReload">Refresh</button>
          <button class="btn" id="btnTreeAll">Tick all shown</button>
          <button class="btn" id="btnTreeNone">Untick all</button>
          <button class="btn" data-open="sorted">Open Sorted folder</button>
        </div>
        <div class="tree" id="tree"></div>
      </div>
      <div class="card">
        <h2>Copy or move them to</h2>
        <div class="dest">
          <input type="text" id="destPath" placeholder="Choose a folder" aria-label="Destination folder">
          <button class="btn" id="btnBrowse">Browse…</button>
        </div>
        <div style="margin:14px 0">
          <label style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><input type="radio" name="mode" value="copy" checked> Copy (the files stay in Sorted)</label>
          <label style="display:flex;gap:8px;align-items:center;margin-bottom:10px"><input type="radio" name="mode" value="move"> Move (removed from Sorted after each copy is checked)</label>
          <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="keepStruct" checked> Keep the date and patient folders</label>
        </div>
        <button class="btn primary" id="btnGo" disabled>Copy</button>
        <div class="muted small" style="margin-top:8px">Nothing is ever overwritten. A file that is already there is skipped.</div>
        <div id="xferResults" style="margin-top:14px"></div>
      </div>
    </div>
  </section>

  <!-- ============ SETTINGS ============ -->
  <section class="panel" id="p-settings" role="tabpanel" aria-labelledby="tab-settings" hidden>
    <div class="card">
      <h2>Folders and limits</h2>
      <div class="muted small" style="margin-bottom:12px">Saved in <span class="mono" id="setFile"></span>. Changes apply straight away.</div>
      <div class="settings" id="setForm"></div>
      <div class="row" style="margin-top:14px"><button class="btn primary" id="btnSaveSet">Save settings</button></div>
      <div class="err" id="setErr" role="alert"></div>
    </div>
  </section>
</div>

<dialog id="dlgPick">
  <h2>Choose the PDFs to sort</h2>
  <div class="mono small" id="pkPath" style="margin:8px 0;overflow-wrap:anywhere"></div>
  <div class="dirs" id="pkList"></div>
  <div class="err" id="pkErr" role="alert"></div>
  <div class="row" style="justify-content:space-between;margin-top:8px">
    <span class="muted small" id="pkCount">0 selected</span>
    <span class="row"><button class="btn" id="pkCancel">Cancel</button><button class="btn primary" id="pkGo" disabled>Sort the selected</button></span>
  </div>
</dialog>
<dialog id="dlgFolder">
  <h2>Choose a folder</h2>
  <div class="mono small" id="fsPath" style="margin:8px 0;overflow-wrap:anywhere"></div>
  <div class="dirs" id="fsList"></div>
  <div class="row"><input type="text" id="fsNew" placeholder="New folder name" aria-label="New folder name" style="flex:1"><button class="btn" id="fsMk">Create folder</button></div>
  <div class="err" id="fsErr" role="alert"></div>
  <div class="row" style="justify-content:flex-end;margin-top:8px">
    <button class="btn" id="fsCancel">Cancel</button><button class="btn primary" id="fsUse">Use this folder</button>
  </div>
</dialog>
<div id="toasts" aria-live="polite"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, json) {
  const opt = {headers: {'X-Token': TOKEN}};
  if (json !== undefined) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(json); }
  const r = await fetch('/api/' + path, opt);
  let data = null; try { data = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((data && data.error) || ('Something went wrong (' + r.status + ')'));
  return data;
}
function toast(msg, bad) {
  const t = document.createElement('div'); t.className = 'toast' + (bad ? ' bad' : ''); t.textContent = msg;
  $('#toasts').appendChild(t); setTimeout(() => t.remove(), bad ? 7000 : 4500);
}

/* ---------- tabs ---------- */
function showTab(name) {
  for (const t of ['sort','review','move','settings']) {
    $('#tab-' + t).setAttribute('aria-selected', t === name);
    $('#p-' + t).hidden = t !== name;
  }
  if (name === 'review') loadReview();
  if (name === 'move') loadTree();
  if (name === 'settings') loadSettings();
}
$$('nav.tabs button').forEach(b => b.addEventListener('click', () => showTab(b.id.replace('tab-', ''))));
$('#btnGoReview').addEventListener('click', () => showTab('review'));
document.addEventListener('click', e => {
  const b = e.target.closest('[data-open]'); if (!b) return;
  api('open', {what: b.dataset.open}).catch(err => toast(err.message, true));
});

/* ---------- sort tab ---------- */
const STEP_MARK = {done: '✓', failed: '✕', skipped: '–', waiting: '', running: ''};
let since = 0;
function renderState(s) {
  const pill = $('#status');
  pill.className = 'pill ' + (s.busy ? 'working' : '');
  $('#statusText').textContent = s.busy ? 'Sorting ' + s.current_name + (s.waiting ? ' (+' + s.waiting + ' waiting)' : '')
    : (s.waiting ? s.waiting + ' waiting' : (s.watch ? 'Ready - watching the Inbox' : 'Ready'));
  if (document.activeElement !== $('#optWatch')) $('#optWatch').checked = s.watch;
  if (document.activeElement !== $('#optDry')) $('#optDry').checked = s.dry;
  $('#btnStop').disabled = !(s.busy || s.waiting);

  $('#nowTitle').textContent = s.busy ? s.current_name
    : (s.last ? 'Last: ' + s.last.name + ' - ' + (s.last.note || s.last.status) : 'Nothing is being sorted.');
  $('#nowSmall').textContent = s.last && !s.busy ? 'finished ' + s.last.at : '';
  $('#steps').innerHTML = s.stages.map(st => {
    const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    const showBar = st.status === 'running' && st.total;
    const detail = st.detail || (st.status === 'running' && st.total ? st.done + ' of ' + st.total : '');
    return '<li class="' + esc(st.status) + '"><span class="mark" aria-hidden="true">' + (STEP_MARK[st.status] || '') + '</span><div>' +
      '<div class="lbl">' + esc(st.label) + '<span class="sr" style="position:absolute;left:-9999px"> - ' + esc(st.status) + '</span></div>' +
      (detail ? '<div class="detail">' + esc(detail) + '</div>' : '') +
      (showBar ? '<div class="bar" role="progressbar" aria-valuenow="' + pct + '" aria-valuemin="0" aria-valuemax="100"><i style="width:' + pct + '%"></i></div>' : '') +
      '</div></li>';
  }).join('');

  $('#queue').innerHTML = s.items.map(i =>
    '<li><span class="nm">' + esc(i.name) + (i.note ? '<span class="why muted small" style="display:block">' + esc(i.note) + '</span>' : '') +
    '</span><span class="chip ' + esc(i.status) + '">' + esc(({attention:'check Review',tested:'tested'})[i.status] || i.status) + '</span></li>').join('');
  $('#queueEmpty').hidden = s.items.length > 0;

  const rows = s.plan || [];
  $('#planTable').hidden = !rows.length; $('#planEmpty').hidden = rows.length > 0;
  $('#planTable tbody').innerHTML = rows.map(r =>
    '<tr><td class="mono">' + esc(r.pages) + '</td><td><span class="chip doc">' + esc(r.doc) + '</span></td>' +
    '<td><span class="chip ' + esc(String(r.result).split(' ')[0].toLowerCase()) + '">' + esc(r.result) + '</span></td>' +
    '<td>' + esc(r.name) + '</td><td class="mono nw">' + esc(r.date) + '</td>' +
    '<td class="mono">' + esc(r.saved_as) + (r.reason ? '<span class="why">' + esc(r.reason) + '</span>' : '') +
    (r.pdfa && /NOT PDF\/A|NOT PDF/.test(r.pdfa) ? '<span class="why" style="color:var(--bad)">' + esc(r.pdfa) + '</span>' : '') + '</td></tr>').join('');
  const c = {}; rows.forEach(r => { const k = String(r.result).split(' ')[0]; c[k] = (c[k] || 0) + 1; });
  $('#planNote').textContent = rows.length ? Object.entries(c).map(([k, v]) => v + ' ' + k).join(', ') : '';

  const a = s.attention || {count: 0, lines: []};
  const parts = [];
  if (s.review_count) parts.push('<div><span class="chip warn">' + s.review_count + '</span> page group(s) waiting in Review.</div>');
  if (a.count) parts.push('<div style="margin-top:6px"><b>' + a.count + '</b> patient folder(s) are missing a document or have a duplicate:</div><ul class="plain small">' +
      a.lines.slice(0, 12).map(l => '<li>' + esc(l) + '</li>').join('') + (a.count > 12 ? '<li class="muted">…and ' + (a.count - 12) + ' more in the report</li>' : '') + '</ul>');
  $('#attnBody').innerHTML = parts.join('') || 'Nothing to check.';
  $('#attnBody').className = parts.length ? '' : 'muted';
  $('#attnNote').textContent = a.patients != null ? a.patients + ' patient folders checked' : '';

  const badge = $('#reviewBadge'); badge.hidden = !s.review_count; badge.textContent = s.review_count;

  const log = $('#log');
  if (s.reset) { log.textContent = ''; }
  if (s.log.length) {
    const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent += s.log.map(l => l.text).join('\n') + '\n';
    if (log.textContent.length > 200000) log.textContent = log.textContent.slice(-150000);
    if (stick) log.scrollTop = log.scrollHeight;
  }
  since = s.log_seq;
}
let lastBusy = false;
async function poll() {
  try {
    const s = await api('state?since=' + since);
    renderState(s);
    if (lastBusy && !s.busy) { if ($('#p-review').hidden === false) loadReview(); if ($('#p-move').hidden === false) loadTree(); }
    lastBusy = s.busy;
  } catch (e) {
    $('#status').className = 'pill off'; $('#statusText').textContent = 'Scan Sorter is not running - open it again';
  } finally { setTimeout(poll, 700); }
}

const drop = $('#drop');
['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => uploadFiles(e.dataTransfer.files));
['dragover', 'drop'].forEach(ev => window.addEventListener(ev, e => e.preventDefault()));   // a missed drop must not open the PDF in the tab
$('#btnChoose').addEventListener('click', () => $('#fileInput').click());
drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#fileInput').click(); } });
$('#fileInput').addEventListener('change', e => { uploadFiles(e.target.files); e.target.value = ''; });
async function uploadFiles(list) {
  const files = [...list];
  const pdfs = files.filter(f => /\.pdf$/i.test(f.name));
  if (files.length > pdfs.length) toast((files.length - pdfs.length) + ' item(s) skipped - only PDF files can be added.', true);
  for (const f of pdfs) {
    try {
      toast('Adding ' + f.name + '…');
      const r = await fetch('/api/upload?name=' + encodeURIComponent(f.name), {method: 'POST', headers: {'X-Token': TOKEN, 'Content-Type': 'application/pdf'}, body: f});
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || 'Could not add ' + f.name);
    } catch (err) { toast(err.message, true); }
  }
}
$('#optWatch').addEventListener('change', e => api('options', {watch: e.target.checked}).catch(err => toast(err.message, true)));
$('#optDry').addEventListener('change', e => api('options', {dry: e.target.checked}).catch(err => toast(err.message, true)));
$('#btnInbox').addEventListener('click', async () => { try { const r = await api('process', {}); toast(r.added ? r.added + ' file(s) queued.' : 'The Inbox has no new PDFs.'); } catch (e) { toast(e.message, true); } });
$('#btnStop').addEventListener('click', () => api('stop', {}).then(() => toast('Stopping…')).catch(e => toast(e.message, true)));
$('#btnQuit').addEventListener('click', async () => {
  if (!confirm('Quit Scan Sorter? Anything being sorted will be stopped.')) return;
  try { await api('stop', {}); await api('quit', {}); } catch (e) {}
  document.body.innerHTML = '<div class="shell"><h1 style="margin-top:40px">Scan Sorter has been closed.</h1><p class="muted">You can close this tab. Start it again from Scan Sorter.bat.</p></div>';
});

/* ---------- review tab ---------- */
let rvItems = [], rvSel = null, rvPage = 0, rvPages = 1;
async function loadReview(keepFile) {
  try {
    rvItems = (await api('review')).items;
  } catch (e) { toast(e.message, true); return; }
  $('#rvEmpty').hidden = rvItems.length > 0;
  $('#rvList').innerHTML = rvItems.map((it, n) =>
    '<button data-n="' + n + '"><div><span class="chip doc">' + esc(it.doc || '?') + '</span> <span class="small muted">' + esc(it.pages) + ' · ' + esc(it.added) + '</span></div>' +
    '<div class="f mono">' + esc(it.file) + '</div><div class="reasons">' + esc(it.reason) + '</div></button>').join('');
  $$('#rvList button').forEach(b => b.addEventListener('click', () => selectReview(+b.dataset.n)));
  const keep = rvItems.findIndex(i => i.file === keepFile);
  if (rvItems.length) selectReview(keep >= 0 ? keep : 0); else { rvSel = null; $('#rvDetail').hidden = true; }
}
function selectReview(n) {
  rvSel = rvItems[n]; rvPage = 0; rvPages = rvSel.page_count || 1;
  $$('#rvList button').forEach((b, i) => b.setAttribute('aria-current', i === n));
  $('#rvDetail').hidden = false;
  $('#rvTitle').textContent = rvSel.file;
  $('#rvWhy').textContent = 'Why it is here: ' + rvSel.reason + (rvSel.source ? ' (from ' + rvSel.source + ', ' + rvSel.pages + ')' : '');
  $('#rvDoc').value = ['CF2','SOA','CSF','HDA','LAB'].includes(rvSel.doc) ? rvSel.doc : '';
  $('#rvName').value = rvSel.name_guess || ''; $('#rvDate').value = rvSel.date_guess || '';
  $('#rvErr').textContent = '';
  showPage();
}
function showPage() {
  $('#rvPageNo').textContent = 'Page ' + (rvPage + 1) + ' of ' + rvPages;
  $('#rvPrev').disabled = rvPage <= 0; $('#rvNext').disabled = rvPage >= rvPages - 1;
  const img = $('#rvImg'); $('#rvImgMsg').hidden = true; img.hidden = false;
  img.onerror = () => { img.hidden = true; $('#rvImgMsg').hidden = false; };
  img.src = '/api/review/preview?t=' + encodeURIComponent(TOKEN) + '&file=' + encodeURIComponent(rvSel.file) + '&page=' + rvPage;
}
$('#rvPrev').addEventListener('click', () => { rvPage--; showPage(); });
$('#rvNext').addEventListener('click', () => { rvPage++; showPage(); });
$('#btnReviewReload').addEventListener('click', () => loadReview(rvSel && rvSel.file));
$('#rvOpen').addEventListener('click', () => api('open', {what: 'review_file', file: rvSel.file}).catch(e => toast(e.message, true)));
$('#rvFile').addEventListener('click', async () => {
  $('#rvErr').textContent = '';
  const doc = $('#rvDoc').value, name = $('#rvName').value, date = $('#rvDate').value;
  if (!doc) { $('#rvErr').textContent = 'Choose the document type.'; return; }
  const idx = rvItems.indexOf(rvSel);
  try {
    const r = await api('review/file', {file: rvSel.file, doc, name, date});
    toast('Filed as ' + r.dest + (r.merged ? ' (added to the existing LAB.pdf)' : '') + (r.pdfa_ok ? '' : ' - ' + r.pdfa), !r.pdfa_ok);
    await loadReview(rvItems[idx + 1] && rvItems[idx + 1].file);
  } catch (e) { $('#rvErr').textContent = e.message; }
});
$('#rvDismiss').addEventListener('click', async () => {
  if (!confirm('This page group is not needed? It is kept in Review\\Dismissed, not deleted.')) return;
  const idx = rvItems.indexOf(rvSel);
  try { await api('review/dismiss', {file: rvSel.file}); toast('Moved to Review\\Dismissed'); await loadReview(rvItems[idx + 1] && rvItems[idx + 1].file); }
  catch (e) { $('#rvErr').textContent = e.message; }
});

/* ---------- move tab ---------- */
let treeData = [];
async function loadTree() {
  try { treeData = (await api('tree')).tree; } catch (e) { toast(e.message, true); return; }
  drawTree();
}
function drawTree() {
  const q = $('#treeFilter').value.trim().toLowerCase();
  const checked = new Set($$('#tree input[data-rel]:checked').map(i => i.dataset.rel));
  let files = 0, shown = '';
  for (const d of treeData) {
    let pats = '';
    for (const p of d.patients) {
      if (q && !(d.date.toLowerCase().includes(q) || p.name.toLowerCase().includes(q))) continue;
      files += p.files.length;
      pats += '<details><summary><input type="checkbox" class="grp" aria-label="Tick ' + esc(p.name) + '"> <b>' + esc(p.name) + '</b> <span class="muted small">' + p.files.length + ' file(s)</span></summary>' +
        p.files.map(f => '<div class="file"><input type="checkbox" data-rel="' + esc(f.rel) + '"' + (checked.has(f.rel) ? ' checked' : '') + ' aria-label="' + esc(f.file) + '"><span class="grow mono">' + esc(f.file) + '</span><span class="muted small">' + f.kb + ' KB</span><button class="link small" data-view="' + esc(f.rel) + '">open</button></div>').join('') + '</details>';
    }
    if (pats) shown += '<details open><summary><input type="checkbox" class="grp" aria-label="Tick ' + esc(d.date) + '"> <b class="mono">' + esc(d.date) + '</b></summary>' + pats + '</details>';
  }
  $('#tree').innerHTML = shown || '<div class="empty">Nothing in the Sorted folder yet.</div>';
  $('#treeNote').textContent = files + ' file(s)';
  syncTree();
}
function syncTree() {
  $$('#tree details').forEach(det => {
    const g = det.querySelector(':scope > summary .grp'); if (!g) return;
    const boxes = $$('input[data-rel]', det); const n = boxes.filter(b => b.checked).length;
    g.checked = boxes.length > 0 && n === boxes.length; g.indeterminate = n > 0 && n < boxes.length;
  });
  const n = $$('#tree input[data-rel]:checked').length;
  const move = $('input[name=mode]:checked').value === 'move';
  $('#btnGo').textContent = (move ? 'Move ' : 'Copy ') + n + ' file(s)'; $('#btnGo').disabled = !n || !$('#destPath').value.trim();
}
$('#tree').addEventListener('change', e => {
  if (e.target.classList.contains('grp')) $$('input[data-rel], .grp', e.target.closest('details')).forEach(b => { b.checked = e.target.checked; });
  syncTree();
});
$('#tree').addEventListener('click', e => { const v = e.target.closest('[data-view]'); if (v) api('open', {what: 'sorted_file', rel: v.dataset.view}).catch(err => toast(err.message, true)); });
$('#treeFilter').addEventListener('input', drawTree);
$('#btnTreeReload').addEventListener('click', loadTree);
$('#btnTreeAll').addEventListener('click', () => { $$('#tree input[type=checkbox]').forEach(b => b.checked = true); syncTree(); });
$('#btnTreeNone').addEventListener('click', () => { $$('#tree input[type=checkbox]').forEach(b => b.checked = false); syncTree(); });
$$('input[name=mode]').forEach(r => r.addEventListener('change', syncTree));
$('#destPath').addEventListener('input', syncTree);
$('#btnGo').addEventListener('click', async () => {
  const paths = $$('#tree input[data-rel]:checked').map(i => i.dataset.rel);
  const move = $('input[name=mode]:checked').value === 'move', dest = $('#destPath').value.trim();
  if (move && !confirm('Move ' + paths.length + ' file(s) to ' + dest + '?\nEach file is removed from Sorted only after its copy has been checked.')) return;
  $('#btnGo').disabled = true; $('#xferResults').textContent = 'Working…';
  try {
    const r = await api('transfer', {paths, dest, move, keep_structure: $('#keepStruct').checked});
    const c = {}; r.results.forEach(x => c[x.status] = (c[x.status] || 0) + 1);
    $('#xferResults').innerHTML = '<div><b>' + Object.entries(c).map(([k, v]) => v + ' ' + k).join(', ') + '</b></div><ul class="plain small">' +
      r.results.filter(x => x.status === 'skipped' || x.status === 'error').map(x => '<li><span class="chip ' + (x.status === 'error' ? 'bad' : 'warn') + '">' + esc(x.status) + '</span><span class="nm mono">' + esc(x.path) + ' - ' + esc(x.detail) + '</span></li>').join('') + '</ul>';
    toast(Object.entries(c).map(([k, v]) => v + ' ' + k).join(', '), !!c.error);
    if (move) loadTree(); else syncTree();
  } catch (e) { $('#xferResults').textContent = ''; toast(e.message, true); syncTree(); }
});

/* folder picker */
let fsCur = '';
async function fsLoad(path) {
  $('#fsErr').textContent = '';
  try {
    const d = await api('fs?path=' + encodeURIComponent(path)); fsCur = d.path;
    $('#fsPath').textContent = d.path || 'This PC';
    $('#fsList').innerHTML = (d.path ? '<button data-p="' + esc(d.parent) + '">⬆ Up one level</button>' : '') +
      d.dirs.map(x => '<button data-p="' + esc(x.path) + '">' + esc(x.name) + '</button>').join('') +
      (!d.dirs.length && d.path ? '<div class="empty" style="padding:8px 12px">No folders inside.</div>' : '');
    $('#fsUse').disabled = !d.path;
  } catch (e) { $('#fsErr').textContent = e.message; }
}
$('#fsList').addEventListener('click', e => { const b = e.target.closest('button[data-p]'); if (b) fsLoad(b.dataset.p); });
$('#btnBrowse').addEventListener('click', () => { $('#dlgFolder').showModal(); fsLoad($('#destPath').value.trim() || ''); });
$('#fsCancel').addEventListener('click', () => $('#dlgFolder').close());
$('#fsUse').addEventListener('click', () => { $('#destPath').value = fsCur; $('#dlgFolder').close(); syncTree(); });
$('#fsMk').addEventListener('click', async () => {
  try { const r = await api('mkdir', {path: fsCur, name: $('#fsNew').value.trim()}); $('#fsNew').value = ''; fsLoad(r.path); } catch (e) { $('#fsErr').textContent = e.message; }
});

/* ---------- settings tab ---------- */
const SET_LABELS = {inbox: 'Inbox (scans arrive here)', sorted: 'Sorted', review: 'Review', processed: 'Processed (original batches)',
  log_file: 'Log file', manifest_dir: 'Batch records', report_file: 'Completeness report file', cache_dir: 'Temporary OCR cache',
  workers: 'Pages read at the same time (1-16)', keep_debug_days: 'Keep debug files (days)', keep_cache_days: 'Keep OCR cache (days)', log_max_mb: 'Archive the log above (MB)'};
async function loadSettings() {
  try {
    const d = await api('settings'); $('#setFile').textContent = d.file;
    $('#setForm').innerHTML = Object.entries(SET_LABELS).map(([k, l]) =>
      '<div class="field"><label for="set_' + k + '">' + esc(l) + '</label><input type="text" id="set_' + k + '" data-k="' + k + '" value="' + esc(d.values[k]) + '"></div>').join('');
  } catch (e) { toast(e.message, true); }
}
$('#btnSaveSet').addEventListener('click', async () => {
  $('#setErr').textContent = '';
  const values = {}; $$('#setForm input').forEach(i => values[i.dataset.k] = i.value.trim());
  try { await api('settings', {values}); toast('Settings saved.'); } catch (e) { $('#setErr').textContent = e.message; }
});

/* ---------- pick PDFs anywhere on this PC ---------- */
let pkCur = '', pkSel = new Set();
async function pkLoad(path) {
  $('#pkErr').textContent = '';
  try {
    const d = await api('fs?files=1&path=' + encodeURIComponent(path)); pkCur = d.path;
    $('#pkPath').textContent = d.path || 'This PC';
    $('#pkList').innerHTML = (d.path ? '<button data-p="' + esc(d.parent) + '">⬆ Up one level</button>' : '') +
      d.dirs.map(x => '<button data-p="' + esc(x.path) + '">📁 ' + esc(x.name) + '</button>').join('') +
      d.files.map(f => '<label class="pkfile" style="display:flex;gap:10px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer"><input type="checkbox" data-f="' + esc(f.path) + '"' + (pkSel.has(f.path) ? ' checked' : '') + '><span style="flex:1;overflow-wrap:anywhere">' + esc(f.name) + '</span><span class="muted small">' + f.kb + ' KB</span></label>').join('') +
      (!d.dirs.length && !d.files.length && d.path ? '<div class="empty" style="padding:8px 12px">No folders or PDFs here.</div>' : '');
  } catch (e) { $('#pkErr').textContent = e.message; }
}
function pkSync() { $('#pkCount').textContent = pkSel.size + ' selected'; $('#pkGo').disabled = !pkSel.size; }
$('#pkList').addEventListener('click', e => { const b = e.target.closest('button[data-p]'); if (b) pkLoad(b.dataset.p); });
$('#pkList').addEventListener('change', e => { const f = e.target.dataset.f; if (!f) return; e.target.checked ? pkSel.add(f) : pkSel.delete(f); pkSync(); });
$('#btnPick').addEventListener('click', () => { pkSel = new Set(); pkSync(); $('#dlgPick').showModal(); pkLoad(pkCur); });
$('#pkCancel').addEventListener('click', () => $('#dlgPick').close());
$('#pkGo').addEventListener('click', async () => {
  $('#pkGo').disabled = true;
  try {
    const r = await api('addfiles', {paths: [...pkSel]});
    const bad = r.results.filter(x => x.status === 'error');
    toast((r.results.length - bad.length) + ' file(s) added to the queue.' + (bad.length ? ' ' + bad.length + ' could not be added: ' + bad.map(b => b.name + ' (' + b.detail + ')').join('; ') : ''), bad.length > 0);
    $('#dlgPick').close();
  } catch (e) { $('#pkErr').textContent = e.message; pkSync(); }
});

poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
