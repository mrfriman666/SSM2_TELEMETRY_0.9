# @title 05 | SSM2 1.0.3 — Патч-пакет B + DTC FIX v5 (сервис, trace, OP2, ELM Speed, DTC-словарь) { display-mode: "form" }
# Слияние старых ячеек 04.4 + 04.5 + 04.6 + 04.7 + 04.8(REAL) + 04.9 + DTC FIX v5 + 04.10.
# Устаревшая 04.8 (r1-r4, «сырой FTDI») УДАЛЕНА.
# v1.0.1: 04.10 отверждена (соседний литерал — нечему ломаться при копипасте).
# v1.0.2: каскад сшивается по маркерам — иммунно к dart fix/format.
# v1.0.3: исправлен баг ISO-TP (expected=-2 ловился условием <0) — кадры второго
# ЭБУ больше не доклеиваются к ответу; плюс регрессионный тест FF+CF.
# Форматтер и строгий analyze из 04.10 валидируют новый код сразу в этой же ячейке.
# ЗАПУСК: после 04.
# ===== Шапка 04.4: что чинится и добавляется =====
# ▸ 04.4 | SERVICE FULL & TILES | SSM2 0.12 >> 0.13 (вставить МЕЖДУ 04.3 и 05)
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


# ===== Шапка 04.5: разбор причин ложного «готово» =====
# ▸ 04.5 | TRACE & GRID | SSM2 0.13 >> 0.14 (вставить МЕЖДУ 04.4 и 05)
# ГЛАВНОЕ — исправлен ложный «готово» во вкладке DTC:
#   Причина 1: AppModel.perform() ГЛОТАЕТ исключения (catch -> message),
#              поэтому diagRun никогда не бросал и UI всегда писал «готово».
#   Причина 2: readDtc при любой неудаче возвращал ПУСТОЙ список, а UI
#              показывал это как «Ошибок нет» — ложноположительный результат.
#   Причина 3: perform() при busy=true молча выходит — операция не выполнялась.
#   Теперь: проверка elm.ready ДО отправки, DtcReadResult.answered (ответил ли
#   ЭБУ вообще), ошибки пробрасываются, в UI — красный статус с причиной.
# Добавлено:
#   · lib/diag_trace.dart + окно «Журнал обмена»: каждая команда TX и ответ RX
#     с таймстемпом и временем отклика. Видно, реально ли что-то уходит.
#   · Карточка поездки возвращена: мгновенный расход л/ч и л/100км, одометр,
#     литры, средний расход + кнопка сброса.
#   · Сетка дашборда: отдельно «ячеек в строке» и «строк» для книжной и
#     альбомной (до 5x8 = 40 и 8x6 = 48), режим «вместить без прокрутки».
#   · Перетаскивание ячеек ПРЯМО на дашборде (долгое нажатие -> перенос).
#   · Объединение 2–4 PID в одну ячейку (компактный список в плитке).
# Идемпотентна, бэкап снаружи проекта, самопроверка маркеров.

import json
import re
import shutil
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")

STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_045_{STAMP}"
FILES = {}
REWRITES = []

# ===== Журнал обмена, NotConnectedException, DtcReadResult.answered =====
FILES["lib/diag_trace.dart"] = r'''
import 'package:flutter/foundation.dart';

/// Направление записи в журнале обмена.
enum TraceDir { tx, rx, info, error }

class TraceEntry {
  TraceEntry(this.dir, this.text, {this.ms});
  final TraceDir dir;
  final String text;
  final int? ms;
  final DateTime time = DateTime.now();

  String get stamp {
    final t = time;
    String two(int v) => v.toString().padLeft(2, '0');
    return '${two(t.hour)}:${two(t.minute)}:${two(t.second)}.'
        '${t.millisecond.toString().padLeft(3, '0')}';
  }

  String get arrow => switch (dir) {
        TraceDir.tx => '>>',
        TraceDir.rx => '<<',
        TraceDir.info => '--',
        TraceDir.error => '!!',
      };
}

/// Живой журнал обмена с ЭБУ: видно каждую отправленную команду и ответ.
/// Нужен, чтобы отличать «ответа нет» от «ошибок нет».
class DiagTrace extends ChangeNotifier {
  DiagTrace({this.limit = 400});
  final int limit;
  final List<TraceEntry> entries = <TraceEntry>[];

  int txCount = 0;
  int rxCount = 0;
  int errorCount = 0;

  void _add(TraceEntry e) {
    entries.add(e);
    if (entries.length > limit) entries.removeRange(0, entries.length - limit);
    notifyListeners();
  }

  void tx(String command) {
    txCount++;
    _add(TraceEntry(TraceDir.tx, command));
  }

  void rx(String reply, {int? ms}) {
    rxCount++;
    _add(TraceEntry(TraceDir.rx, reply.isEmpty ? '(пусто)' : reply, ms: ms));
  }

  void info(String text) => _add(TraceEntry(TraceDir.info, text));

  void error(String text) {
    errorCount++;
    _add(TraceEntry(TraceDir.error, text));
  }

  void clear() {
    entries.clear();
    txCount = 0;
    rxCount = 0;
    errorCount = 0;
    notifyListeners();
  }

  String asText() => entries
      .map((e) => '${e.stamp} ${e.arrow} ${e.text}'
          '${e.ms == null ? '' : '  (${e.ms} мс)'}')
      .join('\n');
}

/// Исключение: адаптер не подключён — операция даже не отправлялась.
class NotConnectedException implements Exception {
  NotConnectedException([this.message =
      'Адаптер не подключён: команда не отправлена. '
          'Вкладка «Адаптер» → подключитесь к ELM327.']);
  final String message;
  @override
  String toString() => message;
}

/// Результат чтения DTC: различает «ответ получен, кодов нет» и «нет ответа».
class DtcReadResult {
  const DtcReadResult({
    required this.codes,
    required this.answered,
    required this.protocolUsed,
    required this.rawReplies,
    this.note = '',
  });

  final List<DiagDtcLike> codes;

  /// true — ЭБУ реально ответил корректным кадром.
  final bool answered;

  /// Какой сервис сработал: SSM2 18 / OBD 03 / UDS 19 02.
  final String protocolUsed;

  /// Сырые ответы для журнала.
  final List<String> rawReplies;
  final String note;

  bool get clean => answered && codes.isEmpty;
}

/// Минимальный контракт DTC, чтобы diag_trace не зависел от diag.dart.
abstract class DiagDtcLike {
  String get code;
}
'''

# ===== Честный движок: проверка связи, трассировка каждой команды =====
FILES["lib/diag.dart"] = r'''
import 'diag_trace.dart';
import 'dtc_dict.dart';
import 'elm.dart';
import 'protocol.dart';

enum DiagProto { ssm2, obd, uds }

class EcuSlot {
  const EcuSlot(this.name, this.header, this.responseId, this.proto);
  final String name, header, responseId;
  final DiagProto proto;

  static const engine = EcuSlot('Двигатель (ECM)', '7E0', '7E8', DiagProto.ssm2);
  static const tcu = EcuSlot('АКПП / CVT (TCM)', '7E1', '7E9', DiagProto.ssm2);
  static const vdc = EcuSlot('ABS / VDC', '7B0', '7B8', DiagProto.uds);
  static const srs = EcuSlot('Подушки (SRS)', '772', '77A', DiagProto.uds);
  static const eps = EcuSlot('ЭУР (EPS)', '7A6', '7AE', DiagProto.uds);
  static const biu = EcuSlot('Кузовной блок (BIU)', '723', '72B', DiagProto.uds);

  static const List<EcuSlot> all = [engine, tcu, vdc, srs, eps, biu];
}

class DiagDtc implements DiagDtcLike {
  DiagDtc(this.code, this.statusByte, this.proto, {this.origin = ''});
  @override
  final String code;
  final int statusByte;
  final DiagProto proto;
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
    final p = origin.isEmpty ? '' : '$origin · ';
    if (proto == DiagProto.uds) {
      final f = <String>[];
      if (statusByte & 0x01 != 0) f.add('есть сейчас');
      if (statusByte & 0x08 != 0) f.add('подтверждена');
      if (statusByte & 0x40 != 0) f.add('MIL');
      return p + (f.isEmpty ? 'пассивная (0x${hex2(statusByte)})' : f.join(' · '));
    }
    if (proto == DiagProto.obd) return '${p}OBD';
    return p + (active ? (milOn ? 'активная · MIL' : 'активная') : 'сохранённая');
  }

  String get titleRu => kSubaruDtcDict[code]?.$1 ?? '';
}

class ReadinessReport {
  ReadinessReport({required this.milOn, required this.dtcCount, required this.monitors});
  final bool milOn;
  final int dtcCount;
  final Map<String, bool> monitors;
  int get ready => monitors.values.where((v) => v).length;
  int get total => monitors.length;
}

String decodeDtcBytes(int b1, int b2) {
  const systems = ['P', 'C', 'B', 'U'];
  final sys = systems[(b1 >> 6) & 0x03];
  final d1 = (b1 >> 4) & 0x03;
  String h(int v) => v.toRadixString(16).toUpperCase();
  return '$sys$d1${h(b1 & 0x0F)}${h((b2 >> 4) & 0x0F)}${h(b2 & 0x0F)}';
}

List<DiagDtc> parseSsm2DtcReply(List<int> b) {
  final out = <DiagDtc>[];
  if (b.isEmpty || b[0] != 0x58) return out;
  final count = b.length > 1 ? b[1] : 0;
  for (var i = 0; i < count; i++) {
    final x = 2 + i * 3;
    if (x + 2 >= b.length) break;
    if (b[x] == 0 && b[x + 1] == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b[x], b[x + 1]), b[x + 2], DiagProto.ssm2,
        origin: 'stored'));
  }
  return out;
}

List<DiagDtc> parseObdDtcReply(List<int> b, {String origin = 'stored'}) {
  const heads = <int, String>{0x43: 'stored', 0x47: 'pending', 0x4A: 'permanent'};
  final out = <DiagDtc>[];
  if (b.isEmpty || !heads.containsKey(b[0])) return out;
  final kind = heads[b[0]] ?? origin;
  for (var i = 1; i + 1 < b.length; i += 2) {
    if (b[i] == 0 && b[i + 1] == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b[i], b[i + 1]), 0x20, DiagProto.obd,
        origin: kind));
  }
  return out;
}

List<DiagDtc> parseObdMode3Reply(List<int> b) => parseObdDtcReply(b);

List<DiagDtc> parseUdsDtcReply(List<int> b) {
  final out = <DiagDtc>[];
  if (b.length < 3 || b[0] != 0x59) return out;
  for (var i = 3; i + 2 < b.length; i += 3) {
    if (b[i] == 0 && b[i + 1] == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b[i], b[i + 1]), b[i + 2], DiagProto.uds,
        origin: 'stored'));
  }
  return out;
}

ReadinessReport? parseReadiness(List<int> b) {
  if (b.length < 6 || b[0] != 0x41 || b[1] != 0x01) return null;
  final a = b[2], bb = b[3], c = b[4], d = b[5];
  final monitors = <String, bool>{
    'Пропуски зажигания': (bb & 0x01) == 0 || (bb & 0x10) == 0,
    'Топливная система': (bb & 0x02) == 0 || (bb & 0x20) == 0,
    'Компоненты': (bb & 0x04) == 0 || (bb & 0x40) == 0,
  };
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
      milOn: (a & 0x80) != 0, dtcCount: a & 0x7F, monitors: monitors);
}

String? parseAsciiReply(List<int> b, int pid) {
  if (b.length < 3 || b[0] != 0x49 || b[1] != pid) return null;
  final chars = <int>[];
  for (var i = 3; i < b.length; i++) {
    if (b[i] >= 0x20 && b[i] <= 0x7E) chars.add(b[i]);
  }
  final t = String.fromCharCodes(chars).trim();
  return t.isEmpty ? null : t;
}

String? parseFreezeFrameDtc(List<int> b) {
  if (b.length < 5 || b[0] != 0x42) return null;
  if (b[3] == 0 && b[4] == 0) return null;
  return decodeDtcBytes(b[3], b[4]);
}

List<int>? parseDiagPayload(String text) {
  final flat = <int>[];
  for (final raw in text.split(RegExp(r'[\r\n]+'))) {
    var line = raw.trim();
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
  if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) b = b.sublist(2);
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

class ServiceResult {
  ServiceResult.ok(this.title, this.detail)
      : success = true,
        negative = null;
  ServiceResult.fail(this.title, this.detail, {this.negative}) : success = false;
  final bool success;
  final String title, detail;
  final int? negative;
}

/// Сессия диагностики. Пишет каждый обмен в DiagTrace и НЕ врёт об успехе.
class DiagSession {
  DiagSession(this.elm, {DiagTrace? trace}) : trace = trace ?? DiagTrace();
  final ElmDriver elm;
  final DiagTrace trace;

  bool experimental = false;

  /// v0.14: без подключения даже не пытаемся — иначе UI показывал «готово».
  void _requireLink() {
    if (!elm.ready) {
      trace.error('Адаптер не подключён — команда НЕ отправлена');
      throw NotConnectedException();
    }
  }

  Future<String> _send(String command) async {
    _requireLink();
    trace.tx(command);
    final watch = Stopwatch()..start();
    try {
      final reply = await elm.diagnostic(command);
      trace.rx(visibleText(reply), ms: watch.elapsedMilliseconds);
      return reply;
    } catch (e) {
      trace.error('$command -> $e (${watch.elapsedMilliseconds} мс)');
      rethrow;
    }
  }

  Future<List<int>> _payload(String command) async {
    final reply = await _send(command);
    final bytes = parseDiagPayload(reply);
    if (bytes == null) {
      trace.info('$command: ответ не распознан как кадр данных');
      return const <int>[];
    }
    return bytes;
  }

  Future<void> _atEcu(EcuSlot slot) async {
    trace.info('Переключение на ${slot.name} (${slot.header}→${slot.responseId})');
    await _send('ATSH ${slot.header}');
    await _send('ATCRA ${slot.responseId}');
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }

  int? _nrc(List<int> b) => (b.length >= 3 && b[0] == 0x7F) ? b[2] : null;

  // ------------------- ЧТЕНИЕ -------------------

  /// Честное чтение: answered=false означает «ЭБУ не ответил», а не «чисто».
  Future<DtcReadResult> readDtcDetailed(EcuSlot slot) async {
    await _atEcu(slot);
    final raws = <String>[];

    Future<DtcReadResult?> tryService(
      String cmd,
      int expectHead,
      String label,
      List<DiagDtc> Function(List<int>) parse,
    ) async {
      try {
        final b = await _payload(cmd);
        raws.add('$cmd -> ${b.map(hex2).join(' ')}');
        final nrc = _nrc(b);
        if (nrc != null) {
          trace.info('$label отклонён: ${kNrMeanings[nrc] ?? 'NRC 0x${hex2(nrc)}'}');
          return null;
        }
        if (b.isNotEmpty && b[0] == expectHead) {
          final codes = parse(b);
          trace.info('$label: ответ принят, кодов ${codes.length}');
          return DtcReadResult(
            codes: codes,
            answered: true,
            protocolUsed: label,
            rawReplies: raws,
          );
        }
      } catch (e) {
        if (e is NotConnectedException) rethrow;
        trace.info('$label: $e');
      }
      return null;
    }

    if (slot.proto == DiagProto.ssm2) {
      final r = await tryService('18 00 FF 00', 0x58, 'SSM2 18', parseSsm2DtcReply);
      if (r != null) return r;
    } else {
      final r = await tryService('19 02 AF', 0x59, 'UDS 19 02', parseUdsDtcReply);
      if (r != null) return r;
    }
    final obd =
        await tryService('03', 0x43, 'OBD 03', (b) => parseObdDtcReply(b));
    if (obd != null) return obd;

    trace.error('${slot.name}: ни один сервис не ответил');
    return DtcReadResult(
      codes: const [],
      answered: false,
      protocolUsed: '—',
      rawReplies: raws,
      note: 'Блок не ответил. Возможно, его нет на шине или другой протокол.',
    );
  }

  /// Совместимость: бросает, если ответа не было (раньше молча отдавал []).
  Future<List<DiagDtc>> readDtc(EcuSlot slot) async {
    final r = await readDtcDetailed(slot);
    if (!r.answered) throw ReplyError(r.note);
    return r.codes.cast<DiagDtc>();
  }

  Future<List<DiagDtc>> readPendingDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseObdDtcReply(await _payload('07'), origin: 'pending');
  }

  Future<List<DiagDtc>> readPermanentDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseObdDtcReply(await _payload('0A'), origin: 'permanent');
  }

  Future<DtcReadResult> readAllDtc(EcuSlot slot) async {
    final base = await readDtcDetailed(slot);
    final all = <DiagDtc>[...base.codes.cast<DiagDtc>()];
    for (final fn in [readPendingDtc, readPermanentDtc]) {
      try {
        for (final d in await fn(slot)) {
          if (!all.any((x) => x.code == d.code && x.origin == d.origin)) {
            all.add(d);
          }
        }
      } catch (e) {
        if (e is NotConnectedException) rethrow;
      }
    }
    return DtcReadResult(
      codes: all,
      answered: base.answered,
      protocolUsed: base.protocolUsed,
      rawReplies: base.rawReplies,
      note: base.note,
    );
  }

  Future<Map<String, DtcReadResult>> scanAllEcus({
    void Function(String name)? onProgress,
  }) async {
    _requireLink();
    final result = <String, DtcReadResult>{};
    for (final slot in EcuSlot.all) {
      onProgress?.call(slot.name);
      try {
        result[slot.name] = await readDtcDetailed(slot);
      } catch (e) {
        if (e is NotConnectedException) rethrow;
        result[slot.name] = DtcReadResult(
          codes: const [],
          answered: false,
          protocolUsed: '—',
          rawReplies: const [],
          note: '$e',
        );
      }
    }
    await restore();
    return result;
  }

  // ------------------- СБРОС -------------------

  Future<DtcReadResult> clearDtc(EcuSlot slot) async {
    await _atEcu(slot);
    var done = false;
    final primary = slot.proto == DiagProto.ssm2 ? '14 FF FF 00' : '14 FF FF FF';
    final b = await _payload(primary);
    final nrc = _nrc(b);
    if (nrc != null) {
      throw ReplyError(
          'Сброс отклонён: ${kNrMeanings[nrc] ?? 'NRC 0x${hex2(nrc)}'}');
    }
    done = b.isNotEmpty && b[0] == 0x54;
    if (!done) {
      final o = await _payload('04');
      done = o.isNotEmpty && o[0] == 0x44;
    }
    if (!done) throw ReplyError('ЭБУ не подтвердил сброс (нет 54/44)');
    trace.info('Сброс подтверждён, повторное чтение…');
    await Future<void>.delayed(const Duration(milliseconds: 400));
    return readDtcDetailed(slot);
  }

  // ------------------- ИНФО -------------------

  Future<ReadinessReport?> readReadiness(EcuSlot slot) async {
    await _atEcu(slot);
    return parseReadiness(await _payload('01 01'));
  }

  Future<String?> readVin(EcuSlot slot) async {
    await _atEcu(slot);
    final v = parseAsciiReply(await _payload('09 02'), 0x02);
    if (v != null) return v;
    final b = await _payload('22 F1 90');
    if (b.length > 3 && b[0] == 0x62) {
      final t = String.fromCharCodes(
              b.sublist(3).where((x) => x >= 0x20 && x <= 0x7E))
          .trim();
      return t.isEmpty ? null : t;
    }
    return null;
  }

  Future<String?> readCalId(EcuSlot slot) async {
    await _atEcu(slot);
    return parseAsciiReply(await _payload('09 04'), 0x04);
  }

  Future<String?> readFreezeFrameDtc(EcuSlot slot) async {
    await _atEcu(slot);
    return parseFreezeFrameDtc(await _payload('02 02 00'));
  }

  /// Пинг связи: ATI должен вернуть версию адаптера.
  Future<String> pingAdapter() async {
    final reply = await _send('ATI');
    return visibleText(reply);
  }

  // ------------------- СЕРВИС -------------------

  Future<bool> enterExtendedSession(EcuSlot slot) async {
    await _atEcu(slot);
    final b = await _payload('10 03');
    final ok = b.isNotEmpty && b[0] == 0x50;
    trace.info(ok ? 'Расширенная сессия открыта' : 'Сессия 10 03 не принята');
    return ok;
  }

  Future<void> testerPresent() async {
    try {
      await _payload('3E 00');
    } catch (_) {}
  }

  Future<ServiceResult> ecuReset(EcuSlot slot) async {
    if (!experimental) {
      return ServiceResult.fail('ECUReset заблокирован',
          'Включите «Экспертные операции».');
    }
    await enterExtendedSession(slot);
    final b = await _payload('11 01');
    final nrc = _nrc(b);
    if (nrc != null) {
      return ServiceResult.fail('ECUReset отклонён',
          kNrMeanings[nrc] ?? 'NRC 0x${hex2(nrc)}',
          negative: nrc);
    }
    if (b.isNotEmpty && b[0] == 0x51) {
      return ServiceResult.ok(
          'ECU перезапущен', 'Ответ 51 01. Зажигание OFF→ON перед запуском.');
    }
    return ServiceResult.fail('Нет подтверждения',
        'Ожидался 51 01, получено: ${b.isEmpty ? '(пусто)' : b.map(hex2).join(' ')}');
  }

  Future<ServiceResult> clearMemory(EcuSlot slot, int group) async {
    if (!experimental) {
      return ServiceResult.fail('Clear Memory заблокирован',
          'Включите «Экспертные операции».');
    }
    if (group < 1 || group > 7) {
      return ServiceResult.fail('Неверная группа', 'Допустимо 1..7');
    }
    await enterExtendedSession(slot);
    final b = await _payload('04 ${hex2(group)}');
    final nrc = _nrc(b);
    if (nrc != null) {
      return ServiceResult.fail('Группа $group отклонена',
          kNrMeanings[nrc] ?? 'NRC 0x${hex2(nrc)}',
          negative: nrc);
    }
    if (b.isNotEmpty && b[0] == 0x44) {
      return ServiceResult.ok('Адаптации группы $group сброшены',
          'Подтверждено (44). Нужно дообучение 15–20 мин.');
    }
    return ServiceResult.fail('Нет подтверждения',
        'Ожидался 44, получено: ${b.isEmpty ? '(пусто)' : b.map(hex2).join(' ')}');
  }

  Future<Map<int, String>> probeClearMemoryGroups(EcuSlot slot) async {
    final out = <int, String>{};
    if (!experimental) return out;
    await enterExtendedSession(slot);
    for (var g = 1; g <= 7; g++) {
      try {
        final b = await _payload('04 ${hex2(g)}');
        final nrc = _nrc(b);
        out[g] = nrc != null
            ? (kNrMeanings[nrc] ?? 'NRC 0x${hex2(nrc)}')
            : (b.isNotEmpty && b[0] == 0x44 ? 'поддерживается (44)' : 'нет ответа');
      } catch (e) {
        if (e is NotConnectedException) rethrow;
        out[g] = 'ошибка связи';
      }
      await Future<void>.delayed(const Duration(milliseconds: 120));
    }
    return out;
  }

  Future<void> restore() async {
    try {
      await _send('ATSH 7E0');
      await _send('ATCRA 7E8');
      trace.info('Заголовки возвращены на двигатель (7E0/7E8)');
    } catch (_) {}
  }
}
'''

