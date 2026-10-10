import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_translator.translation import (protect, restore, TranslationError,
    translate_document, SUPPORTED_TRANSLATION_LANGUAGES)
from paper_translator.pdf_export import generate_latex, compile_pdf, compiler_path, safe_math
from paper_translator.layout import normalize_content_roles, restore_visual_content
from paper_translator.translation import translation_items


def sample():
    return {'source': {'filename': 'paper.pdf'}, 'parser': {'engine': 'mineru'}, 'pages': [{'number': 1}], 'assets': [],
            'blocks': [
                {'id': 'a', 'page': 1, 'order': 1, 'type': 'heading', 'original': 'Scientific paper', 'translated': None},
                {'id': 'b', 'page': 1, 'order': 2, 'type': 'paragraph', 'original': 'The value $x^2$ is 95.14% [1].', 'translated': None},
                {'id': 'c', 'page': 1, 'order': 3, 'type': 'table', 'original': '<table><tr><td rowspan="2">Model</td><td>Value</td></tr><tr><td>95.14</td></tr></table>', 'translated': None},
                {'id': 'd', 'page': 1, 'order': 4, 'type': 'equation', 'original': '$$x^2$$', 'formula': {'latex': '$$x^2$$'}, 'translated': None},
            ]}


class TranslationTests(unittest.TestCase):
    def test_target_language_is_saved_and_changes_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            path.write_text(json.dumps({'source': {'filename': 'paper.pdf'}, 'blocks': [
                {'id': 'a', 'page': 1, 'order': 1, 'type': 'paragraph', 'original': 'A result on Jan. 2, 2025.'}]}))
            def respond(config, items, context, glossary, target_language='zh-CN'):
                self.assertEqual(target_language, 'en')
                return {item['id']: 'An English result on Jan. 2, 2025.' for item in items}, {}
            with patch('paper_translator.translation.model_config', return_value={'base_url': 'https://example.com', 'model': 'test'}), patch('paper_translator.translation.request_translation', side_effect=respond):
                result = translate_document(path, target_language='en')
            self.assertEqual(result['translation']['target_language'], 'en')
            self.assertEqual(result['blocks'][0]['translated'], 'An English result on Jan. 2, 2025.')

    def test_unknown_target_language_is_rejected(self):
        self.assertIn('en', SUPPORTED_TRANSLATION_LANGUAGES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            path.write_text(json.dumps({'source': {'filename': 'paper.pdf'}, 'blocks': []}))
            with self.assertRaises(ValueError):
                translate_document(path, target_language='xx')

    def test_line_numbers_require_a_regular_aside_rail_outside_content(self):
        import copy
        document = {'pages': [{'number': 1, 'width': 600, 'height': 800}],
                    'blocks': [{'id': 'body', 'page': 1, 'type': 'paragraph', 'source_type': 'text',
                                'bbox': [70, 60, 290, 750], 'original': '5 samples reached 10 percent.', 'translated': '已有译文'}]}
        for i, number in enumerate(range(5, 70, 5)):
            document['blocks'].append({'id': f'n{i}', 'page': 1, 'type': 'paragraph', 'source_type': 'aside_text',
                                      'bbox': [300, 90 + i * 50, 308, 99 + i * 50], 'original': str(number)})
        in_table = copy.deepcopy(document)
        in_table['blocks'].append({'id': 'table', 'page': 1, 'type': 'table', 'original': 'data', 'bbox': [295, 70, 550, 750]})
        normalize_content_roles(in_table)
        self.assertFalse(any(b['type'] == 'line_number' for b in in_table['blocks']))
        irregular = copy.deepcopy(document)
        irregular['blocks'][5]['original'] = '2027'
        normalize_content_roles(irregular)
        self.assertFalse(any(b['type'] == 'line_number' for b in irregular['blocks']))
        document['blocks'].append({'id': 'numeric_body', 'page': 1, 'type': 'paragraph', 'source_type': 'text',
                                  'bbox': [100, 100, 110, 110], 'original': '50'})
        normalize_content_roles(document)
        self.assertEqual(sum(b['type'] == 'line_number' for b in document['blocks']), 13)
        self.assertEqual(document['blocks'][0]['translated'], '已有译文')
        self.assertEqual(document['blocks'][-1]['type'], 'paragraph')
        self.assertEqual(len(translation_items(document)[0]), 2)
        self.assertFalse(normalize_content_roles(document))

    def test_missing_charts_and_table_captions_are_recovered_once(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.pdf'
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=600, height=800)
                page.draw_rect(pymupdf.Rect(60, 80, 300, 240))
                pdf.save(source)
            document = {'pages': [{'number': 1, 'width': 600, 'height': 800}], 'assets': [],
                'blocks': [{'id': 'p001-m0002', 'type': 'table', 'page': 1, 'order': 1,
                            'original': '<table><tr><td>A</td></tr></table>', 'translated': '已保存表格'}],
                'mineru': {'content_list': [
                    {'type': 'chart', 'bbox': [100, 100, 500, 300], 'page_idx': 0, 'chart_caption': ['Figure 1. Results.']},
                    {'type': 'table', 'page_idx': 0, 'table_caption': ['Table 1. Summary.']}]}}
            self.assertTrue(restore_visual_content(document, root, source))
            self.assertFalse(restore_visual_content(document, root, source))
            self.assertEqual(len(document['blocks']), 3)
            self.assertTrue((root / document['blocks'][0]['asset']).is_file())
            self.assertEqual(document['blocks'][1]['original'], 'Table 1. Summary.')
            self.assertEqual(document['blocks'][2]['translated'], '已保存表格')

    def test_layout_roles_preserve_academic_footnotes_and_saved_translation(self):
        document = sample()
        sources = [('header', 'Manuscript under review'),
                   ('page_footnote', 'Preliminary work. Do not distribute.'),
                   ('page_footnote', '1 We use the unbiased variance estimator.'),
                   ('text', 'The method is described in a footnote below.')]
        document['mineru'] = {'content_list': [{'type': kind} for kind, _ in sources]}
        document['blocks'] = [{'id': f'p001-m{i:04d}', 'page': 1, 'order': i,
                               'type': 'paragraph', 'original': text, 'translated': '已保存译文'}
                              for i, (_, text) in enumerate(sources, 1)]
        self.assertTrue(normalize_content_roles(document))
        self.assertEqual([b['type'] for b in document['blocks']], ['header', 'publication_note', 'footnote', 'paragraph'])
        self.assertTrue(all(b['translated'] == '已保存译文' for b in document['blocks']))
        self.assertEqual(len(translation_items(document)[0]), 2)
        self.assertFalse(normalize_content_roles(document))

    def test_formulas_numbers_and_citations_are_protected(self):
        text = r'Result $x_{1}=3$ reaches 95.14% [1, 2] with \(y=2\).'
        masked, pieces = protect(text)
        self.assertEqual(restore(masked, pieces), text)
        with self.assertRaises(TranslationError):
            restore(masked.replace('[[PT_KEEP_0]]', ''), pieces)
        with self.assertRaises(TranslationError):
            restore(masked + '[[PT_KEEP_0]]', pieces)

    def test_checkpoint_table_structure_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            original = sample()
            path.write_text(json.dumps(original))
            def respond(config, items, context, glossary):
                return {item['id']: '中文 ' + item['text'] for item in items}, {}
            with patch('paper_translator.translation.model_config', return_value={'base_url': 'https://example.com', 'model': 'test'}), patch('paper_translator.translation.request_translation', side_effect=respond) as api:
                result = translate_document(path)
                self.assertEqual(result['translation']['status'], 'completed')
                self.assertEqual(result['translation']['completed'], result['translation']['total'])
                self.assertIn('rowspan="2"', result['blocks'][2]['translated'])
                self.assertIn('95.14', result['blocks'][2]['translated'])
                self.assertEqual([b['original'] for b in result['blocks']], [b['original'] for b in original['blocks']])
                self.assertIsNone(result['blocks'][3]['translated'])
                calls = api.call_count
                translate_document(path)
                self.assertEqual(api.call_count, calls)
                translate_document(path, glossary='Model = 模型')
                self.assertGreater(api.call_count, calls)

    def test_math_cannot_inject_tex_file_operations(self):
        self.assertFalse(safe_math(r'\input{/etc/passwd}'))
        self.assertFalse(safe_math(r'\end{document}'))
        self.assertTrue(safe_math(r'\frac{x^2}{2}'))

    @unittest.skipUnless(compiler_path(), 'XeLaTeX is not installed')
    def test_real_chinese_pdf_single_and_double_column(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = sample()
            document['translation'] = {'status': 'completed'}
            document['blocks'][0]['translated'] = '论文翻译测试'
            document['blocks'][1]['translated'] = '预测值 $x^2$ 的覆盖率为 95.14% [1]。'
            document['blocks'][2]['translated'] = '<table><tr><td rowspan="2">模型</td><td>数值</td></tr><tr><td>95.14</td></tr></table>'
            path = root / 'document.json'
            path.write_text(json.dumps(document))
            for layout in ('single', 'double'):
                build = root / layout
                generate_latex(path, build, layout=layout)
                pdf = compile_pdf(build)
                with pymupdf.open(pdf) as parsed:
                    text = ''.join(page.get_text() for page in parsed)
                    self.assertIn('论文翻译测试', text)
                    self.assertIn('95.14%', text)
