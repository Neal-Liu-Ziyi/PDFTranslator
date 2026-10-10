"""Source-checked reconstruction of two raster tables; never a generic OCR repair.

Run with PYTHONPATH=src python reviews/KR101689491B1_tables.py JOB_DIR.
The workbench must be idle; restart it after applying the review.
Original OCR and translations remain in each block's review_history.
"""
from copy import deepcopy
from hashlib import sha256
from html import escape
from pathlib import Path
import argparse
import json

from bs4 import BeautifulSoup
from paper_translator.review import apply_review
from paper_translator.quality import source_digest, reviewed
from paper_translator.translation import atomic_json, translation_items, translated_value

PDF_SHA256 = '1de09656b47c6c2c17f5ab92ab962d271062b7330afa120b54e632e2dbfc0a14'
SOURCE_DIGESTS = {
    'p010-m0135': 'baf4bd287a5f6f48406af308e6bcab91ffa0079af46ee3d2453b43634b416a64',
    'p011-m0151': '4cbe19d65f0f01ed0ff7c008122d395ab929be4f10156a205238291e0e6454cf',
}
# Measured physical column intervals in the complete source table images.
# Layout is shared by the original and translated cells, separate from text.
TABLE_LAYOUTS = {
    'p010-m0135': {'column_widths':[56,54,104,124,129,115,128,133,133,137,135,123,129,127],
                   'source':'source-image vertical grid lines'},
    'p011-m0151': {'column_widths':[113,88,169,231,169,224],
                   'source':'source-image vertical grid lines'},
}
# Exact physical column positions in the source image on PDF page 10.
# In most rows the final (3% hydrogen) cell is BLANK. Do not move the
# preceding two values rightward based on an interpretation of their sizes.
ROWS1 = [
    ['CVD','없음','<10','없음','0','3.525','129','50','11','710','320',''],
    ['PVD','없음','<10','없음','0','3.525','73','49','18','590','300',''],
    ['PVD','없음','<10','없음','0','3.526','49','48','25','535','280',''],
    ['PVD','없음','<10','Cu','10','3.528','75','42','11','550','350',''],
    ['CVD','0.13','<10','없음','0.2','3.525','220','51','1','400','480','310'],
    ['PVD','없음','<10','Sn','20','3.614','63','9','0','460','450','450'],
    ['CVD','없음','<10','Sn','2','3.536','130','48','9','650','500',''],
    ['CVD','없음','<10','Sn','5','3.547','131','38','7','510','520',''],
    ['CVD','없음','<10','Sn','5','3.547','149','37','2','550','540',''],
    ['CVD','없음','<10','Sn','8','3.560','133','30','5','480','550',''],
    ['PVD','없음','<10','Sn','2','3.536','88','46','10','485','460',''],
    ['PVD','없음','<10','Sn','5','3.547','94','38','7','510','480',''],
    ['PVD','없음','<10','Sn','8','3.560','78','31','2','450','510',''],
    ['PVD','없음','<10','Ti','5','3.531','53','30','2','520','490',''],
    ['PVD','없음','<10','Bi','10','3.589','70','40','7','505','550',''],
    ['CVD','0.14','<10','Sn','5','3.547','230','38','0','360','550','540'],
]
ROWS2 = [
    ['3.28','3.1','3.12','3.0'], ['3.34','1.9','3.04','3.0'],
    ['3.29','2.6','3.13','3.0'], ['3.27','3.3','3.04','2.7'],
    ['3.22','3.0','3.15','3.0'], ['3.28','','3.10',''], ['', '1.9', '', '2.7'],
]


def cell(value='', *, r=1, c=1, header=False):
    tag = 'th' if header else 'td'
    return f'<{tag} rowspan="{r}" colspan="{c}">{escape(value)}</{tag}>'


def tr(cells):
    return '<tr>' + ''.join(cells) + '</tr>'


def table1(chinese=False):
    labels = (['制造方法','硫添加量（质量%）','Zr（ppm）','添加金属元素','a轴长度（Å）',
               '一次粒径 D50（nm）','饱和磁化强度（emu/g）','剩余磁化强度（emu/g）',
               '二次粒径 D50（nm）','烧结温度（°C）','种类','质量%','气氛中氢浓度']
              if chinese else ['제조방법','유황 첨가량 [질량%]','Zr [ppm]','첨가금속원소','a축길이 (Å)',
               '1차 입자지경 (D50) [nm]','포화자화 [emu/g]','잔류자화 [emu/g]',
               '2차 입자지경 (D50) [nm]','소결온도 [°C]','종류','질량%','분위기수소농도'])
    head = [cell(r=3,c=2,header=True)]
    head += [cell(label,r=3,header=True) for label in labels[:3]]
    head += [cell(labels[3],c=2,header=True)]
    head += [cell(label,r=3,header=True) for label in labels[4:9]]
    head += [cell(labels[9],c=2,header=True)]
    result = '<table><thead>' + tr(head)
    result += tr([cell(labels[10],r=2,header=True),cell(labels[11],r=2,header=True),cell(labels[12],c=2,header=True)])
    result += tr([cell('0.12%',header=True),cell('3%',header=True)]) + '</thead><tbody>'
    for index, row in enumerate(ROWS1):
        cells = []
        if index in (0,6):
            label = ('比较例' if index == 0 else '实施例') if chinese else ('비교예' if index == 0 else '실시예')
            cells.append(cell(label,r=6 if index == 0 else 10))
        cells.append(cell(str(index + 1 if index < 6 else index - 5)))
        cells += [cell('无' if chinese and value == '없음' else value) for value in row]
        result += tr(cells)
    return result + '</tbody></table>'


