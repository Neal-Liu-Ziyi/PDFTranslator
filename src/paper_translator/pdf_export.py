"""Generate a Chinese LaTeX document from translated JSON, retaining source formulas."""
from __future__ import annotations
import hashlib
import html
import math
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from bs4 import BeautifulSoup
import pymupdf
from .layout import NON_BODY, normalize_content_roles
from .quality import assess_tables

MATH = re.compile(r'\$\$[\s\S]*?\$\$|(?<!\\)\$(?:\\.|[^$])*?(?<!\\)\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]')
SPECIAL = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
           '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
           '≤': r'\ensuremath{\leq}', '≥': r'\ensuremath{\geq}', '→': r'\ensuremath{\to}',
           '↓': r'\ensuremath{\downarrow}', '↑': r'\ensuremath{\uparrow}', '−': '-', '–': '-', '—': '-',
           '\u00ad': '', '\u200b': '', 'α': r'\ensuremath{\alpha}', 'β': r'\ensuremath{\beta}',
           'σ': r'\ensuremath{\sigma}', 'μ': r'\ensuremath{\mu}'}
SPECIAL.update({symbol: '\\ensuremath{\\' + command + '}' for symbol, command in zip(
    'γδεζηθικλνξπρτυφχψωΓΔΘΛΞΠΣΥΦΨΩℓ',
    'gamma delta epsilon zeta eta theta iota kappa lambda nu xi pi rho tau upsilon phi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega ell'.split())})
SPECIAL.update({'×': r'\ensuremath{\times}', '÷': r'\ensuremath{\div}', '≠': r'\ensuremath{\ne}',
                '≈': r'\ensuremath{\approx}', '℃': r'\ensuremath{{}^{\circ}\mathrm{C}}',
                'µ': r'\ensuremath{\mu}', 'Å': r'\AA{}',
                '㎚': 'nm', '㎛': r'\ensuremath{\mu}m', '㎜': 'mm',
                '㎑': 'kHz', '㎒': 'MHz', '㎓': 'GHz', '∼': r'\textasciitilde{}'})
SUPERSCRIPTS = dict(zip('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ', '0123456789+-=()ni'))
SUBSCRIPTS = dict(zip('₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎', '0123456789+-=()'))
SAFE_COMMANDS = set('alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi rho varrho sigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega mathbb mathcal mathfrak mathrm mathbf mathit mathsf mathtt boldsymbol bm text textrm textbf textit operatorname hat widehat bar overline underline tilde widetilde vec dot ddot frac dfrac tfrac sqrt binom dbinom tbinom left right big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr le leq ge geq neq ne approx sim simeq equiv propto in notin subset subseteq supset supseteq cup cap emptyset varnothing forall exists neg land lor implies iff to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow mapsto mid vert Vert lvert rvert lVert rVert langle rangle lbrace rbrace lceil rceil lfloor rfloor partial nabla infty pm mp times cdot div ast star circ bullet ldots cdots vdots ddots dots sum prod int iint iiint oint lim inf sup min max arg log ln exp sin cos tan det tr Pr mathop mathrel mathbin mathord mathopen mathclose mathpunct mathinner limits nolimits substack overset underset stackrel underbrace overbrace phantom vphantom hphantom quad qquad thinspace medspace thickspace displaystyle textstyle scriptstyle scriptscriptstyle cal rm bf it sfrac coloneqq cases begin end hspace'.split())


SAFE_COMMANDS.update({'uparrow', 'downarrow', 'updownarrow', 'Uparrow', 'Downarrow', 'ell'})


def compiler_path():
    configured = os.getenv('PAPER_XELATEX')
    candidates = [configured] if configured else [shutil.which('xelatex'), '/Library/TeX/texbin/xelatex']
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)


def escape(text):
    text = re.sub(r'(?<=\d)[−-](?=[⁰¹²³⁴⁵⁶⁷⁸⁹])', '⁻', text)
    chunks, position = [], 0
    for match in re.finditer(r'[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ]+|[₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎]+', text):
        chunks.append(''.join(SPECIAL.get(c, c) for c in text[position:match.start()]))
        superscript = match.group()[0] in SUPERSCRIPTS
        mapping = SUPERSCRIPTS if superscript else SUBSCRIPTS
        chunks.append(('\\textsuperscript{' if superscript else '\\textsubscript{') + ''.join(mapping[c] for c in match.group()) + '}')
        position = match.end()
    chunks.append(''.join(SPECIAL.get(c, c) for c in text[position:]))
    return ''.join(chunks)