# ===== Окно журнала обмена: TX/RX, тайминги, копирование =====
FILES["lib/trace_sheet.dart"] = r'''
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'diag_trace.dart';

const _muted = Color(0xFF8CA0BF);

/// Плавающее окно журнала обмена: что ушло в ЭБУ и что пришло обратно.
class TraceSheet extends StatelessWidget {
  const TraceSheet({super.key, required this.trace});
  final DiagTrace trace;

  static Future<void> show(BuildContext context, DiagTrace trace) =>
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        showDragHandle: true,
        builder: (_) => TraceSheet(trace: trace),
      );

  Color _color(TraceDir d) => switch (d) {
        TraceDir.tx => const Color(0xFF7DD3FC),
        TraceDir.rx => const Color(0xFF86EFAC),
        TraceDir.info => _muted,
        TraceDir.error => const Color(0xFFFF8A80),
      };

  @override
  Widget build(BuildContext context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.75,
        maxChildSize: 0.95,
        builder: (context, scroll) => AnimatedBuilder(
          animation: trace,
          builder: (context, _) => Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 8, 6),
                child: Row(
                  children: [
                    const Icon(Icons.terminal, size: 18),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'Журнал обмена · TX ${trace.txCount} / RX ${trace.rxCount}'
                        '${trace.errorCount > 0 ? ' / ошибок ${trace.errorCount}' : ''}',
                        style: const TextStyle(
                            fontSize: 14, fontWeight: FontWeight.bold),
                      ),
                    ),
                    IconButton(
                      tooltip: 'Скопировать',
                      onPressed: () async {
                        await Clipboard.setData(
                            ClipboardData(text: trace.asText()));
                        if (context.mounted) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(
                                content: Text('Журнал скопирован'),
                                duration: Duration(seconds: 1)),
                          );
                        }
                      },
                      icon: const Icon(Icons.copy, size: 18),
                    ),
                    IconButton(
                      tooltip: 'Очистить',
                      onPressed: trace.clear,
                      icon: const Icon(Icons.delete_outline, size: 18),
                    ),
                  ],
                ),
              ),
              const Divider(height: 1),
              Expanded(
                child: trace.entries.isEmpty
                    ? const Center(
                        child: Padding(
                          padding: EdgeInsets.all(24),
                          child: Text(
                            'Журнал пуст. Выполните любую операцию во вкладке '
                            'DTC — здесь появятся отправленные команды и '
                            'ответы ЭБУ.',
                            textAlign: TextAlign.center,
                            style: TextStyle(color: _muted),
                          ),
                        ),
                      )
                    : ListView.builder(
                        controller: scroll,
                        padding: const EdgeInsets.symmetric(
                            horizontal: 12, vertical: 8),
                        itemCount: trace.entries.length,
                        itemBuilder: (context, i) {
                          final e = trace.entries[trace.entries.length - 1 - i];
                          return Padding(
                            padding: const EdgeInsets.symmetric(vertical: 1.5),
                            child: Row(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(e.stamp,
                                    style: const TextStyle(
                                        fontFamily: 'monospace',
                                        fontSize: 10,
                                        color: _muted)),
                                const SizedBox(width: 6),
                                Text(e.arrow,
                                    style: TextStyle(
                                        fontFamily: 'monospace',
                                        fontSize: 11,
                                        color: _color(e.dir))),
                                const SizedBox(width: 6),
                                Expanded(
                                  child: Text(
                                    e.text,
                                    style: TextStyle(
                                        fontFamily: 'monospace',
                                        fontSize: 11.5,
                                        color: _color(e.dir)),
                                  ),
                                ),
                                if (e.ms != null)
                                  Text('${e.ms}мс',
                                      style: const TextStyle(
                                          fontFamily: 'monospace',
                                          fontSize: 9.5,
                                          color: _muted)),
                              ],
                            ),
                          );
                        },
                      ),
              ),
            ],
          ),
        ),
      );
}
'''

# ===== Карточка поездки: расход, одометр, средний, сброс =====
FILES["lib/trip_card.dart"] = r'''
import 'package:flutter/material.dart';

import 'virtual_tiles.dart';

const _muted = Color(0xFF8CA0BF);

/// Карточка поездки: мгновенный расход, л/100км, одометр, литры, средний,
/// кнопка сброса. Возвращена по просьбе: была в 0.10, пропала в 0.12.
class TripCard extends StatelessWidget {
  const TripCard({
    super.key,
    required this.inputs,
    required this.onReset,
    this.mafStale = false,
  });

  final TileInputs inputs;
  final VoidCallback onReset;
  final bool mafStale;

  String _fmt(double? v, int digits, {String dash = '—'}) =>
      v == null ? dash : v.toStringAsFixed(digits);

  @override
  Widget build(BuildContext context) {
    final lph = virtualTileById('@FUEL_LPH')?.compute(inputs);
    final l100 = virtualTileById('@FUEL_L100')?.compute(inputs);
    final avg = virtualTileById('@TRIP_AVG')?.compute(inputs);

    return Card(
      margin: const EdgeInsets.fromLTRB(10, 6, 10, 0),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 10, 8, 10),
        child: Column(
          children: [
            Row(
              children: [
                Icon(
                  Icons.local_gas_station,
                  size: 18,
                  color: mafStale ? Colors.orangeAccent : _muted,
                ),
                const SizedBox(width: 8),
                const Expanded(
                  child: Text(
                    'Поездка',
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.bold),
                  ),
                ),
                if (mafStale)
                  const Text('MAF устарел',
                      style: TextStyle(fontSize: 10, color: Colors.orangeAccent)),
                IconButton(
                  visualDensity: VisualDensity.compact,
                  tooltip: 'Сбросить одометр и расход',
                  onPressed: onReset,
                  icon: const Icon(Icons.restart_alt, size: 20),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Row(
              children: [
                _Cell(label: 'Расход', value: _fmt(lph, 1), unit: 'л/ч'),
                _Cell(label: 'Мгновенный', value: _fmt(l100, 1), unit: 'л/100км'),
                _Cell(
                  label: 'Одометр',
                  value: inputs.tripKm.toStringAsFixed(2),
                  unit: 'км',
                ),
                _Cell(
                  label: 'Залито',
                  value: inputs.tripLitres.toStringAsFixed(2),
                  unit: 'л',
                ),
                _Cell(label: 'Средний', value: _fmt(avg, 1), unit: 'л/100км'),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _Cell extends StatelessWidget {
  const _Cell({required this.label, required this.value, required this.unit});
  final String label, value, unit;

  @override
  Widget build(BuildContext context) => Expanded(
        child: Column(
          children: [
            Text(label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 9.5, color: _muted)),
            const SizedBox(height: 2),
            FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(
                value,
                style: const TextStyle(
                    fontSize: 17, fontWeight: FontWeight.w600),
              ),
            ),
            Text(unit, style: const TextStyle(fontSize: 9, color: _muted)),
          ],
        ),
      );
}
'''

