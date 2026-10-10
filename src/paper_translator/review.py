"""Apply explicit, source-bound review corrections while retaining previous values."""
from copy import deepcopy
from datetime import datetime, timezone
from .quality import source_digest, assess_tables


def apply_review(document, corrections, *, source_pdf_sha256, reviewer):
    result = deepcopy(document)
    blocks = {block['id']: block for block in result['blocks']}
    for correction in corrections:
        block = blocks[correction['id']]
        if source_digest(block.get('original', '')) != correction['expected_source_sha256']:
            raise ValueError(f"原文已发生变化，未应用核对记录：{block['id']}")
        block.setdefault('review_history', []).append({
            key: deepcopy(block[key]) for key in ('original', 'translated', 'translation_cells', 'review') if key in block})
        for key in ('original', 'translated', 'type'):
            if key in correction:
                block[key] = correction[key]
        block.pop('translation_cells', None)
        block['review'] = {'source_sha256': source_digest(block.get('original', '')),
                           'translation_sha256': source_digest(block.get('translated') or ''),
                           'source_pdf_sha256': source_pdf_sha256, 'reviewer': reviewer,
                           'note': correction['note'], 'reviewed_at': datetime.now(timezone.utc).isoformat()}
    assess_tables(result)
    return result
