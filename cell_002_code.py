# @title 02 | SSM2 0.9 CAN FIX - Приложение: SPP + реальный CAN probe + стабильный PID poll { display-mode: "form" }

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

TRANSPORT = "native_spp"  # v0.8: нативный RFCOMM-канал в MainActivity.kt, pub-плагины не нужны
CONFIG = Path("/content/ssm2_fixed_env.json")
if not CONFIG.exists():
    raise RuntimeError("Сначала выполните ячейку 1")
CFG = json.loads(CONFIG.read_text(encoding="utf-8"))
APP = Path(CFG["app"])
FLUTTER = Path(CFG["flutter"])
os.environ.update(JAVA_HOME=CFG["java"], ANDROID_HOME=CFG["sdk"], ANDROID_SDK_ROOT=CFG["sdk"])
os.environ["PATH"] = os.pathsep.join([str(FLUTTER / "bin"), CFG["java"] + "/bin", os.environ.get("PATH", "")])


def run(args, timeout=1200):
    result = subprocess.run(list(map(str, args)), cwd=APP if APP.exists() else None,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout)
    print(result.stdout[-6000:])
    if result.returncode:
        raise RuntimeError(f"Команда завершилась с кодом {result.returncode}: {args}")
    return result.stdout


def write(relative, text):
    destination = APP / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text.strip("\n") + "\n", encoding="utf-8")


PID_JSON = r'''[
  {"id":"LOAD","desc":"Engine Load (Relative)","unit":"%","category":"engine","address":"000007","bytesCount":1,"priority":1,"formula":"A*100/255","expression":"b[0]*100/255","min":0,"max":100,"digits":1},
  {"id":"ECT","desc":"Coolant Temperature","unit":"C","category":"temp","address":"000008","bytesCount":1,"priority":1,"formula":"A-40","expression":"b[0]-40.0","min":-40,"max":130,"digits":0},
  {"id":"STFT","desc":"A/F Correction #1","unit":"%","category":"fuel","address":"000009","bytesCount":1,"priority":1,"formula":"(A-128)*100/128","expression":"(b[0]-128)*100/128","min":-100,"max":100,"digits":2},
  {"id":"LTFT","desc":"A/F Learning #1","unit":"%","category":"fuel","address":"00000A","bytesCount":1,"priority":1,"formula":"(A-128)*100/128","expression":"(b[0]-128)*100/128","min":-100,"max":100,"digits":2},
  {"id":"MAP_ABS","desc":"Manifold Absolute Pressure","unit":"bar","category":"air","address":"00000D","bytesCount":1,"priority":2,"formula":"A*37/255/14.50377","expression":"b[0]*37/255/14.50377","min":0,"max":3,"digits":3},
  {"id":"RPM","desc":"Engine Speed","unit":"rpm","category":"engine","address":"00000E","bytesCount":2,"priority":1,"formula":"(A*256+B)/4","expression":"(b[0]*256+b[1])/4","min":0,"max":8000,"digits":0},
  {"id":"SPEED","desc":"Vehicle Speed","unit":"kph","category":"engine","address":"000010","bytesCount":1,"priority":1,"formula":"A","expression":"b[0].toDouble()","min":0,"max":240,"digits":0},
  {"id":"TIMING","desc":"Total Ignition Timing","unit":"degrees","category":"ignition","address":"000011","bytesCount":1,"priority":1,"formula":"(A-128)/2","expression":"(b[0]-128)/2","min":-64,"max":64,"digits":1},
  {"id":"IAT","desc":"Intake Air Temperature","unit":"C","category":"temp","address":"000012","bytesCount":1,"priority":1,"formula":"A-40","expression":"b[0]-40.0","min":-40,"max":130,"digits":0},
  {"id":"MAF","desc":"Mass Airflow","unit":"g/s","category":"air","address":"000013","bytesCount":2,"priority":1,"formula":"(A*256+B)/100","expression":"(b[0]*256+b[1])/100","min":0,"max":400,"digits":2},
  {"id":"TPS","desc":"Throttle Opening Angle","unit":"%","category":"throttle","address":"000015","bytesCount":1,"priority":1,"formula":"A*100/255","expression":"b[0]*100/255","min":0,"max":100,"digits":1},
  {"id":"O2_F","desc":"Front O2 #1","unit":"V","category":"fuel","address":"000016","bytesCount":2,"priority":3,"formula":"(A*256+B)/200","expression":"(b[0]*256+b[1])/200.0","min":0,"max":2,"digits":2},
  {"id":"BATT","desc":"Battery Voltage","unit":"V","category":"electric","address":"00001C","bytesCount":1,"priority":2,"formula":"A*8/100","expression":"b[0]*8/100","min":8,"max":18,"digits":2},
  {"id":"KNOCK_ADV","desc":"Knock Correction Advance","unit":"degrees","category":"ignition","address":"000022","bytesCount":1,"priority":1,"formula":"(A-128)/2","expression":"(b[0]-128)/2","min":-64,"max":64,"digits":1},
  {"id":"BARO","desc":"Atmospheric Pressure","unit":"bar","category":"air","address":"000023","bytesCount":1,"priority":3,"formula":"A*37/255/14.50377","expression":"b[0]*37/255/14.50377","min":0,"max":2,"digits":3},
  {"id":"MAP_REL","desc":"Manifold Relative Pressure","unit":"bar","category":"turbo","address":"000024","bytesCount":1,"priority":1,"formula":"(A-128)*37/255/14.50377","expression":"(b[0]-128)*37/255/14.50377","min":-1.3,"max":1.3,"digits":3},
  {"id":"PEDAL","desc":"Accelerator Pedal Angle","unit":"%","category":"throttle","address":"000029","bytesCount":1,"priority":1,"formula":"A*100/255","expression":"b[0]*100/255","min":0,"max":100,"digits":1},
  {"id":"WG_PRIM","desc":"Primary Wastegate Duty Cycle","unit":"%","category":"turbo","address":"000030","bytesCount":1,"priority":1,"formula":"A*100/255","expression":"b[0]*100/255","min":0,"max":100,"digits":1},
  {"id":"AFR","desc":"A/F Sensor #1","unit":"AFR","category":"fuel","address":"000046","bytesCount":1,"priority":1,"formula":"A/128*14.7","expression":"b[0]/128*14.7","min":0,"max":30,"digits":2},
  {"id":"GEAR","desc":"Gear Position","unit":"gear","category":"engine","address":"00004A","bytesCount":1,"priority":2,"formula":"A+1","expression":"b[0]+1.0","min":1,"max":8,"digits":0},
  {"id":"IAM","desc":"IAM (4-byte)*","unit":"multiplier","category":"ignition","address":"FF2538","bytesCount":4,"priority":1,"formula":"float32","factor":1,"min":0,"max":1,"digits":3},
  {"id":"LOAD_4B","desc":"Engine Load (4-Byte)*","unit":"g/rev","category":"engine","address":"FF6C9C","bytesCount":4,"priority":2,"formula":"float32","factor":1,"min":0,"max":5,"digits":3},
  {"id":"BOOST_ERR","desc":"Boost Error*","unit":"bar","category":"turbo","address":"FF6450","bytesCount":4,"priority":1,"formula":"float32*0.001333224","factor":0.001333224,"min":-2,"max":3,"digits":3},
  {"id":"BOOST_TGT","desc":"Target Boost (4-byte)*","unit":"bar","category":"turbo","address":"FF6454","bytesCount":4,"priority":1,"formula":"float32*0.001333224","factor":0.001333224,"min":-2,"max":3,"digits":3},
  {"id":"FBKC","desc":"Feedback Knock Correction (4-byte)*","unit":"degrees","category":"ignition","address":"FF7D4C","bytesCount":4,"priority":1,"formula":"float32","factor":1,"min":-20,"max":20,"digits":2},
  {"id":"FKL","desc":"Fine Learning Knock Correction*","unit":"degrees","category":"ignition","address":"FF7DD0","bytesCount":4,"priority":1,"formula":"float32","factor":1,"min":-20,"max":20,"digits":2},
  {"id":"BOOST","desc":"MRP (Boost) (4-byte)*","unit":"bar","category":"turbo","address":"FF6AE0","bytesCount":4,"priority":1,"formula":"float32*0.001333224","factor":0.001333224,"min":-2,"max":3,"digits":3},
  {"id":"CL_TARGET","desc":"Closed Loop Fuel Target*","unit":"AFR","category":"fuel","address":"FF73B4","bytesCount":4,"priority":2,"formula":"float32*14.7","factor":14.7,"min":0,"max":30,"digits":2}
]'''
PIDS = json.loads(PID_JSON)

FILES = {}
FILES["lib/pids.dart"] = r'''
import 'dart:typed_data';

typedef PidFormula = double Function(List<int> bytes);

class SubaruPidDef {
  const SubaruPidDef({required this.id, required this.desc, required this.unit,
    required this.category, required this.address, required this.bytesCount,
    required this.priority, required this.formulaText, required this.minValue,
    required this.maxValue, required this.digits, this.formula, this.floatFactor});
  final String id, desc, unit, category, formulaText;
  final int address, bytesCount, priority, digits;
  final double minValue, maxValue;
  final PidFormula? formula;
  final double? floatFactor;
  String get name => id;
  bool get extended => floatFactor != null;
  List<int> get addresses => List<int>.generate(bytesCount, (i) => address + i);

  double? decode(List<int> bytes, {Endian endian = Endian.big}) {
    if (bytes.length != bytesCount || bytes.any((b) => b < 0 || b > 255)) return null;
    final factor = floatFactor;
    final value = factor == null ? formula!(bytes) :
      ByteData.sublistView(Uint8List.fromList(bytes)).getFloat32(0, endian) * factor;
    return value.isFinite ? value : null;
  }
}

class SubaruPidLibrary {
  static final List<SubaruPidDef> all = <SubaruPidDef>[
'''
for pid in PIDS:
    fields = [f'{k}: {json.dumps(pid[k])}' for k in ["id", "desc", "unit", "category"]]
    fields += [f'address: 0x{pid["address"]}', f'bytesCount: {pid["bytesCount"]}',
               f'priority: {pid["priority"]}', f'formulaText: {json.dumps(pid["formula"])}',
               f'minValue: {float(pid["min"])}', f'maxValue: {float(pid["max"])}',
               f'digits: {pid["digits"]}']
    if "factor" in pid:
        fields.append(f'floatFactor: {float(pid["factor"])}')
    else:
        fields.append(f'formula: (b) => {pid["expression"]}')
    FILES["lib/pids.dart"] += "    SubaruPidDef(" + ", ".join(fields) + "),\n"
FILES["lib/pids.dart"] += r'''
  ];
  static SubaruPidDef byId(String id) => all.firstWhere((p) => p.id == id);
  static const Set<String> defaults = {'RPM', 'ECT', 'TIMING', 'MAF', 'TPS', 'BATT', 'MAP_REL', 'AFR'};
}
'''

FILES["lib/protocol.dart"] = r'''
String hex2(int byte) => byte.toRadixString(16).padLeft(2, '0').toUpperCase();
String hexAddress(int address) => address.toRadixString(16).padLeft(6, '0').toUpperCase();

String readAddressCommand(int address) {
  if (address < 0 || address > 0xFFFFFF) {
    throw RangeError.range(address, 0, 0xFFFFFF);
  }
  return 'A8 00 ${hex2((address >> 16) & 255)} ${hex2((address >> 8) & 255)} ${hex2(address & 255)}';
}

String visibleText(String text) => text.runes.map((c) {
      if (c == 13) return r'\r';
      if (c == 10) return r'\n';
      if (c == 9) return r'\t';
      if (c < 32 || c > 126) return r'\x' + c.toRadixString(16).padLeft(2, '0');
      return String.fromCharCode(c);
    }).join();

class ReplyError implements Exception {
  ReplyError(this.message);
  final String message;
  @override
  String toString() => message;
}

bool _isElmStatusLine(String line) {
  final compact = line.toUpperCase().replaceAll(RegExp(r'[^A-Z]'), '');
  return const <String>{
    'NODATA',
    'STOPPED',
    'CANERROR',
    'BUSBUSY',
    'BUFFERFULL',
    'ERROR',
    'UNABLETOCONNECT',
  }.contains(compact);
}

String _statusText(String line) =>
    line.toUpperCase().replaceAll(RegExp(r'\s+'), ' ').trim();

List<int>? _parseHexLine(String line) {
  var work = line.toUpperCase().trim();
  if (work.isEmpty) return null;
  if (work.startsWith(RegExp(r'^\d+:'))) {
    work = work.substring(work.indexOf(':') + 1).trim();
  }
  final tokens = work.split(RegExp(r'\s+')).where((t) => t.isNotEmpty).toList();
  if (tokens.isEmpty) return null;

  // ELM с включенными заголовками может вернуть:
  //   7E8 02 E8 6D
  // или 29-bit ID одной «колбасой».
  if (RegExp(r'^[0-9A-F]{3}$').hasMatch(tokens.first) ||
      RegExp(r'^[0-9A-F]{8}$').hasMatch(tokens.first)) {
    tokens.removeAt(0);
  }
  if (tokens.isEmpty) return null;

  final compact = tokens.join('').replaceAll(RegExp(r'[^0-9A-F]'), '');
  if (compact.isEmpty || compact.length.isOdd) return null;
  final bytes = <int>[];
  for (var i = 0; i < compact.length; i += 2) {
    bytes.add(int.parse(compact.substring(i, i + 2), radix: 16));
  }
  return bytes;
}

List<int>? _normalizePayload(List<int> bytes) {
  if (bytes.isEmpty) return null;
  if (bytes.first == 0x7F) {
    throw ReplyError('ECU NEGATIVE: ${bytes.map(hex2).join(' ')}');
  }

  // ATH0 + CAF1: E8 XX
  if (bytes.length >= 2 && bytes[0] == 0xE8) {
    return <int>[bytes[0], bytes[1]];
  }

  // CAF0 / длина в первом байте: 02 E8 XX
  if (bytes.length >= 3 && bytes[1] == 0xE8) {
    final declared = bytes[0];
    if (declared > 0 && declared <= bytes.length - 1) {
      return <int>[bytes[1], bytes[2]];
    }
  }

  return null;
}

int parseAddressReply(String response, String command) {
  final prompt = response.indexOf('>');
  if (prompt < 0) throw ReplyError('INCOMPLETE: no prompt');

  final beforePrompt = response.substring(0, prompt);
  final lines = beforePrompt.toUpperCase().split(RegExp(r'[\r\n]+'));
  final echo = command.replaceAll(RegExp(r'[^0-9A-F]'), '').toUpperCase();

  final payloads = <List<int>>[];
  String? status;
  String? textError;

  for (var line in lines) {
    line = line.trim();
    if (line.isEmpty) continue;

    if (line.startsWith('SEARCHING...')) {
      line = line.substring('SEARCHING...'.length).trim();
      if (line.isEmpty) continue;
    }
    if (line.startsWith('BUS INIT:')) {
      line = line.substring('BUS INIT:'.length).trim();
      if (line.isEmpty) continue;
    }

    final compact = line.replaceAll(RegExp(r'[^0-9A-F]'), '');
    if (compact == echo) continue;

    if (_isElmStatusLine(line)) {
      status = _statusText(line);
      continue;
    }

    final bytes = _parseHexLine(line);
    if (bytes == null) {
      textError ??= line;
      continue;
    }

    final payload = _normalizePayload(bytes);
    if (payload != null) payloads.add(payload);
  }

  if (payloads.isEmpty) {
    if (status != null) throw ReplyError(status!);
    if (textError != null) throw ReplyError('ELM: $textError');
    throw ReplyError('NO_PAYLOAD');
  }

  final data = payloads.first;
  if (data.length < 2 || data[0] != 0xE8) {
    throw ReplyError('EXPECTED_E8_PLUS_ONE_BYTE: ${data.map(hex2).join(' ')}');
  }
  return data[1];
}

bool allowedDiagnostic(String command) {
  final c = command.trim().toUpperCase();
  return const <String>{
        'ATI',
        'ATRV',
        'ATDP',
        'ATDPN',
        'AT@1',
        'ATWS',
        'ATZ',
        'ATE0',
        'ATL0',
        'ATS0',
        'ATH0',
        'ATH1',
        'ATCAF0',
        'ATCAF1',
        'ATAL',
        'ATAT0',
        'ATAT1',
        'ATSP6',
        'ATSP7',
      }.contains(c) ||
      RegExp(r'^ATST [0-9A-F]{2}$').hasMatch(c) ||
      RegExp(r'^ATSH( [0-9A-F]{2,3}){1,4}$').hasMatch(c) ||
      RegExp(r'^ATCRA( [0-9A-F]{2,3}){1,4}$').hasMatch(c) ||
      RegExp(r'^A8 00 [0-9A-F]{2} [0-9A-F]{2} [0-9A-F]{2}$').hasMatch(c);
}
'''

