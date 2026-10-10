"""Conservative digital Korean patent recovery; raster data remains OCR-derived."""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from statistics import median
import pymupdf
from .quality import reviewed

VERSION = 1
NUMBER = re.compile(r'^\[\d{4}\]')
FIGURE = re.compile(r'^도면\s*\d+$')


def readable(text):
    return len(re.findall(r'[가-힣]', text)) >= 100 and '\ufffd' not in text


def native_rows(page):
    """Rejoin PDF spans by baseline, explicitly retaining raised/lowered text."""
    spans = [s for b in page.get_text('dict')['blocks'] if b['type'] == 0
             for line in b['lines'] for s in line['spans'] if s['text'].strip()]
    if not spans:
        return []
    typical = median(s['size'] for s in spans)
    rows = []
    for span in sorted((s for s in spans if s['size'] >= typical * .8), key=lambda s: (s['origin'][1], s['bbox'][0])):
        row = next((r for r in reversed(rows) if abs(r['baseline'] - span['origin'][1]) < 2.5), None)
        if row is None:
            row = {'baseline': span['origin'][1], 'spans': []}
            rows.append(row)
        row['spans'].append((span, None))
    for span in (s for s in spans if s['size'] < typical * .8):
        candidates = [r for r in rows if abs(r['baseline'] - span['origin'][1]) < typical
                      and any(abs(s['bbox'][2] - span['bbox'][0]) < typical or abs(s['bbox'][0] - span['bbox'][2]) < typical
                              for s, _ in r['spans'])]
        if candidates:
            row = min(candidates, key=lambda r: abs(r['baseline'] - span['origin'][1]))
            delta = row['baseline'] - span['origin'][1]
            row['spans'].append((span, 'sup' if delta > typical * .2 else 'sub' if delta < -typical * .15 else None))
        else:
            rows.append({'baseline': span['origin'][1], 'spans': [(span, None)]})
    result = []
    for row in rows:
        parts, previous = [], None
        for s, tag in sorted(row['spans'], key=lambda v: v[0]['bbox'][0]):
            if previous and s['bbox'][0] - previous['bbox'][2] > typical * .6:
                parts.append(' ')
            parts.append(f'<{tag}>{s["text"]}</{tag}>' if tag else s['text'])
            previous = s
        box = pymupdf.Rect()
        for s, _ in row['spans']:
            box |= pymupdf.Rect(s['bbox'])
        result.append({'text': ''.join(parts).strip(), 'bbox': list(box), 'size': max(s['size'] for s, _ in row['spans'])})
    return sorted(result, key=lambda r: (r['bbox'][1], r['bbox'][0]))


