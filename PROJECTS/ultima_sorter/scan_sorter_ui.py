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
ASSETS = Path(__file__).resolve().parent / "assets"      # wallpaper.jpg, startup.mp3, notify.mp3
ASSET_TYPES = {"wallpaper.jpg": "image/jpeg", "startup.mp3": "audio/mpeg", "notify.mp3": "audio/mpeg"}


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
        extra = dict(extra or {})
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message):
        self._send(code, {"error": message})

    def _guard(self, query, need_token=True):
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
        if need_token and not secrets.compare_digest(token, self.server.token):
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
        route = url.path
        if not self._guard(query, need_token=not route.startswith("/assets/")):
            self.close_connection = True
            return
        try:
            if method == "GET" and route == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if method == "GET" and route.startswith("/assets/"):
                name = route[len("/assets/"):]
                f = ASSETS / name
                if name not in ASSET_TYPES or not f.is_file():
                    self._fail(404, "Not found.")
                    return
                self._send(200, f.read_bytes(), ASSET_TYPES[name], {"Cache-Control": "max-age=86400"})
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

    # ---- what the program has learned
    def api_get_learned(self, q):
        self._send(200, ss.learned_summary())

    def api_post_learned_forget(self, q):
        b = self._json_body()
        if b.get("all"):
            ss.forget_learned(None)
        elif b.get("name"):
            ss.forget_learned(b["name"])
        else:
            raise ValueError("Say which name to forget.")
        self._send(200, ss.learned_summary())

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
                        subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,860",
                                          "--autoplay-policy=no-user-gesture-required"])
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%232a62bd'/%3E%3Cpath d='M9 8h10l4 4v12H9z' fill='none' stroke='white' stroke-width='2'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#a9c4e4; --surface:#f4f7fc; --raised:#ffffff; --ink:#14233a; --muted:#4d5f78; --line:#8ea6c6;
  --accent:#1f5fbf; --accent-ink:#ffffff; --accent-soft:#d3e3f8;
  --ok:#17692f; --ok-soft:#d6f0d9; --warn:#8a5200; --warn-soft:#fbeab8; --bad:#a3190f; --bad-soft:#fbd6d1;
  --info:#1b4f9c; --info-soft:#d7e5fa; --idle-soft:#e4e9f1;
  --glass:linear-gradient(#ffffff80,#ffffff10 50%,#00000008 51%,#ffffff30);
  --head:"Trebuchet MS","Lucida Grande",Tahoma,Verdana,sans-serif;
  --body:Tahoma,"Lucida Grande",Verdana,"Segoe UI",sans-serif;
  --mono:"Lucida Console","Courier New",Consolas,monospace;
}
*{box-sizing:border-box}
html{color-scheme:light}
body{margin:0;color:var(--ink);font:13.5px/1.45 var(--body);
  background:#3a6ea5 url(/assets/wallpaper.jpg) center/cover no-repeat fixed}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px dotted #000;outline-offset:2px;box-shadow:0 0 0 3px #ffffffcc}
h1,h2,h3{font-family:var(--head);font-weight:700;margin:0}
.shell{max-width:1240px;margin:14px auto;padding:0 0 22px;background:linear-gradient(#e9f0fa,#d6e2f3);border:1px solid #4a6d9c;border-radius:10px;
  box-shadow:0 0 0 1px #ffffffaa inset,0 10px 30px #1a3a6a66,0 2px 0 #fff inset;overflow:hidden}
.shell>*{margin-left:16px;margin-right:16px}
header.top{margin:0!important;padding:8px 14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  background:linear-gradient(#5e93e0 0%,#2f6bd1 48%,#1f56b3 52%,#2c68c9 100%);border-bottom:1px solid #123a80;color:#fff;
  box-shadow:inset 0 1px 0 #ffffffaa}
header.top h1{font-size:1.25rem;color:#fff;text-shadow:0 1px 2px #0a2a66;letter-spacing:.02em;padding-left:30px;
  background:radial-gradient(circle at 35% 30%,#fff 0 12%,#9fd0ff 13% 40%,#2a6fd6 41% 100%) no-repeat 2px 50%/22px 22px}
.spacer{flex:1}
.pill{display:inline-flex;align-items:center;gap:8px;padding:3px 12px;border-radius:999px;background:linear-gradient(#cfd8c0,#e8efd9);color:#1c2a12;
  border:1px solid #6b7a55;box-shadow:inset 0 2px 3px #00000040,0 1px 0 #ffffff99;font-family:var(--mono);font-size:.85rem;max-width:100%}
.pill .dot{width:11px;height:11px;border-radius:50%;flex:none;background:radial-gradient(circle at 35% 30%,#fff 0 15%,#9aa 16% 100%);box-shadow:0 0 0 1px #0004}
.pill.working .dot{background:radial-gradient(circle at 35% 30%,#fff 0 15%,#5ee27a 16% 55%,#139a35 100%);animation:pulse 1.2s ease-in-out infinite}
.pill.off{background:linear-gradient(#e9c3bd,#f7ded9)} .pill.off .dot{background:radial-gradient(circle at 35% 30%,#fff 0 15%,#ff7a6e 16% 55%,#b4271b 100%)}
@keyframes pulse{50%{opacity:.35}}
nav.tabs{display:flex;gap:3px;margin:14px 16px 0!important;padding:0 6px;overflow-x:auto;border-bottom:1px solid #4a6d9c}
nav.tabs button{background:linear-gradient(#f9fbff,#c9d8ee);border:1px solid #6d89b4;border-bottom:0;border-radius:7px 7px 0 0;padding:6px 18px;cursor:pointer;
  font-family:var(--head);font-size:.95rem;color:#243b5e;white-space:nowrap;margin-top:4px;box-shadow:inset 0 1px 0 #fff}
nav.tabs button:hover{background:linear-gradient(#fff,#dbe7f8)}
nav.tabs button[aria-selected=true]{background:linear-gradient(#ffffff,#f1f6fd);margin-top:0;padding-top:9px;margin-bottom:-1px;color:#000;font-weight:700}
nav.tabs .badge{display:inline-block;min-width:1.5em;padding:0 6px;margin-left:6px;border-radius:999px;color:#fff;font-size:.75rem;text-align:center;
  background:radial-gradient(circle at 40% 25%,#ff9a8f,#d02a1c 60%,#8f150b);border:1px solid #6a0f07;text-shadow:0 1px 1px #0008}
.panel[hidden],.badge[hidden],nav.tabs .badge[hidden]{display:none}
.scroll:has(table[hidden]){display:none}
.panel{margin-top:16px!important}
.card{background:var(--surface);border:1px solid #5f7ca8;border-radius:8px;margin-bottom:16px;overflow:hidden;
  box-shadow:0 2px 5px #1a3a6a40,inset 0 1px 0 #fff;padding:0 0 14px}
.card>*{margin-left:14px;margin-right:14px}
.card>h2{margin:0!important;padding:5px 12px;font-size:.95rem;color:#fff;text-shadow:0 1px 1px #082a66;display:flex;align-items:center;gap:10px;margin-bottom:12px!important;
  background:linear-gradient(#6fa3ea,#3a77d6 48%,#2a62bd 52%,#3a76cf);border-bottom:1px solid #1b4a96;box-shadow:inset 0 1px 0 #ffffff99}
.card>h2 small{font-family:var(--body);font-weight:400;color:#e5efff;font-size:.8rem;text-shadow:none}
.grid{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:16px;align-items:start}
@media (max-width:960px){.grid{grid-template-columns:minmax(0,1fr)}}
.btn{display:inline-flex;align-items:center;gap:6px;padding:5px 16px;border-radius:999px;cursor:pointer;font-family:var(--body);color:#10203a;
  border:1px solid #5d7192;background:linear-gradient(#ffffff 0%,#e6ecf5 48%,#c5d0e2 52%,#e0e8f4 100%);
  box-shadow:inset 0 1px 0 #fff,0 1px 2px #0003;text-shadow:0 1px 0 #ffffffb0}
.btn:hover:not(:disabled){background:linear-gradient(#ffffff 0%,#eef3fb 48%,#d3dff0 52%,#eaf1fa 100%);border-color:#2f5fb0}
.btn:active:not(:disabled){background:linear-gradient(#c0cde2,#dfe8f5);box-shadow:inset 0 2px 4px #0004}
.btn.primary{color:#fff;font-weight:700;text-shadow:0 1px 1px #07295e;border-color:#133f86;
  background:linear-gradient(#7fb2f5 0%,#3b83e6 48%,#1f65c9 52%,#4c93ee 100%)}
.btn.primary:hover:not(:disabled){background:linear-gradient(#97c3fa 0%,#4d93f0 48%,#2b74da 52%,#63a3f4 100%)}
.btn.primary:active:not(:disabled){background:linear-gradient(#1b58b0,#3f86e0)}
.btn.danger{color:#8a1209;border-color:#8f2c22;background:linear-gradient(#fff 0%,#fbe1de 48%,#f0b9b2 52%,#f8d6d1 100%)}
.btn:disabled{opacity:.5;cursor:not-allowed;filter:grayscale(.5)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.muted{color:var(--muted)} .small{font-size:.88rem}
#drop{border:2px dashed #4b74b3;border-radius:8px;padding:26px 18px;text-align:center;
  background:linear-gradient(#ffffff,#e9f1fc);box-shadow:inset 0 3px 8px #1a3a6a33;transition:background .15s}
#drop.over{background:linear-gradient(#fff8d0,#ffe99a);border-color:#b07a00}
#drop .big{font-family:var(--head);font-size:1.3rem;margin-bottom:4px;text-shadow:0 1px 0 #fff;color:#1b3f7a}
#drop .row{justify-content:center;margin-top:12px}
.toggles{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}
.toggles label{display:flex;gap:8px;align-items:center;cursor:pointer}
input[type=checkbox],input[type=radio]{width:16px;height:16px;accent-color:#2a62bd}
ol.steps{list-style:none;margin:0;padding:0}
ol.steps li{position:relative;display:grid;grid-template-columns:30px 1fr;gap:10px;padding:0 0 16px}
ol.steps li:last-child{padding-bottom:0}
ol.steps li::before{content:"";position:absolute;left:13px;top:28px;bottom:0;width:4px;border-radius:2px;background:linear-gradient(90deg,#9fb4d3,#d3dff0);box-shadow:inset 0 0 0 1px #7c93b8}
ol.steps li:last-child::before{display:none}
ol.steps li.done::before{background:linear-gradient(90deg,#2f9e49,#7fd68f)}
.mark{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:.9rem;font-weight:700;z-index:1;color:#56688a;
  background:radial-gradient(circle at 35% 25%,#fff 0 18%,#dbe4f1 19% 70%,#aab9d0 100%);border:1px solid #6d82a6;box-shadow:0 1px 2px #0004}
li.running .mark{background:radial-gradient(circle at 35% 25%,#fff 0 15%,#ffe27a 16% 60%,#e0a100 100%);border-color:#8a6200;box-shadow:0 0 8px #ffbf0099}
li.running .mark::after{content:"";width:8px;height:8px;border-radius:50%;background:#8a5200;animation:pulse 1s ease-in-out infinite}
li.done .mark{color:#fff;text-shadow:0 1px 1px #064;background:radial-gradient(circle at 35% 25%,#d9ffe0 0 12%,#4fd36d 13% 55%,#13852f 100%);border-color:#0d5a1f}
li.failed .mark{color:#fff;background:radial-gradient(circle at 35% 25%,#ffd9d4 0 12%,#f1584a 13% 55%,#a3190f 100%);border-color:#6a0f07}
li.skipped .mark{opacity:.6}
ol.steps .lbl{font-weight:700;line-height:28px;color:#10203a}
li.waiting .lbl{color:var(--muted);font-weight:400}
ol.steps .detail{color:var(--muted);font-size:.88rem;margin-top:-2px;overflow-wrap:anywhere}
li.failed .detail{color:var(--bad)}
.bar{height:14px;border-radius:7px;margin-top:6px;overflow:hidden;background:linear-gradient(#b9c6da,#e7edf6);border:1px solid #5f7ca8;box-shadow:inset 0 2px 3px #0003}
.bar>i{display:block;height:100%;border-radius:6px;transition:width .25s;
  background:linear-gradient(#aef0a8 0%,#5ed15a 45%,#2fa638 50%,#63d56b 100%);box-shadow:inset 0 1px 0 #ffffffb0}
.nowtitle{font-family:var(--head);font-size:1.1rem;overflow-wrap:anywhere;color:#10203a}
.chip{display:inline-block;padding:0 9px;border-radius:999px;font-size:.78rem;font-weight:700;white-space:nowrap;border:1px solid #7a8ca8;
  background:linear-gradient(#fff,#dfe6f1);color:#3a4a63;box-shadow:inset 0 1px 0 #fff}
.chip.ok,.chip.done,.chip.sorted{background:linear-gradient(#e6fbe8,#b6e6bb);border-color:#3f8f4c;color:var(--ok)}
.chip.warn,.chip.attention,.chip.review{background:linear-gradient(#fff6d2,#f7dd8a);border-color:#a8801c;color:var(--warn)}
.chip.bad,.chip.failed{background:linear-gradient(#ffe6e2,#f4b3ab);border-color:#a8453a;color:var(--bad)}
.chip.info,.chip.processing,.chip.tested{background:linear-gradient(#e7f0ff,#b8d0f5);border-color:#4a72b4;color:var(--info)}
.chip.ignored,.chip.waiting,.chip.stopped{color:var(--muted)}
.chip.doc{font-family:var(--mono);font-weight:700;background:linear-gradient(#fff,#e6ebf3)}
.scroll{overflow:auto;max-height:420px;margin:0 14px;border:1px solid #6d89b4;border-radius:4px;background:#fff;box-shadow:inset 0 2px 5px #1a3a6a30}
.scroll table{margin:0}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #d3dcea;vertical-align:top;font-size:.9rem}
th{font-family:var(--head);font-weight:700;color:#10203a;font-size:.84rem;position:sticky;top:0;background:linear-gradient(#fdfeff,#d4deee);border-bottom:1px solid #6d89b4}
tbody tr:nth-child(even){background:#f1f6fd}
td.mono,.mono{font-family:var(--mono);font-size:.82rem} td.nw{white-space:nowrap}
td .why{display:block;color:var(--muted);font-size:.82rem}
.empty{color:var(--muted);padding:8px 14px}
ul.plain{list-style:none;margin:0;padding:0}
ul.plain li{display:flex;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid #cfd9e8}
ul.plain li:last-child{border-bottom:0}
ul.plain .nm{flex:1;overflow-wrap:anywhere}
.card>ol.steps,.card>ul.plain,.card #attnBody ul.plain{margin-left:14px;margin-right:14px}
#attnBody ul.plain{margin:0}
#attnBody,#queue{padding:0}
pre#log{margin:0 14px;background:#06140a;color:#6dff8c;border:2px solid #44566f;border-radius:4px;padding:8px 10px;height:260px;overflow:auto;
  font:12px/1.4 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:inset 0 3px 8px #000c;text-shadow:0 0 4px #29ff5f66}
.rv{display:grid;grid-template-columns:minmax(260px,4fr) minmax(0,8fr);gap:16px}
@media (max-width:960px){.rv{grid-template-columns:minmax(0,1fr)}}
.rvlist button{display:block;width:100%;text-align:left;background:linear-gradient(#fff,#e9eff8);border:1px solid #6d89b4;border-radius:6px;padding:8px 12px;margin-bottom:8px;cursor:pointer;box-shadow:0 1px 2px #0003,inset 0 1px 0 #fff}
.rvlist button[aria-current=true]{background:linear-gradient(#cfe1fb,#a9c8f3);border-color:#1f56b3;box-shadow:inset 0 2px 4px #0003}
.rvlist .f{font-size:.82rem;overflow-wrap:anywhere}
.rvlist .reasons{margin-top:3px;color:var(--warn);font-size:.82rem}
.preview{background:#7d8aa0;border:2px inset #c9d3e3;border-radius:3px;min-height:260px;display:grid;place-items:center;overflow:auto;max-height:560px;margin:0 14px;box-shadow:inset 0 3px 8px #0006}
.preview img{max-width:100%;height:auto;display:block;box-shadow:0 3px 10px #0007}
.form{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:14px 14px}
.field label{display:block;font-size:.84rem;color:var(--muted);margin-bottom:3px}
input[type=text],select{width:100%;padding:5px 8px;border:1px solid #5f7ca8;border-radius:4px;background:#fff;box-shadow:inset 0 2px 4px #1a3a6a40}
input[type=text]:focus,select:focus{border-color:#1f56b3;box-shadow:inset 0 2px 4px #1a3a6a40,0 0 0 3px #7fb2f580;outline:none}
.err{color:var(--bad);margin:8px 14px 0;min-height:1.4em;font-weight:700}
.mv{display:grid;grid-template-columns:minmax(0,6fr) minmax(0,5fr);gap:16px}
@media (max-width:960px){.mv{grid-template-columns:minmax(0,1fr)}}
.tree{max-height:520px;overflow:auto;border:1px solid #6d89b4;border-radius:4px;background:#fff;padding:8px 10px;box-shadow:inset 0 2px 5px #1a3a6a30}
.tree details{margin-left:4px} .tree summary{cursor:pointer;padding:3px 0;display:flex;align-items:center;gap:8px}
.tree .file{display:flex;align-items:center;gap:8px;padding:2px 0 2px 26px}
.tree .file .grow{flex:1}
.link{background:none;border:0;color:#0b45b8;cursor:pointer;padding:0;text-decoration:underline}
.dest{display:flex;gap:8px} .dest input{flex:1}
.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:0 14px}
dialog{border:1px solid #1b4a96;border-radius:8px;background:linear-gradient(#f4f7fc,#dbe6f6);color:var(--ink);padding:0 0 14px;width:min(560px,92vw);box-shadow:0 10px 40px #0009;overflow:hidden}
dialog>*{margin-left:14px;margin-right:14px}
dialog>h2{margin:0!important;padding:5px 12px;font-size:.95rem;color:#fff;text-shadow:0 1px 1px #082a66;margin-bottom:8px!important;
  background:linear-gradient(#6fa3ea,#3a77d6 48%,#2a62bd 52%,#3a76cf);border-bottom:1px solid #1b4a96}
dialog::backdrop{background:rgba(20,40,80,.5)}
.dirs{border:1px solid #6d89b4;border-radius:4px;background:#fff;max-height:300px;overflow:auto;margin:10px 14px;box-shadow:inset 0 2px 5px #1a3a6a30}
.dirs button{display:block;width:100%;text-align:left;background:none;border:0;border-bottom:1px solid #dbe3ef;padding:6px 12px;cursor:pointer}
.dirs button:hover{background:#cfe1fb}
#toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:20;max-width:min(420px,90vw)}
.toast{background:linear-gradient(#fffbe2,#f6e9a6);color:#2a2410;padding:8px 14px;border-radius:6px;border:1px solid #8a7a2a;box-shadow:0 4px 14px #0006,inset 0 1px 0 #fff}
.toast.bad{background:linear-gradient(#ffe6e2,#f4b3ab);border-color:#8f2c22;color:#5a0c05}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<div class="shell">
  <header class="top">
    <h1>Scan Sorter</h1>
    <span id="status" class="pill"><span class="dot"></span><span id="statusText">Connecting…</span></span>
    <span class="spacer"></span>
    <button class="btn" id="btnSound" aria-pressed="true" title="Turn the startup and notification sounds on or off">Sound: on</button>
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
    <div class="card">
      <h2>Names the program has learned <small id="lrnNote"></small></h2>
      <div class="muted small" style="margin-bottom:10px">Names you confirm in Review, and names sorted without trouble in 3 batches, are trusted: a garbled reading that clearly looks like one of them is corrected to it. A wrong name can be removed here. Stored only on this PC in <span class="mono" id="lrnFile"></span>.</div>
      <div class="scroll" style="max-height:300px"><table id="lrnTable"><thead><tr><th>Name</th><th>Sorted</th><th>Confirmed by you</th><th>Last seen</th><th>Trusted</th><th></th></tr></thead><tbody></tbody></table></div>
      <div class="empty" id="lrnEmpty">Nothing learned yet. It starts with your first sorted batch or your first filed Review item.</div>
      <div class="muted small" id="lrnSwaps" style="margin-top:8px"></div>
      <div class="row" style="margin-top:10px"><button class="btn danger" id="btnForgetAll">Forget everything it learned</button></div>
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
/* ---------- sounds ---------- */
let soundOn = true;
try { soundOn = localStorage.getItem('sound') !== 'off'; } catch (e) {}
const sndStart = new Audio('/assets/startup.mp3'), sndNotify = new Audio('/assets/notify.mp3');
function playSound(a) {
  if (!soundOn) return Promise.resolve();
  try { a.currentTime = 0; return a.play().catch(() => {}); } catch (e) { return Promise.resolve(); }
}
function drawSound() { const b = $('#btnSound'); b.textContent = 'Sound: ' + (soundOn ? 'on' : 'off'); b.setAttribute('aria-pressed', soundOn); }
function startupSound() {
  if (!soundOn) return;
  sndStart.play().catch(() => {      /* the browser wants a click first: play on the first click or key */
    const once = () => { removeEventListener('pointerdown', once); removeEventListener('keydown', once); playSound(sndStart); };
    addEventListener('pointerdown', once); addEventListener('keydown', once);
  });
}
function toast(msg, bad, sound) {
  if (bad || sound) playSound(sndNotify);
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
    if (lastBusy && !s.busy) { playSound(sndNotify); if ($('#p-review').hidden === false) loadReview(); if ($('#p-move').hidden === false) loadTree(); }
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
    toast('Filed as ' + r.dest + (r.merged ? ' (added to the existing LAB.pdf)' : '') + (r.pdfa_ok ? '' : ' - ' + r.pdfa), !r.pdfa_ok, true);
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
    toast(Object.entries(c).map(([k, v]) => v + ' ' + k).join(', '), !!c.error, true);
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
  workers: 'Pages read at the same time (1-16)', keep_debug_days: 'Keep debug files (days)', keep_cache_days: 'Keep OCR cache (days)', use_learning: 'Learn from my corrections (1 = yes, 0 = no)', learn_file: 'Learned names file', use_position: 'Name a document from its patient\'s other papers when its own name is unreadable (1 = yes, 0 = no)', log_max_mb: 'Archive the log above (MB)'};
async function loadLearned() {
  try {
    const d = await api('learned'); $('#lrnFile').textContent = d.file;
    $('#lrnTable tbody').innerHTML = d.names.map(n => '<tr><td><b>' + esc(n.name) + '</b></td><td>' + n.auto + '</td><td>' + n.confirmed + '</td><td class="mono nw">' + esc(n.last) + '</td><td>' + (n.trusted ? '<span class="chip ok">trusted</span>' : '<span class="chip waiting">not yet</span>') + '</td><td><button class="link" data-forget="' + esc(n.name) + '">remove</button></td></tr>').join('');
    $('#lrnTable').parentElement.hidden = !d.names.length; $('#lrnEmpty').hidden = d.names.length > 0;
    $('#lrnNote').textContent = d.names.filter(n => n.trusted).length + ' trusted of ' + d.names.length;
    $('#lrnSwaps').textContent = d.swaps.length ? 'Letters it has seen OCR mix up: ' + d.swaps.map(x => x.pair.replace('>', ' read as ') .replace(/^(.) read as (.)$/, 'was $2, read as $1') + ' (' + x.count + ')').join(', ') : '';
  } catch (e) { toast(e.message, true); }
}
async function loadSettings() {
  loadLearned();
  try {
    const d = await api('settings'); $('#setFile').textContent = d.file;
    $('#setForm').innerHTML = Object.entries(SET_LABELS).map(([k, l]) =>
      '<div class="field"><label for="set_' + k + '">' + esc(l) + '</label><input type="text" id="set_' + k + '" data-k="' + k + '" value="' + esc(d.values[k]) + '"></div>').join('');
  } catch (e) { toast(e.message, true); }
}
$('#lrnTable').addEventListener('click', async e => {
  const b = e.target.closest('[data-forget]'); if (!b) return;
  try { await api('learned/forget', {name: b.dataset.forget}); loadLearned(); } catch (err) { toast(err.message, true); }
});
$('#btnForgetAll').addEventListener('click', async () => {
  if (!confirm('Forget every learned name and letter mix-up? Your sorted files are not touched.')) return;
  try { await api('learned/forget', {all: true}); loadLearned(); toast('Forgot everything it learned.'); } catch (err) { toast(err.message, true); }
});
$('#btnSaveSet').addEventListener('click', async () => {
  $('#setErr').textContent = '';
  const values = {}; $$('#setForm input').forEach(i => values[i.dataset.k] = i.value.trim());
  try { await api('settings', {values}); toast('Settings saved.', false, true); } catch (e) { $('#setErr').textContent = e.message; }
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

$('#btnSound').addEventListener('click', () => {
  soundOn = !soundOn; try { localStorage.setItem('sound', soundOn ? 'on' : 'off'); } catch (e) {}
  drawSound(); if (soundOn) playSound(sndNotify);
});
drawSound(); startupSound();
poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()