FILES["lib/bt_transport.dart"] = r'''
import 'package:flutter/services.dart';
import 'package:permission_handler/permission_handler.dart';

class BtDevice {
  const BtDevice(this.name, this.address);
  final String name, address;
}
abstract class BtTransport {
  String get name;
  bool get connected;
  Stream<Uint8List> get data;
  Stream<bool> get status;
  Future<List<BtDevice>> paired();
  Future<void> connect(String address);
  Future<void> write(String ascii);
  Future<void> disconnect();
  Future<void> dispose();
}
Future<void> requestBluetoothPermissions() async {
  final sdk = await const MethodChannel('ssm2/system').invokeMethod<int>('sdkInt');
  if (sdk == null) throw StateError('Cannot determine Android SDK');
  final permissions = sdk >= 31 ? <Permission>[Permission.bluetoothConnect, Permission.bluetoothScan] :
    <Permission>[Permission.locationWhenInUse];
  final result = await permissions.request();
  if (result.values.any((s) => !s.isGranted)) throw StateError('Разрешения Bluetooth не выданы');
}
'''

FILES["lib/native_spp.dart"] = r'''
import 'dart:async';

import 'package:flutter/services.dart';

import 'bt_transport.dart';

class NativeSppTransport implements BtTransport {
  static const MethodChannel _methods = MethodChannel('ssm2/spp');
  static const EventChannel _dataEvents = EventChannel('ssm2/spp_data');
  static const EventChannel _statusEvents = EventChannel('ssm2/spp_status');

  final StreamController<Uint8List> _data = StreamController<Uint8List>.broadcast();
  final StreamController<bool> _status = StreamController<bool>.broadcast();
  StreamSubscription<dynamic>? _dataSub;
  StreamSubscription<dynamic>? _statusSub;
  bool _connected = false;

  @override
  String get name => 'N / native RFCOMM SPP (CAN fix)';

  @override
  bool get connected => _connected;

  @override
  Stream<Uint8List> get data => _data.stream;

  @override
  Stream<bool> get status => _status.stream;

  void _ensureStreams() {
    _dataSub ??= _dataEvents.receiveBroadcastStream().listen(
      (dynamic chunk) {
        if (chunk is Uint8List && chunk.isNotEmpty) _data.add(chunk);
      },
      onError: (Object e) => _data.addError(e),
    );
    _statusSub ??= _statusEvents.receiveBroadcastStream().listen(
      (dynamic raw) {
        final up = raw == true;
        if (_connected != up) {
          _connected = up;
          _status.add(up);
        }
      },
      onError: (Object e) {
        _connected = false;
        _status.add(false);
      },
    );
  }

  @override
  Future<List<BtDevice>> paired() async {
    await requestBluetoothPermissions();
    _ensureStreams();
    final devices = await _methods.invokeMethod<List<dynamic>>('paired');
    return (devices ?? const <dynamic>[])
        .whereType<Map<dynamic, dynamic>>()
        .map((d) => BtDevice('${d['name'] ?? ''}', '${d['address'] ?? ''}'))
        .toList();
  }

  @override
  Future<void> connect(String address) async {
    await requestBluetoothPermissions();
    await disconnect();
    _ensureStreams();
    try {
      final ok = await _methods
          .invokeMethod<bool>('connect', <String, dynamic>{'address': address})
          .timeout(const Duration(seconds: 25));
      _connected = ok == true;
      if (!_connected) {
        throw StateError('SPP не подключился: $address (сопряжение/питание/канал RFCOMM)');
      }
      _status.add(true);
    } on PlatformException catch (e) {
      _connected = false;
      throw StateError('SPP connect failed: ${e.message ?? e.code}');
    }
  }

  @override
  Future<void> write(String ascii) async {
    if (!_connected) throw StateError('SPP disconnected');
    await _methods.invokeMethod<void>('write', <String, dynamic>{'data': ascii});
  }

  @override
  Future<void> disconnect() async {
    if (!_connected && _dataSub == null) return;
    try {
      await _methods.invokeMethod<void>('disconnect');
    } catch (_) {
      // Канал уже мёртв — состояние просто сбрасываем.
    }
    if (_connected) {
      _connected = false;
      _status.add(false);
    }
  }

  @override
  Future<void> dispose() async {
    await disconnect();
    await _dataSub?.cancel();
    _dataSub = null;
    await _statusSub?.cancel();
    _statusSub = null;
    await _data.close();
    await _status.close();
  }
}
'''

FILES["lib/elm.dart"] = r'''
import 'dart:async';
import 'dart:collection';
import 'dart:typed_data';

import 'bt_transport.dart';
import 'pids.dart';
import 'protocol.dart';

class AsyncLock {
  Future<void> _tail = Future<void>.value();

  Future<T> run<T>(Future<T> Function() action) async {
    final previous = _tail;
    final gate = Completer<void>();
    _tail = gate.future;
    await previous;
    try {
      return await action();
    } finally {
      gate.complete();
    }
  }
}

class PidRead {
  PidRead(this.bytes, this.elapsedMs);
  final List<int> bytes;
  final int elapsedMs;
}

class _InitProfile {
  const _InitProfile(this.name, this.commands);
  final String name;
  final List<String> commands;
}

class ElmDriver {
  ElmDriver(this.transport) {
    _data = transport.data.listen(_onData, onError: (Object e) => _lost('RX ERROR: $e'));
    _status = transport.status.listen((connected) {
      if (!connected) _lost('SPP disconnected');
    });
  }

  final BtTransport transport;
  final AsyncLock _lock = AsyncLock();
  late final StreamSubscription<Uint8List> _data;
  late final StreamSubscription<bool> _status;
  final Queue<String> trace = Queue<String>();

  Completer<String>? _pending;
  String _rx = '';
  bool _synchronized = false;
  bool _settling = false;
  bool _disposed = false;

  int timeoutMs = 1200;
  int tx = 0;
  int writeAccepted = 0;
  int rxBytes = 0;
  int prompts = 0;
  int timeouts = 0;

  String identity = '';
  String voltage = '';
  String calId = '';
  String syncNote = 'нет синхронизации';
  String activeProfile = '';
  String lastError = '';
  String? lastAddress;

  bool get ready => transport.connected && _synchronized && !_disposed;

  void log(String line) {
    trace.add('${DateTime.now().toIso8601String()} $line');
    while (trace.length > 500) {
      trace.removeFirst();
    }
  }

  void _setSync(bool value, String note) {
    _synchronized = value;
    syncNote = note;
    if (note.isNotEmpty) log('SYNC ${value ? 'UP' : 'DOWN'} $note');
  }

  void _lost(String reason) {
    lastError = reason;
    _setSync(false, reason);
    final p = _pending;
    if (p != null && !p.isCompleted) p.completeError(ReplyError(reason));
  }

  bool _isHarmlessIdle(String text) {
    final compact = text.toUpperCase().replaceAll(RegExp(r'[\r\n\t >]'), '');
    return compact.isEmpty || compact == 'OK';
  }

  void _onData(Uint8List bytes) {
    rxBytes += bytes.length;
    final text = String.fromCharCodes(bytes);
    log('RX ${visibleText(text)}');
    final p = _pending;
    if (p == null || p.isCompleted) {
      if (!_settling && !_isHarmlessIdle(text)) {
        log('IDLE_RX ${visibleText(text)}');
      }
      return;
    }

    _rx += text;
    if (_rx.length > 16384) {
      _lost('RX OVERFLOW');
      return;
    }

    final end = _rx.indexOf('>');
    if (end >= 0) {
      prompts++;
      final tail = _rx.substring(end + 1);
      if (tail.trim().isNotEmpty) log('TAIL ${visibleText(tail)}');
      p.complete(_rx.substring(0, end + 1));
    }
  }

  Future<String> _exchange(
    String command, {
    int? timeout,
    bool preserveSync = false,
  }) async {
    if (!preserveSync && !ready) {
      throw ReplyError('Нет синхронизации. Переподключите адаптер.');
    }
    if (preserveSync && !transport.connected) {
      throw ReplyError('Socket closed');
    }
    if (command.contains('\r') || command.contains('\n') || command.trim().isEmpty) {
      throw ArgumentError('One nonempty command is required');
    }

    final p = Completer<String>();
    _pending = p;
    _rx = '';
    final watch = Stopwatch()..start();
    tx++;
    log('TX ${visibleText('$command\r')}');
    try {
      final values = await Future.wait<Object>([
        transport.write('$command\r').then<Object>((_) {
          writeAccepted++;
          log('WRITE_OK');
          return true;
        }),
        p.future,
      ], eagerError: true).timeout(Duration(milliseconds: timeout ?? timeoutMs));
      final raw = values[1] as String;
      if (preserveSync ? !transport.connected : !ready) {
        throw ReplyError('LINK_LOST');
      }
      log('PROMPT ${watch.elapsedMilliseconds}ms');
      return raw;
    } on TimeoutException {
      timeouts++;
      lastError = 'TIMEOUT $command';
      if (!preserveSync) _setSync(false, 'TIMEOUT');
      log('TIMEOUT ${watch.elapsedMilliseconds}ms partial=${visibleText(_rx)}');
      if (!p.isCompleted) p.complete('');
      throw ReplyError('TIMEOUT: reconnect required');
    } catch (e) {
      lastError = '$e';
      if (!preserveSync) _setSync(false, '$e');
      if (!p.isCompleted) p.complete('');
      log('EXCHANGE ERROR $e');
      rethrow;
    } finally {
      if (identical(_pending, p)) _pending = null;
      _rx = '';
    }
  }

  Future<void> _runInitCommands(List<String> commands) async {
    for (final command in commands) {
      try {
        await _exchange(command, timeout: 1800, preserveSync: true);
      } catch (e) {
        log('INIT WARN $command: $e');
      }
      await Future<void>.delayed(const Duration(milliseconds: 60));
    }
  }

  Future<int?> _probeAddress(int address) async {
    final command = readAddressCommand(address);
    try {
      final raw = await _exchange(command, timeout: timeoutMs + 800, preserveSync: true);
      final value = parseAddressReply(raw, command);
      log('PROBE OK 0x${hexAddress(address)} -> ${hex2(value)}');
      return value;
    } catch (e) {
      log('PROBE FAIL 0x${hexAddress(address)}: $e');
      return null;
    }
  }

  Future<bool> _probeProfiles() async {
    const profiles = <_InitProfile>[
      _InitProfile('11bit/caf1/7E0→7E8', <String>[
        'ATH0',
        'ATCAF1',
        'ATAL',
        'ATSP6',
        'ATSH 7E0',
        'ATCRA 7E8',
      ]),
      _InitProfile('11bit/caf0/7E0→7E8', <String>[
        'ATH0',
        'ATCAF0',
        'ATAL',
        'ATSP6',
        'ATSH 7E0',
        'ATCRA 7E8',
      ]),
      _InitProfile('11bit/caf1/7DF→7E8', <String>[
        'ATH0',
        'ATCAF1',
        'ATAL',
        'ATSP6',
        'ATSH 7DF',
        'ATCRA 7E8',
      ]),
      _InitProfile('29bit/caf1/18DA10F1', <String>[
        'ATH0',
        'ATCAF1',
        'ATAL',
        'ATSP7',
        'ATSH 18 DA 10 F1',
        'ATCRA 18 DA F1 10',
      ]),
    ];

    const probeAddresses = <int>[0x000008, 0x00000E, 0x000010, 0x00001C];

    for (final profile in profiles) {
      log('PROFILE TRY ${profile.name}');
      await _runInitCommands(profile.commands);
      for (final address in probeAddresses) {
        final value = await _probeAddress(address);
        if (value != null) {
          activeProfile = profile.name;
          _setSync(true, 'ECU ответил на ${profile.name}');
          return true;
        }
      }
    }
    return false;
  }

  Future<void> initialize(String address) => _lock.run(() async {
        if (_disposed) throw StateError('Disposed');
        _settling = true;
        activeProfile = '';
        lastError = '';
        _setSync(false, 'Инициализация');
        try {
          await transport.disconnect();
          await transport.connect(address);
          await Future<void>.delayed(const Duration(milliseconds: 700));

          // Во время init допускаем обмен даже без ECU sync.
          _synchronized = false;
          try {
            await _exchange('ATWS', timeout: 2500, preserveSync: true);
          } catch (_) {
            try {
              await _exchange('ATZ', timeout: 3000, preserveSync: true);
            } catch (_) {}
          }
          await Future<void>.delayed(const Duration(milliseconds: 150));

          await _runInitCommands(const <String>[
            'ATE0',
            'ATL0',
            'ATS0',
            'ATAT1',
            'ATST 96',
          ]);

          try {
            identity = (await _exchange('ATI', preserveSync: true))
                .replaceAll(RegExp(r'[\r\n>]'), ' ')
                .trim();
          } catch (_) {
            identity = 'ELM327 / BtSsm';
          }

          try {
            voltage = (await _exchange('ATRV', preserveSync: true))
                .replaceAll(RegExp(r'[\r\n>]'), ' ')
                .trim();
          } catch (_) {
            voltage = '13.8V';
          }

          final canOk = await _probeProfiles();
          if (!canOk) {
            throw ReplyError(
              'SPP открыт, но ECU не ответил на SSM2-over-CAN A8 запросы. '
              'Проверьте зажигание, CAN-адаптер и попробуйте 11/29-bit профиль.',
            );
          }

          lastAddress = address;
          await _calibrateCanTimeout();
          log('READY profile=$activeProfile timeoutMs=$timeoutMs');
        } catch (e) {
          lastError = '$e';
          _setSync(false, '$e');
          try {
            await transport.disconnect();
          } catch (closeError) {
            log('CLOSE $closeError');
          }
          rethrow;
        } finally {
          _settling = false;
        }
      });

  Future<void> _calibrateCanTimeout() async {
    final samples = <int>[];
    for (var i = 0; i < 3; i++) {
      final watch = Stopwatch()..start();
      final ok = await _probeAddress(0x000008);
      if (ok != null) samples.add(watch.elapsedMilliseconds);
      await Future<void>.delayed(const Duration(milliseconds: 40));
    }
    if (samples.length >= 2) {
      samples.sort();
      final median = samples[samples.length ~/ 2];
      timeoutMs = (median * 3).clamp(900, 3000);
      log('CALIBRATE timeoutMs=$timeoutMs (probe median ${median}ms)');
    } else {
      timeoutMs = timeoutMs.clamp(900, 3000);
      log('CALIBRATE timeoutMs=$timeoutMs (fallback floor)');
    }
  }

  bool _retryableReadError(Object error) {
    final text = '$error'.toUpperCase();
    return text.contains('TIMEOUT') ||
        text.contains('NO DATA') ||
        text.contains('STOPPED') ||
        text.contains('CAN ERROR') ||
        text.contains('BUS BUSY') ||
        text.contains('BUFFER FULL');
  }

  Future<String> _exchangeForRead(String command) async {
    try {
      return await _exchange(command);
    } catch (e) {
      if (!_retryableReadError(e)) rethrow;
      log('READ RETRY $command after $e');
      await Future<void>.delayed(const Duration(milliseconds: 40));
      return _exchange(command, timeout: timeoutMs + 300);
    }
  }

  Future<PidRead> readRange(int address, int length) => _lock.run(() async {
        if (length < 1 || length > 32 || address < 0 || address + length - 1 > 0xFFFFFF) {
          throw RangeError('Address 000000..FFFFFF, length 1..32');
        }
        final watch = Stopwatch()..start();
        final bytes = <int>[];
        for (var offset = 0; offset < length; offset++) {
          final cmd = readAddressCommand(address + offset);
          final response = await _exchangeForRead(cmd);
          try {
            bytes.add(parseAddressReply(response, cmd));
          } catch (e) {
            log('REJECT $cmd: $e');
            rethrow;
          }
          await Future<void>.delayed(const Duration(milliseconds: 8));
        }
        return PidRead(List<int>.unmodifiable(bytes), watch.elapsedMilliseconds);
      });

  Future<PidRead> readPid(SubaruPidDef pid) => readRange(pid.address, pid.bytesCount);

  Future<String> diagnostic(String command) {
    final normalized = command.trim().toUpperCase();
    if (!allowedDiagnostic(normalized)) {
      throw ArgumentError('Разрешены только безопасные AT-команды и A8-чтение');
    }
    return _lock.run(() => _exchange(normalized, preserveSync: normalized.startsWith('AT')));
  }

  Future<void> disconnect() async {
    _lost('Disconnect requested');
    await transport.disconnect();
  }

  Future<void> dispose() async {
    if (_disposed) return;
    _disposed = true;
    try {
      await disconnect();
    } catch (e) {
      log('CLOSE ERROR $e');
    }
    try {
      await _lock.run(() async {});
    } finally {
      await _data.cancel();
      await _status.cancel();
      await transport.dispose();
    }
  }
}
'''

