# ===== Шапка 04.4: что чинится и добавляется =====
# @title 04.4 | SERVICE FULL & TILES | SSM2 0.12 >> 0.13 (вставить МЕЖДУ 04.3 и 05)
# Исправляет и добавляет:
#  1. БАГ 04.2: EXPERIMENTAL был compile-time const -> ECUReset/Clear Memory
#     всегда серые. Теперь рантайм-переключатель в UI + сохранение в настройках.
#  2. UDS-сбросы требуют расширенной сессии (10 03) — раньше её не было,
#     блок отвечал 7F ... 7F/24. Теперь сессия открывается автоматически.
#  3. Полный набор сервисов: pending (07), permanent (0A), стоп-кадр (02),
#     мониторы готовности (01 01) с расшифровкой, VIN (09 02 / 22 F1 90),
#     CALID (09 04), TesterPresent (3E 00), скан всех блоков, проба групп
#     Clear Memory, расшифровка всех негативных ответов (NRC).
#  4. Виртуальные плитки дашборда: мгновенный расход л/ч и л/100км, одометр
#     поездки, литры, средний расход, IDC форсунок, запас насоса, оценка
#     мощности и момента, лямбда, суммарная коррекция детонации, дельта наддува.
# Идемпотентна, бэкап снаружи проекта, самопроверка маркеров.

import json
import re
import shutil
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")

STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_044_{STAMP}"
FILES = {}
REWRITES = []

