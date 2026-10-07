# coding: utf-8
# @title 04.10 | DTC FIX v4 | SSM2 0.16.1 (insert BEFORE 05)
#
# ОТКРЫТИЕ: в diag.dart строка 107:
#   String get titleRu => kSubaruDtcDict[code]?.$1 ?? '';
# Словарь kSubaruDtcDict УЖЕ СУЩЕСТВУЕТ — просто в нём нет кодов P0201 и др.
# Нам нужно: найти файл с kSubaruDtcDict и добавить недостающие записи.
#
# ПЛАН v4:
#  1. Найти файл с kSubaruDtcDict
#  2. Добавить недостающие записи
#  3. Убрать мусор _dtcRu из diag.dart
#  4. Пропатчить _clear(): readAllDtc после clearDtc
#  5. Добавить предупреждение про мотор
import json, re, shutil, subprocess, time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
DTC_PAGE = APP / "lib/dtc_service_page.dart"
DIAG_DART = APP / "lib/diag.dart"
for need in (DTC_PAGE, DIAG_DART, APP / 'build_config.json'):
    if not need.exists(): raise RuntimeError('Нет: ' + str(need))
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / ("ssm2_backup_0410v4_" + STAMP)
BACKUP.mkdir(parents=True, exist_ok=True)

# ============================================================
# 1. Находим файл с kSubaruDtcDict
# ============================================================
dtc_dict_file = None
for f in sorted(APP.rglob('*.dart')):
    try:
        t = f.read_text(encoding='utf-8')
        # Пропускаем diag.dart (там может быть мусор от старых 04.10)
        if 'kSubaruDtcDict' in t and f.name != 'diag.dart':
            dtc_dict_file = f
            print('[found] kSubaruDtcDict в:', f.relative_to(APP))
            # Показываем первые 60 строк файла для диагностики
            for i,l in enumerate(t.splitlines()[:80],1):
                if 'kSubaruDtcDict' in l or 'P020' in l or 'P010' in l:
                    print(f'  {i:4d}: {l}')
    except: pass

if dtc_dict_file is None:
    raise RuntimeError(
        'kSubaruDtcDict не найден ни в одном .dart файле. '
        'Пришлите вывод этой ячейки.')

# ============================================================
# 2. Добавляем недостающие коды в kSubaruDtcDict
# ============================================================
# Структура записи: 'PXXXX': ('Описание', SomeEnum.value)
# Нам нужно выяснить формат записей из уже существующих.
dict_text = dtc_dict_file.read_text(encoding='utf-8')

# Анализируем формат: ищем пример существующей записи
sample = re.search(r"'([A-Z]\d+)':\s*\(([^)]+)\)", dict_text)
if sample:
    print('[format] Пример записи:', sample.group(0)[:80])

# Коды которые надо добавить
MISSING = {
    'P0101': 'ДМРВ — сигнал вне диапазона',
    'P0102': 'ДМРВ — низкий сигнал',
    'P0103': 'ДМРВ — высокий сигнал',
    'P0107': 'ДАД — низкий',
    'P0108': 'ДАД — высокий',
    'P0131': 'Лямбда фронт — вне диапазона',
    'P0132': 'Лямбда фронт — низкое',
    'P0133': 'Лямбда фронт — медленный отклик',
    'P0134': 'Лямбда фронт — нет активности',
    'P0171': 'Смесь слишком бедная',
    'P0172': 'Смесь слишком богатая',
    'P0201': 'Форсунка 1 — обрыв цепи',
    'P0202': 'Форсунка 2 — обрыв цепи',
    'P0203': 'Форсунка 3 — обрыв цепи',
    'P0204': 'Форсунка 4 — обрыв цепи',
    'P0300': 'Пропуски — случайные',
    'P0301': 'Пропуски — цилиндр 1',
    'P0302': 'Пропуски — цилиндр 2',
    'P0303': 'Пропуски — цилиндр 3',
    'P0304': 'Пропуски — цилиндр 4',
    'P0325': 'Датчик детонации — отказ',
    'P0327': 'Датчик детонации — низкий',
    'P0328': 'Датчик детонации — высокий',
    'P0335': 'ДПКВ — нет сигнала',
    'P0340': 'ДПРВ — нет сигнала',
    'P0420': 'Катализатор — низкий КПД',
    'P0442': 'EVAP — малая утечка',
    'P0500': 'Датчик скорости — отказ',
    'P0562': 'Напряжение ЭБУ — низкое',
    'P0563': 'Напряжение ЭБУ — высокое',
    'P2096': 'Коррекция топлива — мала',
    'P2097': 'Коррекция топлива — велика',
    'P2101': 'Дроссель — ошибка привода',
    'P2135': 'Дроссель — несоответствие датчиков',
}

