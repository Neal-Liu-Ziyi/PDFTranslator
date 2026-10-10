"""HTTP client and normalizer for the Fenghua MinerU service."""

from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pymupdf

from .parser import _rect, _save_images
from .layout import normalize_content_roles, restore_visual_content
from .quality import assess_tables
from .native_korean import recover_korean_patent


class MinerUClientError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----paper-translator-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/pdf"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="files"; filename="{file_path.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        file_path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class MinerUClient:
    def __init__(self, base_url: str, timeout_seconds: float = 30.0, poll_seconds: float = 1.0, task_timeout_seconds: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.task_timeout_seconds = task_timeout_seconds

    def _request(self, path: str, *, method: str = "GET", body: bytes | None = None, content_type: str | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1500]
            raise MinerUClientError("MINERU_REQUEST_FAILED", f"MinerU 请求失败（{exc.code}）：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MinerUClientError("MINERU_UNAVAILABLE", f"MinerU 服务不可用：{exc}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MinerUClientError("MINERU_INVALID_RESPONSE", "MinerU 返回的不是有效 JSON") from exc

    def available(self) -> bool:
        try:
            result = self._request("health")
            return isinstance(result, dict) and result.get("status") == "healthy"
        except MinerUClientError:
            return False

    def parse(self, source: Path, language: str = "ch") -> dict[str, Any]:
        fields = {
            "backend": "pipeline",
            "parse_method": "auto",
            "lang_list": language,
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "true",
            "return_images": "true",
        }
        body, content_type = _multipart(fields, source)
        submitted = self._request("tasks", method="POST", body=body, content_type=content_type)
        task_id = submitted.get("task_id") if isinstance(submitted, dict) else None
        if not task_id:
            raise MinerUClientError("MINERU_INVALID_RESPONSE", "MinerU 没有返回任务 ID")
        deadline = time.monotonic() + self.task_timeout_seconds
        while time.monotonic() < deadline:
            status = self._request(f"tasks/{urllib.parse.quote(str(task_id), safe='')}")
            state = status.get("status") if isinstance(status, dict) else None
            if state in {"completed", "success", "done"}:
                break
            if state in {"failed", "error"}:
                raise MinerUClientError("MINERU_PARSE_FAILED", str(status.get("error") or "MinerU 解析失败"))
            time.sleep(self.poll_seconds)
        else:
            raise MinerUClientError("MINERU_TIMEOUT", "MinerU 解析超时")
        result = self._request(f"tasks/{urllib.parse.quote(str(task_id), safe='')}/result")
        results = result.get("results") if isinstance(result, dict) else None
        first = next(iter(results.values()), None) if isinstance(results, dict) else None
        if not isinstance(first, dict):
            raise MinerUClientError("MINERU_EMPTY_RESULT", "MinerU 返回结果为空")
        content = first.get("content_list", "[]")
        middle = first.get("middle_json", "{}")
        try:
            content_list = json.loads(content) if isinstance(content, str) else content
            middle_json = json.loads(middle) if isinstance(middle, str) else middle
        except json.JSONDecodeError as exc:
            raise MinerUClientError("MINERU_INVALID_RESULT", "MinerU 结构化结果不是有效 JSON") from exc
        if not isinstance(content_list, list):
            raise MinerUClientError("MINERU_INVALID_RESULT", "MinerU content_list 不是数组")
        return {
            "version": str(result.get("version") or "unknown"),
            "content_list": content_list,
            "middle_json": middle_json,
            "markdown": first.get("md_content", ""),
            "images": first.get("images", {}),
        }


def _item_text(item: dict[str, Any]) -> str:
    for key in ("text", "content", "md", "latex", "equation", "table_body"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("list_items", "image_caption", "table_caption"):
        value = item.get(key)
        if isinstance(value, list):
            return "\n".join(text for text in value if isinstance(text, str))
    return ""


def _item_bbox(item: dict[str, Any]) -> list[float] | None:
    value = item.get("bbox", item.get("box", item.get("coordinates")))
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return _rect(value[:4])
        except (TypeError, ValueError):
            return None
    return None


def normalize_mineru_result(source: Path, destination: Path, parsed: dict[str, Any]) -> dict[str, Any]:
    """Convert MinerU content blocks into the paper-translator JSON contract."""
    (destination.parent / "assets").mkdir(parents=True, exist_ok=True)
    document = pymupdf.open(source)
    pages: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    image_paths: dict[str, str] = {}
    for name, data_uri in parsed.get("images", {}).items():
        if not isinstance(data_uri, str) or not data_uri.startswith("data:image/"):
            continue
        header, encoded = data_uri.split(",", 1)
        extension = {"data:image/png;base64": "png", "data:image/jpeg;base64": "jpg",
                     "data:image/webp;base64": "webp"}.get(header)
        if not extension:
            continue
        data = base64.b64decode(encoded, validate=True)
        asset = f"assets/mineru-{hashlib.sha256(data).hexdigest()[:20]}.{extension}"
        (destination.parent / asset).write_bytes(data)
        image_paths[Path(name).name] = asset
    seen_images: dict[int, str] = {}
    try:
        for page_number, page in enumerate(document, start=1):
            page_assets = [] if image_paths else _save_images(page, destination.parent / "assets", page_number, seen_images)
            assets.extend(page_assets)
            pages.append({"number": page_number, "width": round(float(page.rect.width), 2), "height": round(float(page.rect.height), 2)})
    finally:
        document.close()

    blocks: list[dict[str, Any]] = []
    for index, item in enumerate(parsed.get("content_list", []), start=1):
        if not isinstance(item, dict):
            continue
        page_number = int(item.get("page_idx") or 0) + 1
        raw_type = str(item.get("type", "text")).lower()
        kind = "equation" if "equation" in raw_type or "formula" in raw_type else (
            "image" if raw_type in {"image", "figure", "picture", "chart"} else (
                "table" if raw_type == "table" else ("heading" if raw_type in {"title", "heading"} or item.get("text_level") else "paragraph")
            )
        )
        text = _item_text(item)
        bbox = _item_bbox(item)
        if not 1 <= page_number <= len(pages):
            raise ValueError("MinerU 返回的页码超出原文范围。")
        if bbox:
            page = pages[page_number - 1]
            bbox = [round(value * page["width" if i % 2 == 0 else "height"] / 1000, 2)
                    for i, value in enumerate(bbox)]
        block: dict[str, Any] = {
            "id": f"p{page_number:03d}-m{index:04d}",
            "type": kind,
            "page": page_number,
            "order": index,
            "column": 1,
            "bbox": bbox,
            "original": text,
            "translated": None,
            "confidence": item.get("score", item.get("confidence")),
        }
        if kind in {"image", "table"}:
            block["mineru_asset"] = item.get("img_path", item.get("image_path"))
        asset = image_paths.get(Path(item.get("img_path") or item.get("image_path") or "").name)
        if asset:
            block["asset"] = asset
            assets.append({"id": block["id"] + "-img", "type": "image", "page": page_number,
                           "asset": asset, "bbox": bbox, "kind": kind})
        if kind == "equation":
            latex = item.get("latex") or (text if item.get("text_format") == "latex" else None)
            block["formula"] = {"latex": latex, "source_text": text, "recognition_status": "ready" if latex else "pending"}
        if text or kind in {"image", "table"}:
            blocks.append(block)

    page_map = {page["number"]: page for page in pages}
    for page_number, page in page_map.items():
        page_blocks = [block for block in blocks if block["page"] == page_number]
        # MinerU already establishes reading order, including multi-column pages.
        for order, block in enumerate(page_blocks, start=1):
            block["order"] = order
            if block["bbox"]:
                block["column"] = 1 if block["bbox"][0] + block["bbox"][2] < page["width"] else 2
        page["text_block_ids"] = [block["id"] for block in page_blocks if block["type"] not in {"image", "table"}]
        page["asset_ids"] = [asset["id"] for asset in assets if asset["page"] == page_number]
        left_count = sum(1 for block in page_blocks if block["column"] == 1 and block["bbox"])
        right_count = sum(1 for block in page_blocks if block["column"] == 2 and block["bbox"])
        page["layout"] = "two_column" if left_count >= 2 and right_count >= 2 else "single_column"
        page["requires_ocr"] = False

    result = {
        "schema_version": "paper-translator.document.v1",
        "source": {"filename": source.name, "path": str(source), "page_count": len(pages)},
        "document": {"layout": "two_column" if sum(page.get("layout") == "two_column" for page in pages) > len(pages) / 2 else "single_column", "pages": len(pages)},
        "parser": {"engine": "mineru", "version": f"mineru-{parsed.get('version', 'unknown')}"},
        "pages": pages,
        "blocks": blocks,
        "assets": assets,
        "warnings": [] if blocks else [{"code": "MINERU_EMPTY_BLOCKS", "message": "MinerU 未返回可用内容块。"}],
        "mineru": {"markdown": parsed.get("markdown", ""), "middle_json": parsed.get("middle_json", {}), "content_list": parsed.get("content_list", [])},
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    restore_visual_content(result, destination.parent, source)
    recover_korean_patent(result, source)
    normalize_content_roles(result)
    assess_tables(result)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
