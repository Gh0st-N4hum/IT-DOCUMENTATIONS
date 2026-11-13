"""
Tests for scan_sorter.py and scan_sorter_ui.py.   Run:  python -m unittest test_scan_sorter -v
Uses only made-up PDFs in a temporary folder; it never touches C:\\Scans.
"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pypdf import PdfWriter, PdfReader
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import scan_sorter as ss
import scan_sorter_ui as ui

XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       b'<rdf:Description xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"><pdfaid:part>1</pdfaid:part>'
       b'<pdfaid:conformance>B</pdfaid:conformance></rdf:Description></rdf:RDF></x:xmpmeta>')


def make_pdf(path, pages=1, pdfa=True):
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    if pdfa:
        s = DecodedStreamObject()
        s.set_data(XMP)
        s.update({NameObject("/Type"): NameObject("/Metadata"), NameObject("/Subtype"): NameObject("/XML")})
        w._root_object[NameObject("/Metadata")] = w._add_object(s)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        w.write(f)
    return Path(path)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = ss.current_settings()
        r = self.tmp
        ss.apply_settings({"inbox": str(r / "Inbox"), "sorted": str(r / "Sorted"), "review": str(r / "Review"),
                           "processed": str(r / "Processed"), "log_file": str(r / "log.txt"),
                           "manifest_dir": str(r / "Manifests"), "report_file": str(r / "report.csv"),
                           "cache_dir": str(r / "Cache")})
        for f in (ss.INBOX, ss.SORTED, ss.REVIEW, ss.PROCESSED):
            f.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        ss.EVENT_HOOK = None
        ss.CANCEL.clear()
        ss.apply_settings(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReviewTests(Base):
    def test_file_review_item(self):
        make_pdf(ss.REVIEW / "x_p1-2_SOA__no-name.pdf", pages=2)
        r = ss.file_review_item("x_p1-2_SOA__no-name.pdf", "SOA", " arruejo ", "10/19/2026")
        self.assertEqual(r["dest"].replace("\\", "/"), "10-19-2026/ARRUEJO/SOA.pdf")
        self.assertTrue(r["pdfa_ok"])
        self.assertEqual(len(PdfReader(str(ss.SORTED / "10-19-2026" / "ARRUEJO" / "SOA.pdf")).pages), 2)
        self.assertFalse((ss.REVIEW / "x_p1-2_SOA__no-name.pdf").exists())

    def test_existing_file_is_never_overwritten(self):
        make_pdf(ss.SORTED / "10-19-2026" / "ARRUEJO" / "SOA.pdf", pages=3)
        make_pdf(ss.REVIEW / "a_SOA.pdf", pages=1)
        r = ss.file_review_item("a_SOA.pdf", "SOA", "ARRUEJO", "10-19-2026")
        self.assertNotEqual(Path(r["dest"]).name, "SOA.pdf")
        self.assertEqual(len(PdfReader(str(ss.SORTED / "10-19-2026" / "ARRUEJO" / "SOA.pdf")).pages), 3)

    def test_lab_is_added_to_existing_lab(self):
        make_pdf(ss.SORTED / "10-19-2026" / "LLANES" / "LAB.pdf", pages=1)
        make_pdf(ss.REVIEW / "b_LAB.pdf", pages=2)
        r = ss.file_review_item("b_LAB.pdf", "LAB", "LLANES", "10-19-2026")
        self.assertTrue(r["merged"])
        merged = ss.SORTED / "10-19-2026" / "LLANES" / "LAB.pdf"
        self.assertEqual(len(PdfReader(str(merged)).pages), 3)
        self.assertIn("xmp", ss.pdfa_markers(PdfReader(str(merged)))[1])
        self.assertFalse((ss.REVIEW / "b_LAB.pdf").exists())

    def test_bad_input_is_refused_and_file_stays(self):
        make_pdf(ss.REVIEW / "c.pdf")
        for doc, name, date in (("SOA", "", "10-19-2026"), ("SOA", "12345", "10-19-2026"),
                                ("SOA", "../EVIL", "10-19-2026"), ("SOA", "ARRUEJO", "13-45-2026"),
                                ("SOA", "ARRUEJO", "10-19-1999"), ("XYZ", "ARRUEJO", "10-19-2026")):
            with self.assertRaises(ValueError, msg=(doc, name, date)):
                ss.file_review_item("c.pdf", doc, name, date)
        self.assertTrue((ss.REVIEW / "c.pdf").exists())

    def test_path_tricks_are_refused(self):
        make_pdf(self.tmp / "outside.pdf")
        with self.assertRaises(FileNotFoundError):
            ss.file_review_item("../outside.pdf", "SOA", "ARRUEJO", "10-19-2026")
        self.assertTrue((self.tmp / "outside.pdf").exists())

    def test_dismiss_keeps_the_file(self):
        make_pdf(ss.REVIEW / "d.pdf")
        ss.dismiss_review_item("d.pdf")
        self.assertTrue((ss.REVIEW / "Dismissed" / "d.pdf").exists())
        self.assertFalse((ss.REVIEW / "d.pdf").exists())

    def test_queue_lists_reason_then_marks_done(self):
        f = make_pdf(ss.REVIEW / "e_SOA__no-name.pdf")
        plan = [{"kind": "review", "dest": f, "pages": [3, 4], "doc": "SOA", "reason": "last name not found"}]
        ss.queue_review_items(Path("batch.pdf"), plan)
        items = ss.list_review_items()
        self.assertEqual([i["reason"] for i in items], ["last name not found"])
        ss.file_review_item(f.name, "SOA", "ARRUEJO", "10-19-2026")
        self.assertEqual(ss.list_review_items(), [])
        self.assertEqual(ss.read_review_queue()[0]["status"], "filed")

    def test_old_review_files_are_listed(self):
        make_pdf(ss.REVIEW / "old_p1_CF2.pdf")
        items = ss.list_review_items()
        self.assertEqual(items[0]["doc"], "CF2")

    def test_reason_slug(self):
        self.assertEqual(ss.reason_slug(["last name not found", "date not found"]), "no-name+no-date")
        self.assertEqual(ss.reason_slug(["5 pages is longer than a SOA should be"]), "too-long")
        self.assertEqual(ss.reason_slug([]), "check")


class TransferTests(Base):
    def setUp(self):
        super().setUp()
        make_pdf(ss.SORTED / "10-19-2026" / "ARRUEJO" / "CF2.pdf", pages=2)
        make_pdf(ss.SORTED / "10-19-2026" / "ARRUEJO" / "SOA.pdf", pages=3)
        self.dest = self.tmp / "Elsewhere"
        self.dest.mkdir()
        self.rel = "10-19-2026/ARRUEJO/CF2.pdf"

    def test_copy_keeps_original(self):
        r = ss.transfer_files([self.rel], self.dest)
        self.assertEqual(r[0]["status"], "copied")
        self.assertTrue((self.dest / "10-19-2026" / "ARRUEJO" / "CF2.pdf").exists())
        self.assertTrue((ss.SORTED / self.rel).exists())

    def test_move_removes_original_and_tidies_empty_folders(self):
        r = ss.transfer_files([self.rel, "10-19-2026/ARRUEJO/SOA.pdf"], self.dest, move=True)
        self.assertEqual([x["status"] for x in r], ["moved", "moved"])
        self.assertFalse((ss.SORTED / "10-19-2026").exists())
        self.assertEqual(len(PdfReader(str(self.dest / "10-19-2026" / "ARRUEJO" / "SOA.pdf")).pages), 3)

    def test_never_overwrites(self):
        (self.dest / "10-19-2026" / "ARRUEJO").mkdir(parents=True)
        (self.dest / "10-19-2026" / "ARRUEJO" / "CF2.pdf").write_bytes(b"different")
        r = ss.transfer_files([self.rel], self.dest, move=True)
        self.assertEqual(r[0]["status"], "skipped")
        self.assertEqual((self.dest / "10-19-2026" / "ARRUEJO" / "CF2.pdf").read_bytes(), b"different")
        self.assertTrue((ss.SORTED / self.rel).exists())

    def test_flat_names(self):
        r = ss.transfer_files([self.rel], self.dest, keep_structure=False)
        self.assertEqual(r[0]["status"], "copied")
        self.assertTrue((self.dest / "10-19-2026_ARRUEJO_CF2.pdf").exists())

    def test_destination_inside_sorted_is_refused(self):
        with self.assertRaises(ValueError):
            ss.transfer_files([self.rel], ss.SORTED / "10-19-2026")
        with self.assertRaises(ValueError):
            ss.transfer_files([self.rel], self.tmp / "does-not-exist")

    def test_paths_outside_sorted_are_refused(self):
        make_pdf(self.tmp / "secret.pdf")
        r = ss.transfer_files(["../secret.pdf", "..\\secret.pdf", "10-19-2026/ARRUEJO/missing.pdf"], self.dest, move=True)
        self.assertEqual({x["status"] for x in r}, {"error"})
        self.assertTrue((self.tmp / "secret.pdf").exists())

    def test_tree(self):
        tree = ss.list_sorted_tree()
        self.assertEqual(tree[0]["date"], "10-19-2026")
        self.assertEqual([f["file"] for f in tree[0]["patients"][0]["files"]], ["CF2.pdf", "SOA.pdf"])


class SameSurnameTests(unittest.TestCase):
    def seg(self, first, name="CRUZ", date="10-19-2026"):
        return {"name": name, "date": date, "first": first}

    def test_two_people_flagged(self):
        segs = [self.seg("JUAN"), self.seg("MARIA"), self.seg("JUAN")]
        ss.flag_same_surname(segs)
        self.assertTrue(all(s.get("same_surname") for s in segs))

    def test_ocr_slip_is_not_flagged(self):
        segs = [self.seg("MARITES"), self.seg("MARITE")]
        ss.flag_same_surname(segs)
        self.assertFalse(any(s.get("same_surname") for s in segs))

    def test_other_day_or_name_is_not_flagged(self):
        segs = [self.seg("JUAN"), self.seg("MARIA", date="10-20-2026"), self.seg("PEDRO", name="SANTOS")]
        ss.flag_same_surname(segs)
        self.assertFalse(any(s.get("same_surname") for s in segs))

    def test_missing_first_name_is_not_flagged(self):
        segs = [self.seg("JUAN"), self.seg(None)]
        ss.flag_same_surname(segs)
        self.assertFalse(any(s.get("same_surname") for s in segs))

    def test_first_name_reading(self):
        self.assertEqual(ss.first_name_of("SOA", "Patient Name: LLANES, MARITES R. x"), "MARITES")


def seg(doc, name=None, date=None, readings=None, pages=(0,)):
    return {"doc": doc, "name": name, "date": date, "readings": readings or [], "pages": list(pages)}


class GroupTests(unittest.TestCase):
    def patient(self, name, date="10-19-2026", hda=None, readings=None, lab=True):
        out = [seg("CF2", name, None), seg("SOA", name, date), seg("CSF", name, None), seg("HDA", hda, date, readings)]
        return out + ([seg("LAB", None, None)] if lab else [])

    def test_groups_follow_the_order(self):
        segs = self.patient("ARCE") + self.patient("ALTURA")
        self.assertEqual(ss.group_segments(segs), 2)
        self.assertEqual([s["group"] for s in segs], [0] * 5 + [1] * 5)

    def test_unreadable_hda_and_lab_take_the_patients_name(self):
        segs = self.patient("ARCE") + self.patient("ALTURA")
        ss.resolve_group_identities(segs)
        self.assertEqual([s["name"] for s in segs], ["ARCE"] * 5 + ["ALTURA"] * 5)
        self.assertTrue(all(s["date"] == "10-19-2026" for s in segs))

    def test_ocr_slip_takes_the_group_spelling(self):
        segs = [seg("CF2", "ALTURA"), seg("SOA", "ALTURA", "10-19-2026"), seg("CSF", "ALTUBA"), seg("HDA")]
        ss.resolve_group_identities(segs)
        self.assertEqual([s["name"] for s in segs], ["ALTURA"] * 4)

    def test_signature_that_contradicts_is_not_trusted(self):
        segs = self.patient("PEDRONAN", readings=["SIPIN", "SIPIN", "PEDRO"]) + self.patient("SIPIN")
        ss.resolve_group_identities(segs)
        hda = segs[3]
        self.assertEqual((hda["name"], hda.get("conflict")), ("SIPIN", "PEDRONAN"))

    def test_garbled_signature_does_not_block(self):
        segs = self.patient("PEDRONAN", readings=["NTHE", "XQZV", "ARBEVO"]) + self.patient("SIPIN")
        ss.resolve_group_identities(segs)
        self.assertEqual(segs[3]["name"], "PEDRONAN")

    def test_hda_with_a_different_name_goes_to_review(self):
        segs = self.patient("PEDRONAN", hda="SIPIN") + self.patient("SIPIN")
        ss.resolve_group_identities(segs)
        self.assertEqual(segs[3].get("conflict"), "PEDRONAN")

    def test_hdas_in_a_row_are_not_guessed(self):
        segs = [seg("CF2", "ARCE"), seg("SOA", "ARCE"), seg("CSF", "ARCE"), seg("HDA"), seg("HDA"), seg("HDA")]
        ss.resolve_group_identities(segs)
        self.assertIsNone(segs[3]["name"])

    def test_two_different_names_in_a_group_is_not_guessed(self):
        segs = [seg("CF2", "ARCE"), seg("SOA", "MENDOZA"), seg("HDA")]
        ss.resolve_group_identities(segs)
        self.assertIsNone(segs[2]["name"])

    def test_switch_off(self):
        old = ss.USE_POSITION
        ss.USE_POSITION = 0
        try:
            segs = self.patient("ARCE")
            ss.resolve_group_identities(segs)
            self.assertIsNone(segs[3]["name"])
        finally:
            ss.USE_POSITION = old

    def test_choose_tolerates_ocr_slips(self):
        self.assertEqual(ss.choose(["LLANES", "LLANE5", "LANES"]), "LLANES")
        self.assertIsNone(ss.choose(["ARCE", "MENDOZA"]))
        self.assertEqual(ss.choose(["RRUEJO", "ARRUEJO"]), "ARRUEJO")


class SettingsAndHousekeeping(Base):
    def test_apply_and_save(self):
        self.assertEqual(ss.apply_settings({"workers": "4", "log_max_mb": 5}), [])
        self.assertEqual((ss.WORKERS, ss.LOG_MAX_MB), (4, 5))
        problems = ss.apply_settings({"workers": "99", "sorted": "", "keep_cache_days": "abc", "nonsense": 1})
        self.assertEqual(len(problems), 3)
        self.assertEqual(ss.WORKERS, 4)                  # bad values changed nothing
        path = self.tmp / "settings.json"
        ss.save_settings({"workers": 3}, path)
        self.assertEqual(ss.load_settings(path)["workers"], 3)

    def test_broken_settings_file_gives_defaults(self):
        p = self.tmp / "s.json"
        p.write_text("{ not json", encoding="utf-8")
        self.assertEqual(ss.load_settings(p), {})
        self.assertEqual(ss.load_settings(self.tmp / "missing.json"), {})

    def test_housekeeping(self):
        old = time.time() - 40 * 86400
        ss.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        files = {"old": ss.CACHE_DIR / "old.json", "new": ss.CACHE_DIR / "new.json",
                 "dbg_old": ss.LOG_FILE.parent / "debug_old.txt", "dbg_new": ss.LOG_FILE.parent / "debug_new.txt"}
        for f in files.values():
            f.write_text("x")
        os.utime(files["old"], (old, old))
        os.utime(files["dbg_old"], (old, old))
        ss.LOG_FILE.write_bytes(b"x" * (ss.LOG_MAX_MB * 1024 * 1024 + 10))
        ss.housekeeping()
        self.assertFalse(files["old"].exists())
        self.assertFalse(files["dbg_old"].exists())
        self.assertTrue(files["new"].exists() and files["dbg_new"].exists())
        self.assertTrue(ss.LOG_FILE.with_name("log.1.txt").exists())


class ProcessFlow(Base):
    """A batch with page texts we control (no OCR needed)."""

    def run_batch(self, texts, dry=False):
        src = make_pdf(ss.INBOX / "batch.pdf", pages=len(texts))
        reader = PdfReader(str(src))
        real = ss.read_all_pages
        ss.read_all_pages = lambda p: (reader, list(texts))
        events = []
        ss.EVENT_HOOK = lambda kind, data: events.append((kind, data))
        try:
            ok = ss.process(src, dry)
        finally:
            ss.read_all_pages = real
        return ok, events, src

    def test_ignored_pages_only(self):
        ok, events, src = self.run_batch(["PhilHealth Benefit Eligibility Form PBEF", "blank"], dry=True)
        kinds = [k for k, _ in events]
        self.assertIn("batch_start", kinds)
        self.assertEqual(kinds[-1], "batch_end")
        self.assertTrue(src.exists())               # a dry run never moves the original

    def patient_pages(self, last, first, day="10/19/2026"):
        return [f"CLAIM FORM 2 patient confinement information\n1. Name of Patient: {last} {first} X\n{day} {day}",
                f"STATEMENT OF ACCOUNT itemized charges\nPatient Name: {last}, {first} X Date and Time Admitted: {day} {day}",
                f"CLAIM SIGNATURE FORM member and patient information\nName of Patient:\n{last}",
                "PHILHEALTH HEMODIALYSIS BENEFITS PACKAGE AGREEMENT FORM annex a",
                f"CLINICAL LABORATORY REPORT hematology\nName: {last}, {first} X Room: OPD Date: {day}"]

    def test_unreadable_hda_is_named_from_its_patient(self):
        texts = self.patient_pages("LLANES", "MARITE") + self.patient_pages("ALTURA", "JUAN")
        real = ss.name_hda_readings
        ss.name_hda_readings = lambda path, pages: ["NTHE", "XQ"]          # the signature could not be read
        try:
            ok, events, src = self.run_batch(texts)
        finally:
            ss.name_hda_readings = real
        self.assertTrue(ok)
        for who in ("LLANES", "ALTURA"):
            got = sorted(f.name for f in (ss.SORTED / "10-19-2026" / who).glob("*.pdf"))
            self.assertEqual(got, ["CF2.pdf", "CSF.pdf", "HDA.pdf", "LAB.pdf", "SOA.pdf"], who)
        self.assertEqual(list(ss.REVIEW.glob("*.pdf")), [])

    def test_hda_signed_by_someone_else_goes_to_review(self):
        texts = self.patient_pages("LLANES", "MARITE") + self.patient_pages("ALTURA", "JUAN")
        real = ss.name_hda_readings
        ss.name_hda_readings = lambda path, pages: ["ALTURA", "ALTURA"] if pages[0] < 8 else ["ALTURA", "ALTURA"]
        try:
            ok, events, src = self.run_batch(texts)
        finally:
            ss.name_hda_readings = real
        self.assertTrue(ok)
        self.assertEqual(len(list(ss.REVIEW.glob("*HDA*.pdf"))), 1)       # LLANES' HDA read as ALTURA twice
        self.assertTrue((ss.SORTED / "10-19-2026" / "ALTURA" / "HDA.pdf").exists())

    def test_cancel_leaves_original_in_inbox(self):
        def hook(kind, data):
            if kind == "stage" and data["key"] == "split":
                ss.CANCEL.set()
        src = make_pdf(ss.INBOX / "batch.pdf", pages=2)
        reader = PdfReader(str(src))
        real = ss.read_all_pages
        ss.read_all_pages = lambda p: (reader, ["x", "y"])
        ss.EVENT_HOOK = hook
        try:
            ok = ss.process(src, False)
        finally:
            ss.read_all_pages = real
        self.assertFalse(ok)
        self.assertTrue(src.exists())
        self.assertFalse(ss.CANCEL.is_set())
        self.assertEqual(list(ss.SORTED.rglob("*.pdf")), [])


# ---------------------------------------------------------------- the window (web UI)
class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.server, self.runner = ui.make_server(0, opener=lambda p: self.opened.append(str(p)))
        self.opened = []
        self.port = self.server.server_address[1]
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.runner.quit.set()
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, token=True, host=None, raw=None):
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-Token"] = self.server.token
        h.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        c.close()
        try:
            return r.status, json.loads(payload)
        except ValueError:
            return r.status, payload

    def test_security(self):
        self.assertEqual(self.call("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.call("GET", "/api/state", host="evil.example.com")[0], 403)
        self.assertEqual(self.call("POST", "/api/stop", body={}, headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        self.assertEqual(self.call("GET", f"/api/state?t={self.server.token}", token=False)[0], 200)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)

    def test_page_is_served(self):
        st, body = self.call("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Scan Sorter", body)

    def test_upload_checks(self):
        self.runner.watch = False
        st, _ = self.call("POST", "/api/upload?name=notes.txt", raw=b"%PDF hello")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/upload?name=fake.pdf", raw=b"this is not a pdf at all")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/upload?name=empty.pdf", raw=b"")
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/api/upload?name=..%2F..%2Fescape.pdf", raw=b"%PDF-1.4 x")
        self.assertEqual(st, 200)
        self.assertEqual(list(ss.INBOX.glob("*.uploading")), [])
        self.assertFalse((self.tmp / "escape.pdf").exists())
        self.assertTrue((ss.INBOX / "escape.pdf").exists())

    def test_upload_is_queued_and_sorted(self):
        src = make_pdf(self.tmp / "scan.pdf", pages=1)
        real = ss.read_all_pages
        ss.read_all_pages = lambda p: (PdfReader(str(p)), ["PhilHealth Benefit Eligibility Form PBEF"])
        try:
            st, body = self.call("POST", "/api/upload?name=scan.pdf", raw=src.read_bytes())
            self.assertEqual(st, 200)
            for _ in range(100):
                s = self.call("GET", "/api/state?since=0")[1]
                if s["items"] and s["items"][0]["status"] not in ("waiting", "processing"):
                    break
                time.sleep(0.1)
        finally:
            ss.read_all_pages = real
        self.assertIn(s["items"][0]["status"], ("done", "attention"))
        self.assertEqual(len(s["stages"]), 6)
        self.assertTrue(src.exists())                # the dragged original is untouched

    def test_watch_picks_up_inbox_files(self):
        real = ss.read_all_pages
        ss.read_all_pages = lambda p: (PdfReader(str(p)), ["PhilHealth Benefit Eligibility Form PBEF"])
        try:
            make_pdf(ss.INBOX / "naps2.pdf")
            for _ in range(100):
                s = self.call("GET", "/api/state?since=0")[1]
                if s["items"] and s["items"][0]["status"] in ("done", "attention", "failed"):
                    break
                time.sleep(0.1)
        finally:
            ss.read_all_pages = real
        self.assertEqual(s["items"][0]["name"], "naps2.pdf")

    def test_review_api(self):
        make_pdf(ss.REVIEW / "r_SOA__no-name.pdf", pages=2)
        st, d = self.call("GET", "/api/review")
        self.assertEqual((st, d["items"][0]["page_count"]), (200, 2))
        st, _ = self.call("POST", "/api/review/file", {"file": "r_SOA__no-name.pdf", "doc": "SOA", "name": "", "date": "x"})
        self.assertEqual(st, 400)
        st, d = self.call("POST", "/api/review/file", {"file": "r_SOA__no-name.pdf", "doc": "SOA", "name": "Reyes", "date": "10-19-2026"})
        self.assertEqual(st, 200)
        self.assertTrue((ss.SORTED / "10-19-2026" / "REYES" / "SOA.pdf").exists())

    def test_transfer_and_folder_picker(self):
        make_pdf(ss.SORTED / "10-19-2026" / "REYES" / "SOA.pdf")
        dest = self.tmp / "Out"
        st, d = self.call("POST", "/api/transfer", {"paths": [], "dest": str(dest)})
        self.assertEqual(st, 400)
        st, d = self.call("POST", "/api/mkdir", {"path": str(self.tmp), "name": "Out"})
        self.assertEqual(st, 200)
        st, d = self.call("POST", "/api/mkdir", {"path": str(self.tmp), "name": "a/b"})
        self.assertEqual(st, 400)
        st, d = self.call("POST", "/api/transfer", {"paths": ["10-19-2026/REYES/SOA.pdf"], "dest": str(dest), "move": True})
        self.assertEqual(d["results"][0]["status"], "moved")
        st, d = self.call("GET", "/api/fs?path=" + str(self.tmp))
        self.assertIn("Out", [x["name"] for x in d["dirs"]])
        self.assertEqual(self.call("GET", "/api/fs?path=" + str(self.tmp / "nope"))[0], 400)

    def test_open_is_limited_to_known_places(self):
        make_pdf(ss.SORTED / "10-19-2026" / "REYES" / "SOA.pdf")
        self.assertEqual(self.call("POST", "/api/open", {"what": "sorted"})[0], 200)
        self.assertEqual(self.call("POST", "/api/open", {"what": "sorted_file", "rel": "10-19-2026/REYES/SOA.pdf"})[0], 200)
        self.assertEqual(self.call("POST", "/api/open", {"what": "sorted_file", "rel": "../../etc/passwd"})[0], 400)
        self.assertEqual(self.call("POST", "/api/open", {"what": "cmd.exe"})[0], 400)
        self.assertEqual(len(self.opened), 2)

    def test_pick_pdfs_anywhere(self):
        self.runner.watch = False
        a = make_pdf(self.tmp / "Docs" / "one.pdf")
        (self.tmp / "Docs" / "note.txt").write_text("hi")
        (self.tmp / "Docs" / "fake.pdf").write_bytes(b"not a pdf")
        st, d = self.call("GET", "/api/fs?files=1&path=" + str(self.tmp / "Docs"))
        self.assertEqual(sorted(f["name"] for f in d["files"]), ["fake.pdf", "one.pdf"])
        st, d = self.call("POST", "/api/addfiles", {"paths": [str(a), str(self.tmp / "Docs" / "fake.pdf"),
                                                             str(self.tmp / "Docs" / "note.txt"), str(self.tmp / "nope.pdf")]})
        self.assertEqual([r["status"] for r in d["results"]], ["queued", "error", "error", "error"])
        self.assertTrue(a.exists())                       # original untouched
        self.assertTrue((ss.INBOX / "one.pdf").exists() or any(i["name"].startswith("one") for i in self.call("GET", "/api/state")[1]["items"]))
        self.assertEqual(self.call("POST", "/api/addfiles", {"paths": []})[0], 400)
        make_pdf(ss.SORTED / "10-19-2026" / "X" / "SOA.pdf")
        st, d = self.call("POST", "/api/addfiles", {"paths": [str(ss.SORTED / "10-19-2026" / "X" / "SOA.pdf")]})
        self.assertEqual(d["results"][0]["status"], "error")

    def test_settings_api(self):
        st, d = self.call("GET", "/api/settings")
        self.assertEqual(st, 200)
        self.assertIn("workers", d["values"])
        before = ss.WORKERS
        st, d = self.call("POST", "/api/settings", {"values": {"workers": "banana"}})
        self.assertEqual(st, 400)
        self.assertEqual(ss.WORKERS, before)


if __name__ == "__main__":
    unittest.main()