# Фильтруем уже существующие
to_add = {k: v for k, v in MISSING.items() if k not in dict_text}
print('[codes] Отсутствуют в словаре:', len(to_add), 'из', len(MISSING))

if to_add:
    shutil.copyfile(dtc_dict_file, BACKUP / (dtc_dict_file.stem + '.dart.bak'))

    # Определяем формат значения: ($1, $2) — берём из существующей записи
    # Самый общий подход: ищем закрывающую скобку словаря и вставляем перед ней
    # Определяем формат второго поля из уже существующей записи
    # Формат: ('описание', 'категория') — Dart record (String, String)
    # Ищем пример: 'P0030': ("текст", 'engine')
    fmt_match = re.search(r"'[A-Z]\d+':\s*\([^)]+,\s*(['\"][^'\"]*['\"])\)", dict_text)
    if fmt_match:
        second_val = fmt_match.group(1).strip()
        print('[format] Категория из существующей записи:', second_val)
    else:
        second_val = "'engine'"
        print('[format] Категория по умолчанию: engine')

    # Строим строки для вставки
    new_entries = []
    for code, desc in to_add.items():
        new_entries.append('  ' + repr(code) + ': ("' + desc + '", ' + second_val + '),')

    # Ищем конец словаря kSubaruDtcDict: первую }; после его начала
    start = dict_text.find('kSubaruDtcDict')
    brace = dict_text.find('{', start)
    # Ищем закрывающую }; с учётом вложенности
    depth = 0
    pos = brace
    while pos < len(dict_text):
        if dict_text[pos] == '{': depth += 1
        elif dict_text[pos] == '}':
            depth -= 1
            if depth == 0: break
        pos += 1
    # Вставляем перед закрывающей }
    insert = '\n' + '\n'.join(new_entries) + '\n'
    new_text = dict_text[:pos] + insert + dict_text[pos:]
    dtc_dict_file.write_text(new_text, encoding='utf-8')
    print('[OK] Добавлено', len(to_add), 'кодов в', dtc_dict_file.name)
else:
    print('[SKIP] Все коды уже есть в словаре')

# ============================================================
# 2б. Исправляем записи с null (от прошлых прогонов 04.10)
# ============================================================
# Прошлые версии вставляли ('Текст', null) — это неверный тип для (String, String).
# Берём категорию из первой нормальной записи и заменяем null на неё.
dict_text = dtc_dict_file.read_text(encoding='utf-8')
if ', null)' in dict_text:
    shutil.copyfile(dtc_dict_file, BACKUP / (dtc_dict_file.stem + '.null_fix.bak'))
    # Берём реальную категорию из первой нормальной записи
    cat_match = re.search(r"'[A-Z]\d+':\s*\([^)]+,\s*(['\"][^'\"]+['\"])\)", dict_text)
    category = cat_match.group(1) if cat_match else "'engine'"
    print('[fix] Заменяем null -> ' + category + ' в записях')
    # Заменяем , null) на , category) во всех строках словаря
    fixed = re.sub(r', null\)', ', ' + category + ')', dict_text)
    dtc_dict_file.write_text(fixed, encoding='utf-8')
    print('[OK] null исправлен в', dict_text.count(', null)'), 'записях')
else:
    print('[OK] null-записей нет')

# ============================================================
# 3. Убираем мусор _dtcRu/_kDtcRu из diag.dart
# Метод: просто отфильтровываем все строки содержащие эти имена.
# _kDtcRu — словарь, _dtcRu — функция. Оба не нужны.
# ============================================================
diag = DIAG_DART.read_text(encoding='utf-8')
diag_changed = False

# Шаг A: убираем строки с именами _dtcRu/_kDtcRu
if '_dtcRu' in diag or '_kDtcRu' in diag:
    shutil.copyfile(DIAG_DART, BACKUP / 'diag.dart.bak')
    bad = {'_dtcRu', '_kDtcRu'}
    diag = '\n'.join(ln for ln in diag.split('\n')
                      if not any(b in ln for b in bad))
    diag_changed = True
    print('[cleanup-A] _dtcRu/_kDtcRu строки удалены')

