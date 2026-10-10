# PDFTranslator

将论文 PDF 解析为结构化 JSON，使用大模型翻译成可选目标语言，再使用单栏或双栏 LaTeX 模板生成译文 PDF。

GitHub description:

> Extract PDFs into structured JSON, translate scientific papers with LLMs, and rebuild readable multilingual PDFs with LaTeX.

## 一键启动网页

```bash
bash start.sh
```

然后打开 <http://127.0.0.1:8765>。脚本使用现有 `fenghua` Python，同时提供网页和后台服务，无需另外启动前端，也不需要 Node.js。按 `Ctrl+C` 停止。

再次运行 `start.sh` 时，会自动停止本项目占用目标端口的旧工作台，等待端口释放后重新启动。已保存的文件和记录会保留；正在解析的任务会中断，重启后可点击重新解析。若端口属于其他程序，脚本会提示并退出，避免误关闭其他应用。`PORT` 和 `--port` 指定的端口同样适用。

网页支持选择或拖入 PDF、上传进度、后台解析、原文逐页预览、文字/图片/公式/JSON 查看、JSON 下载以及失败重试。原文件和解析结果保存在项目 `.data/` 中，刷新网页或重启服务后可继续查看处理记录。公式页签展示提取到的 LaTeX 源码或待识别候选，不把候选文本当作已识别公式。

每条处理记录右侧提供「删除」按钮，确认后移入「回收站」，原文、解析结果、翻译和生成文件一起隐藏。回收站中点击「恢复」可继续查看，刷新或重启服务后仍可恢复。正在解析、翻译或编译的记录暂不能删除。回收站不会释放磁盘空间，也不会删除上传前位于电脑其他目录的原始文件。

端口被占用时：

```bash
PORT=8766 bash start.sh
```

`FENGHUA_PYTHON` 可以覆盖 Python 路径；`PAPER_DATA_DIR` 可以指定数据保存目录。网页只监听本机地址，适用于本地单人使用。

**OCR 服务说明：** `start.sh` 同时启动网页（默认 8765）和本机 MinerU 3.4.5 服务（默认 18000），等待 OCR 服务可连接后开放网页。退出时一起停止；再次启动会清理本项目的旧服务。模型使用项目 `.mineru.json` 配置，缓存放在本机。当前使用 pipeline 后端，自动选择 CUDA / Apple MPS / CPU，MPS 不支持的算子允许回退 CPU。可通过 `MINERU_DEVICE_MODE=cpu` 强制使用 CPU。

英文、日文论文选择「英文 / 日文 / 中文」，韩文论文选择「韩文（可包含英文）」。MinerU 的 `ch` 模型实际支持英文与日文。原文预览和解析内容按页同步切换，JSON 下载包含全文。公式页签展示 LaTeX 源码；OCR 结果仍可能需要人工核对。

首次在新环境安装、下载模型（本机已完成）：

```bash
python -m pip install -r requirements-ocr.txt
MINERU_TOOLS_CONFIG_JSON="$PWD/.mineru.json" python \
  -m mineru.cli.models_download -s huggingface -m pipeline
bash start.sh
```

`MINERU_PORT` 可修改 OCR 服务端口。已有 MinerU HTTP 服务时，可通过 `MINERU_BASE_URL=http://地址:端口` 接入，脚本会检查连接但不会停止或接管该外部服务。日志在 `.data/mineru-service.log`，每次解析的日志在对应任务的 `run-*/parse.log`。仅需文字层提取时，可以直接运行 `PYTHONPATH=src python -m paper_translator.web --replace`。

网页接口测试（真实执行上传、解析、预览、下载、重试与历史恢复）：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_web.py -v
```

## 命令行解析

```bash
python -m paper_translator.cli input.pdf --output output/document.json
```

在当前 fenghua 环境中，如果环境不允许写入 site-packages，直接从源码运行：

```bash
PYTHONPATH=src python \
  -m paper_translator.cli input.pdf --output output/document.json
