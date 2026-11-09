"""
scan_sorter.py - sorts BATCH scans of PhilHealth paperwork.

Drop one big scanned PDF (many patients, many days) into the INBOX folder. The script:
  1. reads every page (OCR),
  2. splits the batch into separate documents (SOA, HDA, CF2, CSF, LAB),
  3. finds each document's DATE and the patient's LAST NAME,
  4. saves each one as   SORTED\\10-16-2026\\ARRUEJO\\SOA.pdf

Anything it is not sure about goes to REVIEW (never guessed). The original big PDF is
kept in PROCESSED, so nothing is ever lost.

Safety checks (every batch):
  - PAGE COUNT: every page must end up in exactly one place (sorted, Review or ignored).
    If not, nothing is saved and the original stays in INBOX.
  - Each saved file is re-opened and its page count and PDF/A markers are checked
    (a full check is done too if veraPDF is installed). A failure undoes the batch.
  - MANIFEST: a CSV per batch (C:\\Scans\\Manifests) shows where every page went and why.
  - COMPLETENESS: patients missing a CF2/SOA/CSF/HDA are listed (completeness_report.csv).

SETUP (once):  pip install pytesseract pdf2image pillow pypdf
(Tesseract and Poppler must already be installed.)

USE
  python scan_sorter.py --dry-run   # test: shows the plan, moves/creates nothing
  python scan_sorter.py --once      # process what is in INBOX, then stop
  python scan_sorter.py --report    # list which patients are missing CF2/SOA/CSF/HDA, then stop
  python scan_sorter.py --init-settings   # create settings.json (folders, limits) to edit in Notepad
  (the window version: python scan_sorter_ui.py)
  python scan_sorter.py             # keep watching INBOX (Ctrl+C to stop)
"""

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path, PurePath

os.environ.setdefault("OMP_THREAD_LIMIT", "1")
import pytesseract
from pdf2image import convert_from_path
from PIL import ImageOps
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject
from pytesseract import Output

# ------------------------------------------------------------------ SETTINGS
INBOX = Path(r"C:\Scans\Inbox")           # NAPS2 saves the big batch PDF here
SORTED = Path(r"C:\Scans\Sorted")         # result:  Sorted\10-16-2026\ARRUEJO\SOA.pdf
REVIEW = Path(r"C:\Scans\Review")         # documents / pages the script was not sure about
PROCESSED = Path(r"C:\Scans\Processed")   # the original big PDFs end up here
LOG_FILE = Path(r"C:\Scans\sorter_log.txt")
MANIFEST_DIR = Path(r"C:\Scans\Manifests")            # one CSV per batch: where every page went
REPORT_FILE = Path(r"C:\Scans\completeness_report.csv")  # which patients lack which documents

REQUIRED_DOCS = ("CF2", "SOA", "CSF", "HDA")   # LAB is optional, so it is never "missing"
CHECK_PDFA = True                               # re-open every saved file and check PDF/A markers

CACHE_DIR = Path(r"C:\Scans\Cache")      # OCR text kept a few days, so a re-run of a batch is fast
KEEP_DEBUG_DAYS = 7                         # debug_*.txt files and the cache are deleted after this
KEEP_CACHE_DAYS = 7
LOG_MAX_MB = 2                              # the log is archived (sorter_log.1.txt) above this size
OCR_DPI = 300
SETTINGS_FILE = Path(__file__).resolve().with_name("settings.json")   # optional, overrides the above

WORKERS = max(1, min(8, os.cpu_count() or 1))   # pages read at the same time

# Found automatically. Only change if Tesseract is installed somewhere else.
_TESS = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if _TESS.exists():
    pytesseract.pytesseract.tesseract_cmd = str(_TESS)

# How the FIRST PAGE of each document is recognised.
#   head = title phrases near the TOP of the page (strong, worth 3)
#   body = phrases anywhere on the page (weak, worth 1)
# A page only starts a new document if a title phrase is found. Other pages
# (page 2, page 3 ...) stay attached to the document before them.
DOC_TYPES = {
    "CF2": {"head": ["claim form 2"],
            "body": ["patient confinement information", "health care institution (hci) information",
                     "accreditation number (pan)", "was patient referred by another"]},
    "CSF": {"head": ["claim signature form"],
            "body": ["member and patient information", "employer's certification", "pin) of member",
                     "relationship to member", "certification of member",
                     "provider information and certification"]},
    "HDA": {"head": ["hemodialysis benefits package", "hd treatment session"],
            "body": ["agreement form", "annex a", "i have been fully informed",
                     "package rate for hd", "treatment sessions per calendar year"]},
    "LAB": {"head": ["clinical laboratory", "laboratory report", "laboratory result"],
            "body": ["hematology", "chemistry", "urinalysis", "serology", "normal values",
                     "reference range", "requesting md", "license no", "pathologist",
                     "medical technologist", "specimen"]},
    "SOA": {"head": ["statement of account"],
            "body": ["itemized charges", "summary of fees", "soa reference", "professional fees",
                     "drugs and medicines", "final diagnosis"]},
}
HEAD_CHARS = 700

# Documents that are NOT needed. Their pages (and any pages that follow them, e.g. page 2
# of the CF4) are dropped: never saved to SORTED or REVIEW. Matched near the TOP of the page.
IGNORE_TYPES = {
    "PBEF": ["benefit eligibility form", "pbef reference no"],
    "CF4": ["claim form 4", "(claim form 4)"],
}
IGNORE_REGEX = {"CF4": re.compile(r"\bcf\s?4\b")}
# The CF4 title can be missed by OCR, so also recognise its CONTENT anywhere on the page.
# Two or more of these phrases = a CF4 page (they do not appear on SOA/CF2/CSF/HDA).
CF4_CONTENT = ["history of present illness", "pertinent signs and symptoms", "reason for admission",
               "course in the ward", "physical examination", "pertinent past medical history"]

# Safety check: a document longer than this means a title page was probably missed,
# so it goes to REVIEW instead of risking another patient's pages in the wrong folder.
MAX_PAGES_PER_DOC = {"CF2": 3, "CSF": 2, "HDA": 3, "SOA": 4, "LAB": 2}
# ---------------------------------------------------------------------------

SUFFIXES = {"JR", "SR", "II", "III", "IV"}
NOT_NAMES = {"DISEASE", "FAILURE", "SYNDROME", "STAGE", "NOS", "PHILHEALTH", "RN", "MD",
             "FACILITY", "PATIENT", "MEMBER", "TOTAL", "DATE", "NAME"}


# ------------------------------------------------------------------- reading
def render(path, i, dpi=None):
    """One page as an image (one at a time, so a 100-page batch never fills the memory)."""
    return convert_from_path(str(path), dpi=dpi or OCR_DPI, first_page=i + 1, last_page=i + 1)[0]


def prep(img, cut=140):
    """Grayscale + contrast + black/white. Helps with grey scans and faint text."""
    return ImageOps.autocontrast(img.convert("L")).point(lambda p: 255 if p > cut else 0)


# ------------------------------------------------------- events, cancel, settings
EVENT_HOOK = None                 # the UI sets this to follow what is happening
CANCEL = threading.Event()        # the UI sets this to stop the current batch
LAST_SUMMARY = {}


class Cancelled(Exception):
    pass


def emit(kind, **data):
    """Tell the UI (if there is one) what is happening. Never breaks the sorting."""
    hook = EVENT_HOOK
    if hook:
        try:
            hook(kind, data)
        except Exception:
            pass


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled()