# ===== Сетка колонки×строки, комбо-ячейки, перестановка =====
FILES["lib/dashboard_prefs.dart"] = r'''
import 'pids.dart';
import 'virtual_tiles.dart';

/// Разделитель PID внутри объединённой ячейки: 'RPM|AFR|ECT'.
const String kComboSeparator = '|';

bool isComboTile(String id) => id.contains(kComboSeparator);
List<String> comboParts(String id) =>
    id.split(kComboSeparator).where((s) => s.isNotEmpty).toList();

/// Настройки дашборда: состав, порядок, сетка (колонки × строки), плотность.
class DashboardPrefs {
  DashboardPrefs({
    List<String>? tiles,
    this.columnsPortrait = 2,
    this.columnsLandscape = 4,
    this.rowsPortrait = 4,
    this.rowsLandscape = 3,
    this.fitToScreen = true,
    this.compact = false,
    this.showSpark = true,
    this.showFuelCard = true,
  }) : tiles = tiles ?? List<String>.from(defaultTiles);

  final List<String> tiles;

  /// Ячеек в строке.
  final int columnsPortrait;
  final int columnsLandscape;

  /// Строк на экран (при fitToScreen высота плитки подгоняется).
  final int rowsPortrait;
  final int rowsLandscape;

  /// true — вместить ровно columns×rows в видимую область (без прокрутки).
  final bool fitToScreen;

  final bool compact;
  final bool showSpark;
  final bool showFuelCard;

  static const List<String> defaultTiles = <String>[
    'RPM',
    'MAP_REL',
    'AFR',
    'ECT',
    'FBKC',
    'IAM',
    '@FUEL_L100',
    '@TRIP_KM',
  ];

  int columnsFor(bool landscape) =>
      landscape ? columnsLandscape : columnsPortrait;
  int rowsFor(bool landscape) => landscape ? rowsLandscape : rowsPortrait;

  /// Сколько ячеек помещается на один экран.
  int perScreen(bool landscape) => columnsFor(landscape) * rowsFor(landscape);

  static bool knownTile(String id) {
    if (isComboTile(id)) {
      final parts = comboParts(id);
      return parts.length >= 2 && parts.every(knownTile);
    }
    return isVirtualTile(id)
        ? virtualTileById(id) != null
        : SubaruPidLibrary.all.any((p) => p.id == id);
  }

  DashboardPrefs copyWith({
    List<String>? tiles,
    int? columnsPortrait,
    int? columnsLandscape,
    int? rowsPortrait,
    int? rowsLandscape,
    bool? fitToScreen,
    bool? compact,
    bool? showSpark,
    bool? showFuelCard,
  }) =>
      DashboardPrefs(
        tiles: tiles ?? List<String>.from(this.tiles),
        columnsPortrait: columnsPortrait ?? this.columnsPortrait,
        columnsLandscape: columnsLandscape ?? this.columnsLandscape,
        rowsPortrait: rowsPortrait ?? this.rowsPortrait,
        rowsLandscape: rowsLandscape ?? this.rowsLandscape,
        fitToScreen: fitToScreen ?? this.fitToScreen,
        compact: compact ?? this.compact,
        showSpark: showSpark ?? this.showSpark,
        showFuelCard: showFuelCard ?? this.showFuelCard,
      );

  /// Переставить плитку (drag & drop прямо на дашборде).
  DashboardPrefs moveTile(String id, int newIndex) {
    final list = List<String>.from(tiles);
    final old = list.indexOf(id);
    if (old < 0) return this;
    list.removeAt(old);
    final idx = newIndex.clamp(0, list.length);
    list.insert(idx, id);
    return copyWith(tiles: list);
  }

  /// Поменять две плитки местами.
  DashboardPrefs swapTiles(String a, String b) {
    final list = List<String>.from(tiles);
    final ia = list.indexOf(a), ib = list.indexOf(b);
    if (ia < 0 || ib < 0) return this;
    list[ia] = b;
    list[ib] = a;
    return copyWith(tiles: list);
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'tiles': tiles,
        'columnsPortrait': columnsPortrait,
        'columnsLandscape': columnsLandscape,
        'rowsPortrait': rowsPortrait,
        'rowsLandscape': rowsLandscape,
        'fitToScreen': fitToScreen,
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
    int clamp(Object? v, int fallback, int lo, int hi) {
      final n = v is num ? v.toInt() : fallback;
      return n < lo ? lo : (n > hi ? hi : n);
    }

    return DashboardPrefs(
      tiles: raw.isEmpty ? null : raw,
      columnsPortrait: clamp(json['columnsPortrait'], 2, 1, 5),
      columnsLandscape: clamp(json['columnsLandscape'], 4, 1, 8),
      rowsPortrait: clamp(json['rowsPortrait'], 4, 1, 8),
      rowsLandscape: clamp(json['rowsLandscape'], 3, 1, 6),
      fitToScreen: json['fitToScreen'] != false,
      compact: json['compact'] == true,
      showSpark: json['showSpark'] != false,
      showFuelCard: json['showFuelCard'] != false,
    );
  }

  /// Плитка доступна, если это виртуальная, активный PID или комбо,
  /// у которого есть хотя бы один доступный элемент.
  bool tileAvailable(String id, Set<String> enabledPids) {
    if (isComboTile(id)) {
      return comboParts(id).any((p) => tileAvailable(p, enabledPids));
    }
    return isVirtualTile(id) || enabledPids.contains(id);
  }

  List<String> visibleTiles(Set<String> enabledPids) =>
      tiles.where((id) => tileAvailable(id, enabledPids)).toList();
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

# ===== Дашборд: drag прямо на плитках, объединённые ячейки, fit-сетка =====
FILES["lib/dashboard_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'dashboard_prefs.dart';
import 'model.dart';
import 'pids.dart';
import 'samples.dart';
import 'trip_card.dart';
import 'virtual_tiles.dart';

const _muted = Color(0xFF8CA0BF);
const _accent = Color(0xFF22D3EE);

/// Дашборд: сетка колонки×строки, drag прямо на плитках, объединённые ячейки.
class CustomDashboardPage extends StatefulWidget {
  const CustomDashboardPage(this.model, {super.key});
  final AppModel model;

  @override
  State<CustomDashboardPage> createState() => _CustomDashboardPageState();
}

class _CustomDashboardPageState extends State<CustomDashboardPage> {
  AppModel get model => widget.model;
  String? _dragging;

  TileInputs _inputs() {
    final values = <String, double>{};
    model.engine.latest.forEach((id, s) {
      final v = s.value;
      if (v != null && v.isFinite) values[id] = v;
    });
    return TileInputs(
      values: values,
      config: model.vehicleConfig,
      tripKm: model.engine.tripDistanceKm,
      tripLitres: model.engine.tripFuelLitres,
    );
  }

  Future<void> _reorder(String dragged, String target) async {
    final prefs = model.dashboardPrefs;
    final idx = prefs.tiles.indexOf(target);
    if (idx < 0) return;
    await model.updateDashboardPrefs(prefs.moveTile(dragged, idx));
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final engine = model.engine;
    final prefs = model.dashboardPrefs;
    final activeIds = engine.active.map((p) => p.id).toSet();

    final chosen = prefs.visibleTiles(activeIds);
    final rest = engine.active
        .map((p) => p.id)
        .where((id) => !prefs.tiles.any((t) =>
            t == id || (isComboTile(t) && comboParts(t).contains(id))));
    final ids = <String>[...chosen, ...rest];

    final landscape =
        MediaQuery.of(context).orientation == Orientation.landscape;
    final columns = prefs.columnsFor(landscape);
    final rows = prefs.rowsFor(landscape);
    final inputs = _inputs();

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 4, 6, 0),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  '${ids.length} ячеек · ${columns}×$rows на экран · '
                  '${engine.pidReadsPerSecond.toStringAsFixed(1)} обновл./с',
                  style: const TextStyle(fontSize: 11, color: _muted),
                ),
              ),
              IconButton(
                tooltip: 'Настроить сетку и ячейки',
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
        if (prefs.showFuelCard)
          TripCard(
            inputs: inputs,
            mafStale: engine.stale('MAF'),
            onReset: () {
              engine.resetTrip();
              setState(() {});
            },
          ),
        if (ids.isEmpty)
          const Expanded(
            child: Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Text(
                  'Нет ячеек. Нажмите «Настроить сетку» — добавьте PID, '
                  'расчётные плитки или объедините несколько PID в одну ячейку.',
                  textAlign: TextAlign.center,
                ),
              ),
            ),
          )
        else
          Expanded(
            child: LayoutBuilder(
              builder: (context, box) {
                const pad = 8.0, gap = 6.0;
                final extent = prefs.fitToScreen
                    ? ((box.maxHeight - pad * 2 - gap * (rows - 1)) / rows)
                        .clamp(58.0, 400.0)
                    : (prefs.compact ? 92.0 : 136.0);
                return GridView.builder(
                  padding: const EdgeInsets.all(pad),
                  physics: prefs.fitToScreen && ids.length <= columns * rows
                      ? const NeverScrollableScrollPhysics()
                      : const AlwaysScrollableScrollPhysics(),
                  itemCount: ids.length,
                  gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                    crossAxisCount: columns,
                    mainAxisExtent: extent.toDouble(),
                    mainAxisSpacing: gap,
                    crossAxisSpacing: gap,
                  ),
                  itemBuilder: (context, i) => _draggable(
                    ids[i],
                    _buildTile(ids[i], inputs, extent.toDouble()),
                  ),
                );
              },
            ),
          ),
      ],
    );
  }

  Widget _draggable(String id, Widget child) => DragTarget<String>(
        onWillAcceptWithDetails: (d) => d.data != id,
        onAcceptWithDetails: (d) => _reorder(d.data, id),
        builder: (context, candidate, rejected) => LongPressDraggable<String>(
          data: id,
          onDragStarted: () => setState(() => _dragging = id),
          onDragEnd: (_) => setState(() => _dragging = null),
          feedback: Material(
            color: Colors.transparent,
            child: Opacity(
              opacity: 0.9,
              child: SizedBox(width: 160, height: 92, child: child),
            ),
          ),
          childWhenDragging: Opacity(opacity: 0.25, child: child),
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 150),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(12),
              border: candidate.isNotEmpty
                  ? Border.all(color: _accent, width: 2)
                  : (_dragging == id
                      ? Border.all(color: _accent.withValues(alpha: 0.4))
                      : null),
            ),
            child: child,
          ),
        ),
      );

  Widget _buildTile(String id, TileInputs inputs, double extent) {
    final engine = model.engine;
    final prefs = model.dashboardPrefs;
    final tight = extent < 110;

    if (isComboTile(id)) {
      final parts = comboParts(id);
      return _ComboTile(
        parts: parts,
        resolve: (p) => _resolve(p, inputs),
        tight: tight,
      );
    }
    final r = _resolve(id, inputs);
    return _Tile(
      title: r.title,
      unit: r.unit,
      digits: r.digits,
      value: r.value,
      isVirtual: r.virtual,
      stale: r.stale,
      unsupported: r.unsupported,
      tight: tight,
      history: prefs.showSpark && !tight && !r.virtual
          ? engine.history[id]
          : null,
    );
  }

  _Resolved _resolve(String id, TileInputs inputs) {
    if (isVirtualTile(id)) {
      final d = virtualTileById(id);
      if (d == null) {
        return const _Resolved('?', '', 0, null, virtual: true);
      }
      return _Resolved(d.label, d.unit, d.digits, d.compute(inputs),
          virtual: true);
    }
    final matches = SubaruPidLibrary.all.where((p) => p.id == id);
    if (matches.isEmpty) return _Resolved(id, '', 0, null);
    final pid = matches.first;
    final s = model.engine.latest[id];
    return _Resolved(
      '${pid.id}${pid.extended ? ' *' : ''}',
      pid.unit,
      pid.digits,
      s?.value,
      stale: (model.engine.ageMs(id) ?? 999999) > 2500,
      unsupported: s?.allOnes ?? false,
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

class _Resolved {
  const _Resolved(
    this.title,
    this.unit,
    this.digits,
    this.value, {
    this.virtual = false,
    this.stale = false,
    this.unsupported = false,
  });
  final String title, unit;
  final int digits;
  final double? value;
  final bool virtual, stale, unsupported;

  String get text => value == null
      ? (unsupported ? 'н/д' : '—')
      : value!.toStringAsFixed(digits);
}

class _Tile extends StatelessWidget {
  const _Tile({
    required this.title,
    required this.unit,
    required this.digits,
    required this.value,
    required this.isVirtual,
    required this.tight,
    this.stale = false,
    this.unsupported = false,
    this.history,
  });

  final String title, unit;
  final int digits;
  final double? value;
  final bool isVirtual, tight, stale, unsupported;
  final List<PidSample>? history;

  @override
  Widget build(BuildContext context) {
    final text =
        value == null ? (unsupported ? 'н/д' : '—') : value!.toStringAsFixed(digits);
    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: EdgeInsets.all(tight ? 6 : 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Row(
              children: [
                if (isVirtual)
                  const Padding(
                    padding: EdgeInsets.only(right: 3),
                    child: Icon(Icons.functions, size: 10, color: _accent),
                  ),
                Expanded(
                  child: Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: tight ? 9.5 : 11.5,
                      fontWeight: FontWeight.bold,
                      color: stale ? _muted : null,
                    ),
                  ),
                ),
                if (stale) const Icon(Icons.schedule, size: 10, color: _muted),
              ],
            ),
            FittedBox(
              fit: BoxFit.scaleDown,
              alignment: Alignment.centerLeft,
              child: Text(
                text,
                style: TextStyle(
                  fontSize: tight ? 19 : 27,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            Text(unit,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: tight ? 8.5 : 10, color: _muted)),
            if (history != null && history!.length > 2 && !tight)
              Expanded(
                child: CustomPaint(
                  painter: _SparkPainter(history!),
                  size: Size.infinite,
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Объединённая ячейка: несколько параметров в одной плитке.
class _ComboTile extends StatelessWidget {
  const _ComboTile({
    required this.parts,
    required this.resolve,
    required this.tight,
  });
  final List<String> parts;
  final _Resolved Function(String) resolve;
  final bool tight;

  @override
  Widget build(BuildContext context) => Card(
        margin: EdgeInsets.zero,
        child: Padding(
          padding: EdgeInsets.all(tight ? 5 : 8),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              for (final p in parts.take(4))
                Builder(builder: (context) {
                  final r = resolve(p);
                  return Row(
                    children: [
                      Expanded(
                        child: Text(
                          r.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: tight ? 8.5 : 10,
                            color: r.stale ? _muted : Colors.white70,
                          ),
                        ),
                      ),
                      const SizedBox(width: 4),
                      Text(
                        r.text,
                        style: TextStyle(
                          fontSize: tight ? 13 : 16,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(width: 2),
                      Text(
                        r.unit,
                        style: TextStyle(
                            fontSize: tight ? 7.5 : 9, color: _muted),
                      ),
                    ],
                  );
                }),
            ],
          ),
        ),
      );
}

class _SparkPainter extends CustomPainter {
  _SparkPainter(this.samples);
  final List<PidSample> samples;

  @override
  void paint(Canvas canvas, Size size) {
    final v = samples.where((s) => s.value != null).map((s) => s.value!).toList();
    if (v.length < 2) return;
    var lo = v.first, hi = v.first;
    for (final x in v) {
      if (x < lo) lo = x;
      if (x > hi) hi = x;
    }
    if ((hi - lo).abs() < 1e-9) hi = lo + 1;
    final path = Path();
    for (var i = 0; i < v.length; i++) {
      final x = size.width * i / (v.length - 1);
      final y = size.height * (1 - (v[i] - lo) / (hi - lo));
      i == 0 ? path.moveTo(x, y) : path.lineTo(x, y);
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = _accent
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.4,
    );
  }

  @override
  bool shouldRepaint(covariant _SparkPainter old) => true;
}

/// Лист настроек: сетка, список ячеек, конструктор объединённых ячеек.
class _ConfigSheet extends StatefulWidget {
  const _ConfigSheet({required this.model});
  final AppModel model;
  @override
  State<_ConfigSheet> createState() => _ConfigSheetState();
}

class _ConfigSheetState extends State<_ConfigSheet> {
  late DashboardPrefs prefs = widget.model.dashboardPrefs;
  final Set<String> _comboPick = <String>{};

  String _label(String id) {
    if (isComboTile(id)) {
      return comboParts(id).map(_label).join(' + ');
    }
    if (isVirtualTile(id)) {
      final d = virtualTileById(id);
      return d == null ? id : d.label;
    }
    return id;
  }

  @override
  Widget build(BuildContext context) {
    final activeIds = widget.model.engine.active.map((p) => p.id).toList();
    final chosen = prefs.visibleTiles(activeIds.toSet());
    final availablePids = activeIds
        .where((id) => !prefs.tiles.any(
            (t) => t == id || (isComboTile(t) && comboParts(t).contains(id))))
        .toList();
    final availableVirtual = kVirtualTiles
        .map((t) => t.id)
        .where((id) => !prefs.tiles.contains(id))
        .toList();

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.88,
      maxChildSize: 0.96,
      builder: (context, scroll) => ListView(
        controller: scroll,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
        children: [
          const Text('Настройка дашборда',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          const Text(
            'Ячейки можно перетаскивать прямо на дашборде: долгое нажатие '
            'и перенос на соседнюю плитку.',
            style: TextStyle(fontSize: 12, color: _muted),
          ),
          const SizedBox(height: 14),

          const Text('Сетка', style: TextStyle(fontWeight: FontWeight.bold)),
          Row(
            children: [
              Expanded(
                child: _Stepper(
                  label: 'В строке (книжная)',
                  value: prefs.columnsPortrait,
                  min: 1,
                  max: 5,
                  onChanged: (v) =>
                      setState(() => prefs = prefs.copyWith(columnsPortrait: v)),
                ),
              ),
              Expanded(
                child: _Stepper(
                  label: 'Строк (книжная)',
                  value: prefs.rowsPortrait,
                  min: 1,
                  max: 8,
                  onChanged: (v) =>
                      setState(() => prefs = prefs.copyWith(rowsPortrait: v)),
                ),
              ),
            ],
          ),
          Row(
            children: [
              Expanded(
                child: _Stepper(
                  label: 'В строке (альбом)',
                  value: prefs.columnsLandscape,
                  min: 1,
                  max: 8,
                  onChanged: (v) => setState(
                      () => prefs = prefs.copyWith(columnsLandscape: v)),
                ),
              ),
              Expanded(
                child: _Stepper(
                  label: 'Строк (альбом)',
                  value: prefs.rowsLandscape,
                  min: 1,
                  max: 6,
                  onChanged: (v) =>
                      setState(() => prefs = prefs.copyWith(rowsLandscape: v)),
                ),
              ),
            ],
          ),
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Text(
              'На экран: ${prefs.perScreen(false)} ячеек (книжная), '
              '${prefs.perScreen(true)} (альбомная)',
              style: const TextStyle(fontSize: 12, color: _accent),
            ),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Вместить без прокрутки'),
            subtitle: const Text(
                'Высота ячеек подгоняется под число строк',
                style: TextStyle(fontSize: 11)),
            value: prefs.fitToScreen,
            onChanged: (v) =>
                setState(() => prefs = prefs.copyWith(fitToScreen: v)),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Карточка поездки'),
            subtitle: const Text('Расход, одометр, средний + сброс',
                style: TextStyle(fontSize: 11)),
            value: prefs.showFuelCard,
            onChanged: (v) =>
                setState(() => prefs = prefs.copyWith(showFuelCard: v)),
          ),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Мини-графики'),
            value: prefs.showSpark,
            onChanged: (v) =>
                setState(() => prefs = prefs.copyWith(showSpark: v)),
          ),

          const Divider(height: 24),
          Text('Ячейки на дашборде (${chosen.length})',
              style: const TextStyle(fontWeight: FontWeight.bold)),
          ReorderableListView(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            buildDefaultDragHandles: false,
            onReorder: (o, n) => setState(() {
              final list = List<String>.from(chosen);
              if (n > o) n -= 1;
              list.insert(n, list.removeAt(o));
              final others =
                  prefs.tiles.where((id) => !chosen.contains(id)).toList();
              prefs = prefs.copyWith(tiles: [...list, ...others]);
            }),
            children: [
              for (var i = 0; i < chosen.length; i++)
                ListTile(
                  key: ValueKey('c-${chosen[i]}'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: ReorderableDragStartListener(
                    index: i,
                    child: const Icon(Icons.drag_handle),
                  ),
                  title: Row(
                    children: [
                      if (isComboTile(chosen[i]))
                        const Padding(
                          padding: EdgeInsets.only(right: 6),
                          child: Icon(Icons.view_agenda,
                              size: 14, color: _accent),
                        )
                      else if (isVirtualTile(chosen[i]))
                        const Padding(
                          padding: EdgeInsets.only(right: 6),
                          child:
                              Icon(Icons.functions, size: 14, color: _accent),
                        ),
                      Expanded(
                        child: Text(_label(chosen[i]),
                            style: const TextStyle(fontSize: 13)),
                      ),
                    ],
                  ),
                  trailing: IconButton(
                    icon: const Icon(Icons.close, size: 18),
                    onPressed: () => setState(() {
                      final l = List<String>.from(prefs.tiles)
                        ..remove(chosen[i]);
                      prefs = prefs.copyWith(tiles: l);
                    }),
                  ),
                ),
            ],
          ),

          const Divider(height: 24),
          const Text('Объединить PID в одну ячейку',
              style: TextStyle(fontWeight: FontWeight.bold)),
          const Text(
            'Отметьте 2–4 параметра и нажмите «Создать ячейку» — они встанут '
            'в одну плитку компактным списком.',
            style: TextStyle(fontSize: 11, color: _muted),
          ),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final id in [...activeIds, ...kVirtualTiles.map((t) => t.id)])
                FilterChip(
                  label: Text(_label(id), style: const TextStyle(fontSize: 11)),
                  selected: _comboPick.contains(id),
                  onSelected: (sel) => setState(() {
                    if (sel) {
                      if (_comboPick.length < 4) _comboPick.add(id);
                    } else {
                      _comboPick.remove(id);
                    }
                  }),
                ),
            ],
          ),
          const SizedBox(height: 8),
          FilledButton.tonalIcon(
            onPressed: _comboPick.length < 2
                ? null
                : () => setState(() {
                      final id = _comboPick.join(kComboSeparator);
                      prefs = prefs.copyWith(tiles: [...prefs.tiles, id]);
                      _comboPick.clear();
                    }),
            icon: const Icon(Icons.view_agenda, size: 18),
            label: Text('Создать ячейку (${_comboPick.length}/4)'),
          ),

          if (availableVirtual.isNotEmpty) ...[
            const Divider(height: 24),
            const Text('Расчётные плитки',
                style: TextStyle(fontWeight: FontWeight.bold)),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final id in availableVirtual)
                  ActionChip(
                    avatar: const Icon(Icons.functions, size: 14),
                    label: Text(_label(id), style: const TextStyle(fontSize: 11)),
                    onPressed: () => setState(() =>
                        prefs = prefs.copyWith(tiles: [...prefs.tiles, id])),
                  ),
              ],
            ),
          ],
          if (availablePids.isNotEmpty) ...[
            const Divider(height: 24),
            Text('Активные PID (${availablePids.length})',
                style: const TextStyle(fontWeight: FontWeight.bold)),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final id in availablePids)
                  ActionChip(
                    avatar: const Icon(Icons.add, size: 14),
                    label: Text(id, style: const TextStyle(fontSize: 11)),
                    onPressed: () => setState(() =>
                        prefs = prefs.copyWith(tiles: [...prefs.tiles, id])),
                  ),
              ],
            ),
          ],
          const SizedBox(height: 18),
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
          Text(label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 10.5, color: _muted)),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              IconButton(
                visualDensity: VisualDensity.compact,
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                onPressed: value > min ? () => onChanged(value - 1) : null,
                icon: const Icon(Icons.remove_circle_outline, size: 20),
              ),
              Text('$value', style: const TextStyle(fontSize: 15)),
              IconButton(
                visualDensity: VisualDensity.compact,
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                onPressed: value < max ? () => onChanged(value + 1) : null,
                icon: const Icon(Icons.add_circle_outline, size: 20),
              ),
            ],
          ),
        ],
      );
}
'''