def patent_fields(text):
    """Unwrap bibliography columns while keeping each numbered field distinct."""
    fields, separated = [], False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            separated = True
            continue
        if not fields or re.match(r'^\(\d{2}\)', line):
            fields.append(line)
        else:
            # A Chinese word split across physical lines needs no extra space.
            separator = '；' if separated else '' if re.search(r'[\u3400-\u9fff]$', fields[-1]) and re.match(r'[\u3400-\u9fff]', line) else ' '
            fields[-1] += separator + line
        separated = False
    return fields


def math_body(text):
    if text.startswith('$$'):
        return text[2:-2]
    if text.startswith('$'):
        return text[1:-1]
    if text.startswith(('\\(', '\\[')):
        return text[2:-2]
    return text


def math_key(text):
    return re.sub(r'\s+', '', math_body(text).strip())


def safe_math(text):
    commands = re.findall(r'\\([A-Za-z]+)', text)
    if any(command not in SAFE_COMMANDS for command in commands):
        return False
    if any(env not in {'aligned', 'gathered', 'cases', 'matrix', 'pmatrix', 'bmatrix', 'smallmatrix'}
           for env in re.findall(r'\\(?:begin|end)\s*\{([^}]+)\}', text)):
        return False
    balance = 0
    for char in re.sub(r'\\[{}]', '', text):
        balance += (char == '{') - (char == '}')
        if balance < 0:
            return False
    return balance == 0 and '$' not in text and '^^' not in text


def table_column_weights(soup, columns, layout=None):
    """Use recovered grid geometry or OCR colgroup widths without executing CSS."""
    values = (layout or {}).get('column_widths')
    if not values:
        values = []
        for col in soup.select('colgroup > col'):
            raw = col.get('width') or ''
            match = re.search(r'(?:^|;)\s*width\s*:\s*([\d.]+(?:px|%)?)', col.get('style', ''), re.I)
            raw = match.group(1) if match else raw
            if not re.fullmatch(r'\d+(?:\.\d+)?(?:px|%)?', str(raw)):
                values = []
                break
            values.extend([float(re.sub(r'px|%', '', raw))] * max(1, min(30, int(col.get('span', 1)))))
    if len(values) != columns:
        return [1.] * columns
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):
        return [1.] * columns
    if not all(math.isfinite(v) and v > 0 for v in values):
        return [1.] * columns
    return [value * columns / sum(values) for value in values]