SETTING_KEYS = {   # settings.json key -> (variable in this file, kind)
    "inbox": ("INBOX", "path"), "sorted": ("SORTED", "path"), "review": ("REVIEW", "path"),
    "processed": ("PROCESSED", "path"), "log_file": ("LOG_FILE", "path"),
    "manifest_dir": ("MANIFEST_DIR", "path"), "report_file": ("REPORT_FILE", "path"),
    "cache_dir": ("CACHE_DIR", "path"),
    "workers": ("WORKERS", (1, 16)), "keep_debug_days": ("KEEP_DEBUG_DAYS", (0, 3650)),
    "keep_cache_days": ("KEEP_CACHE_DAYS", (0, 3650)), "log_max_mb": ("LOG_MAX_MB", (1, 1000)),
}


def current_settings():
    out = {}
    for key, (var, kind) in SETTING_KEYS.items():
        v = globals()[var]
        out[key] = str(v) if kind == "path" else v
    return out


def load_settings(path=None):
    """settings.json next to this script (optional). Missing or broken -> {} (defaults are used)."""
    path = Path(path) if path else SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_settings(values):
    """Apply a dict of settings. Returns a list of problems (a bad value is skipped, never guessed)."""
    problems = []
    for key, val in (values or {}).items():
        if key not in SETTING_KEYS:
            continue
        var, kind = SETTING_KEYS[key]
        if kind == "path":
            if not isinstance(val, str) or not val.strip():
                problems.append(f"{key}: needs a folder or file path")
                continue
            globals()[var] = Path(val.strip())
        else:
            lo, hi = kind
            try:
                num = int(val)
            except (TypeError, ValueError):
                problems.append(f"{key}: needs a whole number")
                continue
            if not lo <= num <= hi:
                problems.append(f"{key}: must be between {lo} and {hi}")
                continue
            globals()[var] = num
    return problems


def save_settings(values, path=None):
    """Write settings.json (keeps any keys we do not know about)."""
    path = Path(path) if path else SETTINGS_FILE
    data = load_settings(path)
    data.update({k: v for k, v in values.items() if k in SETTING_KEYS})
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------- OCR cache
_CACHE_FILES = {}


def file_fingerprint(path):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_ocr_cache(path):
    """-> (cache file, {page index as text: OCR text}). A missing/broken cache is just empty."""
    try:
        cache_file = CACHE_DIR / f"{file_fingerprint(path)}.json"
    except OSError:
        return None, {}
    _CACHE_FILES[str(path)] = cache_file
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("dpi") == OCR_DPI and isinstance(data.get("pages"), dict):
            return cache_file, data["pages"]
    except (OSError, ValueError):
        pass
    return cache_file, {}


def save_ocr_cache(cache_file, pages):
    if not cache_file or not pages:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"dpi": OCR_DPI, "pages": pages}), encoding="utf-8")
        os.replace(tmp, cache_file)
    except OSError:
        pass


def drop_ocr_cache(path):
    """The batch is finished: the cached text (it holds patient details) is not needed any more."""
    cache_file = _CACHE_FILES.pop(str(path), None)
    if cache_file:
        try:
            cache_file.unlink(missing_ok=True)
        except OSError:
            pass


def read_all_pages(path):
    reader = PdfReader(str(path))
    n = len(reader.pages)
    layers = []
    for p in reader.pages:
        try:
            layers.append(p.extract_text() or "")
        except Exception:
            layers.append("")

    cache_file, cached = load_ocr_cache(path)
    fresh, lock = {}, threading.Lock()

    def work(i):   # the PDF's own text if it has any, else the cached OCR, else OCR the page
        if len(layers[i].strip()) > 30:
            return layers[i]
        if str(i) in cached:
            return cached[str(i)]
        text = pytesseract.image_to_string(render(path, i))
        with lock:
            fresh[str(i)] = text
        return text

    texts = []
    global LAST_SOURCES
    LAST_SOURCES = ["text layer" if len(l.strip()) > 30 else ("cache" if str(i) in cached else "OCR")
                    for i, l in enumerate(layers)]
    pool = ThreadPoolExecutor(WORKERS)
    try:
        futures = [pool.submit(work, i) for i in range(n)]
        for k, fut in enumerate(futures, 1):
            texts.append(fut.result())
            emit("progress", key="read", done=k, total=n)
            print(f"\r  reading page {k}/{n}", end="", flush=True)
            check_cancel()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)   # stop at once (a Stop press, an error)
        raise
    else:
        pool.shutdown()
    finally:
        with lock:
            save_ocr_cache(cache_file, {**cached, **fresh})   # what was read is kept for a retry
    print()
    return reader, texts


LAST_SOURCES = []


def write_debug(path, texts, segs, ignored):
    """--debug: one file showing, per page, where the text came from and what was found."""
    out = LOG_FILE.parent / f"debug_{path.stem}.txt"
    owner = {}
    for s in segs + ignored:
        for i in s["pages"]:
            owner[i] = s["doc"] if s["doc"] != "IGNORE" else "IGNORE-" + s["label"]
    with open(out, "w", encoding="utf-8") as f:
        for i, t in enumerate(texts):
            f.write(f"##### page {i + 1}  | read by: {LAST_SOURCES[i]}  | belongs to: {owner.get(i, '?')}\n")
            f.write(f"dates found: {valid_dates(t)}\n")
            f.write(t[:600].strip() + "\n\n")
    log(f"  debug written: {out}")


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\u2019", "'").lower())


# --------------------------------------------------------------- splitting
def detect_start(text):
    """Return 'SOA'/'HDA'/'CF2'/'CSF'/'LAB' if this page is the FIRST page of a document."""
    flat = norm(text)
    head = flat[:HEAD_CHARS]
    scores = {d: 3 * sum(k in head for k in kw["head"]) + sum(k in flat for k in kw["body"])
              for d, kw in DOC_TYPES.items()}
    best = max(scores.values())
    winners = [d for d, s in scores.items() if s == best]
    return winners[0] if best >= 3 and len(winners) == 1 else None


def detect_ignore(text):
    """Return 'PBEF'/'CF4' if this page starts a document we do not want, else None."""
    flat = norm(text)
    head = flat[:HEAD_CHARS]
    if sum(k in flat for k in CF4_CONTENT) >= 2:
        return "CF4"
    for label, keys in IGNORE_TYPES.items():
        if any(k in head for k in keys):
            return label
        rx = IGNORE_REGEX.get(label)
        if rx and rx.search(head):
            return label
    return None


def split_batch(texts):
    """Documents we do not want come back as {"doc": "IGNORE", "label": ...}. Pages that
    follow them (CF4 page 2, ...) stay attached to them and are dropped with them."""
    segments, orphans = [], []
    for i, t in enumerate(texts):
        skip = detect_ignore(t)
        doc = None if skip else detect_start(t)
        if skip:
            segments.append({"doc": "IGNORE", "label": skip, "pages": [i]})
        elif doc:
            segments.append({"doc": doc, "pages": [i]})
        elif segments:
            segments[-1]["pages"].append(i)
        else:
            orphans.append(i)
    return segments, orphans


# --------------------------------------------------------------------- date
PLAIN_DATE = re.compile(r"(?<![\d/\-])(\d{2})[-/](\d{2})[-/](\d{4})(?![\d/\-])")
LABEL_DATES = {   # a date printed right after a label (counts extra)
    "SOA": r"(?i:date and time admitted)[\s:|\"'.\-]*",
    "HDA": r"(?i:date \(month/day/year\))[\s:|\"'.\-]*",
    "LAB": r"\b(?i:date)\b[\s:|\"'.\-]*",
}


def valid_dates(text):
    out = []
    for m in PLAIN_DATE.finditer(text):
        mo, d, y = (int(x) for x in m.groups())
        try:
            datetime(y, mo, d)
        except ValueError:
            continue
        if 2020 <= y <= 2035:
            out.append(f"{mo:02d}-{d:02d}-{y}")
    return out