```

也可以直接使用安装后的命令：

```bash
paper-parse input.pdf -o output/document.json
```

输出包含：

- 页面尺寸、页码和单栏/双栏判断；
- 按阅读顺序排列的文本块、标题、图注、列表和疑似公式；
- 从 PDF 中提取的图片资源及其页面位置；
- 每个块的页面坐标、所在栏和原始文本；
- 扫描页检测、公式待识别和布局判断等警告。

图片保存在 JSON 文件旁边的 `assets/` 目录中。命令行解析只生成 JSON；翻译和 PDF 导出可在网页中执行。

## 多语言翻译与 PDF 导出

1. 上传 PDF 并等待解析完成，或从处理记录中选择已解析的论文。
2. 选择目标语言（简体中文、English、日本語、한국어、Français、Deutsch、Español 或 Русский），可选填写术语表，然后开始翻译。
3. 在「译文」中逐页查看结果，展开「对照原文」核对内容。每批完成后自动保存；服务重启或请求失败后，点击「继续翻译」即可从保存位置继续。
4. 全文翻译完成后，选择单栏或双栏，点击「生成 PDF」，即可下载对应语言的译文 PDF 和包含图片资源的 LaTeX 源码包。网页右上角还可以切换中文/English 界面。

原文保留在 JSON 的 `original` 中，译文写入同一内容块的 `translated`；表格保留行列结构，分别翻译单元格。公式、数值和引用标记在翻译请求中受到保护。更改术语表或模型配置后再次翻译，会重新翻译全文，以免混用不同设置的结果。已有译文不代表 OCR 或翻译绝对正确，重要公式和结论仍应核对原文。

在项目 `.env` 中配置兼容 Chat Completions 的服务（不要将密钥提交到 Git）：

```dotenv
PAPER_AI_BASE_URL=https://your-model-provider.example/v1
PAPER_AI_MODEL=你的模型名称
PAPER_AI_API_KEY_FILE=/绝对路径/apikey.txt
```

也可使用 `PAPER_AI_API_KEY` 环境变量。默认逐批翻译，避免服务并发限制；可选 `PAPER_TRANSLATION_WORKERS=3` 控制并发批次数（1–4）。相同的表格标签会复用已有译文。页面显示服务是否配置，认证失败、超时或返回格式错误会给出具体提示。请求会将论文文本发给这里配置的模型服务。

保留 MinerU 的版面类别：页眉、页脚、页码及出版声明在页面底部折叠展示，不混入正文或重复送去翻译。普通学术脚注仍保留并翻译。已有解析记录会在启动时更新类别，不重新 OCR，也不删除已有译文。导出的 PDF 使用新的页码，出版声明保留为独立首页注释。

PDF 导出需要 XeLaTeX（TeX Live / MacTeX）以及 `ctex`、`fandol`、`adjustbox`、`tabularray` 等包。本机已具备。可通过 `PAPER_XELATEX` 指定可执行文件。导出采用统一模板，不逐像素复刻原论文；图片和独立公式优先保留原始图像，行内公式优先从原 PDF 裁剪，避免 OCR 公式错误导致编译失败。图像中的文字不会自动改写。没有可用图像的公式才尝试使用经过限制的 LaTeX 表达式。

翻译结果在原有 `run-*/document.json` 中；PDF、源码及编译日志在该任务的 `pdf-*` 目录中。生成失败不会删除译文，可以重试。

### 内容与排版质量保护

- 英文完整日期（包括月份缩写、日期范围和年月）作为整体转换，避免年月日错位；数值、公式和引用仍逐项校验保留标记。
- 专利页边连续行号归入原文元数据，不再进入中文正文；尖括号包围的普通小标题仍会翻译。
- Unicode 上下标及 HTML `sup/sub` 会转换为真正的排版上下标；编译日志出现缺字时停止交付，避免指数被静默遗漏。
- 双栏文档中的宽表按正文顺序插入，表下注释紧随表格，不再漂移到权利要求之后。
- 表格发现合并样品编号、缺失编号或不一致列数时，工作台和 PDF 保留原表图片并提示核对，不对疑似错位的数据直接翻译重排。这些检查只能发现部分结构异常，不代表通过检查的表格数值一定正确。
- 对照原 PDF 核对后的修正可通过 `paper_translator.review.apply_review` 应用；原 OCR、旧译文、原 PDF 摘要及核对说明保存在 `review_history` / `review` 中。核对记录绑定原文与译文摘要，后续内容变化会使其失效。重新翻译保留仍有效的已核对译文。

### 韩文专利优化

对于带可靠文字层、带四位段落编号的韩国专利，MinerU 继续提取图片和表格，正文改用原 PDF 的文字层校正。保留段落编号、跨页续句和字体位置确定的上下标，并按图片位置恢复图号。首页专利著录字段不会再当作实验表格翻译。扫描件及其他文档不启用这一专项校正，仍走原有 OCR 流程。

新解析及启动时的历史记录更新都会应用校正；原始块完整保存在 `native_recovery.original_blocks`，并记录原 PDF 摘要。已经人工核对的文档不会自动改写；内容改变的块需要重新翻译，原文完全匹配（忽略空白）的译文可复用。

韩文、日文完整日期作为整体保护，韩文词尾紧连的数字也会保护。表格中拆开的比较符号、残缺单位括号及可疑指数会触发原图回退；这意味着原表中的韩文仍保留，并不表示已经生成准确的中文可编辑表格。

角度、质量百分比及常见科学单位与数值一起保护。若表格对应 PDF 中独立嵌入的图片，会恢复该图片的完整边界，避免 OCR 裁剪遗漏最后一行。原图导出按表格宽度缩放，标题与表格保持在一起。分段重试中已经校验成功的子批次立即保存，后续失败也可续译。

翻译每批最多 12 条、通常不超过 4000 原文字符（单个长段落不截断）。限流、部分服务错误和超时最多自动重试两次，遵守服务的 `Retry-After`；要求等待超过 60 秒时停止并保留进度，用户可稍后继续。认证错误不会自动重试。等待原因及重试次数会显示在工作台。

当前镍粉专利的表 1 已逐项对照原图恢复 15 个样品、12 列数据，权利要求已核对数量条件及限定范围。这是该文档的专项核对，不意味着其他文档的 OCR 错误会自动被准确修复。

运行网页与翻译/中文编译测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 在 fenghua 环境中运行

```bash
python -m pip install -e .
python -m paper_translator.cli input.pdf -o output/document.json
```

默认 `--engine auto` 会先访问 `http://127.0.0.1:18000/health`。MinerU 可用时使用 MinerU；服务不可用时自动回退到本地 PyMuPDF。也可以明确指定：

```bash
# 强制使用 MinerU
PYTHONPATH=src python \
  -m paper_translator.cli input.pdf -o output/document.json --engine mineru

# 强制使用本地解析
PYTHONPATH=src python \
  -m paper_translator.cli input.pdf -o output/document.json --engine pymupdf
```

MinerU 返回的 `content_list`、`middle_json` 和 Markdown 会保存在 JSON 的 `mineru` 字段中，便于后续生成 LaTeX。扫描页由 MinerU 负责 OCR；如果回退到 PyMuPDF，扫描页会被标记 `requires_ocr: true`。
