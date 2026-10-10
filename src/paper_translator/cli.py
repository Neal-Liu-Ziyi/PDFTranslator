"""Command-line interface for structured PDF extraction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .parser import parse_document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="将 PDF 解析为结构化 JSON，并提取图片资源")
    parser.add_argument("input", type=Path, help="输入 PDF 文件")
    parser.add_argument("-o", "--output", type=Path, required=True, help="输出 JSON 文件")
    parser.add_argument("--engine", choices=("auto", "mineru", "pymupdf"), default="auto", help="解析引擎；auto 会优先使用 MinerU")
    parser.add_argument("--mineru-url", default=None, help="MinerU 服务地址，默认 http://127.0.0.1:18000")
    parser.add_argument("--mineru-timeout", type=float, default=30.0, help="MinerU 单次请求超时时间（秒）")
    parser.add_argument("--ocr-language", choices=("ch", "korean"), default="ch", help="ch 支持英文/日文/中文，korean 支持韩文/英文")
    args = parser.parse_args(argv)
    try:
        result = parse_document(args.input, args.output, engine=args.engine, mineru_url=args.mineru_url, mineru_timeout_seconds=args.mineru_timeout, ocr_language=args.ocr_language)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output.resolve()), "pages": len(result["pages"]), "blocks": len(result["blocks"]), "assets": len(result["assets"]), "warnings": len(result["warnings"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