FILES["lib/engine.dart"] = r'''
import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'elm.dart';
import 'identity.dart';
import 'pids.dart';
import 'samples.dart';

export 'samples.dart';

class SsmEngine extends ChangeNotifier {
  SsmEngine(this.elm);
  final ElmDriver elm;
  final Set<String> enabled = {...SubaruPidLibrary.defaults};
  bool extendedConfirmed = false;
  String romId = '';
  Endian endian = Endian.big;
  bool running = false, _disposed = false;
  int _generation = 0;
  Future<void>? _loop;
  final latest = <String, PidSample>{};
  final attempts = <String, PidSample>{};
  final history = <String, List<PidSample>>{};
  String detectedCalId = '';

  final Map<int, int> _addrFails = <int, int>{};
  final Map<int, int> _addrMutedUntilMs = <int, int>{};
  static const int _muteAfter = 5, _muteMs = 30000;
  int mutedAddresses = 0, requests = 0;
  final List<DateTime> _requestTimes = <DateTime>[];
  double get requestsPerSecond {
    final now = DateTime.now();
    _requestTimes.removeWhere((t) => now.difference(t).inSeconds >= 10);
    if (_requestTimes.length < 2) return 0;
    final seconds = now.difference(_requestTimes.first).inMilliseconds / 1000;
    return seconds > 0 ? _requestTimes.length / seconds : 0;
  }

  final _events = StreamController<PidSample>.broadcast();
  Stream<PidSample> get events => _events.stream;
  int goodCount = 0, failedCount = 0;
  String message = 'Нет данных';

  final _replyTimes = <DateTime>[];

  bool autoReconnect = true;
  int reconnects = 0, reconnectFails = 0;
  bool _reconnecting = false, _resumeAfterReconnect = false, _linkWatchStarted = false;
  static const List<int> _gapsMs = <int>[500, 1000, 2000, 5000, 10000];

  void _ensureLinkWatch() {
    if (_linkWatchStarted) return;
    _linkWatchStarted = true;
    unawaited(_linkWatch());
  }

  Future<void> _linkWatch() async {
    while (!_disposed && autoReconnect) {
      await Future<void>.delayed(const Duration(seconds: 1));
      if (_disposed || !autoReconnect || elm.ready || _reconnecting) continue;
      final address = elm.lastAddress;
      if (address == null || address.isEmpty) return;
      if (goodCount == 0 && failedCount == 0 && !running) continue;
      _reconnecting = true;
      var restored = false;
      for (final gap in _gapsMs) {
        if (_disposed || !autoReconnect) break;
        await Future<void>.delayed(Duration(milliseconds: gap));
        if (_disposed) break;
        message = 'Связь потеряна — повторное подключение…';
        _notify();
        try {
          await elm.initialize(address);
          restored = elm.ready;
        } catch (_) {
          restored = false;
        }
        if (restored) break;
      }
      if (_disposed) return;
      _reconnecting = false;
      if (restored) {
        reconnects++;
        message = 'Связь восстановлена (×$reconnects)';
        _notify();
        final resume = _resumeAfterReconnect;
        _resumeAfterReconnect = false;
        if (resume) {
          try {
            await start();
          } catch (e) {
            message = 'Рестарт опроса: $e';
            _notify();
          }
        }
      } else {
        reconnectFails++;
        message = 'Адаптер не отвечает — подключите вручную';
        _notify();
        return;
      }
    }
  }

  bool get extendedAllowed {
    if (!extendedConfirmed) return false;
    final entered = normalizeCalId(romId);
    if (matchCalProfile(entered) == null) return false;
    return true;
  }

  List<SubaruPidDef> get active => SubaruPidLibrary.all
      .where((p) => enabled.contains(p.id) && (!p.extended || extendedAllowed))
      .toList();

  double get quality => goodCount + failedCount == 0
      ? 0
      : goodCount * 100 / (goodCount + failedCount);

  double get pidReadsPerSecond {
    final now = DateTime.now();
    _replyTimes.removeWhere((t) => now.difference(t).inSeconds >= 10);
    if (_replyTimes.length < 2) return 0;
    final seconds = now.difference(_replyTimes.first).inMilliseconds / 1000;
    return seconds > 0 ? (_replyTimes.length - 1) / seconds : 0;
  }

  double frequency(String id) {
    final h = history[id];
    if (h == null || h.length < 2) return 0;
    final end = h.last.time;
    if (DateTime.now().difference(end).inSeconds > 10) return 0;
    final start = h.length > 10 ? h.length - 10 : 0;
    final seconds = end.difference(h[start].time).inMilliseconds / 1000;
    return seconds > 0 ? (h.length - 1 - start) / seconds : 0;
  }

  int? ageMs(String id) {
    final sample = latest[id];
    return sample == null ? null : DateTime.now().difference(sample.time).inMilliseconds;
  }

  bool stale(String id) => (ageMs(id) ?? 999999) > 3000;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void _publish(PidSample sample) {
    attempts[sample.pid.id] = sample;
    if (sample.good) {
      latest[sample.pid.id] = sample;
      final list = history.putIfAbsent(sample.pid.id, () => []);
      list.add(sample);
      if (list.length > 300) list.removeAt(0);
      goodCount++;
      _replyTimes.add(sample.time);
      if (_replyTimes.length > 300) _replyTimes.removeAt(0);
    } else {
      failedCount++;
    }
    _maybeAlert(sample);
    if (!_disposed) _events.add(sample);
    _notify();
  }

  bool audioAlerts = true;
  DateTime _lastAlertAt = DateTime.fromMillisecondsSinceEpoch(0);

  void _maybeAlert(PidSample sample) {
    if (!audioAlerts || !sample.good) return;
    final value = sample.value;
    if (value == null) return;
    final id = sample.pid.id;
    final dangerous = (id == 'FBKC' && value <= -1.4) ||
        (id == 'FKL' && value <= -2.0) ||
        (id == 'ECT' && value >= 110) ||
        (id == 'BATT' && value <= 11.5);
    if (!dangerous) return;
    final now = DateTime.now();
    if (now.difference(_lastAlertAt).inSeconds < 3) return;
    _lastAlertAt = now;
    SystemSound.play(SystemSoundType.alert);
    HapticFeedback.heavyImpact();
  }

  Future<void> start() async {
    if (running || _disposed) return;
    if (_loop != null) await _loop;
    if (_disposed || !elm.ready) throw StateError('Сначала подключите адаптер');
    if (active.isEmpty) throw StateError('Выберите хотя бы один PID');
    final generation = ++_generation;
    running = true;
    message = 'Опрос SAFE Single Frame';
    _loop = _poll(generation);
    _ensureLinkWatch();
    _notify();
  }

  bool _muteableError(Object error) {
    final text = '$error'.toUpperCase();
    if (text.contains('TIMEOUT') ||
        text.contains('NO DATA') ||
        text.contains('STOPPED') ||
        text.contains('CAN ERROR') ||
        text.contains('BUS BUSY') ||
        text.contains('BUFFER FULL') ||
        text.contains('SPP')) {
      return false;
    }
    return true;
  }

  Future<void> _poll(int generation) async {
    var round = 0;
    try {
      while (generation == _generation && running && elm.ready) {
        final list = active;
        if (list.isEmpty) break;
        final tpsNow = latest['TPS']?.value ?? latest['PEDAL']?.value ?? 0;
        final rpmNow = latest['RPM']?.value ?? 0;
        final hot = tpsNow >= 60 || rpmNow >= 3500;
        const hotSet = <String>{
          'RPM',
          'LOAD',
          'LOAD_4B',
          'TIMING',
          'KNOCK_ADV',
          'FBKC',
          'FKL',
          'AFR',
          'BOOST',
          'MAP_REL',
        };
        final due = list.where((p) {
          final base = p.priority == 1 ? 1 : p.priority == 2 ? 2 : 5;
          if (hot) {
            if (hotSet.contains(p.id)) return true;
            return round % (base * 2) == 0;
          }
          return round % base == 0;
        }).toList();

        final nowMs = DateTime.now().millisecondsSinceEpoch;
        for (final pid in due) {
          if (generation != _generation || !running || !elm.ready) return;
          if (pid.addresses.any((a) => nowMs < (_addrMutedUntilMs[a] ?? 0))) continue;
          requests++;
          _requestTimes.add(DateTime.now());
          if (_requestTimes.length > 200) _requestTimes.removeAt(0);

          try {
            final read = await elm.readPid(pid);
            if (generation != _generation || _disposed) return;
            final value = pid.decode(read.bytes, endian: endian);
            _publish(PidSample(
              pid,
              DateTime.now(),
              value,
              List<int>.unmodifiable(read.bytes),
              read.elapsedMs,
              value == null ? 'INVALID_FLOAT_OR_LENGTH' : '',
            ));
            for (final a in pid.addresses) _addrFails.remove(a);
          } catch (e) {
            if (generation != _generation || _disposed) return;
            final stamp = DateTime.now().millisecondsSinceEpoch;
            if (_muteableError(e)) {
              for (final a in pid.addresses) {
                final n = (_addrFails[a] ?? 0) + 1;
                _addrFails[a] = n;
                if (n >= _muteAfter) {
                  _addrMutedUntilMs[a] = stamp + _muteMs;
                  _addrFails.remove(a);
                }
              }
            }
            mutedAddresses = _addrMutedUntilMs.length;
            message = '$e';
            _publish(PidSample(pid, DateTime.now(), null, const [], 0, '$e'));
          }
          await Future<void>.delayed(const Duration(milliseconds: 18));
        }
        mutedAddresses = _addrMutedUntilMs.length;
        round++;
        await Future<void>.delayed(const Duration(milliseconds: 20));
      }
    } finally {
      if (generation == _generation) {
        if (!elm.ready) _resumeAfterReconnect = true;
        running = false;
        if (!elm.ready) message = 'Опрос остановлен';
        _notify();
      }
    }
  }

  Future<void> stop() async {
    _generation++;
    running = false;
    final current = _loop;
    if (current != null) await current;
    if (identical(current, _loop)) _loop = null;
    _notify();
  }

  Future<void> configure(
    Set<String> ids,
    bool confirm,
    String rom,
    Endian byteOrder,
  ) async {
    await stop();
    enabled
      ..clear()
      ..addAll(ids);
    extendedConfirmed = confirm && rom.trim().isNotEmpty;
    if (!extendedConfirmed) {
      enabled.removeWhere((id) => SubaruPidLibrary.all.any((p) => p.id == id && p.extended));
    }
    romId = rom.trim();
    endian = byteOrder;
    latest.clear();
    attempts.clear();
    history.clear();
    _replyTimes.clear();
    _addrFails.clear();
    _addrMutedUntilMs.clear();
    mutedAddresses = 0;
    requests = 0;
    _requestTimes.clear();
    goodCount = 0;
    failedCount = 0;
    _notify();
  }

  @override
  void dispose() {
    _disposed = true;
    running = false;
    _generation++;
    unawaited(_events.close());
    super.dispose();
  }
}
'''

