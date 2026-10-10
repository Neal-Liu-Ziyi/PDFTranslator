#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${PDFTRANSLATOR_PYTHON:-}" ]]; then
  PYTHON_BIN="$PDFTRANSLATOR_PYTHON"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
  PYTHON_BIN="$VIRTUAL_ENV/bin/python"
else
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "找不到可用的 Python。请激活虚拟环境，或用 PDFTRANSLATOR_PYTHON 指定 Python。" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import pymupdf' >/dev/null 2>&1; then
  echo "当前 Python 环境缺少 PyMuPDF，请先安装：\"$PYTHON_BIN\" -m pip install 'PyMuPDF>=1.24,<2'" >&2
  exit 1
fi
cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1
exec "$PYTHON_BIN" -m paper_translator.web --replace --with-mineru --port "${PORT:-8765}" --data-dir "${PAPER_DATA_DIR:-$PROJECT_DIR/.data}" "$@"
