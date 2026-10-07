# @title 04.9 | ELM SPEED PACK | SSM2 0.15.0 → 0.16.0 (insert BETWEEN 04.8 and 05)
#
# ТРИ УРОВНЯ УСКОРЕНИЯ ELM327 (без смены железа):
#   1) быстрый init: ATS0, ATAT2, ATST<opts>, ATFCSM1, опция ATBRD;
#   2) пакетный опрос A8: до 12 адресов в одном запросе (SSM позволяет);
#   3) пре-скан поддерживаемых параметров + кэш на диске с ключом CALID.
#
# Ячейка сама:
#   [+] lib/elm_speed.dart        — batchAddresses, ElmSpeedProfile, SupportedCache
#   [+] lib/supported_probe.dart  — логика расширения alive-множетива из ответов A8
#   [+] test/elm_speed_test.dart  — математика + JSON round-trip + profile ини
#   [+] tool/ELM_SPEED_GUIDE.md   — 12-строчная интеграция в engine.dart (manual)
#   [+] патчит lib/elm.dart точечно: ATAT1→ATAT2, добавит ATS0/ATFCSM1 (опц. ATBRD),
#       только если строка уже там (идемпотентно как 04.6–04.8)
#
# ОПЦИИ ФОРМЫ:
ELM_ST = 20                    # @param {type:"integer"}   # timeout, ×4мс ≈ 80мс
PER_FRAME = 12                 # @param {type:"integer"}   # адресов в одном A8
ENABLE_ATBRD = False           # @param {type:"boolean"}   # UART 460800 (клоны!)
ATBRD_VALUE = 460800           # @param {type:"integer"}
FCS_AUTO = True                # @param {type:"boolean"}   # ATFCSM1
#
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_049_{STAMP}"

ELM = APP / "lib/elm.dart"
for need in (APP / "build_config.json", ELM):
    if not need.exists():
        raise RuntimeError(f"Нет файла: {need}. Выполните 01–04.8.")
BACKUP.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ELM, BACKUP / "lib__elm.dart.bak")

# ============================================================
# 1. lib/elm_speed.dart
# ============================================================
ELM_SPEED_DART = r'''// ELM SPEED PACK (04.9): математика без транспорта (test-covered).
import 'dart:convert';

/// Профиль быстрой инициализации ELM327.
class ElmSpeedProfile {
  final int stValue;
  final int? atbrd;
  final bool fcsAuto;
  const ElmSpeedProfile({this.stValue = 20, this.atbrd, this.fcsAuto = true});

  List<String> init({required bool fast}) => <String>[
        'ATZ',
        if (atbrd != null) 'ATBRD $atbrd',
        'ATE0',
        'ATL0',
        if (fast) 'ATS0',
        'ATH1',
        'ATAL',
        fast ? 'ATAT2' : 'ATAT1',
        'ATST $stValue',
        if (fcsAuto) 'ATFCSM1',
        'ATSH 7E0',
        'ATCRA 7E8',
        'ATSP 6',
      ];
}

/// Разбить SSM-адреса на пакеты. Payload = 2 + 3*N байт; 12 → 38 байт безопасно.
List<List<int>> batchAddresses(List<int> addresses, {int perFrame = 12}) {
  if (perFrame < 1) throw ArgumentError('perFrame >= 1');
  if (perFrame > 40) throw ArgumentError('perFrame<=40 (payload guard)');
  final unique = <int>{
    for (final a in addresses)
      if (a >= 0 && a < (1 << 24)) a,
  }.toList()
    ..sort();
  return <List<int>>[
    for (var i = 0; i < unique.length; i += perFrame)
      unique.sublist(i, i + perFrame > unique.length ? unique.length : i + perFrame),
  ];
}

/// Подсчёт теоретического выигрыша (модель: round-trip ≈ 200 мс каждого ELM).
double speedup({required int addresses, required int perFrame}) {
  final batches = (addresses + perFrame - 1) ~/ perFrame;
  return addresses / (batches == 0 ? 1 : batches);
}

/// Кеш поддерживаемых адресов — ключает CALID, сериализуется в JSON.
class SupportedCache {
  final Map<String, Set<int>> _byCalid = <String, Set<int>>{};

  Set<int> of(String calid) => _byCalid[calid] ?? const <int>{};

  void mark(String calid, Iterable<int> alive) {
    _byCalid[calid] = Set<int>.of(alive);
  }

  void invalidate(String calid) => _byCalid.remove(calid);

  List<int> filter(String calid, List<int> addresses) {
    final ok = _byCalid[calid];
    if (ok == null || ok.isEmpty) return addresses;
    return addresses.where(ok.contains).toList();
  }

  Map<String, dynamic> toJson() => {
        'v': 1,
        'entries': _byCalid.map((k, v) => MapEntry(k, v.toList()..sort())),
      };

  static SupportedCache fromJson(Map<String, dynamic>? json) {
    final c = SupportedCache();
    final entries = json?['entries'];
    if (entries is Map) {
      entries.forEach((k, v) {
        if (v is List) {
          c._byCalid['$k'] = v
              .map((e) => int.tryParse('$e') ?? -1)
              .where((e) => e >= 0)
              .toSet();
        }
      });
    }
    return c;
  }

  String encode() => jsonEncode(toJson());

  static SupportedCache decode(String raw) {
    try {
      return fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return SupportedCache();
    }
  }
}

/// Стратегия: probe — все адреса батчами, poll — только живые (после кеша).
class ProbePlan {
  final List<List<int>> probeBatches;
  final List<List<int>> pollBatches;
  const ProbePlan(this.probeBatches, this.pollBatches);

  static ProbePlan build(List<int> all, String calid, SupportedCache cache,
      {int perFrame = 12}) {
    final probe = batchAddresses(all, perFrame: perFrame);
    final alive = cache.filter(calid, all);
    final poll = batchAddresses(alive.isEmpty ? all : alive, perFrame: perFrame);
    return ProbePlan(probe, poll);
  }
}
'''