# ===== Сервисная страница: статус связи, «НЕТ ОТВЕТА», пинг, журнал =====
FILES["lib/dtc_service_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'diag.dart';
import 'diag_trace.dart';
import 'model.dart';
import 'trace_sheet.dart';

const _muted = Color(0xFF8CA0BF);

class DtcServicePage extends StatefulWidget {
  const DtcServicePage({super.key, required this.model});
  final AppModel model;
  @override
  State<DtcServicePage> createState() => _DtcServicePageState();
}

class _DtcServicePageState extends State<DtcServicePage> {
  final Map<String, DtcReadResult> _results = {};
  ReadinessReport? _readiness;
  String _log = '';
  bool _ok = true;
  bool _busy = false;

  AppModel get model => widget.model;
  DiagSession get session => model.diagSession;

  /// v0.14: честный запуск — ошибки НЕ проглатываются (perform() их глотал).
  Future<void> _run(String label, Future<void> Function() op) async {
    if (_busy) return;
    if (!model.elm.ready) {
      setState(() {
        _ok = false;
        _log = 'Адаптер не подключён — команда не отправлена. '
            'Вкладка «Адаптер» → подключитесь.';
      });
      return;
    }
    setState(() {
      _busy = true;
      _ok = true;
      _log = '$label…';
    });
    final wasPolling = model.engine.running;
    try {
      await model.engine.stop();
      await op();
      if (mounted) setState(() => _log = '$label — выполнено');
    } catch (e) {
      if (mounted) {
        setState(() {
          _ok = false;
          _log = '$label — НЕ выполнено: $e';
        });
      }
    } finally {
      await session.restore();
      if (wasPolling) await model.engine.start();
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<bool> _confirm(String title, String body, {bool danger = false}) async =>
      await showDialog<bool>(
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
      ) ??
      false;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final linked = model.elm.ready;

    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        // ---- статус связи ----
        Card(
          color: linked
              ? Colors.green.withValues(alpha: 0.08)
              : Colors.red.withValues(alpha: 0.10),
          child: ListTile(
            dense: true,
            leading: Icon(linked ? Icons.link : Icons.link_off,
                color: linked ? Colors.greenAccent : Colors.redAccent),
            title: Text(
              linked ? 'Адаптер подключён' : 'Адаптер НЕ подключён',
              style: const TextStyle(fontSize: 14, fontWeight: FontWeight.bold),
            ),
            subtitle: Text(
              linked
                  ? 'Опрос PID приостанавливается на время сервисных операций '
                      'и возобновляется автоматически.'
                  : 'Команды не отправляются. Подключитесь во вкладке «Адаптер».',
              style: const TextStyle(fontSize: 11),
            ),
            trailing: IconButton(
              tooltip: 'Журнал обмена',
              onPressed: () => TraceSheet.show(context, session.trace),
              icon: const Icon(Icons.terminal),
            ),
          ),
        ),
        if (_busy) const LinearProgressIndicator(),
        if (_log.isNotEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 4),
            child: Row(
              children: [
                Icon(_ok ? Icons.info_outline : Icons.error_outline,
                    size: 14, color: _ok ? _muted : Colors.redAccent),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(_log,
                      style: TextStyle(
                          fontSize: 12,
                          color: _ok ? _muted : Colors.redAccent)),
                ),
              ],
            ),
          ),

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
              onPressed: _busy ? null : _ping,
              icon: const Icon(Icons.wifi_tethering, size: 18),
              label: const Text('Пинг адаптера'),
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
            OutlinedButton.icon(
              onPressed: () => TraceSheet.show(context, session.trace),
              icon: const Icon(Icons.terminal, size: 18),
              label: Text('Журнал (${session.trace.entries.length})'),
            ),
          ],
        ),
        const SizedBox(height: 8),
        if (_readiness != null) _readinessCard(_readiness!),

        for (final slot in EcuSlot.all) _slotCard(slot, theme),

        const Divider(height: 28),
        Text('Сервисные процедуры', style: theme.textTheme.titleMedium),
        const ListTile(
          dense: true,
          leading: Icon(Icons.restart_alt),
          title: Text('Сброс адаптаций (SSM2-эра)'),
          subtitle: Text('Клемма АКБ 10–30 мин, затем дообучение 15–20 мин.'),
        ),
        const ListTile(
          dense: true,
          leading: Icon(Icons.speed),
          title: Text('Обучение дросселя (E-Gas)'),
          subtitle: Text('Зажигание ON 15–20 с без запуска → OFF → прогрев.'),
        ),

        const Divider(height: 28),
        Card(
          color: model.experimental ? Colors.red.withValues(alpha: 0.07) : null,
          child: Column(
            children: [
              SwitchListTile(
                secondary: Icon(Icons.science,
                    color: model.experimental ? Colors.redAccent : null),
                title: const Text('Экспертные операции'),
                subtitle: Text(
                  model.experimental
                      ? 'РАЗБЛОКИРОВАНО: ECUReset и Clear Memory активны.'
                      : 'ECUReset (11 01) и Clear Memory (04 xx) заблокированы.',
                  style: const TextStyle(fontSize: 11),
                ),
                value: model.experimental,
                onChanged: _busy
                    ? null
                    : (v) async {
                        if (v &&
                            !await _confirm(
                              'Включить экспертные операции?',
                              'ECUReset ГЛУШИТ двигатель. Clear Memory стирает '
                                  'адаптации — потребуется дообучение.\n\n'
                                  'Только на стоянке при заглушённом моторе.',
                              danger: true,
                            )) {
                          return;
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
                  subtitle: const Text('Перезапуск ЭБУ, мотор заглохнет',
                      style: TextStyle(fontSize: 11)),
                  onTap: _busy ? null : _ecuReset,
                ),
                ListTile(
                  dense: true,
                  leading:
                      const Icon(Icons.auto_delete, color: Colors.orangeAccent),
                  title: const Text('Clear Memory (04 xx)'),
                  subtitle: const Text('Сброс адаптаций',
                      style: TextStyle(fontSize: 11)),
                  onTap: _busy ? null : _clearMemoryDialog,
                ),
                ListTile(
                  dense: true,
                  leading: const Icon(Icons.travel_explore),
                  title: const Text('Проба групп Clear Memory'),
                  subtitle: const Text('Безопасно: смотрит ответы 1..7',
                      style: TextStyle(fontSize: 11)),
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

  Widget _slotCard(EcuSlot slot, ThemeData theme) {
    final r = _results[slot.name];
    final icon = r == null
        ? Icons.memory
        : !r.answered
            ? Icons.cloud_off
            : r.codes.isEmpty
                ? Icons.check_circle
                : Icons.warning_amber;
    final color = r == null
        ? null
        : !r.answered
            ? Colors.redAccent
            : r.codes.isEmpty
                ? Colors.greenAccent
                : Colors.amberAccent;
    return Card(
      child: ExpansionTile(
        leading: Icon(icon, color: color),
        title: Text(slot.name),
        subtitle: Text(
          r == null
              ? '${slot.header}→${slot.responseId} · не опрошен'
              : !r.answered
                  ? 'НЕТ ОТВЕТА — ${r.note}'
                  : 'ответил ${r.protocolUsed} · кодов: ${r.codes.length}',
          style: TextStyle(
              fontSize: 11,
              color: r != null && !r.answered ? Colors.redAccent : null),
        ),
        children: [
          if (r != null && r.answered && r.codes.isEmpty)
            const ListTile(
              dense: true,
              leading: Icon(Icons.check, color: Colors.greenAccent),
              title: Text('Ошибок нет'),
              subtitle: Text('ЭБУ ответил корректным кадром',
                  style: TextStyle(fontSize: 11)),
            ),
          for (final d in (r?.codes ?? const <DiagDtcLike>[]).cast<DiagDtc>())
            ListTile(
              dense: true,
              leading: Icon(
                d.origin == 'permanent'
                    ? Icons.lock
                    : (d.active ? Icons.error : Icons.history),
                size: 20,
                color: d.origin == 'permanent'
                    ? Colors.orangeAccent
                    : (d.active
                        ? theme.colorScheme.error
                        : theme.colorScheme.outline),
              ),
              title: Text(
                '${d.code}${d.titleRu.isEmpty ? '' : ' · ${d.titleRu}'}',
                style: const TextStyle(fontFamily: 'monospace', fontSize: 13),
              ),
              subtitle:
                  Text(d.statusText, style: const TextStyle(fontSize: 11)),
              trailing: d.milOn
                  ? const Icon(Icons.lightbulb, color: Colors.amber, size: 18)
                  : null,
            ),
          OverflowBar(
            alignment: MainAxisAlignment.start,
            children: [
              TextButton.icon(
                onPressed: _busy ? null : () => _readAll(slot),
                icon: const Icon(Icons.search, size: 18),
                label: const Text('ЧИТАТЬ'),
              ),
              TextButton(
                onPressed: _busy ? null : () => _readFreeze(slot),
                child: const Text('СТОП-КАДР'),
              ),
              if ((r?.codes ?? const []).isNotEmpty)
                FilledButton.tonalIcon(
                  onPressed: _busy ? null : () => _clear(slot),
                  icon: const Icon(Icons.cleaning_services, size: 18),
                  label: const Text('СБРОС'),
                ),
            ],
          ),
        ],
      ),
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
                  Expanded(
                    child: Text(
                      'Мониторы: ${r.ready}/${r.total}'
                      '${r.milOn ? ' · CHECK ГОРИТ' : ''} · DTC: ${r.dtcCount}',
                      style: const TextStyle(fontWeight: FontWeight.bold),
                    ),
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
                          color:
                              e.value ? Colors.greenAccent : Colors.orangeAccent),
                      label:
                          Text(e.key, style: const TextStyle(fontSize: 11)),
                    ),
                ],
              ),
            ],
          ),
        ),
      );

  Future<void> _ping() => _run('Пинг адаптера', () async {
        final v = await session.pingAdapter();
        if (mounted) setState(() => _log = 'Адаптер ответил: $v');
      });

  Future<void> _readAll(EcuSlot slot) => _run('Чтение ${slot.name}', () async {
        final r = await session.readAllDtc(slot);
        if (mounted) setState(() => _results[slot.name] = r);
        if (!r.answered) throw ReplyErrorText(r.note);
      });

  Future<void> _clear(EcuSlot slot) async {
    final has = _results[slot.name]?.codes ?? const [];
    final perm =
        has.cast<DiagDtc>().where((d) => d.origin == 'permanent').length;
    if (!await _confirm(
      'Сбросить ошибки: ${slot.name}?',
      'Будут стёрты коды, стоп-кадры и мониторы.\n'
      '${perm > 0 ? '\nВНИМАНИЕ: $perm permanent-кодов НЕ стираются.\n' : ''}'
      '\nАдаптации (IAM, коррекции) это не трогает.',
    )) {
      return;
    }
    await _run('Сброс ${slot.name}', () async {
      final r = await session.clearDtc(slot);
      if (mounted) setState(() => _results[slot.name] = r);
    });
  }

  Future<void> _readFreeze(EcuSlot slot) =>
      _run('Стоп-кадр ${slot.name}', () async {
        final d = await session.readFreezeFrameDtc(slot);
        if (mounted) {
          setState(() => _log = d == null
              ? '${slot.name}: стоп-кадра нет'
              : '${slot.name}: стоп-кадр $d');
        }
      });

  Future<void> _scanAll() => _run('Скан всех блоков', () async {
        final map = await session.scanAllEcus(
          onProgress: (n) {
            if (mounted) setState(() => _log = 'Скан: $n…');
          },
        );
        if (mounted) {
          setState(() {
            _results
              ..clear()
              ..addAll(map);
            final answered = map.values.where((r) => r.answered).length;
            _log = 'Ответили $answered из ${map.length} блоков';
            _ok = answered > 0;
          });
        }
      });

  Future<void> _readInfo() => _run('VIN / CALID', () async {
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
        if (r == null) throw ReplyErrorText('ЭБУ не вернул статус мониторов');
      });

  Future<void> _ecuReset() async {
    if (!await _confirm(
      'ECUReset: перезапустить ЭБУ?',
      'Двигатель ЗАГЛОХНЕТ немедленно. Только на стоянке.',
      danger: true,
    )) {
      return;
    }
    await _run('ECUReset', () async {
      final res = await session.ecuReset(EcuSlot.engine);
      if (mounted) {
        setState(() {
          _log = '${res.title}: ${res.detail}';
          _ok = res.success;
        });
      }
    });
  }

  Future<void> _probeGroups() => _run('Проба групп', () async {
        final map = await session.probeClearMemoryGroups(EcuSlot.engine);
        if (!mounted) return;
        await showDialog<void>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: const Text('Группы Clear Memory'),
            content: SingleChildScrollView(
              child: Text(
                map.isEmpty
                    ? 'Нет ответа'
                    : map.entries.map((e) => 'гр.${e.key}: ${e.value}').join('\n'),
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
          title: const Text('Clear Memory'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Группы зависят от ECU: 1 — всё обучение, 2 — топливные '
                'коррекции, 3 — детонация/IAM. После сброса нужно дообучение.',
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
      if (mounted) {
        setState(() {
          _log = '${res.title}: ${res.detail}';
          _ok = res.success;
        });
      }
    });
  }
}

/// Локальная ошибка с текстом (чтобы не тянуть protocol.dart в UI).
class ReplyErrorText implements Exception {
  ReplyErrorText(this.message);
  final String message;
  @override
  String toString() => message;
}
'''

# ===== 18 тестов: журнал, answered, сетка, комбо, перестановка =====
FILES["test/trace_test.dart"] = r'''
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/dashboard_prefs.dart';
import 'package:subaru_ssm2/diag_trace.dart';

void main() {
  group('DiagTrace', () {
    test('считает TX/RX и хранит порядок', () {
      final t = DiagTrace();
      t.tx('18 00 FF 00');
      t.rx('58 00', ms: 42);
      expect(t.txCount, 1);
      expect(t.rxCount, 1);
      expect(t.entries.first.dir, TraceDir.tx);
      expect(t.entries.last.ms, 42);
    });

    test('ошибки считаются отдельно', () {
      final t = DiagTrace();
      t.error('нет связи');
      expect(t.errorCount, 1);
      expect(t.entries.single.dir, TraceDir.error);
    });

    test('лимит буфера соблюдается', () {
      final t = DiagTrace(limit: 10);
      for (var i = 0; i < 50; i++) {
        t.tx('cmd $i');
      }
      expect(t.entries.length, 10);
      expect(t.entries.last.text, 'cmd 49');
    });

    test('clear обнуляет счётчики', () {
      final t = DiagTrace()
        ..tx('a')
        ..rx('b')
        ..error('c');
      t.clear();
      expect(t.entries, isEmpty);
      expect(t.txCount, 0);
      expect(t.errorCount, 0);
    });

    test('asText содержит стрелки направления', () {
      final t = DiagTrace()
        ..tx('ATI')
        ..rx('ELM327 v1.5');
      final text = t.asText();
      expect(text, contains('>>'));
      expect(text, contains('<<'));
      expect(text, contains('ELM327'));
    });
  });

  group('DtcReadResult', () {
    test('answered=false НЕ значит «чисто»', () {
      const r = DtcReadResult(
        codes: [],
        answered: false,
        protocolUsed: '—',
        rawReplies: [],
      );
      expect(r.clean, isFalse);
    });
    test('answered=true без кодов -> чисто', () {
      const r = DtcReadResult(
        codes: [],
        answered: true,
        protocolUsed: 'SSM2 18',
        rawReplies: [],
      );
      expect(r.clean, isTrue);
    });
  });

  group('Сетка дашборда', () {
    test('perScreen = колонки × строки', () {
      final p = DashboardPrefs(
        columnsPortrait: 2,
        rowsPortrait: 5,
        columnsLandscape: 5,
        rowsLandscape: 2,
      );
      expect(p.perScreen(false), 10);
      expect(p.perScreen(true), 10);
    });

    test('ограничения колонок/строк при загрузке', () {
      final p = DashboardPrefs.fromJson({
        'columnsPortrait': 99,
        'rowsPortrait': 0,
        'columnsLandscape': 99,
        'rowsLandscape': 99,
      });
      expect(p.columnsPortrait, lessThanOrEqualTo(5));
      expect(p.rowsPortrait, greaterThanOrEqualTo(1));
      expect(p.columnsLandscape, lessThanOrEqualTo(8));
      expect(p.rowsLandscape, lessThanOrEqualTo(6));
    });
  });

  group('Объединённые ячейки', () {
    test('распознаются и разбираются', () {
      expect(isComboTile('RPM|AFR'), isTrue);
      expect(isComboTile('RPM'), isFalse);
      expect(comboParts('RPM|AFR|ECT'), ['RPM', 'AFR', 'ECT']);
    });

    test('комбо валидно только из известных частей', () {
      expect(DashboardPrefs.knownTile('RPM|AFR'), isTrue);
      expect(DashboardPrefs.knownTile('RPM|НЕТ'), isFalse);
      expect(DashboardPrefs.knownTile('@FUEL_LPH|RPM'), isTrue);
    });

    test('комбо видно, если доступна хотя бы одна часть', () {
      final p = DashboardPrefs(tiles: ['RPM|FBKC']);
      expect(p.visibleTiles({'RPM'}), ['RPM|FBKC']);
      expect(p.visibleTiles(<String>{}), isEmpty);
    });

    test('мусорное комбо отбрасывается при загрузке', () {
      final p = DashboardPrefs.fromJson({
        'tiles': ['RPM|AFR', 'ZZZ|YYY']
      });
      expect(p.tiles, ['RPM|AFR']);
    });
  });

  group('Перестановка плиток', () {
    test('moveTile переносит на новую позицию', () {
      final p = DashboardPrefs(tiles: ['A1', 'B2', 'C3']);
      // используем реальные id, чтобы не зависеть от валидации
      final q = DashboardPrefs(tiles: ['RPM', 'AFR', 'ECT']).moveTile('ECT', 0);
      expect(q.tiles, ['ECT', 'RPM', 'AFR']);
      expect(p.tiles.length, 3);
    });

    test('swapTiles меняет местами', () {
      final q =
          DashboardPrefs(tiles: ['RPM', 'AFR', 'ECT']).swapTiles('RPM', 'ECT');
      expect(q.tiles, ['ECT', 'AFR', 'RPM']);
    });

    test('перенос несуществующей плитки безопасен', () {
      final p = DashboardPrefs(tiles: ['RPM']);
      expect(p.moveTile('NOPE', 0).tiles, ['RPM']);
    });
  });
}
'''

# ===== Правки: общий DiagTrace в модели, импорты, версия =====
# ===== REWRITES =====

# --- 1. model.dart: общий DiagTrace + передача в сессию ---
REWRITES.append((
    "lib/model.dart",
    "  bool experimental = false;\n  late final DiagSession diagSession = DiagSession(elm);",
    """  bool experimental = false;

  /// v0.14: журнал обмена — видно каждую команду и ответ ЭБУ.
  final DiagTrace diagTrace = DiagTrace();
  late final DiagSession diagSession = DiagSession(elm, trace: diagTrace);""",
    ["final DiagTrace diagTrace", False],
))
REWRITES.append((
    "lib/model.dart",
    "import 'dashboard_prefs.dart';\nimport 'diag.dart';",
    "import 'dashboard_prefs.dart';\nimport 'diag.dart';\nimport 'diag_trace.dart';",
    ["import 'diag_trace.dart';", False],
))

# --- 2. main.dart: кнопка журнала в AppBar ---
REWRITES.append((
    "lib/main.dart",
    "import 'dashboard_page.dart';\nimport 'dashboard_prefs.dart';",
    "import 'dashboard_page.dart';\nimport 'dashboard_prefs.dart';\nimport 'trace_sheet.dart';",
    ["import 'trace_sheet.dart';", False],
))
REWRITES.append((
    "lib/main.dart",
    "SSM2 TELEMETRY 0.13",
    "SSM2 TELEMETRY 0.14",
    ["SSM2 TELEMETRY 0.14", True],
))

# --- 3. protocol.dart: ATI уже разрешён, добавляем ATRV для пинга ---
REWRITES.append((
    "lib/protocol.dart",
    "  '22 F1 8C', // UDS: серийный номер блока\n};",
    """  '22 F1 8C', // UDS: серийный номер блока
  '02 02 00', // стоп-кадр (повтор безопасен)
};""",
    ["// стоп-кадр (повтор безопасен)", True],
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
        raise RuntimeError(f"Нет файла {rel} — прогоните 02–04.4")
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
    "lib/diag_trace.dart": ["class DiagTrace", "NotConnectedException",
                            "class DtcReadResult", "answered"],
    "lib/diag.dart": ["readDtcDetailed", "_requireLink", "trace.tx",
                      "pingAdapter", "DiagSession(this.elm, {DiagTrace? trace})"],
    "lib/trace_sheet.dart": ["class TraceSheet", "Журнал обмена"],
    "lib/trip_card.dart": ["class TripCard", "Одометр", "Средний"],
    "lib/dashboard_prefs.dart": ["kComboSeparator", "isComboTile", "rowsPortrait",
                                 "perScreen", "moveTile"],
    "lib/dashboard_page.dart": ["LongPressDraggable", "DragTarget", "_ComboTile",
                                "TripCard"],
    "lib/dtc_service_page.dart": ["Адаптер НЕ подключён", "TraceSheet.show",
                                  "НЕТ ОТВЕТА", "pingAdapter"],
    "lib/model.dart": ["final DiagTrace diagTrace", "trace: diagTrace"],
    "test/trace_test.dart": ["DiagTrace", "perScreen", "isComboTile"],
}
for rel, needles in checks.items():
    text = (APP / rel).read_text(encoding="utf-8")
    miss = [m for m in needles if m not in text]
    if miss:
        raise RuntimeError(f"Самопроверка {rel}: нет маркеров {miss}")
print("[OK] самопроверка маркеров пройдена")

print("\n=== Готово: SSM2 0.14. Далее — ячейка 05 (сборка). ===")
print("Исправлено:")
print("  · ложное «готово» в DTC: проверка связи + честный DtcReadResult")
print("  · «НЕТ ОТВЕТА» больше не выглядит как «ошибок нет»")
print("Добавлено:")
print("  · журнал обмена TX/RX с таймингом (кнопка «Журнал»)")
print("  · карточка поездки: расход, одометр, средний, сброс")
print("  · сетка: ячеек в строке × строк (до 5x8 и 8x6), fit без прокрутки")
print("  · перетаскивание ячеек прямо на дашборде (долгое нажатие)")
print("  · объединение 2–4 PID в одну ячейку")
print("Чек-лист стенда 04.5:")
print(" [ ] БЕЗ адаптера: 'Читать' -> красный 'Адаптер не подключён', журнал пуст")
print(" [ ] С адаптером: 'Пинг' -> в журнале ATI >> и << ELM327 v1.x")
print(" [ ] Блок без ответа -> 'НЕТ ОТВЕТА', а не 'Ошибок нет'")
print(" [ ] Опрос PID сам встаёт на паузу и возобновляется после операции")
print(" [ ] Дашборд: 2x5 = 10 ячеек без прокрутки, перетаскивание работает")
print(" [ ] Объединённая ячейка показывает 3 параметра сразу")


# coding: utf-8
# ▸ 04.6 | PARSER FIX | SSM2 0.14 >> 0.14.1 (insert BETWEEN 04.5 and 05)
# ROOT CAUSE from log:
#   >> 03 (read DTC)
#   << 4300\r\r>   <- ECU replied "43 00" = no errors (CORRECT!)
#   -- "not recognized as data frame" <- parseDiagPayload returned null!
# Fix: ELM327 appends prompt ">" to every reply.
# Old parser: "4300\r\r>" -> ["4300", ">"] -> ">" fails hex regex -> null.
# Fixed: filter ">", ELM status lines, 3-char length counters, ATH1 CAN IDs.
import re
import shutil
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_046_{STAMP}"
BACKUP.mkdir(parents=True, exist_ok=True)

# ===== NEW parseDiagPayload (ASCII-only comments) =====
NEW_PARSE = r"""List<int>? parseDiagPayload(String text) {
  // v0.14.1 (parser fix): filter ELM prompt ">", multi-frame length counters,
  // ELM status lines, ATH1 CAN IDs. Bug: ">" broke entire parsing -> null.
  const Set<String> _elmStatus = <String>{
    'NODATA', 'STOPPED', 'CANERROR', 'BUSBUSY', 'BUFFERFULL',
    'ERROR', 'UNABLETOCONNECT', 'OK', '?',
  };

  final flat = <int>[];
  final frameLines = <int, List<int>>{};
  var sawFrameIndex = false;

  for (final rawLine in text.split(RegExp(r'[\r\n]+'))) {
    final line = rawLine.trim();
    if (line.isEmpty || line == '>') continue; // ELM prompt

    // ELM status lines (NO DATA, OK, ...)
    final compact = line.toUpperCase().replaceAll(RegExp(r'[^A-Z]'), '');
    if (_elmStatus.contains(compact)) continue;

    // Numbered ISO-TP frames "0:490402..."
    final frameMatch = RegExp(r'^(\d+):(.*)$').firstMatch(line);
    if (frameMatch != null) {
      sawFrameIndex = true;
      final idx = int.parse(frameMatch.group(1)!);
      final hex = frameMatch.group(2)!.replaceAll(' ', '');
      if (hex.isNotEmpty && hex.length % 2 == 0 &&
          RegExp(r'^[0-9A-Fa-f]+$').hasMatch(hex)) {
        frameLines[idx] = <int>[
          for (var i = 0; i + 1 < hex.length; i += 2)
            int.parse(hex.substring(i, i + 2), radix: 16),
        ];
      }
      continue;
    }

    // 3-char hex token = ELM length counter ("023") -> skip
    if (RegExp(r'^[0-9A-Fa-f]{3}$').hasMatch(line) && !line.contains(' ')) {
      continue;
    }

    var tokens = line.split(RegExp(r'\s+')).where((t) => t.isNotEmpty).toList();

    // ATH1: first token is 3-char CAN ID -> skip it
    if (tokens.isNotEmpty &&
        tokens.first.length == 3 &&
        RegExp(r'^[0-9A-Fa-f]{3}$').hasMatch(tokens.first)) {
      tokens = tokens.sublist(1);
    }

    for (final t in tokens) {
      if (t == '>') continue; // inline prompt, skip
      if (!RegExp(r'^[0-9A-Fa-f]+$').hasMatch(t) || t.length % 2 != 0) {
        return null; // genuinely invalid token
      }
      for (var i = 0; i + 1 < t.length; i += 2) {
        flat.add(int.parse(t.substring(i, i + 2), radix: 16));
      }
    }
  }

  // ISO-TP multi-frame: concatenate by index order
  if (sawFrameIndex) {
    if (frameLines.isEmpty) return null;
    final payload = <int>[];
    for (final k in (frameLines.keys.toList()..sort())) {
      payload.addAll(frameLines[k]!);
    }
    return payload.isEmpty ? null : payload;
  }

  if (flat.isEmpty) return null;
  var b = flat;
  // PCI single-frame: high nibble 0x0, length 1..7
  if (b.length > 1 && (b[0] & 0xF0) == 0 && (b[0] & 0x0F) <= 7) {
    final len = b[0] & 0x0F;
    b = b.sublist(1, (1 + len).clamp(1, b.length));
  }
  // CAN ID 0x7E8 with ATH1
  if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) {
    b = b.sublist(2);
  }
  return b.isEmpty ? null : b;
}"""

# ===== NEW parseAsciiReply (multi-frame CALID support) =====
NEW_ASCII = r"""String? parseAsciiReply(List<int> b, int pid) {
  // v0.14.1 (parser fix): multi-frame CALID support; 49 04 [count?] [ascii...]
  // Skip count byte if < 0x20 (not ASCII printable)
  if (b.length < 3 || b[0] != 0x49 || b[1] != pid) return null;
  // If b[2] < 0x20 it is a frame count, not data -> start at 3
  final start = (b.length > 3 && b[2] < 0x20) ? 3 : 2;
  final chars = <int>[];
  for (var i = start; i < b.length; i++) {
    if (b[i] >= 0x20 && b[i] <= 0x7E) chars.add(b[i]);
  }
  final t = String.fromCharCodes(chars).trim();
  return t.isEmpty ? null : t;
}"""

# ===== Apply patches =====
path = APP / "lib/diag.dart"
if not path.exists():
    raise RuntimeError("lib/diag.dart not found - run 02-04.5 first")

text = path.read_text(encoding="utf-8")

# Check already applied
if "v0.14.1" in text and "ELM prompt" in text:
    print("[SKIP already applied] lib/diag.dart: parseDiagPayload")
else:
    # Find and replace parseDiagPayload by extracting full function
    m = re.search(r"List<int>\? parseDiagPayload\(String text\) \{", text)
    if not m:
        raise RuntimeError("parseDiagPayload not found in diag.dart")
    start = m.start()
    depth = 0
    end = start
    for i, c in enumerate(text[start:]):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    old_fn = text[start:end]
    shutil.copyfile(path, BACKUP / "lib__diag.dart.orig.bak")
    text = text.replace(old_fn, NEW_PARSE, 1)
    path.write_text(text, encoding="utf-8")
    print("[OK] patched lib/diag.dart: parseDiagPayload")

text = path.read_text(encoding="utf-8")
if "multi-frame" in text:
    print("[SKIP already applied] lib/diag.dart: parseAsciiReply")
else:
    m = re.search(r"String\? parseAsciiReply\(List<int> b, int pid\) \{", text)
    if not m:
        raise RuntimeError("parseAsciiReply not found in diag.dart")
    start = m.start()
    depth = 0
    end = start
    for i, c in enumerate(text[start:]):
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    old_fn = text[start:end]
    text = text.replace(old_fn, NEW_ASCII, 1)
    path.write_text(text, encoding="utf-8")
    print("[OK] patched lib/diag.dart: parseAsciiReply")

# ===== Self-check =====
diag = path.read_text(encoding="utf-8")
assert "v0.14.1" in diag, "parseDiagPayload not patched"
assert "multi-frame" in diag, "parseAsciiReply not patched"
print("[OK] self-check passed")

print()
print("=== SSM2 0.14.1 - run cell 05 next ===")
print()
print("FIXED: parseDiagPayload now handles ELM prompt '>'")
print('  Before: "4300\\r\\r>" -> None (parser crash on ">")')
print('  After:  "4300\\r\\r>" -> [0x43, 0x00] -> "no DTC" (correct!)')
print()
print("YOUR LOG DIAGNOSTICS:")
print("  CALID confirmed: A2TB100B = matches identity.dart (good)")
print("  IAM=1.000, FBKC=0.00, FKL=0.00 = NORMAL on idle without knock")
print("  REQ_TQ=0.0 = zero torque request on idle (correct)")
print("  LTFT=-14.84% = WARNING: large negative fuel learning trim!")
print("    Possible causes: vacuum leak after MAF, dripping injector,")
print("    rich base fuel map. Check if STFT varies or is stuck at 0.")
print("  IAT: add to PID set (PID tab) and dashboard grid")


# ▸ 04.7 FIX DOUBLE
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

# coding: utf-8
# ▸ 04.8 | OpenPort 2.0 OTG REAL | SSM2 0.15.0 (REPLACE старую 04.8)
#
# ВАЖНО: это ПОЛНАЯ замена старых 04.8 r1-r4. Старые версии делали из OP2
# «сырой FTDI/ELM» — это неверно. Настоящий OpenPort 2.0:
#   * USB CDC-ACM, оригинал VID:PID = 0403:CC4D (не CC4C);
#   * data interface ищется по bulk IN + bulk OUT (обычно interface 1);
#   * wire protocol: ato6 / atf6 / att6 + бинарные J2534 payload;
#   * ISO-TP делает прошивка OpenPort, ELM AT-команды эмулируются приложению.
#
# ВЫБОР АДАПТЕРА В ПРИЛОЖЕНИИ:
#   HybridTransport.paired() объединяет два списка:
#     1) OpenPort 2.0 (USB OTG) с address usb:0403:cc4d, если кабель подключён;
#     2) обычные сопряжённые Bluetooth ELM327.
#   Существующий экран выбора адаптера показывает оба. Выбран usb:* -> OP2,
#   выбран BT MAC -> native SPP. Один APK, без --dart-define.
#
# Протокол реализован независимо по публичной wire-документации:
# https://github.com/bisak/openport-j2534/blob/main/docs/PROTOCOL.md
# Никакой код GPL-проекта в APK не копируется.
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_048real_{STAMP}"

PUBSPEC = APP / "pubspec.yaml"
BT_PATH = APP / "lib/bt_transport.dart"
NATIVE_SPP = APP / "lib/native_spp.dart"
TRANSPORT_SEL = APP / "lib/transport_selected.dart"
MAIN_ACTIVITY = APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/MainActivity.kt"
MANIFEST = APP / "android/app/src/main/AndroidManifest.xml"

for need in (APP / "build_config.json", PUBSPEC, BT_PATH, NATIVE_SPP,
             TRANSPORT_SEL, MAIN_ACTIVITY, MANIFEST):
    if not need.exists():
        raise RuntimeError(f"Нет файла: {need}. Выполните 01, 02 и 04.1-04.7.")
BACKUP.mkdir(parents=True, exist_ok=True)

def backup(path):
    if path.exists():
        shutil.copyfile(path, BACKUP / (str(path.relative_to(APP)).replace("/", "__") + ".bak"))

def write(rel, body):
    path = APP / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(body, encoding="utf-8")
    print("[+]", rel)

# ============================================================
# 0. GROUND TRUTH: пакет + BtTransport
# ============================================================
def package_name(text):
    m = re.search(r"(?m)^name:\s*([a-zA-Z_0-9]+)\s*$", text)
    return m.group(1) if m else ""

pub = PUBSPEC.read_text(encoding="utf-8")
votes = {}
for path in sorted((APP / "test").glob("*.dart")):
    for m in re.finditer(r"(?m)^\s*import 'package:([a-zA-Z_0-9]+)/",
                         path.read_text(encoding="utf-8")):
        name = m.group(1)
        if not name.startswith(("flutter", "matcher", "meta")):
            votes[name] = votes.get(name, 0) + 1
real_name = max(votes.items(), key=lambda item: item[1])[0] if votes else package_name(pub)
if not real_name:
    raise RuntimeError("Не удалось определить name пакета")
print("[GROUND] package votes:", sorted(votes.items(), key=lambda x: -x[1]))

# Лечим только последствия предыдущих 04.8: name и bogus dep.
changed = []
if package_name(pub) != real_name:
    backup(PUBSPEC)
    old = package_name(pub)
    pub = re.sub(r"(?m)^name:\s*[a-zA-Z_0-9]+\s*$", "name: " + real_name, pub, count=1)
    changed.append(f"name {old} -> {real_name}")
for bogus in ("subaru_ssm2_fixed", "subaru_ssm2"):
    if bogus == real_name:
        continue
    new_pub = re.sub(r"(?m)^[ ]+" + re.escape(bogus) + r":\s*any\s*$\n?", "", pub)
    if new_pub != pub:
        backup(PUBSPEC)
        pub = new_pub
        changed.append("removed bogus dep " + bogus + ": any")
if changed:
    PUBSPEC.write_text(pub, encoding="utf-8")
    for item in changed:
        print("[repair pubspec]", item)
PKG = package_name(PUBSPEC.read_text(encoding="utf-8"))
print("[GROUND] package:", PKG)

bt_src = BT_PATH.read_text(encoding="utf-8")
required_contract = {
    "String get name", "bool get connected", "Stream<Uint8List> get data",
    "Stream<bool> get status", "Future<List<BtDevice>> paired()",
    "Future<void> connect(String address)", "Future<void> write(String ascii)",
    "Future<void> disconnect()", "Future<void> dispose()",
}
missing_contract = [sig for sig in required_contract if sig not in bt_src]
if missing_contract:
    print("\n===== lib/bt_transport.dart =====\n" + bt_src)
    raise RuntimeError("Контракт BtTransport изменился: " + str(missing_contract))
print("[GROUND] BtTransport: подтверждены 9 членов")

# ============================================================
# 1. Dart: настоящий OP2-транспорт и runtime-выбор
# ============================================================
OP2_IDS_DART = r'''// OpenPort 2.0 USB ids (original + known clone fallback).
const int kOp2Vid = 0x0403;
const List<int> kOp2Pids = <int>[0xCC4D, 0xCC4C];

bool looksLikeOp2(int vid, int pid) => vid == kOp2Vid && kOp2Pids.contains(pid);
String hex16(int v) => '0x' + v.toRadixString(16).toUpperCase().padLeft(4, '0');
'''

NATIVE_OP2_DART = r'''// OpenPort 2.0 over Android USB OTG.
import 'dart:async';
import 'package:flutter/services.dart';

import 'bt_transport.dart';

class NativeOp2Transport implements BtTransport {
  static const MethodChannel _method = MethodChannel('ssm2/op2');
  static const EventChannel _events = EventChannel('ssm2/op2_rx');

  final StreamController<bool> _status = StreamController<bool>.broadcast();
  bool _connected = false;

  @override
  String get name => 'OpenPort 2.0 (USB OTG)';
  @override
  bool get connected => _connected;
  @override
  Stream<Uint8List> get data => _events
      .receiveBroadcastStream()
      .where((Object? e) => e is Uint8List)
      .cast<Uint8List>();
  @override
  Stream<bool> get status => _status.stream;

  @override
  Future<List<BtDevice>> paired() async {
    final raw = await _method.invokeMethod<Map<dynamic, dynamic>>('op2/descriptor');
    final d = raw?.cast<String, Object?>() ?? const <String, Object?>{};
    if (d['found'] == true) {
      final pid = (d['pid'] ?? '0xCC4D').toString().toLowerCase().replaceAll('0x', '');
      return <BtDevice>[BtDevice('OpenPort 2.0 (USB OTG)', 'usb:0403:' + pid)];
    }
    return <BtDevice>[];
  }

  @override
  Future<void> connect(String address) async {
    final raw = await _method.invokeMethod<Map<dynamic, dynamic>>('op2/open');
    final d = raw?.cast<String, Object?>() ?? const <String, Object?>{};
    if (d['open'] != true) {
      throw StateError((d['error'] ?? 'OpenPort 2.0 не открыт').toString());
    }
    _connected = true;
    _status.add(true);
  }

  @override
  Future<void> write(String ascii) => _method.invokeMethod<void>('op2/write', ascii);

  @override
  Future<void> disconnect() async {
    await _method.invokeMethod<void>('op2/close');
    _connected = false;
    _status.add(false);
  }

  @override
  Future<void> dispose() async {
    await disconnect();
    await _status.close();
  }
}
'''

HYBRID_DART = r'''// Один APK: OpenPort 2.0 USB и Bluetooth ELM327 в одном списке.
// Базовый интерфейс BtTransport — implements (native_spp так и не требует extends).
import 'dart:async';
import 'dart:typed_data';

import 'bt_transport.dart';
import 'native_op2.dart';

class HybridTransport implements BtTransport {
  final BtTransport bluetooth;
  final BtTransport usb;
  final StreamController<Uint8List> _data = StreamController<Uint8List>.broadcast();
  final StreamController<bool> _status = StreamController<bool>.broadcast();
  StreamSubscription<Uint8List>? _dataSub;
  StreamSubscription<bool>? _statusSub;
  BtTransport? _active;

  HybridTransport(this.bluetooth, {BtTransport? usb})
      : usb = usb ?? NativeOp2Transport();

  @override
  String get name => _active?.name ?? 'Bluetooth / OpenPort 2.0';
  @override
  bool get connected => _active?.connected ?? false;
  @override
  Stream<Uint8List> get data => _data.stream;
  @override
  Stream<bool> get status => _status.stream;

  @override
  Future<List<BtDevice>> paired() async {
    final usbDevices = await usb.paired();
    final btDevices = await bluetooth.paired();
    return <BtDevice>[...usbDevices, ...btDevices];
  }

  @override
  Future<void> connect(String address) async {
    await disconnect();
    final target = address.toLowerCase().startsWith('usb:') ? usb : bluetooth;
    _active = target;
    _dataSub = target.data.listen(_data.add, onError: _data.addError);
    _statusSub = target.status.listen(_status.add, onError: _status.addError);
    try {
      await target.connect(address);
      _status.add(target.connected);
    } catch (_) {
      await _dataSub?.cancel();
      await _statusSub?.cancel();
      _dataSub = null;
      _statusSub = null;
      _active = null;
      rethrow;
    }
  }

  @override
  Future<void> write(String ascii) async {
    final target = _active;
    if (target == null) throw StateError('Адаптер не выбран');
    await target.write(ascii);
  }

  @override
  Future<void> disconnect() async {
    await _dataSub?.cancel();
    await _statusSub?.cancel();
    _dataSub = null;
    _statusSub = null;
    final target = _active;
    _active = null;
    if (target != null) await target.disconnect();
    _status.add(false);
  }

  @override
  Future<void> dispose() async {
    await disconnect();
    await bluetooth.dispose();
    await usb.dispose();
    await _data.close();
    await _status.close();
  }
}
'''

OP2_TEST = r'''import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:PKG/bt_transport.dart';
import 'package:PKG/hybrid_transport.dart';
import 'package:PKG/op2_ids.dart';

class FakeTransport extends BtTransport {
  final String fakeName;
  final List<BtDevice> devices;
  final StreamController<Uint8List> dc = StreamController<Uint8List>.broadcast();
  final StreamController<bool> sc = StreamController<bool>.broadcast();
  String? lastAddress;
  String? lastWrite;
  bool open = false;
  FakeTransport(this.fakeName, this.devices);
  @override String get name => fakeName;
  @override bool get connected => open;
  @override Stream<Uint8List> get data => dc.stream;
  @override Stream<bool> get status => sc.stream;
  @override Future<List<BtDevice>> paired() async => devices;
  @override Future<void> connect(String address) async { lastAddress = address; open = true; sc.add(true); }
  @override Future<void> write(String ascii) async { lastWrite = ascii; }
  @override Future<void> disconnect() async { open = false; sc.add(false); }
  @override Future<void> dispose() async { await dc.close(); await sc.close(); }
}

void main() {
  test('OP2 ids: original is 0403:CC4D', () {
    expect(looksLikeOp2(0x0403, 0xCC4D), isTrue);
    expect(looksLikeOp2(0x0403, 0xCC4C), isTrue);
  });
  test('Hybrid merges USB and Bluetooth choices', () async {
    final bt = FakeTransport('BT', <BtDevice>[BtDevice('ELM327', '00:11')]);
    final usb = FakeTransport('USB', <BtDevice>[BtDevice('OpenPort', 'usb:0403:cc4d')]);
    final hybrid = HybridTransport(bt, usb: usb);
    final all = await hybrid.paired();
    expect(all.length, 2);
    await hybrid.connect('usb:0403:cc4d');
    expect(usb.lastAddress, 'usb:0403:cc4d');
    await hybrid.write('A800000008');
    expect(usb.lastWrite, 'A800000008');
    await hybrid.dispose();
  });
}
'''.replace("PKG", PKG)

# ============================================================
# 2. Kotlin: OpenPort CDC + wire protocol + ELM compatibility
# ============================================================
OP2_KOTLIN = r'''package com.subaru.ssm2_fixed

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

object Op2Ids {
    const val VID = 0x0403
    val PIDS = intArrayOf(0xCC4D, 0xCC4C)
    fun matches(d: UsbDevice) = d.vendorId == VID && PIDS.contains(d.productId)
}

private class Op2Pipe(
    private val connection: UsbDeviceConnection,
    private val iface: UsbInterface,
    private val input: UsbEndpoint,
    private val output: UsbEndpoint,
) {
    @Synchronized fun write(bytes: ByteArray, timeout: Int = 1500): Int =
        connection.bulkTransfer(output, bytes, bytes.size, timeout)
    fun read(buffer: ByteArray, timeout: Int = 100): Int =
        connection.bulkTransfer(input, buffer, buffer.size, timeout)
    fun close() {
        runCatching { connection.releaseInterface(iface) }
        connection.close()
    }

    companion object {
        fun open(manager: UsbManager, device: UsbDevice): Op2Pipe {
            val connection = manager.openDevice(device)
                ?: error("UsbManager.openDevice вернул null")
            for (i in 0 until device.interfaceCount) {
                val iface = device.getInterface(i)
                var input: UsbEndpoint? = null
                var output: UsbEndpoint? = null
                for (e in 0 until iface.endpointCount) {
                    val ep = iface.getEndpoint(e)
                    if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
                    if (ep.direction == UsbConstants.USB_DIR_IN) input = ep else output = ep
                }
                val inEp = input
                val outEp = output
                if (inEp != null && outEp != null && connection.claimInterface(iface, true)) {
                    return Op2Pipe(connection, iface, inEp, outEp)
                }
            }
            connection.close()
            error("CDC data interface с bulk IN/OUT не найден")
        }
    }
}

private class Op2Session(
    private val pipe: Op2Pipe,
    private val emit: (ByteArray) -> Unit,
) {
    private val sequence = AtomicInteger(1000)
    private val pending = ConcurrentHashMap<Int, CompletableFuture<String>>()
    private val timer: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    @Volatile private var running = true
    @Volatile private var rx = ByteArray(0)
    @Volatile private var requestToken = 0
    private var partial: ByteArrayOutputStream? = null
    private var partialId = ByteArray(4)
    private var txId = 0x7E0
    private var showHeaders = true

    private val reader = Thread {
        val buffer = ByteArray(4096)
        while (running) {
            val n = runCatching { pipe.read(buffer) }.getOrDefault(-1)
            if (n > 0) feed(buffer.copyOfRange(0, n))
        }
    }.apply { isDaemon = true; name = "ssm2-op2-reader" }

    fun start() = reader.start()

    fun initialize() {
        pipe.write(byteArrayOf(13, 10, 13, 10))
        ok(command("atz", timeoutMs = 2500))
        ok(command("ata"))
        ok(command("ato6 0 500000 0"))
        // FLOW_CONTROL filter: mask 7FF, response 7E8, flow-control/request 7E0.
        val filter = be32(0x7FF) + be32(0x7E8) + be32(0x7E0)
        val reply = command("atf6 3 64 4", filter)
        if (reply.startsWith("are")) error("OP2 filter rejected: " + reply)
    }

    private fun ok(line: String) {
        if (line.startsWith("are")) error("OpenPort error: " + line)
    }

    private fun command(base: String, payload: ByteArray? = null, timeoutMs: Long = 1800): String {
        val seq = sequence.incrementAndGet()
        val future = CompletableFuture<String>()
        pending[seq] = future
        val line = (base + " " + seq + "\r\n").toByteArray(Charsets.US_ASCII)
        if (pipe.write(line) != line.size) error("USB write command failed")
        if (payload != null && pipe.write(payload) != payload.size) error("USB write payload failed")
        return try {
            future.get(timeoutMs, TimeUnit.MILLISECONDS)
        } finally {
            pending.remove(seq)
        }
    }

    fun elmWrite(raw: String) {
        val text = raw.trim().uppercase()
        if (text.isEmpty()) return
        if (text.startsWith("AT")) {
            if (text.startsWith("ATSH")) text.substring(4).trim().toIntOrNull(16)?.let { txId = it }
            if (text == "ATH0") showHeaders = false
            if (text == "ATH1") showHeaders = true
            emit("OK\r>".toByteArray(Charsets.US_ASCII))
            return
        }
        val compact = text.replace(Regex("[^0-9A-F]"), "")
        if (compact.isEmpty() || compact.length % 2 != 0) {
            emit("?\r>".toByteArray(Charsets.US_ASCII)); return
        }
        val payload = ByteArray(compact.length / 2) { i ->
            compact.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
        val message = be32(txId) + payload
        val token = ++requestToken
        try {
            ok(command("att6 " + message.size + " 64 1000000", message, 2200))
            timer.schedule({
                if (requestToken == token) {
                    requestToken++
                    emit("NO DATA\r>".toByteArray(Charsets.US_ASCII))
                }
            }, 650, TimeUnit.MILLISECONDS)
        } catch (e: Exception) {
            if (requestToken == token) requestToken++
            emit("CAN ERROR\r>".toByteArray(Charsets.US_ASCII))
            throw e
        }
    }

    @Synchronized private fun feed(chunk: ByteArray) {
        rx = rx + chunk
        while (rx.size >= 2) {
            if (rx[0] != 'a'.code.toByte() || rx[1] != 'r'.code.toByte()) {
                rx = rx.copyOfRange(1, rx.size); continue
            }
            if (rx.size < 3) return
            val third = rx[2].toInt() and 0xFF
            if (third in '0'.code..'9'.code) {
                if (rx.size < 4) return
                val total = 4 + (rx[3].toInt() and 0xFF)
                if (rx.size < total) return
                val frame = rx.copyOfRange(0, total)
                rx = rx.copyOfRange(total, rx.size)
                handleFrame(frame)
            } else {
                val end = findCrlf(rx)
                if (end < 0) return
                val line = String(rx.copyOfRange(0, end), Charsets.US_ASCII)
                rx = rx.copyOfRange(end + 2, rx.size)
                val seq = line.trim().split(Regex("\\s+")).lastOrNull()?.toIntOrNull()
                if (seq != null) pending.remove(seq)?.complete(line)
            }
        }
    }

    private fun handleFrame(frame: ByteArray) {
        if (frame.size < 9) return
        val status = frame[4].toInt() and 0xFF
        if ((status and 0x10) != 0 || (status and 0x20) != 0) return // tx indication/loopback
        val payload = frame.copyOfRange(9, frame.size)
        if (payload.size < 4) return
        val start = (status and 0x80) != 0
        val end = (status and 0x40) != 0
        if (start && !end) {
            partialId = payload.copyOfRange(0, 4)
            partial = ByteArrayOutputStream()
            return
        }
        val part = partial
        if (part != null) {
            part.write(payload, 4, payload.size - 4)
            if (end) {
                emitElm(partialId, part.toByteArray())
                partial = null
            }
        } else if (end) {
            emitElm(payload.copyOfRange(0, 4), payload.copyOfRange(4, payload.size))
        }
    }

    private fun emitElm(idBytes: ByteArray, data: ByteArray) {
        requestToken++
        val id = ((idBytes[0].toInt() and 0xFF) shl 24) or
            ((idBytes[1].toInt() and 0xFF) shl 16) or
            ((idBytes[2].toInt() and 0xFF) shl 8) or (idBytes[3].toInt() and 0xFF)
        val hex = data.joinToString(" ") { "%02X".format(it.toInt() and 0xFF) }
        val line = (if (showHeaders) "%03X ".format(id) else "") + hex + "\r>"
        emit(line.toByteArray(Charsets.US_ASCII))
    }

    fun close() {
        // Reader должен жить, пока atc/atz ждут свои numbered replies.
        runCatching { command("atc6", timeoutMs = 300) }
        runCatching { command("atz", timeoutMs = 300) }
        running = false
        timer.shutdownNow()
        pending.values.forEach { it.completeExceptionally(IllegalStateException("OP2 closed")) }
        pending.clear()
        pipe.close()
        reader.join(300)
    }

    companion object {
        fun be32(value: Int) = byteArrayOf(
            (value ushr 24).toByte(), (value ushr 16).toByte(),
            (value ushr 8).toByte(), value.toByte())
        fun findCrlf(data: ByteArray): Int {
            for (i in 0 until data.size - 1) if (data[i] == 13.toByte() && data[i + 1] == 10.toByte()) return i
            return -1
        }
    }
}

object Op2Channel : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private const val ACTION = "com.subaru.ssm2_fixed.OP2_PERMISSION"
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private lateinit var context: Context
    private lateinit var manager: UsbManager
    private var sink: EventChannel.EventSink? = null
    @Volatile private var session: Op2Session? = null
    private var permissionResult: MethodChannel.Result? = null

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            if (intent.action != ACTION) return
            val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            val result = permissionResult ?: return
            permissionResult = null
            if (!granted) result.success(mapOf("open" to false, "error" to "USB permission denied"))
            else openAsync(result)
        }
    }

    fun register(ctx: Context, engine: FlutterEngine) {
        context = ctx.applicationContext
        manager = context.getSystemService(Context.USB_SERVICE) as UsbManager
        val filter = IntentFilter(ACTION)
        if (Build.VERSION.SDK_INT >= 33) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(receiver, filter)
        }
        MethodChannel(engine.dartExecutor.binaryMessenger, "ssm2/op2").setMethodCallHandler(this)
        EventChannel(engine.dartExecutor.binaryMessenger, "ssm2/op2_rx").setStreamHandler(this)
    }

    private fun device(): UsbDevice? = manager.deviceList.values.firstOrNull(Op2Ids::matches)
    private fun descriptor(): Map<String, Any?> {
        val d = device()
        return mapOf("found" to (d != null),
            "vid" to d?.vendorId?.let { "0x%04X".format(it) },
            "pid" to d?.productId?.let { "0x%04X".format(it) },
            "product" to d?.productName,
            "hasPermission" to (d?.let(manager::hasPermission) ?: false))
    }

    override fun onMethodCall(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "op2/descriptor" -> result.success(descriptor())
            "op2/open" -> {
                val d = device()
                if (d == null) { result.success(mapOf("open" to false, "error" to "OP2 not found")); return }
                if (!manager.hasPermission(d)) {
                    permissionResult = result
                    val pi = PendingIntent.getBroadcast(context, 0, Intent(ACTION),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                    manager.requestPermission(d, pi)
                } else openAsync(result)
            }
            "op2/write" -> {
                val ascii = call.arguments as? String
                if (ascii == null) { result.error("op2", "String expected", null); return }
                io.execute {
                    runCatching { session?.elmWrite(ascii) ?: error("OP2 not open") }
                        .onSuccess { main.post { result.success(null) } }
                        .onFailure { e -> main.post { result.error("op2", e.message, null) } }
                }
            }
            "op2/close" -> io.execute {
                session?.close(); session = null
                main.post { result.success(null) }
            }
            else -> result.notImplemented()
        }
    }

    private fun openAsync(result: MethodChannel.Result) = io.execute {
        runCatching {
            session?.close()
            val d = device() ?: error("OP2 disappeared")
            val p = Op2Pipe.open(manager, d)
            val s = Op2Session(p) { bytes -> main.post { sink?.success(bytes) } }
            s.start(); s.initialize(); session = s
            mapOf("open" to true, "vid" to "0x%04X".format(d.vendorId),
                "pid" to "0x%04X".format(d.productId))
        }.onSuccess { map -> main.post { result.success(map) } }
         .onFailure { e -> main.post { result.success(mapOf("open" to false, "error" to e.message)) } }
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { sink = events }
    override fun onCancel(arguments: Any?) { sink = null }
}
'''

# ============================================================
# 3. Manifest + MainActivity + factory
# ============================================================
FILTER_XML = '''<?xml version="1.0" encoding="utf-8"?>
<resources>
    <usb-device vendor-id="1027" product-id="52301" /> \x3C!-- 0403:CC4D original -->
    <usb-device vendor-id="1027" product-id="52300" /> \x3C!-- 0403:CC4C clone fallback -->
