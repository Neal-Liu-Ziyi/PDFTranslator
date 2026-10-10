import importlib.util
from pathlib import Path
import tempfile
import unittest

from paper_translator.pdf_export import Renderer, table_column_weights
from bs4 import BeautifulSoup

spec = importlib.util.spec_from_file_location('korean_tables', Path(__file__).resolve().parents[1] / 'reviews/KR101689491B1_tables.py')
tables = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tables)


class ReviewedTableTests(unittest.TestCase):
    def test_table1_positions_and_precision_match_source_image(self):
        original = tables.expanded_grid(tables.table1())
        translated = tables.expanded_grid(tables.table1(True))
        self.assertEqual((len(translated),len(translated[0])),(19,14))
        self.assertEqual(translated[3][-3:], ['710','320',''])
        self.assertEqual(translated[7][5:], ['无','0.2','3.525','220','51','1','400','480','310'])
        self.assertEqual(translated[12][7], '3.560')
        self.assertEqual(translated[-1][-3:], ['360','550','540'])
        self.assertEqual(translated[3][0], '比较例')
        self.assertIsNone(translated[8][0])
        self.assertEqual(translated[9][0], '实施例')
        for source,target in zip(original[3:],translated[3:]):
            self.assertEqual([v for v in source if v and any(c.isdigit() for c in v)],
                             [v for v in target if v and any(c.isdigit() for c in v)])

    def test_table2_statistics_remain_blank_and_exponent_is_preserved(self):
        grid = tables.expanded_grid(tables.table2(True))
        self.assertEqual((len(grid),len(grid[0])),(10,6))
        self.assertEqual(grid[2][3], '绝缘电阻（10¹⁰Ω）')
        self.assertEqual(grid[-2][2:], ['3.28','','3.10',''])
        self.assertEqual(grid[-1][2:], ['', '1.9', '', '2.7'])
        with tempfile.TemporaryDirectory() as folder:
            tex = Renderer({},folder,folder,None,'single').table(tables.table2(True),11)
            self.assertIn(r'10\textsuperscript{10}\ensuremath{\Omega}',tex)
            self.assertNotIn(r'\begin{landscape}',tex)

    def test_wide_table_has_full_header_and_safe_column_flow(self):
        with tempfile.TemporaryDirectory() as folder:
            tex = Renderer({},folder,folder,None,'double').table(tables.table1(True),10,
                layout=tables.TABLE_LAYOUTS['p010-m0135'])
            self.assertIn('rowhead=3',tex)
            self.assertIn(r'\SetCell[r=3,c=2]',tex)
            self.assertIn(r'\begin{minipage}',tex)
            self.assertNotIn('landscape',tex)
            self.assertLess(tex.index(r'\end{multicols}'),tex.index(r'\begin{minipage}'))
            self.assertLess(tex.index(r'\end{minipage}'),tex.index(r'\begin{multicols}'))
            weights=table_column_weights(BeautifulSoup(tables.table1(True),'html.parser'),14,tables.TABLE_LAYOUTS['p010-m0135'])
            self.assertAlmostEqual(weights[0]/weights[7],56/133)
            self.assertIn(f'X[{weights[0]:.5f},l]',tex)

    def test_review_rejects_other_source_pdf(self):
        with self.assertRaisesRegex(ValueError,'checked source PDF'):
            tables.reconstruct({},'different PDF')

    def test_numbered_patent_caption_moves_with_table_once(self):
        document = {'blocks':[
            dict(id='marker',page=10,order=1,type='paragraph',original='[0095] [表1]'),
            dict(id='t-caption',page=10,order=2,type='caption',parent_id='t',original='表1'),
            dict(id='t',page=10,order=3,type='table',original=tables.table1(True)),
        ]}
        # Use the same review gate as production, so source-image fallbacks
        # cannot accidentally make this a test of a different rendering path.
        from paper_translator.review import apply_review
        from paper_translator.quality import source_digest
        document = apply_review(document,[dict(id='t',expected_source_sha256=source_digest(tables.table1(True)),
            translated=tables.table1(True),note='fixture')],source_pdf_sha256='fixture',reviewer='test')
        with tempfile.TemporaryDirectory() as folder:
            tex = Renderer(document,folder,folder,None,'single').generate()
        self.assertEqual(tex.count('[0095]'),1)
        self.assertEqual(tex.count('表1'),1)
        self.assertNotIn('landscape',tex)
        self.assertLess(tex.index(r'\begin{minipage}'),tex.index('[0095]'))
        self.assertLess(tex.index('[0095]'),tex.index(r'\begin{tblr}'))

    def test_ocr_column_proportions_and_invalid_widths(self):
        soup=BeautifulSoup('<table><colgroup><col width="20%"><col style="width: 80%"></colgroup></table>','html.parser')
        self.assertEqual(table_column_weights(soup,2),[.4,1.6])
        self.assertEqual(table_column_weights(soup,2,{'column_widths':[float('nan'),1]}),[1,1])


if __name__ == '__main__':
    unittest.main()