class Renderer:
    def __init__(self, document, result_root, build, source_pdf, layout):
        self.document, self.root, self.build = document, Path(result_root), Path(build)
        self.build.mkdir(parents=True, exist_ok=True)
        (self.build / 'assets').mkdir(exist_ok=True)
        self.source = pymupdf.open(source_pdf) if source_pdf else None
        self.layout = layout
        self.inline = {}
        self.warnings = []
        for page in document.get('mineru', {}).get('middle_json', {}).get('pdf_info', []):
            def walk(value):
                if isinstance(value, dict):
                    if value.get('type') == 'inline_equation' and value.get('content') and value.get('bbox'):
                        self.inline.setdefault((page['page_idx'] + 1, math_key(value['content'])), value['bbox'])
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(page.get('para_blocks', []))

    def copy_asset(self, asset):
        if not asset:
            return None
        source = (self.root / asset).resolve()
        if not source.is_relative_to((self.root / 'assets').resolve()) or not source.is_file():
            raise ValueError('图片资源不存在或路径无效。')
        if source.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.pdf'}:
            raise ValueError('图片格式不支持 LaTeX 编译。')
        name = 'assets/' + hashlib.sha256(asset.encode()).hexdigest()[:20] + source.suffix.lower()
        shutil.copyfile(source, self.build / name)
        return name

    def inline_formula(self, text, page):
        bbox = self.inline.get((page, math_key(text)))
        if bbox and self.source:
            name = 'assets/math-' + hashlib.sha256(f'{page}:{bbox}'.encode()).hexdigest()[:20] + '.png'
            if not (self.build / name).exists():
                crop = pymupdf.Rect(bbox) + (-.7, -.6, .7, .6)
                self.source[page - 1].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=crop, alpha=False).save(self.build / name)
            height = min(max((bbox[3] - bbox[1]) * .88, 8), 22)
            return r'\raisebox{-0.2em}{\adjustbox{max width=.92\linewidth}{\includegraphics[height=' + f'{height:.2f}pt' + ']{' + name + '}}}'
        body = math_body(text)
        if safe_math(body):
            return '$' + body + '$'
        self.warnings.append(f'第 {page} 页有无法安全排版的公式，已保留其原始表达文本。')
        return escape(text)

    def text(self, value, page):
        # Keep superscript/subscript semantics and literal angle-bracket headings.
        value = re.sub(r'</?(?:span|b|i|em|strong|p|br)(?:\s[^<>]*)?\s*/?>', '', value)
        value = html.unescape(value)
        value = value.replace(r'\-', '-').replace(r'\~', '~')
        parts, position = [], 0
        token = re.compile(MATH.pattern + r'|<(sup|sub)>(.*?)</\1>', re.S)
        for match in token.finditer(value):
            rendered = ('\\textsuperscript{' if match.group(1) == 'sup' else '\\textsubscript{') + escape(match.group(2)) + '}' if match.group(1) else self.inline_formula(match.group(), page)
            parts.extend([escape(value[position:match.start()]), rendered])
            position = match.end()
        parts.append(escape(value[position:]))
        return ''.join(parts).replace('\n', '\n\n')

    def table(self, html, page, caption='', note='', layout=None):
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.find_all('tr')
        grid, occupied, columns, cell_metrics = [], set(), 0, []
        for row_index, row in enumerate(rows):
            cells, column = {}, 0
            for cell in row.find_all(['td', 'th'], recursive=False):
                while (row_index, column) in occupied:
                    column += 1
                rowspan = max(1, min(int(cell.get('rowspan', 1)), 100))
                colspan = max(1, min(int(cell.get('colspan', 1)), 30))
                content = self.text(cell.decode_contents(), page)
                if rowspan > 1 or colspan > 1:
                    content = f'\\SetCell[r={rowspan},c={colspan}]{{l}} ' + content
                cells[column] = content
                cell_metrics.append((row_index, column, rowspan, colspan, cell.get_text()))
                for r in range(row_index, row_index + rowspan):
                    for c in range(column, column + colspan):
                        occupied.add((r, c))
                column += colspan
            columns = max(columns, column)
            grid.append(cells)
        if not columns or columns > 30:
            raise ValueError('表格列数无效。')
        # Repeat the complete merged header, not only its first physical row.
        header_rows = len(soup.thead.find_all('tr')) if soup.thead else 1
        weights = table_column_weights(soup, columns, layout)
        body = '\n'.join(' & '.join(row.get(c, '') for c in range(columns)) + r' \\' for row in grid)
        colsep = 1.5 if columns >= 10 else 3
        spec = (r'colspec={' + ' '.join(f'X[{weight:.5f},l]' for weight in weights)
                + r'},width=\linewidth,hlines,vlines,rowhead=' + str(header_rows)
                + f',colsep={colsep}pt' + r',rowsep=4pt,stretch=0,cells={font=\footnotesize}')
        # Keep short numeric tables together, including their caption and notes.
        row_heights = [19.] * len(rows)
        for row, column, rowspan, colspan, text in cell_metrics:
            width = ((480 - columns * (colsep * 2 + .4)) * sum(weights[column:column+colspan]) / sum(weights)
                     + (colspan-1) * (colsep*2+.4))
            text_width = sum(8 if ord(char) > 255 else 4 for char in text)
            height = (11 * max(1, math.ceil(text_width / max(8, width))) + 8) / rowspan
            for index in range(row, min(len(rows), row+rowspan)):
                row_heights[index] = max(row_heights[index], height)
        compact = len(rows) <= 20 and sum(row_heights) < 580 and len(caption) + len(note) < 500
        caption = r'\noindent\textbf{' + caption + '}\\par\\smallskip\n' if caption else ''
        note = '\n\\par\\smallskip{\\footnotesize ' + note + '}\\par\n' if note else ''
        if compact:
            rendered = '\\par\\noindent\\begin{minipage}{\\linewidth}\n' + caption + '\\begin{tblr}{' + spec + '}\n' + body + '\n\\end{tblr}\n' + note + '\\end{minipage}\\par\n'
        else:
            rendered = caption + '\\begin{longtblr}{' + spec + '}\n' + body + '\n\\end{longtblr}\n' + note
        # Keep the original table orientation and grid; wide tables span both
        # columns, but are never silently replaced by a landscape layout.
        begin, end = ('\\end{multicols}\n', '\\begin{multicols}{2}\n') if self.layout == 'double' else ('', '')
        return '\n' + begin + rendered + end

    def generate(self):
        normalize_content_roles(self.document)
        assess_tables(self.document)
        output = []
        blocks = sorted(self.document['blocks'], key=lambda b: (b['page'], b['order']))
        table_ids = {b['id'] for b in blocks if b['type'] == 'table'}
        table_captions = {b['parent_id']: b for b in blocks if b.get('parent_id') in table_ids and b['id'].endswith('-caption')}
        table_notes = {b['parent_id']: b for b in blocks if b.get('parent_id') in table_ids and b['id'].endswith('-footnote')}
        # A numbered patent table marker is its caption. Keep it on the same
        # page as the table rather than leaving it behind at a page break.
        table_markers = {}
        for index, block in enumerate(blocks):
            if block['type'] != 'table' or '<table' not in (block.get('translated') or block.get('original') or '').lower():
                continue
            for previous in reversed(blocks[:index]):
                if previous['type'] in NON_BODY or previous.get('parent_id') == block['id']:
                    continue
                value = previous.get('translated') or previous.get('original') or ''
                if re.fullmatch(r'\s*\[\d{4}\]\s*\[(?:表|표|Table)\s*\d+\]\s*', value, re.I):
                    table_markers[block['id']] = previous
                break
        marker_ids = {b['id'] for b in table_markers.values()}
        first_heading = bool(blocks and blocks[0]['type'] == 'heading')
        patent_cover = any(b.get('source_type') == 'native_patent_metadata' or re.search(r'\(12\)\s*United States Patent', b.get('original', ''), re.I) for b in blocks[:10])
        for block in blocks:
            page, kind = block['page'], block['type']
            if kind in NON_BODY or block['id'] in marker_ids:
                continue
            if patent_cover and page > 1:
                output.append('\\end{multicols}\\clearpage\\begin{multicols}{2}' if self.layout == 'double' else '\\clearpage')
                patent_cover = False
            if block.get('parent_id') in table_ids and block['id'].endswith(('-caption', '-footnote')):
                continue
            value = block.get('translated') or block.get('original') or ''
            if block.get('source_type') == 'native_patent_metadata':
                output.append('\\begingroup\\small\\setlength{\\parskip}{2pt}\n' +
                              '\n'.join('\\noindent ' + self.text(field, page) + '\\par' for field in patent_fields(value)) +
                              '\n\\endgroup\n')
            elif kind == 'heading':
                value = re.sub(r'^<([^<>]+)>$', r'\1', value.strip())
                if first_heading:
                    output.append(r'\begin{center}{\Large\bfseries ' + self.text(value, page) + r'}\end{center}' + '\n')
                    notes = dict.fromkeys(b['original'] for b in blocks if b['type'] == 'publication_note')
                    for note in notes:
                        output.append(r'\begingroup\renewcommand{\thefootnote}{}\footnotetext{' + self.text(note, page) + r'}\endgroup' + '\n')
                    first_heading = False
                else:
                    command = 'subsection' if re.match(r'^\d+\.\d+', value) else 'section'
                    output.append('\\' + command + '*{' + self.text(value, page) + '}\n')
            elif kind == 'table' and '<table' in value.lower():
                caption = table_markers.get(block['id']) or table_captions.get(block['id'], {})
                caption_text = self.text(caption.get('translated') or caption.get('original') or '', page)
                note = table_notes.get(block['id'], {})
                note_text = self.text(note.get('translated') or note.get('original') or '', page)
                if block.get('table_quality', {}).get('render') == 'source_image':
                    asset = self.copy_asset(block.get('asset'))
                    if not asset:
                        raise ValueError(f'第 {page} 页的表格疑似错位且没有原图，请核对后再导出。')
                    self.warnings.append(f'第 {page} 页表格识别疑似错位，已保留原表图片，未重排疑似错误的数据。')
                    begin, end = ('\\end{multicols}\n', '\\begin{multicols}{2}\n') if self.layout == 'double' else ('', '')
                    bbox = block.get('bbox') or [0, 0, 300, 0]
                    first_row = BeautifulSoup(block.get('original', ''), 'html.parser').find('tr')
                    columns = sum(int(cell.get('colspan', 1)) for cell in first_row.find_all(['td', 'th'])) if first_row else 1
                    natural = max(240, bbox[2] - bbox[0], columns * 32)
                    output.append(begin + '\\par\\noindent\\begin{minipage}{\\linewidth}\n' + caption_text +
                                  r'\begin{center}\includegraphics[width=' + f'{natural:.2f}pt' +
                                  r',max width=\linewidth,max height=.7\textheight,keepaspectratio]{' + asset +
                                  r'}\end{center}\end{minipage}' + '\n' + note_text + '\n' + end)
                else:
                    output.append(self.table(value, page, caption_text, note_text, block.get('table_layout')))
            elif kind in {'image', 'figure', 'table', 'equation'}:
                asset = self.copy_asset(block.get('asset'))
                if asset:
                    width = (block.get('bbox') or [0, 0, 300, 0])
                    natural = max(40, (width[2] - width[0]) * .9)
                    output.append(r'\begin{center}\includegraphics[width=' + f'{natural:.2f}pt' + r',max width=\linewidth,max height=.78\textheight,keepaspectratio]{' + asset + r'}\end{center}' + '\n')
                elif kind == 'equation':
                    body = math_body(block.get('formula', {}).get('latex') or block['original']).strip()
                    output.append(('\\[\n' + body + '\n\\]\n') if safe_math(body) else self.text(value, page) + '\n\n')
                if kind != 'equation' and value.strip():
                    output.append(self.text(value, page) + '\n\n')
            elif kind == 'footnote' and value.strip():
                output.append(r'\par{\footnotesize 脚注：' + self.text(value, page) + '}\par\n')
            elif value.strip():
                output.append(self.text(value, page) + '\n\n')
        # Preserve embedded image assets for the fast text-layer parser as well.
        if self.document.get('parser', {}).get('engine') == 'pymupdf':
            for asset in self.document.get('assets', []):
                output.append(r'\begin{center}\includegraphics[max width=\linewidth,max height=.75\textheight]{' + self.copy_asset(asset['asset']) + r'}\end{center}')
        option = ''
        preamble = r'''\documentclass[UTF8,fontset=fandol,10pt,a4paper''' + option + r''']{ctexart}
\usepackage[margin=19mm]{geometry}
\usepackage{amsmath,amssymb,mathtools,bm}
\usepackage[export]{adjustbox}
\usepackage{tabularray}
\DefTblrTemplate{caption-tag}{default}{}
\DefTblrTemplate{caption-sep}{default}{}
\DefTblrTemplate{caption-text}{default}{}
\DefTblrTemplate{conthead-text}{default}{（续表）}
\DefTblrTemplate{contfoot-text}{default}{续下页}
\usepackage{multicol}
\raggedcolumns
\usepackage{hyperref}
\hypersetup{hidelinks}
\setlength{\columnsep}{7mm}
\setlength{\parindent}{2em}
\setlength{\parskip}{4pt}
\setlength{\emergencystretch}{3em}
\ctexset{section={format=\large\bfseries},subsection={format=\normalsize\bfseries}}
\pagestyle{plain}
\begin{document}
'''
        begin, end = ('\\begin{multicols}{2}\n', '\n\\end{multicols}') if self.layout == 'double' else ('', '')
        return preamble + begin + '\n'.join(output) + end + '\n\\end{document}\n'