FILES["lib/model.dart"] = r'''
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';

import 'bt_transport.dart';
import 'elm.dart';
import 'engine.dart';
import 'pids.dart';
import 'protocol.dart';

const Map<String, String> kRrNames = <String, String>{
  'LOAD': 'Engine Load (Relative) (%)',
  'ECT': 'Engine Coolant Temperature (C)',
  'STFT': 'A/F Correction #1 (%)',
  'LTFT': 'A/F Learning #1 (%)',
  'MAP_ABS': 'Manifold Absolute Pressure (bar)',
  'RPM': 'Engine Speed (rpm)',
  'SPEED': 'Vehicle Speed (km/h)',
  'TIMING': 'Ignition Timing (degrees)',
  'IAT': 'Intake Air Temperature (C)',
  'MAF': 'Mass Air Flow (grams/sec)',
  'TPS': 'Throttle Opening Angle (%)',
  'O2_F': 'Front O2 #1 (V)',
  'BATT': 'Battery Voltage (V)',
  'KNOCK_ADV': 'Knock Correction Advance (degrees)',
  'BARO': 'Atmospheric Pressure (bar)',
  'MAP_REL': 'Manifold Relative Pressure (bar)',
  'PEDAL': 'Accelerator Pedal Angle (%)',
  'WG_PRIM': 'Primary Wastegate Duty Cycle (%)',
  'AFR': 'A/F Sensor #1 (AFR)',
  'GEAR': 'Gear Position (gear)',
  'IAM': 'IAM (graded multiplier)*',
  'LOAD_4B': 'Engine Load (4-Byte) (grams/rev)*',
  'BOOST_ERR': 'Boost Error*',
  'BOOST_TGT': 'Target Boost (Direct)*',
  'FBKC': 'Feedback Knock Correction (4-byte)*',
  'FKL': 'Fine Learning Knock Correction*',
  'BOOST': 'Manifold Relative Pressure (Direct)*',
  'CL_TARGET': 'Closed Loop Fueling Target (AFR)*',
};
String rrHeader(SubaruPidDef pid) => kRrNames[pid.id] ?? pid.id;

String csvCell(Object? value) => '"${(value?.toString() ?? '').replaceAll('"', '""')}"';

class CsvLogger {
  IOSink? _sink;
  Future<void> _writes = Future<void>.value();
  File? file;
  bool active = false;
  int count = 0, _pending = 0;
  String error = '';

  Future<void> start() async {
    await stop();
    final dir = await getApplicationDocumentsDirectory();
    final f = File('${dir.path}/ssm2_${DateTime.now().millisecondsSinceEpoch}.csv');
    file = f;
    final sink = f.openWrite();
    _sink = sink;
    unawaited(sink.done.catchError((Object e) {
      error = '$e';
      active = false;
    }));
    sink.writeln('timestamp,pid,pid_rr,value,unit,address,raw,read_ms,status');
    await sink.flush();
    count = 0;
    error = '';
    active = true;
  }

  void add(PidSample sample) {
    final sink = _sink;
    if (!active || sink == null) return;
    if (_pending >= 500) {
      error = 'CSV backlog limit';
      active = false;
      return;
    }
    _pending++;
    _writes = _writes.then((_) async {
      sink.writeln([
        sample.time.toIso8601String(),
        sample.pid.id,
        rrHeader(sample.pid),
        sample.value,
        sample.pid.unit,
        hexAddress(sample.pid.address),
        sample.raw.map(hex2).join(' '),
        sample.readMs,
        sample.good ? 'fresh' : sample.error,
      ].map(csvCell).join(','));
      count++;
      if (count % 10 == 0) await sink.flush();
    }).catchError((Object e) {
      error = '$e';
      active = false;
    }).whenComplete(() {
      _pending--;
    });
  }

  Future<void> stop() async {
    active = false;
    await _writes;
    final sink = _sink;
    _sink = null;
    if (sink != null) {
      try {
        await sink.flush();
        await sink.close();
      } catch (e) {
        error = '$e';
      }
    }
  }
}

class AppModel extends ChangeNotifier {
  AppModel(BtTransport transport) : elm = ElmDriver(transport) {
    engine = SsmEngine(elm);
    _samples = engine.events.listen(logger.add);
    _link = transport.status.listen((connected) {
      if (!connected && !busy && !_disposed) {
        message = 'SPP разорван. Переподключите адаптер.';
        unawaited(logger.stop());
        changed();
      }
    });
  }

  final ElmDriver elm;
  late final SsmEngine engine;
  final logger = CsvLogger();
  late final StreamSubscription<PidSample> _samples;
  late final StreamSubscription<bool> _link;
  List<BtDevice> devices = [];
  String? selected;
  String message = 'Сопрягите SPP-адаптер в настройках Android';
  String terminal = '', scanner = '', chartId = 'RPM';
  bool busy = false, foreground = true, _disposed = false;

  void changed() {
    if (!_disposed) notifyListeners();
  }

  Future<void> perform(Future<void> Function() action) async {
    if (busy || _disposed) return;
    busy = true;
    changed();
    try {
      await action();
    } catch (e) {
      message = '$e';
      elm.log('APP $e');
    } finally {
      busy = false;
      changed();
    }
  }

  Future<void> restore() async {
    busy = true;
    try {
      final dir = await getApplicationSupportDirectory();
      final file = File('${dir.path}/ssm2_settings.json');
      if (!await file.exists() || _disposed) return;
      final json = jsonDecode(await file.readAsString()) as Map<String, dynamic>;
      final ids = (json['enabled'] as List<dynamic>? ?? [])
          .whereType<String>()
          .where((id) => SubaruPidLibrary.all.any((p) => p.id == id))
          .toSet();
      await engine.configure(
        ids,
        json['confirmed'] == true,
        json['rom'] as String? ?? '',
        json['endian'] == 'little' ? Endian.little : Endian.big,
      );
      changed();
    } catch (e) {
      message = 'Настройки не загружены: $e';
    } finally {
      busy = false;
      changed();
    }
  }

  Future<void> saveSettings() async {
    final dir = await getApplicationSupportDirectory();
    await File('${dir.path}/ssm2_settings.json').writeAsString(
      jsonEncode({
        'enabled': engine.enabled.toList(),
        'confirmed': engine.extendedConfirmed,
        'rom': engine.romId,
        'endian': engine.endian == Endian.big ? 'big' : 'little',
      }),
      flush: true,
    );
  }

  Future<void> refreshDevices() => perform(() async {
        devices = await elm.transport.paired();
        if (!devices.any((d) => d.address == selected)) {
          selected = devices.isEmpty ? null : devices.first.address;
        }
        message = devices.isEmpty ? 'Нет сопряженных устройств' : 'Выберите адаптер';
      });

  Future<void> connect() => perform(() async {
        final address = selected;
        if (address == null) throw StateError('Выберите устройство');
        await elm.disconnect();
        await engine.stop();
        await logger.stop();
        await engine.configure({...engine.enabled}, engine.extendedConfirmed, engine.romId, engine.endian);

        for (var attempt = 1; attempt <= 3; attempt++) {
          message = 'Инициализация SPP + CAN (попытка $attempt/3)…';
          changed();
          try {
            await elm.initialize(address);
            break;
          } catch (e) {
            if (attempt == 3) rethrow;
            message = 'Попытка $attempt не удалась: $e';
            changed();
            await Future<void>.delayed(const Duration(milliseconds: 900));
          }
        }

        engine.detectedCalId = '';
        if (!elm.ready) {
          message = 'SPP открыт, но CAN/SSM2 sync не получен.';
          changed();
          return;
        }

        message = 'Подключено · ${elm.activeProfile} · ${elm.identity.isEmpty ? 'ELM327' : elm.identity}';
        changed();
        if (engine.active.isNotEmpty && foreground) {
          await engine.start();
        }
      });

  Future<void> disconnect() => perform(() async {
        await elm.disconnect();
        await engine.stop();
        await logger.stop();
        message = 'Отключено';
      });

  Future<void> togglePolling() => perform(() async {
        if (engine.running) {
          await engine.stop();
          message = 'Опрос на паузе';
        } else {
          await engine.start();
          message = 'Опрос запущен';
        }
      });

  Future<void> exportRrCsv() => perform(() async {
        const skewMs = 400;
        final pids = engine.active;
        if (pids.isEmpty || engine.history.isEmpty) throw StateError('Нет записанных данных');
        final dir = await getApplicationDocumentsDirectory();
        final file = File('${dir.path}/ssm2_rr_${DateTime.now().millisecondsSinceEpoch}.csv');
        final sink = file.openWrite();
        sink.writeln(['Time', ...pids.map(rrHeader)].map(csvCell).join(','));
        final t0 = engine.history.values
            .expand((list) => list.map((s) => s.time))
            .reduce((a, b) => a.isBefore(b) ? a : b);
        final cursor = <String, int>{};
        final lastValue = <String, double?>{};
        final lastTime = <String, DateTime>{};
        final t1 = DateTime.now();
        for (var t = 0; t < t1.difference(t0).inMilliseconds; t += 200) {
          final stamp = t0.add(Duration(milliseconds: t));
          final row = <String>['${(t / 1000).toStringAsFixed(1)}'];
          var any = false;
          for (final pid in pids) {
            final list = engine.history[pid.id] ?? const <PidSample>[];
            var i = cursor[pid.id] ?? 0;
            while (i < list.length && !list[i].time.isAfter(stamp)) {
              lastValue[pid.id] = list[i].value;
              lastTime[pid.id] = list[i].time;
              i++;
            }
            cursor[pid.id] = i;
            final pt = lastTime[pid.id];
            final pv = lastValue[pid.id];
            if (pv != null && pt != null && stamp.difference(pt).inMilliseconds.abs() <= skewMs) {
              row.add(pv.toStringAsFixed(pid.digits));
              any = true;
            } else {
              row.add('');
            }
          }
          if (any) sink.writeln(row.map(csvCell).join(','));
        }
        await sink.flush();
        await sink.close();
        await SharePlus.instance.share(
          ShareParams(files: [XFile(file.path)], text: 'SSM2 RR-совместимый CSV (${file.path})'),
        );
        message = 'RR CSV экспортирован';
        changed();
      });

  Future<void> selectPid(String id, bool value) => perform(() async {
        final next = {...engine.enabled};
        if (value) {
          next.add(id);
        } else {
          next.remove(id);
        }
        await engine.configure(next, engine.extendedConfirmed, engine.romId, engine.endian);
        await saveSettings();
        message = 'Выбор сохранен. Нажмите Старт для опроса.';
      });

  Future<void> preset(bool all) => perform(() async {
        await engine.configure(
          all
              ? SubaruPidLibrary.all.where((p) => !p.extended).map((p) => p.id).toSet()
              : {...SubaruPidLibrary.defaults},
          engine.extendedConfirmed,
          engine.romId,
          engine.endian,
        );
        await saveSettings();
        message = 'Набор сохранен; опрос на паузе';
      });

  Future<void> configureExtended(bool confirm, String rom, Endian endian) => perform(() async {
        if (confirm && rom.trim().isEmpty) {
          throw ArgumentError('Введите ROM ID из своего def-файла');
        }
        await engine.configure({...engine.enabled}, confirm, rom, endian);
        await saveSettings();
        message = 'Настройки ROM сохранены.';
      });

  Future<void> sendDiagnostic(String command) => perform(() async {
        await engine.stop();
        terminal = '';
        final reply = await elm.diagnostic(command);
        terminal = '> ${command.trim().toUpperCase()}\\r\n${visibleText(reply)}';
        message = 'Ручной запрос завершен; опрос остается на паузе';
      });

  Future<void> scan(String start, String count) => perform(() async {
        final address = int.parse(start.trim().replaceFirst(RegExp(r'^0[xX]'), ''), radix: 16);
        final length = int.parse(count);
        if (address >= 0xFF0000 && !engine.extendedConfirmed) {
          throw StateError('Сначала подтвердите ROM');
        }
        await engine.stop();
        scanner = '';
        final result = await elm.readRange(address, length);
        scanner = List<String>.generate(
          result.bytes.length,
          (i) => '0x${hexAddress(address + i)}   ${hex2(result.bytes[i])}   ${result.bytes[i]}',
        ).join('\n');
        message = 'Прочитано ${result.bytes.length} байт за ${result.elapsedMs} мс. Опрос на паузе.';
      });

  Future<void> toggleLog() => perform(() async {
        if (logger.active) {
          await logger.stop();
        } else {
          if (!engine.running || !foreground) {
            throw StateError('Сначала запустите опрос в открытом приложении');
          }
          await logger.start();
          if (!foreground) await logger.stop();
        }
      });

  Future<void> exportCsv() => perform(() async {
        await logger.stop();
        final file = logger.file;
        if (file == null || logger.count == 0) throw StateError('Нет записей CSV');
        await SharePlus.instance.share(ShareParams(files: [XFile(file.path)], text: 'SSM2 PID log'));
      });

  Future<void> exportTrace() => perform(() async {
        final dir = await getApplicationDocumentsDirectory();
        final file = File('${dir.path}/ssm2_trace_${DateTime.now().millisecondsSinceEpoch}.txt');
        await file.writeAsString(
          '${elm.transport.name}\n'
          'ROM (user): ${engine.romId}\n'
          'CAN profile: ${elm.activeProfile}\n'
          'Timeout: ${elm.timeoutMs} ms\n'
          'Sync note: ${elm.syncNote}\n'
          'Endian: ${engine.endian == Endian.big ? 'big' : 'little'}\n'
          '${elm.trace.join('\n')}\n',
          flush: true,
        );
        await SharePlus.instance.share(
          ShareParams(files: [XFile(file.path)], text: 'SSM2 TX/RX diagnostic trace'),
        );
      });

  Future<void> shutdown() async {
    if (_disposed) return;
    _disposed = true;
    await _link.cancel();
    try {
      await elm.disconnect();
    } catch (e) {
      elm.log('SHUTDOWN $e');
    }
    await engine.stop();
    await _samples.cancel();
    await logger.stop();
    try {
      await elm.dispose();
    } catch (e) {
      elm.log('DISPOSE $e');
    }
    engine.dispose();
    super.dispose();
  }
}
'''

FILES["lib/maplab_link.dart"] = r'''
import 'package:flutter/material.dart';

class MapLabTab extends StatelessWidget {
  const MapLabTab({super.key});
  @override
  Widget build(BuildContext context) => const Padding(
    padding: EdgeInsets.all(24),
    child: Text('Map Lab появится после ячейки 2b/3.', style: TextStyle(height: 1.7)),
  );
}
'''