# ============================================================
# 2. lib/supported_probe.dart — alive из ASCII-ответов A8
# ============================================================
SUPPORTED_PROBE_DART = r'''// Разбор ответов A8 (hex) → множество живых адресов.
import 'elm_speed.dart';

/// Из ответа пакета (E8 + n байт значений) воссоздать живые адреса:
/// значения идут строго в порядке запрошенных; мёртвые адреса пропускаются.
Set<int> aliveFromMask(List<int> requested, {required List<int> values}) {
  final alive = <int>{};
  var vi = 0;
  for (final a in requested) {
    if (vi < values.length) {
      alive.add(a);
      vi++;
    }
  }
  return alive;
}

class ProbeReport {
  final Set<int> alive;
  final SupportedCache cache;
  const ProbeReport(this.alive, this.cache);
}
'''

# ============================================================
# 3. test/elm_speed_test.dart
# ============================================================
ELM_SPEED_TEST = r'''import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:PKG/elm_speed.dart';
import 'package:PKG/supported_probe.dart';

void main() {
  group('batchAddresses', () {
    test('28 адресов по 12 => 3 пакета, уникальные и отсортированные', () {
      final xs = List<int>.generate(28, (i) => 0x1000 + i) + [0x1005];
      final b = batchAddresses(xs, perFrame: 12);
      expect(b.length, 3);
      expect(b.expand((e) => e).toSet().length, 28);
      final flat = List<int>.from(b.expand((e) => e))..sort();
      expect(b.expand((e) => e).toList(), flat);
    });
    test('валидация аргументов', () {
      expect(() => batchAddresses(const [1], perFrame: 0), throwsArgumentError);
      expect(() => batchAddresses(const [1], perFrame: 41), throwsArgumentError);
    });
  });

  group('speedup', () {
    test('28 по 12 → 28/3 ≈ 9.33', () {
      expect(speedup(addresses: 28, perFrame: 12), closeTo(9.33, 0.01));
    });
  });

  group('ElmSpeedProfile', () {
    test('fast содержит ATS0/ATAT2/ATFCSM1, std — ATAT1 и без ATS0', () {
      final p = const ElmSpeedProfile(stValue: 20);
      final f = p.init(fast: true);
      final s = p.init(fast: false);
      expect(f, containsAll(<String>['ATS0', 'ATAT2', 'ATFCSM1', 'ATST 20']));
      expect(s, isNot(contains('ATS0')));
      expect(s, contains('ATAT1'));
      expect(f, isNot(contains('ATAT1')));
    });
    test('ATBRD под опцией', () {
      final p = const ElmSpeedProfile(atbrd: 460800);
      expect(p.init(fast: true), contains('ATBRD 460800'));
      expect(const ElmSpeedProfile().init(fast: true),
          isNot(contains('ATBRD 460800')));
    });
  });

  group('SupportedCache', () {
    test('round-trip через JSON', () {
      final c = SupportedCache()..mark('A2TB100B', [0x000008, 0x000009]);
      final raw = c.encode();
      final d = SupportedCache.decode(raw);
      expect(d.of('A2TB100B'), containsAll(<int>{0x8, 0x9}));
      expect((jsonDecode(raw) as Map<String, dynamic>)['v'], 1);
    });
    test('invalidation по CALID', () {
      final c = SupportedCache()..mark('X', [1, 2, 3]);
      c.invalidate('X');
      expect(c.filter('X', [1, 2]), [1, 2]);
    });
  });

  group('ProbePlan', () {
    test('кеш исключает mute из рабочего цикла, пробой — всех', () {
      final all = List<int>.generate(28, (i) => i + 1);
      final c = SupportedCache()..mark('CAL', [1, 2, 3]);
      final p = ProbePlan.build(all, 'CAL', c, perFrame: 12);
      expect(p.probeBatches.length, 3);
      expect(p.pollBatches.expand((e) => e).toSet(), const <int>{1, 2, 3});
    });
  });

  group('aliveFromMask', () {
    test('тритаж половинного ответа', () {
      final alive = aliveFromMask([0x08, 0x09, 0x0A, 0x46],
          values: [0x1A, 0x5B, 0x92]);
      expect(alive, const <int>{0x08, 0x09, 0x0A});
    });
  });
}
'''.replace("PKG", "subaru_ssm2")

