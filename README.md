# PDFTranslator

PDFTranslator extracts PDF documents into structured JSON, translates scientific papers with an OpenAI-compatible model provider, and rebuilds readable multilingual PDFs with LaTeX.

GitHub description:

> Extract PDFs into structured JSON, translate scientific papers with LLMs, and rebuild readable multilingual PDFs with LaTeX.

## Features

- Text-layer and MinerU-based PDF parsing with page-aware reading order.
- OCR and layout extraction for scanned documents when MinerU is installed.
- Structured JSON containing text blocks, headings, tables, images, equations, page coordinates, and source assets.
- Translation into selectable target languages: Simplified Chinese, English, Japanese, Korean, French, German, Spanish, and Russian.
- Formula, number, citation, date, and unit protection during translation.
- Table-cell translation while preserving the detected table structure.
- Single-column or double-column LaTeX PDF export.
- Local processing history with retry and reversible recycle-bin deletion.
- Chinese and English interface switching in the web workbench.

## Requirements

- Python 3.11 or newer.
- A virtual environment is recommended.
- XeLaTeX for PDF export.
- An OpenAI-compatible Chat Completions endpoint for translation.
- MinerU is optional. Without it, text-based PDFs can still be parsed with PyMuPDF.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
bash start.sh
```

Open <http://127.0.0.1:8765> in a browser. The startup script keeps uploaded files and processing results in `.data/`, which is ignored by Git.

To use OCR and layout extraction, install the optional OCR dependencies and download the MinerU pipeline model according to the MinerU documentation:

```bash
python -m pip install -r requirements-ocr.txt
MINERU_TOOLS_CONFIG_JSON="$PWD/.mineru.json" python -m mineru.cli.models_download -s huggingface -m pipeline
```

Set `PDFTRANSLATOR_PYTHON` only if you want `start.sh` to use a specific Python executable.

## Model provider configuration

Copy the example configuration and replace every placeholder with your own provider settings:

```bash
cp .env.example .env
```

```dotenv
PAPER_AI_BASE_URL=https://your-model-provider.example/v1
PAPER_AI_MODEL=your-model-name
PAPER_AI_API_KEY_FILE=/absolute/path/to/apikey.txt
```

You may use `PAPER_AI_API_KEY` instead of a key file. Do not commit `.env`, API keys, provider credentials, local MinerU configuration, PDFs, generated outputs, or model caches. These paths are ignored by `.gitignore`.

The web workbench sends each translation request to the configured provider and records the target language in the document JSON. Changing the target language or glossary creates a new translation signature, so an earlier translation is not silently reused for a different language.

## Command-line parsing

```bash
PYTHONPATH=src python -m paper_translator.cli input.pdf -o output/document.json
```

The JSON output contains page dimensions, reading-order blocks, headings, captions, tables, equations, extracted assets, page coordinates, and parser warnings. Translation and PDF export are available from the web workbench.

## Web workflow

1. Upload one PDF and select automatic, MinerU, or PyMuPDF parsing.
2. Review the page preview and structured content by page.
3. Select a target translation language and optionally provide a glossary.
4. Start translation and review the source/translation tabs.
5. Select a single-column or double-column layout and build the translated PDF.
6. Download the PDF or the LaTeX source archive.

Every translation batch is checkpointed. If a request times out or the provider returns a transient error, completed blocks remain saved and the task can be continued.

## Tests

Run the test suite from the project root:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The tests cover parsing, OCR/layout recovery, multilingual translation, formula and numeric protection, table preservation, PDF export, retry behavior, and the local web API.

## Privacy and generated files

The default workbench is local-only and listens on `127.0.0.1`. Uploaded PDFs, parsed JSON, translated content, generated PDFs, logs, and model caches stay under `.data/` or `output/` unless you explicitly configure another location. The configured model provider receives the text included in translation requests.

## License

See [LICENSE](LICENSE).