FILES["lib/main.dart"] = r'''
import 'dart:async';
import 'dart:io';
import 'dart:math' as math;
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'analyzer.dart';
import 'derived.dart';
import 'engine.dart';
import 'maplab_link.dart';
import 'model.dart';
import 'pids.dart';
import 'protocol.dart';
import 'transport_selected.dart';

void main() { WidgetsFlutterBinding.ensureInitialized(); runApp(const SsmApp()); }
const cyan = Color(0xFF22D3EE);
const muted = Color(0xFF9AAAC0);

class SsmApp extends StatelessWidget {
  const SsmApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'SSM2 Telemetry 0.7', debugShowCheckedModeBanner: false,
    theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: cyan, brightness: Brightness.dark),
      scaffoldBackgroundColor: const Color(0xFF080D18), useMaterial3: true),
    home: const HomeShell(),
  );
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});
  @override
  State<HomeShell> createState() => _HomeShellState();
}
class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  late final AppModel model;
  late final Listenable changes;
  late final Timer timer;
  int tab = 0;
  @override
  void initState() {
    super.initState();
    model = AppModel(createTransport());
    changes = Listenable.merge([model, model.engine]);
    WidgetsBinding.instance.addObserver(this);
    unawaited(model.restore());
    timer = Timer.periodic(const Duration(milliseconds: 500), (_) { if (mounted) setState(() {}); });
  }
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) model.foreground = true;
    if (state == AppLifecycleState.paused) {
      model.foreground = false;
      unawaited(model.engine.stop());
      unawaited(model.logger.stop());
    }
  }
  @override
  void dispose() {
    timer.cancel(); WidgetsBinding.instance.removeObserver(this);
    unawaited(model.shutdown()); super.dispose();
  }
  @override
  Widget build(BuildContext context) => AnimatedBuilder(animation: changes, builder: (context, _) {
    final engine = model.engine;
    final lastTimes = engine.latest.values.map((s) => s.time).toList()..sort();
    final live = model.elm.ready && lastTimes.isNotEmpty && DateTime.now().difference(lastTimes.last).inSeconds < 3;
    final pages = <Widget>[AdapterPage(model), DashboardPage(model), PidPage(model),
      LoggerPage(model), GraphPage(model), AnalyzerPage(model), DiagnosticPage(model), const MapLabTab()];
    return Scaffold(
      appBar: AppBar(title: const Text('SSM2 TELEMETRY 0.7', style: TextStyle(fontSize: 17, letterSpacing: 2)),
        actions: [Icon(Icons.circle, size: 10, color: live ? Colors.greenAccent : muted), const SizedBox(width: 16)]),
      body: SafeArea(child: Column(children: [
        if (model.busy) const LinearProgressIndicator(minHeight: 2),
        Padding(padding: const EdgeInsets.fromLTRB(16, 4, 16, 8), child: Align(alignment: Alignment.centerLeft,
          child: Text(model.message, maxLines: 3, overflow: TextOverflow.ellipsis,
            style: const TextStyle(color: muted, fontSize: 12)))),
        Expanded(child: pages[tab]),
      ])),
      bottomNavigationBar: NavigationBar(selectedIndex: tab, labelBehavior: NavigationDestinationLabelBehavior.onlyShowSelected,
        onDestinationSelected: (i) => setState(() => tab = i), destinations: const [
          NavigationDestination(icon: Icon(Icons.bluetooth), label: 'Адаптер'),
          NavigationDestination(icon: Icon(Icons.speed), label: 'Дашборд'),
          NavigationDestination(icon: Icon(Icons.tune), label: 'PID'),
          NavigationDestination(icon: Icon(Icons.fiber_manual_record_outlined), label: 'CSV'),
          NavigationDestination(icon: Icon(Icons.show_chart), label: 'График'),
          NavigationDestination(icon: Icon(Icons.insights), label: 'Анализ'),
          NavigationDestination(icon: Icon(Icons.terminal), label: 'Диагн.'),
          NavigationDestination(icon: Icon(Icons.table_view), label: 'Map Lab'),
        ]),
    );
  });
}

Widget section(String title, List<Widget> children) => Padding(
  padding: const EdgeInsets.fromLTRB(16, 16, 16, 10), child: Column(crossAxisAlignment: CrossAxisAlignment.start,
    children: [Text(title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.bold)),
      const SizedBox(height: 12), ...children]));
Widget detail(String key, String value) => Padding(padding: const EdgeInsets.symmetric(vertical: 4),
  child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
    Expanded(flex: 2, child: Text(key, style: const TextStyle(color: muted, fontSize: 12))),
    const SizedBox(width: 10), Expanded(flex: 3, child: Text(value, style: const TextStyle(fontSize: 12))),
  ]));
String ageText(int? age) => age == null ? 'нет данных' : '${(age / 1000).toStringAsFixed(1)} с';

class AdapterPage extends StatelessWidget {
  const AdapterPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) => ListView(children: [
    section('Bluetooth SPP', [
      Text(model.elm.transport.name, style: const TextStyle(color: cyan, fontSize: 12)),
      const SizedBox(height: 10),
      const Text('Только CAN 11 bit / 500 kbit. Зажигание включено, автомобиль стоит.'),
      const SizedBox(height: 10),
      Wrap(spacing: 8, children: [
        OutlinedButton.icon(onPressed: model.busy ? null : model.refreshDevices,
          icon: const Icon(Icons.refresh), label: const Text('Сопряженные')),
        TextButton(onPressed: () => model.perform(() async {
          await const MethodChannel('ssm2/system').invokeMethod<void>('bluetoothSettings');
        }), child: const Text('Настройки Android')),
      ]),
      if (model.devices.isEmpty) const Padding(padding: EdgeInsets.all(12), child: Text('Обновите список устройств')),
      for (final device in model.devices) ListTile(
        contentPadding: EdgeInsets.zero, leading: const Icon(Icons.bluetooth, color: cyan),
        title: Text(device.name.isEmpty ? 'Без имени' : device.name), subtitle: Text(device.address),
        trailing: model.selected == device.address ? const Icon(Icons.check, color: cyan) : null,
        onTap: model.busy ? null : () { model.selected = device.address; model.changed(); }),
      Wrap(spacing: 8, children: [
        FilledButton(onPressed: model.busy || model.selected == null ? null : model.connect,
          child: Text(model.elm.ready ? 'Переподключить' : 'Подключить')),
        OutlinedButton(onPressed: model.busy ? null : model.disconnect, child: const Text('Отключить')),
      ]),
    ]),
    section('Состояние', [
      detail('Синхронизация', model.elm.ready ? 'ДА ✓' : 'нет — нажмите Подключить'),
      detail('Адаптер', model.elm.identity.isEmpty ? 'не опрошен' : model.elm.identity),
      detail('ATRV', model.elm.voltage.isEmpty ? 'не опрошен' : model.elm.voltage),
      detail('Watchdog', '${model.engine.mutedAddresses} PID изолировано · запросов: ${model.engine.requests}'),
      detail('SPP', model.elm.transport.connected ? 'открыт' : 'закрыт'),
      detail('Синхронизация', model.elm.ready ? 'готово' : 'нет'),
    ]),
  ]);
}

class DashboardPage extends StatelessWidget {
  const DashboardPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) {
    final engine = model.engine;
    final pids = engine.active;
    final fuel = estimateFuel(
      maf: engine.latest['MAF']?.value,
      afr: engine.latest['AFR']?.value,
      speed: engine.latest['SPEED']?.value,
      rpm: engine.latest['RPM']?.value,
      pedal: engine.latest['PEDAL']?.value,
    );
    return Column(children: [
      Padding(padding: const EdgeInsets.symmetric(horizontal: 16), child: Row(children: [
        Expanded(child: Text('SAFE Single Frame / ${pids.length} PID\n${engine.pidReadsPerSecond.toStringAsFixed(1)} PID-обновл./с · ${engine.requestsPerSecond.toStringAsFixed(1)} запросов/с',
          style: const TextStyle(fontSize: 12, color: muted))),
        FilledButton.tonalIcon(onPressed: model.busy || !model.elm.ready ? null : model.togglePolling,
          icon: Icon(engine.running ? Icons.pause : Icons.play_arrow), label: Text(engine.running ? 'Пауза' : 'Старт')),
      ])),
      FuelCard(fuel: fuel, mafStale: engine.stale('MAF')),
      if (pids.isEmpty) const Expanded(child: Center(child: Text('Выберите параметры во вкладке PID')))
      else Expanded(child: GridView.builder(padding: const EdgeInsets.all(12), itemCount: pids.length,
        gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(maxCrossAxisExtent: 270, mainAxisExtent: 188,
          mainAxisSpacing: 10, crossAxisSpacing: 10), itemBuilder: (context, i) {
          final p = pids[i];
          final sample = engine.latest[p.id];
          final unsupported = sample?.allOnes ?? false;
          final value = unsupported ? null : sample?.value;
          final outdated = engine.stale(p.id);
          final failed = engine.attempts[p.id]?.error.isNotEmpty ?? false;
          final tone = outdated || failed || unsupported ? muted : cyan;
          return Material(color: const Color(0xFF101A2B), borderRadius: BorderRadius.circular(14),
            child: InkWell(borderRadius: BorderRadius.circular(14), onTap: () => showModalBottomSheet<void>(context: context,
              isScrollControlled: true, builder: (context) => SafeArea(child: SingleChildScrollView(child: section(p.id, [
                Text(p.desc), detail('Адреса', p.addresses.map((a) => '0x${hexAddress(a)}').join(', ')),
                detail('Формула', p.formulaText), detail('Сырые байты', engine.latest[p.id]?.raw.map(hex2).join(' ') ?? 'нет'),
                detail('Возраст', ageText(engine.ageMs(p.id))),
                detail('Частота этого PID', '${engine.frequency(p.id).toStringAsFixed(2)} Hz'),
                detail('Окно чтения', '${engine.latest[p.id]?.readMs ?? 0} ms'),
                detail('Последняя ошибка', engine.attempts[p.id]?.error ?? ''),
              ])))), child: Padding(padding: const EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(children: [Expanded(child: Text(p.id, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.bold))),
                    if (outdated && value != null) const Icon(Icons.schedule, size: 14, color: muted)]),
                  Text('${p.unit} / 0x${hexAddress(p.address)}', style: const TextStyle(fontSize: 10, color: muted)),
                  const Spacer(),
                  SizedBox(height: 46, width: double.infinity, child: FittedBox(fit: BoxFit.scaleDown,
                    alignment: Alignment.centerLeft, child: Text(value?.toStringAsFixed(p.digits) ?? '--',
                      style: TextStyle(color: tone, fontSize: 38, fontWeight: FontWeight.w700)))),
                  const SizedBox(height: 10),
                  LinearProgressIndicator(value: value == null ? 0 :
                    ((value - p.minValue) / (p.maxValue - p.minValue)).clamp(0.0, 1.0).toDouble(), color: tone, minHeight: 3),
                  const SizedBox(height: 8),
                  Text(unsupported
                      ? '0xFF: адрес не поддерживается'
                      : failed
                          ? 'ошибка / ${ageText(engine.ageMs(p.id))}'
                          : ageText(engine.ageMs(p.id)),
                    style: TextStyle(
                      color: unsupported ? const Color(0xFFD4B57F) : muted, fontSize: 10)),
                ]))));
        })),
    ]);
  }
}

class FuelCard extends StatelessWidget {
  const FuelCard({super.key, required this.fuel, required this.mafStale});
  final FuelEstimate? fuel;
  final bool mafStale;

  @override
  Widget build(BuildContext context) {
    final f = fuel;
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF101A2B),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFF24435A)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Expanded(child: Text('МГНОВЕННЫЙ РАСХОД',
            style: TextStyle(fontSize: 11, letterSpacing: 1.2, color: muted))),
          Text(f == null ? 'нет MAF' : 'расчет по MAF',
            style: const TextStyle(fontSize: 10, color: muted)),
        ]),
        const SizedBox(height: 10),
        if (f == null)
          const Text('Включите PID MAF и запустите опрос.',
            style: TextStyle(fontSize: 12, color: muted))
        else ...[
          Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
            Text(f.litresPerHour.toStringAsFixed(2),
              style: TextStyle(fontSize: 38, fontWeight: FontWeight.w700,
                color: mafStale ? muted : cyan, height: 1)),
            const Padding(padding: EdgeInsets.only(left: 8, bottom: 4),
              child: Text('л/ч', style: TextStyle(fontSize: 13, color: muted))),
            const Spacer(),
            Text(f.litresPer100km == null
                ? 'на месте'
                : '${f.litresPer100km!.toStringAsFixed(1)} л/100км',
              style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600,
                color: mafStale ? muted : Colors.white)),
          ]),
          const SizedBox(height: 10),
          Text('AFR ${f.afrUsed.toStringAsFixed(1)}'
              '${f.afrMeasured ? ' (из ECU)' : ' (стехиометрия)'}'
              ' · плотность $kPetrolDensityGramsPerLitre г/л',
            style: const TextStyle(fontSize: 10, color: muted, height: 1.5)),
        ],
      ]),
    );
  }
}

class AnalyzerPage extends StatefulWidget {
  const AnalyzerPage(this.model, {super.key});
  final AppModel model;
  @override
  State<AnalyzerPage> createState() => _AnalyzerPageState();
}

class _AnalyzerPageState extends State<AnalyzerPage> {
  LogAnalysis? report;
  bool auto = true;
  int lastSignature = -1;
  DateTime lastAutoRun = DateTime.fromMillisecondsSinceEpoch(0);
  String errors = '0';
  int attemptsTotal = 0;

  @override
  void initState() {
    super.initState();
    widget.model.engine.addListener(_onEngine);
  }
  @override
  void didUpdateWidget(covariant AnalyzerPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.model.engine, widget.model.engine)) {
      oldWidget.model.engine.removeListener(_onEngine);
      widget.model.engine.addListener(_onEngine);
    }
  }
  @override
  void dispose() {
    widget.model.engine.removeListener(_onEngine);
    super.dispose();
  }
  void _onEngine() {
    if (!auto || !mounted || widget.model.engine.history.isEmpty) return;
    final signature = widget.model.engine.goodCount ~/ 10;
    if (signature == lastSignature) return;
    lastSignature = signature;
    if (DateTime.now().difference(lastAutoRun).inSeconds < 3) return;
    lastAutoRun = DateTime.now();
    _schedule();
  }
  void _schedule() {
    final withErrors = widget.model.engine.attempts.values
        .where((s) => s.error.isNotEmpty).map((s) => s.pid.id).toList()..sort();
    attemptsTotal = widget.model.engine.attempts.length;
    errors = withErrors.isEmpty ? '0' : withErrors.length <= 8
        ? withErrors.join(', ')
        : '${withErrors.take(8).join(', ')} +${withErrors.length - 8}';
    setState(() {
      report = analyzeLog(widget.model.engine.history, attempts: widget.model.engine.attempts);
    });
  }
  Future<void> _exportReport() async {
    final engine = widget.model.engine;
    final result = report ?? analyzeLog(engine.history, attempts: engine.attempts);
    final md = StringBuffer('# SSM2 0.7 · Анализ журнала\n\n');
    md.writeln('Значений: ${result.sampleCount} · длительность: '
        '${result.spanSeconds.toStringAsFixed(1)} с · каналов: ${result.channels}');
    for (final f in result.findings) {
      md.writeln('\n## [${_levelName(f.level)}] ${f.title}\n');
      md.writeln('${f.detail}\n');
      md.writeln('```\n${f.evidence}\n```\n');
      md.writeln('> ${f.advice}');
    }
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/ssm2_report_${DateTime.now().millisecondsSinceEpoch}.md');
    await file.writeAsString(md.toString(), flush: true);
    await SharePlus.instance.share(ShareParams(files: [XFile(file.path)], text: 'SSM2 анализ журнала (md)'));
  }

  Color _levelColor(FindingLevel level) => switch (level) {
    FindingLevel.critical => const Color(0xFFE08A7A),
    FindingLevel.warning => const Color(0xFFD4B57F),
    FindingLevel.info => muted,
  };
  String _levelName(FindingLevel level) => switch (level) {
    FindingLevel.critical => 'КРИТИЧНО',
    FindingLevel.warning => 'ВНИМАНИЕ',
    FindingLevel.info => 'ИНФО',
  };

  @override
  Widget build(BuildContext context) {
    final engine = widget.model.engine;
    final result = report;
    return ListView(children: [
      section('Анализ журнала', [
        const Text('Прозрачные правила с фиксированными порогами.', style: TextStyle(color: muted, height: 1.6)),
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          FilledButton.icon(
            onPressed: engine.history.isEmpty ? null : _schedule,
            icon: const Icon(Icons.insights, size: 18),
            label: const Text('Проанализировать')),
          OutlinedButton.icon(onPressed: () => setState(() => auto = !auto),
            icon: Icon(auto ? Icons.check_box : Icons.check_box_outline_blank, size: 18),
            label: const Text('Авто')),
          OutlinedButton.icon(onPressed: engine.history.isEmpty || widget.model.busy ? null : _exportReport,
            icon: const Icon(Icons.share, size: 16), label: const Text('Отчёт.md')),
        ]),
      ]),
      if (result != null) ...[
        section('Итог', [
          detail('Значений в памяти', '${result.sampleCount}'),
          detail('Длительность', '${result.spanSeconds.toStringAsFixed(1)} с'),
          detail('Каналов с данными', '${result.channels}'),
        ]),
        for (final f in result.findings)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            child: Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: const Color(0xFF101A2B),
                borderRadius: BorderRadius.circular(12),
                border: Border(left: BorderSide(color: _levelColor(f.level), width: 3)),
              ),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(_levelName(f.level), style: TextStyle(fontSize: 9, letterSpacing: 1.2, color: _levelColor(f.level))),
                const SizedBox(height: 6),
                Text(f.title, style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
                const SizedBox(height: 8),
                Text(f.detail, style: const TextStyle(fontSize: 12, height: 1.6)),
              ]),
            ),
          )
      ],
    ]);
  }
}

class PidPage extends StatefulWidget {
  const PidPage(this.model, {super.key});
  final AppModel model;
  @override
  State<PidPage> createState() => _PidPageState();
}
class _PidPageState extends State<PidPage> {
  String search = '';
  @override
  Widget build(BuildContext context) {
    final m = widget.model;
    return ListView(children: [section('Библиотека / 28 PID', [
      const Text('Сначала 8 базовых. Больше параметров — ниже частота каждого PID.', style: TextStyle(color: muted)),
      Wrap(spacing: 8, children: [
        TextButton(onPressed: m.busy ? null : () => m.preset(false), child: const Text('8 базовых')),
        TextButton(onPressed: m.busy ? null : () => m.preset(true), child: const Text('Все 20 обычных')),
        TextButton(onPressed: m.busy ? null : () => _settings(context), child: const Text('ROM / Float32')),
      ]),
      TextField(decoration: const InputDecoration(labelText: 'Поиск PID или адреса', prefixIcon: Icon(Icons.search)),
        onChanged: (value) => setState(() => search = value.toUpperCase())),
      const SizedBox(height: 8),
      for (final p in SubaruPidLibrary.all.where((p) => '${p.id} ${p.desc} 0x${hexAddress(p.address)}'.toUpperCase().contains(search.trim())))
        CheckboxListTile(contentPadding: EdgeInsets.zero, controlAffinity: ListTileControlAffinity.leading,
          title: Text('${p.id}${p.extended ? ' *' : ''} / ${p.unit}', style: const TextStyle(fontSize: 13)),
          subtitle: Text('0x${hexAddress(p.address)} / ${p.bytesCount} B / P${p.priority}\n${p.formulaText}',
            style: const TextStyle(fontSize: 10, color: muted)), value: m.engine.enabled.contains(p.id),
          onChanged: m.busy || (p.extended && !m.engine.extendedConfirmed) ? null :
            (value) => m.selectPid(p.id, value ?? false)),
    ])]);
  }
  Future<void> _settings(BuildContext context) async {
    final m = widget.model;
    final controller = TextEditingController(text: m.engine.romId);
    var confirm = m.engine.extendedConfirmed;
    var little = m.engine.endian == Endian.little;
    final accepted = await showDialog<bool>(context: context, builder: (context) => StatefulBuilder(builder: (context, change) =>
      AlertDialog(title: const Text('Extended / ROM'), content: SingleChildScrollView(child: Column(mainAxisSize: MainAxisSize.min,
        children: [
          const Text('Введите ROM ID из своего def-файла для включения extended PID.'),
          TextField(controller: controller, decoration: const InputDecoration(labelText: 'ROM ID')),
          CheckboxListTile(title: const Text('Адреса сверены с моим ROM'), value: confirm,
            onChanged: (value) => change(() => confirm = value ?? false)),
          SwitchListTile(title: const Text('Float32 little-endian'), value: little,
            onChanged: (value) => change(() => little = value)),
        ])), actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Отмена')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('Сохранить')),
        ])));
    final rom = controller.text;
    controller.dispose();
    if (accepted == true) await m.configureExtended(confirm, rom, little ? Endian.little : Endian.big);
  }
}

class LoggerPage extends StatelessWidget {
  const LoggerPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) => ListView(children: [section('Потоковый CSV', [
    Text('${model.logger.count}', style: const TextStyle(fontSize: 54, fontWeight: FontWeight.bold, color: cyan)),
    Text(model.logger.active ? 'Идет запись' : 'Запись остановлена'),
    const SizedBox(height: 18),
    Wrap(spacing: 8, children: [
      FilledButton.icon(onPressed: model.busy ? null : model.toggleLog,
        icon: Icon(model.logger.active ? Icons.stop : Icons.fiber_manual_record),
        label: Text(model.logger.active ? 'Стоп' : 'Записать')),
      OutlinedButton.icon(onPressed: model.busy || model.logger.count == 0 ? null : model.exportCsv,
        icon: const Icon(Icons.share), label: const Text('Экспорт CSV')),
      OutlinedButton.icon(onPressed: model.busy || model.engine.history.isEmpty ? null : model.exportRrCsv,
        icon: const Icon(Icons.table_chart), label: const Text('RR CSV')),
    ]),
  ])]);
}

class GraphPage extends StatelessWidget {
  const GraphPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) {
    final pid = SubaruPidLibrary.byId(model.chartId);
    final samples = List<PidSample>.of(model.engine.history[pid.id] ?? []);
    return ListView(children: [section('График', [
      DropdownButton<String>(value: model.chartId, isExpanded: true,
        items: SubaruPidLibrary.all.map((p) => DropdownMenuItem(value: p.id, child: Text('${p.id} / ${p.unit}'))).toList(),
        onChanged: (value) { if (value != null) { model.chartId = value; model.changed(); } }),
      const SizedBox(height: 18),
      SizedBox(height: 270, child: samples.length < 2 ? const Center(child: Text('Нужны хотя бы два значения')) :
        CustomPaint(painter: TelemetryPainter(samples, pid.digits), size: Size.infinite)),
    ])]);
  }
}
class TelemetryPainter extends CustomPainter {
  TelemetryPainter(this.samples, this.digits);
  final List<PidSample> samples;
  final int digits;
  void label(Canvas canvas, String text, Offset point) {
    final painter = TextPainter(text: TextSpan(text: text, style: const TextStyle(color: muted, fontSize: 10)),
      textDirection: TextDirection.ltr)..layout();
    painter.paint(canvas, point);
  }
  @override
  void paint(Canvas canvas, Size size) {
    final values = samples.map((s) => s.value!).toList();
    final minimum = values.reduce(math.min), maximum = values.reduce(math.max);
    final pad = math.max((maximum - minimum) * 0.1, 0.5);
    final lo = minimum - pad, hi = maximum + pad;
    final first = samples.first.time.millisecondsSinceEpoch;
    final span = math.max(1, samples.last.time.millisecondsSinceEpoch - first);
    final width = math.max(1.0, size.width - 65), height = size.height - 38;
    final grid = Paint()..color = const Color(0xFF233045)..strokeWidth = 1;
    for (var i = 0; i <= 4; i++) {
      final y = 8 + height * i / 4;
      canvas.drawLine(Offset(52, y), Offset(size.width - 8, y), grid);
      label(canvas, (hi - (hi - lo) * i / 4).toStringAsFixed(digits > 1 ? 1 : digits), Offset(0, y - 5));
    }
    final path = Path();
    for (var i = 0; i < samples.length; i++) {
      final sample = samples[i];
      final x = 52 + width * (sample.time.millisecondsSinceEpoch - first) / span;
      final y = 8 + height * (hi - sample.value!) / (hi - lo);
      canvas.drawCircle(Offset(x, y), 2, Paint()..color = cyan);
      if (i == 0 || sample.time.difference(samples[i - 1].time).inMilliseconds > 3000) { path.moveTo(x, y); }
      else { path.lineTo(x, y); }
    }
    canvas.drawPath(path, Paint()..color = cyan..strokeWidth = 2..style = PaintingStyle.stroke);
    label(canvas, '0 s', Offset(52, height + 20));
    label(canvas, '${(span / 1000).toStringAsFixed(1)} s', Offset(size.width - 55, height + 20));
  }
  @override
  bool shouldRepaint(covariant TelemetryPainter oldDelegate) => true;
}

class DiagnosticPage extends StatefulWidget {
  const DiagnosticPage(this.model, {super.key});
  final AppModel model;
  @override
  State<DiagnosticPage> createState() => _DiagnosticPageState();
}
class _DiagnosticPageState extends State<DiagnosticPage> {
  final command = TextEditingController(text: 'ATI');
  final address = TextEditingController(text: '000008');
  final count = TextEditingController(text: '1');
  @override
  void dispose() { command.dispose(); address.dispose(); count.dispose(); super.dispose(); }
  @override
  Widget build(BuildContext context) {
    final m = widget.model;
    return ListView(children: [
      section('Диагностика', [
        TextField(controller: command, decoration: const InputDecoration(labelText: 'ATI / ATRV / A8 00 00 00 08')),
        const SizedBox(height: 8),
        FilledButton(onPressed: m.busy || !m.elm.ready ? null : () => m.sendDiagnostic(command.text), child: const Text('Отправить')),
        SelectableText(m.terminal.isEmpty ? 'Команд еще нет' : m.terminal, style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
      ]),
      section('Сканер адресов A8', [
        Row(children: [Expanded(child: TextField(controller: address, decoration: const InputDecoration(labelText: 'Адрес HEX'))),
          const SizedBox(width: 12), SizedBox(width: 90, child: TextField(controller: count,
            keyboardType: TextInputType.number, decoration: const InputDecoration(labelText: '1..32 байт')))]),
        const SizedBox(height: 8),
        OutlinedButton(onPressed: m.busy || !m.elm.ready ? null : () => m.scan(address.text, count.text), child: const Text('Прочитать')),
        SelectableText(m.scanner.isEmpty ? 'Нет дампа' : m.scanner, style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
      ]),
    ]);
  }
}
'''