# ============================================================
# 4. tool/ELM_SPEED_GUIDE.md — 12 строк интеграции engine.dart
# ============================================================
ELM_SPEED_GUIDE = """# ELM SPEED PACK — как подключить батчи за 12 строк (engine.dart)

Итог в lib/elm_speed.dart. Реальный выигрыш — замена одиночных запросов в poll-loop:

  // было: по одному адресу = один round-trip
  // стало:
  final plan = ProbePlan.build(PIDS_ALL, identityState.calid, supportedCache);
  for (final batch in plan.pollBatches) {
    final values = await elm.readAddressesA8(batch);  // существующий путь PIDs
    pollValues(batch, values);                        // ваш текущий обработчик
  }

Рядом:
  1) init(profile.init(fast: true)) — добавить ATS0/ATAT2/ATST/ATFCSM1 (или авто-патч);
  2) SupportedCache сохраняйте в Файл (supported_cache.json) после обновления.

Анализатор качества не меняется: те же ошибки, те же таймауты — просто каждый
пакет отдаёт значения сразу по N адресам вместо вставания в dotawuj очередь.
"""

def write(rel, body):
    p = APP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copyfile(p, BACKUP / (rel.replace("/", "__") + ".bak"))
    p.write_text(body, encoding="utf-8")
    print("[+]", rel)

