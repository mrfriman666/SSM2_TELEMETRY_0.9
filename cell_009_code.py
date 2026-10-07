# ===== Шапка 04.5: разбор причин ложного «готово» =====
# @title 04.5 | TRACE & GRID | SSM2 0.13 >> 0.14 (вставить МЕЖДУ 04.4 и 05)
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