def recover_korean_patent(document, source_pdf):
    """Idempotent migration with a complete pre-recovery block snapshot."""
    if document.get('native_recovery', {}).get('version') == VERSION:
        return False
    with pymupdf.open(source_pdf) as pdf:
        cover = pdf[0].get_text() if len(pdf) else ''
        if not readable(cover) or not re.search(r'등록특허|특허공보', cover):
            return False
        if any(reviewed(b) for b in document['blocks']):
            return False  # Manual verification takes precedence over automation.
        numbered = [i for i, page in enumerate(pdf) if re.search(r'\[\d{4}\]', page.get_text())]
        if not numbered or any(not readable(pdf[i].get_text()) for i in numbered):
            return False
        original = deepcopy(document['blocks'])
        # Bibliographic numbered fields are text, not an experimental table.
        for b in document['blocks']:
            if b['page'] != 1 or b['type'] != 'table' or not b.get('bbox'):
                continue
            box = pymupdf.Rect(b['bbox']) + (-3, -3, 3, 3)
            text = pdf[0].get_text('text', clip=box)
            if not readable(text) or len(re.findall(r'\((?:19|12|45|11|24|51|52|21|22|73|72|74)\)', text)) < 5:
                continue
            mid = pdf[0].rect.width / 2
            columns = [pymupdf.Rect(box.x0, box.y0, mid, box.y1), pymupdf.Rect(mid, box.y0, box.x1, box.y1)]
            b.update(type='paragraph', source_type='native_patent_metadata',
                     original='\n\n'.join(pdf[0].get_text('text', clip=c, sort=True).strip() for c in columns),
                     translated=None, native_source='pdf_text_layer')
            b.pop('translation_cells', None)
            b.pop('table_quality', None)
        previous_tail = None
        for index in numbered:
            page_no, page = index + 1, pdf[index]
            rows = [r for r in native_rows(page) if not re.fullmatch(r'등록특허\s+[\d-]+|[-–]?\s*\d+\s*[-–]?', r['text'])]
            old = [b for b in document['blocks'] if b['page'] == page_no]
            retained = [b for b in old if b['type'] in {'image', 'table', 'equation', 'caption', 'header', 'footer', 'page_number'}]
            regions = [pymupdf.Rect(b['bbox']) for b in retained if b['type'] in {'table', 'image', 'equation'} and b.get('bbox')]
            rows = [r for r in rows if not any(region.contains(pymupdf.Rect(r['bbox'])) for region in regions)]
            typical = median(r['size'] for r in rows)
            groups = []
            for row in rows:
                heading = row['size'] >= typical * 1.07 and not NUMBER.match(row['text'])
                prev = groups[-1] if groups else None
                if prev and not heading and prev['type'] != 'heading' and not NUMBER.match(row['text']) and row['bbox'][1] - prev['bbox'][3] < 9:
                    prev['original'] += '\n' + row['text']
                    prev['bbox'] = list(pymupdf.Rect(prev['bbox']) | pymupdf.Rect(row['bbox']))
                else:
                    groups.append({'id': f'p{page_no:03d}-native{len(groups)+1:04d}', 'page': page_no,
                                   'type': 'heading' if heading else 'paragraph', 'source_type': 'native_text',
                                   'column': 1, 'bbox': row['bbox'], 'original': row['text'], 'translated': None,
                                   'native_source': 'pdf_text_layer', 'source_pages': [page_no]})
            if groups and previous_tail and previous_tail['page'] == page_no - 1:
                first = groups[0]
                if (first['type'] == 'paragraph' and not NUMBER.match(first['original'])
                        and first['bbox'][1] < page.rect.height * .12
                        and not re.search(r'[.!?。]\s*$', previous_tail['original'])):
                    previous_tail['original'] += '\n' + first['original']
                    previous_tail['translated'] = None
                    previous_tail['source_pages'].append(page_no)
                    previous_tail.setdefault('continuation_bboxes', []).append({'page': page_no, 'bbox': first['bbox']})
                    groups.pop(0)
            previous_tail = groups[-1] if groups and groups[-1]['bbox'][3] > page.rect.height * .90 and NUMBER.match(groups[-1]['original']) else None
            known = {re.sub(r'\s+', '', b.get('original', '')): b for b in old if b.get('translated')}
            for b in groups:
                match = known.get(re.sub(r'\s+', '', b['original']))
                if match:
                    b['translated'] = match['translated']
            document['blocks'] = [b for b in document['blocks'] if b['page'] != page_no] + retained + groups
        # The nearest illustration below a Korean figure title owns that title.
        for index, page in enumerate(pdf):
            labels = [r for r in native_rows(page) if FIGURE.fullmatch(r['text'])]
            images = sorted([b for b in document['blocks'] if b['page'] == index + 1 and b['type'] == 'image' and b.get('bbox')], key=lambda b: b['bbox'][1])
            for label in labels:
                candidates = [b for b in images if b['bbox'][1] >= label['bbox'][3] - 2]
                if not candidates or candidates[0]['bbox'][1] - label['bbox'][3] > 80:
                    continue
                b = candidates[0]
                if b.get('original') != label['text']:
                    b.update(original=label['text'], translated=None, native_source='pdf_text_layer')
                for other in document['blocks']:
                    if other['page'] == index + 1 and other['type'] in {'header', 'caption'} and other.get('original') == label['text']:
                        other.update(type='header', source_type='header', caption_parent_id=b['id'])
        by_id = {b['id']: b for b in document['blocks']}
        def position(b):
            box = b.get('bbox') or by_id.get(b.get('parent_id'), {}).get('bbox') or [0, 0, 0, 0]
            return b['page'], box[1], -1 if b['type'] == 'caption' else 0, box[0]
        document['blocks'].sort(key=position)
        for page in document['pages']:
            for order, b in enumerate((b for b in document['blocks'] if b['page'] == page['number']), 1):
                b['order'] = order
            if page['number'] - 1 in numbered:
                page['layout'] = 'single_column'
        document['native_recovery'] = {'version': VERSION, 'source_sha256': hashlib.sha256(Path(source_pdf).read_bytes()).hexdigest(),
                                       'pages': [i + 1 for i in numbered], 'original_blocks': original}
        return True