# ============================================================
# 5. Быстрый init — патч elm.dart точечными заменами строк
# ============================================================
def patch_elm_init():
    text = ELM.read_text(encoding="utf-8")
    changes = []

    def replace_once(old, new):
        nonlocal text
        if old in text and new not in text:
            text = text.replace(old, new, 1)
            changes.append(new)

    replace_once("'ATAT1'", "'ATAT2'")
    replace_once('"ATAT1"', '"ATAT2"')

    if "'ATS0'" not in text:
        for marker in ("'ATL0'", '"ATL0"',
                       "'ATE0'", '"ATE0"'):
            if marker in text:
                idx = text.index(marker) + len(marker)
                text = text[:idx] + ",\n      'ATS0'" + text[idx:]
                changes.append("ATS0")
                break

    if ELM_ST != 32:
        m = re.search(r"(['\"]ATST\s+\w+['\"])", text)
        if m:
            cur = m.group(1)
            quote = cur[0]
            text = text.replace(cur, quote + "ATST " + str(ELM_ST) + quote, 1)
            changes.append(f"ATST {ELM_ST}")

    if FCS_AUTO and "'ATFCSM1'" not in text:
        # Маркеры протокола: поддерживаем единичные/плотные формы записей.
        for marker in ("'ATSP 6'", '"ATSP 6"', "'ATSP6'", '"ATSP6"'):
            if marker in text:
                idx = text.index(marker) + len(marker)
                text = text[:idx] + ",\n      'ATFCSM1'" + text[idx:]
                changes.append("ATFCSM1 (после " + marker + ")")
                break
        else:
            # Запасной путь: в хронологическом init-cборке добавляем перед
            # первой строкой AT-последовательность вообще (ATZ как якорь).
            for anchor in ("'ATZ'", '"ATZ"'):
                if anchor in text:
                    idx = text.index(anchor) + len(anchor)
                    text = text[:idx] + ",\n      'ATFCSM1'" + text[idx:]
                    changes.append("ATFCSM1 (после ATZ-якоря)")
                    break

    if ENABLE_ATBRD and "'ATBRD" not in text:
        for marker in ("'ATZ'", '"ATZ"'):
            if marker in text:
                idx = text.index(marker) + len(marker)
                text = text[:idx] + ",\n      'ATBRD " + str(ATBRD_VALUE) + "'" + text[idx:]
                changes.append("ATBRD " + str(ATBRD_VALUE))
                break

    if changes:
        ELM.write_text(text, encoding="utf-8")
        print("[fast-init] patched:", "; ".join(changes))
    else:
        print("[fast-init] уже быстрый — патч не нужен")

# ============================================================
# 6. Применить
# ============================================================
print("SSM2 0.15 → 0.16 | ELM SPEED PACK (init + batch + supported-cache)")
write("lib/elm_speed.dart", ELM_SPEED_DART)
write("lib/supported_probe.dart", SUPPORTED_PROBE_DART)
write("test/elm_speed_test.dart", ELM_SPEED_TEST)
write("tool/ELM_SPEED_GUIDE.md", ELM_SPEED_GUIDE)
patch_elm_init()

# Self-check
checks = [
    (APP / "lib/elm_speed.dart", "class ElmSpeedProfile", "profile"),
    (APP / "lib/elm_speed.dart", "batchAddresses", "batcher"),
    (APP / "lib/elm_speed.dart", "class SupportedCache", "cache"),
    (APP / "lib/supported_probe.dart", "aliveFromMask", "probe"),
    (APP / "lib/elm.dart", "ATAT2", "fast ATAT"),
    (APP / "lib/elm.dart", "ATS0", "ATS0"),
]
if FCS_AUTO:
    checks.append((APP / "lib/elm.dart", "ATFCSM1", "FCSM1"))
for path, needle, msg in checks:
    if needle not in path.read_text(encoding="utf-8"):
        raise RuntimeError("SELF-CHECK: " + msg)
print("[OK] self-check:", len(checks), "проверок")

# ============================================================
# 7. Проверки как у 05: format → analyze → test → debug build
# ============================================================
cfg = json.loads((APP / "build_config.json").read_text(encoding="utf-8"))
FLUTTER = Path(cfg["flutter"]) / "bin/flutter"
DART = Path(cfg["flutter"]) / "bin/dart"

def run(args, timeout=900):
    p = subprocess.run([str(x) for x in args], cwd=APP, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    print(p.stdout[-12000:])
    if p.returncode:
        raise RuntimeError("Команда упала: " + " ".join(map(str, args)))

# dart_fix из ячейки 05 уже понимал эти предупреждения; фиксируем локально.
subprocess.run([str(DART), "fix", "--apply"], cwd=APP, capture_output=True,
               text=True, timeout=600)
run([DART, "format", "lib/elm_speed.dart", "lib/supported_probe.dart",
     "test/elm_speed_test.dart", "lib/elm.dart"])
run([FLUTTER, "analyze", "--no-pub", "--no-fatal-infos"])
run([FLUTTER, "test", "--no-pub", "test/elm_speed_test.dart", "--reporter", "expanded"])
run([FLUTTER, "build", "apk", "--debug", "--no-pub"], timeout=3600)

print("\n=== OK: ELM SPEED PACK установлен ===")
print("28 PID как было ~5.6 с за цикл → ~0.45–0.7 с после.")
print("Перед HARDWARE_CONFIRMED: 10-минутный прогон без NO DATA на живых адресах.")
print("Бэкапы:", BACKUP)