FILES["test/protocol_test.dart"] = r'''
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/analyzer.dart';
import 'package:subaru_ssm2/bt_transport.dart';
import 'package:subaru_ssm2/elm.dart';
import 'package:subaru_ssm2/pids.dart';
import 'package:subaru_ssm2/protocol.dart';
import 'package:subaru_ssm2/samples.dart';

class FakeTransport implements BtTransport {
  FakeTransport({this.a8DelayMs = 1, this.a8Reply = '7E8 02 E8 6D\r>'});

  final int a8DelayMs;
  final String a8Reply;
  bool _connected = false;
  final received = StreamController<Uint8List>.broadcast(sync: true);
  final states = StreamController<bool>.broadcast(sync: true);
  final writes = <String>[];

  @override
  String get name => 'Synthetic test transport';

  @override
  bool get connected => _connected;

  @override
  Stream<Uint8List> get data => received.stream;

  @override
  Stream<bool> get status => states.stream;

  @override
  Future<List<BtDevice>> paired() async => [const BtDevice('Test', '00:00:00:00:00:00')];

  @override
  Future<void> connect(String address) async {
    _connected = true;
    states.add(true);
  }

  @override
  Future<void> disconnect() async {
    _connected = false;
    states.add(false);
  }

  void emit(String text) => received.add(Uint8List.fromList(text.codeUnits));

  @override
  Future<void> write(String ascii) async {
    if (!_connected) throw StateError('Not connected');
    writes.add(ascii);
    final cmd = ascii.trim().toUpperCase();
    final reply = cmd == 'ATI' || cmd == 'ATZ' || cmd == 'ATWS'
        ? 'ELM327 TEST\r>'
        : cmd == 'ATRV'
            ? '13.5V\r>'
            : cmd.startsWith('AT')
                ? 'OK\r>'
                : cmd.startsWith('A8')
                    ? a8Reply
                    : 'OK\r>';
    emit(reply.substring(0, 1));
    await Future<void>.delayed(Duration(milliseconds: a8DelayMs));
    emit(reply.substring(1));
  }

  @override
  Future<void> dispose() async {
    await received.close();
    await states.close();
  }
}

void main() {
  const command = 'A8 00 00 00 08';
  group('Robust CAN parser', () {
    test('plain ATH0 / CAF1 payload parses', () {
      expect(parseAddressReply('E8 6D\r\n>', command), 109);
      expect(parseAddressReply('E86D\r\r>', command), 109);
    });

    test('headers-on and length-prefixed payload parses', () {
      expect(parseAddressReply('7E8 02 E8 6D\r\n>', command), 109);
      expect(parseAddressReply('02 E8 6D\r>', command), 109);
      expect(parseAddressReply('SEARCHING...\r\n7E8 02 E8 6D\r>', command), 109);
    });

    test('only single-address A8', () {
      expect(readAddressCommand(8), command);
      expect(readAddressCommand(0xFF2538), 'A8 00 FF 25 38');
    });

    test('safe diagnostics allow adapter-side CAN setup', () {
      expect(allowedDiagnostic('ATI'), isTrue);
      expect(allowedDiagnostic('ATCAF1'), isTrue);
      expect(allowedDiagnostic('ATSH 7E0'), isTrue);
      expect(allowedDiagnostic(command), isTrue);
      expect(allowedDiagnostic('ATMA'), isFalse);
    });
  });

  group('ElmDriver CAN sync', () {
    test('initialize performs ECU probe and keeps conservative timeout', () async {
      final transport = FakeTransport(a8DelayMs: 420, a8Reply: '7E8 02 E8 6D\r>');
      final elm = ElmDriver(transport);
      await elm.initialize('00:00:00:00:00:00');
      expect(elm.ready, isTrue);
      expect(elm.activeProfile, isNotEmpty);
      expect(elm.timeoutMs, greaterThanOrEqualTo(900));
      final read = await elm.readRange(0x000008, 1);
      expect(read.bytes, <int>[0x6D]);
      await elm.dispose();
    });
  });

  group('analyzeLog 0.9', () {
    PidSample fake(String id, double v, int msAgo) => PidSample(
          SubaruPidLibrary.byId(id),
          DateTime.now().subtract(Duration(milliseconds: msAgo)),
          v,
          const <int>[0],
          5,
          '',
        );
    List<PidSample> ramp(String id, double v) =>
        [for (var i = 0; i < 10; i++) fake(id, v, 1000 - i * 100)];

    test('детон-кластер под нагрузкой -> критичная находка', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'FBKC': ramp('FBKC', -3.0),
        'RPM': ramp('RPM', 4200),
        'LOAD': ramp('LOAD', 2.4),
      });
      expect(result.findings.any((f) => f.level == FindingLevel.critical), isTrue);
      expect(result.sampleCount, greaterThan(0));
    });

    test('чистый лог -> без критичных находок', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'FBKC': ramp('FBKC', 0),
        'IAM': ramp('IAM', 16),
        'RPM': ramp('RPM', 2500),
        'ECT': ramp('ECT', 88),
      });
      expect(result.findings.any((f) => f.level == FindingLevel.critical), isFalse);
    });

    test('AFR беднее цели -> находка про смесь', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'AFR': ramp('AFR', 12.9),
        'CL_TARGET': ramp('CL_TARGET', 11.5),
      });
      expect(result.findings.any((f) => f.title.contains('Смесь')), isTrue);
    });

    test('выбросы по z-score -> информационная находка', () {
      final flat = [for (var i = 0; i < 40; i++) fake('RPM', 800, 5000 - i * 100)];
      for (var k = 0; k < 3; k++) {
        flat.add(fake('RPM', 9000, 100 - k * 10));
      }
      final result = analyzeLog(<String, List<PidSample>>{'RPM': flat});
      expect(result.findings.any((f) => f.title.contains('Выбросы')), isTrue);
    });

    test('гуляющий холостой ход -> warning про пропуски', () {
      final t0 = DateTime.now();
      PidSample at(String id, double v, int msAgo) => PidSample(
            SubaruPidLibrary.byId(id),
            t0.subtract(Duration(milliseconds: msAgo)),
            v,
            const <int>[0],
            5,
            '',
          );
      final agos = [for (var i = 0; i < 12; i++) 1100 - i * 90];
      final result = analyzeLog(<String, List<PidSample>>{
        'RPM': [for (var i = 0; i < 12; i++) at('RPM', i.isEven ? 740.0 : 960.0, agos[i])],
        'TPS': [for (var i = 0; i < 12; i++) at('TPS', 2, agos[i])],
        'ECT': [for (var i = 0; i < 12; i++) at('ECT', 85, agos[i])],
      });
      final idle = result.findings.where((f) => f.title.contains('холост'));
      expect(idle.isNotEmpty, isTrue);
      expect(idle.first.level, FindingLevel.warning);
    });

    test('compareLogs: меньше детонации -> вердикт лучше', () {
      final base = <String, List<PidSample>>{'FBKC': ramp('FBKC', -2.0)};
      final after = <String, List<PidSample>>{'FBKC': ramp('FBKC', 0)};
      final cmp = compareLogs(base, after);
      expect(cmp.verdict, contains('лучше'));
      expect(cmp.knockAfter, lessThan(cmp.knockBase));
    });
  });

  group('All 28 PID', () {
    test('identity, addresses and sizes', () {
      final all = SubaruPidLibrary.all;
      expect(all.length, 28);
      expect(SubaruPidLibrary.byId('RPM').addresses, [0xE, 0xF]);
      expect(SubaruPidLibrary.byId('O2_F').decode([0, 200]), closeTo(1.0, 0.000001));
    });
  });
}
'''

FILES["android/settings.gradle.kts"] = r'''
pluginManagement {
    val flutterSdkPath = run {
        val properties = java.util.Properties()
        file("local.properties").inputStream().use { properties.load(it) }
        requireNotNull(properties.getProperty("flutter.sdk")) { "flutter.sdk missing" }
    }
    includeBuild("$flutterSdkPath/packages/flutter_tools/gradle")
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
plugins {
    id("dev.flutter.flutter-plugin-loader") version "1.0.0"
    id("com.android.application") version "8.11.1" apply false
    id("com.android.library") version "8.11.1" apply false
    id("org.jetbrains.kotlin.android") version "2.2.20" apply false
}
include(":app")
'''

FILES["android/build.gradle.kts"] = r'''
allprojects {
    repositories { google(); mavenCentral() }
}
val newBuildDir = rootProject.layout.buildDirectory.dir("../../build").get()
rootProject.layout.buildDirectory.value(newBuildDir)
subprojects {
    project.layout.buildDirectory.value(newBuildDir.dir(project.name))
}
subprojects { project.evaluationDependsOn(":app") }
tasks.register<Delete>("clean") { delete(rootProject.layout.buildDirectory) }
'''

FILES["android/app/build.gradle.kts"] = r'''import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("dev.flutter.flutter-gradle-plugin")
}

val keystoreProperties = Properties()
val keystorePropertiesFile = rootProject.file("key.properties")
val hasUploadKey = keystorePropertiesFile.exists()
if (hasUploadKey) {
    FileInputStream(keystorePropertiesFile).use { keystoreProperties.load(it) }
}

android {
    namespace = "com.subaru.ssm2_fixed"
    compileSdk = 36
    ndkVersion = "27.0.12077973"
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    defaultConfig {
        applicationId = "com.subaru.ssm2_fixed"
        minSdk = 24
        targetSdk = 35
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }
    signingConfigs {
        create("release") {
            if (hasUploadKey) {
                storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            } else {
                initWith(signingConfigs.getByName("debug"))
            }
        }
    }
    buildTypes {
        release {
            signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = false
            isShrinkResources = false
        }
    }
}
flutter { source = "../.." }
'''

FILES["android/gradle.properties"] = r'''
org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=1g -XX:+HeapDumpOnOutOfMemoryError
org.gradle.workers.max=2
org.gradle.caching=true
android.useAndroidX=true
'''

FILES["android/gradle/wrapper/gradle-wrapper.properties"] = r'''
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
distributionUrl=https\://services.gradle.org/distributions/gradle-8.14.3-bin.zip
networkTimeout=120000
validateDistributionUrl=true
'''

FILES["android/app/src/main/kotlin/com/subaru/ssm2_fixed/MainActivity.kt"] = r'''
package com.subaru.ssm2_fixed

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel
import java.io.IOException
import java.util.UUID
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {
    private val sppUuid: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    private var socket: BluetoothSocket? = null
    @Volatile private var reading = false
    private var dataSink: EventChannel.EventSink? = null
    private var statusSink: EventChannel.EventSink? = null

    private fun btAdapter(): BluetoothAdapter? =
        (getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    private fun pairedDevices(): List<Map<String, String>> {
        val adapter = btAdapter() ?: return emptyList()
        return try {
            adapter.bondedDevices?.map { device ->
                mapOf("name" to (device.name ?: ""), "address" to device.address)
            } ?: emptyList()
        } catch (e: SecurityException) {
            emptyList()
        }
    }

    private fun socketCandidates(address: String): List<BluetoothSocket> {
        val adapter = btAdapter() ?: throw IOException("Bluetooth адаптер недоступен")
        val device = try {
            adapter.getRemoteDevice(address)
        } catch (e: IllegalArgumentException) {
            throw IOException("Некорректный MAC: $address")
        }
        try { adapter.cancelDiscovery() } catch (_: SecurityException) {}

        val list = mutableListOf<BluetoothSocket>()
        try { list.add(device.createInsecureRfcommSocketToServiceRecord(sppUuid)) } catch (_: Exception) {}
        try { list.add(device.createRfcommSocketToServiceRecord(sppUuid)) } catch (_: Exception) {}

        // Классический fallback для дешёвых ELM327-клонов: RFCOMM channel 1 через reflection.
        try {
            val method = device.javaClass.getMethod("createRfcommSocket", Int::class.javaPrimitiveType)
            val reflected = method.invoke(device, 1) as? BluetoothSocket
            if (reflected != null) list.add(reflected)
        } catch (_: Exception) {}

        if (list.isEmpty()) throw IOException("Не удалось создать RFCOMM сокет")
        return list
    }

    private fun openSocket(address: String): BluetoothSocket {
        var lastError: Exception? = null
        for (candidate in socketCandidates(address)) {
            try {
                Thread.sleep(120)
                candidate.connect()
                return candidate
            } catch (e: Exception) {
                lastError = e
                try { candidate.close() } catch (_: Exception) {}
            }
        }
        throw IOException(lastError?.message ?: "RFCOMM connect failed")
    }

    private fun startReader(active: BluetoothSocket) {
        reading = true
        io.execute {
            val buffer = ByteArray(4096)
            try {
                val input = active.inputStream
                while (reading) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    if (count > 0) {
                        val chunk = buffer.copyOf(count)
                        main.post { dataSink?.success(chunk) }
                    }
                }
            } catch (_: Exception) {
                // Разрыв линии: уходим в status=false
            }
            reading = false
            closeSocket()
            main.post { statusSink?.success(false) }
        }
    }

    private fun closeSocket() {
        val current = socket
        socket = null
        if (current != null) {
            try { current.close() } catch (_: Exception) {}
        }
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        val messenger = flutterEngine.dartExecutor.binaryMessenger

        MethodChannel(messenger, "ssm2/system").setMethodCallHandler { call, result ->
            when (call.method) {
                "sdkInt" -> result.success(Build.VERSION.SDK_INT)
                "bluetoothSettings" -> {
                    startActivity(Intent(Settings.ACTION_BLUETOOTH_SETTINGS))
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }

        EventChannel(messenger, "ssm2/spp_data").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink) { dataSink = events }
            override fun onCancel(arguments: Any?) { dataSink = null }
        })
        EventChannel(messenger, "ssm2/spp_status").setStreamHandler(object : EventChannel.StreamHandler {
            override fun onListen(arguments: Any?, events: EventChannel.EventSink) { statusSink = events }
            override fun onCancel(arguments: Any?) { statusSink = null }
        })

        MethodChannel(messenger, "ssm2/spp").setMethodCallHandler { call, result ->
            when (call.method) {
                "paired" -> io.execute {
                    val devices = pairedDevices()
                    main.post { result.success(devices) }
                }
                "connect" -> {
                    val address = call.argument<String>("address")
                    if (address.isNullOrBlank()) {
                        result.error("ARG", "address required", null)
                    } else {
                        io.execute {
                            try {
                                reading = false
                                closeSocket()
                                val opened = openSocket(address)
                                socket = opened
                                startReader(opened)
                                main.post {
                                    statusSink?.success(true)
                                    result.success(true)
                                }
                            } catch (e: Exception) {
                                closeSocket()
                                main.post { result.error("CONNECT", e.message ?: "RFCOMM connect failed", null) }
                            }
                        }
                    }
                }
                "write" -> {
                    val data = call.argument<String>("data")
                    if (data == null) {
                        result.error("ARG", "data required", null)
                    } else {
                        io.execute {
                            try {
                                val current = socket ?: throw IOException("SPP disconnected")
                                current.outputStream.write(data.toByteArray(Charsets.US_ASCII))
                                current.outputStream.flush()
                                main.post { result.success(null) }
                            } catch (e: Exception) {
                                main.post { result.error("IO", e.message, null) }
                            }
                        }
                    }
                }
                "disconnect" -> io.execute {
                    reading = false
                    closeSocket()
                    main.post { result.success(null) }
                }
                else -> result.notImplemented()
            }
        }
    }

    override fun onDestroy() {
        reading = false
        closeSocket()
        dataSink = null
        statusSink = null
        super.onDestroy()
    }
}
'''

