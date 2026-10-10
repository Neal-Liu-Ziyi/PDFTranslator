import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from paper_translator.native_korean import native_rows, recover_korean_patent
from paper_translator.quality import assess_tables, source_digest
from paper_translator.translation import (protect, restore, request_with_retry,
    TranslationError, TransientTranslationError, retry_delay, translate_document)
from paper_translator.pdf_export import escape, patent_fields
from paper_translator.layout import restore_visual_content


class KoreanTests(unittest.TestCase):
    def test_raster_table_recovers_complete_embedded_image_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.pdf'
            with pymupdf.open() as pdf:
                page = pdf.new_page(width=600, height=800)
                pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
                pix.clear_with(0x555555)
                page.insert_image(pymupdf.Rect(60, 80, 360, 230), pixmap=pix)
                pdf.save(source)
            document = {'pages': [{'number': 1}], 'assets': [{'id': 'p001-m0001-img', 'page': 1, 'asset': 'cropped.png'}],
                'blocks': [{'id': 'p001-m0001', 'type': 'table', 'page': 1, 'order': 1, 'bbox': [65, 82, 355, 224], 'asset': 'cropped.png', 'original': '<table></table>'}],
                'mineru': {'content_list': [{'type': 'table', 'page_idx': 0}]}}
            document['native_recovery'] = {'version': 1}
            document['blocks'].append({'id': 'p001-native', 'page': 1, 'order': 8, 'bbox': [60, 50, 360, 65], 'type': 'paragraph', 'original': 'Text above table'})
            self.assertTrue(restore_visual_content(document, root, source))
            self.assertEqual(document['blocks'][0]['id'], 'p001-native')
            self.assertEqual(document['blocks'][1]['bbox'], [60, 80, 360, 230])
            self.assertEqual(document['blocks'][1]['asset_recovery']['original_asset'], 'cropped.png')
            self.assertTrue((root / document['blocks'][1]['asset']).exists())
            self.assertFalse(restore_visual_content(document, root, source))

    def test_korean_dates_and_attached_numbers_are_atomic(self):
        text = '공고일자2016년12월23일 니켈50질량% 주석0.1∼10질량%'
        masked, pieces = protect(text, localize_dates=True)
        self.assertIn('2016年12月23日', pieces)
        self.assertTrue({'50质量%', '0.1', '10质量%'} <= set(pieces))
        self.assertEqual(restore(masked, pieces), text.replace('2016년12월23일', '2016年12月23日').replace('질량%', '质量%'))
        self.assertEqual(restore(*protect(text)), text)
        self.assertIn(r'\AA{}', escape('Å'))
        self.assertIn('GHz', escape('㎓'))
        masked, pieces = protect('44도 1000℃ 3.530Å', localize_dates=True)
        self.assertEqual(pieces, ['44°', '1000℃', '3.530Å'])
        self.assertEqual(restore(masked, pieces), '44° 1000℃ 3.530Å')

    def test_patent_fields_unwrap_addresses_without_losing_dates(self):
        self.assertEqual(patent_fields('(45) 公告日\n2016年12月23日\n\n(73) 专利权人\nJFE Mineral株式会\n社内'),
                         ['(45) 公告日 2016年12月23日', '(73) 专利权人 JFE Mineral株式会社内'])

    def test_superscript_survives_separate_pdf_lines(self):
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 100), 'x10', fontsize=12)
            page.insert_text((70, 94), '10', fontsize=7)
            page.insert_text((78, 100), ' ohm', fontsize=12)
            self.assertIn('<sup>10</sup>', native_rows(page)[0]['text'])

    def test_retry_is_bounded_and_does_not_retry_auth_errors(self):
        with patch('paper_translator.translation.request_translation', side_effect=[TransientTranslationError('busy', 2), ({'a': '结果'}, {})]) as request, patch('paper_translator.translation.time.sleep') as sleep:
            self.assertEqual(request_with_retry({}, [], '', {})[0], {'a': '结果'})
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(2)
        for error, count in [(TranslationError('authentication'), 1), (TransientTranslationError('busy'), 3), (TransientTranslationError('busy', 120), 1)]:
            with patch('paper_translator.translation.request_translation', side_effect=error) as request, patch('paper_translator.translation.time.sleep'):
                with self.assertRaises(TranslationError):
                    request_with_retry({}, [], '', {})
                self.assertEqual(request.call_count, count)
        self.assertEqual(retry_delay('12'), 12)
        self.assertIsNone(retry_delay('invalid'))

    def test_corrupted_comparators_and_exponents_use_source_images(self):
        for html in ['<table><tr><td>없음 &amp;</td><td>lt;10</td></tr></table>', '<table><tr><td>절연저항[1010Ω]</td><td>절연저항[i01요]</td></tr></table>']:
            doc = {'blocks': [{'type': 'table', 'original': html, 'asset': 'source.png'}]}
            assess_tables(doc)
            self.assertEqual(doc['blocks'][0]['table_quality']['render'], 'source_image')
        doc = {'blocks': [{'type': 'table', 'original': '<table><tr><td>농도[질량%]</td><td>&lt;10</td></tr></table>'}]}
        assess_tables(doc)
        self.assertEqual(doc['blocks'][0]['table_quality']['render'], 'structured')

    def test_split_batch_success_is_saved_before_later_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'document.json'
            path.write_text(json.dumps({'source': {'filename': 'test.pdf'}, 'blocks': [
                {'id': 'a', 'type': 'paragraph', 'original': 'First text'},
                {'id': 'b', 'type': 'paragraph', 'original': 'Second text'}]}))
            responses = [TranslationError('模型返回格式异常'), ({'a': '第一段'}, {}), TranslationError('认证失败')]
            config = {'base_url': 'https://example.test', 'model': 'test'}
            with patch('paper_translator.translation.model_config', return_value=config), patch('paper_translator.translation.request_translation', side_effect=responses):
                with self.assertRaises(TranslationError):
                    translate_document(path)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['blocks'][0]['translated'], '第一段')
            self.assertEqual(saved['translation']['completed'], 1)
            with patch('paper_translator.translation.model_config', return_value=config), patch('paper_translator.translation.request_translation', return_value=({'b': '第二段'}, {})) as request:
                result = translate_document(path)
            self.assertEqual([i['id'] for i in request.call_args.args[1]], ['b'])
            self.assertEqual(result['translation']['completed'], 2)

    def test_recovery_cross_page_figures_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.pdf'
            with pymupdf.open() as pdf:
                cover = pdf.new_page(width=595, height=842)
                cover.insert_text((60, 80), '등록특허', fontname='korea', fontsize=9)
                for line in range(8):
                    cover.insert_text((60, 110 + line * 14), '한국어 문서 내용 ' * 5, fontname='korea', fontsize=9)
                for number in (1, 2):
                    page = pdf.new_page(width=595, height=842)
                    page.insert_text((60, 80), ('체를 얻는다.' if number == 2 else '명세서'), fontname='korea', fontsize=9)
                    page.insert_text((10, 120), f'[{number:04d}]', fontsize=9)
                    for line in range(8):
                        page.insert_text((60, 120 + line * 14), '한국어 문서 내용을 확인합니다. ' * 3, fontname='korea', fontsize=9)
                    if number == 1:
                        page.insert_text((10, 774), '[0003]', fontsize=9)
                        page.insert_text((60, 774), '세라믹 소', fontname='korea', fontsize=9)
                page = pdf.new_page(width=595, height=842)
                page.insert_text((60, 80), '도면3', fontname='korea', fontsize=10)
                pdf.save(source)
            document = {'pages': [{'number': i} for i in range(1, 5)], 'blocks': [
                {'id': 'bad', 'page': 2, 'type': 'paragraph', 'original': 'OCR merged', 'translated': '旧译文'},
                {'id': 'fig', 'page': 4, 'type': 'image', 'original': '도면4', 'translated': '图4', 'bbox': [60, 100, 350, 300]}]}
            original = copy.deepcopy(document)
            self.assertTrue(recover_korean_patent(document, source))
            paragraph = next(b for b in document['blocks'] if '[0003]' in b['original'])
            self.assertIn('체를 얻는다.', paragraph['original'])
            self.assertEqual(paragraph['source_pages'], [2, 3])
            self.assertIsNone(paragraph['translated'])
            self.assertEqual(next(b for b in document['blocks'] if b['id'] == 'fig')['original'], '도면3')
            self.assertEqual(document['native_recovery']['original_blocks'], original['blocks'])
            before = json.dumps(document, sort_keys=True)
            self.assertFalse(recover_korean_patent(document, source))
            self.assertEqual(before, json.dumps(document, sort_keys=True))
            b = original['blocks'][0]
            b['review'] = {'source_sha256': source_digest(b['original']), 'translation_sha256': source_digest(b['translated'])}
            self.assertFalse(recover_korean_patent(original, source))


if __name__ == '__main__':
    unittest.main()