# ===== Полный сервисный движок: pending/permanent, стоп-кадр, мониторы, VIN, сессии, NRC =====
FILES["lib/diag.dart"] = r'''
import 'dtc_dict.dart';
import 'elm.dart';
import 'protocol.dart';

/// Поколение протокола ECU.
enum DiagProto { ssm2, obd, uds }

/// Блок управления Subaru на шине CAN.
class EcuSlot {
  const EcuSlot(this.name, this.header, this.responseId, this.proto);
  final String name;
  final String header; // ATSH
  final String responseId; // ATCRA
  final DiagProto proto;

  static const engine = EcuSlot('Двигатель (ECM)', '7E0', '7E8', DiagProto.ssm2);
  static const tcu = EcuSlot('АКПП / CVT (TCM)', '7E1', '7E9', DiagProto.ssm2);
  static const vdc = EcuSlot('ABS / VDC', '7B0', '7B8', DiagProto.uds);
  static const srs = EcuSlot('Подушки (SRS)', '772', '77A', DiagProto.uds);
  static const eps = EcuSlot('ЭУР (EPS)', '7A6', '7AE', DiagProto.uds);
  static const biu = EcuSlot('Кузовной блок (BIU)', '723', '72B', DiagProto.uds);

  static const List<EcuSlot> all = [engine, tcu, vdc, srs, eps, biu];
}

/// Одна запись неисправности.
class DiagDtc {
  DiagDtc(this.code, this.statusByte, this.proto, {this.origin = ''});
  final String code;
  final int statusByte;
  final DiagProto proto;

  /// Откуда прочитан код: stored / pending / permanent.
  final String origin;

  bool get active => proto == DiagProto.uds
      ? (statusByte & 0x01) != 0
      : proto == DiagProto.obd
          ? origin != 'permanent'
          : (statusByte & 0x20) != 0;
  bool get confirmed => proto == DiagProto.uds ? (statusByte & 0x08) != 0 : true;
  bool get milOn => proto == DiagProto.uds
      ? (statusByte & 0x40) != 0
      : proto == DiagProto.obd
          ? false
          : (statusByte & 0x80) != 0;

  String get statusText {
    final prefix = origin.isEmpty ? '' : '$origin · ';
    if (proto == DiagProto.uds) {
      final f = <String>[];
      if (statusByte & 0x01 != 0) f.add('есть сейчас');
      if (statusByte & 0x08 != 0) f.add('подтверждена');
      if (statusByte & 0x40 != 0) f.add('MIL');
      return prefix +
          (f.isEmpty ? 'пассивная (0x${hex2(statusByte)})' : f.join(' · '));
    }
    if (proto == DiagProto.obd) return '${prefix}OBD mode';
    return prefix + (active ? (milOn ? 'активная · MIL' : 'активная') : 'сохранённая');
  }

  String get titleRu => kSubaruDtcDict[code]?.$1 ?? '';
}

/// Статус мониторов готовности OBD (mode 01 PID 01).
class ReadinessReport {
  ReadinessReport({
    required this.milOn,
    required this.dtcCount,
    required this.monitors,
  });
  final bool milOn;
  final int dtcCount;

  /// имя монитора -> готов ли (true = завершён)
  final Map<String, bool> monitors;

  int get ready => monitors.values.where((v) => v).length;
  int get total => monitors.length;
}

/// Декодер двухбайтового DTC.
String decodeDtcBytes(int b1, int b2) {
  const systems = ['P', 'C', 'B', 'U'];
  final sys = systems[(b1 >> 6) & 0x03];
  final d1 = (b1 >> 4) & 0x03;
  String h(int v) => v.toRadixString(16).toUpperCase();
  return '$sys$d1${h(b1 & 0x0F)}${h((b2 >> 4) & 0x0F)}${h(b2 & 0x0F)}';
}

/// SSM2: 58 <count> <b1> <b2> <status> × n
List<DiagDtc> parseSsm2DtcReply(List<int> bytes) {
  final out = <DiagDtc>[];
  if (bytes.isEmpty || bytes[0] != 0x58) return out;
  final count = bytes.length > 1 ? bytes[1] : 0;
  for (var i = 0; i < count; i++) {
    final base = 2 + i * 3;
    if (base + 2 >= bytes.length) break;
    final b1 = bytes[base], b2 = bytes[base + 1], st = bytes[base + 2];
    if (b1 == 0 && b2 == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b1, b2), st, DiagProto.ssm2, origin: 'stored'));
  }
  return out;
}

/// OBD mode 03/07/0A: <0x43|0x47|0x4A> <b1> <b2> × n
List<DiagDtc> parseObdDtcReply(List<int> bytes, {String origin = 'stored'}) {
  const heads = <int, String>{0x43: 'stored', 0x47: 'pending', 0x4A: 'permanent'};
  final out = <DiagDtc>[];
  if (bytes.isEmpty || !heads.containsKey(bytes[0])) return out;
  final kind = heads[bytes[0]] ?? origin;
  for (var i = 1; i + 1 < bytes.length; i += 2) {
    final b1 = bytes[i], b2 = bytes[i + 1];
    if (b1 == 0 && b2 == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b1, b2), 0x20, DiagProto.obd, origin: kind));
  }
  return out;
}

/// Совместимость с 04.2: алиас старого имени.
List<DiagDtc> parseObdMode3Reply(List<int> bytes) => parseObdDtcReply(bytes);

/// UDS 19 02: 59 02 <mask> <b1> <b2> <status> × n
List<DiagDtc> parseUdsDtcReply(List<int> bytes) {
  final out = <DiagDtc>[];
  if (bytes.length < 3 || bytes[0] != 0x59) return out;
  for (var i = 3; i + 2 < bytes.length; i += 3) {
    final b1 = bytes[i], b2 = bytes[i + 1], st = bytes[i + 2];
    if (b1 == 0 && b2 == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b1, b2), st, DiagProto.uds, origin: 'stored'));
  }
  return out;
}

/// mode 01 PID 01 -> 41 01 A B C D
ReadinessReport? parseReadiness(List<int> bytes) {
  if (bytes.length < 6 || bytes[0] != 0x41 || bytes[1] != 0x01) return null;
  final a = bytes[2], b = bytes[3], c = bytes[4], d = bytes[5];
  final monitors = <String, bool>{
    // B: непрерывные мониторы (bit 0..2 доступен, bit 4..6 НЕ завершён)
    'Пропуски зажигания': (b & 0x01) == 0 || (b & 0x10) == 0,
    'Топливная система': (b & 0x02) == 0 || (b & 0x20) == 0,
    'Компоненты': (b & 0x04) == 0 || (b & 0x40) == 0,
  };
  // C: доступность, D: незавершённость (бит=1 -> НЕ готов)
  const names = <int, String>{
    0x01: 'Катализатор',
    0x02: 'Подогрев катализатора',
    0x04: 'EVAP (адсорбер)',
    0x08: 'Вторичный воздух',
    0x20: 'Датчики O2',
    0x40: 'Подогрев O2',
    0x80: 'EGR / VVT',
  };
  names.forEach((mask, label) {
    if ((c & mask) != 0) monitors[label] = (d & mask) == 0;
  });
  return ReadinessReport(
    milOn: (a & 0x80) != 0,
    dtcCount: a & 0x7F,
    monitors: monitors,
  );
}

/// mode 09: 49 <pid> <count> <ascii...> — VIN (02) / CALID (04)
String? parseAsciiReply(List<int> bytes, int pid) {
  if (bytes.length < 3 || bytes[0] != 0x49 || bytes[1] != pid) return null;
  final chars = <int>[];
  for (var i = 3; i < bytes.length; i++) {
    final b = bytes[i];
    if (b >= 0x20 && b <= 0x7E) chars.add(b);
  }
  final text = String.fromCharCodes(chars).trim();
  return text.isEmpty ? null : text;
}

/// mode 02: 42 02 00 <b1> <b2> — DTC, вызвавший стоп-кадр.
String? parseFreezeFrameDtc(List<int> bytes) {
  if (bytes.length < 5 || bytes[0] != 0x42) return null;
  final b1 = bytes[3], b2 = bytes[4];
  if (b1 == 0 && b2 == 0) return null;
  return decodeDtcBytes(b1, b2);
}

/// Универсальный парсер «сырого» ответа ELM.
List<int>? parseDiagPayload(String text) {
  final flat = <int>[];
  for (final rawLine in text.split(RegExp(r'[\r\n]+'))) {
    var line = rawLine.trim();
    if (line.isEmpty) continue;
    if (RegExp(r'^\d+:').hasMatch(line)) {
      line = line.substring(line.indexOf(':') + 1).trim();
    }
    for (final t in line.split(RegExp(r'\s+')).where((x) => x.isNotEmpty)) {
      if (!RegExp(r'^[0-9A-Fa-f]{2,16}$').hasMatch(t) || t.length % 2 != 0) {
        return null;
      }
      for (var i = 0; i + 1 < t.length; i += 2) {
        flat.add(int.parse(t.substring(i, i + 2), radix: 16));
      }
    }
  }
  if (flat.isEmpty) return null;
  var b = flat;
  if (b.length > 1 && (b[0] & 0xF0) == 0 && (b[0] & 0x0F) <= 7) {
    final len = b[0] & 0x0F;
    b = b.sublist(1, 1 + len <= b.length ? 1 + len : b.length);
  }
  if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) {
    b = b.sublist(2);
  }
  return b.isEmpty ? null : b;
}

const Map<int, String> kNrMeanings = <int, String>{
  0x10: 'generalReject — блок отклонил запрос',
  0x11: 'serviceNotSupported — сервис не поддерживается',
  0x12: 'subFunctionNotSupported — подфункция не поддерживается',
  0x13: 'incorrectMessageLength — неверная длина',
  0x21: 'busyRepeatRequest — блок занят',
  0x22: 'conditionsNotCorrect — не выполнены условия (зажигание/обороты)',
  0x24: 'requestSequenceError — нужна сессия 10 03',
  0x31: 'requestOutOfRange — параметр вне диапазона',
  0x33: 'securityAccessDenied — нужен ключ дилера',
  0x7E: 'subFunctionNotSupportedInActiveSession',
  0x7F: 'serviceNotSupportedInActiveSession — нужна сессия 10 03',
  0x78: 'responsePending — блок думает',
};

/// Результат сервисной операции для UI.
class ServiceResult {
  ServiceResult.ok(this.title, this.detail)
      : success = true,
        negative = null;
  ServiceResult.fail(this.title, this.detail, {this.negative}) : success = false;
  final bool success;
  final String title;
  final String detail;
  final int? negative;
}

/// Сессия диагностики поверх ElmDriver.diagnostic.
class DiagSession {
  DiagSession(this.elm);
  final ElmDriver elm;

  /// v0.13: рантайм-флаг (раньше был compile-time const и не включался).
  bool experimental = false;

  Future<void> _atEcu(EcuSlot slot) async {
    await elm.diagnostic('ATSH ${slot.header}');
    await elm.diagnostic('ATCRA ${slot.responseId}');
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }

  Future<List<int>> _payload(String command) async {
    final reply = await elm.diagnostic(command);
    return parseDiagPayload(reply) ?? const <int>[];
  }

  int? _negativeCode(List<int> b) =>
      (b.length >= 3 && b[0] == 0x7F) ? b[2] : null;

  // ------------------------- ЧТЕНИЕ ОШИБОК -------------------------

  /// Сохранённые DTC: SSM2 0x18 -> OBD 03 -> UDS 19 02 AF.
  Future<List<DiagDtc>> readDtc(EcuSlot slot) async {
    await _atEcu(slot);
    if (slot.proto == DiagProto.ssm2) {
      try {
        final b = await _payload('18 00 FF 00');
        if (b.isNotEmpty && b[0] == 0x58) return parseSsm2DtcReply(b);
      } catch (_) {}
      try {
        return parseObdDtcReply(await _payload('03'));
      } catch (_) {}
      return const [];
    }
    final b = await _payload('19 02 AF');
    if (b.isNotEmpty && b[0] == 0x59) return parseUdsDtcReply(b);
    try {
      return parseObdDtcReply(await _payload('03'));
    } catch (_) {
      return const [];
    }
  }

  /// Текущие (pending) — OBD mode 07.
  Future<List<DiagDtc>> readPendingDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseObdDtcReply(await _payload('07'), origin: 'pending');
  }

  /// Постоянные (permanent) — OBD mode 0A. Не стираются сбросом!
  Future<List<DiagDtc>> readPermanentDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseObdDtcReply(await _payload('0A'), origin: 'permanent');
  }

  /// Все три категории разом.
  Future<List<DiagDtc>> readAllDtc(EcuSlot slot) async {
    final out = <DiagDtc>[...await readDtc(slot)];
    for (final fn in [readPendingDtc, readPermanentDtc]) {
      try {
        for (final d in await fn(slot)) {
          if (!out.any((x) => x.code == d.code && x.origin == d.origin)) {
            out.add(d);
          }
        }
      } catch (_) {}
    }
    return out;
  }

  /// Обход всех блоков: имя -> список кодов (или ошибка).
  Future<Map<String, List<DiagDtc>>> scanAllEcus({
    void Function(String name)? onProgress,
  }) async {
    final result = <String, List<DiagDtc>>{};
    for (final slot in EcuSlot.all) {
      onProgress?.call(slot.name);
      try {
        result[slot.name] = await readDtc(slot);
      } catch (_) {
        // блок не ответил — это норма для части поколений
      }
    }
    await restore();
    return result;
  }

  // ------------------------- СБРОС ОШИБОК -------------------------

  Future<List<DiagDtc>> clearDtc(EcuSlot slot) async {
    await _atEcu(slot);
    var done = false;
    if (slot.proto == DiagProto.ssm2) {
      final b = await _payload('14 FF FF 00');
      done = b.isNotEmpty && b[0] == 0x54;
      if (!done) {
        final o = await _payload('04');
        done = o.isNotEmpty && o[0] == 0x44;
      }
    } else {
      final b = await _payload('14 FF FF FF');
      done = b.isNotEmpty && b[0] == 0x54;
      if (!done) {
        final o = await _payload('04');
        done = o.isNotEmpty && o[0] == 0x44;
      }
    }
    if (!done) throw ReplyError('ECU отказал в сбросе DTC');
    await Future<void>.delayed(const Duration(milliseconds: 400));
    return readDtc(slot);
  }

  // ------------------------- ИНФОРМАЦИЯ -------------------------

  Future<ReadinessReport?> readReadiness(EcuSlot slot) async {
    await _atEcu(slot);
    return parseReadiness(await _payload('01 01'));
  }

  Future<String?> readVin(EcuSlot slot) async {
    await _atEcu(slot);
    final v = parseAsciiReply(await _payload('09 02'), 0x02);
    if (v != null) return v;
    // UDS ReadDataByIdentifier F190
    final b = await _payload('22 F1 90');
    if (b.length > 3 && b[0] == 0x62) {
      final chars = b
          .sublist(3)
          .where((x) => x >= 0x20 && x <= 0x7E)
          .toList();
      final text = String.fromCharCodes(chars).trim();
      return text.isEmpty ? null : text;
    }
    return null;
  }

  /// CALID прошивки (mode 09 PID 04).
  Future<String?> readCalId(EcuSlot slot) async {
    await _atEcu(slot);
    return parseAsciiReply(await _payload('09 04'), 0x04);
  }

  /// DTC, вызвавший стоп-кадр (mode 02).
  Future<String?> readFreezeFrameDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseFreezeFrameDtc(await _payload('02 02 00'));
  }

  // ------------------------- СЕРВИСНЫЕ -------------------------

  /// UDS DiagnosticSessionControl: расширенная сессия (нужна для 11 01 / 04 xx).
  Future<bool> enterExtendedSession(EcuSlot slot) async {
    await _atEcu(slot);
    final b = await _payload('10 03');
    return b.isNotEmpty && b[0] == 0x50;
  }

  /// Keep-alive, чтобы сессия не отвалилась.
  Future<void> testerPresent() async {
    try {
      await _payload('3E 00');
    } catch (_) {}
  }

  /// UDS ECUReset 11 01 — ГЛУШИТ двигатель.
  Future<ServiceResult> ecuReset(EcuSlot slot) async {
    if (!experimental) {
      return ServiceResult.fail('ECUReset заблокирован',
          'Включите «Экспертные операции» в настройках сервисной страницы.');
    }
    await enterExtendedSession(slot);
    final b = await _payload('11 01');
    final nr = _negativeCode(b);
    if (nr != null) {
      return ServiceResult.fail('ECUReset отклонён',
          kNrMeanings[nr] ?? 'NRC 0x${hex2(nr)}',
          negative: nr);
    }
    if (b.isNotEmpty && b[0] == 0x51) {
      return ServiceResult.ok('ECU перезапущен',
          'Блок ответил 51 01. Поверните зажигание OFF→ON перед запуском.');
    }
    return ServiceResult.fail(
        'Нет подтверждения', 'Ожидался ответ 51 01, получено: ${_hex(b)}');
  }

  /// SSM3 Clear Memory 04 gr — сброс адаптаций (группы зависят от ECU).
  Future<ServiceResult> clearMemory(EcuSlot slot, int group) async {
    if (!experimental) {
      return ServiceResult.fail('Clear Memory заблокирован',
          'Включите «Экспертные операции» в настройках сервисной страницы.');
    }
    if (group < 1 || group > 7) {
      return ServiceResult.fail('Неверная группа', 'Допустимо 1..7');
    }
    await enterExtendedSession(slot);
    final b = await _payload('04 ${hex2(group)}');
    final nr = _negativeCode(b);
    if (nr != null) {
      return ServiceResult.fail('Группа $group отклонена',
          kNrMeanings[nr] ?? 'NRC 0x${hex2(nr)}',
          negative: nr);
    }
    if (b.isNotEmpty && b[0] == 0x44) {
      return ServiceResult.ok('Адаптации группы $group сброшены',
          'Блок подтвердил (44). Нужно дообучение: 15–20 мин спокойной езды.');
    }
    return ServiceResult.fail(
        'Нет подтверждения', 'Ожидался 44, получено: ${_hex(b)}');
  }

  /// Проба всех групп Clear Memory: какие поддерживает ваш ECU.
  Future<Map<int, String>> probeClearMemoryGroups(EcuSlot slot) async {
    final out = <int, String>{};
    if (!experimental) return out;
    await enterExtendedSession(slot);
    for (var g = 1; g <= 7; g++) {
      try {
        final b = await _payload('04 ${hex2(g)}');
        final nr = _negativeCode(b);
        out[g] = nr != null
            ? (kNrMeanings[nr] ?? 'NRC 0x${hex2(nr)}')
            : (b.isNotEmpty && b[0] == 0x44 ? 'поддерживается (44)' : 'нет ответа');
      } catch (e) {
        out[g] = 'ошибка связи';
      }
      await Future<void>.delayed(const Duration(milliseconds: 120));
    }
    return out;
  }

  Future<void> restore() async {
    try {
      await elm.diagnostic('ATSH 7E0');
      await elm.diagnostic('ATCRA 7E8');
    } catch (_) {}
  }

  String _hex(List<int> b) =>
      b.isEmpty ? '(пусто)' : b.map(hex2).join(' ');
}
'''