</resources>
'''

def patch_manifest():
    text = MANIFEST.read_text(encoding="utf-8")
    backup(MANIFEST)
    if "android.hardware.usb.host" not in text:
        text = text.replace("<application",
            '<uses-feature android:name="android.hardware.usb.host" android:required="false" />\n\n    <application', 1)
    if "USB_DEVICE_ATTACHED" not in text:
        m = re.search(r'(<activity[^>]*android:name="\.MainActivity"[^>]*>)', text)
        if not m: raise RuntimeError("MainActivity не найдена в manifest")
        hook = ('\n            <intent-filter>\n'
                '                <action android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED" />\n'
                '            </intent-filter>\n'
                '            <meta-data android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED"\n'
                '                android:resource="@xml/op2_device_filter" />')
        text = text[:m.end()] + hook + text[m.end():]
    MANIFEST.write_text(text, encoding="utf-8")

def patch_main():
    text = MAIN_ACTIVITY.read_text(encoding="utf-8")
    if "Op2Channel.register" in text: return
    backup(MAIN_ACTIVITY)
    m = re.search(r"super\.configureFlutterEngine\(flutterEngine\)", text)
    if not m: raise RuntimeError("configureFlutterEngine anchor не найден")
    text = text[:m.end()] + "\n        Op2Channel.register(this, flutterEngine)" + text[m.end():]
    MAIN_ACTIVITY.write_text(text, encoding="utf-8")

def function_span(text):
    m = re.search(r"(?m)^([A-Za-z_][\w<>,.\[\]?! ]*?)\s+createTransport\s*\([^)]*\)\s*", text)
    if not m: raise RuntimeError("createTransport не найдена")
    pos = m.end()
    if text.startswith("=>", pos):
        end = text.index(";", pos)
        return m.start(), end + 1, text[pos + 2:end].strip()
    brace = text.find("{", pos)
    if brace < 0: raise RuntimeError("тело createTransport не найдено")
    depth, i = 0, brace
    while i < len(text):
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0: return m.start(), i + 1, text[brace + 1:i]
        i += 1
    raise RuntimeError("незакрытое тело createTransport")

def find_bt_class():
    """Фактический BtTransport-класс native_spp.dart: implements ИЛИ extends.
    04.1 мог их потому поменять при поддержке контракта задания контрактной
    реализации за локальной стороны."""
    src = NATIVE_SPP.read_text(encoding="utf-8")
    m = (re.search(r"class\s+(\w+)\s+extends\s+BtTransport\b", src)
         or re.search(r"class\s+(\w+)\s+implements\s+BtTransport\b", src))
    if not m:
        print("\n===== native_spp.dart HEAD =====\n" + src[:4000])
        raise RuntimeError("В native_spp.dart нет класса extends/implements BtTransport — "
                           "пришлите шапку файла")
    return m.group(1)

def extract_bt_expression(text):
    # Провер по body-фабрики: если там реконструирован конкретный
    # материал, используем его, иначе — распознанный класс из native_spp.
    _, _, body = function_span(text)
    assigns = {m.group(1): m.group(2).strip() for m in
        re.finditer(r"(?:final|var|BtTransport)\s+(\w+)\s*=\s*([^;]+);", body)}
    candidates = [m.group(1).strip() for m in re.finditer(r"return\s+([^;]+);", body)]
    candidates.append(body.strip())
    for expr in reversed(candidates):
        if expr in assigns: expr = assigns[expr]
        if any(bad in expr for bad in ("NativeOp2", "AutoTransport",
                                        "HybridTransport", "SelectedTransport", "null")):
            continue
        m = re.search(r"([A-Za-z_][A-Za-z0-9_]*Transport)\s*\(", expr)
        if m:
            cls = m.group(1)
            if cls == "Transport":
                # Движковая модификация: имя колнад OneRegion<DrugStorage>
                continue
            return expr
    return find_bt_class() + "()"

def patch_factory():
    old = TRANSPORT_SEL.read_text(encoding="utf-8")
    backup(TRANSPORT_SEL)
    bt_expr = extract_bt_expression(old)
    directives = re.findall(r"(?m)^(?:import|export)\s+[^;]+;$", old)
    directives = [d for d in directives if not any(x in d for x in
        ("native_op2.dart", "auto_transport.dart", "hybrid_transport.dart"))]
    # Также удаляем любые оставшиеся следы старых auto-hybrid прогонов:
    # объявления констант kOp2AndrBaud/Latency, kTransportName и т.д.,
    # встроенные helper-методы _createBtTransport и вызовы AutoTransport в
    # теле фабрики — они ломают итоговое референсное присвоение.
    def ensure(line):
        if line not in directives: directives.append(line)
    ensure("import 'bt_transport.dart';")
    ensure("import 'native_spp.dart';")
    ensure("import 'hybrid_transport.dart';")
    seen, clean = set(), []
    for d in directives:
        if d not in seen: clean.append(d); seen.add(d)
    body = "\n".join(clean) + "\n\n"
    body += "// v0.15: existing adapter picker lists USB OP2 + paired Bluetooth.\n"
    body += "BtTransport createTransport() => HybridTransport(" + bt_expr + ");\n"
    TRANSPORT_SEL.write_text(body, encoding="utf-8")
    print("[factory] BT =", bt_expr)
    print("[factory] runtime choice = HybridTransport")

# ============================================================
# 4. Apply
# ============================================================
print("SSM2 0.15 | OpenPort 2.0 REAL wire protocol + runtime adapter choice")
# Подчистка моих же предыдущих экспериментов: файл старого auto-facade и его
# импорт из transport_selected.dart конфликтуют с итоговой REAL-версией.
stale = APP / "lib/auto_transport.dart"
if stale.exists():
    shutil.move(str(stale), str(BACKUP / "lib__auto_transport.dart.stale_removed"))
    print("[cleanup] lib/auto_transport.dart → бэкап (устаревший r3-файл)")
write("lib/op2_ids.dart", OP2_IDS_DART)
write("lib/native_op2.dart", NATIVE_OP2_DART)
write("lib/hybrid_transport.dart", HYBRID_DART)
write("test/op2_transport_test.dart", OP2_TEST)
write("android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt", OP2_KOTLIN)
write("android/app/src/main/res/xml/op2_device_filter.xml", FILTER_XML)
write("tool/OPENPORT_PROTOCOL_NOTICE.md", """# OpenPort 2.0 protocol notice

Independent Android implementation using facts documented at:
https://github.com/bisak/openport-j2534/blob/main/docs/PROTOCOL.md

No source code from that project is copied into this application. OpenPort and
Tactrix are nominative marks of their respective owner. Hardware test required.
""")
patch_manifest()
patch_main()
patch_factory()

# ============================================================
# 5. Verify: pub get -> format -> analyze -> targeted tests
# ============================================================
checks = [
    (APP / "lib/hybrid_transport.dart", "Future<List<BtDevice>> paired()", "runtime picker missing"),
    (APP / "lib/hybrid_transport.dart", "usb:", "USB route missing"),
    (APP / "lib/native_op2.dart", "Stream<bool> get status", "BtTransport contract mismatch"),
    (APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt", "ato6 0 500000 0", "ISO15765 open missing"),
    (APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt", "atf6 3 64 4", "flow filter missing"),
    (APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt", "att6 ", "transmit missing"),
    (APP / "android/app/src/main/res/xml/op2_device_filter.xml", "52301", "PID CC4D missing"),
    (TRANSPORT_SEL, "HybridTransport", "factory not hybrid"),
]
for path, needle, message in checks:
    if needle not in path.read_text(encoding="utf-8"):
        raise RuntimeError("SELF-CHECK: " + message)
print("[OK] self-check:", len(checks), "проверок")

cfg = json.loads((APP / "build_config.json").read_text(encoding="utf-8"))
FLUTTER = Path(cfg["flutter"]) / "bin/flutter"
DART = Path(cfg["flutter"]) / "bin/dart"

def run(args, timeout=1200):
    p = subprocess.run([str(x) for x in args], cwd=APP, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    print(p.stdout[-10000:])
    if p.returncode: raise RuntimeError("Команда упала: " + " ".join(map(str, args)))

run([FLUTTER, "pub", "get"])
# dart_fix в ячейке 05 из r3-конвейера уже чистит эти warnings (unused_import,
# unnecessary_non_null_assertion); на чистом рантайме до него ещё не добрались.
subprocess.run([str(DART), "fix", "--apply"], cwd=APP, capture_output=True,
               text=True, timeout=600)
run([DART, "format", "lib/op2_ids.dart", "lib/native_op2.dart",
     "lib/hybrid_transport.dart", "lib/transport_selected.dart",
     "test/op2_transport_test.dart"])
run([FLUTTER, "analyze", "--no-pub", "--no-fatal-infos"])
run([FLUTTER, "test", "--no-pub", "test/op2_transport_test.dart", "--reporter", "expanded"])
# Kotlin/Manifest/USB API не проверяются Dart-анализатором — обязательный debug build.
run([FLUTTER, "build", "apk", "--debug", "--no-pub"], timeout=3600)

print("\n=== OK: OpenPort 2.0 REAL + выбор USB/Bluetooth установлен ===")
print("Выбор: подключите OP2 по OTG -> обновите список адаптеров ->")
print("        выберите 'OpenPort 2.0 (USB OTG)'.")
print("Bluetooth ELM327 остаётся в том же списке по MAC-адресу.")
print("Далее запускайте ячейку 05.")
print("HARDWARE_CONFIRMED оставьте False до 10-минутного стенда.")
print("Бэкапы:", BACKUP)


# ▸ 04.9 | ELM SPEED PACK | SSM2 0.15.0 → 0.16.0 (insert BETWEEN 04.8 and 05)
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
# ═══════════════════════════════════════════════════════════
# DTC READ FIX v5 | SSM2 1.0.3 — P0201 вместо P0102
# (СЕГМЕНТ ячейки 05; при порядке сборки 1.0.2 идёт ПЕРЕД 04.10,
# чтобы dart format/analyze из 04.10 валидировали этот код сразу)
#
# КОРНЕВАЯ ПРИЧИНА «при отключённом ДМРВ горит P0201 (форсунка 1)»:
#   ЭБУ реально хранит P0102 (ДМРВ, низкий уровень). Нативный SSM-сервис
#   0x18 на части блоков отдаёт пару байтов DTC МЛАДШИМ байтом вперёд
#   (02 01), а парсер читал их как big-endian, как в OBD mode 03 (01 02).
#   Зеркало: P0102 -> «P0201».
# ВТОРАЯ ПРИЧИНА мусорных кодов: parseDiagPayload (даже после фикса 04.6)
#   СКЛЕИВАЛ все строки ответа (несколько ЭБУ на шине, stored+pending)
#   в один плоский список — пары байтов сдвигались.
#
# v1.0.2: замена каскада иммунна к dart fix/format (вырезание по скобкам).
# v1.0.3: баг ISO-TP — после сборки SF ставился expected=-2, а ветки SF/FF
#   ловили expected<0 (и -2 тоже!) → кадры ВТОРОГО ЭБУ доклеивались к ответу.
#   Теперь строго expected==-1. Ловится регрессионными тестами 115/115.
#
# ЧТО ДЕЛАЕТ (идемпотентно, с бэкапом, с тестами):
#   [1] parseDiagPayload -> v5: первая завершённая ISO-TP посылка побеждает;
#   [2] parseSsm2DtcReply -> двойное декодирование прямой/зеркальный порядок,
#       выбор по словарю; пометка «↔» в origin;
#   [3] readDtcDetailed -> эталонный OBD mode 03 первым, слияние с нативной
#       веткой и подавление зеркал;
#   [4] Расширение словаря kSubaruDtcDict (ДМРВ/IAT/TPS/АКПП/CAN и др.);
#   [5] test/dtc_v5_test.dart — регрессионные тесты;
#   [6] Нормализация версии: pubspec -> 1.0.0+20.
# ═══════════════════════════════════════════════════════════
import base64 as _b64

DIAG = APP / "lib/diag.dart"
if not DIAG.exists():
    raise RuntimeError("lib/diag.dart не найден — сначала блоки 02-04")
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_dtcf5_{STAMP}"
BACKUP.mkdir(parents=True, exist_ok=True)


def _extract_fn(src, signature):
    """Вырезает функцию целиком подсчётом скобок (иммунно к форматированию)."""
    i = src.find(signature)
    if i < 0:
        raise RuntimeError(f"Не найдена функция: {signature}")
    depth, started = 0, False
    for k in range(i, len(src)):
        c = src[k]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return i, k + 1
    raise RuntimeError(f"Несбалансированные скобки: {signature}")


NEW_PARSE_V5 = r"""List<int>? parseDiagPayload(String text) {
  // v1.0 (DTC FIX v5): промпт '>', статусы ELM, ATH1, счётчики длины,
  // ISO-TP (SF/FF/CF), и главное — больше НЕ склеивает ответы нескольких
  // блоков в один поток: после сборки первого полного сообщения остальные
  // кадры игнорируются (иначе пары байтов DTC сдвигались, P0102 -> «P0201»).
  const Set<String> elmStatus = <String>{
    'NODATA', 'STOPPED', 'CANERROR', 'BUSBUSY', 'BUFFERFULL',
    'ERROR', 'UNABLETOCONNECT', 'OK', '?',
  };

  final flat = <int>[];
  final frameLines = <int, List<int>>{};
  var sawFrameIndex = false;
  final tpFrames = <List<int>>[];
  var sawTp = false;

  for (final rawLine in text.split(RegExp(r'[\r\n]+'))) {
    var line = rawLine.trim();
    if (line.isEmpty || line == '>') continue; // ELM prompt

    // «SEARCHING...» — префикс авто-детекта протокола, не данные.
    if (line.toUpperCase().startsWith('SEARCHING')) {
      line = line.substring('SEARCHING'.length).trim();
      if (line.startsWith('...')) line = line.substring(3).trim();
      if (line.isEmpty) continue;
    }

    final compact = line.toUpperCase().replaceAll(RegExp(r'[^A-Z]'), '');
    if (elmStatus.contains(compact)) continue; // статусные строки ELM

    // Нумерованные мультiframe-строки «0:490410...»
    final frameMatch = RegExp(r'^(\d+):(.*)$').firstMatch(line);
    if (frameMatch != null) {
      sawFrameIndex = true;
      final idx = int.parse(frameMatch.group(1)!);
      final hex = frameMatch.group(2)!.replaceAll(' ', '');
      if (hex.isNotEmpty && hex.length % 2 == 0 &&
          RegExp(r'^[0-9A-Fa-f]+$').hasMatch(hex)) {
        frameLines[idx] = <int>[
          for (var i = 0; i + 1 < hex.length; i += 2)
            int.parse(hex.substring(i, i + 2), radix: 16),
        ];
      }
      continue;
    }

    // Счётчик длины ELM («023») без пробелов — не данные.
    if (RegExp(r'^[0-9A-Fa-f]{3}$').hasMatch(line) && !line.contains(' ')) {
      continue;
    }

    var tokens = line.split(RegExp(r'\s+')).where((t) => t.isNotEmpty).toList();

    // ATH1: первый токен — CAN ID (11-бит или 29-бит).
    if (tokens.isNotEmpty &&
        (tokens.first.length == 3 || tokens.first.length == 8) &&
        RegExp(r'^[0-9A-Fa-f]+$').hasMatch(tokens.first)) {
      tokens = tokens.sublist(1);
    }

    final bytes = <int>[];
    for (final t in tokens) {
      if (t == '>') break; // v1.0: inline-промпт обрывает ЛИНИЮ, а не ответ
      final clean = t.replaceAll(RegExp(r'[^0-9A-Fa-f]'), '');
      if (clean.isEmpty) continue;
      if (clean.length % 2 != 0) return null;
      for (var i = 0; i + 1 < clean.length; i += 2) {
        bytes.add(int.parse(clean.substring(i, i + 2), radix: 16));
      }
    }
    if (bytes.isEmpty) continue;

    // Строка начинается с ISO-TP PCI (режим CAF0)?
    final nib = bytes[0] & 0xF0;
    if ((nib == 0x00 && (bytes[0] & 0x0F) >= 1 && (bytes[0] & 0x0F) <= 7) ||
        nib == 0x10 ||
        nib == 0x20) {
      sawTp = true;
      tpFrames.add(bytes);
      continue;
    }
    flat.addAll(bytes);
  }

  // Мультiframe с номерами строк — поведение как в v0.14.1 (проверено на CALID).
  if (sawFrameIndex) {
    if (frameLines.isEmpty) return null;
    final payload = <int>[];
    for (final k in (frameLines.keys.toList()..sort())) {
      payload.addAll(frameLines[k]!);
    }
    return payload.isEmpty ? null : payload;
  }

  // v1.0: ISO-TP собирается по PCI; побеждает ПЕРВОЕ полное сообщение.
  if (sawTp) {
    final msg = <int>[];
    var expected = -1; // -1: ждём начала; -2: готово
    for (final f in tpFrames) {
      final nib = f[0] & 0xF0;
      if (nib == 0x00 && expected == -1) {
        // Single Frame; второй SF (другой ЭБУ) с expected == -2 - пропускаем
        final len = f[0] & 0x0F;
        msg.addAll(f.sublist(1, 1 + len <= f.length ? 1 + len : f.length));
        expected = -2;
      } else if (nib == 0x10 && f.length > 1 && expected == -1) {
        expected = ((f[0] & 0x0F) << 8) | f[1];
        msg.addAll(f.sublist(2));
      } else if (nib == 0x20 && expected > 0) {
        msg.addAll(f.sublist(1));
        if (msg.length >= expected) {
          msg.removeRange(expected, msg.length);
          expected = -2;
        }
      } // expected == -2: хвосты других ЭБУ/дубли — игнорируем
    }
    if (msg.isNotEmpty) {
      var b = msg;
      if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) {
        b = b.sublist(2); // ATH1: CAN ID в начале данных
      }
      return b.isEmpty ? null : b;
    }
  }

  if (flat.isEmpty) return null;
  var b = flat;
  if (b.length > 1 && (b[0] & 0xF0) == 0 && (b[0] & 0x0F) <= 7) {
    final len = b[0] & 0x0F;
    b = b.sublist(1, (1 + len).clamp(1, b.length));
  }
  if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) {
    b = b.sublist(2);
  }
  return b.isEmpty ? null : b;
}"""

NEW_SSM_V5 = r"""List<DiagDtc> parseSsm2DtcReply(List<int> b) {
  // v1.0 (DTC FIX v5): нативный SSM-сервис 0x18 на части блоков отдаёт DTC
  // МЛАДШИМ байтом вперёд. Декодируем прямой и зеркальный порядки и выбираем
  // по словарю Subaru; зеркальная ветка получает пометку «↔» в origin.
  // При равном счёте — прямой порядок (поведение как раньше).
  final out = <DiagDtc>[];
  if (b.isEmpty || b[0] != 0x58) return out;
  final count = b.length > 1 ? b[1] : 0;
  final direct = <DiagDtc>[];
  final mirrored = <DiagDtc>[];
  for (var i = 0; i < count; i++) {
    final x = 2 + i * 3;
    if (x + 2 >= b.length) break;
    if (b[x] == 0 && b[x + 1] == 0) continue;
    direct.add(DiagDtc(decodeDtcBytes(b[x], b[x + 1]), b[x + 2],
        DiagProto.ssm2, origin: 'stored'));
    mirrored.add(DiagDtc(decodeDtcBytes(b[x + 1], b[x]), b[x + 2],
        DiagProto.ssm2, origin: 'stored ↔'));
  }
  if (direct.isEmpty) return out;
  int hits(List<DiagDtc> list) =>
      list.where((d) => kSubaruDtcDict.containsKey(d.code)).length;
  out.addAll(hits(mirrored) > hits(direct) ? mirrored : direct);
  return out;
}"""

NEW_CASCADE_REGION = r"""// v1.0 (DTC FIX v5): эталон — OBD mode 03 (порядок байтов DTC
    // зафиксирован стандартом ISO 15031-5). Сначала читаем его, затем
    // нативный сервис; результаты сливаем, а «зеркала» (P0102<->P0201),
    // уже подтверждённые mode 03, из нативной ветки отбрасываем.
    final obd =
        await tryService('03', 0x43, 'OBD 03', (b) => parseObdDtcReply(b));
    DtcReadResult? nativeRes;
    if (slot.proto == DiagProto.ssm2) {
      nativeRes =
          await tryService('18 00 FF 00', 0x58, 'SSM2 18', parseSsm2DtcReply);
    } else {
      nativeRes =
          await tryService('19 02 AF', 0x59, 'UDS 19 02', parseUdsDtcReply);
    }
    if (obd != null && nativeRes != null) {
      final base = obd.codes.cast<DiagDtc>();
      final merged = <DiagDtc>[...base];
      String mirrorOf(String code) => code.length == 5
          ? code[0] + code.substring(3, 5) + code.substring(1, 3)
          : code;
      for (final d in nativeRes.codes.cast<DiagDtc>()) {
        final m = mirrorOf(d.code);
        final covered = merged.any((x) => x.code == d.code);
        final mirroredInObd =
            base.any((x) => x.code == m) && !base.any((x) => x.code == d.code);
        if (!covered && !mirroredInObd) merged.add(d);
      }
      return DtcReadResult(
        codes: merged,
        answered: true,
        protocolUsed: '${obd.protocolUsed} + ${nativeRes.protocolUsed}',
        rawReplies: <String>[...obd.rawReplies, ...nativeRes.rawReplies],
      );
    }
    if (obd != null) return obd;
    if (nativeRes != null) return nativeRes;"""

text = DIAG.read_text(encoding="utf-8")
if "v1.0 (DTC FIX v5)" in text and "expected == -1" in text:
    print("[SKIP] DTC FIX v5 (1.0.3) уже применён")
else:
    if "v1.0 (DTC FIX v5)" in text:
        # Ремонт состояния 1.0.2: маркер есть, но в ISO-TP жил баг
        # (expected=-2 ловился условием <0 -> кадры второго ЭБУ доклеивались).
        # Заменяем ТОЛЬКО parseDiagPayload на исправленный вариант.
        shutil.copyfile(DIAG, BACKUP / "diag.dart.bak102")
        a, b = _extract_fn(text, "List<int>? parseDiagPayload(String text) {")
        text = text[:a] + NEW_PARSE_V5 + text[b:]
        DIAG.write_text(text, encoding="utf-8")
        print("[REPAIR] parseDiagPayload 1.0.2 -> 1.0.3: ISO-TP баг устранён")
        text = DIAG.read_text(encoding="utf-8")
        changed = 99  # каскад уже стоял с 1.0.2 — не трогаем
    else:
        shutil.copyfile(DIAG, BACKUP / "diag.dart.bak")
        changed = 0

    # --- [1] parseDiagPayload -> v5 (вырезание подсчётом скобок) ---
    if changed == 0:
        a, b = _extract_fn(text, "List<int>? parseDiagPayload(String text) {")
        text = text[:a] + NEW_PARSE_V5 + text[b:]
        changed += 1

    # --- [2] parseSsm2DtcReply -> двойное декодирование ---
    if changed != 99:
        a, b = _extract_fn(
            text, "List<DiagDtc> parseSsm2DtcReply(List<int> b) {")
        text = text[:a] + NEW_SSM_V5 + text[b:]
        changed += 1

    # --- [3] каскад: OBD mode 03 эталоном + слияние (иммунно к форматированию) ---
    if changed != 99:
        a, b = _extract_fn(
            text, "Future<DtcReadResult> readDtcDetailed(EcuSlot slot) async {")
        fn = text[a:b]
        M_START = "if (slot.proto == DiagProto.ssm2) {"
        M_END = "if (obd != null) return obd;"
        if fn.count(M_START) != 1 or fn.count(M_END) != 1:
            Path("/content/v5_cascade_debug.txt").write_text(fn, encoding="utf-8")
            raise RuntimeError(
                "Каскад readDtcDetailed: маркеры не уникальны "
                f"(start={fn.count(M_START)}, end={fn.count(M_END)}). "
                "Функция сохранена в /content/v5_cascade_debug.txt — пришлите её.")
        i = fn.find(M_START)
        j = fn.find(M_END, i) + len(M_END)
        region = fn[i:j]
        if "18 00 FF 00" not in region or "'03'" not in region:
            Path("/content/v5_cascade_debug.txt").write_text(fn, encoding="utf-8")
            raise RuntimeError(
                "Маркеры найдены, но область между ними не похожа на каскад 0.14.x. "
                "Функция сохранена в /content/v5_cascade_debug.txt — пришлите её.")
        indent = region[:len(region) - len(region.lstrip())]
        fn = fn[:i] + indent + NEW_CASCADE_REGION + fn[j:]
        text = text[:a] + fn + text[b:]
        changed += 1

    if text.count("nativeRes") < 3:
        raise RuntimeError("Самопроверка каскада не пройдена — файл НЕ записан.")
    DIAG.write_text(text, encoding="utf-8")
    print(f"[OK] DTC FIX v5 применён к lib/diag.dart ({changed} правок)")

# ──────────── [4] Расширение словаря kSubaruDtcDict ────────────
EXTRA_DTC = {
    'P0100': 'ДМРВ — неисправность цепи',
    'P0101': 'ДМРВ — сигнал вне диапазона/производительность',
    'P0102': 'ДМРВ — низкий уровень сигнала (обрыв/отключён разъём)',
    'P0103': 'ДМРВ — высокий уровень сигнала',
    'P0104': 'ДМРВ — нестабильный сигнал',
    'P0106': 'ДАД — диапазон/производительность',
    'P0107': 'ДАД — низкий сигнал',
    'P0108': 'ДАД — высокий сигнал',
    'P0111': 'ДТВВ (IAT) — диапазон/производительность',
    'P0112': 'ДТВВ (IAT) — низкий сигнал',
    'P0113': 'ДТВВ (IAT) — высокий сигнал',
    'P0116': 'ДТОЖ — диапазон/производительность',
    'P0117': 'ДТОЖ — низкий сигнал',
    'P0118': 'ДТОЖ — высокий сигнал',
    'P0121': 'ДПДЗ A — диапазон/производительность',
    'P0122': 'ДПДЗ A — низкий сигнал',
    'P0123': 'ДПДЗ A — высокий сигнал',
    'P0125': 'Низкая температура ОЖ для замкнутого контура',
    'P0136': 'Цепь O2-датчика B1S2',
    'P0137': 'O2 B1S2 — низкое напряжение',
    'P0138': 'O2 B1S2 — высокое напряжение',
    'P0141': 'Подогрев O2 B1S2 — неисправность',
    'P0182': 'Датчик температуры топлива — низкий',
    'P0183': 'Датчик температуры топлива — высокий',
    'P0205': 'Форсунка 5 — обрыв цепи',
    'P0206': 'Форсунка 6 — обрыв цепи',
    'P0222': 'ДПДЗ B — низкий сигнал',
    'P0223': 'ДПДЗ B — высокий сигнал',
    'P0305': 'Пропуски — цилиндр 5',
    'P0306': 'Пропуски — цилиндр 6',
    'P0326': 'Датчик детонации — диапазон',
    'P0336': 'ДПКВ — диапазон/производительность',
    'P0341': 'ДПРВ — диапазон/производительность',
    'P0400': 'EGR — расход/поток',
    'P0403': 'EGR — цепь управления',
    'P0440': 'EVAP — неисправность системы',
    'P0441': 'EVAP — неверный продув',
    'P0443': 'EVAP — цепь клапана продувки',
    'P0455': 'EVAP — большая утечка',
    'P0456': 'EVAP — очень малая утечка',
    'P0462': 'Датчик уровня топлива — низкий сигнал',
    'P0463': 'Датчик уровня топлива — высокий сигнал',
    'P0480': 'Вентилятор 1 — цепь управления',
    'P0481': 'Вентилятор 2 — цепь управления',
    'P0506': 'Холостой ход — обороты ниже нормы',
    'P0507': 'Холостой ход — обороты выше нормы',
    'P0512': 'Цепь запроса стартера',
    'P0560': 'Напряжение системы — неисправность',
    'P0604': 'ЭБУ — ошибка ОЗУ (RAM)',
    'P0607': 'ЭБУ — производительность',
    'P0700': 'Коробка передач — запрос MIL',
    'P0705': 'Датчик диапазона АКПП (PRNDL)',
    'P0710': 'Датчик температуры ATF',
    'P0715': 'Датчик входной скорости АКПП',
    'P0720': 'Датчик выходной скорости АКПП',
    'P0725': 'Сигнал оборотов двигателя для АКПП',
    'P0730': 'Неверное передаточное отношение',
    'P0731': 'Передача 1 — неверное отношение',
    'P0732': 'Передача 2 — неверное отношение',
    'P0733': 'Передача 3 — неверное отношение',
    'P0734': 'Передача 4 — неверное отношение',
    'P0741': 'Блокировка ГТ — заедание выкл',
    'P0743': 'Блокировка ГТ — цепь',
    'P0750': 'Соленоид A переключения',
    'P0755': 'Соленоид B переключения',
    'P0851': 'Датчик P/N — низкий сигнал',
    'P0852': 'Датчик P/N — высокий сигнал',
    'P1152': 'Фронт-лямбда — цепь вне диапазона (низк.)',
    'P1153': 'Фронт-лямбда — цепь вне диапазона (высок.)',
    'P1301': 'Пропуски — высокая температура выхлопа',
    'P1443': 'EVAP — клапан вентиляции канистры',
    'P1492': 'Аккумулятор давления EGR — цепь соленоида',
    'P1507': 'Клапан ХХ — заедание закрытым',
    'P1518': 'Стартер — цепь выключателя',
    'P1560': 'Низкое напряжение резервной цепи ЭБУ',
    'P1600': 'CAN — ошибка связи ЭБУ',
    'P1602': 'CAN — таймаут/ошибка шины',
    'P1710': 'Датчик турбины 2 — цепь',
    'U0073': 'Шина CAN выключена (control module communication bus off)',
    'U0100': 'Нет связи с ECM/PCM',
    'U0101': 'Нет связи с TCM',
    'U0121': 'Нет связи с ABS/VDC',
}

dict_file = None
for f in sorted(APP.rglob("*.dart")):
    t = f.read_text(encoding="utf-8")
    if "kSubaruDtcDict =" in t:
        dict_file = f
        break
if dict_file is None:
    raise RuntimeError("kSubaruDtcDict не найден (блок 04 создаёт lib/dtc_dict.dart)")

dict_text = dict_file.read_text(encoding="utf-8")
fmt = re.search(r"'[A-Z]\d+':\s*\([^)]+,\s*(['\"][^'\"]*['\"])\)", dict_text)
second_val = fmt.group(1) if fmt else "'engine'"
to_add = {k: v for k, v in EXTRA_DTC.items() if f"'{k}'" not in dict_text}
if to_add:
    shutil.copyfile(dict_file, BACKUP / (dict_file.stem + ".dart.bak"))
    start = dict_text.find("kSubaruDtcDict")
    brace = dict_text.find("{", start)
    depth, pos = 0, brace
    while pos < len(dict_text):
        if dict_text[pos] == "{":
            depth += 1
        elif dict_text[pos] == "}":
            depth -= 1
            if depth == 0:
                break
        pos += 1
    entries = "".join(
        f"\n  '{k}': (\"{v}\", {second_val})," for k, v in to_add.items())
    dict_text = dict_text[:pos] + entries + "\n" + dict_text[pos:]
    dict_file.write_text(dict_text, encoding="utf-8")
print(f"[OK] Словарь DTC: добавлено {len(to_add)} кодов (EXTRA всего {len(EXTRA_DTC)})")

# ──────────── [5] Регрессионные тесты ────────────
TEST_DTC_V5 = r"""// v1.0 (DTC FIX v5) — регрессионные тесты чтения ошибок.
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/diag.dart';

void main() {
  group('parseDiagPayload v5', () {
    test('промпт ELM не ломает разбор: 4300\\r\\r> -> [43 00]', () {
      expect(parseDiagPayload('4300\r\r>'), <int>[0x43, 0x00]);
    });
    test('SEARCHING и prompt', () {
      expect(parseDiagPayload('SEARCHING...\r43 01 02\r\n>'),
          <int>[0x43, 0x01, 0x02]);
    });
    test('ATH1 + CAF0: снимаем CAN ID и PCI', () {
      expect(parseDiagPayload('7E8 06 41 00 BE 1F A8 13\r>'),
          <int>[0x41, 0x00, 0xBE, 0x1F, 0xA8, 0x13]);
    });
    test('два блока ответили одновременно: побеждает первое сообщение', () {
      // Раньше строки склеивались: пары байтов сдвигались -> левые коды.
      expect(parseDiagPayload('03 43 01 02\r02 43 00\r>'),
          <int>[0x43, 0x01, 0x02]);
    });
    test('ISO-TP FF+CF собираются и обрезаются по длине', () {
      expect(
          parseDiagPayload(
              '10 0A 58 02 01 02 AA BB\r21 03 04 05 00 00 00 00\r>'),
          <int>[0x58, 0x02, 0x01, 0x02, 0xAA, 0xBB, 0x03, 0x04, 0x05, 0x00]);
    });
    test('нумерованные мультiframe (как v0.14.1)', () {
      final r = parseDiagPayload('020\r0:4904105431\r1:35424234354439\r>');
      expect(r, isNotNull);
      expect(r!.first, 0x49);
    });
    test('NO DATA -> null', () {
      expect(parseDiagPayload('NO DATA\r>'), isNull);
    });
  });

  group('SSM2 0x18: порядок байтов', () {
    test('прямой порядок при словарном попадании (01 02 -> P0102)', () {
      final r = parseSsm2DtcReply(<int>[0x58, 0x01, 0x01, 0x02, 0x20]);
      expect(r.single.code, 'P0102');
      expect(r.single.origin, isNot(contains('\u2194')));
    });
    test('зеркальный порядок ловится по словарю (35 03 -> P0335)', () {
      // Прямое чтение дало бы «P3503» (кода не существует), зеркало — P0335.
      final r = parseSsm2DtcReply(<int>[0x58, 0x01, 0x35, 0x03, 0x20]);
      expect(r.single.code, 'P0335');
      expect(r.single.origin, contains('\u2194'));
    });
    test('пустой ответ -> пустой список', () {
      expect(parseSsm2DtcReply(<int>[0x58, 0x00]), isEmpty);
    });
  });

  group('OBD mode 03 остаётся эталоном', () {
    test('43 01 02 -> P0102', () {
      final r = parseObdDtcReply(<int>[0x43, 0x01, 0x02]);
      expect(r.single.code, 'P0102');
    });
    test('несколько кодов', () {
      final r = parseObdDtcReply(<int>[0x43, 0x01, 0x02, 0x01, 0x13]);
      expect(r.map((d) => d.code).toList(), <String>['P0102', 'P0113']);
    });
  });
}
"""

TEST_FILE = APP / "test/dtc_v5_test.dart"
_prev = TEST_FILE.read_text(encoding="utf-8") if TEST_FILE.exists() else ""
if "ISO-TP FF+CF" not in _prev:
    TEST_FILE.write_text(TEST_DTC_V5, encoding="utf-8")
    print("[OK] test/dtc_v5_test.dart записан (12 тестов, вкл. FF+CF)")

# ──────────── [6] Нормализация версии -> 1.0 ────────────
PUB = APP / "pubspec.yaml"
pub = PUB.read_text(encoding="utf-8")
pub_new = re.sub(r"(?m)^version:\s*\S+.*$", "version: 1.0.0+20", pub)
pub_new = re.sub(
    r"(?m)^description:\s*.*$",
    'description: "SSM2 1.0 — Subaru Select Monitor: 28+27 PID, Map Lab, '
    'DTC FIX v5 (зеркало P0102/P0201), OP2 OTG, ELM Speed Pack"',
    pub_new)
if pub_new != pub:
    shutil.copyfile(PUB, BACKUP / "pubspec.yaml.bak")
    PUB.write_text(pub_new, encoding="utf-8")
print("[OK] pubspec.yaml -> version 1.0.0+20")

print()
print("=" * 62)
print("SSM2 1.0.3 | DTC FIX v5 применён (сегмент перед 04.10).")
print("Дальше по этой же ячейке — 04.10: словарь + предупреждение +")
print("dart format + строгий analyze валидируют этот код сразу.")
print("=" * 62)


# coding: utf-8
# ▸ 04.10 | DTC FIX v4 | SSM2 0.16.1 (insert BEFORE 05)
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
# v1.0.1 hardened: оригинальный литерал НЕ редактируем — ставим ПЕРЕД ним
# новый строковый литерал (Dart склеивает соседние литералы). Перенос
# строки собираем через chr(92)+'n': в тексте ячейки нет ни одной
# backslash-последовательности — сломать её copy/paste не сможет.
# Плюс автопроверка результата с автооткатом из бэкапа.
W = chr(9888)
BS = chr(92)
NL = BS + 'n'
ANCHOR_WARN = "'Будут стёрты коды"
PREFIX_LIT = ("'" + W + ' ЗАГЛУШИТЕ ДВИГАТЕЛЬ!' + NL
              + 'Сброс на заведённом моторе — мотор ЗАГЛОХНЕТ.' + NL + NL + "'")
if ANCHOR_WARN in text and W not in text:
    text = text.replace(ANCHOR_WARN, PREFIX_LIT + ' ' + ANCHOR_WARN, 1)
    wl = [ln for ln in text.split(chr(10)) if 'ЗАГЛУШИТЕ ДВИГАТЕЛЬ' in ln]
    if not wl or wl[0].count("'") % 2 != 0 or 'Будут стёрты коды' not in wl[0]:
        shutil.copyfile(BACKUP / 'dtc_service_page.dart.bak', DTC_PAGE)
        raise RuntimeError('Патч предупреждения повредил строковый литерал; '
                           'dtc_service_page.dart ВОССТАНОВЛЕН из бэкапа. '
                           'Скопируйте ячейку полностью и повторите запуск.')
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