# Шаг B: убираем «висячие» строки словаря без объявления.
# Признак: строка вида   'Pxxxx': 'текст', — внутри класса или на уровне файла,
# а сразу после идёт }; (закрывающая без const ... = {).
# Ищем блок: от первой такой строки вверх до ближайшей пустой/конца предыдущего блока.
import re
# Находим '};' которая закрывает «ничего» (без парной const ... = {)
lines = diag.split('\n')
# Ищем висячие записи: строка содержит pattern 'Pdddd': 'текст' но НЕ содержит const
orphan_pattern = re.compile(r"^\s*'[A-Z]\d{4}':\s*'[^']+',?\s*$")
closing_brace = re.compile(r'^\s*\};\s*$')
new_lines = []
i = 0
while i < len(lines):
    ln = lines[i]
    if orphan_pattern.match(ln):
        # Нашли висячую запись — ищем }; вперёд и пропускаем весь блок
        j = i
        while j < len(lines) and not closing_brace.match(lines[j]):
            j += 1
        if j < len(lines):
            print('[cleanup-B] Удалены висячие строки', i+1, '-', j+2)
            i = j + 1  # пропускаем включая };
            diag_changed = True
            continue
    new_lines.append(ln)
    i += 1
diag = '\n'.join(new_lines)

if diag_changed:
    DIAG_DART.write_text(diag, encoding='utf-8')
    print('[cleanup] diag.dart очищен')
else:
    print('[OK] diag.dart уже чист')

# ============================================================
# 4. Патч dtc_service_page.dart
# ============================================================
text = DTC_PAGE.read_text(encoding='utf-8')
shutil.copyfile(DTC_PAGE, BACKUP / 'dtc_service_page.dart.bak')
changed = False

# 4.1 Предупреждение ЗАГЛУШИТЕ ДВИГАТЕЛЬ в диалоге сброса
W = chr(9888)
OLD_WARN = 'Будут стёрты коды, стоп-кадры и мониторы.'
NEW_WARN = W + ' ЗАГЛУШИТЕ ДВИГАТЕЛЬ!\nСброс на заведённом моторе — мотор ЗАГЛОХНЕТ.\n\nБудут стёрты коды, стоп-кадры и мониторы.'
if OLD_WARN in text and W not in text:
    text = text.replace(OLD_WARN, NEW_WARN, 1)
    print('[patch] Предупреждение про мотор добавлено'); changed = True

# 4.2 readAllDtc после clearDtc (точная строка из реального файла)
OLD_C = ('final r = await session.clearDtc(slot);\n'
         '      if (mounted) setState(() => _results[slot.name] = r);')
NEW_C = ('await session.clearDtc(slot);\n'
         '      final fresh = await session.readAllDtc(slot);  // v0.16.1\n'
         '      if (mounted) setState(() => _results[slot.name] = fresh);')
if OLD_C in text:
    text = text.replace(OLD_C, NEW_C, 1)
    print('[patch] readAllDtc после clearDtc'); changed = True
elif 'fresh' in text:
    print('[SKIP] readAllDtc уже применён')

if changed:
    DTC_PAGE.write_text(text, encoding='utf-8')
    print('[OK] dtc_service_page.dart сохранён')

# ============================================================
# 5. Format + Strict Analyze
# ============================================================
cfg = json.loads((APP / 'build_config.json').read_text(encoding='utf-8'))
DART = Path(cfg['flutter']) / 'bin/dart'
FLUTTER = Path(cfg['flutter']) / 'bin/flutter'
subprocess.run([str(DART), 'fix', '--apply'], cwd=APP, capture_output=True)
subprocess.run([str(DART), 'format',
                str(dtc_dict_file.relative_to(APP)),
                'lib/dtc_service_page.dart', 'lib/diag.dart'], cwd=APP)
smoke = subprocess.run([str(FLUTTER), 'analyze', '--no-pub', '--no-fatal-infos'],
                       cwd=APP, capture_output=True, text=True)
out = smoke.stdout + smoke.stderr
print(out[-3000:])
if smoke.returncode:
    raise RuntimeError('Analyze упал. Пришлите вывод выше.')
print()
print('=== OK: DTC FIX v4 ===')
print('P0201 теперь: P0201 · Форсунка 1 — обрыв цепи')
print('СБРОС: предупреждение + авто-обновление экрана')
print('Запустите ячейку 05.')