# ===== Расчётные плитки: расход, одометр, IDC, запас насоса, мощность =====
FILES["lib/virtual_tiles.dart"] = r'''
import 'vehicle_config.dart';

/// Префикс идентификатора виртуальной (расчётной) плитки.
const String kVirtualPrefix = '@';

/// Входные данные для расчёта виртуальных плиток.
class TileInputs {
  const TileInputs({
    required this.values,
    required this.config,
    required this.tripKm,
    required this.tripLitres,
  });

  /// id PID -> текущее значение
  final Map<String, double> values;
  final VehicleConfig config;
  final double tripKm;
  final double tripLitres;

  double? v(String id) {
    final x = values[id];
    return (x == null || !x.isFinite) ? null : x;
  }

  /// наддув: 4-байтовый, иначе стандартный
  double? get boost => v('BOOST') ?? v('MAP_REL');

  /// расход топлива, г/с (MAF / AFR)
  double? get fuelGramsPerSec {
    final maf = v('MAF');
    if (maf == null || maf <= 0) return null;
    final afr = v('AFR');
    final ratio = (afr != null && afr >= 8 && afr <= 25) ? afr : config.fuel.stoich;
    return maf / ratio;
  }
}

class VirtualTileDef {
  const VirtualTileDef({
    required this.id,
    required this.label,
    required this.unit,
    required this.digits,
    required this.compute,
    this.hint = '',
  });
  final String id, label, unit, hint;
  final int digits;
  final double? Function(TileInputs) compute;
}

/// Плотность бензина, г/л.
const double _rho = 745.0;

final List<VirtualTileDef> kVirtualTiles = <VirtualTileDef>[
  VirtualTileDef(
    id: '@FUEL_LPH',
    label: 'Расход',
    unit: 'л/ч',
    digits: 1,
    hint: 'Мгновенный: MAF / AFR × 3600 / 745',
    compute: (i) {
      final g = i.fuelGramsPerSec;
      return g == null ? null : g * 3600 / _rho;
    },
  ),
  VirtualTileDef(
    id: '@FUEL_L100',
    label: 'Расход',
    unit: 'л/100км',
    digits: 1,
    hint: 'Мгновенный на скорости ≥ 5 км/ч',
    compute: (i) {
      final g = i.fuelGramsPerSec;
      final sp = i.v('SPEED');
      if (g == null || sp == null || sp < 5) return null;
      return g * 3600 / _rho / sp * 100;
    },
  ),
  VirtualTileDef(
    id: '@TRIP_KM',
    label: 'Одометр',
    unit: 'км',
    digits: 2,
    hint: 'Пробег с момента сброса поездки',
    compute: (i) => i.tripKm,
  ),
  VirtualTileDef(
    id: '@TRIP_L',
    label: 'Залито за поездку',
    unit: 'л',
    digits: 2,
    hint: 'Израсходовано топлива с момента сброса',
    compute: (i) => i.tripLitres,
  ),
  VirtualTileDef(
    id: '@TRIP_AVG',
    label: 'Средний расход',
    unit: 'л/100км',
    digits: 1,
    hint: 'Поездка: литры / км × 100',
    compute: (i) =>
        i.tripKm < 0.1 ? null : i.tripLitres / i.tripKm * 100,
  ),
  VirtualTileDef(
    id: '@IDC',
    label: 'Загрузка форсунок',
    unit: '%',
    digits: 0,
    hint: 'По ширине импульса, иначе по MAF/AFR. Предел 85%',
    compute: (i) {
      final pw = i.v('INJPW');
      final rpm = i.v('RPM');
      if (pw != null && rpm != null && rpm > 500) {
        return injectorDutyFromPulse(pulseWidthMs: pw, rpm: rpm);
      }
      final maf = i.v('MAF');
      final afr = i.v('AFR');
      if (maf == null || afr == null || afr < 5) return null;
      return injectorDutyFromMaf(
          mafGramsPerSec: maf, afr: afr, config: i.config);
    },
  ),
  VirtualTileDef(
    id: '@PUMP',
    label: 'Запас насоса',
    unit: '%',
    digits: 0,
    hint: 'Сколько ещё может дать бензонасос',
    compute: (i) {
      final maf = i.v('MAF');
      final afr = i.v('AFR');
      if (maf == null || afr == null || afr < 5) return null;
      return pumpHeadroomPct(
          mafGramsPerSec: maf, afr: afr, config: i.config);
    },
  ),
  VirtualTileDef(
    id: '@POWER',
    label: 'Мощность (оценка)',
    unit: 'л.с.',
    digits: 0,
    hint: 'MAF × 1.32 — грубая оценка по расходу воздуха',
    compute: (i) {
      final maf = i.v('MAF');
      return maf == null ? null : maf * 1.32;
    },
  ),
  VirtualTileDef(
    id: '@TORQUE',
    label: 'Момент (оценка)',
    unit: 'Н·м',
    digits: 0,
    hint: 'Из оценки мощности и оборотов',
    compute: (i) {
      final maf = i.v('MAF');
      final rpm = i.v('RPM');
      if (maf == null || rpm == null || rpm < 500) return null;
      final hp = maf * 1.32;
      return hp * 5252 / rpm * 1.3558;
    },
  ),
  VirtualTileDef(
    id: '@LAMBDA',
    label: 'Лямбда',
    unit: 'λ',
    digits: 3,
    hint: 'AFR / стехиометрия выбранного топлива',
    compute: (i) {
      final afr = i.v('AFR');
      return afr == null ? null : afr / i.config.fuel.stoich;
    },
  ),
  VirtualTileDef(
    id: '@KNOCK_TOT',
    label: 'Суммарная коррекция',
    unit: '°',
    digits: 2,
    hint: 'FBKC + FKL: полная поправка зажигания по детонации',
    compute: (i) {
      final fb = i.v('FBKC');
      final fk = i.v('FKL');
      if (fb == null && fk == null) return null;
      return (fb ?? 0) + (fk ?? 0);
    },
  ),
  VirtualTileDef(
    id: '@BOOST_DELTA',
    label: 'Отклонение наддува',
    unit: 'бар',
    digits: 3,
    hint: 'Факт минус цель (BOOST − BOOST_TGT)',
    compute: (i) {
      final b = i.boost;
      final t = i.v('BOOST_TGT');
      if (b == null || t == null) return null;
      return b - t;
    },
  ),
  VirtualTileDef(
    id: '@BOOST_PCT',
    label: 'Наддув от лимита',
    unit: '%',
    digits: 0,
    hint: 'Доля вашего лимита наддува из конфигурации мотора',
    compute: (i) {
      final b = i.boost;
      if (b == null || i.config.maxSafeBoostBar <= 0) return null;
      return b / i.config.maxSafeBoostBar * 100;
    },
  ),
];

VirtualTileDef? virtualTileById(String id) {
  for (final t in kVirtualTiles) {
    if (t.id == id) return t;
  }
  return null;
}

bool isVirtualTile(String id) => id.startsWith(kVirtualPrefix);
'''

