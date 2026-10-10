"""Real HTTP integration tests, using the same fenghua runtime as the app."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import pymupdf
from paper_translator.web import Workbench


def sample_pdf(encrypted=False):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((50, 60), "A sample research paper", fontsize=20)
        page.insert_text((50, 110), "This document tests text extraction and image preview.")
        page.insert_text((50, 150), "y = x + 2")
        pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 40, 40), False)
        pix.clear_with(120)
        page.insert_image(pymupdf.Rect(50, 180, 150, 280), stream=pix.tobytes("png"))
        doc.new_page().insert_text((50, 70), "Second page of the sample document.")
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": "test", "owner_pw": "owner"} if encrypted else {}
        return doc.tobytes(**options)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.unavailable = socket.socket()
        self.unavailable.bind(("127.0.0.1", 0))
        self.environment = patch.dict(os.environ, {"MINERU_BASE_URL": f"http://127.0.0.1:{self.unavailable.getsockname()[1]}"})
        self.environment.start()
        self.server = Workbench(("127.0.0.1", 0), self.directory.name)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server.executor.shutdown(wait=True)
        self.environment.stop()
        self.unavailable.close()
        self.directory.cleanup()

    def request(self, path, body=None, **headers):
        if body is not None:
            headers = {"X-Paper-Workbench": "1", **headers}
        try:
            response = urlopen(Request(self.base + path, data=body, headers=headers), timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def upload(self, engine="pymupdf"):
        status, _, body = self.request("/api/jobs", sample_pdf(), **{"X-Filename": "sample.pdf", "X-Engine": engine})
        self.assertEqual(status, 202, body)
        return json.loads(body)["id"]

    def completed(self, job_id):
        for _ in range(100):
            job = json.loads(self.request("/api/jobs/" + job_id)[2])
            if job["status"] in {"completed", "failed"}:
                return job
            time.sleep(.1)
        self.fail("Parsing did not finish")

    def test_upload_parse_preview_assets_download_and_retry(self):
        job_id = self.upload()
        job = self.completed(job_id)
        self.assertEqual(job["status"], "completed", job)
        status, headers, body = self.request(f"/api/jobs/{job_id}/result?download=1")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        result = json.loads(body)
        self.assertEqual(result["source"]["filename"], "sample.pdf")
        self.assertEqual(len(result["pages"]), 2)
        self.assertTrue(any("sample research" in b["original"] for b in result["blocks"]))
        self.assertTrue(any(b["type"] == "equation" for b in result["blocks"]))
        self.assertEqual(len(result["assets"]), 1)
        for suffix in ("pages/1", result["assets"][0]["asset"]):
            status, _, image = self.request(f"/api/jobs/{job_id}/{suffix}")
            self.assertEqual(status, 200)
            self.assertTrue(image.startswith(b"\x89PNG"))
        self.assertEqual(self.request(f"/api/jobs/{job_id}/pages/3")[0], 404)
        self.assertTrue(self.request(f"/api/jobs/{job_id}/source")[2].startswith(b"%PDF"))
        self.assertEqual(self.request(f"/api/jobs/{job_id}/retry", b"")[0], 202)
        retried = self.completed(job_id)
        self.assertEqual(retried["status"], "completed")
        self.assertNotEqual(retried["result_dir"], job["result_dir"])

    def test_bad_pdf_encryption_and_filename(self):
        for data, name in [(b"not a PDF", "bad.pdf"), (sample_pdf(True), "locked.pdf"), (sample_pdf(), "bad.txt")]:
            status, _, body = self.request("/api/jobs", data, **{"X-Filename": name})
            self.assertEqual(status, 400, body)
        self.assertEqual(json.loads(self.request("/api/jobs")[2]), [])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_fallback_and_explicit_ocr_failure(self):
        fallback = self.completed(self.upload("auto"))
        self.assertEqual(fallback["status"], "completed")
        self.assertEqual(fallback["actual_engine"], "pymupdf")
        self.assertTrue(any(w["code"] == "MINERU_FALLBACK" for w in fallback["warnings"]))
        failed = self.completed(self.upload("mineru"))
        self.assertEqual(failed["status"], "failed")
        self.assertIn("MinerU", failed["error"])

    def test_history_recovery(self):
        job_id = self.upload()
        self.assertEqual(self.completed(job_id)["status"], "completed")
        loaded = Workbench(("127.0.0.1", 0), self.directory.name)
        try:
            self.assertEqual(loaded.jobs[job_id]["status"], "completed")
            loaded.update(job_id, status="running")
        finally:
            loaded.server_close()
            loaded.executor.shutdown()
        recovered = Workbench(("127.0.0.1", 0), self.directory.name)
        try:
            self.assertEqual(recovered.jobs[job_id]["status"], "failed")
            self.assertIn("重新解析", recovered.jobs[job_id]["error"])
        finally:
            recovered.server_close()
            recovered.executor.shutdown()

    def test_origin_assets_and_static_files(self):
        for path in ("/", "/app.js", "/style.css", "/api/health"):
            self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request("/api/jobs", Origin="https://example.com")[0], 403)
        job_id = self.upload()
        self.completed(job_id)
        self.assertEqual(self.request(f"/api/jobs/{job_id}/assets/%2e%2e/document.json")[0], 404)

    def test_delete_restore_preserves_files_and_survives_restart(self):
        job_id = self.upload()
        job = self.completed(job_id)
        root = Path(self.directory.name) / job_id
        result = root / job['result_dir'] / 'document.json'
        before = result.read_bytes()
        source = (root / 'source.pdf').read_bytes()
        self.assertEqual(self.request(f'/api/jobs/{job_id}/delete', b'{}')[0], 200)
        self.assertEqual(json.loads(self.request('/api/jobs')[2]), [])
        recycled = json.loads(self.request('/api/jobs?deleted=1')[2])
        self.assertEqual([item['id'] for item in recycled], [job_id])
        self.assertTrue(recycled[0]['deleted_at'])
        for suffix in ('', '/source', '/pages/1', '/result', '/translated.pdf', '/latex-source.zip'):
            self.assertEqual(self.request(f'/api/jobs/{job_id}{suffix}')[0], 404)
        for action in ('retry', 'translate', 'pdf'):
            self.assertEqual(self.request(f'/api/jobs/{job_id}/{action}', b'{}')[0], 404)
        self.assertEqual(result.read_bytes(), before)
        self.assertEqual((root / 'source.pdf').read_bytes(), source)
        loaded = Workbench(('127.0.0.1', 0), self.directory.name)
        try:
            self.assertTrue(loaded.jobs[job_id]['deleted_at'])
            self.assertEqual(loaded.recycle(job_id, False)[0], 200)
            self.assertIsNone(json.loads((root / 'job.json').read_text())['deleted_at'])
        finally:
            loaded.server_close()
            loaded.executor.shutdown()
        self.assertEqual(self.request(f'/api/jobs/{job_id}/restore', b'{}')[0], 200)
        self.assertEqual(self.request(f'/api/jobs/{job_id}/result')[2], before)
        self.assertEqual(json.loads(self.request('/api/jobs?deleted=1')[2]), [])
        self.assertEqual(len(json.loads(self.request('/api/jobs')[2])), 1)

    def test_delete_protects_running_jobs_origin_and_disk_failures(self):
        job_id = self.upload()
        self.completed(job_id)
        for key in ('status', 'translation_status', 'pdf_status'):
            for status in ('running', 'queued'):
                self.server.update(job_id, **{key: status})
                self.assertEqual(self.request(f'/api/jobs/{job_id}/delete', b'{}')[0], 409)
                self.assertFalse(self.server.jobs[job_id].get('deleted_at'))
            self.server.update(job_id, **{key: 'completed'})
        self.assertEqual(self.request(f'/api/jobs/{job_id}/delete', b'{}', Origin='https://example.com')[0], 403)
        self.assertEqual(self.request(f'/api/jobs/{job_id}/delete', b'{}', **{'X-Paper-Workbench':'0'})[0], 403)
        self.assertEqual(self.request('/api/jobs/' + '0'*32 + '/delete', b'{}')[0], 404)
        with patch('paper_translator.web.atomic_json', side_effect=OSError('Disk full')):
            self.assertEqual(self.request(f'/api/jobs/{job_id}/delete', b'{}')[0], 500)
        self.assertFalse(self.server.jobs[job_id].get('deleted_at'))
        self.assertEqual(len(json.loads(self.request('/api/jobs')[2])), 1)


if __name__ == "__main__":
    unittest.main()