def find_date(doc, text):
    """Most common clearly-printed date in the document (the label counts extra).
    Needs at least 2 votes and no tie. The boxed digit fields on the CF2/CSF are not
    read (OCR is unreliable on them)."""
    votes = Counter(valid_dates(text))
    pat = LABEL_DATES.get(doc)
    if pat:
        m = re.search(pat + r"(\d{2}[-/]\d{2}[-/]\d{4})", text)
        if m:
            for d in valid_dates(m.group(1)):
                votes[d] += 2
    ranked = votes.most_common()
    if not ranked or ranked[0][1] < 2 or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


def fill_missing_dates(segs):
    """A document with no readable date (the CSF has only boxed digits) takes the date of
    the SAME patient's other documents in this batch - only if that is unambiguous."""
    for idx, s in enumerate(segs):
        if s.get("date") or not s.get("name"):
            continue
        same = {t["date"] for t in segs if t.get("date") and t.get("name") == s["name"]}
        if len(same) == 1:
            s["date"], s["date_note"] = same.pop(), "date taken from this patient's other documents"
        elif len(same) > 1:
            near = {segs[j]["date"] for j in (idx - 1, idx + 1)
                    if 0 <= j < len(segs) and segs[j].get("date") and segs[j].get("name") == s["name"]}
            if len(near) == 1:
                s["date"], s["date_note"] = near.pop(), "date taken from the neighbouring document"


# --------------------------------------------------------------------- name
def last_from_tokens(tokens):
    """['FRANCISCO','B.','ARRUEJO','JR.'] -> 'ARRUEJO'"""
    words = [re.sub(r"[^A-ZÑ'\-]", "", t.upper()) for t in tokens]
    words = [w for w in words if len(w) >= 2 and w not in SUFFIXES]
    return words[-1] if words else None


SAME_LINE = r"(?i:name of patient)[\s:\-|]*([A-ZÑ][A-ZÑ'\-]{2,30})"
NEXT_LINE = r"(?i:name of patient)[^\n]*\n\s*([A-ZÑ][A-ZÑ'\-]{2,30})"
LABEL_PATTERNS = {
    "SOA": [r"(?i:patient name)[\s:\-|]*([A-ZÑ][A-ZÑ'\- ]{1,30}?)\s*,"],   # ARRUEJO, FRANCISCO ...
    "CF2": [SAME_LINE, NEXT_LINE],                                        # ARRUEJO FRANCISCO JR ...
    "CSF": [SAME_LINE, NEXT_LINE],
    "LAB": [r"\b(?i:name)\b[\s:\-|]*([A-ZÑ][A-ZÑ'\- ]{1,30}?)\s*,",       # Name: LLANES, MARITE T.
            r"\b(?i:patient(?: name)?)\b[\s:\-|]*([A-ZÑ][A-ZÑ'\- ]{1,30}?)\s*,"],
}


def name_labeled(doc, text):
    for pattern in LABEL_PATTERNS.get(doc, []):
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None


def name_labeled_clean_ocr(doc, path, pages):
    """Second try: re-read the first page with black/white clean-up (fixes dropped letters)."""
    if doc not in LABEL_PATTERNS:
        return None
    text = pytesseract.image_to_string(prep(render(path, pages[0])), config="--psm 6")
    return name_labeled(doc, text)


def name_by_comma(text):
    """Most frequent 'LAST, FIRST' pair (skips doctors 'M.D.' and common medical words)."""
    found = Counter()
    for m in re.finditer(r"\b([A-ZÑ][A-ZÑ'\-]{2,}),\s+([A-ZÑ]{2,})([^\n]{0,25})", text):
        last, after = m.group(1), m.group(3)
        if last in NOT_NAMES or "M.D" in after.upper():
            continue
        found[last] += 1
    top = found.most_common(1)
    return top[0][0] if top and top[0][1] >= 2 else None


def _hda_caption(g):
    """Find the caption 'Printed name and signature of patient' -> (x0, x1, y_top) or None."""
    d = pytesseract.image_to_data(g, config="--psm 6", output_type=Output.DICT)
    words = [(d["text"][k], d["left"][k], d["top"][k], d["width"][k])
             for k in range(len(d["text"])) if d["text"][k].strip()]
    for k, w in enumerate(words):
        if w[0].lower().startswith("patient") and k and words[k - 1][0].lower() == "of":
            # the 'Printed' word that starts this same caption (left of 'patient', same line)
            starts = [x for x in words
                      if abs(x[2] - w[2]) < 30 and x[1] < w[1] and x[0].lower().startswith("printed")]
            if starts:
                return max(0, starts[-1][1] - 30), w[1] + w[3] + 30, starts[-1][2] - 2
    return None


def name_hda_readings(path, pages):
    """HDA: the printed name sits above 'Printed name and signature of patient', usually with
    the pen strokes of the signature running through it. One black/white read can turn those
    strokes into solid bars, so read the same spot several different ways. Returns every
    last-name reading (garbled ones included) - they are checked later, never trusted alone."""
    readings = []
    for i in reversed(pages):
        img = render(path, i)
        g = prep(img)
        cap = _hda_caption(g)
        if not cap:
            continue
        x0, x1, y1 = cap
        gray = ImageOps.autocontrast(img.convert("L"))   # no hard threshold: strokes stay thin
        tall, band = max(0, y1 - 170), max(0, y1 - 100)
        reads = [(g.crop((x0, tall, x1, y1)), "--psm 6"),
                 (gray.crop((x0, band, x1, y1)), "--psm 6"),
                 (gray.crop((x0, band, x1, y1)), "--psm 7"),
                 (prep(img, 110).crop((x0, band, x1, y1)), "--psm 7"),
                 (prep(img, 170).crop((x0, band, x1, y1)), "--psm 7")]
        for crop, cfg in reads:
            lines = [l for l in pytesseract.image_to_string(crop, config=cfg).splitlines()
                     if re.search(r"[A-Z]{3,}", l)]
            for l in reversed(lines):
                if "conforme" in l.lower():
                    continue
                last = last_from_tokens(l.split())
                if last:
                    readings.append(last)
                    break
        if readings:
            return readings
    return readings


def snap_to_batch(readings, batch_names, min_ratio=0.75, margin=0.1, min_votes=2):
    """Match garbled readings ('ARRBEVO') to a name that was read CLEANLY from another
    document of this same batch (CF2/SOA/CSF have printed 'Name of patient' fields).
    Safety rules, so a document is never filed under ANOTHER patient:
      - a reading must be very close to the batch name (ratio >= min_ratio),
      - at least min_votes separate readings must point to the same name,
      - a different name that was read twice (a clean read of someone NOT in this batch)
        blocks the match, because then the batch name is probably the wrong patient.
    Otherwise None (-> next step / Review)."""
    votes = Counter()
    for r in readings:
        ranked = sorted(((SequenceMatcher(None, r, n).ratio(), n) for n in batch_names), reverse=True)
        if ranked and ranked[0][0] >= min_ratio and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= margin):
            votes[ranked[0][1]] += 1
    top = votes.most_common()
    if not top or top[0][1] < min_votes or (len(top) > 1 and top[0][1] == top[1][1]):
        return None
    winner = top[0][0]
    for name, count in Counter(readings).items():       # a repeated reading of someone else
        if count >= 2 and len(name) >= 4 and name != winner and SequenceMatcher(None, name, winner).ratio() < min_ratio:
            return None
    return winner


def name_hda(path, pages, batch_names):
    readings = name_hda_readings(path, pages)
    hit = snap_to_batch(readings, batch_names) if batch_names else None
    if hit:
        return hit, readings
    agreed = [n for n, c in Counter(readings).most_common() if c >= 2]   # 2+ reads must agree
    return (agreed[0] if len(agreed) == 1 else None), readings


def name_from_known(text, known):
    up = text.upper()
    hits = [n for n in known if re.search(rf"\b{re.escape(n)}\b", up)]
    return hits[0] if len(hits) == 1 else None