# ===== Префы дашборда с поддержкой виртуальных плиток =====
FILES["lib/dashboard_prefs.dart"] = r'''
import 'pids.dart';
import 'virtual_tiles.dart';

/// Настройки дашборда: состав плиток (включая расчётные), порядок, плотность.
class DashboardPrefs {
  DashboardPrefs({
    List<String>? tiles,
    this.columnsPortrait = 2,
    this.columnsLandscape = 4,
    this.compact = false,
    this.showSpark = true,
    this.showFuelCard = true,
  }) : tiles = tiles ?? List<String>.from(defaultTiles);

  final List<String> tiles;
  final int columnsPortrait;
  final int columnsLandscape;
  final bool compact;
  final bool showSpark;
  final bool showFuelCard;

  /// v0.13: по умолчанию сразу показываем расход и одометр.
  static const List<String> defaultTiles = <String>[
    'RPM',
    'MAP_REL',
    'AFR',
    '@FUEL_L100',
    '@TRIP_KM',
    'ECT',
    'FBKC',
    'IAM',
  ];

  static bool knownTile(String id) =>
      isVirtualTile(id)
          ? virtualTileById(id) != null
          : SubaruPidLibrary.all.any((p) => p.id == id);

  DashboardPrefs copyWith({
    List<String>? tiles,
    int? columnsPortrait,
    int? columnsLandscape,
    bool? compact,
    bool? showSpark,
    bool? showFuelCard,
  }) =>
      DashboardPrefs(
        tiles: tiles ?? List<String>.from(this.tiles),
        columnsPortrait: columnsPortrait ?? this.columnsPortrait,
        columnsLandscape: columnsLandscape ?? this.columnsLandscape,
        compact: compact ?? this.compact,
        showSpark: showSpark ?? this.showSpark,
        showFuelCard: showFuelCard ?? this.showFuelCard,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'tiles': tiles,
        'columnsPortrait': columnsPortrait,
        'columnsLandscape': columnsLandscape,
        'compact': compact,
        'showSpark': showSpark,
        'showFuelCard': showFuelCard,
      };

  static DashboardPrefs fromJson(Map<String, dynamic>? json) {
    if (json == null) return DashboardPrefs();
    final raw = (json['tiles'] as List<dynamic>? ?? const <dynamic>[])
        .whereType<String>()
        .where(knownTile)
        .toList();
    int clampCols(Object? v, int fallback) {
      final n = v is num ? v.toInt() : fallback;
      return n < 1 ? 1 : (n > 6 ? 6 : n);
    }

    return DashboardPrefs(
      tiles: raw.isEmpty ? null : raw,
      columnsPortrait: clampCols(json['columnsPortrait'], 2),
      columnsLandscape: clampCols(json['columnsLandscape'], 4),
      compact: json['compact'] == true,
      showSpark: json['showSpark'] != false,
      showFuelCard: json['showFuelCard'] != false,
    );
  }

  /// Видимые плитки: PID должен быть активен, виртуальные доступны всегда.
  List<String> visibleTiles(Set<String> enabledPids) =>
      tiles.where((id) => isVirtualTile(id) || enabledPids.contains(id)).toList();
}

enum OrientationMode {
  auto('Авто (по датчику)'),
  portrait('Только книжная'),
  landscape('Только альбомная');

  const OrientationMode(this.label);
  final String label;
}

class UiPrefs {
  const UiPrefs({this.orientation = OrientationMode.auto, this.keepAwake = true});
  final OrientationMode orientation;
  final bool keepAwake;

  UiPrefs copyWith({OrientationMode? orientation, bool? keepAwake}) => UiPrefs(
        orientation: orientation ?? this.orientation,
        keepAwake: keepAwake ?? this.keepAwake,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'orientation': orientation.name,
        'keepAwake': keepAwake,
      };

  static UiPrefs fromJson(Map<String, dynamic>? json) {
    if (json == null) return const UiPrefs();
    return UiPrefs(
      orientation: OrientationMode.values.firstWhere(
        (o) => o.name == json['orientation'],
        orElse: () => OrientationMode.auto,
      ),
      keepAwake: json['keepAwake'] != false,
    );
  }
}
'''

# ===== Дашборд: PID + расчётные плитки, сброс поездки, альбомный режим =====
FILES["lib/dashboard_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'dashboard_prefs.dart';
import 'model.dart';
import 'pids.dart';
import 'samples.dart';
import 'virtual_tiles.dart';

const _muted = Color(0xFF8CA0BF);

/// Настраиваемый дашборд: PID + расчётные плитки (расход, одометр, IDC...).
class CustomDashboardPage extends StatefulWidget {
  const CustomDashboardPage(this.model, {super.key});
  final AppModel model;

  @override
  State<CustomDashboardPage> createState() => _CustomDashboardPageState();
}

class _CustomDashboardPageState extends State<CustomDashboardPage> {
  AppModel get model => widget.model;

  TileInputs _inputs() {
    final values = <String, double>{};
    model.engine.latest.forEach((id, sample) {
      final v = sample.value;
      if (v != null && v.isFinite) values[id] = v;
    });
    return TileInputs(
      values: values,
      config: model.vehicleConfig,
      tripKm: model.engine.tripDistanceKm,
      tripLitres: model.engine.tripFuelLitres,
    );
  }

  @override
  Widget build(BuildContext context) {
    final engine = model.engine;
    final prefs = model.dashboardPrefs;
    final activeIds = engine.active.map((p) => p.id).toSet();

    final chosen = prefs.tiles
        .where((id) => isVirtualTile(id) || activeIds.contains(id))
        .toList();
    final rest = engine.active
        .map((p) => p.id)
        .where((id) => !chosen.contains(id));
    final ids = <String>[...chosen, ...rest];

    final isLandscape =
        MediaQuery.of(context).orientation == Orientation.landscape;
    final columns = isLandscape ? prefs.columnsLandscape : prefs.columnsPortrait;
    final inputs = _inputs();

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 4, 12, 0),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  '${ids.length} плиток · ${engine.pidReadsPerSecond.toStringAsFixed(1)} обновл./с'
                  '${isLandscape ? ' · альбомная' : ''}',
                  style: const TextStyle(fontSize: 12, color: _muted),
                ),
              ),
              IconButton(
                tooltip: 'Сбросить поездку',
                onPressed: () {
                  engine.resetTrip();
                  setState(() {});
                },
                icon: const Icon(Icons.restart_alt),
              ),
              IconButton(
                tooltip: 'Настроить дашборд',
                onPressed: () => _configure(context),
                icon: const Icon(Icons.dashboard_customize),
              ),
              FilledButton.tonalIcon(
                onPressed:
                    model.busy || !model.elm.ready ? null : model.togglePolling,
                icon: Icon(engine.running ? Icons.pause : Icons.play_arrow),
                label: Text(engine.running ? 'Пауза' : 'Старт'),
              ),
            ],
          ),
        ),
        if (ids.isEmpty)
          const Expanded(
            child: Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Text(
                  'Нет плиток. Нажмите «Настроить дашборд» и добавьте '
                  'параметры или расчётные плитки (расход, одометр, IDC).',
                  textAlign: TextAlign.center,
                ),
              ),
            ),
          )
        else
          Expanded(
            child: GridView.builder(
              padding: const EdgeInsets.all(10),
              itemCount: ids.length,
              gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                crossAxisCount: columns,
                mainAxisExtent: prefs.compact ? 104 : 150,
                mainAxisSpacing: 8,
                crossAxisSpacing: 8,
              ),
              itemBuilder: (context, i) {
                final id = ids[i];
                if (isVirtualTile(id)) {
                  final def = virtualTileById(id);
                  if (def == null) return const SizedBox.shrink();
                  return _Tile(
                    title: def.label,
                    unit: def.unit,
                    digits: def.digits,
                    value: def.compute(inputs),
                    compact: prefs.compact,
                    isVirtual: true,
                    history: null,
                  );
                }
                final pid = SubaruPidLibrary.all.firstWhere(
                  (p) => p.id == id,
                  orElse: () => SubaruPidLibrary.byId('RPM'),
                );
                final sample = engine.latest[pid.id];
                return _Tile(
                  title: '${pid.id}${pid.extended ? ' *' : ''}',
                  unit: pid.unit,
                  digits: pid.digits,
                  value: sample?.value,
                  compact: prefs.compact,
                  isVirtual: false,
                  stale: (engine.ageMs(pid.id) ?? 999999) > 2500,
                  unsupported: sample?.allOnes ?? false,
                  history: prefs.showSpark && !prefs.compact
                      ? engine.history[pid.id]
                      : null,
                );
              },
            ),
          ),
      ],
    );
  }

  Future<void> _configure(BuildContext context) async {
    final result = await showModalBottomSheet<DashboardPrefs>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _ConfigSheet(model: model),
    );
    if (result != null) {
      await model.updateDashboardPrefs(result);
      if (mounted) setState(() {});
    }
  }
}