def table2(chinese=False):
    labels = (['比较例1','实施例2','未添加','添加锡（5质量%）','介电损耗 DF（%）','绝缘电阻（10¹⁰Ω）','编号','平均值','最小值']
              if chinese else ['비교예1','실시예2','첨가없음','주석 첨가 (5질량%)','DF [%]','절연저항 [10¹⁰Ω]','No.','평균값','최저값'])
    result = '<table><thead>' + tr([cell(r=3,c=2,header=True),cell(labels[0],c=2,header=True),cell(labels[1],c=2,header=True)])
    result += tr([cell(labels[2],c=2,header=True),cell(labels[3],c=2,header=True)])
    result += tr([cell(labels[i],header=True) for i in (4,5,4,5)]) + '</thead><tbody>'
    for index,row in enumerate(ROWS2):
        cells = [cell(labels[6],r=5)] if index == 0 else []
        cells += [cell(str(index+1))] if index < 5 else [cell(labels[index+2],c=2)]
        result += tr(cells + [cell(value) for value in row])
    return result + '</tbody></table>'


def expanded_grid(html):
    """Validate all spans and expose cell positions for source/translation checks."""
    rows = BeautifulSoup(html, 'html.parser').find_all('tr')
    occupied = {}
    for i,row in enumerate(rows):
        col = 0
        for tag in row.find_all(['td','th'],recursive=False):
            while (i,col) in occupied:
                col += 1
            r,c = int(tag.get('rowspan',1)),int(tag.get('colspan',1))
            if r < 1 or c < 1 or i+r > len(rows):
                raise ValueError('Invalid cell span')
            for y in range(i,i+r):
                for x in range(col,col+c):
                    if (y,x) in occupied:
                        raise ValueError('Overlapping merged cells')
                    occupied[y,x] = tag.get_text() if (y,x) == (i,col) else None
            col += c
    width = max(x for _,x in occupied)+1
    if len(occupied) != len(rows)*width:
        raise ValueError('Incomplete table grid')
    return [[occupied[y,x] for x in range(width)] for y in range(len(rows))]


def reconstruct(document, source_sha256):
    if source_sha256 != PDF_SHA256:
        raise ValueError('This review is only valid for the checked source PDF')
    corrections = []
    for block_id, builder in zip(SOURCE_DIGESTS,(table1,table2)):
        block = next(b for b in document['blocks'] if b['id'] == block_id)
        original, translated = builder(),builder(True)
        if reviewed(block) and block['original'] == original and block['translated'] == translated:
            continue
        source_grid, target_grid = expanded_grid(original),expanded_grid(translated)
        assert len(source_grid) == len(target_grid)
        for source_row,target_row in zip(source_grid[3:],target_grid[3:]):
            assert len(source_row) == len(target_row)
            for source,target in zip(source_row,target_row):
                if source is None or source == '' or any(c.isdigit() for c in source):
                    assert source == target, (source,target)
        corrections.append(dict(id=block_id,expected_source_sha256=SOURCE_DIGESTS[block_id],
            original=original,translated=translated,
            note='对照源 PDF 第10、11页完整嵌入表格图片逐格核对。保留原表数值、精度、单位、合并格及空白列位置；表1未推测移动数据，表2斜线空格保持为空。'))
    result = apply_review(document,corrections,source_pdf_sha256=source_sha256,
                          reviewer='source-image-checked reconstruction') if corrections else deepcopy(document)
    for block in result['blocks']:
        if block['id'] in TABLE_LAYOUTS:
            block['table_layout'] = deepcopy(TABLE_LAYOUTS[block['id']])
    items,bindings = translation_items(result)
    completed = sum(bool(translated_value(bindings[item['id']])) for item in items)
    if completed != len(items):
        raise ValueError('Unexpected missing translation; leaving the saved document untouched')
    result['translation'].update(total=len(items),completed=completed,status='completed')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_dir',type=Path)
    args = parser.parse_args()
    root = args.job_dir
    job = json.loads((root/'job.json').read_text())
    if any(job.get(key) in ('queued','running') for key in ('status','translation_status','pdf_status')):
        raise ValueError('Wait until the workbench job is idle')
    path = root/job['result_dir']/'document.json'
    document = reconstruct(json.loads(path.read_text()),sha256((root/'source.pdf').read_bytes()).hexdigest())
    # Persistent snapshots make the one-off source review reversible.
    for source in (path,root/'job.json'):
        backup = source.with_name(source.name+'.before-table-review')
        if not backup.exists():
            backup.write_bytes(source.read_bytes())
    atomic_json(path,document)
    job.update(translation_total=document['translation']['total'],translation_completed=document['translation']['completed'],
               pdf_status='idle',pdf_error=None,pdf_warnings=[])
    atomic_json(root/'job.json',job)
    print('Saved two source-checked Chinese tables; restart the workbench and regenerate the PDF.')


if __name__ == '__main__':
    main()