def choose(cands):
    """Majority vote between the different ways of reading the name. OCR often DROPS
    letters ('RRUEJO'), so a shorter result inside a longer one votes for the longer one.
    A tie means 'not sure' -> None."""
    cands = [c for c in cands if c]
    votes = Counter()
    for c in cands:
        longer = [o for o in cands if o != c and c in o]
        votes[max(longer, key=len) if longer else c] += 1
    ranked = votes.most_common()
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


def find_last_name(doc, text, path, pages, known, batch_names=None, info=None):
    if doc == "HDA":      # no printed 'Name of patient' field: signature area + batch names
        try:
            hit, readings = name_hda(path, pages, batch_names or set())
        except Exception:
            hit, readings = None, []
        print(f"\n    HDA name readings: {readings} -> {hit}")      # so a wrong name can be traced
        if info is not None:
            info["readings"] = readings
        return hit or name_from_known(text, known)
    attempts = [
        lambda: name_labeled(doc, text),
        lambda: name_labeled_clean_ocr(doc, path, pages),
        lambda: name_from_known(text, known),
        lambda: name_by_comma(text),
    ]
    raw = []
    for attempt in attempts:
        try:
            raw.append(attempt())
        except Exception:
            raw.append(None)
    cands = []
    for name in raw:
        name = re.sub(r"[^A-ZÑ'\- ]", "", (name or "").upper()).strip()
        if len(name) >= 3 and name not in NOT_NAMES:
            cands.append(name)
    return choose(cands)


