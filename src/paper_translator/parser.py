"""Extract a PDF into a stable, translation-friendly JSON structure.

The parser deliberately keeps the original text and coordinates. Translation and
LaTeX rendering are separate stages, so extraction can be repeated without
calling an LLM or losing the source document.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pymupdf as fitz  # PyMuPDF 1.24+
except ImportError:
    try:
        import fitz  # Older PyMuPDF releases
    except ImportError as exc:  # pragma: no cover - exercised by the CLI error path
        raise RuntimeError(
            "PyMuPDF is required. Install it in the fenghua environment with "
            "python -m pip install 'PyMuPDF>=1.24,<2'."
        ) from exc


_CAPTION_RE = re.compile(r"^(?:figure|fig\.?|table|scheme|图|表)\s*[\dA-Za-z.-]*", re.I)
_LIST_RE = re.compile(r"^(?:[•●▪‣*-]|\(?\d+[.)]|\(?[A-Za-z][.)])\s+")
_FORMULA_CHARS_RE = re.compile(r"[=+−\-*/^_∑∫√≤≥≈≠∞∂∇α-ωΑ-Ω]")
_FORMULA_FONT_RE = re.compile(r"(?:math|symbol|cmmi|cmr|stix|cambria|mt extra)", re.I)


def _rect(values: tuple[float, float, float, float] | list[float]) -> list[float]:
    return [round(float(value), 2) for value in values]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _font_is_bold(font: str) -> bool:
    return bool(re.search(r"bold|black|semibold|demi", font, re.I))


def _is_formula(text: str, spans: list[dict[str, Any]]) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact or len(compact) > 240:
        return False
    formula_font = any(_FORMULA_FONT_RE.search(str(span.get("font", ""))) for span in spans)
    operators = len(_FORMULA_CHARS_RE.findall(compact))
    digits = sum(char.isdigit() for char in compact)
    letters = sum(char.isalpha() for char in compact)
    return formula_font or (operators >= 2 and digits + letters >= 2 and len(compact.split()) <= 16)


def _block_kind(text: str, spans: list[dict[str, Any]], median_size: float) -> str:
    if _is_formula(text, spans):
        return "equation"
    if _CAPTION_RE.match(text):
        return "caption"
    if _LIST_RE.match(text):
        return "list_item"
    max_size = max((float(span.get("size", 0)) for span in spans), default=0)
    bold = any(_font_is_bold(str(span.get("font", ""))) for span in spans)
    if len(text) <= 180 and (max_size >= median_size * 1.22 or (bold and len(text) <= 120)):
        return "heading"
    return "paragraph"


def _page_layout(blocks: list[dict[str, Any]], width: float) -> str:
    if len(blocks) < 4:
        return "single_column"
    left = [block for block in blocks if block["center"][0] < width * 0.47]
    right = [block for block in blocks if block["center"][0] > width * 0.53]
    middle = len(blocks) - len(left) - len(right)
    if len(left) >= 2 and len(right) >= 2 and middle / len(blocks) <= 0.18:
        return "two_column"
    return "single_column"


def _reading_key(block: dict[str, Any], layout: str) -> tuple[float, float]:
    if layout == "two_column":
        column = 0 if block["center"][0] < block["page_width"] / 2 else 1
        return (column, block["bbox"][1])
    return (block["bbox"][1], block["bbox"][0])


def _extract_text_blocks(page: Any, page_number: int) -> list[dict[str, Any]]:
    page_dict = page.get_text("dict", sort=False)
    raw_blocks = [item for item in page_dict.get("blocks", []) if item.get("type") == 0]
    spans_for_size = [
        span
        for item in raw_blocks
        for line in item.get("lines", [])
        for span in line.get("spans", [])
        if str(span.get("text", "")).strip()
    ]
    sizes = [float(span.get("size", 0)) for span in spans_for_size]
    median_size = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
    blocks: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks, start=1):
        spans = [
            span
            for line in raw.get("lines", [])
            for span in line.get("spans", [])
            if str(span.get("text", "")).strip()
        ]
        text = "\n".join(
            "".join(str(span.get("text", "")) for span in line.get("spans", []))
            for line in raw.get("lines", [])
        ).strip()
        if not text:
            continue
        bbox = _rect(raw.get("bbox", (0, 0, 0, 0)))
        blocks.append(
            {
                "id": f"p{page_number:03d}-b{index:03d}",
                "type": _block_kind(_clean_text(text), spans, median_size),
                "page": page_number,
                "bbox": bbox,
                "center": [round((bbox[0] + bbox[2]) / 2, 2), round((bbox[1] + bbox[3]) / 2, 2)],
                "original": text,
                "translated": None,
                "font": {
                    "family": str(spans[0].get("font", "")) if spans else "",
                    "size": round(max((float(span.get("size", 0)) for span in spans), default=0), 2),
                    "bold": any(_font_is_bold(str(span.get("font", ""))) for span in spans),
                },
            }
        )
    return blocks


def _save_images(page: Any, assets_dir: Path, page_number: int, seen: dict[int, str]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for image_index, image in enumerate(page.get_images(full=True), start=1):
        xref = int(image[0])
        if xref in seen:
            asset_path = seen[xref]
        else:
            extracted = page.parent.extract_image(xref)
            extension = extracted.get("ext", "bin")
            digest = hashlib.sha1(extracted["image"]).hexdigest()[:12]
            filename = f"page-{page_number:03d}-image-{image_index:03d}-{digest}.{extension}"
            output = assets_dir / filename
            output.write_bytes(extracted["image"])
            asset_path = str(Path("assets") / filename)
            seen[xref] = asset_path
        rectangles = [_rect(rect) for rect in page.get_image_rects(xref)]
        assets.append(
            {
                "id": f"p{page_number:03d}-img{image_index:03d}",
                "type": "image",
                "page": page_number,
                "asset": asset_path,
                "bbox": rectangles[0] if rectangles else None,
                "all_bboxes": rectangles,
                "xref": xref,
            }
        )
    return assets


def parse_pdf(input_path: str | Path, output_path: str | Path | None = None) -> dict[str, Any]:
    """Parse ``input_path`` and optionally write the JSON beside the PDF output."""
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = Path(output_path).expanduser().resolve() if output_path else source.with_suffix(".json")
    assets_dir = destination.parent / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(source)
    seen_images: dict[int, str] = {}
    pages: list[dict[str, Any]] = []
    all_blocks: list[dict[str, Any]] = []
    all_assets: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    try:
        for page_index, page in enumerate(document, start=1):
            width, height = float(page.rect.width), float(page.rect.height)
            blocks = _extract_text_blocks(page, page_index)
            layout = _page_layout(blocks, width)
            for block in blocks:
                block["page_width"] = width
                block["column"] = 1 if layout == "single_column" or block["center"][0] < width / 2 else 2
            blocks.sort(key=lambda item: _reading_key(item, layout))
            for order, block in enumerate(blocks, start=1):
                block["order"] = order
                block.pop("page_width", None)
                block.pop("center", None)
                if block["type"] == "equation":
                    block["formula"] = {
                        "latex": None,
                        "source_text": block["original"],
                        "recognition_status": "pending",
                    }
                all_blocks.append(block)
            images = _save_images(page, assets_dir, page_index, seen_images)
            all_assets.extend(images)
            requires_ocr = not blocks and bool(images)
            if requires_ocr:
                warnings.append({"code": "OCR_REQUIRED", "page": page_index, "message": "页面没有可提取文字，需要 OCR。"})
            pages.append(
                {
                    "number": page_index,
                    "width": round(width, 2),
                    "height": round(height, 2),
                    "layout": layout,
                    "text_block_ids": [block["id"] for block in blocks],
                    "asset_ids": [asset["id"] for asset in images],
                    "requires_ocr": requires_ocr,
                }
            )
    finally:
        document.close()
    result = {
        "schema_version": "paper-translator.document.v1",
        "source": {"filename": source.name, "path": str(source), "page_count": len(pages)},
        "document": {"layout": "two_column" if sum(page["layout"] == "two_column" for page in pages) > len(pages) / 2 else "single_column", "pages": len(pages)},
        "parser": {"engine": "pymupdf", "version": "PyMuPDF"},
        "pages": pages,
        "blocks": all_blocks,
        "assets": all_assets,
        "warnings": warnings,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_document(
    input_path: str | Path,
    output_path: str | Path,
    *,
    engine: str = "auto",
    mineru_url: str | None = None,
    mineru_timeout_seconds: float = 30.0,
    ocr_language: str = "ch",
) -> dict[str, Any]:
    """Use MinerU when available and fall back to local PyMuPDF parsing."""
    if engine not in {"auto", "mineru", "pymupdf"}:
        raise ValueError("engine must be auto, mineru, or pymupdf")
    if engine != "pymupdf":
        from .mineru import MinerUClient, MinerUClientError, normalize_mineru_result

        client = MinerUClient(
            mineru_url or os.getenv("MINERU_BASE_URL", "http://127.0.0.1:18000"),
            timeout_seconds=mineru_timeout_seconds,
        )
        try:
            if not client.available():
                raise MinerUClientError("MINERU_UNAVAILABLE", "MinerU 服务不可用")
            source = Path(input_path).expanduser().resolve()
            destination = Path(output_path).expanduser().resolve()
            return normalize_mineru_result(source, destination, client.parse(source, language=ocr_language))
        except MinerUClientError as exc:
            if engine == "mineru" or exc.code != "MINERU_UNAVAILABLE":
                raise
    result = parse_pdf(input_path, output_path)
    if engine == "auto":
        result["parser"]["requested_engine"] = "auto"
        result["warnings"].append({"code": "MINERU_FALLBACK", "message": "MinerU 不可用，已使用 PyMuPDF 本地解析。"})
        Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