def generate_latex(document_path, build_dir, source_pdf=None, layout='single'):
    if layout not in {'single', 'double'}:
        raise ValueError('请选择单栏或双栏排版。')
    document_path = Path(document_path)
    document = json.loads(document_path.read_text())
    if document.get('translation', {}).get('status') != 'completed':
        raise ValueError('请先完成全文翻译，再生成译文 PDF。')
    renderer = Renderer(document, document_path.parent, build_dir, source_pdf, layout)
    try:
        tex = renderer.generate()
        (Path(build_dir) / 'translated.tex').write_text(tex, encoding='utf-8')
        return list(dict.fromkeys(renderer.warnings))
    finally:
        if renderer.source:
            renderer.source.close()


def compile_pdf(build_dir):
    compiler = compiler_path()
    if not compiler:
        raise RuntimeError('未找到 XeLaTeX，请安装 TeX Live / MacTeX 后重试。')
    build_dir = Path(build_dir)
    env = dict(os.environ, openin_any='p', openout_any='p')
    # Preserve the xelatex executable name: resolving its symlink selects the wrong format.
    executable = os.getenv('PAPER_XELATEX') or shutil.which('xelatex') or '/Library/TeX/texbin/xelatex'
    with (build_dir / 'compile.log').open('w') as log:
        for _ in range(2):
            try:
                result = subprocess.run([executable, '-no-shell-escape', '-interaction=nonstopmode',
                                         '-halt-on-error', 'translated.tex'], cwd=build_dir,
                                        env=env, stdout=log, stderr=log, timeout=180)
            except subprocess.TimeoutExpired:
                raise RuntimeError('PDF 编译超时。译文已保存，可重新生成。') from None
            if result.returncode:
                raise RuntimeError('LaTeX 编译失败，译文已保存。请查看本次生成目录中的 compile.log。')
    pdf = build_dir / 'translated.pdf'
    if 'Missing character:' in (build_dir / 'compile.log').read_text(errors='replace'):
        raise RuntimeError('PDF 字体存在缺字，已停止交付。请检查上标、下标或特殊字符。译文仍已保存。')
    if not pdf.is_file():
        raise RuntimeError('编译未生成 PDF。')
    return pdf