# ------------------------------------------------------------------- output
def log(msg):
    print(msg)
    emit("log", text=msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def housekeeping():
    """Run at start: archive a big log, delete old debug files and old OCR cache (they hold patient text)."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_MB * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE.with_name(LOG_FILE.stem + ".1" + LOG_FILE.suffix))
    except OSError:
        pass
    now = time.time()
    for folder, pattern, days in ((LOG_FILE.parent, "debug_*.txt", KEEP_DEBUG_DAYS),
                                  (CACHE_DIR, "*.json", KEEP_CACHE_DAYS),
                                  (CACHE_DIR, "*.tmp", KEEP_CACHE_DAYS)):
        try:
            for f in folder.glob(pattern):
                if now - f.stat().st_mtime > days * 86400:
                    f.unlink(missing_ok=True)
        except OSError:
            pass


def unique_path(folder, stem, taken=()):
    p, n = folder / f"{stem}.pdf", 2
    while p.exists() or p in taken:   # same patient, same day, scanned again -> SOA_2.pdf
        p, n = folder / f"{stem}_{n}.pdf", n + 1
    return p


def copy_pdfa_identity(reader, writer):
    """Copy the PDF/A markers (XMP metadata, colour profile, document info, file ID) from
    the NAPS2 original into the split file. Without them PhilHealth sees a plain PDF.
    The pages themselves are copied untouched, so they stay PDF/A-1b."""
    root = reader.trailer["/Root"]
    for key in ("/Metadata", "/OutputIntents"):      # XMP block that says 'PDF/A-1b' + ICC profile
        if key in root:
            # raw_get keeps it a REFERENCE, so it is written as its own object. Using
            # root[key] wrote the XMP inside the catalog and Acrobat said "root object invalid".
            writer._root_object[NameObject(key)] = root.raw_get(key).clone(writer)
    if reader.metadata:                               # Title/Author/etc. must match the XMP
        writer.add_metadata({k: str(v) for k, v in reader.metadata.items()})
    try:                                              # PDF/A-1 wants a file ID in the trailer
        if "/ID" in reader.trailer:
            writer._ID = reader.trailer["/ID"]
    except Exception:
        pass


def save_pages(reader, pages, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    w = PdfWriter()
    for i in pages:
        w.add_page(reader.pages[i])
    copy_pdfa_identity(reader, w)
    with open(dest, "wb") as f:
        w.write(f)


def flag_hda_conflicts(segs):
    """Safety check. Pages of one patient are scanned together, so an HDA sits next to the
    same patient's CF2/SOA/CSF/LAB. If the document BEFORE it and the document AFTER it
    (HDAs skipped) both belong to one patient and the HDA was given a DIFFERENT name, the
    name is probably a misread -> the HDA goes to Review instead of the wrong folder."""
    others = [s for s in segs if s["doc"] != "HDA"]
    for s in segs:
        if s["doc"] != "HDA" or not s.get("name") or not s["pages"]:
            continue
        first, last = s["pages"][0], s["pages"][-1]
        before = [o for o in others if o["pages"][-1] < first and o.get("name")]
        after = [o for o in others if o["pages"][0] > last and o.get("name")]
        if before and after and before[-1]["name"] == after[0]["name"] != s["name"]:
            s["conflict"] = before[-1]["name"]


def span(pages):
    if len(pages) == 1:
        return f"p{pages[0] + 1}"
    if pages[-1] - pages[0] == len(pages) - 1:           # one unbroken run of pages
        return f"p{pages[0] + 1}-{pages[-1] + 1}"
    return "p" + ",".join(str(i + 1) for i in pages)


# ------------------------------------------------------------ PDF/A checking
_PDFA_PART = re.compile(rb'pdfaid:part(?:>|=")\s*(\d)')
_PDFA_CONF = re.compile(rb'pdfaid:conformance(?:>|=")\s*([A-Za-z])')
MARKER_NAMES = {"xmp": "PDF/A declaration", "intent": "colour profile", "id": "file ID"}
_VERAPDF = "unset"


def pdfa_markers(reader):
    """Which PDF/A markers does this PDF carry? -> (label like 'PDF/A-1B' or None, set of
    markers found: 'xmp' = the PDF/A declaration, 'intent' = colour profile, 'id' = file ID)."""
    found, label = set(), None
    try:
        root = reader.trailer["/Root"]
        if "/Metadata" in root:
            data = root["/Metadata"].get_object().get_data()
            part, conf = _PDFA_PART.search(data), _PDFA_CONF.search(data)
            if part and conf:
                label = f"PDF/A-{part.group(1).decode()}{conf.group(1).decode().upper()}"
                found.add("xmp")
        if "/OutputIntents" in root and len(root["/OutputIntents"]) > 0:
            found.add("intent")
        if "/ID" in reader.trailer:
            found.add("id")
    except Exception:
        pass
    return label, found


def find_verapdf():
    """veraPDF (free validator) if it is installed - used for a full PDF/A check."""
    global _VERAPDF
    if _VERAPDF == "unset":
        _VERAPDF = shutil.which("verapdf") or shutil.which("verapdf.bat")
        if not _VERAPDF:
            for base in (r"C:\Program Files\veraPDF", r"C:\Program Files (x86)\veraPDF"):
                for name in ("verapdf.bat", "verapdf.exe"):
                    if (Path(base) / name).exists():
                        _VERAPDF = str(Path(base) / name)
                        break
                if _VERAPDF:
                    break
    return _VERAPDF or None


def run_verapdf(exe, dest):
    """-> (ok, message). An unclear answer counts as OK but says so, it is never hidden."""
    try:
        out = subprocess.run([exe, "--flavour", "1b", str(dest)],
                             capture_output=True, text=True, timeout=120)
        lines = (out.stdout or "").strip().splitlines()
        head = lines[0].upper() if lines else ""
        if head.startswith("PASS"):
            return True, "veraPDF: PDF/A-1b compliant"
        if head.startswith("FAIL"):
            return False, "veraPDF: NOT PDF/A-1b compliant"
        return True, f"veraPDF gave no clear answer ({(out.stderr or '').strip()[:80]})"
    except Exception as e:
        return True, f"veraPDF could not run ({e})"


def verify_output(dest, expected_pages, expected_markers):
    """Re-open a file we just saved. -> (hard_error or None, pdfa_ok, detail).
    A hard error (cannot open / wrong page count) undoes the whole batch.
    A PDF/A problem is reported loudly but the sorted file stays."""
    try:
        r = PdfReader(str(dest))
        if len(r.pages) != expected_pages:
            return f"has {len(r.pages)} pages, expected {expected_pages}", False, "wrong page count"
    except Exception as e:
        return f"cannot be re-opened ({e})", False, "unreadable"
    if not CHECK_PDFA:
        return None, True, "not checked"
    label, found = pdfa_markers(r)
    missing = [MARKER_NAMES[m] for m in ("xmp", "intent", "id") if m in expected_markers and m not in found]
    if missing:
        return None, False, "NOT PDF/A - missing: " + ", ".join(missing)
    exe = find_verapdf()
    if exe:
        ok, msg = run_verapdf(exe, dest)
        return None, ok, msg
    return None, True, f"{label} markers ok (install veraPDF for a full check)"


# ------------------------------------------------------- page accounting
def check_page_accounting(total, plan):
    """Every page of the batch must go to exactly ONE place. -> list of problems (empty = OK)."""
    count = Counter(i for item in plan for i in item["pages"])
    problems = []
    lost = [i + 1 for i in range(total) if count[i] == 0]
    twice = sorted(i + 1 for i, c in count.items() if c > 1 and 0 <= i < total)
    outside = sorted(i + 1 for i in count if i < 0 or i >= total)
    if lost:
        problems.append("page(s) not going anywhere: " + ", ".join(map(str, lost)))
    if twice:
        problems.append("page(s) used twice: " + ", ".join(map(str, twice)))
    if outside:
        problems.append("page number(s) outside the batch: " + ", ".join(map(str, outside)))
    return problems


# ------------------------------------------------------------- manifest
def write_manifest(path, plan):
    """One CSV per batch: where every page went and why (opens in Excel)."""
    try:
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        out = MANIFEST_DIR / f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["source", "pages", "document", "result", "patient", "date",
                        "saved_as", "reason_or_note", "name_readings", "pdfa_check"])
            for it in plan:
                dest = it.get("dest")
                if dest is None:
                    saved = ""
                elif it["kind"] == "sorted":
                    saved = str(dest.relative_to(SORTED))
                else:
                    saved = f"REVIEW\\{dest.name}"
                w.writerow([path.name, span(it["pages"]), it["doc"], it["result"],
                            it.get("name") or "", it.get("date") or "", saved,
                            it.get("reason", ""), "|".join(it.get("readings") or []),
                            it.get("pdfa", "")])
        log(f"  manifest: {out}")
    except Exception as e:
        log(f"  ! could not write the manifest: {e}")


# --------------------------------------------------------- completeness
DOC_FILE = re.compile(r"^(CF2|SOA|CSF|HDA|LAB)(?:_\d+)?$", re.I)


def scan_completeness():
    """{(date_folder, patient_folder): Counter(doc -> how many files)} for everything in SORTED."""
    rows = {}
    if not SORTED.exists():
        return rows
    for date_dir in SORTED.iterdir():
        if not date_dir.is_dir():
            continue
        for name_dir in date_dir.iterdir():
            if not name_dir.is_dir():
                continue
            counts = Counter()
            for f in name_dir.glob("*.pdf"):
                m = DOC_FILE.match(f.stem)
                if m:
                    counts[m.group(1).upper()] += 1
            rows[(date_dir.name, name_dir.name)] = counts
    return rows


def completeness_issues(counts):
    """-> (missing documents, duplicated documents). LAB may repeat or be absent."""
    missing = [d for d in REQUIRED_DOCS if counts.get(d, 0) == 0]
    extra = [f"{d} x{c}" for d, c in sorted(counts.items()) if c > 1 and d != "LAB"]
    return missing, extra


def _date_key(key):
    try:
        return (datetime.strptime(key[0], "%m-%d-%Y"), key[1])
    except ValueError:
        return (datetime.max, key[0] + key[1])


def write_completeness_report(rows):
    """Writes REPORT_FILE. -> list of (key, missing, extra) for patients that need attention."""
    problems = []
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT_FILE, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["date", "patient", *REQUIRED_DOCS, "LAB", "status"])
            for key in sorted(rows, key=_date_key):
                counts = rows[key]
                missing, extra = completeness_issues(counts)
                parts = []
                if missing:
                    parts.append("MISSING: " + ", ".join(missing))
                if extra:
                    parts.append("DUPLICATE: " + ", ".join(extra))
                status = "; ".join(parts) or "complete"
                if parts:
                    problems.append((key, missing, extra))
                w.writerow([key[0], key[1], *[counts.get(d, 0) for d in REQUIRED_DOCS],
                            counts.get("LAB", 0), status])
    except Exception as e:
        log(f"  ! could not write the completeness report: {e}")
    return problems


def describe_issue(key, missing, extra):
    parts = []
    if missing:
        parts.append("missing " + ", ".join(missing))
    if extra:
        parts.append("duplicate " + ", ".join(extra))
    return f"{key[0]}\\{key[1]}: " + "; ".join(parts)


# ------------------------------------------------- same last name, same day
FIRST_PATTERNS = {   # the FIRST name, read where the paper prints "LAST, FIRST" or "LAST FIRST"
    "SOA": r"(?i:patient name)[\s:|\-]*[A-ZÑ][A-ZÑ'\-]+(?: [A-ZÑ][A-ZÑ'\-]+)*\s*,\s*([A-ZÑ][A-ZÑ'\-]{2,})",
    "LAB": r"\b(?i:name)\b[\s:|\-]*[A-ZÑ][A-ZÑ'\-]+(?: [A-ZÑ][A-ZÑ'\-]+)*\s*,\s*([A-ZÑ][A-ZÑ'\-]{2,})",
    "CF2": r"(?i:name of patient)[\s:|\-]*[A-ZÑ][A-ZÑ'\-]+[\s|]+([A-ZÑ][A-ZÑ'\-]{2,})",
}


def first_name_of(doc, text):
    pattern = FIRST_PATTERNS.get(doc)
    m = re.search(pattern, text) if pattern else None
    name = m.group(1).upper() if m else None
    return name if name and name not in NOT_NAMES and name not in SUFFIXES else None


def flag_same_surname(segs):
    """Folders are named by LAST name only. If one batch holds documents for two patients with
    the same last name on the same date (different first names), the pages cannot be told
    apart safely -> all of those documents go to Review. OCR slips ('MARITE'/'MARITES') are
    tolerated; the first names must be clearly different."""
    groups = {}
    for s in segs:
        if s.get("name") and s.get("date"):
            groups.setdefault((s["name"], s["date"]), []).append(s)
    for members in groups.values():
        clusters = []
        for s in members:
            first = s.get("first")
            if not first:
                continue
            for c in clusters:
                if first == c[0] or SequenceMatcher(None, first, c[0]).ratio() >= 0.75:
                    c.append(first)
                    break
            else:
                clusters.append([first])
        if len(clusters) >= 2:
            label = " / ".join(c[0] for c in clusters)
            for s in members:
                s["same_surname"] = label


# ------------------------------------------------------------ Review workflow
REVIEW_FIELDS = ["file", "source", "pages", "doc", "reason", "name_guess", "date_guess",
                 "added", "status", "filed_as"]
_QUEUE_LOCK = threading.Lock()


def reason_slug(reasons):
    """Short, file-name-safe reason codes: name-not-found -> 'no-name', ..."""
    codes = []
    for r in reasons:
        low = r.lower()
        if "longer than" in low:
            codes.append("too-long")
        elif "both sides" in low:
            codes.append("name-conflict")
        elif "two different patients" in low:
            codes.append("same-surname")
        elif "last name" in low:
            codes.append("no-name")
        elif "date not found" in low:
            codes.append("no-date")
        elif "no title" in low:
            codes.append("no-title")
        else:
            codes.append(re.sub(r"[^a-z0-9]+", "-", low)[:20].strip("-") or "check")
    return "+".join(dict.fromkeys(codes)) or "check"


def review_queue_path():
    return REVIEW / "review_queue.csv"


def read_review_queue():
    p = review_queue_path()
    if not p.exists():
        return []
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _write_review_queue(rows):
    p = review_queue_path()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, p)


def queue_review_items(path, plan):
    """Remember why each page group went to Review (review_queue.csv, opens in Excel)."""
    rows = [{"file": it["dest"].name, "source": path.name, "pages": span(it["pages"]), "doc": it["doc"],
             "reason": it.get("reason", ""), "name_guess": it.get("name") or "",
             "date_guess": it.get("date") or "", "added": f"{datetime.now():%Y-%m-%d %H:%M}",
             "status": "open", "filed_as": ""} for it in plan if it["kind"] == "review"]
    if not rows:
        return
    with _QUEUE_LOCK:
        try:
            REVIEW.mkdir(parents=True, exist_ok=True)
            _write_review_queue(read_review_queue() + rows)
        except OSError as e:
            log(f"  ! could not update review_queue.csv ({e}); the files are still in the Review folder")


def list_review_items():
    """Open Review items, newest first: queue rows whose PDF still exists, plus PDFs in the
    Review folder that the queue does not know (older files)."""
    known = {r["file"]: r for r in read_review_queue() if r.get("status") == "open"}
    items = []
    if REVIEW.exists():
        for f in REVIEW.glob("*.pdf"):
            r = known.get(f.name)
            if r is None:
                m = re.search(r"_(CF2|SOA|CSF|HDA|LAB)(?:__|\.pdf$)", f.name)
                r = {"file": f.name, "source": "", "pages": "", "doc": m.group(1) if m else "",
                     "reason": "no note saved (older file)", "name_guess": "", "date_guess": "",
                     "added": f"{datetime.fromtimestamp(f.stat().st_mtime):%Y-%m-%d %H:%M}",
                     "status": "open", "filed_as": ""}
            items.append(dict(r))
    return sorted(items, key=lambda r: (r["added"], r["file"]), reverse=True)


def _review_file(name):
    p = REVIEW / Path(str(name)).name
    if p.suffix.lower() != ".pdf" or not p.is_file():
        raise FileNotFoundError(f"{name} is not in the Review folder any more")
    return p


def _mark_review(file, status, filed_as):
    with _QUEUE_LOCK:
        try:
            rows = read_review_queue()
            hit = False
            for r in rows:
                if r.get("file") == file and r.get("status") == "open":
                    r["status"], r["filed_as"], hit = status, filed_as, True
                    break
            if not hit:
                rows.append({"file": file, "status": status, "filed_as": filed_as,
                             "added": f"{datetime.now():%Y-%m-%d %H:%M}"})
            _write_review_queue(rows)
        except OSError as e:
            log(f"  ! could not update review_queue.csv ({e})")


def clean_name(value):
    v = re.sub(r"\s+", " ", (value or "").strip().upper())
    if not re.fullmatch(r"[A-ZÑ][A-ZÑ'\- ]{1,40}", v):
        raise ValueError("Type the patient's last name using letters only (for example ARRUEJO).")
    return v


def clean_date(value):
    v = (value or "").strip().replace("/", "-").replace(".", "-")
    try:
        d = datetime.strptime(v, "%m-%d-%Y")
    except ValueError:
        raise ValueError("Type the date as month-day-year, for example 10-19-2026.")
    if not 2020 <= d.year <= 2035:
        raise ValueError("The year looks wrong. Type the date as month-day-year, for example 10-19-2026.")
    return d.strftime("%m-%d-%Y")


def append_pdf(existing, extra):
    """Add the pages of `extra` to the end of `existing` (all lab reports of a day stay in one
    LAB.pdf). Written to a temp file and checked before it replaces the old one."""
    ra, rb = PdfReader(str(existing)), PdfReader(str(extra))
    na, nb = len(ra.pages), len(rb.pages)
    w = PdfWriter()
    for page in ra.pages:
        w.add_page(page)
    for page in rb.pages:
        w.add_page(page)
    copy_pdfa_identity(ra, w)
    tmp = existing.with_name(existing.stem + ".merging.tmp")
    try:
        with open(tmp, "wb") as f:
            w.write(f)
        if len(PdfReader(str(tmp)).pages) != na + nb:
            raise RuntimeError("the combined file has the wrong number of pages")
        os.replace(tmp, existing)
    finally:
        tmp.unlink(missing_ok=True)
    extra.unlink()
    return na + nb


def file_review_item(file, doc, name, date):
    """File a Review PDF as SORTED\\date\\NAME\\DOC.pdf (a second LAB is added to the existing LAB.pdf).
    Returns {'dest', 'merged', 'pdfa_ok', 'pdfa'}; raises ValueError for a bad name/date."""
    src = _review_file(file)
    doc = (doc or "").strip().upper()
    if doc not in DOC_TYPES:
        raise ValueError("Choose the document type.")
    name, date = clean_name(name), clean_date(date)
    reader = PdfReader(str(src))
    pages = len(reader.pages)
    _, found = pdfa_markers(reader)
    folder = SORTED / date / name
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{doc}.pdf"
    merged = doc == "LAB" and target.exists()
    if merged:
        expected = append_pdf(target, src)
        dest = target
    else:
        dest = unique_path(folder, doc)
        shutil.move(str(src), str(dest))
        expected = pages
    err, ok, detail = verify_output(dest, expected, found | {"xmp"})
    if err:
        raise RuntimeError(f"{dest.name} {err}")
    rel = str(dest.relative_to(SORTED))
    _mark_review(src.name, "filed", rel)
    log(f"  Review: {src.name} filed as {rel}" + ("  [added to the existing LAB.pdf]" if merged else ""))
    return {"dest": rel, "merged": merged, "pdfa_ok": ok, "pdfa": detail}


def dismiss_review_item(file):
    """Not needed? Move it to REVIEW\\Dismissed (nothing is deleted)."""
    src = _review_file(file)
    folder = REVIEW / "Dismissed"
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_path(folder, src.stem)
    shutil.move(str(src), str(dest))
    _mark_review(src.name, "dismissed", f"Dismissed\\{dest.name}")
    log(f"  Review: {src.name} dismissed (kept in Review\\Dismissed)")
    return {"dest": f"Dismissed\\{dest.name}"}


# ----------------------------------------------------------- moving files around
def list_sorted_tree():
    """[{date, patients: [{name, files: [{file, rel, kb}]}]}] for everything in SORTED."""
    tree = []
    if not SORTED.exists():
        return tree
    for date_dir in sorted((d for d in SORTED.iterdir() if d.is_dir()),
                           key=lambda d: _date_key((d.name, ""))):
        patients = []
        for name_dir in sorted(d for d in date_dir.iterdir() if d.is_dir()):
            files = [{"file": f.name, "rel": f"{date_dir.name}/{name_dir.name}/{f.name}",
                      "kb": max(1, round(f.stat().st_size / 1024))}
                     for f in sorted(name_dir.glob("*.pdf"))]
            patients.append({"name": name_dir.name, "files": files})
        tree.append({"date": date_dir.name, "patients": patients})
    return tree


def transfer_files(rel_paths, dest_dir, move=False, keep_structure=True):
    """Copy (or move) sorted PDFs to another folder on this PC.
    Safe by design: never overwrites; every copy is checked (size + page count) before the
    original is removed; a file that already exists at the destination is skipped.
    Returns one {'path', 'status', 'detail'} per file."""
    dest_root = Path(dest_dir)
    if not dest_root.is_dir():
        raise ValueError("The destination folder does not exist.")
    dest_res, sorted_res = dest_root.resolve(), SORTED.resolve()
    if dest_res == sorted_res or sorted_res in dest_res.parents:
        raise ValueError("Pick a folder outside the Sorted folder.")
    results = []
    for rel in rel_paths:
        tmp = None
        try:
            parts = PurePath(str(rel).replace("\\", "/")).parts
            src = SORTED.joinpath(*parts).resolve()
            if sorted_res not in src.parents or src.suffix.lower() != ".pdf" or not src.is_file():
                raise ValueError("not a sorted PDF")
            relparts = src.relative_to(sorted_res).parts
            target = dest_res.joinpath(*relparts) if keep_structure else dest_res / "_".join(relparts)
            if target.exists():
                same = target.stat().st_size == src.stat().st_size
                results.append({"path": str(rel), "status": "skipped",
                                "detail": "already there" if same else "a different file with this name is already there"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            shutil.copy2(src, tmp)
            if tmp.stat().st_size != src.stat().st_size:
                raise OSError("the copy is a different size")
            if len(PdfReader(str(tmp)).pages) != len(PdfReader(str(src)).pages):
                raise OSError("the copy has a different page count")
            os.replace(tmp, target)
            tmp = None
            if move:
                src.unlink()
                for d in (src.parent, src.parent.parent):      # tidy up emptied folders only
                    if d != sorted_res:
                        try:
                            d.rmdir()
                        except OSError:
                            pass
            results.append({"path": str(rel), "status": "moved" if move else "copied", "detail": str(target)})
        except Exception as e:
            results.append({"path": str(rel), "status": "error", "detail": str(e)})
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    done = Counter(r["status"] for r in results)
    log(f"  files {'moved' if move else 'copied'} to {dest_res}: " +
        ", ".join(f"{n} {k}" for k, n in sorted(done.items())))
    return results


def plan_view(plan):
    """The plan in a form the UI can show."""
    out = []
    for it in plan:
        dest = it.get("dest")
        saved = ""
        if dest is not None:
            saved = str(dest.relative_to(SORTED)) if it["kind"] == "sorted" else f"Review\\{dest.name}"
        out.append({"pages": span(it["pages"]), "doc": it["doc"], "result": it["result"],
                    "name": it.get("name") or "", "date": it.get("date") or "", "saved_as": saved,
                    "reason": it.get("reason", ""), "pdfa": it.get("pdfa", "")})
    return out


# ---------------------------------------------------------------- process
def _process(path, dry):
    """Sorts one batch. Returns True when the batch is finished, False when it was NOT
    (the original is then left in INBOX and nothing from this batch is kept)."""
    log(f"=== {path.name}")
    emit("batch_start", name=path.name, dry=dry)
    emit("stage", key="read", status="running", detail="")
    reader, texts = read_all_pages(path)
    total = len(texts)
    emit("stage", key="read", status="done", detail=f"{total} pages")
    emit("stage", key="split", status="running", detail="")

    expected_markers = {"xmp"}
    if CHECK_PDFA:
        src_label, src_found = pdfa_markers(reader)
        expected_markers = src_found | {"xmp"}
        if "xmp" not in src_found:
            log("  !! WARNING: this batch is NOT PDF/A, so the sorted files will be rejected by "
                "PhilHealth. In NAPS2 open the PDF settings, set Compatibility to PDF/A-1b, "
                "and scan again.")
        else:
            lacking = [MARKER_NAMES[m] for m in ("intent", "id") if m not in src_found]
            log(f"  batch format: {src_label}" + (f" (no {', no '.join(lacking)})" if lacking else ""))

    segs, orphans = split_batch(texts)
    ignored = [s for s in segs if s["doc"] == "IGNORE"]
    segs = [s for s in segs if s["doc"] != "IGNORE"]
    if "--debug" in sys.argv:
        write_debug(path, texts, segs + ignored, [])
    log(f"  {total} pages -> {len(segs)} documents found")
    emit("stage", key="split", status="done", detail=f"{len(segs)} documents, {len(ignored)} not needed")
    emit("stage", key="identify", status="running", detail="")

    known = {d.name for d in SORTED.glob("*/*") if d.is_dir()} if SORTED.exists() else set()
    # HDAs last: their name is read from a signature area, so it is checked against the
    # names read cleanly from the other documents of this batch.
    order = [s for s in segs if s["doc"] != "HDA"] + [s for s in segs if s["doc"] == "HDA"]
    batch_names = set()
    for k, s in enumerate(order, 1):
        text = "\n".join(texts[i] for i in s["pages"])
        print(f"\r  identifying document {k}/{len(order)}", end="", flush=True)
        check_cancel()
        emit("progress", key="identify", done=k - 1, total=len(order))
        s["name"] = find_last_name(s["doc"], text, path, s["pages"], known, batch_names, s)
        s["date"] = find_date(s["doc"], text)
        s["first"] = first_name_of(s["doc"], text)
        if s["name"]:
            known.add(s["name"])
            if s["doc"] != "HDA":
                batch_names.add(s["name"])
    print()
    emit("progress", key="identify", done=len(order), total=len(order))
    fill_missing_dates(segs)
    flag_same_surname(segs)
    emit("stage", key="identify", status="done", detail=f"{len(order)} documents read")

    # ALL lab reports of the same patient and date go into ONE file (LAB.pdf).
    # A lab report missing its name/date, or longer than it should be, is not merged -> REVIEW.
    labs, merged = {}, []
    for s in segs:
        if (s["doc"] == "LAB" and s.get("name") and s.get("date") and not s.get("same_surname")
                and len(s["pages"]) <= MAX_PAGES_PER_DOC["LAB"]):
            key = (s["name"], s["date"])
            if key in labs:
                labs[key]["pages"] += s["pages"]
                labs[key]["combined"] += 1
                continue
            s["combined"] = 1
            labs[key] = s
        merged.append(s)
    segs = merged
    flag_hda_conflicts(segs)

    # ---- the PLAN: every page of the batch gets exactly one destination (nothing is saved yet)
    taken, plan = set(), []
    for s in ignored:
        plan.append({"kind": "ignored", "result": "ignored", "doc": s["label"], "pages": s["pages"],
                     "dest": None, "reason": "not needed"})
    for s in segs:
        pages, doc = s["pages"], s["doc"]
        reasons = []
        if len(pages) > MAX_PAGES_PER_DOC[doc] and not s.get("combined"):
            reasons.append(f"{len(pages)} pages is longer than a {doc} should be")
        if s.get("conflict"):
            reasons.append(f"read as {s['name']} but the documents on both sides are {s['conflict']}")
        if s.get("same_surname"):
            reasons.append(f"two different patients named {s['name']} on this date "
                           f"(first names {s['same_surname']}) - pages cannot be told apart")
        if not s["name"]:
            reasons.append("last name not found")
        if not s["date"]:
            reasons.append("date not found")
        item = {"doc": doc, "pages": pages, "name": s["name"], "date": s["date"],
                "readings": s.get("readings")}
        if reasons:
            item.update(kind="review", result="REVIEW", reason="; ".join(reasons),
                        dest=unique_path(REVIEW, f"{path.stem}_{span(pages)}_{doc}__{reason_slug(reasons)}", taken))
        else:
            notes = []
            if s.get("date_note"):
                notes.append(s["date_note"])
            if s.get("combined", 1) > 1:
                notes.append(f"{s['combined']} lab reports combined")
            item.update(kind="sorted", result="sorted", reason="; ".join(notes),
                        dest=unique_path(SORTED / s["date"] / s["name"], doc, taken))
        taken.add(item["dest"])
        plan.append(item)
    if orphans:     # pages before the first recognisable title page
        dest = unique_path(REVIEW, f"{path.stem}_{span(orphans)}_UNKNOWN__no-title", taken)
        taken.add(dest)
        plan.append({"kind": "review", "result": "REVIEW", "doc": "???", "pages": orphans, "dest": dest,
                     "reason": "no title page found before these pages"})
    plan.sort(key=lambda it: it["pages"][0])

    for it in plan:
        pg = f"{span(it['pages']):>9}"
        if it["kind"] == "ignored":
            log(f"  {pg}  {it['doc']}  ->  ignored (not needed)")
        elif it["kind"] == "review":
            log(f"  {pg}  {it['doc']}  ->  REVIEW ({it['reason']})")
        else:
            note = f"   [{it['reason'].replace('; ', ']   [')}]" if it["reason"] else ""
            log(f"  {pg}  {it['doc']}  ->  {it['dest'].relative_to(SORTED)}{note}")

    emit("plan", items=plan_view(plan))

    # ---- SAFETY CHECK 1: every page accounted for, exactly once
    emit("stage", key="check", status="running", detail="")
    check_cancel()
    problems = check_page_accounting(total, plan)
    if problems:
        emit("stage", key="check", status="failed", detail="; ".join(problems))
        log("  !! PAGE COUNT MISMATCH - " + "; ".join(problems))
        log("  !! Nothing was saved. The original stays in the Inbox.")
        return False
    emit("stage", key="check", status="done", detail=f"all {total} pages accounted for")

    stats = Counter(it["kind"] for it in plan)
    pdfa_bad = 0

    # ---- SAFETY CHECK 2: save, then re-open every saved file. Any hard failure undoes the batch.
    if dry:
        emit("stage", key="save", status="skipped", detail="test only - nothing saved")
    else:
        files_total = sum(1 for it in plan if it["dest"] is not None)
        emit("stage", key="save", status="running", detail="")
        written, failure = [], None
        for it in plan:
            if it["dest"] is None:
                continue
            emit("progress", key="save", done=len(written), total=files_total)
            written.append(it["dest"])
            try:
                save_pages(reader, it["pages"], it["dest"])
                err, ok, detail = verify_output(it["dest"], len(it["pages"]), expected_markers)
            except Exception as e:
                err, ok, detail = str(e), False, "could not be saved"
            it["pdfa"] = detail
            if err:
                failure = f"{it['dest'].name} {err}"
                break
            if not ok:
                pdfa_bad += 1
                log(f"  !! PDF/A CHECK FAILED for {it['dest'].name}: {detail}")
        if failure:
            for dest in written:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            for dest in written:
                for d in (dest.parent, dest.parent.parent):
                    if d not in (SORTED, REVIEW):
                        try:
                            d.rmdir()      # only succeeds when empty
                        except OSError:
                            pass
            for it in plan:
                it["result"] = "FAILED - undone"
            emit("stage", key="save", status="failed", detail=failure)
            emit("plan", items=plan_view(plan))
            log(f"  !! BATCH UNDONE: {failure}. Files saved from this batch were removed and "
                f"the original stays in the Inbox.")
            write_manifest(path, plan)
            return False
        emit("progress", key="save", done=files_total, total=files_total)
        emit("stage", key="save", status="done", detail=f"{files_total} files saved and checked")
        emit("plan", items=plan_view(plan))
        write_manifest(path, plan)
        queue_review_items(path, plan)

    summary = f"  done: {stats['sorted']} sorted, {stats['review']} to Review"
    if stats["ignored"]:
        summary += f", {stats['ignored']} ignored"
    if pdfa_bad:
        summary += f", {pdfa_bad} FAILED the PDF/A check"
    log(summary)
    global LAST_SUMMARY
    LAST_SUMMARY = {"sorted": stats["sorted"], "review": stats["review"], "ignored": stats["ignored"],
                    "pdfa_failed": pdfa_bad, "issues": 0}
    emit("stage", key="finish", status="running", detail="")

    # ---- COMPLETENESS of the patients touched by this batch
    rows = scan_completeness()
    touched = {(it["date"], it["name"]) for it in plan if it["kind"] == "sorted"}
    if dry:     # nothing was saved, so add what WOULD be saved
        for it in plan:
            if it["kind"] == "sorted":
                rows.setdefault((it["date"], it["name"]), Counter())[it["doc"]] += 1
    issues = []
    for key in sorted(touched, key=_date_key):
        missing, extra = completeness_issues(rows.get(key, Counter()))
        if missing or extra:
            issues.append((key, missing, extra))
    LAST_SUMMARY["issues"] = len(issues)
    log(f"  completeness: {len(touched) - len(issues)} of {len(touched)} patients in this batch "
        f"are complete (CF2, SOA, CSF, HDA all present, no duplicates)")
    for key, missing, extra in issues:
        log(f"     ! {describe_issue(key, missing, extra)}")
    if issues:
        log("     (a document waiting in Review is not counted)")

    if dry:
        emit("stage", key="finish", status="done", detail="test only - the original was not moved")
        return True
    write_completeness_report(rows)
    try:
        PROCESSED.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), unique_path(PROCESSED, path.stem))
    except OSError as e:
        log(f"  !! The sorted files are saved, but the original could not be moved out of the "
            f"Inbox ({e}). Close any program using it and move it to Processed by hand, or it "
            f"would be sorted again.")
        emit("stage", key="finish", status="failed", detail="the original could not be moved out of the Inbox")
        return False
    drop_ocr_cache(path)
    emit("stage", key="finish", status="done", detail="original moved to Processed")
    return True


def process(path, dry):
    """Sorts one batch (see _process). Returns True when finished, False when not.
    Also tells the UI how it ended, and honours the Stop button."""
    global LAST_SUMMARY
    LAST_SUMMARY = {}
    try:
        ok = _process(path, dry)
    except Cancelled:
        log("  !! Stopped. Nothing from this batch was kept; the original stays in the Inbox.")
        emit("batch_end", ok=False, reason="stopped", summary={})
        return False
    except Exception as e:
        emit("batch_end", ok=False, reason=str(e), summary={})
        raise
    finally:
        CANCEL.clear()
    emit("batch_end", ok=ok, reason="" if ok else "not finished - see the log", summary=dict(LAST_SUMMARY))
    return ok


def is_ready(path):
    """Skip files NAPS2 is still writing."""
    try:
        size = path.stat().st_size
        time.sleep(3)
        return size > 0 and size == path.stat().st_size
    except OSError:
        return False


def main():
    dry = "--dry-run" in sys.argv
    once = dry or "--once" in sys.argv
    if "--init-settings" in sys.argv:
        if SETTINGS_FILE.exists():
            print(f"{SETTINGS_FILE} already exists - edit that file.")
        else:
            save_settings(current_settings())
            print(f"Created {SETTINGS_FILE} - open it in Notepad to change folders and limits.")
        return
    for problem in apply_settings(load_settings()):
        print(f"settings.json: {problem} (ignored)")
    housekeeping()
    for folder in (INBOX, SORTED, REVIEW, PROCESSED):
        folder.mkdir(parents=True, exist_ok=True)
    if "--report" in sys.argv:
        rows = scan_completeness()
        problems = write_completeness_report(rows)
        print(f"{len(rows)} patient folders checked, {len(problems)} need attention")
        for key, missing, extra in problems:
            print("  ! " + describe_issue(key, missing, extra))
        print("(a document waiting in Review is not counted)")
        print(f"Full list: {REPORT_FILE}")
        return
    print(f"{'DRY RUN - ' if dry else ''}Watching {INBOX} ... (Ctrl+C to stop)")
    failed = set()      # batches that could not be finished: left alone until the script is restarted
    while True:
        for f in sorted(INBOX.glob("*.pdf")):
            if f.name in failed or not is_ready(f):
                continue
            try:
                ok = process(f, dry)
            except Exception as e:
                log(f"  ! could not process {f.name}: {e}")
                ok = False
            if not ok:
                failed.add(f.name)
        if once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