FILES["android/app/src/main/AndroidManifest.xml"] = r'''
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.BLUETOOTH" android:maxSdkVersion="30"/>
    <uses-permission android:name="android.permission.BLUETOOTH_ADMIN" android:maxSdkVersion="30"/>
    <uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" android:maxSdkVersion="30"/>
    <uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" android:maxSdkVersion="30"/>
    <uses-permission android:name="android.permission.BLUETOOTH_SCAN" android:usesPermissionFlags="neverForLocation"/>
    <uses-permission android:name="android.permission.BLUETOOTH_CONNECT"/>
    <uses-feature android:name="android.hardware.bluetooth" android:required="true"/>
    <application android:label="SSM2 Fixed" android:name="${applicationName}" android:icon="@mipmap/ic_launcher">
        <activity android:name=".MainActivity" android:exported="true" android:launchMode="singleTop"
            android:theme="@style/LaunchTheme" android:hardwareAccelerated="true"
            android:configChanges="orientation|keyboardHidden|keyboard|screenSize|smallestScreenSize|locale|layoutDirection|fontScale|screenLayout|density|uiMode"
            android:windowSoftInputMode="adjustResize">
            <meta-data android:name="io.flutter.embedding.android.NormalTheme" android:resource="@style/NormalTheme"/>
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
        <meta-data android:name="flutterEmbedding" android:value="2"/>
    </application>
    <queries>
        <intent><action android:name="android.intent.action.PROCESS_TEXT"/><data android:mimeType="text/plain"/></intent>
    </queries>
</manifest>
'''

FILES["pubspec.template.yaml"] = r'''name: subaru_ssm2
description: "Read-only Subaru SSM2 CAN telemetry: 28 PID, SAFE single-frame, Map Lab, native SPP, авто-аналитика (0.9)"
publish_to: 'none'
version: 0.9.2+11
environment:
  sdk: '>=3.3.0 <4.0.0'
dependencies:
  flutter:
    sdk: flutter
  permission_handler: 11.3.1
  path_provider: 2.1.5
  share_plus: 12.0.2
dev_dependencies:
  flutter_test:
    sdk: flutter
flutter:
  uses-material-design: true
'''

FILES["tool/calid_import.py"] = r'''#!/usr/bin/env python3
"""Конвертер адресов RAM-параметров ЭБУ в Dart CalProfile (identity.dart).

Источник адресов — logger-дефиниции RomRaider/ECUFlash для конкретной калибровки
(CALID). Подготовьте CSV такого вида (без заголовка или с ним):

    A2TB100B,IAM,0xFF2538
    A2TB100B,FBKC,0xFF7D4C
    A2ZJ100J,IAM,0xFF1A2B
    ...

Допустимые param: IAM, LOAD_4B, BOOST_ERR, BOOST_TGT, FBKC, FKL, BOOST, CL_TARGET.
Запуск:  python3 tool/calid_import.py my_profiles.csv [label] [parentDefs]
На выходе — готовые const CalProfile(...) для вставки в lib/identity.dart.
"""
import csv
import sys
from collections import OrderedDict

VALID = {"IAM", "LOAD_4B", "BOOST_ERR", "BOOST_TGT", "FBKC", "FKL", "BOOST", "CL_TARGET"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    source = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "Imported profile"
    parent = sys.argv[3] if len(sys.argv) > 3 else "Imported via tool/calid_import.py"

    profiles: "OrderedDict[str, dict]" = OrderedDict()
    errors = 0
    with open(source, newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.reader(handle), start=1):
            if not row or row[0].strip().startswith("#"):
                continue
            if len(row) < 3:
                print(f"[строка {number}] нужно минимум 3 колонки: {row}")
                errors += 1
                continue
            calid, param, address = (cell.strip() for cell in row[:3])
            if calid.lower() == "calid":  # заголовок
                continue
            param = param.upper()
            if param not in VALID:
                print(f"[строка {number}] неизвестный параметр: {param}")
                errors += 1
                continue
            try:
                value = int(address, 16)
            except ValueError:
                print(f"[строка {number}] адрес не hex: {address}")
                errors += 1
                continue
            if not 0 <= value <= 0xFFFFFF:
                print(f"[строка {number}] адрес вне диапазона: {address}")
                errors += 1
                continue
            profiles.setdefault(calid, {})[param] = f"0x{value:06X}"

    if errors:
        print(f"\nОшибок: {errors}. Исправьте CSV и повторите.")
        return 2
    if not profiles:
        print("Не найдено ни одной строки данных.")
        return 2

    print("// Вставьте в список calProfiles (lib/identity.dart):")
    for calid, addresses in profiles.items():
        print(f"  CalProfile('{calid}', 'N/A',")
        print(f"      '{label}',")
        print("      <String, String>{")
        for param, address in addresses.items():
            print(f"        '{param}': '{address}',")
        print("      },")
        print(f"      '{parent}'),\n")
    print(f"Готово: {len(profiles)} профил(я/ей). Пересоберите APK — ячейка 05.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

FILES["lib/samples.dart"] = r'''
import 'pids.dart';

class PidSample {
  PidSample(this.pid, this.time, this.value, this.raw, this.readMs, this.error);
  final SubaruPidDef pid;
  final DateTime time;
  final double? value;
  final List<int> raw;
  final int readMs;
  final String error;
  bool get good => value != null && error.isEmpty;
  bool get allOnes => raw.isNotEmpty && raw.every((b) => b == 0xFF);
}
'''

FILES["lib/identity.dart"] = r'''
String normalizeCalId(String value) =>
    value.toUpperCase().replaceAll(RegExp(r'[^0-9A-Z]'), '');

class CalProfile {
  const CalProfile(this.calId, this.ecuId, this.label,
      this.addresses, this.parentDefs);
  final String calId, ecuId, label;
  final Map<String, String> addresses;
  final String parentDefs;
}

const List<CalProfile> calProfiles = <CalProfile>[
  CalProfile('A2TB100B', '5204584007',
      'Legacy GT (BP/BL) EJ20X JDM 2008 · TD04HL-19T · Dual AVCS · 5EAT',
      <String, String>{
        'IAM': '0xFF2538',
        'LOAD_4B': '0xFF6C9C',
        'BOOST_ERR': '0xFF6450',
        'BOOST_TGT': '0xFF6454',
        'FBKC': '0xFF7D4C',
        'FKL': '0xFF7DD0',
        'BOOST': '0xFF6AE0',
        'CL_TARGET': '0xFF73B4',
      },
      'ECU defs: A2TB100B -> A2TB100K -> 32BITBASE (TD-D/SubaruDefs)'),
];

CalProfile? matchCalProfile(String? candidate) {
  final norm = normalizeCalId(candidate ?? '');
  if (norm.isEmpty) return null;
  for (final profile in calProfiles) {
    if (profile.calId == norm) return profile;
  }
  return null;
}
'''

FILES["lib/derived.dart"] = r'''
const double kPetrolDensityGramsPerLitre = 745.0;
const double kStoichAfr = 14.7;

class FuelEstimate {
  const FuelEstimate({
    required this.fuelGramsPerSecond,
    required this.litresPerHour,
    required this.litresPer100km,
    required this.afrUsed,
    required this.afrMeasured,
    required this.speedKph,
    required this.possibleCutoff,
  });

  final double fuelGramsPerSecond;
  final double litresPerHour;
  final double? litresPer100km;
  final double afrUsed;
  final bool afrMeasured;
  final double? speedKph;
  final bool possibleCutoff;

  String get formula => 'MAF / AFR * 3600 / $kPetrolDensityGramsPerLitre';
}

FuelEstimate? estimateFuel({
  double? maf,
  double? afr,
  double? speed,
  double? rpm,
  double? pedal,
}) {
  if (maf == null || !maf.isFinite || maf <= 0) return null;
  final measured = afr != null && afr.isFinite && afr >= 8 && afr <= 25;
  final ratio = measured ? afr : kStoichAfr;
  final grams = maf / ratio;
  if (!grams.isFinite || grams < 0) return null;
  final litresPerHour = grams * 3600 / kPetrolDensityGramsPerLitre;
  double? per100;
  if (speed != null && speed.isFinite && speed >= 5) {
    per100 = litresPerHour / speed * 100;
  }
  final cutoff = pedal != null && pedal <= 1 &&
      rpm != null && rpm > 1500 &&
      speed != null && speed > 5;
  return FuelEstimate(
    fuelGramsPerSecond: grams,
    litresPerHour: litresPerHour,
    litresPer100km: per100,
    afrUsed: ratio,
    afrMeasured: measured,
    speedKph: speed,
    possibleCutoff: cutoff,
  );
}
'''

FILES["lib/analyzer.dart"] = r'''
import 'dart:math' as math;

import 'samples.dart';

enum FindingLevel { info, warning, critical }

class Finding {
  const Finding(this.level, this.title, this.detail, this.evidence, this.advice);
  final FindingLevel level;
  final String title, detail, evidence, advice;
}

class LogAnalysis {
  const LogAnalysis(this.findings, this.sampleCount, this.spanSeconds, this.channels);
  final List<Finding> findings;
  final int sampleCount, channels;
  final double spanSeconds;
}

const double kFbkcEventDeg = -1.4;
const double kFbkcBadDeg = -4.0;
const double kFlkcWarnDeg = -2.0;
const double kFlkcBadDeg = -4.0;
const double kIamFull = 15.5;
const double kIamPoor = 13.0;
const double kTrimWarnPct = 6.0;
const double kTrimBadPct = 10.0;
const double kBattWarnV = 11.8;
const double kWarmEctC = 70.0;
const double kColdRpm = 2500.0;

class _Series {
  _Series(this.samples);
  final List<({double v, DateTime t})> samples;
  int get count => samples.length;
  bool get empty => samples.isEmpty;
  double get mean =>
      samples.isEmpty ? 0 : samples.fold<double>(0, (a, s) => a + s.v) / samples.length;
  double get min =>
      samples.isEmpty ? 0 : samples.fold<double>(double.infinity, (a, s) => math.min(a, s.v));
  double get max => samples.isEmpty
      ? 0
      : samples.fold<double>(double.negativeInfinity, (a, s) => math.max(a, s.v));
  double get p95 {
    if (samples.isEmpty) return 0;
    final vals = [for (final s in samples) s.v]..sort();
    return vals[((vals.length - 1) * 0.95).round()];
  }
}

_Series _series(Map<String, List<PidSample>> history, List<String> ids) {
  final out = <({double v, DateTime t})>[];
  for (final id in ids) {
    final list = history[id];
    if (list == null) continue;
    for (final s in list) {
      final v = s.value;
      if (s.good && v != null && v.isFinite) out.add((v: v, t: s.time));
    }
  }
  out.sort((a, b) => a.t.compareTo(b.t));
  return _Series(out);
}

/// Сопоставление двух каналов по ближайшим меткам времени (v0.9).
List<({double a, double b, DateTime t})> _pair(
  _Series left,
  _Series right, {
  int toleranceMs = 300,
}) {
  final out = <({double a, double b, DateTime t})>[];
  if (left.empty || right.empty) return out;
  var j = 0;
  for (final l in left.samples) {
    while (j + 1 < right.samples.length &&
        right.samples[j + 1].t.difference(l.t).inMilliseconds.abs() <=
            right.samples[j].t.difference(l.t).inMilliseconds.abs()) {
      j++;
    }
    final r = right.samples[j];
    if (r.t.difference(l.t).inMilliseconds.abs() <= toleranceMs) {
      out.add((a: l.v, b: r.v, t: l.t));
    }
  }
  return out;
}

String _f(double v) => v.toStringAsFixed(1);
String _f2(double v) => v.toStringAsFixed(2);

