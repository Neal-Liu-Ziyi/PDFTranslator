"""Conservative structural checks; questionable tables retain the source image."""
import hashlib
import re
from bs4 import BeautifulSoup


def source_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def reviewed(block):
    review = block.get('review', {})
    return (review.get('source_sha256') == source_digest(block.get('original', ''))
            and review.get('translation_sha256') == source_digest(block.get('translated') or ''))


def table_issues(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = [[cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'], recursive=False)]
            for row in soup.find_all('tr')]
    issues = []
    if not rows:
        return ['未识别出表格行。']
    cells = [text for row in rows for text in row]
    if any(re.match(r'\s*(?:lt|gt|amp);', text) for text in cells):
        issues.append('比较符号或 HTML 字符被拆到不同单元格，数值关系需核对。')
    if any(text.count('[') != text.count(']') for text in cells):
        issues.append('列标题或单位括号不完整，可能存在识别错误。')
    units = re.findall(r'\[([^\[\]]+)\]', ' '.join(cells))
    if any(re.fullmatch(r'[iIlLoO0-9]+[가-힣]+', unit) or re.search(r'10\d{2,}\s*Ω', unit) for unit in units):
        issues.append('单位或指数识别不明确，保留原表以免改变数量级。')
    if not soup.select('[rowspan], [colspan]') and len({len(row) for row in rows}) > 1:
        issues.append('表格各行列数不一致。')
    # Only numbered sample tables: do not reinterpret general numerical data tables.
    if re.search(r'sample|specimen|样品|样本', ' '.join(rows[0]), re.I):
        labels = [row[0] if row else '' for row in rows[1:]]
        if any(re.fullmatch(r'\d+\s+\d+', label) for label in labels):
            issues.append('样品编号被合并到同一单元格。')
        if any(not label for label in labels):
            issues.append('存在缺失样品编号，可能发生行错位。')
        singles = [int(re.match(r'\d+', s)[0]) for s in labels if re.fullmatch(r'\d+\*?', s)]
        if len(singles) != len(set(singles)):
            issues.append('样品编号重复，需对照原表。')
    return issues


def assess_tables(document):
    changed = False
    for block in document.get('blocks', []):
        if block['type'] != 'table':
            continue
        issues = table_issues(block.get('original', ''))
        value = {'status': 'reviewed' if reviewed(block) else 'needs_review' if issues else 'unchecked',
                 'issues': issues,
                 'render': 'source_image' if issues and not reviewed(block) else 'structured'}
        if block.get('table_quality') != value:
            block['table_quality'] = value
            changed = True
    return changed
