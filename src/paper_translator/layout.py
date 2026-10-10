"""Preserve layout roles supplied by OCR instead of flattening them to body text."""
import re
from pathlib import Path
from statistics import median

NON_BODY = {'header', 'footer', 'page_number', 'publication_note', 'line_number'}
ROLE_MAP = {'header': 'header', 'page_header': 'header', 'footer': 'footer',
            'page_footer': 'footer', 'page_number': 'page_number', 'page_no': 'page_number',
            'line_number': 'line_number', 'line_numbering': 'line_number'}
NOTICE = re.compile(r'preliminary work|under review|do not distribute|copyright|©|all rights reserved', re.I)


def content_role(raw_type, text):
    raw_type = raw_type.lower()
    if raw_type in ROLE_MAP:
        return ROLE_MAP[raw_type]
    if raw_type in {'page_footnote', 'footnote'}:
        return 'publication_note' if NOTICE.search(text) else 'footnote'
    return None


def mark_line_numbers(document):
    """Identify regular number rails in OCR side notes, not arbitrary numeric text."""
    changed = False
    for page in document.get('pages', []):
        width, height = page.get('width', 0), page.get('height', 0)
        if not width or not height:
            continue
        blocks = [b for b in document['blocks'] if b['page'] == page['number']]
        protected = [b['bbox'] for b in blocks if b['type'] in {'image', 'table', 'equation'} and b.get('bbox')]
        body = [b['bbox'] for b in blocks if b['type'] in {'paragraph', 'heading', 'list_item'}
                and b.get('bbox') and b.get('source_type') != 'aside_text']
        candidates = []
        for block in blocks:
            bbox = block.get('bbox')
            if block.get('source_type') != 'aside_text' or not bbox or not re.fullmatch(r'\s*\d{1,4}\s*', block.get('original', '')):
                continue
            x, y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
            if bbox[2] - bbox[0] > width * .035 or bbox[3] - bbox[1] > height * .03:
                continue
            # Numbers in chart axes, tables and formulas must stay intact.
            if any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in protected + body):
                continue
            if not any(r[1] - height * .06 <= y <= r[3] + height * .06
                       and min(abs(x - r[0]), abs(x - r[2])) < width * .06 for r in body):
                continue
            candidates.append((x, y, int(block['original']), block))
        rails = []
        for item in sorted(candidates, key=lambda v: (v[0], v[1])):
            rail = next((r for r in rails if abs(item[0] - median(v[0] for v in r)) < width * .012), None)
            if rail is None:
                rails.append([item])
            else:
                rail.append(item)
        for rail in rails:
            rail.sort(key=lambda item: item[1])
            if len(rail) < 4 or rail[-1][1] - rail[0][1] < height * .15:
                continue
            increments = [b[2] - a[2] for a, b in zip(rail, rail[1:])]
            if len(set(increments)) != 1 or increments[0] not in {1, 5, 10}:
                continue
            gaps = [b[1] - a[1] for a, b in zip(rail, rail[1:])]
            typical = median(gaps)
            if any(abs(gap - typical) > typical * .35 for gap in gaps):
                continue
            for _, _, _, block in rail:
                if block['type'] != 'line_number':
                    block['type'] = 'line_number'
                    block['layout_role_reason'] = 'regular_numeric_aside_rail'
                    changed = True
    return changed


def normalize_content_roles(document):
    """Also migrate saved results, retaining IDs, source text and existing translations."""
    changed = False
    raw = document.get('mineru', {}).get('content_list', [])
    for block in document.get('blocks', []):
        source_type = block.get('source_type')
        match = re.fullmatch(r'p\d+-m(\d+)', block.get('id', ''))
        if source_type is None and match and 0 < int(match[1]) <= len(raw):
            source_type = raw[int(match[1]) - 1].get('type', 'text')
            block['source_type'] = source_type
            changed = True
        role = content_role(source_type or '', block.get('original', ''))
        if role and block['type'] != role:
            block['type'] = role
            changed = True
        if block['type'] == 'paragraph' and re.fullmatch(r'<[A-Za-z][A-Za-z /-]{2,100}>', block.get('original', '').strip()) and ' ' in block['original']:
            block['type'] = 'heading'
            changed = True
    changed = mark_line_numbers(document) or changed
    for page in document.get('pages', []):
        blocks = [b for b in document['blocks'] if b['page'] == page['number']]
        body_ids = [b['id'] for b in blocks if b['type'] not in NON_BODY | {'image', 'table'}]
        other_ids = [b['id'] for b in blocks if b['type'] in NON_BODY]
        for key, value in [('text_block_ids', body_ids), ('page_metadata_ids', other_ids)]:
            if page.get(key) != value:
                page[key] = value
                changed = True
    return changed