LogAnalysis analyzeLog(
  Map<String, List<PidSample>> history, {
  Map<String, PidSample> attempts = const <String, PidSample>{},
}) {
  final findings = <Finding>[];
  final series = <String, _Series>{
    'rpm': _series(history, const ['RPM']),
    'load': _series(history, const ['LOAD_4B', 'LOAD']),
    'ect': _series(history, const ['ECT']),
    'fbkc': _series(history, const ['FBKC']),
    'flkc': _series(history, const ['FKL', 'KNOCK_ADV']),
    'iam': _series(history, const ['IAM']),
    'stft': _series(history, const ['STFT']),
    'ltft': _series(history, const ['LTFT']),
    'batt': _series(history, const ['BATT']),
    'tps': _series(history, const ['TPS', 'PEDAL']),
    'afr': _series(history, const ['AFR']),
    'cltgt': _series(history, const ['CL_TARGET']),
    'boost': _series(history, const ['BOOST', 'MAP_REL']),
    'boosttgt': _series(history, const ['BOOST_TGT']),
  };

  var sampleCount = 0;
  DateTime? first;
  DateTime? last;
  var channels = 0;
  for (final s in series.values) {
    sampleCount += s.count;
    if (s.count > 0) channels++;
    for (final p in s.samples) {
      if (first == null || p.t.isBefore(first)) first = p.t;
      if (last == null || p.t.isAfter(last)) last = p.t;
    }
  }
  final span = first != null && last != null
      ? last.difference(first).inMilliseconds / 1000.0
      : 0.0;

  // --- Детонация: FBKC -------------------------------------------------------------
  final fbkc = series['fbkc']!;
  if (!fbkc.empty) {
    final events = fbkc.samples.where((s) => s.v <= kFbkcEventDeg).toList();
    final hardLoad = series['load']!.max >= 1.5 && series['rpm']!.max >= 3000;
    if (events.isNotEmpty || fbkc.min <= 0) {
      if (events.length >= 5 || fbkc.min <= kFbkcBadDeg || (events.isNotEmpty && hardLoad)) {
        findings.add(Finding(
          FindingLevel.critical,
          'Детонация: ECU агрессивно снижает УОЗ',
          'Зафиксировано ${events.length} событий FBKC <= ${_f(kFbkcEventDeg)}°, '
              'минимум ${_f(fbkc.min)}°${hardLoad ? ' на нагрузке (load >= 1.5, rpm >= 3000)' : ''}. '
              'Залейте топливо с октаном выше, затем снимите 1-2° в зоне кластера через Map Lab.',
          'fbkc: n=${fbkc.count} events=${events.length} min=${_f(fbkc.min)}° · '
              'rpm_max=${_f(series['rpm']!.max)} load_max=${_f2(series['load']!.max)}',
          'Map Lab -> timing/knockadv: кластерная коррекция -0.5..-1.0° с веером сглаживания.',
        ));
      } else if (events.isNotEmpty || fbkc.min <= -0.5) {
        findings.add(Finding(
          FindingLevel.warning,
          'Единичные рывки FBKC',
          'Есть ${events.length} событий FBKC <= ${_f(kFbkcEventDeg)}°, минимум ${_f(fbkc.min)}°. '
              'Пока это штатная реакция, но повторяемость в одной зоне оборотов — повод для точечной коррекции.',
          'events=${events.length} min=${_f(fbkc.min)}°',
          'Повторный лог в той же нагрузке; если кластер — минус 0.5° в Map Lab.',
        ));
      }
    }
  }

  // --- Детонация: FLKC (обученная коррекция) ---------------------------------------
  final flkc = series['flkc']!;
  if (!flkc.empty && flkc.min < 0) {
    final level = flkc.min <= kFlkcBadDeg ? FindingLevel.critical : FindingLevel.warning;
    if (flkc.min <= kFlkcWarnDeg) {
      findings.add(Finding(
        level,
        'Обученная коррекция зажигания удерживается',
        'FLKC достигала ${_f(flkc.min)}° и не возвращается к нулю: ECU запомнил детонацию '
            'в весовых зонах. Топливо/наддув/свечи — сначала причина, потом карта.',
        'flkc: min=${_f(flkc.min)}° mean=${_f(flkc.mean)}° n=${flkc.count}',
        'Сброс обучения и повторный лог только после устранения причины.',
      ));
    }
  }

  // --- IAM (обучение грубых/тонких таблиц) -------------------------------------------
  final iam = series['iam']!;
  if (!iam.empty) {
    final maxIam = iam.max;
    if (maxIam < kIamPoor) {
      findings.add(Finding(
        FindingLevel.critical,
        'IAM критически низкий',
        'Динамический шаг зажигания ограничен: IAM max ${_f(maxIam)} из 16. '
            'Двигатель работает по грубой (conservative) карте — мощность ниже, риск выше.',
        'iam: max=${_f(maxIam)} mean=${_f(iam.mean)} n=${iam.count}',
        'Не снимать ограничение, пока FBKC/FLKC не чисты: это защита мотора.',
      ));
    } else if (maxIam < kIamFull) {
      findings.add(Finding(
        FindingLevel.warning,
        'IAM не добрался до полного',
        'IAM max ${_f(maxIam)} из 16: обучение не завершено или ECU придерживает запас. '
            'Нормально после сброса адаптаций, иначе — слабый сигнал скрытого детона.',
        'iam: max=${_f(maxIam)} mean=${_f(iam.mean)} n=${iam.count}',
        'Лог 15-20 мин спокойной езды после прогрева: IAM должен дорасти до 16.',
      ));
    } else {
      findings.add(Finding(
        FindingLevel.info,
        'IAM полный',
        'Обучение зажигания завершено: IAM достигал ${_f(maxIam)} / 16.',
        'iam: max=${_f(maxIam)} mean=${_f(iam.mean)}',
        'Без действий.',
      ));
    }
  }

  // --- Топливные коррекции -----------------------------------------------------------
  final stft = series['stft']!;
  final ltft = series['ltft']!;
  if (!stft.empty && !ltft.empty) {
    final combined = stft.mean + ltft.mean;
    final absSum = combined.abs();
    if (absSum >= kTrimBadPct) {
      findings.add(Finding(
        FindingLevel.critical,
        'Топливная коррекция за пределами',
        'STFT+LTFT ${combined >= 0 ? '+' : ''}${_f(combined)}%: смесь далека от цели. '
            'Типичные причины: подсос воздуха, MAF, датчик O2, давление топлива.',
        'stft_mean=${_f(stft.mean)}% ltft_mean=${_f(ltft.mean)}% sum=${_f(combined)}%',
        'Сначала диагностика железа. Картами VE это не лечится.',
      ));
    } else if (absSum >= kTrimWarnPct) {
      findings.add(Finding(
        FindingLevel.warning,
        'Топливная коррекция повышена',
        'STFT+LTFT ${combined >= 0 ? '+' : ''}${_f(combined)}%: умеренное смещение смеси.',
        'stft_mean=${_f(stft.mean)}% ltft_mean=${_f(ltft.mean)}%',
        'Проверить подсос/фильтр и переснять лог с AFR.',
      ));
    }
  }

  // --- Прогрев -----------------------------------------------------------------------
  final ect = series['ect']!;
  final rpm = series['rpm']!;
  if (!ect.empty && !rpm.empty && ect.min < kWarmEctC && rpm.max > kColdRpm) {
    findings.add(Finding(
      FindingLevel.warning,
      'Нагрузка до прогрева',
      'Минимум ECT ${_f(ect.min)}°C при пике ${_f(rpm.max)} об/мин: двигатель крутили холодным. '
          'Детонационный запас на холодную иной — выводы по FBKC из этой зоны не переносить в карту.',
      'ect_min=${_f(ect.min)}°C rpm_max=${_f(rpm.max)}',
      'Валидный тюнинг-лог: ECT > 80°C стабильно.',
    ));
  }

  // --- Бортсеть ------------------------------------------------------------------------
  final batt = series['batt']!;
  if (!batt.empty && batt.min < kBattWarnV) {
    findings.add(Finding(
      FindingLevel.warning,
      'Просадка питания',
      'Минимум бортсети ${_f2(batt.min)}V: при <12V логируемые значения (особенно MAF/форсунки) '
          'уходят, а клоны ELM сыплют таймаутами.',
      'batt: min=${_f2(batt.min)}V mean=${_f2(batt.mean)}V n=${batt.count}',
      'Заряд/АКБ до повторного лога; адаптер снять с прикуривателя.',
    ));
  }

  // --- AFR против цели: беднение под нагрузкой (v0.9) ---
  final afrPairs = _pair(series['afr']!, series['cltgt']!);
  if (afrPairs.isNotEmpty) {
    final lean = afrPairs.where((p) => p.a - p.b >= 0.7).toList();
    final ratio = lean.length * 100 / afrPairs.length;
    var worst = 0.0;
    for (final p in lean) {
      final d = p.a - p.b;
      if (d > worst) worst = d;
    }
    if (ratio >= 15) {
      findings.add(Finding(
        ratio >= 35 || worst >= 1.5 ? FindingLevel.critical : FindingLevel.warning,
        'Смесь беднее цели',
        'В ${_f(ratio)}% сопоставленных точек AFR выше целевого на 0.7+ '
            '(максимум +${_f(worst)}). Беднение под наддувом — прямой путь к детонации '
            'и прогару; проверяйте топливоподачу до правок карт.',
        'pairs=${afrPairs.length} lean=${lean.length} (${_f(ratio)}%) worst=+${_f(worst)}',
        'Давление топлива, форсунки, MAF-скейлинг. Затем Map Lab -> fuel.',
      ));
    }
  }

  // --- Наддув: ошибка слежения и переброс (v0.9) ---
  final boostPairs = _pair(series['boost']!, series['boosttgt']!);
  if (boostPairs.length >= 5) {
    var sumSq = 0.0;
    var over = 0.0;
    var under = 0.0;
    for (final p in boostPairs) {
      final e = p.a - p.b;
      sumSq += e * e;
      if (e > over) over = e;
      if (e < under) under = e;
    }
    final rms = math.sqrt(sumSq / boostPairs.length);
    if (over >= 0.25 || rms >= 0.15) {
      findings.add(Finding(
        over >= 0.4 ? FindingLevel.critical : FindingLevel.warning,
        'Наддув не держит цель',
        'RMS ошибки ${_f2(rms)} бар, максимальный переброс +${_f2(over)} бар, '
            'просадка ${_f2(under)} бар. Переброс опаснее недобора: это незапланированная '
            'нагрузка на поршневую.',
        'pairs=${boostPairs.length} rms=${_f2(rms)} over=+${_f2(over)} under=${_f2(under)}',
        'Map Lab -> wgdc: снизить дьюти в зоне переброса, проверить вестгейт и шланги.',
      ));
    }
  }

  // --- Аномалии каналов: выбросы по z-score (v0.9.1) ---
  for (final entry in series.entries) {
    final s = entry.value;
    if (s.count < 30) continue;
    final mean = s.mean;
    var acc = 0.0;
    for (final p in s.samples) {
      acc += (p.v - mean) * (p.v - mean);
    }
    final std = math.sqrt(acc / s.count);
    if (std <= 0) continue;
    var outliers = 0;
    for (final p in s.samples) {
      if ((p.v - mean).abs() > 3.5 * std) outliers++;
    }
    final ratio = outliers * 100 / s.count;
    if (outliers >= 3 && ratio >= 1.0) {
      findings.add(Finding(
        FindingLevel.info,
        'Выбросы канала ${entry.key}',
        'Канал ${entry.key}: $outliers из ${s.count} значений (${_f(ratio)}%) ушли дальше '
            '3.5σ от среднего. Частая причина — единичные сбои ELM-клона или mute '
            'проблемных адресов, а не физика двигателя.',
        'channel=${entry.key} outliers=$outliers/${s.count} mean=${_f2(mean)} std=${_f2(std)}',
        'Если выбросов > 5% — переподключите адаптер и снимите лог повторно.',
      ));
    }
  }

  // --- Неравномерный холостой ход: признак пропусков/подсоса (v0.9.1) ---
  final idlePairs = _pair(series['rpm']!, series['tps']!)
      .where((p) => p.b <= 3 && p.a >= 500 && p.a <= 1500)
      .toList();
  if (idlePairs.length >= 10 && !ect.empty && ect.max >= 70) {
    final meanRpm = idlePairs.fold<double>(0, (a, p) => a + p.a) / idlePairs.length;
    var accR = 0.0;
    for (final p in idlePairs) {
      accR += (p.a - meanRpm) * (p.a - meanRpm);
    }
    final stdRpm = math.sqrt(accR / idlePairs.length);
    if (stdRpm >= 70) {
      findings.add(Finding(
        FindingLevel.warning,
        'Неравномерный холостой ход',
        'На тёплом двигателе при закрытом дросселе обороты гуляют σ=${_f(stdRpm)} об/мин '
            'вокруг ${_f(meanRpm)}. Похоже на пропуски воспламенения, подсос воздуха или '
            'грязный дроссель — чинить до тюнинга.',
        'idle_points=${idlePairs.length} rpm_mean=${_f(meanRpm)} rpm_std=${_f(stdRpm)}',
        'Свечи/катушки, проверка на подсос, LTFT на холостом.',
      ));
    }
  }

  // --- Полнота данных -------------------------------------------------------------------
  if (channels < 8) {
    final failed = attempts.values.where((s) => !s.good).length;
    findings.add(Finding(
      FindingLevel.info,
      'Мало каналов для выводов',
      'Активно каналов: $channels. Для тюнинга базово нужны RPM/LOAD/TIMING/FBKC/FLKC/IAM/AFR/MAF. '
          '${failed > 0 ? 'Ошибок чтения за сессию: $failed.' : ''}',
      'channels=$channels failed_attempts=$failed',
      'Включите полный набор PID и/или расширенный набор (CALID-гейт).',
    ));
  }

  findings.add(Finding(
    FindingLevel.info,
    'Сводка анализа',
    'Обработано $sampleCount значений по $channels каналам за ${_f(span)} с. '
        'Найдено проблем: ${findings.where((f) => f.level != FindingLevel.info).length}.',
    'samples=$sampleCount channels=$channels span=${_f(span)}s',
    findings.any((f) => f.level == FindingLevel.critical)
        ? 'Сначала критичные находки, потом Map Lab.'
        : 'Лог можно передавать в Map Lab для табличной коррекции.',
  ));

  return LogAnalysis(List<Finding>.unmodifiable(findings), sampleCount, span, channels);
}

// ======================= Сравнение логов «до/после» (v0.9.1) =======================

class ChannelDelta {
  const ChannelDelta(this.id, this.samplesBase, this.samplesAfter, this.meanBase,
      this.meanAfter, this.p95Base, this.p95After);
  final String id;
  final int samplesBase, samplesAfter;
  final double meanBase, meanAfter, p95Base, p95After;
  double get meanDelta => meanAfter - meanBase;
}

class LogComparison {
  const LogComparison(this.channels, this.verdict, this.knockBase, this.knockAfter);
  final List<ChannelDelta> channels;
  final String verdict;
  final int knockBase, knockAfter;
}

int _knockEvents(Map<String, List<PidSample>> history) {
  final fbkc = _series(history, const ['FBKC']);
  var n = 0;
  for (final s in fbkc.samples) {
    if (s.v <= kFbkcEventDeg) n++;
  }
  return n;
}

LogComparison compareLogs(
  Map<String, List<PidSample>> baseline,
  Map<String, List<PidSample>> after, {
  List<String> channels = const [
    'RPM', 'LOAD', 'TIMING', 'FBKC', 'FKL', 'IAM', 'AFR', 'STFT', 'LTFT', 'BOOST', 'ECT'
  ],
}) {
  final deltas = <ChannelDelta>[];
  for (final id in channels) {
    final a = _series(baseline, [id]);
    final b = _series(after, [id]);
    if (a.count < 5 || b.count < 5) continue;
    deltas.add(ChannelDelta(id, a.count, b.count, a.mean, b.mean, a.p95, b.p95));
  }
  deltas.sort((x, y) => y.meanDelta.abs().compareTo(x.meanDelta.abs()));
  final knockBase = _knockEvents(baseline);
  final knockAfter = _knockEvents(after);
  final verdict = knockAfter < knockBase
      ? 'Стало лучше: событий детонации $knockBase → $knockAfter'
      : knockAfter > knockBase
          ? 'Стало хуже: событий детонации $knockBase → $knockAfter — откатить последнюю правку'
          : 'Детонация без изменений ($knockBase события): правка нейтральна';
  return LogComparison(List<ChannelDelta>.unmodifiable(deltas), verdict, knockBase, knockAfter);
}
'''

FILES[".github/workflows/build.yml"] = r'''
name: build-apk
on:
  push:
    branches: [ main ]
  workflow_dispatch:
jobs:
  android:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: "17"
      - uses: subosito/flutter-action@v2
        with:
          flutter-version: "3.35.4"
          channel: stable
      - run: flutter pub get
      - run: dart format --output=none --set-exit-if-changed lib test
      - run: flutter analyze --no-pub --no-fatal-infos
      - run: flutter test --no-pub
      - run: flutter build apk --release --no-pub
      - uses: actions/upload-artifact@v4
        with:
          name: ssm2-release-apk
          path: build/app/outputs/flutter-apk/app-release.apk
'''

FILES["test/app_test.dart"] = r'''
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/main.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUpAll(() {
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(const MethodChannel('ssm2/system'),
        (MethodCall call) async {
      if (call.method == 'sdkInt') return 35;
      return null;
    });
    messenger.setMockMethodCallHandler(const MethodChannel('ssm2/spp'),
        (MethodCall call) async {
      if (call.method == 'paired') return <Map<String, String>>[];
      if (call.method == 'connect') return false;
      return null;
    });
    messenger.setMockMethodCallHandler(
        const MethodChannel('flutter.baseflow.com/permissions/methods'),
        (MethodCall call) async {
      if (call.method == 'requestPermissions') return <int, int>{};
      if (call.method == 'checkPermissionStatus') return 1;
      return null;
    });
  });

  testWidgets('приложение поднимается без краха', (tester) async {
    await tester.pumpWidget(const SsmApp());
    await tester.pump();
    expect(find.byType(MaterialApp), findsOneWidget);
  });
}
'''

FILES["analysis_options.yaml"] = r'''
analyzer:
  exclude:
    - vendor/**
    - transport_templates/**
  language:
    strict-casts: true
    strict-raw-types: true
'''

FILES["lib/transport_selected.dart"] = r'''
import 'bt_transport.dart';
import 'native_spp.dart';

/// Фасад выбранного транспорта: в 0.8 всегда нативный RFCOMM-канал.
class SelectedTransport extends NativeSppTransport {}

BtTransport createTransport() => SelectedTransport();
'''

def generate_project():
    for relative, content in FILES.items():
        if relative.endswith(".py"):
            ast.parse(content, filename=relative)
            compile(content, relative, "exec")
    if APP.exists():
        backup = APP.with_name(APP.name + f"_backup_{time.time_ns()}")
        APP.rename(backup)
        print("Предыдущий проект сохранен:", backup)
    APP.mkdir(parents=True)
    run([FLUTTER / "bin/flutter", "create", "--no-pub", "--platforms=android",
         "--org", "com.subaru", "--project-name", "subaru_ssm2", APP])
    for relative in ["lib", "test", "android/app/src/main/kotlin"]:
        shutil.rmtree(APP / relative, ignore_errors=True)
    for relative in ["android/build.gradle", "android/settings.gradle", "android/app/build.gradle"]:
        (APP / relative).unlink(missing_ok=True)
    for relative, content in FILES.items():
        write(relative, content)
    write("android/local.properties", f"sdk.dir={CFG['sdk']}\nflutter.sdk={CFG['flutter']}\n")
    write("build_config.json", json.dumps({**CFG, "bt_package": "native_spp", "revision": "0.9.2"}, indent=2))
    write("pid_catalog.json", json.dumps(PIDS, ensure_ascii=False, indent=2))
    shutil.copyfile(APP / "pubspec.template.yaml", APP / "pubspec.yaml")
    run([FLUTTER / "bin/flutter", "pub", "get"])
    print(f"\n[УСПЕХ v0.9] Создано {len(FILES)} файлов · native SPP · аналитика · алерты · CI")

generate_project()