# @title 04.7 FIX DOUBLE
from pathlib import Path
import re
import shutil
import time

p = Path("/content/subaru_ssm2_fixed/lib/protocol.dart")
text = p.read_text(encoding="utf-8")

pattern = (
    r"(?m)^[ \t]*'02 02 00'[ \t]*,[ \t]*//[ \t]*"
    r"стоп-кадр \(повтор безопасен\)[ \t]*(?:\r?\n|$)"
)
m = re.search(pattern, text)

if m is None:
    print("[SKIP] Добавленная строка отсутствует; файл не изменён.")
else:
    start = text.rfind("{", 0, m.start())
    end = text.find("}", m.end())
    if start < 0 or end < 0:
        raise RuntimeError("Не удалось определить границы набора; файл не изменён.")

    block = text[start:end]
    count = len(re.findall(
        r"(?m)^[ \t]*'02 02 00'[ \t]*,", block
    ))
    if count != 2:
        raise RuntimeError(
            f"Ожидались две записи '02 02 00', найдено {count}. "
            "Предположение о дубле не подтверждено; файл не изменён."
        )

    backup = Path(
        f"/content/protocol_before_fix_{time.strftime('%Y%m%d_%H%M%S')}.dart.bak"
    )
    shutil.copy2(p, backup)
    p.write_text(text[:m.start()] + text[m.end():], encoding="utf-8")

    print("[OK] Удалена повторная строка из 04.5; первая запись сохранена.")
    print("Бэкап:", backup)