def restore_visual_content(document, result_root, source_pdf):
    """Recover omitted chart blocks/captions from saved MinerU output, without OCR."""
    import pymupdf
    raw = document.get('mineru', {}).get('content_list', [])
    blocks = {b['id']: b for b in document.get('blocks', [])}
    changed, source = False, None
    root = Path(result_root)
    try:
        for index, item in enumerate(raw, 1):
            kind = item.get('type')
            if kind not in {'chart', 'image', 'table'}:
                continue
            page = int(item.get('page_idx', 0)) + 1
            block_id = f'p{page:03d}-m{index:04d}'
            block = blocks.get(block_id)
            if block is None and kind == 'chart':
                size = document['pages'][page - 1]
                bbox = item.get('bbox')
                if not bbox:
                    continue
                bbox = [round(v * size['width' if i % 2 == 0 else 'height'] / 1000, 2) for i, v in enumerate(bbox)]
                block = {'id': block_id, 'type': 'image', 'source_type': 'chart', 'page': page,
                         'order': index, 'column': 1, 'bbox': bbox, 'original': '', 'translated': None}
                document['blocks'].append(block)
                blocks[block_id] = block
                changed = True
            if block is None:
                continue
            if kind == 'table' and block.get('bbox') and not block.get('asset_recovery'):
                if source is None:
                    source = pymupdf.open(source_pdf)
                predicted = pymupdf.Rect(block['bbox'])
                candidates = []
                for info in source[page - 1].get_image_info():
                    bounds = pymupdf.Rect(info['bbox'])
                    intersection = (bounds & predicted).get_area()
                    union = bounds.get_area() + predicted.get_area() - intersection
                    if union and intersection / union >= .7:
                        candidates.append((intersection / union, bounds))
                if candidates:
                    bounds = max(candidates, key=lambda item: item[0])[1]
                    asset = f'assets/complete-{block_id}.png'
                    (root / 'assets').mkdir(exist_ok=True)
                    source[page - 1].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=bounds + (-1, -1, 1, 1), alpha=False).save(root / asset)
                    block['asset_recovery'] = {'kind': 'embedded_image_bounds', 'original_asset': block.get('asset'), 'original_bbox': block['bbox']}
                    block.update(asset=asset, bbox=list(bounds))
                    for record in document.get('assets', []):
                        if record.get('id') == block_id + '-img':
                            record.update(asset=asset, bbox=list(bounds))
                    changed = True
            if kind == 'chart' and not block.get('asset'):
                if source is None:
                    source = pymupdf.open(source_pdf)
                asset = f'assets/recovered-{block_id}.png'
                (root / 'assets').mkdir(exist_ok=True)
                source[page - 1].get_pixmap(matrix=pymupdf.Matrix(2.5, 2.5), clip=pymupdf.Rect(block['bbox']), alpha=False).save(root / asset)
                block['asset'] = asset
                document.setdefault('assets', []).append({'id': block_id + '-img', 'type': 'image', 'kind': 'image', 'page': page, 'asset': asset, 'bbox': block['bbox']})
                changed = True
            captions = '\n'.join(item.get(kind + '_caption') or [])
            if kind in {'chart', 'image'} and captions and not block.get('original'):
                block['original'] = captions
                changed = True
            for suffix, text in [('caption', captions if kind == 'table' else ''),
                                 ('footnote', '\n'.join(item.get(kind + '_footnote') or []))]:
                extra_id = block_id + '-' + suffix
                if text and extra_id not in blocks and text != block.get('original'):
                    extra = {'id': extra_id, 'type': 'caption', 'parent_id': block_id,
                             'page': page, 'order': index, 'column': block.get('column', 1),
                             'bbox': None, 'original': text, 'translated': None}
                    document['blocks'].append(extra)
                    blocks[extra_id] = extra
                    changed = True
        if changed:
            def source_order(block):
                if document.get('native_recovery'):
                    bbox = block.get('bbox') or blocks.get(block.get('parent_id'), {}).get('bbox') or [0, 0, 0, 0]
                    return (block['page'], bbox[1], -1 if block['type'] == 'caption' else 0, bbox[0])
                match = re.match(r'p\d+-m(\d+)', block['id'])
                order = int(match[1]) if match else block['order']
                return (block['page'], order, -1 if block['id'].endswith('-caption') else 1 if block['id'].endswith('-footnote') else 0)
            document['blocks'].sort(key=source_order)
            for page in document['pages']:
                for order, block in enumerate((b for b in document['blocks'] if b['page'] == page['number']), 1):
                    block['order'] = order
                page['asset_ids'] = [a['id'] for a in document.get('assets', []) if a['page'] == page['number']]
    finally:
        if source:
            source.close()
    return changed