class _Tile extends StatelessWidget {
  const _Tile({
    required this.title,
    required this.unit,
    required this.digits,
    required this.value,
    required this.compact,
    required this.isVirtual,
    required this.history,
    this.stale = false,
    this.unsupported = false,
  });

  final String title, unit;
  final int digits;
  final double? value;
  final bool compact, isVirtual, stale, unsupported;
  final List<PidSample>? history;

  @override
  Widget build(BuildContext context) {
    final text = value == null
        ? (unsupported ? 'н/д' : '—')
        : value!.toStringAsFixed(digits);
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: EdgeInsets.all(compact ? 8 : 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                if (isVirtual)
                  const Padding(
                    padding: EdgeInsets.only(right: 4),
                    child: Icon(Icons.functions, size: 12, color: Color(0xFF22D3EE)),
                  ),
                Expanded(
                  child: Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: compact ? 11 : 12,
                      fontWeight: FontWeight.bold,
                      color: stale ? _muted : null,
                    ),
                  ),
                ),
                if (stale) const Icon(Icons.schedule, size: 12, color: _muted),
              ],
            ),
            const SizedBox(height: 2),
            FittedBox(
              fit: BoxFit.scaleDown,
              alignment: Alignment.centerLeft,
              child: Text(
                text,
                style: TextStyle(
                  fontSize: compact ? 22 : 30,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            Text(unit, style: const TextStyle(fontSize: 10, color: _muted)),
            if (history != null && history!.length > 2 && !compact) ...[
              const SizedBox(height: 4),
              Expanded(
                child: CustomPaint(
                  painter: _SparkPainter(history!),
                  size: Size.infinite,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _SparkPainter extends CustomPainter {
  _SparkPainter(this.samples);
  final List<PidSample> samples;

  @override
  void paint(Canvas canvas, Size size) {
    final values =
        samples.where((s) => s.value != null).map((s) => s.value!).toList();
    if (values.length < 2) return;
    var lo = values.first, hi = values.first;
    for (final v in values) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if ((hi - lo).abs() < 1e-9) hi = lo + 1;
    final path = Path();
    for (var i = 0; i < values.length; i++) {
      final x = size.width * i / (values.length - 1);
      final y = size.height * (1 - (values[i] - lo) / (hi - lo));
      i == 0 ? path.moveTo(x, y) : path.lineTo(x, y);
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = const Color(0xFF22D3EE)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.6,
    );
  }

  @override
  bool shouldRepaint(covariant _SparkPainter old) => true;
}

class _ConfigSheet extends StatefulWidget {
  const _ConfigSheet({required this.model});
  final AppModel model;
  @override
  State<_ConfigSheet> createState() => _ConfigSheetState();
}

class _ConfigSheetState extends State<_ConfigSheet> {
  late DashboardPrefs prefs = widget.model.dashboardPrefs;

  String _labelOf(String id) {
    if (isVirtualTile(id)) {
      final d = virtualTileById(id);
      return d == null ? id : '${d.label} (${d.unit})';
    }
    return id;
  }

  String _subOf(String id) {
    if (isVirtualTile(id)) return virtualTileById(id)?.hint ?? '';
    final p = SubaruPidLibrary.all.where((x) => x.id == id);
    return p.isEmpty ? '' : p.first.desc;
  }

  @override
  Widget build(BuildContext context) {
    final activeIds = widget.model.engine.active.map((p) => p.id).toList();
    final chosen = prefs.tiles
        .where((id) => isVirtualTile(id) || activeIds.contains(id))
        .toList();
    final availablePids = activeIds.where((id) => !chosen.contains(id)).toList();
    final availableVirtual = kVirtualTiles
        .map((t) => t.id)
        .where((id) => !chosen.contains(id))
        .toList();

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.85,
      maxChildSize: 0.95,
      builder: (context, scroll) => ListView(
        controller: scroll,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
        children: [
          const Text('Настройка дашборда',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          const Text(
            'Перетаскивайте за ручку. Плитки со значком ƒ — расчётные '
            '(расход, одометр, IDC) и работают без отдельного PID.',
            style: TextStyle(fontSize: 12, color: _muted),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: _Stepper(
                  label: 'Колонок (книжная)',
                  value: prefs.columnsPortrait,
                  min: 1,
                  max: 4,
                  onChanged: (v) =>
                      setState(() => prefs = prefs.copyWith(columnsPortrait: v)),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _Stepper(
                  label: 'Колонок (альбомная)',
                  value: prefs.columnsLandscape,
                  min: 2,
                  max: 6,
                  onChanged: (v) => setState(
                      () => prefs = prefs.copyWith(columnsLandscape: v)),
                ),
              ),
            ],
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Компактные плитки'),
            value: prefs.compact,
            onChanged: (v) => setState(() => prefs = prefs.copyWith(compact: v)),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Мини-графики'),
            value: prefs.showSpark,
            onChanged: (v) =>
                setState(() => prefs = prefs.copyWith(showSpark: v)),
          ),
          const Divider(height: 24),
          Text('На дашборде (${chosen.length})',
              style: const TextStyle(fontWeight: FontWeight.bold)),
          ReorderableListView(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            buildDefaultDragHandles: false,
            onReorder: (oldIndex, newIndex) => setState(() {
              final list = List<String>.from(chosen);
              if (newIndex > oldIndex) newIndex -= 1;
              list.insert(newIndex, list.removeAt(oldIndex));
              final others =
                  prefs.tiles.where((id) => !chosen.contains(id)).toList();
              prefs = prefs.copyWith(tiles: [...list, ...others]);
            }),
            children: [
              for (var i = 0; i < chosen.length; i++)
                ListTile(
                  key: ValueKey('t-${chosen[i]}'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: ReorderableDragStartListener(
                    index: i,
                    child: const Icon(Icons.drag_handle),
                  ),
                  title: Row(
                    children: [
                      if (isVirtualTile(chosen[i]))
                        const Padding(
                          padding: EdgeInsets.only(right: 6),
                          child: Icon(Icons.functions,
                              size: 14, color: Color(0xFF22D3EE)),
                        ),
                      Expanded(child: Text(_labelOf(chosen[i]))),
                    ],
                  ),
                  subtitle: Text(
                    _subOf(chosen[i]),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 11, color: _muted),
                  ),
                  trailing: IconButton(
                    icon: const Icon(Icons.visibility_off, size: 18),
                    onPressed: () => setState(() {
                      final list = List<String>.from(prefs.tiles)
                        ..remove(chosen[i]);
                      prefs = prefs.copyWith(tiles: list);
                    }),
                  ),
                ),
            ],
          ),
          if (availableVirtual.isNotEmpty) ...[
            const Divider(height: 24),
            const Text('Расчётные плитки',
                style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 6),
            for (final id in availableVirtual)
              ListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.functions,
                    size: 16, color: Color(0xFF22D3EE)),
                title: Text(_labelOf(id), style: const TextStyle(fontSize: 13)),
                subtitle: Text(_subOf(id),
                    style: const TextStyle(fontSize: 11, color: _muted)),
                trailing: IconButton(
                  icon: const Icon(Icons.add_circle_outline, size: 20),
                  onPressed: () => setState(() =>
                      prefs = prefs.copyWith(tiles: [...prefs.tiles, id])),
                ),
              ),
          ],
          if (availablePids.isNotEmpty) ...[
            const Divider(height: 24),
            Text('Активные PID (${availablePids.length})',
                style: const TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final id in availablePids)
                  ActionChip(
                    label: Text(id, style: const TextStyle(fontSize: 12)),
                    avatar: const Icon(Icons.add, size: 16),
                    onPressed: () => setState(() =>
                        prefs = prefs.copyWith(tiles: [...prefs.tiles, id])),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 20),
          Row(
            children: [
              TextButton(
                onPressed: () => setState(() => prefs = DashboardPrefs()),
                child: const Text('Сбросить'),
              ),
              const Spacer(),
              FilledButton(
                onPressed: () => Navigator.pop(context, prefs),
                child: const Text('Применить'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _Stepper extends StatelessWidget {
  const _Stepper({
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.onChanged,
  });
  final String label;
  final int value, min, max;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: const TextStyle(fontSize: 11, color: _muted)),
          Row(
            children: [
              IconButton(
                visualDensity: VisualDensity.compact,
                onPressed: value > min ? () => onChanged(value - 1) : null,
                icon: const Icon(Icons.remove_circle_outline),
              ),
              Text('$value', style: const TextStyle(fontSize: 16)),
              IconButton(
                visualDensity: VisualDensity.compact,
                onPressed: value < max ? () => onChanged(value + 1) : null,
                icon: const Icon(Icons.add_circle_outline),
              ),
            ],
          ),
        ],
      );
}
'''

# ===== Сервисная страница: скан, мониторы, VIN, экспертные операции =====
FILES["lib/dtc_service_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'diag.dart';
import 'model.dart';

const _muted = Color(0xFF8CA0BF);

/// Вкладка «DTC»: ошибки всех блоков + полный набор сервисных функций.
class DtcServicePage extends StatefulWidget {
  const DtcServicePage({super.key, required this.model});
  final AppModel model;

  @override
  State<DtcServicePage> createState() => _DtcServicePageState();
}

class _DtcServicePageState extends State<DtcServicePage> {
  final Map<String, List<DiagDtc>> _results = {};
  final Map<String, String> _notes = {};
  ReadinessReport? _readiness;
  String _log = '';
  bool _busy = false;

  AppModel get model => widget.model;
  DiagSession get session => model.diagSession;

  Future<void> _run(String label, Future<void> Function() op) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _log = '$label…';
    });
    try {
      await model.diagRun(label, op);
      if (mounted) setState(() => _log = '$label — готово');
    } catch (e) {
      if (mounted) setState(() => _log = '$label — ошибка: $e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<bool> _confirm(String title, String body, {bool danger = false}) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('ОТМЕНА')),
          danger
              ? FilledButton(
                  style: FilledButton.styleFrom(backgroundColor: Colors.red),
                  onPressed: () => Navigator.pop(ctx, true),
                  child: const Text('ВЫПОЛНИТЬ'))
              : FilledButton.tonal(
                  onPressed: () => Navigator.pop(ctx, true),
                  child: const Text('ВЫПОЛНИТЬ')),
        ],
      ),
    );
    return ok ?? false;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        if (_busy) const LinearProgressIndicator(),
        if (_log.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Text(_log, style: theme.textTheme.bodySmall),
          ),

        // ----- быстрые действия -----
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.tonalIcon(
              onPressed: _busy ? null : _scanAll,
              icon: const Icon(Icons.radar, size: 18),
              label: const Text('Скан всех блоков'),
            ),
            OutlinedButton.icon(
              onPressed: _busy ? null : _readInfo,
              icon: const Icon(Icons.badge, size: 18),
              label: const Text('VIN / CALID'),
            ),
            OutlinedButton.icon(
              onPressed: _busy ? null : _readReadiness,
              icon: const Icon(Icons.checklist, size: 18),
              label: const Text('Мониторы'),
            ),
          ],
        ),
        const SizedBox(height: 8),

        if (_readiness != null) _readinessCard(_readiness!),

        // ----- блоки -----
        for (final slot in EcuSlot.all)
          Card(
            child: ExpansionTile(
              leading: Icon(
                _results.containsKey(slot.name)
                    ? (_results[slot.name]!.isEmpty
                        ? Icons.check_circle
                        : Icons.warning_amber)
                    : Icons.memory,
                color: _results[slot.name]?.isEmpty == true
                    ? Colors.greenAccent
                    : (_results[slot.name]?.isNotEmpty == true
                        ? Colors.amberAccent
                        : null),
              ),
              title: Text(slot.name),
              subtitle: Text(
                '${slot.header}→${slot.responseId} · ${slot.proto.name.toUpperCase()}'
                '${_notes[slot.name] == null ? '' : ' · ${_notes[slot.name]}'}',
                style: const TextStyle(fontSize: 11),
              ),
              children: [
                for (final d in _results[slot.name] ?? const <DiagDtc>[])
                  ListTile(
                    dense: true,
                    leading: Icon(
                      d.origin == 'permanent'
                          ? Icons.lock
                          : (d.active ? Icons.error : Icons.history),
                      color: d.origin == 'permanent'
                          ? Colors.orangeAccent
                          : (d.active
                              ? theme.colorScheme.error
                              : theme.colorScheme.outline),
                      size: 20,
                    ),
                    title: Text(
                      '${d.code}${d.titleRu.isEmpty ? '' : ' · ${d.titleRu}'}',
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 13),
                    ),
                    subtitle: Text(d.statusText,
                        style: const TextStyle(fontSize: 11)),
                    trailing: d.milOn
                        ? const Icon(Icons.lightbulb,
                            color: Colors.amber, size: 18)
                        : null,
                  ),
                if (_results[slot.name]?.isEmpty == true)
                  const ListTile(
                    dense: true,
                    leading: Icon(Icons.check, color: Colors.greenAccent),
                    title: Text('Ошибок нет'),
                  ),
                OverflowBar(
                  alignment: MainAxisAlignment.start,
                  children: [
                    TextButton.icon(
                      onPressed: _busy ? null : () => _readAll(slot),
                      icon: const Icon(Icons.search, size: 18),
                      label: const Text('ЧИТАТЬ ВСЁ'),
                    ),
                    TextButton(
                      onPressed: _busy ? null : () => _readFreeze(slot),
                      child: const Text('СТОП-КАДР'),
                    ),
                    if ((_results[slot.name] ?? const []).isNotEmpty)
                      FilledButton.tonalIcon(
                        onPressed: _busy ? null : () => _clear(slot),
                        icon: const Icon(Icons.cleaning_services, size: 18),
                        label: const Text('СБРОС'),
                      ),
                  ],
                ),
              ],
            ),
          ),

        const Divider(height: 28),
        Text('Сервисные процедуры', style: theme.textTheme.titleMedium),
        const ListTile(
          dense: true,
          leading: Icon(Icons.restart_alt),
          title: Text('Сброс адаптаций (SSM2-эра)'),
          subtitle: Text(
              'Клемма АКБ 10–30 мин. Контроль IAM до/после, дообучение 15–20 мин.'),
        ),
        const ListTile(
          dense: true,
          leading: Icon(Icons.speed),
          title: Text('Обучение дросселя (E-Gas)'),
          subtitle: Text('Зажигание ON 15–20 с без запуска → OFF → прогрев.'),
        ),

        const Divider(height: 28),
        // ----- экспертные операции -----
        Card(
          color: model.experimental
              ? Colors.red.withValues(alpha: 0.07)
              : null,
          child: Column(
            children: [
              SwitchListTile(
                secondary: Icon(Icons.science,
                    color: model.experimental ? Colors.redAccent : null),
                title: const Text('Экспертные операции'),
                subtitle: Text(
                  model.experimental
                      ? 'РАЗБЛОКИРОВАНО: ECUReset и Clear Memory активны. '
                          'Только на стоянке, двигатель заглушён, зажигание ON.'
                      : 'ECUReset (11 01) и Clear Memory (04 xx) заблокированы. '
                          'Включите, если понимаете риск.',
                  style: const TextStyle(fontSize: 11),
                ),
                value: model.experimental,
                onChanged: _busy
                    ? null
                    : (v) async {
                        if (v) {
                          final ok = await _confirm(
                            'Включить экспертные операции?',
                            'ECUReset перезапускает блок управления и ГЛУШИТ двигатель. '
                            'Clear Memory стирает адаптации (IAM, топливные коррекции) — '
                            'потребуется дообучение 15–20 минут езды.\n\n'
                            'Выполняйте только на стоянке при заглушённом моторе.',
                            danger: true,
                          );
                          if (!ok) return;
                        }
                        await model.setExperimental(v);
                        if (mounted) setState(() {});
                      },
              ),
              if (model.experimental) ...[
                const Divider(height: 1),
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.power_settings_new,
                      color: Colors.redAccent),
                  title: const Text('ECUReset (11 01)'),
                  subtitle: const Text(
                      'Перезапуск ЭБУ. Двигатель заглохнет.',
                      style: TextStyle(fontSize: 11)),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: _busy ? null : _ecuReset,
                ),
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.auto_delete, color: Colors.orangeAccent),
                  title: const Text('Clear Memory (04 xx)'),
                  subtitle: const Text(
                      'Сброс адаптаций. Сначала «Проба групп».',
                      style: TextStyle(fontSize: 11)),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: _busy ? null : _clearMemoryDialog,
                ),
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.travel_explore),
                  title: const Text('Проба групп Clear Memory'),
                  subtitle: const Text(
                      'Безопасно: опрашивает 1..7 и показывает ответы ECU',
                      style: TextStyle(fontSize: 11)),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: _busy ? null : _probeGroups,
                ),
              ],
            ],
          ),
        ),
        const SizedBox(height: 24),
      ],
    );
  }

  Widget _readinessCard(ReadinessReport r) => Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(r.milOn ? Icons.lightbulb : Icons.lightbulb_outline,
                      color: r.milOn ? Colors.amber : _muted, size: 18),
                  const SizedBox(width: 8),
                  Text(
                    'Мониторы: ${r.ready}/${r.total} готовы'
                    '${r.milOn ? ' · CHECK ГОРИТ' : ''} · DTC: ${r.dtcCount}',
                    style: const TextStyle(fontWeight: FontWeight.bold),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: [
                  for (final e in r.monitors.entries)
                    Chip(
                      visualDensity: VisualDensity.compact,
                      avatar: Icon(
                        e.value ? Icons.check_circle : Icons.pending,
                        size: 16,
                        color: e.value ? Colors.greenAccent : Colors.orangeAccent,
                      ),
                      label: Text(e.key, style: const TextStyle(fontSize: 11)),
                    ),
                ],
              ),
            ],
          ),
        ),
      );

  // ------------------------- действия -------------------------

  Future<void> _readAll(EcuSlot slot) => _run('Чтение ${slot.name}', () async {
        final list = await session.readAllDtc(slot);
        if (mounted) {
          setState(() {
            _results[slot.name] = list;
            _notes[slot.name] = '${list.length} код(ов)';
          });
        }
      });

  Future<void> _clear(EcuSlot slot) async {
    final has = _results[slot.name] ?? const <DiagDtc>[];
    final permanent = has.where((d) => d.origin == 'permanent').length;
    final ok = await _confirm(
      'Сбросить ошибки: ${slot.name}?',
      'Будут стёрты коды, стоп-кадры и мониторы готовности. '
      'CHECK погаснет после перезапуска зажигания.\n\n'
      '${permanent > 0 ? 'ВНИМАНИЕ: $permanent постоянных (permanent) кодов НЕ стираются '
          'сбросом — они уйдут только после успешного прохождения циклов.\n\n' : ''}'
      'Адаптации (IAM, топливные коррекции) это НЕ трогает.',
    );
    if (!ok) return;
    await _run('Сброс ${slot.name}', () async {
      final rest = await session.clearDtc(slot);
      if (mounted) {
        setState(() {
          _results[slot.name] = rest;
          _notes[slot.name] =
              rest.isEmpty ? 'очищено' : 'осталось ${rest.length}';
        });
      }
    });
  }

  Future<void> _readFreeze(EcuSlot slot) =>
      _run('Стоп-кадр ${slot.name}', () async {
        final dtc = await session.readFreezeFrameDtc(slot);
        if (mounted) {
          setState(() => _notes[slot.name] =
              dtc == null ? 'стоп-кадра нет' : 'стоп-кадр: $dtc');
        }
      });

  Future<void> _scanAll() => _run('Скан всех блоков', () async {
        final map = await session.scanAllEcus(
          onProgress: (name) {
            if (mounted) setState(() => _log = 'Скан: $name…');
          },
        );
        if (mounted) {
          setState(() {
            _results
              ..clear()
              ..addAll(map);
            _notes.clear();
            for (final e in map.entries) {
              _notes[e.key] = '${e.value.length} код(ов)';
            }
          });
        }
      });

  Future<void> _readInfo() => _run('Чтение VIN / CALID', () async {
        final vin = await session.readVin(EcuSlot.engine);
        final cal = await session.readCalId(EcuSlot.engine);
        if (mounted) {
          setState(() => _log =
              'VIN: ${vin ?? 'нет ответа'} · CALID: ${cal ?? 'нет ответа'}');
        }
      });

  Future<void> _readReadiness() => _run('Мониторы готовности', () async {
        final r = await session.readReadiness(EcuSlot.engine);
        if (mounted) setState(() => _readiness = r);
      });

  Future<void> _ecuReset() async {
    final ok = await _confirm(
      'ECUReset: перезапустить ЭБУ?',
      'Двигатель ЗАГЛОХНЕТ немедленно. Выполняйте только на стоянке. '
      'После сброса поверните зажигание OFF→ON.',
      danger: true,
    );
    if (!ok) return;
    await _run('ECUReset', () async {
      final res = await session.ecuReset(EcuSlot.engine);
      if (mounted) setState(() => _log = '${res.title}: ${res.detail}');
    });
  }

  Future<void> _probeGroups() => _run('Проба групп Clear Memory', () async {
        final map = await session.probeClearMemoryGroups(EcuSlot.engine);
        if (!mounted) return;
        final text = map.entries
            .map((e) => 'гр.${e.key}: ${e.value}')
            .join('\n');
        await showDialog<void>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Поддержка групп Clear Memory'),
            content: SingleChildScrollView(
              child: Text(
                text.isEmpty ? 'Нет ответа от ECU' : text,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
              ),
            ),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(ctx),
                  child: const Text('ЗАКРЫТЬ')),
            ],
          ),
        );
      });

  Future<void> _clearMemoryDialog() async {
    var group = 1;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: const Text('Clear Memory: сброс адаптаций'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Группы зависят от поколения ECU. Обычно: 1 — всё обучение, '
                '2 — топливные коррекции, 3 — детонация/IAM.\n\n'
                'После сброса нужно дообучение: 15–20 минут спокойной езды '
                'на прогретом моторе.',
                style: TextStyle(fontSize: 12),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<int>(
                initialValue: group,
                decoration: const InputDecoration(labelText: 'Группа'),
                items: [
                  for (var g = 1; g <= 7; g++)
                    DropdownMenuItem(value: g, child: Text('Группа $g')),
                ],
                onChanged: (v) => set(() => group = v ?? 1),
              ),
            ],
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(ctx, false),
                child: const Text('ОТМЕНА')),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Colors.orange),
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('СБРОСИТЬ'),
            ),
          ],
        ),
      ),
    );
    if (ok != true) return;
    await _run('Clear Memory гр.$group', () async {
      final res = await session.clearMemory(EcuSlot.engine, group);
      if (mounted) setState(() => _log = '${res.title}: ${res.detail}');
    });
  }
}
'''

# ===== 20 тестов: OBD 07/0A, мониторы, VIN, NRC, виртуальные плитки =====
FILES["test/service_test.dart"] = r'''
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/dashboard_prefs.dart';
import 'package:subaru_ssm2/diag.dart';
import 'package:subaru_ssm2/vehicle_config.dart';
import 'package:subaru_ssm2/virtual_tiles.dart';

void main() {
  group('OBD mode 07 / 0A', () {
    test('pending помечается origin=pending', () {
      final d = parseObdDtcReply(const [0x47, 0x01, 0x10]);
      expect(d.single.code, 'P0110');
      expect(d.single.origin, 'pending');
    });
    test('permanent помечается и не считается активной', () {
      final d = parseObdDtcReply(const [0x4A, 0x04, 0x20]);
      expect(d.single.origin, 'permanent');
      expect(d.single.active, isFalse);
    });
    test('mode 03 остаётся stored', () {
      expect(parseObdDtcReply(const [0x43, 0x01, 0x10]).single.origin, 'stored');
    });
  });

  group('Мониторы готовности', () {
    test('41 01 с MIL и счётчиком DTC', () {
      // A=0x83: MIL on, 3 DTC
      final r = parseReadiness(const [0x41, 0x01, 0x83, 0x07, 0x21, 0x00]);
      expect(r, isNotNull);
      expect(r!.milOn, isTrue);
      expect(r.dtcCount, 3);
      expect(r.monitors, isNotEmpty);
    });
    test('незавершённый монитор виден как не готовый', () {
      // C=0x01 (катализатор доступен), D=0x01 (не завершён)
      final r = parseReadiness(const [0x41, 0x01, 0x00, 0x00, 0x01, 0x01]);
      expect(r!.monitors['Катализатор'], isFalse);
    });
    test('мусор -> null', () {
      expect(parseReadiness(const [0x7F, 0x01, 0x12]), isNull);
    });
  });

  group('VIN / CALID / стоп-кадр', () {
    test('ASCII VIN из mode 09', () {
      final bytes = <int>[0x49, 0x02, 0x01, ...'JF1BP9LL'.codeUnits];
      expect(parseAsciiReply(bytes, 0x02), 'JF1BP9LL');
    });
    test('CALID из mode 09 PID 04', () {
      final bytes = <int>[0x49, 0x04, 0x01, ...'A2TB100B'.codeUnits];
      expect(parseAsciiReply(bytes, 0x04), 'A2TB100B');
    });
    test('стоп-кадр возвращает код', () {
      expect(parseFreezeFrameDtc(const [0x42, 0x02, 0x00, 0x01, 0x10]), 'P0110');
      expect(parseFreezeFrameDtc(const [0x42, 0x02, 0x00, 0x00, 0x00]), isNull);
    });
  });

  group('NRC', () {
    test('ключевые коды расшифрованы', () {
      expect(kNrMeanings[0x22], contains('conditionsNotCorrect'));
      expect(kNrMeanings[0x7F], contains('10 03'));
      expect(kNrMeanings[0x33], contains('дилер'));
    });
  });

  group('Виртуальные плитки', () {
    TileInputs make(Map<String, double> v, {double km = 0, double l = 0}) =>
        TileInputs(
          values: v,
          config: const VehicleConfig(),
          tripKm: km,
          tripLitres: l,
        );

    test('мгновенный расход л/ч считается из MAF и AFR', () {
      final t = virtualTileById('@FUEL_LPH')!;
      final v = t.compute(make({'MAF': 10, 'AFR': 14.7}));
      // 10/14.7 = 0.68 г/с -> *3600/745 = 3.29 л/ч
      expect(v, closeTo(3.29, 0.05));
    });

    test('л/100км требует скорость >= 5', () {
      final t = virtualTileById('@FUEL_L100')!;
      expect(t.compute(make({'MAF': 10, 'AFR': 14.7, 'SPEED': 0})), isNull);
      expect(t.compute(make({'MAF': 10, 'AFR': 14.7, 'SPEED': 90})),
          greaterThan(0));
    });

    test('одометр и средний расход поездки', () {
      expect(virtualTileById('@TRIP_KM')!.compute(make({}, km: 12.5)), 12.5);
      final avg = virtualTileById('@TRIP_AVG')!
          .compute(make({}, km: 100, l: 12))!;
      expect(avg, closeTo(12.0, 0.001));
      expect(virtualTileById('@TRIP_AVG')!.compute(make({}, km: 0)), isNull);
    });

    test('IDC по ширине импульса', () {
      final v = virtualTileById('@IDC')!
          .compute(make({'INJPW': 10, 'RPM': 6000}));
      expect(v, closeTo(50.0, 0.1));
    });

    test('лямбда и суммарная коррекция', () {
      expect(virtualTileById('@LAMBDA')!.compute(make({'AFR': 14.7})),
          closeTo(1.0, 0.001));
      expect(
        virtualTileById('@KNOCK_TOT')!.compute(make({'FBKC': -1.4, 'FKL': -2.0})),
        closeTo(-3.4, 0.001),
      );
    });

    test('каждая плитка имеет уникальный id с префиксом @', () {
      final ids = kVirtualTiles.map((t) => t.id).toList();
      expect(ids.toSet().length, ids.length);
      expect(ids.every(isVirtualTile), isTrue);
    });
  });

  group('DashboardPrefs с виртуальными', () {
    test('дефолт содержит расход и одометр', () {
      expect(DashboardPrefs.defaultTiles, contains('@FUEL_L100'));
      expect(DashboardPrefs.defaultTiles, contains('@TRIP_KM'));
    });
    test('виртуальные проходят валидацию, мусор — нет', () {
      final p = DashboardPrefs.fromJson({
        'tiles': ['@FUEL_LPH', '@НЕТ_ТАКОЙ', 'RPM']
      });
      expect(p.tiles, contains('@FUEL_LPH'));
      expect(p.tiles, isNot(contains('@НЕТ_ТАКОЙ')));
    });
    test('visibleTiles не требует активного PID для виртуальных', () {
      final p = DashboardPrefs(tiles: ['@TRIP_KM', 'AFR']);
      expect(p.visibleTiles(<String>{}), ['@TRIP_KM']);
    });
  });
}
'''

# ===== Правки: рантайм-EXPERIMENTAL, whitelist сервисов, версия =====
# ===== REWRITES =====

# --- 1. model.dart: EXPERIMENTAL -> рантайм (был compile-time const) ---
REWRITES.append((
    "lib/model.dart",
    """  final bool experimental =
      const bool.fromEnvironment('SSM2_EXPERIMENTAL', defaultValue: false);
  late final DiagSession diagSession =
      DiagSession(elm, experimental: experimental);""",
    """  /// v0.13 (04.4): рантайм-флаг экспертных операций. В 04.2 он был
  /// compile-time константой, поэтому переключатель всегда оставался серым.
  bool experimental = false;
  late final DiagSession diagSession = DiagSession(elm);

  Future<void> setExperimental(bool value) async {
    experimental = value;
    diagSession.experimental = value;
    await saveSettings();
    changed();
  }""",
    ["Future<void> setExperimental", False],
))

# --- 2. model.dart: сохранение флага ---
REWRITES.append((
    "lib/model.dart",
    "        'vehicle': vehicleConfig.toJson(), // v0.12",
    "        'experimental': experimental, // v0.13\n        'vehicle': vehicleConfig.toJson(), // v0.12",
    ["'experimental': experimental", False],
))

# --- 3. model.dart: восстановление флага ---
REWRITES.append((
    "lib/model.dart",
    "      uiPrefs = UiPrefs.fromJson(json['ui'] as Map<String, dynamic>?);",
    """      uiPrefs = UiPrefs.fromJson(json['ui'] as Map<String, dynamic>?);
      experimental = json['experimental'] == true; // v0.13
      diagSession.experimental = experimental;""",
    ["diagSession.experimental = experimental", False],
))

# --- 4. protocol.dart: сервисные команды в белый список ---
REWRITES.append((
    "lib/protocol.dart",
    "  '11 01', // UDS ECUReset (глушится EXPERIMENTAL в diag.dart)\n};",
    """  '11 01', // UDS ECUReset (глушится EXPERIMENTAL в diag.dart)
  // v0.13 (04.4): расширенный сервисный набор
  '02 02 00', // OBD freeze frame: DTC стоп-кадра
  '09 04', // CALID прошивки
  '09 00', // список поддерживаемых PID mode 09
  '01 00', // список поддерживаемых PID mode 01
  '01 41', // статус мониторов текущего цикла
  '10 03', // UDS extended session (нужна для 11 01 и 04 xx)
  '10 01', // UDS default session
  '3E 00', // TesterPresent (keep-alive)
  '22 F1 90', // UDS ReadDataByIdentifier: VIN
  '22 F1 8C', // UDS: серийный номер блока
};""",
    ["'10 03', // UDS extended session", False],
))

# --- 5. main.dart: версия ---
REWRITES.append((
    "lib/main.dart",
    "SSM2 TELEMETRY 0.12",
    "SSM2 TELEMETRY 0.13",
    ["SSM2 TELEMETRY 0.13", True],
))

# ===== Запись, правки, самопроверка, чек-лист стенда =====
# ===== запись файлов =====
bad = [r for r in FILES if "(" in r or ")" in r]
if bad:
    raise RuntimeError(f"Невалидные FILES-ключи: {bad}")
BACKUP.mkdir(parents=True, exist_ok=True)
for rel, body in FILES.items():
    dest = APP / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copyfile(dest, BACKUP / (rel.replace("/", "__") + ".bak"))
    dest.write_text(body.strip("\n") + "\n", encoding="utf-8")
    print(f"[OK] {rel}")
print(f"[OK] файлов: {len(FILES)}")

# ===== точечные правки =====
patched, skipped = 0, 0
for rel, needle, replacement, sig in REWRITES:
    optional = False
    if sig and sig[-1] is True:
        sig, optional = sig[:-1], True
    elif sig and sig[-1] is False:
        sig, optional = sig[:-1], False
    path = APP / rel
    if not path.exists():
        raise RuntimeError(f"Нет файла {rel} — прогоните 02–04.3")
    text = path.read_text(encoding="utf-8")
    if sig and all(s in text for s in sig):
        skipped += 1
        print(f"[SKIP уже применено] {rel}: {sig[0][:50]}")
        continue
    if needle in text:
        shutil.copyfile(path, BACKUP / (rel.replace("/", "__") + ".orig.bak"))
        path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
        patched += 1
        print(f"[OK] правка {rel}")
        continue
    if optional:
        skipped += 1
        print(f"[SKIP необязательная] {rel}")
        continue
    raise RuntimeError(f"Маркер не найден в {rel}:\n{needle[:100]!r}")
print(f"[OK] правок: {patched}, пропущено: {skipped}")

# ===== самопроверка =====
checks = {
    "lib/diag.dart": ["readPendingDtc", "readPermanentDtc", "parseReadiness",
                      "enterExtendedSession", "probeClearMemoryGroups",
                      "class ServiceResult", "bool experimental = false"],
    "lib/virtual_tiles.dart": ["@FUEL_LPH", "@TRIP_KM", "@IDC", "kVirtualTiles"],
    "lib/dashboard_prefs.dart": ["isVirtualTile", "'@FUEL_L100'"],
    "lib/dashboard_page.dart": ["virtualTileById", "TileInputs"],
    "lib/dtc_service_page.dart": ["Экспертные операции", "_probeGroups", "_readReadiness"],
    "lib/model.dart": ["Future<void> setExperimental", "'experimental': experimental"],
    "lib/protocol.dart": ["'10 03', // UDS extended session", "'3E 00'"],
    "test/service_test.dart": ["parseReadiness", "@FUEL_LPH"],
}
for rel, needles in checks.items():
    text = (APP / rel).read_text(encoding="utf-8")
    miss = [m for m in needles if m not in text]
    if miss:
        raise RuntimeError(f"Самопроверка {rel}: нет маркеров {miss}")
print("[OK] самопроверка маркеров пройдена")

print("\n=== Готово: SSM2 0.13. Далее — ячейка 05 (сборка). ===")
print("Исправлено и добавлено:")
print("  · EXPERIMENTAL теперь рантайм: переключатель во вкладке DTC")
print("  · UDS-сбросы открывают сессию 10 03 автоматически")
print("  · сервисы: pending/permanent DTC, стоп-кадр, мониторы, VIN, CALID")
print("  · скан всех блоков и проба групп Clear Memory")
print("  · плитки: расход л/ч и л/100км, одометр, средний, IDC, запас насоса")
print("Чек-лист стенда 04.4:")
print(" [ ] дашборд: видны «Расход л/100км» и «Одометр» без настройки")
print(" [ ] вкладка DTC: «Скан всех блоков» проходит по 6 адресам")
print(" [ ] «Мониторы» показывают готовность после сброса ошибок")
print(" [ ] VIN/CALID читаются (CALID должен совпасть с A2TB100B)")
print(" [ ] переключатель «Экспертные операции» включается с подтверждением")
print(" [ ] «Проба групп» возвращает ответы ECU без побочных эффектов")
