import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_translator.translation import protect, restore, translate_document, TranslationError
from paper_translator.quality import assess_tables, source_digest, reviewed
from paper_translator.review import apply_review
from paper_translator.pdf_export import Renderer, generate_latex, compile_pdf, compiler_path


class QualityTests(unittest.TestCase):
    def test_complete_dates_and_ranges_are_single_protected_values(self):
        dates = {'Nov. 17, 2009': '2009年11月17日', 'Nov. 20, 2006': '2006年11月20日',
                 'Jun. 7, 2007': '2007年6月7日', 'Dec. 7, 2005': '2005年12月7日',
                 'Mar. 26-28, 2002': '2002年3月26日至28日', 'Sep. 1986': '1986年9月',
                 'Sep. 20, 1988': '1988年9月20日'}
        for original, expected in dates.items():
            masked, pieces = protect(original, localize_dates=True)
            self.assertEqual(masked, '[[PT_KEEP_0]]')
            self.assertEqual(restore(masked, pieces), expected)
            masked, pieces = protect(original)
            self.assertEqual(restore(masked, pieces), original)
        with self.assertRaises((ValueError, TranslationError)):
            protect('Feb. 30, 2002', localize_dates=True)

    def test_angle_headings_are_translatable_but_scientific_markup_is_preserved(self):
        masked, _ = protect('<Content of Carbon>')
        self.assertEqual(masked, '<Content of Carbon>')
        original = '100×10−⁶ g; <20 ppm; m<sup>2</sup>/g; H<sub>2</sub>O'
        masked, pieces = protect(original)
        self.assertEqual(restore(masked, pieces), original)
        with tempfile.TemporaryDirectory() as directory:
            renderer = Renderer({}, directory, directory, None, 'single')
            result = renderer.text('100×10−⁶ g; &lt;20; m<sup>2</sup>/g; H<sub>2</sub>O; $x^2$', 1)
            self.assertIn(r'10\textsuperscript{-6}', result)
            self.assertIn('<20', result)
            self.assertNotIn('lt;', result)
            self.assertIn(r'm\textsuperscript{2}/g', result)
            self.assertIn(r'H\textsubscript{2}O', result)
            self.assertIn('$x^2$', result)

    def test_suspect_table_uses_original_image_until_reviewed(self):
        original = '<table><tr><td>Sample</td><td>Result</td></tr><tr><td>1 2</td><td>3 4</td></tr><tr><td></td><td>5</td></tr></table>'
        block = {'id': 'table', 'type': 'table', 'page': 1, 'order': 1, 'original': original}
        document = {'blocks': [block]}
        self.assertTrue(assess_tables(document))
        self.assertEqual(block['table_quality']['render'], 'source_image')
        self.assertFalse(assess_tables(document))
        fixed = '<table><tr><td>样品</td><td>结果</td></tr><tr><td>1</td><td>3</td></tr><tr><td>2</td><td>4</td></tr></table>'
        correction = {'id': 'table', 'expected_source_sha256': source_digest(original),
                      'original': fixed, 'translated': fixed, 'note': 'Checked original.'}
        result = apply_review(document, [correction], source_pdf_sha256='test', reviewer='test')
        self.assertEqual(result['blocks'][0]['review_history'][0]['original'], original)
        self.assertTrue(reviewed(result['blocks'][0]))
        self.assertEqual(result['blocks'][0]['table_quality']['status'], 'reviewed')
        result['blocks'][0]['translated'] += 'changed'
        self.assertFalse(reviewed(result['blocks'][0]))
        with self.assertRaises(ValueError):
            apply_review(result, [correction], source_pdf_sha256='test', reviewer='test')

    def test_reviewed_translation_survives_model_configuration_changes(self):
        original = 'Claim 1.'
        document = {'source': {'filename': 'test.pdf'}, 'blocks': [
            {'id': 'a', 'type': 'paragraph', 'page': 1, 'order': 1, 'original': original}]}
        document = apply_review(document, [{'id': 'a', 'expected_source_sha256': source_digest(original),
                    'translated': '1. 已核对的权利要求。', 'note': 'Verified'}], source_pdf_sha256='test', reviewer='test')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            path.write_text(json.dumps(document))
            with patch('paper_translator.translation.model_config', return_value={'base_url': 'https://example.com', 'model': 'new'}), patch('paper_translator.translation.request_translation') as request:
                result = translate_document(path, glossary='new')
                self.assertEqual(result['blocks'][0]['translated'], '1. 已核对的权利要求。')
                request.assert_not_called()

    @unittest.skipUnless(compiler_path(), 'XeLaTeX is not installed')
    def test_wide_table_and_note_precede_claims_in_compiled_double_layout(self):
        import pymupdf
        table = '<table><tr><td>样品</td><td>比表面积 m<sup>2</sup>/g</td></tr><tr><td>1</td><td>&lt;20</td></tr></table>'
        contents = [('a', 'paragraph', '正文 100×10−⁶ g。'), ('t-caption', 'caption', '表 1'),
                    ('t', 'table', table), ('t-footnote', 'caption', '* 不属于本发明的范围。'), ('z', 'heading', '权利要求')]
        document = {'translation': {'status': 'completed'}, 'blocks': []}
        for i, (id, kind, value) in enumerate(contents):
            block = dict(id=id, type=kind, page=1, order=i, original=value, translated=value)
            if id.startswith('t-'):
                block['parent_id'] = 't'
            document['blocks'].append(block)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'document.json'
            path.write_text(json.dumps(document))
            generate_latex(path, root / 'build', layout='double')
            pdf = compile_pdf(root / 'build')
            with pymupdf.open(pdf) as parsed:
                text = ''.join(page.get_text() for page in parsed)
            self.assertLess(text.index('样品'), text.index('不属于'))
            self.assertLess(text.index('不属于'), text.index('权利要求'))
            self.assertNotIn('Missing character:', (root / 'build/compile.log').read_text())
