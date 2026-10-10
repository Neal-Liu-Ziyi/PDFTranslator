"""Local, single-user PDF workbench. Start with bash start.sh."""
from __future__ import annotations

import argparse
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import zipfile

import pymupdf

from .mineru import MinerUClient
from .translation import (translate_document, public_config, model_config, TranslationError,
                           atomic_json, translation_items, translated_value,
                           SUPPORTED_TRANSLATION_LANGUAGES, target_language_name)
from .layout import normalize_content_roles, restore_visual_content
from .pdf_export import generate_latex, compile_pdf, compiler_path
from .quality import assess_tables
from .native_korean import recover_korean_patent

STATIC = Path(__file__).parent / "static"
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
PDF_LOCK = threading.Lock()  # PyMuPDF does not support concurrent thread use.


def now():
    return datetime.now(timezone.utc).isoformat()


class Workbench(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.lock = threading.RLock()
        self.processes = set()
        self.stopping = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-parser")
        for path in self.data_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text())
                if not IDENTIFIER.fullmatch(job["id"]) or path.parent.name != job["id"]:
                    continue
                self.jobs[job["id"]] = job
                if job.get('deleted_at'):
                    continue  # Keep recycled documents untouched until restored.
                if job["status"] in {"queued", "running"}:
                    self.update(job["id"], status="failed", error="上次服务退出时解析未完成，请点击重新解析。")
                for operation in ("translation", "pdf"):
                    if job.get(operation + "_status") in {"queued", "running"}:
                        self.update(job["id"], **{operation + "_status": "failed", operation + "_error": "上次服务退出时未完成，可继续处理。"})
                if job.get('status') == 'completed' and job.get('result_dir'):
                    result_path = path.parent / job['result_dir'] / 'document.json'
                    document = json.loads(result_path.read_text())
                    visual_changed = restore_visual_content(document, result_path.parent, path.parent / 'source.pdf')
                    native_changed = recover_korean_patent(document, path.parent / 'source.pdf')
                    roles_changed = normalize_content_roles(document)
                    quality_changed = assess_tables(document)
                    if roles_changed or quality_changed or visual_changed or native_changed:
                        updates = {'pdf_status': 'idle', 'pdf_error': None, 'pdf_warnings': []}
                        updates.update(block_count=len(document['blocks']), asset_count=len(document.get('assets', [])))
                        if 'translation' in document:
                            items, bindings = translation_items(document)
                            completed = sum(bool(translated_value(bindings[i['id']])) for i in items)
                            document['translation'].update(total=len(items), completed=completed)
                            updates.update(translation_total=len(items), translation_completed=completed)
                            if completed < len(items) and job.get('translation_status') == 'completed':
                                document['translation']['status'] = 'failed'
                                updates.update(translation_status='failed', translation_error='已校正文档内容，请点击继续翻译。未发生变化的译文已保留。')
                        atomic_json(result_path, document)
                        self.update(job['id'], **updates)
            except (ValueError, KeyError, OSError):
                continue
        super().__init__(address, Handler)

    def update(self, job_id, **changes):
        with self.lock:
            self.jobs[job_id].update(changes, updated_at=now())
            path = self.data_dir / job_id / "job.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.jobs[job_id], ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)

    def public_job(self, job):
        value = dict(job)
        if job.get('result_dir'):
            path = self.data_dir / job['id'] / job['result_dir'] / 'document.json'
            if path.exists():
                value['result_version'] = str(path.stat().st_mtime_ns)
        return value

    def busy(self, job):
        return any(job.get(key) in {'queued', 'running'} for key in ('status', 'translation_status', 'pdf_status'))

    def recycle(self, job_id, deleted):
        """Persist a reversible tombstone; keep all document files together."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return 404, {'error': '处理记录不存在。'}
            if self.busy(job):
                return 409, {'error': '该文档正在处理，请完成后再删除。'}
            updated = dict(job, deleted_at=(job.get('deleted_at') or now()) if deleted else None,
                           updated_at=now())
            atomic_json(self.data_dir / job_id / 'job.json', updated)
            self.jobs[job_id] = updated
            return 200, {'id': job_id, 'deleted': deleted}

    def run_translation(self, job_id, glossary, target_language='zh-CN'):
        self.update(job_id, translation_status='running', translation_error=None)
        job = self.jobs[job_id]
        path = self.data_dir / job_id / job['result_dir'] / 'document.json'
        try:
            def progress(meta):
                self.update(job_id, translation_completed=meta['completed'], translation_total=meta['total'], translation_notice=meta.get('notice'))
            result = translate_document(path, glossary=glossary, progress=progress,
                                        target_language=target_language)
            self.update(job_id, translation_status='completed', translation_model=result['translation']['model'],
                        target_language=target_language)
        except Exception as exc:
            self.update(job_id, translation_status='failed', translation_error=str(exc) if isinstance(exc, TranslationError) else '翻译处理失败，已保存的译文可以继续使用。')

    def run_pdf(self, job_id, layout):
        folder = self.data_dir / job_id
        attempt = 'pdf-' + uuid.uuid4().hex
        self.update(job_id, pdf_status='running', pdf_error=None, pdf_dir=attempt, pdf_layout=layout)
        try:
            document = folder / self.jobs[job_id]['result_dir'] / 'document.json'
            with PDF_LOCK:
                warnings = generate_latex(document, folder / attempt, folder / 'source.pdf', layout)
            compile_pdf(folder / attempt)
            with zipfile.ZipFile(folder / attempt / 'latex-source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in [folder / attempt / 'translated.tex', *(folder / attempt / 'assets').iterdir()]:
                    archive.write(path, path.relative_to(folder / attempt))
            self.update(job_id, pdf_status='completed', pdf_warnings=warnings)
        except Exception as exc:
            self.update(job_id, pdf_status='failed', pdf_error=str(exc))

    def run_job(self, job_id):
        self.update(job_id, status="running", error=None)
        folder = self.data_dir / job_id
        try:
            # Use a unique output folder for each attempt; retain earlier artifacts.
            attempt = "run-" + uuid.uuid4().hex
            output = folder / attempt / "document.json"
            output.parent.mkdir()
            # Isolate PyMuPDF parsing from preview rendering and upload validation.
            # A slow OCR request must not freeze the original PDF preview.
            with (output.parent / "parse.log").open("w") as log:
                with self.lock:
                    if self.stopping:
                        return
                    process = subprocess.Popen([
                        sys.executable, "-m", "paper_translator.cli",
                        str(folder / "source.pdf"), "-o", str(output),
                        "--engine", self.jobs[job_id]["requested_engine"],
                        "--ocr-language", self.jobs[job_id].get("ocr_language", "ch"),
                    ], stdout=log, stderr=log)
                    self.processes.add(process)
                try:
                    returncode = process.wait(timeout=1900)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise RuntimeError("解析超过 30 分钟，请重试或减少 PDF 页数。")
                finally:
                    with self.lock:
                        self.processes.discard(process)
            if returncode:
                detail = (output.parent / "parse.log").read_text(errors="replace")[-2000:]
                raise RuntimeError(detail.strip() or "解析进程意外退出，请重新解析。")
            result = json.loads(output.read_text(encoding="utf-8"))
            result["source"]["filename"] = self.jobs[job_id]["filename"]
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.update(job_id, status="completed", result_dir=attempt,
                        translation_status='idle', pdf_status='idle', translation_error=None, pdf_error=None,
                        actual_engine=result.get("parser", {}).get("engine"),
                        warnings=result.get("warnings", []),
                        block_count=len(result.get("blocks", [])),
                        asset_count=len(result.get("assets", [])))
        except Exception as exc:
            self.update(job_id, status="failed", error=str(exc) or type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid logging filenames from upload headers.
        pass

    def send_bytes(self, body, content_type, status=200, download=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def same_origin(self):
        host = self.headers.get("Host", "")
        allowed = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        return not origin or origin == f"http://{host}"

    def do_GET(self):
        if not self.same_origin():
            return self.send_json({"error": "仅允许本机页面访问。"}, 403)
        path = urlsplit(self.path).path
        if path in {"/", "/app.js", "/style.css"}:
            name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            return self.send_bytes((STATIC / name).read_bytes(), (mimetypes.guess_type(name)[0] or "text/plain") + "; charset=utf-8")
        if path == "/api/jobs":
            deleted = parse_qs(urlsplit(self.path).query).get('deleted') == ['1']
            with self.server.lock:
                jobs = [self.server.public_job(job) for job in self.server.jobs.values()
                        if bool(job.get('deleted_at')) == deleted]
            return self.send_json(sorted(jobs, key=lambda job: job["created_at"], reverse=True))
        if path == "/api/health":
            url = os.getenv("MINERU_BASE_URL", "http://127.0.0.1:18000")
            return self.send_json({"mineru_available": MinerUClient(url, timeout_seconds=2).available(),
                                   'translation': public_config(),
                                   'target_languages': SUPPORTED_TRANSLATION_LANGUAGES,
                                   'pdf_available': bool(compiler_path())})
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(?:/(.*))?", path)
        if not match:
            return self.send_json({"error": "未找到内容。"}, 404)
        job_id, action = match.groups()
        with self.server.lock:
            job = self.server.public_job(self.server.jobs[job_id]) if job_id in self.server.jobs else {}
        if not job or job.get('deleted_at'):
            return self.send_json({"error": "处理记录不存在。"}, 404)
        root = self.server.data_dir / job_id
        if not action:
            return self.send_json(job)
        try:
            if action == "source":
                return self.send_bytes((root / "source.pdf").read_bytes(), "application/pdf", download="source.pdf")
            if action.startswith("pages/"):
                page_number = int(action[6:])
                if page_number < 1 or page_number > job["page_count"]:
                    return self.send_json({"error": "页码超出范围。"}, 404)
                preview = root / f"page-{page_number}.png"
                with PDF_LOCK:
                    if not preview.exists():
                        with pymupdf.open(root / "source.pdf") as pdf:
                            page = pdf[page_number - 1]
                            scale = min(1.6, 1600 / max(page.rect.width, page.rect.height))
                            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(preview)
                return self.send_bytes(preview.read_bytes(), "image/png")
            if job["status"] != "completed":
                return self.send_json({"error": "解析尚未完成。"}, 409)
            result_root = root / job["result_dir"]
            if action in {'translated.pdf', 'latex-source.zip'}:
                if job.get('pdf_status') != 'completed':
                    return self.send_json({'error': '译文 PDF 尚未生成成功。'}, 409)
                file = root / job['pdf_dir'] / action
                mime = 'application/pdf' if action.endswith('.pdf') else 'application/zip'
                return self.send_bytes(file.read_bytes(), mime, download=action)
            if action == "result":
                download = "document.json" if "download" in parse_qs(urlsplit(self.path).query) else None
                return self.send_bytes((result_root / "document.json").read_bytes(), "application/json; charset=utf-8", download=download)
            if action.startswith("assets/"):
                asset = (result_root / unquote(action)).resolve()
                if not asset.is_relative_to((result_root / "assets").resolve()) or not asset.is_file():
                    return self.send_json({"error": "图片不存在。"}, 404)
                mime = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/tiff", "image/bmp"}:
                    return self.send_json({"error": "该图片格式不支持预览。"}, 415)
                return self.send_bytes(asset.read_bytes(), mime)
        except (OSError, ValueError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        self.send_json({"error": "未找到内容。"}, 404)

    def do_POST(self):
        if not self.same_origin() or self.headers.get("X-Paper-Workbench") != "1":
            return self.send_json({"error": "请从本机解析工作台提交。"}, 403)
        path = urlsplit(self.path).path
        recycle = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/(delete|restore)', path)
        if recycle:
            try:
                status, result = self.server.recycle(recycle[1], recycle[2] == 'delete')
                return self.send_json(result, status)
            except OSError:
                return self.send_json({'error': '无法保存记录变更，请检查本地磁盘后重试。'}, 500)
        operation = re.fullmatch(r'/api/jobs/([0-9a-f]{32})/(translate|pdf)', path)
        if operation:
            job_id, action = operation.groups()
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 16384:
                    raise ValueError('请求参数过长。')
                payload = json.loads(self.rfile.read(size) or b'{}')
                if not isinstance(payload, dict):
                    raise ValueError('请求参数格式错误。')
                glossary = payload.get('glossary', '')
                if not isinstance(glossary, str) or len(glossary) > 4000:
                    raise ValueError('术语表请控制在 4000 字以内。')
                target_language = payload.get('target_language', 'zh-CN')
                if not isinstance(target_language, str):
                    raise ValueError('请选择有效的目标语言。')
                target_language_name(target_language)
                layout = payload.get('layout', 'single')
                if layout not in {'single', 'double'}:
                    raise ValueError('请选择单栏或双栏。')
                with self.server.lock:
                    job = self.server.jobs.get(job_id)
                    if job and job.get('deleted_at'):
                        return self.send_json({'error': '该记录已删除，请先从回收站恢复。'}, 404)
                    if not job or job['status'] != 'completed':
                        return self.send_json({'error': '请先完成 PDF 解析。'}, 409)
                    if self.server.busy(job):
                        return self.send_json({'error': '该文档正在处理，请等待完成。'}, 409)
                    if action == 'translate':
                        model_config()
                        self.server.update(job_id, translation_status='queued', translation_error=None,
                                           glossary=glossary, target_language=target_language, pdf_status='idle')
                        self.server.executor.submit(self.server.run_translation, job_id, glossary, target_language)
                    else:
                        if job.get('translation_status') != 'completed':
                            return self.send_json({'error': '请先完成全文翻译。'}, 409)
                        if not compiler_path():
                            raise ValueError('未找到 XeLaTeX，请安装 TeX Live / MacTeX。')
                        self.server.update(job_id, pdf_status='queued', pdf_error=None)
                        self.server.executor.submit(self.server.run_pdf, job_id, layout)
                return self.send_json({'id': job_id}, 202)
            except (ValueError, TranslationError) as exc:
                return self.send_json({'error': str(exc)}, 400)
        retry = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/retry", path)
        if retry:
            job_id = retry[1]
            with self.server.lock:
                job = self.server.jobs.get(job_id)
                if not job or job.get('deleted_at'):
                    return self.send_json({"error": "处理记录不存在。"}, 404)
                if self.server.busy(job):
                    return self.send_json({"error": "该文件正在处理。"}, 409)
                self.server.update(job_id, status="queued", error=None)
            self.server.executor.submit(self.server.run_job, job_id)
            return self.send_json({"id": job_id}, 202)
        if path != "/api/jobs":
            return self.send_json({"error": "未找到接口。"}, 404)
        folder = None
        try:
            engine = self.headers.get("X-Engine", "auto")
            if engine not in {"auto", "mineru", "pymupdf"}:
                raise ValueError("请选择有效的解析方式。")
            language = self.headers.get("X-OCR-Language", "ch")
            if language not in {"ch", "korean"}:
                raise ValueError("请选择有效的论文语言。")
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0:
                raise ValueError("文件为空，请选择 PDF。")
            filename = Path(unquote(self.headers.get("X-Filename", "document.pdf")).replace("\\", "/")).name
            if not filename.lower().endswith(".pdf"):
                raise ValueError("请上传 PDF 文件。")
            job_id = uuid.uuid4().hex
            folder = self.server.data_dir / job_id
            folder.mkdir()
            self.connection.settimeout(120)
            with (folder / "source.pdf").open("wb") as stream:
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("上传中断，请重新上传。")
                    stream.write(chunk)
                    remaining -= len(chunk)
            with PDF_LOCK, pymupdf.open(folder / "source.pdf") as pdf:
                if not pdf.is_pdf:
                    raise ValueError("文件内容不是 PDF。")
                if pdf.needs_pass:
                    raise ValueError("PDF 已加密，请先解除密码后上传。")
                if len(pdf) == 0:
                    raise ValueError("PDF 没有页面。")
                page_count = len(pdf)
            with self.server.lock:
                self.server.jobs[job_id] = {"id": job_id, "filename": filename, "size": size,
                    "page_count": page_count, "requested_engine": engine, "ocr_language": language,
                    "status": "queued", "created_at": now()}
                self.server.update(job_id)
            self.server.executor.submit(self.server.run_job, job_id)
            self.send_json({"id": job_id}, 202)
        except Exception as exc:
            if folder and folder.exists() and folder.name not in self.server.jobs:
                # Only remove this request's incomplete, newly created upload.
                shutil.rmtree(folder)
            self.send_json({"error": str(exc) or "上传失败，请重新选择 PDF。"}, 400)


def replace_previous_server(port, module="paper_translator.web"):
    """Release the selected port only when owned by this project's workbench."""
    if not shutil.which("lsof"):
        raise RuntimeError("找不到 lsof，无法检查旧服务占用的端口。")

    def listeners():
        result = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise RuntimeError("无法检查端口占用：" + result.stderr.strip())
        return {int(pid) for pid in result.stdout.split() if pid.isdigit()}

    pids = listeners()
    project_dir = Path(__file__).resolve().parents[2]
    for pid in pids:
        command = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                 capture_output=True, text=True).stdout
        cwd_info = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                  capture_output=True, text=True).stdout
        same_project = any(line.startswith("n") and Path(line[1:]).resolve() == project_dir
                           for line in cwd_info.splitlines())
        if not same_project or not re.search(r"(?:^|\s)-m\s+" + re.escape(module) + r"(?:\s|$)", command):
            raise RuntimeError(f"端口 {port} 被其他程序占用（PID {pid}），未停止该程序。请更换 PORT 或手动关闭它。")
    for pid in pids:
        print(f"正在停止旧工作台（PID {pid}），释放端口 {port}…", flush=True)
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    if pids:
        deadline = time.monotonic() + 10
        while listeners():
            if time.monotonic() >= deadline:
                raise RuntimeError(f"旧服务未能及时释放端口 {port}，请检查旧服务终端后重试。")
            time.sleep(.1)
        print(f"端口 {port} 已释放，正在启动…", flush=True)


def stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process and process.stdin:
        process.stdin.close()


def start_mineru(data_dir):
    if os.getenv("MINERU_BASE_URL"):
        if not MinerUClient(os.environ["MINERU_BASE_URL"], timeout_seconds=3).available():
            raise RuntimeError("指定的 MINERU_BASE_URL 无法连接，请检查 OCR 服务。")
        print("已连接指定的 MinerU 服务。", flush=True)
        return None
    if importlib.util.find_spec("mineru") is None:
        raise RuntimeError("当前 Python 环境尚未安装 MinerU，请安装 requirements-ocr.txt。")
    port = int(os.getenv("MINERU_PORT", "18000"))
    if not 1 <= port <= 65535:
        raise RuntimeError("MINERU_PORT 必须在 1 到 65535 之间。")
    replace_previous_server(port, "mineru.cli.fast_api")
    project = Path(__file__).resolve().parents[2]
    config = project / ".mineru.json"
    if not config.exists() and not os.getenv("MINERU_TOOLS_CONFIG_JSON"):
        raise RuntimeError("缺少 MinerU 模型配置，请先按照 README 下载模型。")
    env = dict(os.environ)
    env.setdefault("MINERU_TOOLS_CONFIG_JSON", str(config))
    env.setdefault("MINERU_MODEL_SOURCE", "local")
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    env["MINERU_API_SHUTDOWN_ON_STDIN_EOF"] = "1"
    env["MINERU_API_DISABLE_ACCESS_LOG"] = "1"
    env["MINERU_API_OUTPUT_ROOT"] = str(data_dir / "mineru-output")
    log_path = data_dir / "mineru-service.log"
    with log_path.open("a") as log:
        process = subprocess.Popen([sys.executable, "-m", "mineru.cli.fast_api",
                                    "--host", "127.0.0.1", "--port", str(port)],
                                   env=env, cwd=project, stdin=subprocess.PIPE,
                                   stdout=log, stderr=log, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    print(f"正在启动 MinerU OCR 服务（{url}）…", flush=True)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"MinerU 启动失败，请查看 {log_path}")
            if MinerUClient(url, timeout_seconds=2).available():
                os.environ["MINERU_BASE_URL"] = url
                print("MinerU 已连接，网页可以进行 OCR、公式和版面解析。", flush=True)
                return process
            time.sleep(.5)
        raise RuntimeError(f"MinerU 启动超时，请查看 {log_path}")
    except BaseException:
        stop_process(process)
        raise


def main():
    parser = argparse.ArgumentParser(description="启动本地 PDF 解析工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--replace", action="store_true", help="启动前停止本项目占用该端口的旧服务")
    parser.add_argument("--with-mineru", action="store_true", help="同时启动本地 MinerU OCR 服务")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间。")
    server = None
    mineru_process = None
    try:
        if args.replace:
            replace_previous_server(args.port)
        server = Workbench(("127.0.0.1", args.port), args.data_dir)
        if args.with_mineru:
            mineru_process = start_mineru(server.data_dir)
    except (OSError, RuntimeError) as exc:
        if server:
            server.server_close()
            server.executor.shutdown(wait=False)
        parser.exit(1, f"启动失败：{exc}\n端口占用时可使用 PORT=8766 bash start.sh\n")
    print(f"PDF 解析工作台：http://127.0.0.1:{server.server_port}\n数据保存在：{server.data_dir}\n按 Ctrl+C 停止服务。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        with server.lock:
            server.stopping = True
            for process in server.processes:
                process.terminate()
        server.executor.shutdown(wait=False, cancel_futures=True)
        stop_process(mineru_process)
    # A remote parsing request may be waiting; saved jobs will be recovered on restart.
    os._exit(0)


if __name__ == "__main__":
    main()
