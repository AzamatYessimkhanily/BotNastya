"""Read plain contact tables from XLSX or UTF-8 CSV without executing formulas."""
import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
from zipfile import ZipFile

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
ALIASES = {
    'phone': ('phone', 'телефон', 'номер', 'номер телефона'),
    'name': ('name', 'имя', 'фио'),
    'branch': ('branch', 'филиал'),
    'consent': ('consent', 'согласие', 'согласие на рассылку'),
    'evidence': ('evidence', 'подтверждение согласия', 'источник согласия', 'основание согласия'),
}


def xlsx_rows(path, sheet_name=None):
    with ZipFile(path) as z:
        if sum(info.file_size for info in z.infolist()) > 50 * 1024 * 1024:
            raise ValueError('Распакованный Excel превышает 50 MiB')
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.text or '' for t in item.findall('.//s:t', NS))
                       for item in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si', NS)]
        sheets = ET.fromstring(z.read('xl/workbook.xml')).findall('s:sheets/s:sheet', NS)
        sheet = next((s for s in sheets if s.attrib['name'] == (sheet_name or 'Контакты')), None)
        if sheet is None:
            if sheet_name or len(sheets) != 1:
                raise ValueError('Укажите --sheet: файл содержит несколько листов без листа «Контакты»')
            sheet = sheets[0]
        rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == rid)
        filename = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        root = ET.fromstring(z.read(filename))
        for row in root.findall('s:sheetData/s:row', NS):
            cells = {}
            for cell in row.findall('s:c', NS):
                if cell.find('s:f', NS) is not None:
                    raise ValueError('В таблице есть формулы: сохраните значения, затем импортируйте')
                letters = re.match(r'[A-Z]+', cell.attrib['r']).group()
                index = 0
                for letter in letters:
                    index = index * 26 + ord(letter) - 64
                if index > 64:
                    raise ValueError('В таблице более 64 столбцов')
                value = cell.findtext('s:v', '', NS)
                kind = cell.attrib.get('t')
                if kind == 's':
                    value = strings[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(t.text or '' for t in cell.findall('.//s:t', NS))
                elif value and kind in (None, 'n'):
                    try:
                        number = Decimal(value)
                        value = str(int(number)) if number == number.to_integral_value() else value
                    except (InvalidOperation, ValueError, OverflowError):
                        pass
                cells[index - 1] = value
            if cells:
                yield [cells.get(i, '') for i in range(max(cells) + 1)]


def read_contacts(filename, sheet=None):
    path = Path(filename)
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError('Файл превышает 10 MiB')
    if path.suffix.lower() == '.xlsx':
        rows = list(xlsx_rows(path, sheet))
    elif path.suffix.lower() == '.csv':
        text = path.read_text(encoding='utf-8-sig')
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t') if any(x in text for x in ',;\t') else csv.excel
        rows = list(csv.reader(text.splitlines(), dialect))
    else:
        raise ValueError('Сохраните таблицу в .xlsx или CSV UTF-8')
    if not rows or len(rows) > 50001:
        raise ValueError('Нужна таблица с заголовком и не более 50 000 строк')
    headers = [str(v).strip().lower() for v in rows[0]]
    mapping = {}
    for key, aliases in ALIASES.items():
        matches = [i for i, value in enumerate(headers) if value in aliases]
        if len(matches) > 1:
            raise ValueError('Неоднозначный столбец: ' + key)
        if matches:
            mapping[key] = matches[0]
    if 'phone' not in mapping:
        raise ValueError('В первой строке нужен заголовок «Телефон»')
    return [{key: row[i] if i < len(row) else '' for key, i in mapping.items()}
            for row in rows[1:] if any(i < len(row) and str(row[i]).strip() for i in mapping.values())]
