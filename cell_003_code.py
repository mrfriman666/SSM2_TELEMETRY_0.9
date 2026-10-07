# @title 03 | Map Lab 0.9 - heatmap + 3D (ЖЕСТЫ ИСПРАВЛЕНЫ) + table zoom { display-mode: "form" }

import json
import re
import urllib.request
from pathlib import Path

CONFIG = Path("/content/ssm2_fixed_env.json")
if not CONFIG.exists():
    raise RuntimeError("Сначала выполните ячейку 1")
CFG = json.loads(CONFIG.read_text(encoding="utf-8"))
APP = Path(CFG["app"])
if not (APP / "pubspec.yaml").exists():
    raise RuntimeError("Проект не найден. Сначала выполните ячейку 2/3.")
FILES = {}

FILES["lib/maps_lab/maps_lab_core.dart"] = r"""
/// Map Lab core — чистый Dart без Flutter.
library;

import 'dart:convert';
import 'dart:typed_data';

class SafeExpr {
  static final RegExp _ok = RegExp(r'^[0-9xX\.\+\-\*/\(\)\s]+$');

  static double Function(double) compile(String? src) {
    if (src == null || src.trim().isEmpty) return (v) => v;
    final s = src.replaceAll('X', 'x');
    if (!_ok.hasMatch(s)) return (v) => v;
    try {
      final parser = _ExprParser(s);
      final node = parser.parse();
      return (x) => node(x);
    } catch (_) {
      return (v) => v;
    }
  }
}

class _ExprParser {
  _ExprParser(this.s);
  final String s;
  int i = 0;

  double Function(double) parse() {
    final n = _expr();
    _ws();
    if (i != s.length) throw FormatException('лишние символы: ${s.substring(i)}');
    return n;
  }

  void _ws() {
    while (i < s.length && s[i] == ' ') {
      i++;
    }
  }

  bool _eat(String ch) {
    _ws();
    if (i < s.length && s[i] == ch) {
      i++;
      return true;
    }
    return false;
  }

  double Function(double) _expr() {
    var node = _term();
    while (true) {
      if (_eat('+')) {
        final r = _term();
        final l = node;
        node = (x) => l(x) + r(x);
      } else if (_eat('-')) {
        final r = _term();
        final l = node;
        node = (x) => l(x) - r(x);
      } else {
        return node;
      }
    }
  }

  double Function(double) _term() {
    var node = _factor();
    while (true) {
      if (_eat('*')) {
        final r = _factor();
        final l = node;
        node = (x) => l(x) * r(x);
      } else if (_eat('/')) {
        final r = _factor();
        final l = node;
        node = (x) => r(x) == 0 ? double.nan : l(x) / r(x);
      } else {
        return node;
      }
    }
  }

  static final RegExp _num = RegExp(r'(\d+\.?\d*|\.\d+)');

  double Function(double) _factor() {
    _ws();
    if (_eat('-')) {
      final n = _factor();
      return (x) => -n(x);
    }
    if (_eat('+')) return _factor();
    if (_eat('(')) {
      final n = _expr();
      if (!_eat(')')) throw const FormatException('нет закрывающей скобки');
      return n;
    }
    _ws();
    if (i < s.length && s[i] == 'x') {
      i++;
      return (x) => x;
    }
    final m = _num.matchAsPrefix(s, i);
    if (m == null) throw FormatException('ожидалось число @ $i');
    i = m.end;
    final v = double.parse(m.group(0)!);
    return (x) => v;
  }
}

enum Stype { u8, i8, u16, i16, f32 }

class Scaling {
  Scaling({
    required this.name,
    required this.units,
    required this.type,
    required this.bigEndian,
    required this.to,
    required this.fr,
    required this.format,
  });

  factory Scaling.fromAttrs(String name, Map<String, String> a) {
    final st = switch (a['storagetype'] ?? 'uint8') {
      'int8' => Stype.i8,
      'uint16' => Stype.u16,
      'int16' => Stype.i16,
      'float' => Stype.f32,
      _ => Stype.u8,
    };
    return Scaling(
      name: name,
      units: a['units'] ?? '',
      type: st,
      bigEndian: (a['endian'] ?? 'big') == 'big',
      to: SafeExpr.compile(a['toexpr']),
      fr: SafeExpr.compile(a['frexpr']),
      format: a['format'] ?? '%.2f',
    );
  }

  final String name;
  final String units;
  final Stype type;
  final bool bigEndian;
  final double Function(double) to;
  final double Function(double) fr;
  final String format;

  int get bytes => switch (type) {
        Stype.f32 => 4,
        Stype.u16 || Stype.i16 => 2,
        _ => 1,
      };
}

class AxisDef {
  String name = '';
  String? scalingName;
  int? address;
  int? elements;
  Scaling? scaling;
}

class TableDef {
  TableDef(this.name);
  final String name;
  String? type;
  String? category;
  String? scalingName;
  int? dataAddress;
  Scaling? dataScaling;
  final List<AxisDef> axes = [];
}

class DefMeta {
  String xmlid = '';
  String internalIdAddress = '2000';
  String internalIdString = '';
  String ecuid = '';
  String checksumModule = '';
}

class _RawTable {
  _RawTable(this.attrs, this.children);
  final Map<String, String> attrs;
  final List<Map<String, String>> children;
}

class _RawDef {
  final meta = DefMeta();
  final includes = <String>[];
  final scalings = <String, Map<String, String>>{};
  final tables = <_RawTable>[];
}

class DefSet {
  DefSet._();
  final meta = DefMeta();
  final chain = <String>[];
  final scalings = <String, Scaling>{};
  final tables = <String, TableDef>{};

  static const _generic = {'x', 'y', 'z', ''};

  static final _attrRe = RegExp(r'([\w:-]+)="([^"]*)"');
  static final _tableTok = RegExp(r'<table\s[^>]*?>|</table>');
  static final _scalingRe = RegExp(r'<scaling\s+([^>]*?)/>');
  static final _romidRe = RegExp(r'<romid>([\s\S]*?)</romid>');
  static final _innerTag = RegExp(r'<(\w+)>([^<]*)</\1>');
  static final _includeRe = RegExp(r'<include>\s*([^<]+?)\s*</include>');

  static Map<String, String> _attrs(String tag) =>
      {for (final m in _attrRe.allMatches(tag)) m.group(1)!: m.group(2)!};

  static _RawDef _parse(String raw) {
    final def = _RawDef();
    final romid = _romidRe.firstMatch(raw);
    if (romid != null) {
      for (final m in _innerTag.allMatches(romid.group(1)!)) {
        final v = m.group(2)!.trim();
        if (v.isEmpty) continue;
        switch (m.group(1)) {
          case 'xmlid':
            def.meta.xmlid = v;
          case 'internalidaddress':
            def.meta.internalIdAddress = v;
          case 'internalidstring':
            def.meta.internalIdString = v;
          case 'ecuid':
            def.meta.ecuid = v;
          case 'checksummodule':
            def.meta.checksumModule = v;
        }
      }
    }
    for (final m in _includeRe.allMatches(raw)) {
      def.includes.add(m.group(1)!);
    }
    for (final m in _scalingRe.allMatches(raw)) {
      final a = _attrs(m.group(0)!);
      if (a['storagetype'] == 'bloblist') continue;
      final n = a['name'];
      if (n != null && n.isNotEmpty) def.scalings[n] = a;
    }

    var depth = 0;
    for (final m in _tableTok.allMatches(raw)) {
      final tok = m.group(0)!;
      if (tok == '</table>') {
        if (depth > 0) depth--;
        continue;
      }
      final selfClose = tok.endsWith('/>');
      final a = _attrs(tok);
      if (depth == 0) {
        def.tables.add(_RawTable(a, []));
        if (!selfClose) depth = 1;
      } else {
        def.tables.last.children.add(a);
      }
    }
    return def;
  }

  static DefSet build(Map<String, String> xmlById, String rootId) {
    final raw = <String, _RawDef>{};
    for (final e in xmlById.entries) {
      raw[e.key] = _parse(e.value);
    }

    final order = <String>[];
    final seen = <String>{};
    void walk(String id) {
      if (!seen.add(id)) return;
      for (final inc in raw[id]?.includes ?? const <String>[]) {
        if (raw.containsKey(inc)) walk(inc);
      }
      order.add(id);
    }

    walk(rootId);

    final set = DefSet._();
    set.chain.addAll(order);
    if (raw.containsKey(rootId)) {
      final m = raw[rootId]!.meta;
      set.meta
        ..xmlid = m.xmlid
        ..internalIdAddress = m.internalIdAddress
        ..internalIdString = m.internalIdString
        ..ecuid = m.ecuid
        ..checksumModule = m.checksumModule;
    }

    for (final id in order) {
      final d = raw[id]!;
      d.scalings.forEach((n, a) {
        set.scalings[n] = Scaling.fromAttrs(n, a);
      });
      for (final rt in d.tables) {
        final name = (rt.attrs['name'] ?? '').trim();
        if (name.isEmpty) continue;
        final t = set.tables.putIfAbsent(name, () => TableDef(name));
        for (final k in ['type', 'category', 'level', 'scaling']) {
          final v = rt.attrs[k];
          if (v != null && v.isNotEmpty) {
            switch (k) {
              case 'type':
                t.type = v;
              case 'category':
                t.category = v;
              case 'scaling':
                t.scalingName = v;
            }
          }
        }
        final addr = rt.attrs['address'];
        if (addr != null && addr.isNotEmpty) t.dataAddress = int.parse(addr, radix: 16);
        for (var i = 0; i < rt.children.length; i++) {
          while (t.axes.length <= i) {
            t.axes.add(AxisDef());
          }
          final a = t.axes[i];
          final ch = rt.children[i];
          final cn = (ch['name'] ?? '').trim();
          if (!_generic.contains(cn.toLowerCase())) a.name = cn;
          if ((ch['scaling'] ?? '').isNotEmpty) a.scalingName = ch['scaling'];
          if ((ch['elements'] ?? '').isNotEmpty) a.elements = int.parse(ch['elements']!);
          if ((ch['address'] ?? '').isNotEmpty) a.address = int.parse(ch['address']!, radix: 16);
        }
      }
    }

    for (final t in set.tables.values) {
      t.dataScaling = set.scalings[t.scalingName];
      for (final a in t.axes) {
        a.scaling = set.scalings[a.scalingName];
      }
    }
    return set;
  }

  bool isReadable3D(TableDef t) =>
      t.dataAddress != null &&
      t.dataScaling != null &&
      t.axes.length >= 2 &&
      t.axes[0].address != null &&
      t.axes[0].elements != null &&
      t.axes[0].scaling != null &&
      t.axes[1].address != null &&
      t.axes[1].elements != null &&
      t.axes[1].scaling != null;
}

class AxisVals {
  AxisVals(this.name, this.values, this.guess);
  final String name;
  final List<double> values;
  final String guess;
}

class MapGrid {
  MapGrid({
    required this.name,
    required this.kind,
    required this.units,
    required this.addr,
    required this.x,
    required this.y,
    required this.data,
  });
  final String name;
  final String kind;
  final String units;
  final int addr;
  final AxisVals x;
  final AxisVals y;
  final List<List<double>> data;

  int get rows => data.length;
  int get cols => data.isEmpty ? 0 : data[0].length;
  double get vmin => data.expand((r) => r).reduce((a, b) => a < b ? a : b);
  double get vmax => data.expand((r) => r).reduce((a, b) => a > b ? a : b);
}

class RomParser {
  RomParser(this.rom);
  final Uint8List rom;

  String readRomId(int addr) {
    if (addr < 0 || addr + 16 > rom.length) return '';
    final bytes = <int>[];
    for (var i = addr; i < addr + 16; i++) {
      final b = rom[i];
      if (b == 0) break;
      bytes.add(b);
    }
    return ascii.decode(bytes, allowInvalid: true).trim();
  }

  List<double> _read(Scaling sc, int addr, int count) {
    final size = sc.bytes;
    if (addr < 0 || addr + count * size > rom.length) {
      throw FormatException(
          'диапазон 0x${addr.toRadixString(16)} + $count×$size вне ROM (${rom.length} байт)');
    }
    final bd = ByteData.sublistView(rom, addr, addr + count * size);
    final en = sc.bigEndian ? Endian.big : Endian.little;
    final out = List<double>.filled(count, 0);
    for (var i = 0; i < count; i++) {
      final v = switch (sc.type) {
        Stype.u8 => bd.getUint8(i).toDouble(),
        Stype.i8 => bd.getInt8(i).toDouble(),
        Stype.u16 => bd.getUint16(i * 2, en).toDouble(),
        Stype.i16 => bd.getInt16(i * 2, en).toDouble(),
        Stype.f32 => bd.getFloat32(i * 4, en),
      };
      out[i] = sc.to(v);
    }
    return out;
  }

  static String guessAxis(List<double> v) {
    if (v.isEmpty) return '?';
    var lo = v.first, hi = v.first;
    for (final x in v) {
      if (x < lo) lo = x;
      if (x > hi) hi = x;
    }
    if (hi > 800) return 'rpm';
    if (hi > 50 && hi <= 600) return 'нм/у.е.';
    if (hi <= 5.5 && lo >= -0.5) return 'г/об·бар';
    if (hi <= 14 && lo >= 0) return 'вольты/%';
    return '?';
  }

  static String classify(String name, String units) {
    final n = name.toLowerCase();
    if (n.contains('wastegate duty')) return 'wgdc';
    if (n.contains('target boost')) return 'boost';
    if (n.contains('requested torque')) return 'torque';
    if (n.contains('knock correction')) return 'knockadv';
    if (n.contains('fueling') || n.contains('fuel')) return 'fuel';
    if (n.contains('timing')) return 'timing';
    return 'other';
  }

  double _round4(double v) => (v * 10000).roundToDouble() / 10000;

  MapGrid? extract(TableDef t) {
    if (t.dataAddress == null || t.dataScaling == null) return null;
    if (t.axes.length < 2) return null;
    final ax = t.axes[0], ay = t.axes[1];
    if (ax.address == null || ax.elements == null || ax.scaling == null) return null;
    if (ay.address == null || ay.elements == null || ay.scaling == null) return null;

    final xv = _read(ax.scaling!, ax.address!, ax.elements!);
    final yv = _read(ay.scaling!, ay.address!, ay.elements!);
    final cols = ax.elements!, rows = ay.elements!;
    final flat = _read(t.dataScaling!, t.dataAddress!, rows * cols);
    final grid = <List<double>>[
      for (var r = 0; r < rows; r++) [for (var c = 0; c < cols; c++) _round4(flat[r * cols + c])],
    ];
    return MapGrid(
      name: t.name,
      kind: classify(t.name, t.dataScaling!.units),
      units: t.dataScaling!.units,
      addr: t.dataAddress!,
      x: AxisVals(ax.name.isEmpty ? 'X' : ax.name, [for (final v in xv) _round4(v)], guessAxis(xv)),
      y: AxisVals(ay.name.isEmpty ? 'Y' : ay.name, [for (final v in yv) _round4(v)], guessAxis(yv)),
      data: grid,
    );
  }

  static const keyTables = [
    'Base Timing Primary Cruise',
    'Base Timing Primary Non-Cruise',
    'Primary Open Loop Fueling',
    'Target Boost_',
    'Initial Wastegate Duty_',
    'Max Wastegate Duty_',
    'Knock Correction Advance Max Non-Cruise',
    'Requested Torque A (Accelerator Pedal) SI-DRIVE Sport',
  ];

  Map<String, MapGrid> extractKeys(DefSet defs) {
    final out = <String, MapGrid>{};
    for (final n in keyTables) {
      final t = defs.tables[n];
      if (t == null) continue;
      try {
        final g = extract(t);
        if (g != null) out[n] = g;
      } catch (_) {}
    }
    return out;
  }
}
"""

# maps_lab_log — без Color (чтобы library оставался чистым). Heatmap — в page/3d.
FILES["lib/maps_lab/maps_lab_log.dart"] = r"""
/// Map Lab: лог и правила.
library;

import 'dart:convert';
import 'dart:math' as math;

import 'maps_lab_core.dart';

class LogData {
  final cols = <String, List<double?>>{};
  int get rows => cols.isEmpty ? 0 : cols.values.first.length;

  bool has(String k) => cols.containsKey(k);
  List<double?>? operator [](String k) => cols[k];

  static const aliases = <String, List<String>>{
    'time': ['time', 'timestamp', 'time s', 'elapsed'],
    'rpm': ['engine speed', 'rpm', 'engine speed rpm'],
    'load': [
      'engine load g/rev',
      'load_4b',
      'engine load 4-byte',
      'calculated load',
      'engine load',
      'load'
    ],
    'fbkc': ['feedback knock correction', 'fbkc'],
    'flkc': ['fine learning knock correction', 'fkl', 'fine learning knock advance', 'flkc'],
    'iam': ['iam', 'ignition advance multiplier'],
    'timing': ['total ignition timing', 'ignition timing', 'timing'],
    'afr': ['afr', 'a/f sensor #1', 'a/f sensor 1', 'air/fuel ratio', 'estimated afr', 'lambda'],
    'boost': ['manifold relative pressure', 'boost', 'boost_rel'],
    'tgt': ['target boost', 'boost_tgt'],
    'berr': ['boost error', 'boost_err'],
    'wgdc': ['primary wastegate duty', 'wastegate duty', 'wgdc', 'boost control solenoid duty'],
    'tq': ['requested torque', 'cl_target', 'demand torque'],
    'thr': ['throttle opening angle', 'throttle plate', 'throttle', 'throttle position'],
    'iat': ['intake air temperature', 'iat'],
    'ect': ['coolant temperature', 'ect', 'engine coolant temperature'],
    'loop': ['cl/ol', 'fueling status', 'closed loop', 'loop'],
  };

  static String _canon(String s) {
    var c = s.toLowerCase().trim();
    final p = c.indexOf('(');
    if (p > 0) c = c.substring(0, p);
    final b = c.indexOf('[');
    if (b > 0) c = c.substring(0, b);
    return c.replaceAll('*', '').replaceAll(RegExp(r'\s+'), ' ').trim();
  }

  static LogData parse(String text) {
    final head = text.substring(0, math.min(text.length, 4096));
    final sep = ';'.allMatches(head).length > ','.allMatches(head).length ? ';' : ',';
    final decComma = sep == ';' && ','.allMatches(head).length > '.'.allMatches(head).length;

    final lines = const LineSplitter().convert(text).where((l) => l.trim().isNotEmpty).toList();
    if (lines.isEmpty) return LogData();
    final header = lines.first.split(sep).map((h) => h.trim().replaceAll('"', '')).toList();

    final raw = List<List<double?>>.generate(header.length, (_) => []);
    final canonHead = header.map(_canon).toList();

    for (var li = 1; li < lines.length; li++) {
      final parts = lines[li].split(sep);
      for (var i = 0; i < header.length; i++) {
        if (i >= parts.length) {
          raw[i].add(null);
          continue;
        }
        var tok = parts[i].trim().replaceAll('"', '');
        if (decComma) tok = tok.replaceAll(',', '.');
        raw[i].add(double.tryParse(tok));
      }
    }

    final log = LogData();
    final usedNames = <String>{};
    for (final entry in aliases.entries) {
      final canon = entry.key;
      final variants = entry.value.map(_canon).toSet();
      for (var i = 0; i < header.length; i++) {
        if (usedNames.contains(header[i])) continue;
        if (variants.contains(canonHead[i])) {
          log.cols[canon] = raw[i];
          usedNames.add(header[i]);
          break;
        }
      }
    }

    final afr = log.cols['afr'];
    if (afr != null) {
      final vals = afr.whereType<double>().toList()..sort();
      if (vals.isNotEmpty && vals[vals.length ~/ 2] <= 2.2) {
        log.cols['afr'] = [for (final v in afr) v == null ? null : v * 14.7];
      }
    }
    final boost = log.cols['boost'], tgt = log.cols['tgt'];
    if (boost != null && tgt != null && log.cols['berr'] == null) {
      log.cols['berr'] = [
        for (var i = 0; i < boost.length; i++)
          (boost[i] != null && tgt[i] != null) ? boost[i]! - tgt[i]! : null,
      ];
    }
    return log;
  }
}

class LogHealth {
  final notes = <String>[];
  final missing = <String, List<String>>{};
  bool blockReady(String b) => (missing[b] ?? const []).isEmpty;
}

class LogAudit {
  static const _needFor = {
    'timing': ['rpm', 'load', 'fbkc', 'flkc', 'iat'],
    'fuel': ['rpm', 'load', 'afr', 'boost'],
    'wgdc': ['rpm', 'tq', 'berr'],
  };

  static const _hint = {
    'wgdc': 'Primary Wastegate Duty',
    'tq': 'Requested Torque',
    'thr': 'Throttle Opening Angle',
    'afr': 'A/F Sensor #1',
    'loop': 'CL/OL Fueling Status',
  };

  static LogHealth check(LogData log) {
    final h = LogHealth();
    h.notes.add('строк: ${log.rows}');

    double? maxOf(String k) {
      final c = log[k];
      if (c == null) return null;
      double? m;
      for (final v in c) {
        if (v != null && (m == null || v > m)) m = v;
      }
      return m;
    }

    double? minOf(String k) {
      final c = log[k];
      if (c == null) return null;
      double? m;
      for (final v in c) {
        if (v != null && (m == null || v < m)) m = v;
      }
      return m;
    }

    final mr = maxOf('rpm');
    if (mr != null) h.notes.add('RPM max: ${mr.toStringAsFixed(0)}');
    final iamMin = minOf('iam');
    if (iamMin != null) {
      h.notes.add('IAM min: ${iamMin.toStringAsFixed(2)}'
          '${iamMin < 0.99 ? ' — ЛОГ НЕ ГОДИТСЯ: сначала доучить ЭБУ' : ' — ок'}');
    }
    final mb = maxOf('boost');
    if (mb != null) h.notes.add('буст max: ${mb.toStringAsFixed(2)} бар');

    final t = log['time'];
    if (t != null) {
      final dts = <double>[];
      for (var i = 1; i < t.length; i++) {
        if (t[i] != null && t[i - 1] != null) dts.add(t[i]! - t[i - 1]!);
      }
      dts.sort();
      if (dts.isNotEmpty) {
        final med = dts[dts.length ~/ 2];
        final gaps = dts.where((d) => d > 0.3).length;
        h.notes.add('частота: медиана ${(1 / med).toStringAsFixed(1)} Гц · провалов >300 мс: $gaps');
        if (med > 0.25) h.notes.add('!! реже 4 Гц — сократите набор PID до 10–12');
      }
    }

    _needFor.forEach((block, req) {
      final miss = req.where((r) => !log.has(r)).toList();
      h.missing[block] = miss;
    });
    final allMiss = {for (final v in h.missing.values) ...v};
    for (final m in allMiss) {
      final hint = _hint[m];
      if (hint != null) h.notes.add('добавить в логгер PID: $hint');
    }
    return h;
  }
}

class CellNote {
  int n = 0;
  double? fbkc, flkc, iat, iam, afr, target, berr;
  String why = '';
}

class MapResult {
  MapResult(int rows, int cols)
      : delta = List.generate(rows, (_) => List.filled(cols, 0.0)),
        info = List.generate(rows, (_) => List<CellNote?>.filled(cols, null));
  final List<List<double>> delta;
  final List<List<CellNote?>> info;

  int get rows => delta.length;
  int get cols => delta.isNotEmpty ? delta[0].length : 0;

  int get dec {
    var c = 0;
    for (final r in delta) {
      for (final v in r) {
        if (v < 0) c++;
      }
    }
    return c;
  }

  int get inc {
    var c = 0;
    for (final r in delta) {
      for (final v in r) {
        if (v > 0) c++;
      }
    }
    return c;
  }
}

class AnalyzerConfig {
  const AnalyzerConfig({
    this.minN = 6,
    this.fbkcEvent = -1.5,
    this.flkcEvent = -1.5,
    this.timingStep = 0.5,
    this.timingMaxCut = -3.0,
    this.allowTimingAdd = false,
    this.afrErr = 0.40,
    this.afrMaxCut = -0.8,
    this.boostErr = 0.04,
    this.wgdcMaxDelta = 6.0,
    this.wotLoad = 2.2,
    this.targetBoostGain = 0.0,
  });
  final int minN;
  final double fbkcEvent, flkcEvent, timingStep, timingMaxCut;
  final bool allowTimingAdd;
  final double afrErr, afrMaxCut, boostErr, wgdcMaxDelta, wotLoad, targetBoostGain;
}

class Analyzer {
  Analyzer(this.cfg);
  final AnalyzerConfig cfg;

  double _roundStep(double v, double s) => (v / s).roundToDouble() * s;

  int _binIdx(List<double> axis, double v) {
    if (axis.length <= 1) return 0;
    var lo = 0, hi = axis.length - 1;
    while (lo < hi - 1) {
      final mid = (lo + hi) >> 1;
      if (v >= axis[mid]) {
        lo = mid;
      } else {
        hi = mid;
      }
    }
    final mid = (axis[lo] + axis[hi]) / 2;
    return v < mid ? lo : hi;
  }

  MapResult? analyzeMap(MapGrid g, LogData log) {
    final kind = g.kind;
    if (!{'timing', 'knockadv', 'fuel', 'wgdc', 'boost'}.contains(kind)) return null;

    final xIsRpm = g.x.guess == 'rpm' || (g.y.guess != 'rpm' && g.x.values.last > 800);
    final rpmAxis = xIsRpm ? g.x.values : g.y.values;
    final otherAxis = xIsRpm ? g.y.values : g.x.values;

    final need = switch (kind) {
      'timing' || 'knockadv' => ['rpm', 'load', 'fbkc', 'flkc'],
      'fuel' => ['rpm', 'load', 'afr', 'fbkc', 'flkc'],
      _ => ['rpm', 'tq', 'berr'],
    };
    if (need.any((k) => !log.has(k))) return null;

    final rpmCol = log['rpm']!;
    final otherCol = switch (kind) {
      'timing' || 'knockadv' || 'fuel' => log['load']!,
      _ => log['tq']!,
    };

    final rows = g.rows, cols = g.cols;
    final cnt = List<int>.filled(rows * cols, 0);
    final fbkcMin = List<double>.filled(rows * cols, 0);
    final flkcSum = List<double>.filled(rows * cols, 0);
    final flkcLateSum = List<double>.filled(rows * cols, 0);
    final flkcLateCnt = List<int>.filled(rows * cols, 0);
    final halfRows = (log.rows / 2).round();
    final iatMax = List<double>.filled(rows * cols, -999);
    final iamMin = List<double>.filled(rows * cols, 999);
    final afrSum = List<double>.filled(rows * cols, 0);
    final berrSum = List<double>.filled(rows * cols, 0);

    final fbkc = log['fbkc'], flkc = log['flkc'], iat = log['iat'];
    final iam = log['iam'], afr = log['afr'], berr = log['berr'];

    for (var i = 0; i < log.rows; i++) {
      final rv = rpmCol[i], ov = otherCol[i];
      if (rv == null || ov == null) continue;
      final ri = _binIdx(xIsRpm ? otherAxis : rpmAxis, xIsRpm ? ov : rv);
      final ci = _binIdx(xIsRpm ? rpmAxis : otherAxis, xIsRpm ? rv : ov);
      final idx = ri * cols + ci;
      cnt[idx]++;
      final f = fbkc?[i];
      if (f != null && (cnt[idx] == 1 || f < fbkcMin[idx])) fbkcMin[idx] = f;
      final fl = flkc?[i];
      if (fl != null) {
        flkcSum[idx] += fl;
        if (i >= halfRows) {
          flkcLateSum[idx] += fl;
          flkcLateCnt[idx]++;
        }
      }
      final it = iat?[i];
      if (it != null && it > iatMax[idx]) iatMax[idx] = it;
      final im = iam?[i];
      if (im != null && im < iamMin[idx]) iamMin[idx] = im;
      final a = afr?[i];
      if (a != null) afrSum[idx] += a;
      final be = berr?[i];
      if (be != null) berrSum[idx] += be;
    }

    final res = MapResult(rows, cols);

    void fanTiming(int ri, int ci, double d, AnalyzerConfig cfg) {
      const fan = <List<num>>[
        [-1, 0, 0.5],
        [1, 0, 0.5],
        [0, -1, 0.5],
        [0, 1, 0.5],
        [-1, -1, 0.25],
        [-1, 1, 0.25],
        [1, -1, 0.25],
        [1, 1, 0.25],
      ];
      for (final e in fan) {
        final rr = ri + e[0].toInt(), cc = ci + e[1].toInt();
        if (rr < 0 || rr >= rows || cc < 0 || cc >= cols) continue;
        if (res.delta[rr][cc] != 0) continue;
        final d2 = _roundStep(d * e[2].toDouble(), cfg.timingStep);
        if (d2 == 0) continue;
        res.delta[rr][cc] = d2;
        res.info[rr][cc] = CellNote()..why = 'веер от соседнего кластера: ${d2.toStringAsFixed(1)}°';
      }
    }

    for (var ri = 0; ri < rows; ri++) {
      for (var ci = 0; ci < cols; ci++) {
        final idx = ri * cols + ci;
        final n = cnt[idx];
        if (n < cfg.minN) continue;
        final note = CellNote()
          ..n = n
          ..fbkc = fbkcMin[idx]
          ..flkc = flkcSum[idx] / n
          ..iat = iatMax[idx] < -900 ? null : iatMax[idx]
          ..iam = iamMin[idx] > 900 ? null : iamMin[idx]
          ..afr = afrSum[idx] > 0 ? afrSum[idx] / n : null
          ..berr = berrSum[idx] != 0 ? berrSum[idx] / n : null;

        if (kind == 'timing' || kind == 'knockadv') {
          if (note.iam != null && note.iam! < 0.99) {
            res.info[ri][ci] = note..why = 'IAM=${note.iam!.toStringAsFixed(2)} — сначала доучить';
            continue;
          }
          final lateCount = flkcLateCnt[idx];
          final lateFlkc = lateCount > 0 ? flkcLateSum[idx] / lateCount : note.flkc!;
          final knock = math.min(note.fbkc!, math.min(note.flkc!, lateFlkc) * 1.4);
          if (knock <= cfg.fbkcEvent) {
            final d = math.max(cfg.timingMaxCut,
                math.min(-cfg.timingStep, _roundStep(knock * 0.6, cfg.timingStep)));
            res.delta[ri][ci] = d;
            fanTiming(ri, ci, d, cfg);
            res.info[ri][ci] = note
              ..why = 'детон-кластер · FBKC ${note.fbkc!.toStringAsFixed(1)}°, '
                  'FLKC ${note.flkc!.toStringAsFixed(1)}° (поздняя ${lateFlkc.toStringAsFixed(1)}°) · '
                  'снять ${d.abs().toStringAsFixed(1)}° здесь, соседи сглажены веером';
          } else if (res.delta[ri][ci] == 0 &&
              cfg.allowTimingAdd &&
              n >= 18 &&
              (note.iat == null || note.iat! <= 45)) {
            res.delta[ri][ci] = cfg.timingStep;
            res.info[ri][ci] = note..why = 'чисто, IAM=1.0 — опционально +0.5°';
          }
        } else if (kind == 'fuel') {
          final loadAxisValue = xIsRpm ? g.y.values[ri] : g.x.values[ci];
          if (loadAxisValue < cfg.wotLoad || note.afr == null) continue;
          var target = g.data[ri][ci];
          if (target < 9.0) target *= 14.7;
          note.target = target;
          final err = note.afr! - target;
          final knock = math.min(note.fbkc!, note.flkc!);
          if (err > cfg.afrErr) {
            final d = math.max(cfg.afrMaxCut, _roundStep(-err, 0.1));
            res.delta[ri][ci] = d;
            res.info[ri][ci] = note
              ..why = 'факт ${note.afr!.toStringAsFixed(2)} против цели ${target.toStringAsFixed(2)} '
                  '— обогатить на ${d.abs().toStringAsFixed(1)}; если не помогает — MAF/давление';
          } else if (knock <= cfg.fbkcEvent) {
            res.delta[ri][ci] = -0.3;
            res.info[ri][ci] = note..why = 'детон при цели ~совпадает — запас −0.3 AFR';
          }
        } else {
          final e = note.berr;
          if (e == null) continue;
          if (kind == 'wgdc') {
            if (e.abs() > cfg.boostErr) {
              final d = _roundStep(-e * 45, 1)
                  .clamp(-cfg.wgdcMaxDelta, cfg.wgdcMaxDelta - 1)
                  .toDouble();
              if (d != 0) {
                res.delta[ri][ci] = d;
                res.info[ri][ci] = note
                  ..why = '${e > 0 ? 'овербуст' : 'недобор'} ${e.toStringAsFixed(2)} бар '
                      '→ WGDC ${d > 0 ? '+' : ''}${d.toStringAsFixed(0)}%';
              }
            }
          } else {
            if (e > cfg.boostErr) {
              res.info[ri][ci] = note
                ..why = 'овербуст ${e.toStringAsFixed(2)} бар — чинить через WGDC/TD, не таргет';
            }
            if (cfg.targetBoostGain > 0) {
              res.delta[ri][ci] = cfg.targetBoostGain;
              res.info[ri][ci] = note
                ..why = 'политика +${cfg.targetBoostGain} бар (после фикса WGDC и чистого детон-лога)';
            }
          }
        }
      }
    }
    return res;
  }

  Map<String, MapResult> run(Map<String, MapGrid> maps, LogData log) {
    final out = <String, MapResult>{};
    for (final e in maps.entries) {
      final r = analyzeMap(e.value, log);
      if (r != null) out[e.key] = r;
    }
    return out;
  }
}
"""

FILES["lib/maps_lab/map3d_view.dart"] = r"""
/// 3D-визуализатор: тепловая карта + жесты, которые НЕ отдаёт ListView.
library;

import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import 'maps_lab_core.dart';
import 'maps_lab_log.dart';
import 'maps_lab_page.dart' show getHeatmapColor;

/// v0.9 FIX. Почему 3D не вращалась: вертикальный скролл страницы использует
/// VerticalDragGestureRecognizer со слопом ~18px, а pan внутри — ~36px. Скролл
/// выигрывал арену раньше, и карта не получала ни одного события. Старый хак
/// (rejectGesture -> acceptGesture) в Flutter 3.35 уже не спасает: арена к этому
/// моменту разрешена в пользу скролла. Решение — забирать жест себе сразу при
/// касании поверхности карты. Scale-распознаватель заодно даёт щипковый зум.
class _EagerScaleGestureRecognizer extends ScaleGestureRecognizer {
  @override
  void addAllowedPointer(PointerDownEvent event) {
    super.addAllowedPointer(event);
    resolve(GestureDisposition.accepted);
  }
}

class Map3DView extends StatefulWidget {
  const Map3DView({
    super.key,
    required this.grid,
    this.result,
    this.selRi,
    this.selCi,
    this.onCell,
  });

  final MapGrid grid;
  final MapResult? result;
  final int? selRi;
  final int? selCi;
  final void Function(int ri, int ci)? onCell;

  @override
  State<Map3DView> createState() => Map3DViewState();
}

class Map3DViewState extends State<Map3DView> with SingleTickerProviderStateMixin {
  double _yaw = 0.95;
  double _pitch = 0.55;
  double _zoom = 1.0;
  bool _auto = true;
  late final AnimationController _ticker;
  final _painter = _SurfacePainter();

  @override
  void initState() {
    super.initState();
    _ticker = AnimationController(vsync: this, duration: const Duration(seconds: 1))
      ..addListener(() {
        if (_auto) {
          _yaw += 0.003;
          setState(() {});
        }
      })
      ..repeat();
  }

  @override
  void dispose() {
    _ticker.dispose();
    super.dispose();
  }

  double _zoomAtStart = 1.0;

  void _onScaleStart(ScaleStartDetails _) {
    _auto = false;
    _zoomAtStart = _zoom;
  }

  void _onScaleUpdate(ScaleUpdateDetails d) {
    setState(() {
      if (d.pointerCount > 1) {
        _zoom = (_zoomAtStart * d.scale).clamp(0.55, 2.4);
      }
      _yaw += d.focalPointDelta.dx * 0.01;
      _pitch = (_pitch + d.focalPointDelta.dy * 0.008).clamp(0.12, 1.35);
    });
  }

  void _resetView() {
    setState(() {
      _yaw = 0.95;
      _pitch = 0.55;
      _zoom = 1.0;
      _auto = true;
    });
  }

  // Арена целиком отдана вращению, поэтому тап/двойной тап считаем сами.
  Offset? _downAt;
  DateTime _downTime = DateTime.fromMillisecondsSinceEpoch(0);
  DateTime _lastTapTime = DateTime.fromMillisecondsSinceEpoch(0);

  void _onPointerDown(PointerDownEvent e) {
    _downAt = e.localPosition;
    _downTime = DateTime.now();
  }

  void _onPointerUp(PointerUpEvent e) {
    final start = _downAt;
    _downAt = null;
    if (start == null) return;
    final moved = (e.localPosition - start).distance;
    final heldMs = DateTime.now().difference(_downTime).inMilliseconds;
    if (moved > 12 || heldMs > 400) return;
    final now = DateTime.now();
    if (now.difference(_lastTapTime).inMilliseconds < 320) {
      _lastTapTime = DateTime.fromMillisecondsSinceEpoch(0);
      _resetView();
      return;
    }
    _lastTapTime = now;
    final hit = _painter.pick(e.localPosition);
    final onCell = widget.onCell;
    if (hit != null && onCell != null) onCell(hit.$1, hit.$2);
  }

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: [
        Listener(
          behavior: HitTestBehavior.opaque,
          onPointerDown: _onPointerDown,
          onPointerUp: _onPointerUp,
          child: RawGestureDetector(
            behavior: HitTestBehavior.opaque,
            gestures: <Type, GestureRecognizerFactory>{
              _EagerScaleGestureRecognizer:
                  GestureRecognizerFactoryWithHandlers<_EagerScaleGestureRecognizer>(
                () => _EagerScaleGestureRecognizer(),
                (_EagerScaleGestureRecognizer instance) {
                  instance
                    ..onStart = _onScaleStart
                    ..onUpdate = _onScaleUpdate;
                },
              ),
            },
            child: ClipRect(
              child: CustomPaint(
                painter: _painter
                  ..update(
                    grid: widget.grid,
                    result: widget.result,
                    yaw: _yaw,
                    pitch: _pitch,
                    zoom: _zoom,
                    selRi: widget.selRi,
                    selCi: widget.selCi,
                  ),
                size: Size.infinite,
              ),
            ),
          ),
        ),
        // кнопки зума 3D
        Positioned(
          right: 8,
          bottom: 8,
          child: Column(
            children: [
              _zBtn(Icons.add, () => setState(() => _zoom = (_zoom * 1.15).clamp(0.55, 2.4))),
              const SizedBox(height: 6),
              _zBtn(Icons.remove, () => setState(() => _zoom = (_zoom / 1.15).clamp(0.55, 2.4))),
              const SizedBox(height: 6),
              _zBtn(Icons.threed_rotation, () {
                setState(() {
                  _auto = !_auto;
                });
              }),
            ],
          ),
        ),
        const Positioned(
          left: 8,
          bottom: 8,
          child: Text('палец — поворот · щипок — зум · двойной тап — сброс',
              style: TextStyle(fontSize: 10, color: Colors.white38)),
        ),
      ],
    );
  }

  Widget _zBtn(IconData icon, VoidCallback onTap) {
    return Material(
      color: Colors.black54,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: Icon(icon, size: 18, color: Colors.white),
        ),
      ),
    );
  }
}

class _Proj {
  _Proj(this.sx, this.sy, this.depth);
  final double sx, sy, depth;
}

class _Quad {
  _Quad(this.ri, this.ci, this.pts, this.depth, this.cx, this.cy, this.sideA, this.sideB);
  final int ri, ci;
  final List<Offset> pts;
  final List<Offset> sideA, sideB;
  final double depth, cx, cy;
}

class _SurfacePainter extends CustomPainter {
  MapGrid? grid;
  MapResult? result;
  double yaw = 0.9, pitch = 0.62, zoom = 1.0;
  int? selRi, selCi;
  final List<_Quad> _quads = [];

  void update({
    required MapGrid grid,
    MapResult? result,
    required double yaw,
    required double pitch,
    required double zoom,
    int? selRi,
    int? selCi,
  }) {
    this.grid = grid;
    this.result = result;
    this.yaw = yaw;
    this.pitch = pitch;
    this.zoom = zoom;
    this.selRi = selRi;
    this.selCi = selCi;
  }

  (int, int)? pick(Offset p) {
    _Quad? best;
    var bd = 1e9;
    for (final q in _quads) {
      final d = math.sqrt(math.pow(q.cx - p.dx, 2) + math.pow(q.cy - p.dy, 2));
      if (d < 28 && d < bd) {
        bd = d;
        best = q;
      }
    }
    return best == null ? null : (best.ri, best.ci);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final g = grid;
    if (g == null || g.rows == 0 || g.cols == 0) return;
    _quads.clear();

    final rows = g.rows, cols = g.cols;
    final vmin = g.vmin, vmax = g.vmax;
    final scale = math.min(size.width, size.height) * 0.34 * zoom;
    final cx = size.width / 2, cy = size.height * 0.52;
    const cam = 3.4;

    _Proj proj(double x, double y, double z) {
      final x1 = x * math.cos(yaw) + z * math.sin(yaw);
      final z1 = -x * math.sin(yaw) + z * math.cos(yaw);
      final y2 = y * math.cos(pitch) - z1 * math.sin(pitch);
      final z2 = y * math.sin(pitch) + z1 * math.cos(pitch);
      final f = cam / (cam - z2);
      return _Proj(cx + x1 * scale * f, cy - y2 * scale * f, z2);
    }

    double hgt(double v) => 0.12 + ((v - vmin) / ((vmax - vmin) == 0 ? 1 : (vmax - vmin))) * 0.72;

    final floorPaint = Paint()
      ..color = const Color(0xFF1B2330)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    final floor = [proj(-1, 0, -1), proj(1, 0, -1), proj(1, 0, 1), proj(-1, 0, 1)];
    final fp = Path()
      ..moveTo(floor[0].sx, floor[0].sy)
      ..lineTo(floor[1].sx, floor[1].sy)
      ..lineTo(floor[2].sx, floor[2].sy)
      ..lineTo(floor[3].sx, floor[3].sy)
      ..close();
    canvas.drawPath(fp, floorPaint);

    for (var ri = 0; ri < rows; ri++) {
      for (var ci = 0; ci < cols; ci++) {
        final gz0 = (ri / math.max(1, rows - 1)) * 2 - 1;
        final gz1 = rows > 1 ? ((ri + 1) / (rows - 1)) * 2 - 1 : gz0;
        final gx0 = (ci / math.max(1, cols - 1)) * 2 - 1;
        final gx1 = cols > 1 ? ((ci + 1) / (cols - 1)) * 2 - 1 : gx0;
        const scx = 0.96;
        final x0 = gx0 + ((gx1 - gx0) * (1 - scx)) / 2;
        final x1 = gx1 - ((gx1 - gx0) * (1 - scx)) / 2;
        final z0 = gz0 + ((gz1 - gz0) * (1 - scx)) / 2;
        final z1 = gz1 - ((gz1 - gz0) * (1 - scx)) / 2;
        final h = hgt(g.data[ri][ci]);
        final p = [proj(x0, h, z0), proj(x1, h, z0), proj(x1, h, z1), proj(x0, h, z1)];
        final depth = (p[0].depth + p[1].depth + p[2].depth + p[3].depth) / 4;
        final sideA = [proj(x0, h, z1), proj(x1, h, z1), proj(x1, 0, z1), proj(x0, 0, z1)];
        final sideB = [proj(x1, h, z0), proj(x1, h, z1), proj(x1, 0, z1), proj(x1, 0, z0)];
        _quads.add(_Quad(
          ri,
          ci,
          [for (final q in p) Offset(q.sx, q.sy)],
          depth,
          (p[0].sx + p[2].sx) / 2,
          (p[0].sy + p[2].sy) / 2,
          [for (final q in sideA) Offset(q.sx, q.sy)],
          [for (final q in sideB) Offset(q.sx, q.sy)],
        ));
      }
    }

    _quads.sort((a, b) => a.depth.compareTo(b.depth));

    final sidePaint = Paint()..color = const Color.fromARGB(235, 10, 14, 20);
    final edgePaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 0.55
      ..color = const Color.fromARGB(120, 6, 8, 12);

    for (final q in _quads) {
      void poly(List<Offset> pts, Paint fill) {
        final path = Path()..moveTo(pts[0].dx, pts[0].dy);
        for (var i = 1; i < pts.length; i++) {
          path.lineTo(pts[i].dx, pts[i].dy);
        }
        path.close();
        canvas.drawPath(path, fill);
      }

      poly(q.sideA, sidePaint);
      poly(q.sideB, sidePaint);

      final v = g.data[q.ri][q.ci];
      final d = result?.delta[q.ri][q.ci] ?? 0;
      final sel = selRi == q.ri && selCi == q.ci;

      var cellColor = getHeatmapColor(v, vmin, vmax);
      if (sel) cellColor = Color.lerp(cellColor, Colors.white, 0.35)!;
      poly(q.pts, Paint()..color = cellColor);

      final topPath = Path()..moveTo(q.pts[0].dx, q.pts[0].dy);
      for (var i = 1; i < q.pts.length; i++) {
        topPath.lineTo(q.pts[i].dx, q.pts[i].dy);
      }
      topPath.close();

      if (sel) {
        canvas.drawPath(
            topPath,
            Paint()
              ..style = PaintingStyle.stroke
              ..strokeWidth = 2.0
              ..color = Colors.white);
      } else {
        canvas.drawPath(topPath, edgePaint);
      }

      if (d != 0) {
        canvas.drawCircle(Offset(q.cx, q.cy), 2.6, Paint()..color = Colors.lightBlueAccent);
      }

      // v0.9.1: маркер детонационного кластера прямо на поверхности карты.
      final note = result?.info[q.ri][q.ci];
      final knock = note != null && ((note.fbkc ?? 0) <= -1.0 || (note.flkc ?? 0) <= -1.0);
      if (knock) {
        canvas.drawCircle(
            Offset(q.cx, q.cy),
            4.4,
            Paint()
              ..style = PaintingStyle.stroke
              ..strokeWidth = 2.0
              ..color = const Color(0xFFFF5252));
        canvas.drawCircle(Offset(q.cx, q.cy), 1.7, Paint()..color = const Color(0xFFFF5252));
      }
    }
  }

  @override
  bool shouldRepaint(covariant _SurfacePainter oldDelegate) => true;
}
"""

FILES["lib/maps_lab/maps_lab_page.dart"] = r"""
/// Экран Map Lab: папки, локальные XML, heatmap, zoom 2D, 3D-rotate.
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:share_plus/share_plus.dart';

import 'map3d_view.dart';
import 'maps_lab_core.dart';
import 'maps_lab_log.dart';

/// Тепловая карта: зелёный → жёлтый → оранжевый → красный.
Color getHeatmapColor(double v, double min, double max) {
  if (max <= min) return const Color(0xFF4CAF50);
  final t = ((v - min) / (max - min)).clamp(0.0, 1.0);
  if (t < 0.33) {
    return Color.lerp(const Color(0xFF4CAF50), const Color(0xFFFFEB3B), t / 0.33)!;
  } else if (t < 0.66) {
    return Color.lerp(const Color(0xFFFFEB3B), const Color(0xFFFF9800), (t - 0.33) / 0.33)!;
  } else {
    return Color.lerp(const Color(0xFFFF9800), const Color(0xFFF44336), (t - 0.66) / 0.34)!;
  }
}

class MapLabPage extends StatefulWidget {
  const MapLabPage({super.key});

  @override
  State<MapLabPage> createState() => _MapLabPageState();
}

class _MapLabPageState extends State<MapLabPage> {
  static const _defKeys = ['A2TB100B', 'A2TB100K', '32BITBASE'];

  final _journal = <String>[];
  DefSet? _defs;
  Map<String, MapGrid>? _maps;
  LogData? _log;
  Map<String, MapResult>? _results;
  String? _mapName;
  int? _selRi, _selCi;
  bool _view3D = false;
  String _defsSource = '—';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _bootstrapDefs());
  }

  void _say(String s) {
    if (!mounted) return;
    setState(() => _journal.add('${TimeOfDay.now().format(context)}  $s'));
  }

  Future<Directory> _cacheDir() async {
    final docs = await getApplicationDocumentsDirectory();
    final dir = Directory('${docs.path}/defs');
    if (!dir.existsSync()) dir.createSync(recursive: true);
    return dir;
  }

  Future<Map<String, String>> _tryAssets() async {
    final out = <String, String>{};
    for (final k in _defKeys) {
      try {
        final s = await rootBundle.loadString('assets/defs/$k.xml');
        if (s.trim().length > 200) out[k] = s;
      } catch (_) {}
    }
    return out;
  }

  Future<Map<String, String>> _tryCache() async {
    final out = <String, String>{};
    final dir = await _cacheDir();
    for (final k in _defKeys) {
      final f = File('${dir.path}/$k.xml');
      if (f.existsSync() && f.lengthSync() > 200) {
        try {
          out[k] = await f.readAsString();
        } catch (_) {}
      }
    }
    return out;
  }

  Future<void> _saveCache(Map<String, String> xmlById) async {
    final dir = await _cacheDir();
    for (final e in xmlById.entries) {
      try {
        await File('${dir.path}/${e.key}.xml').writeAsString(e.value, flush: true);
      } catch (_) {}
    }
  }

  bool _applyDefs(Map<String, String> xmlById, String source) {
    if (xmlById.length < 3) return false;
    try {
      _defs = DefSet.build(xmlById, 'A2TB100B');
      _defsSource = source;
      _say('дефиниции [$source]: ${_defs!.chain.join(' → ')} · '
          'ecuid ${_defs!.meta.ecuid} · ${_defs!.tables.length} таблиц');
      return true;
    } catch (e) {
      _say('!! ошибка разбора дефиниций: $e');
      return false;
    }
  }

  Future<void> _bootstrapDefs() async {
    var xml = await _tryAssets();
    if (_applyDefs(xml, 'APK assets')) {
      await _saveCache(xml);
      setState(() {});
      return;
    }
    xml = await _tryCache();
    if (_applyDefs(xml, 'кэш телефона')) {
      setState(() {});
      return;
    }
    _say('!! дефиниций нет. Нажмите «0 · Дефиниции» и укажите папку с XML.');
    setState(() {});
  }

  Future<Map<String, String>> _scanDefsInDir(Directory root) async {
    final found = <String, File>{};
    bool matchName(String name, String key) {
      final n = name.toLowerCase();
      return n == '${key.toLowerCase()}.xml' || n.contains(key.toLowerCase());
    }

    void consider(File f) {
      final name = f.uri.pathSegments.last;
      for (final k in _defKeys) {
        if (found.containsKey(k)) continue;
        if (matchName(name, k) && f.lengthSync() > 200) found[k] = f;
      }
    }

    try {
      for (final e in root.listSync(followLinks: false)) {
        if (e is File && e.path.toLowerCase().endsWith('.xml')) consider(e);
      }
      for (final e in root.listSync(followLinks: false)) {
        if (e is! Directory) continue;
        try {
          for (final f in e.listSync(followLinks: false)) {
            if (f is File && f.path.toLowerCase().endsWith('.xml')) consider(f);
          }
        } catch (_) {}
      }
    } catch (_) {}

    final out = <String, String>{};
    for (final e in found.entries) {
      try {
        out[e.key] = await e.value.readAsString();
      } catch (_) {}
    }
    return out;
  }

  Future<void> _pickDefs() async {
    final result = await showModalBottomSheet<Object>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (ctx) => const _FilePickerSheet(
        extensions: ['.xml'],
        title: 'Дефиниции RomRaider (.xml)',
        allowPickFolderDefs: true,
      ),
    );
    if (result == null) return;

    var xml = <String, String>{};
    var source = 'локально';

    if (result is Directory) {
      xml = await _scanDefsInDir(result);
      source = 'папка ${result.path.split('/').last}';
      _say('сканирование ${result.path}: найдено ${xml.length}/3');
    } else if (result is File) {
      xml = await _scanDefsInDir(result.parent);
      if (xml.length < 3) {
        final name = result.uri.pathSegments.last.toUpperCase();
        for (final k in _defKeys) {
          if (name.contains(k)) {
            try {
              xml[k] = await result.readAsString();
            } catch (_) {}
          }
        }
      }
      source = 'файл+папка';
    } else if (result is Map) {
      xml = Map<String, String>.from(result);
    }

    if (xml.length < 3) {
      final miss = _defKeys.where((k) => !xml.containsKey(k)).join(', ');
      _say('!! не хватает XML: $miss');
      return;
    }

    if (_applyDefs(xml, source)) {
      await _saveCache(xml);
      setState(() {});
    }
  }

  Future<File?> _chooseFile(List<String> exts, String title) async {
    final r = await showModalBottomSheet<Object>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (ctx) => _FilePickerSheet(extensions: exts, title: title),
    );
    return r is File ? r : null;
  }

  Future<void> _pickRom() async {
    if (_defs == null) {
      _say('сначала загрузите дефиниции');
      return;
    }
    final f = await _chooseFile(['.bin', '.hex', '.rom'], 'Выберите прошивку (.bin)');
    if (f == null) return;
    try {
      final bytes = await f.readAsBytes();
      setState(() {
        _results = null;
        _selRi = _selCi = null;
      });
      final parser = RomParser(bytes);
      final idAddr = int.tryParse(_defs?.meta.internalIdAddress ?? '2000', radix: 16) ?? 0x2000;
      final romId = parser.readRomId(idAddr);
      final expected = _defs?.meta.internalIdString ?? '';
      final matched = expected.isEmpty || romId == expected;
      _say('ROM: ${f.uri.pathSegments.last} · ${(bytes.length / 1024).toStringAsFixed(0)} КБ · '
          'ID="$romId"${matched ? '' : '  (ожидался $expected)'}');
      _maps = parser.extractKeys(_defs!);
      _mapName = _maps!.keys.firstOrNull;
      _say('карт извлечено: ${_maps!.length} из ${RomParser.keyTables.length}');
      setState(() {});
    } catch (e) {
      _say('!! ошибка парсинга ROM: $e');
    }
  }

  Future<void> _pickLog() async {
    final f = await _chooseFile(['.csv', '.txt', '.log'], 'Выберите файл лога (.csv)');
    if (f == null) return;
    try {
      final text = utf8.decode(await f.readAsBytes(), allowMalformed: true);
      _log = LogData.parse(text);
      final health = LogAudit.check(_log!);
      _say('лог: ${f.uri.pathSegments.last} · ${_log!.rows} строк');
      for (final n in health.notes) {
        _say('   $n');
      }
      setState(() {});
    } catch (e) {
      _say('!! ошибка чтения лога: $e');
    }
  }

  void _runAnalysis() {
    if (_maps == null || _log == null) return;
    const cfg = AnalyzerConfig();
    _results = Analyzer(cfg).run(_maps!, _log!);
    var dec = 0, inc = 0;
    for (final r in _results!.values) {
      dec += r.dec;
      inc += r.inc;
    }
    _say('анализ: вердикты по ${_results!.length} картам · убавить $dec, прибавить $inc ячеек');
    setState(() {});
  }

  Future<void> _export() async {
    if (_maps == null || _results == null) return;
    final dir = await getApplicationDocumentsDirectory();
    final stamp = DateTime.now().toIso8601String().replaceAll(':', '-').split('.').first;
    final outDir = Directory('${dir.path}/maplab_$stamp')..createSync(recursive: true);
    final files = <XFile>[];
    final md = StringBuffer('# Map Lab A2TB100B · рекомендации\n');
    for (final e in _maps!.entries) {
      final g = e.value;
      final res = _results![e.key];
      if (res == null) continue;
      final head = '\t${g.x.values.map((v) => v.toStringAsFixed(2)).join('\t')}';
      final rows = <String>[
        for (var ri = 0; ri < g.rows; ri++)
          '${g.y.values[ri].toStringAsFixed(2)}\t${[
            for (var ci = 0; ci < g.cols; ci++)
              (g.data[ri][ci] + res.delta[ri][ci]).toStringAsFixed(3),
          ].join('\t')}',
      ];
      final tsv = '$head\n${rows.join('\n')}\n';
      final file = File('${outDir.path}/${e.key.replaceAll(RegExp(r'[/ ]'), '_')}_recommended.tsv')
        ..writeAsStringSync(tsv);
      files.add(XFile(file.path));
      md.writeln('\n## ${e.key} (${g.units})');
      for (var ri = 0; ri < g.rows; ri++) {
        for (var ci = 0; ci < g.cols; ci++) {
          final d = res.delta[ri][ci];
          if (d == 0) continue;
          final inf = res.info[ri][ci];
          md.writeln('- ${g.x.values[ci]} × ${g.y.values[ri]}: ${g.data[ri][ci]} → '
              '${(g.data[ri][ci] + d).toStringAsFixed(2)} (${d > 0 ? '+' : ''}$d)'
              '${inf != null && inf.why.isNotEmpty ? ' — ${inf.why}' : ''}');
        }
      }
    }
    final mf = File('${outDir.path}/recommendations.md')..writeAsStringSync(md.toString());
    files.insert(0, XFile(mf.path));
    _say('экспорт: ${files.length} файлов → ${outDir.path}');
    await Share.shareXFiles(files, text: 'Map Lab A2TB100B — рекомендации');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final map = _maps == null ? null : _maps![_mapName];
    MapResult? res;
    if (map != null) res = _results?[map.name];

    // ВАЖНО: карта в Expanded, а не внутри ListView — иначе 3D не крутится.
    return Scaffold(
      appBar: AppBar(
        title: const Text('Map Lab · A2TB100B'),
        actions: [
          if (_results != null)
            IconButton(icon: const Icon(Icons.ios_share), onPressed: _export, tooltip: 'Экспорт'),
        ],
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _stepBtn('0 · Дефиниции', Icons.folder_special, _pickDefs, ok: _defs != null),
                    _stepBtn('1 · Прошивка', Icons.memory, _pickRom, ok: _maps != null),
                    _stepBtn('2 · Лог', Icons.description, _pickLog, ok: _log != null),
                    _stepBtn('3 · Анализ', Icons.psychology,
                        (_maps != null && _log != null) ? _runAnalysis : null,
                        ok: _results != null),
                    _stepBtn('Экспорт', Icons.ios_share, _results != null ? _export : null),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  _defs != null
                      ? 'XML: $_defsSource · ${_defs!.meta.xmlid}'
                      : 'Нужны A2TB100B.xml, A2TB100K.xml, 32BITBASE.xml',
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: _defs != null ? Colors.greenAccent : Colors.amber,
                  ),
                ),
                if (_maps != null) ...[
                  const SizedBox(height: 8),
                  SizedBox(
                    height: 40,
                    child: ListView(
                      scrollDirection: Axis.horizontal,
                      children: [
                        for (final e in _maps!.entries)
                          Padding(
                            padding: const EdgeInsets.only(right: 8),
                            child: ChoiceChip(
                              selected: _mapName == e.key,
                              onSelected: (_) => setState(() {
                                _mapName = e.key;
                                _selRi = _selCi = null;
                              }),
                              label: Text(e.key, style: const TextStyle(fontSize: 11)),
                            ),
                          ),
                      ],
                    ),
                  ),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          map == null
                              ? ''
                              : '${map.name} · ${map.rows}×${map.cols}',
                          style: theme.textTheme.bodySmall,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      SegmentedButton<bool>(
                        segments: const [
                          ButtonSegment(value: false, icon: Icon(Icons.table_chart, size: 16), label: Text('2D')),
                          ButtonSegment(value: true, icon: Icon(Icons.threed_rotation, size: 16), label: Text('3D')),
                        ],
                        selected: {_view3D},
                        onSelectionChanged: (s) => setState(() => _view3D = s.first),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
          if (map != null)
            Expanded(
              flex: 5,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
                child: Card(
                  clipBehavior: Clip.antiAlias,
                  child: _view3D
                      ? Map3DView(
                          grid: map,
                          result: res,
                          selRi: _selRi,
                          selCi: _selCi,
                          onCell: (ri, ci) => setState(() {
                            _selRi = ri;
                            _selCi = ci;
                          }),
                        )
                      : _Table2D(
                          grid: map,
                          result: res,
                          selRi: _selRi,
                          selCi: _selCi,
                          onCell: (ri, ci) => setState(() {
                            _selRi = ri;
                            _selCi = ci;
                          }),
                        ),
                ),
              ),
            ),
          if (map != null && _selRi != null && _selCi != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: _inspector(map, res),
            ),
          if (map != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 4, 12, 0),
              child: Wrap(spacing: 12, runSpacing: 4, children: [
                _legend(const Color(0xFF4CAF50), 'низ'),
                _legend(const Color(0xFFFFEB3B), 'середина'),
                _legend(const Color(0xFFF44336), 'верх'),
                _legend(Colors.lightBlueAccent, 'правка'),
              ]),
            ),
          Expanded(
            flex: map == null ? 8 : 2,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('Журнал', style: theme.textTheme.titleSmall),
                  const SizedBox(height: 6),
                  Expanded(
                    child: Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.35),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: ListView(
                        children: [
                          for (final j in _journal)
                            Text(j, style: const TextStyle(fontFamily: 'monospace', fontSize: 11)),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _stepBtn(String label, IconData icon, VoidCallback? onTap, {bool ok = false}) {
    return FilledButton.tonalIcon(
      onPressed: onTap,
      icon: Icon(ok ? Icons.check_circle : icon, size: 18),
      label: Text(label, style: const TextStyle(fontSize: 12)),
    );
  }

  Widget _legend(Color c, String t) => Row(mainAxisSize: MainAxisSize.min, children: [
        Container(width: 12, height: 8, decoration: BoxDecoration(color: c, borderRadius: BorderRadius.circular(2))),
        const SizedBox(width: 6),
        Text(t, style: const TextStyle(fontSize: 10.5)),
      ]);

  Widget _inspector(MapGrid g, MapResult? res) {
    final ri = _selRi!, ci = _selCi!;
    final v = g.data[ri][ci];
    final d = res?.delta[ri][ci] ?? 0;
    final inf = res?.info[ri][ci];
    final col = d < 0 ? const Color(0xFFFF5C5C) : d > 0 ? const Color(0xFFA8FF3E) : null;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Ячейка: ${g.x.name}=${g.x.values[ci]} · ${g.y.name}=${g.y.values[ri]}',
              style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
          const SizedBox(height: 4),
          Row(children: [
            Text(v.toStringAsFixed(2),
                style: const TextStyle(
                    fontSize: 18, fontFamily: 'monospace', decoration: TextDecoration.lineThrough)),
            const SizedBox(width: 8),
            const Icon(Icons.arrow_forward, size: 16),
            const SizedBox(width: 8),
            Text('${(v + d).toStringAsFixed(2)} ${g.units}',
                style: TextStyle(fontSize: 22, fontFamily: 'monospace', color: col)),
          ]),
          if (inf != null && inf.why.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(inf.why, style: const TextStyle(fontSize: 12)),
            ),
        ]),
      ),
    );
  }
}

class _FilePickerSheet extends StatefulWidget {
  const _FilePickerSheet({
    required this.extensions,
    required this.title,
    this.allowPickFolderDefs = false,
  });
  final List<String> extensions;
  final String title;
  final bool allowPickFolderDefs;

  @override
  State<_FilePickerSheet> createState() => _FilePickerSheetState();
}

class _FilePickerSheetState extends State<_FilePickerSheet> {
  Directory _currentDir = Directory('/storage/emulated/0');
  bool _hasPermission = false;
  List<FileSystemEntity> _items = [];

  @override
  void initState() {
    super.initState();
    _checkPermissionAndRefresh();
  }

  Future<void> _checkPermissionAndRefresh() async {
    var ok = false;
    try {
      ok = await Permission.manageExternalStorage.isGranted;
    } catch (_) {}
    if (!ok) {
      final docs = await getApplicationDocumentsDirectory();
      _currentDir = docs;
    } else if (!_currentDir.existsSync()) {
      _currentDir = Directory('/storage/emulated/0');
    }
    setState(() => _hasPermission = ok);
    _refreshList();
  }

  Future<void> _requestPermission() async {
    try {
      await Permission.manageExternalStorage.request();
    } catch (_) {}
    try {
      await openAppSettings();
    } catch (_) {}
    _checkPermissionAndRefresh();
  }

  void _refreshList() {
    try {
      if (!_currentDir.existsSync()) {
        setState(() => _items = []);
        return;
      }
      final list = _currentDir.listSync(followLinks: false);
      final dirs = <Directory>[];
      final files = <File>[];
      for (final e in list) {
        final name = e.path.split('/').last;
        if (name.startsWith('.')) continue;
        if (e is Directory) {
          dirs.add(e);
        } else if (e is File) {
          if (widget.extensions.any((x) => name.toLowerCase().endsWith(x))) files.add(e);
        }
      }
      dirs.sort((a, b) => a.path.toLowerCase().compareTo(b.path.toLowerCase()));
      files.sort((a, b) => b.lastModifiedSync().compareTo(a.lastModifiedSync()));
      setState(() => _items = [...dirs, ...files]);
    } catch (_) {
      setState(() => _items = []);
    }
  }

  void _goUp() {
    final parent = _currentDir.parent;
    if (parent.path.length >= 4) {
      setState(() => _currentDir = parent);
      _refreshList();
    }
  }

  void _goTo(String path) {
    final d = Directory(path);
    if (d.existsSync()) {
      setState(() => _currentDir = d);
      _refreshList();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      height: MediaQuery.of(context).size.height * 0.85,
      decoration: const BoxDecoration(
        color: Color(0xFF141921),
        borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
            child: Row(
              children: [
                Expanded(
                  child: Text(widget.title, style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                ),
                IconButton(icon: const Icon(Icons.close), onPressed: () => Navigator.of(context).pop()),
              ],
            ),
          ),
          if (!_hasPermission)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: TextButton(onPressed: _requestPermission, child: const Text('РАЗРЕШИТЬ доступ к файлам')),
            ),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            child: Row(children: [
              _chip('Память', '/storage/emulated/0'),
              _chip('Downloads', '/storage/emulated/0/Download'),
              _chip('Telegram', '/storage/emulated/0/Telegram'),
              _chip('Documents', '/storage/emulated/0/Documents'),
            ]),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            color: Colors.black26,
            child: Row(
              children: [
                IconButton(icon: const Icon(Icons.arrow_upward, size: 18), onPressed: _goUp),
                Expanded(
                  child: Text(_currentDir.path,
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 11, color: Colors.cyanAccent),
                      overflow: TextOverflow.ellipsis),
                ),
                if (widget.allowPickFolderDefs)
                  FilledButton.tonal(
                    onPressed: () => Navigator.of(context).pop(_currentDir),
                    child: const Text('Взять XML из ЭТОЙ папки', style: TextStyle(fontSize: 11)),
                  ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: ListView.builder(
              itemCount: _items.length,
              itemBuilder: (ctx, i) {
                final item = _items[i];
                final name = item.path.split('/').last;
                if (item is Directory) {
                  return ListTile(
                    dense: true,
                    leading: const Icon(Icons.folder, color: Colors.amber),
                    title: Text(name),
                    onTap: () {
                      setState(() => _currentDir = item);
                      _refreshList();
                    },
                  );
                } else if (item is File) {
                  return ListTile(
                    dense: true,
                    leading: const Icon(Icons.insert_drive_file, color: Colors.lightBlueAccent),
                    title: Text(name, style: const TextStyle(fontFamily: 'monospace', fontSize: 13)),
                    onTap: () => Navigator.of(ctx).pop(item),
                  );
                }
                return const SizedBox.shrink();
              },
            ),
          ),
        ],
      ),
    );
  }

  Widget _chip(String label, String path) {
    return Padding(
      padding: const EdgeInsets.only(right: 6),
      child: ActionChip(
        visualDensity: VisualDensity.compact,
        label: Text(label, style: const TextStyle(fontSize: 11)),
        onPressed: () => _goTo(path),
      ),
    );
  }
}

/// 2D таблица с pinch-zoom и кнопками +/−.
class _Table2D extends StatefulWidget {
  const _Table2D({required this.grid, this.result, this.selRi, this.selCi, this.onCell});
  final MapGrid grid;
  final MapResult? result;
  final int? selRi, selCi;
  final void Function(int, int)? onCell;

  @override
  State<_Table2D> createState() => _Table2DState();
}

class _Table2DState extends State<_Table2D> {
  final _tc = TransformationController();
  static const _minS = 0.25;
  static const _maxS = 6.0;

  @override
  void dispose() {
    _tc.dispose();
    super.dispose();
  }

  void _zoomBy(double factor) {
    final m = _tc.value.clone();
    final s = m.getMaxScaleOnAxis();
    final next = (s * factor).clamp(_minS, _maxS);
    final f = next / s;
    // зум относительно центра видимой области
    final child = context.findRenderObject() as RenderBox?;
    if (child == null) {
      _tc.value = m.clone()..multiply(Matrix4.diagonal3Values(f, f, f));
      return;
    }
    final center = child.size.center(Offset.zero);
    final scene = _tc.toScene(center);
    m.multiply(Matrix4.translationValues(scene.dx, scene.dy, 0));
    m.multiply(Matrix4.diagonal3Values(f, f, f));
    m.multiply(Matrix4.translationValues(-scene.dx, -scene.dy, 0));
    _tc.value = m;
    setState(() {});
  }

  void _reset() {
    _tc.value = Matrix4.identity();
    setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final g = widget.grid;
    return Stack(
      children: [
        InteractiveViewer(
          transformationController: _tc,
          minScale: _minS,
          maxScale: _maxS,
          boundaryMargin: const EdgeInsets.all(200),
          constrained: false,
          panEnabled: true,
          scaleEnabled: true,
          child: Padding(
            padding: const EdgeInsets.all(8),
            child: Table(
              defaultColumnWidth: const IntrinsicColumnWidth(),
              children: [
                TableRow(children: [
                  _head('${g.y.guess}↓ ${g.x.guess}→'),
                  for (final v in g.x.values) _head(v.toStringAsFixed(0), accent: true),
                ]),
                for (var ri = 0; ri < g.rows; ri++)
                  TableRow(children: [
                    _head(g.y.values[ri].toStringAsFixed(2), accent: true),
                    for (var ci = 0; ci < g.cols; ci++) _cell(ri, ci),
                  ]),
              ],
            ),
          ),
        ),
        Positioned(
          right: 8,
          bottom: 8,
          child: Column(
            children: [
              _zBtn(Icons.add, () => _zoomBy(1.25)),
              const SizedBox(height: 6),
              _zBtn(Icons.remove, () => _zoomBy(1 / 1.25)),
              const SizedBox(height: 6),
              _zBtn(Icons.center_focus_strong, _reset),
            ],
          ),
        ),
        const Positioned(
          left: 8,
          bottom: 8,
          child: Text('pinch / кнопки = зум', style: TextStyle(fontSize: 10, color: Colors.white38)),
        ),
      ],
    );
  }

  Widget _zBtn(IconData icon, VoidCallback onTap) {
    return Material(
      color: Colors.black54,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(8),
          child: Icon(icon, size: 18, color: Colors.white),
        ),
      ),
    );
  }

  Widget _head(String t, {bool accent = false}) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        decoration: BoxDecoration(border: Border.all(color: Colors.white12)),
        child: Text(t,
            style: TextStyle(
                fontSize: 9.5,
                fontFamily: 'monospace',
                color: accent ? const Color(0xCC4BE1FF) : Colors.white54)),
      );

  Widget _cell(int ri, int ci) {
    final g = widget.grid;
    final v = g.data[ri][ci];
    final d = widget.result?.delta[ri][ci] ?? 0;
    final sel = widget.selRi == ri && widget.selCi == ci;
    final heat = getHeatmapColor(v, g.vmin, g.vmax).withValues(alpha: 0.55);
    final hasFix = d != 0;

    return InkWell(
      onTap: () => widget.onCell?.call(ri, ci),
      child: Container(
        constraints: const BoxConstraints(minWidth: 48, minHeight: 40),
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 3),
        decoration: BoxDecoration(
          color: heat,
          border: Border.all(
            color: sel
                ? Colors.white
                : hasFix
                    ? Colors.lightBlueAccent
                    : Colors.white12,
            width: sel ? 1.6 : (hasFix ? 1.2 : 0.5),
          ),
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Text(v.toStringAsFixed(2),
                style: const TextStyle(fontSize: 10, fontFamily: 'monospace', color: Colors.white)),
            if (hasFix)
              Text((v + d).toStringAsFixed(2),
                  style: const TextStyle(
                      fontSize: 11,
                      fontWeight: FontWeight.bold,
                      fontFamily: 'monospace',
                      color: Colors.lightBlueAccent)),
          ],
        ),
      ),
    );
  }
}
"""

FILES["test/maps_lab_test.dart"] = r"""
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:__PKG__/maps_lab/maps_lab_core.dart';
import 'package:__PKG__/maps_lab/maps_lab_log.dart';

void main() {
  group('SafeExpr', () {
    test('uint8 timing: (x*.3515625)-20', () {
      final f = SafeExpr.compile('(x*.3515625)-20');
      expect(f(100), closeTo(15.15625, 0.0001));
      expect(f(0), -20.0);
    });
    test('estimated AFR: 14.7/(1+x*.0078125)', () {
      final f = SafeExpr.compile('14.7/(1+x*.0078125)');
      expect(f(80), closeTo(9.0461, 0.001));
    });
    test('float identity + garbage guard', () {
      expect(SafeExpr.compile('x')(42.5), 42.5);
      expect(SafeExpr.compile('eval(x)+1')(3), 3);
    });
  });

  const baseXml = '''
<rom>
  <romid><xmlid>32BITBASE</xmlid></romid>
  <scaling name="Timing8" units="deg" toexpr="(x*.3515625)-20" frexpr="(x+20)/.3515625" format="%.2f" storagetype="uint8" endian="big"/>
  <scaling name="RPM" units="RPM" toexpr="x" frexpr="x" format="%.0f" storagetype="float" endian="big"/>
  <table name="Base Timing" category="Ignition" type="3D" level="4" scaling="Timing8">
    <table name="Engine Load" type="X Axis" elements="2" scaling="RPM"/>
    <table name="Engine Speed" type="Y Axis" elements="3" scaling="RPM"/>
  </table>
</rom>''';

  const derivedXml = '''
<rom>
  <romid><xmlid>TEST_ROM</xmlid><internalidaddress>2000</internalidaddress><internalidstring>TEST_ROM</internalidstring></romid>
  <include>32BITBASE</include>
  <table name="Base Timing" address="1000">
    <table name="X" address="2400" elements="4"/>
    <table name="Y" address="2500"/>
  </table>
</rom>''';

  test('merge include-цепочки: адрес у производного, скейлинг наследуется', () {
    final defs = DefSet.build({'32BITBASE': baseXml, 'TEST_ROM': derivedXml}, 'TEST_ROM');
    expect(defs.chain, ['32BITBASE', 'TEST_ROM']);
    final t = defs.tables['Base Timing']!;
    expect(t.dataAddress, 0x1000);
    expect(t.dataScaling!.units, 'deg');
    expect(t.axes[0].name, 'Engine Load');
    expect(t.axes[0].elements, 4);
    expect(t.axes[0].address, 0x2400);
    expect(t.axes[1].elements, 3);
    expect(defs.isReadable3D(t), isTrue);
  });

  test('ROM: декод float32-осей и uint8-данных big-endian', () {
    final rom = Uint8List(0x3000);
    final bd = ByteData.view(rom.buffer);
    for (var i = 0; i < 8; i++) {
      bd.setUint8(0x2000 + i, 'TEST_ROM'.codeUnitAt(i));
    }
    final xs = [1000.0, 2000.0, 3000.0, 4000.0];
    for (var i = 0; i < 4; i++) {
      bd.setFloat32(0x2400 + i * 4, xs[i], Endian.big);
    }
    final ys = [0.8, 1.6, 2.4];
    for (var i = 0; i < 3; i++) {
      bd.setFloat32(0x2500 + i * 4, ys[i], Endian.big);
    }
    for (var i = 0; i < 12; i++) {
      bd.setUint8(0x1000 + i, 100);
    }

    final defs = DefSet.build({'32BITBASE': baseXml, 'TEST_ROM': derivedXml}, 'TEST_ROM');
    final parser = RomParser(rom);
    expect(parser.readRomId(0x2000), 'TEST_ROM');
    final grid = parser.extract(defs.tables['Base Timing']!)!;
    expect(grid.rows, 3);
    expect(grid.cols, 4);
    expect(grid.x.values.last, 4000);
    expect(grid.y.values.first, closeTo(0.8, 0.001));
    expect(grid.data[2][3], closeTo(15.15625, 0.001));
    expect(grid.kind, 'timing');
  });

  group('LogData + Analyzer', () {
    LogData syntheticLog({required double fbkc}) {
      final rpm = <double?>[], load = <double?>[], f = <double?>[], fl = <double?>[];
      for (var i = 0; i < 600; i++) {
        rpm.add(4000 + (i % 80) - 40);
        load.add(3.0);
        f.add(fbkc);
        fl.add(fbkc * 0.4);
      }
      return LogData()
        ..cols['rpm'] = rpm
        ..cols['load'] = load
        ..cols['fbkc'] = f
        ..cols['flkc'] = fl;
    }

    MapGrid smallGrid() => MapGrid(
          name: 'Base Timing Primary Non-Cruise',
          kind: 'timing',
          units: 'deg',
          addr: 0,
          x: AxisVals('Engine Speed', [3000, 4000, 5000], 'rpm'),
          y: AxisVals('Engine Load', [2.0, 3.0], 'г/об·бар'),
          data: [
            [20, 18, 16],
            [16, 14, 12],
          ],
        );

    test('детон-кластер → отрицательная дельта в своей ячейке и веер сглаживания', () {
      final res = Analyzer(const AnalyzerConfig()).analyzeMap(smallGrid(), syntheticLog(fbkc: -4.0))!;
      final d = res.delta[1][1];
      expect(d, lessThan(0));
      expect(d, greaterThanOrEqualTo(-3.0));
      expect(res.delta[0][0], -0.5);
      expect(res.info[1][1]!.why, contains('детон'));
    });

    test('чистый лог при выключенном allowTimingAdd → ноль правок', () {
      final res = Analyzer(const AnalyzerConfig()).analyzeMap(smallGrid(), syntheticLog(fbkc: 0))!;
      final anyDelta = res.delta.expand((r) => r).any((v) => v != 0);
      expect(anyDelta, isFalse);
    });

    test('CSV: автоопределение ; и десятичной запятой, алиасы колонок', () {
      const csv = 'Time (s);Engine Speed (RPM);Feedback Knock Correction;Load_4B\r\n'
          '0,0;3000;0,0;3,00\r\n'
          '0,1;3100;-2,5;3,10\r\n';
      final log = LogData.parse(csv);
      expect(log['rpm']![1], 3100);
      expect(log['fbkc']![1], -2.5);
      expect(log['load']![1], 3.1);
    });
  });
}
"""

# ── pubspec / manifest / write ──────────────────────────────────────────────
pub = APP / "pubspec.yaml"
text = pub.read_text(encoding="utf-8")

if "assets/defs/" not in text:
    try:
        if re.search(r"(?m)^  assets:\s*$", text):
            text = re.sub(r"(?m)^(  assets:\s*\n)", r"\1    - assets/defs/\n", text, count=1)
        elif re.search(r"(?m)^flutter:\s*$", text):
            text = re.sub(r"(?m)^(flutter:\s*\n)", r"\1  assets:\n    - assets/defs/\n", text, count=1)
        else:
            text += "\nflutter:\n  uses-material-design: true\n  assets:\n    - assets/defs/\n"
        pub.write_text(text, encoding="utf-8")
        print("[pubspec] assets/defs добавлены")
    except Exception as e:
        print(f"[pubspec] {e}")

man = APP / "android/app/src/main/AndroidManifest.xml"
try:
    if man.exists():
        m = man.read_text(encoding="utf-8")
        if "MANAGE_EXTERNAL_STORAGE" not in m:
            m = m.replace(
                "<application",
                '    <uses-permission android:name="android.permission.MANAGE_EXTERNAL_STORAGE"/>\n    <application',
                1,
            )
            man.write_text(m, encoding="utf-8")
            print("[manifest] MANAGE_EXTERNAL_STORAGE добавлен")
except Exception as e:
    print(f"[manifest] {e}")

pkg_match = re.search(r"(?m)^name:\s*(\S+)", text)
PKG = pkg_match.group(1) if pkg_match else "subaru_ssm2_fixed"

for rel, src in FILES.items():
    dest = APP / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.replace("__PKG__", PKG) + "\n", encoding="utf-8")
    print("[dart]", rel)

RAW = "https://raw.githubusercontent.com/TD-D/SubaruDefs/Stable/ECUFlash/subaru%20metric"
DEFS = {
    "A2TB100B.xml": f"{RAW}/Legacy%20GT/A2TB100B.xml",
    "A2TB100K.xml": f"{RAW}/Legacy%20GT%20spec.B/A2TB100K.xml",
    "32BITBASE.xml": f"{RAW}/Bases/32BITBASE.xml",
}
defs_dir = APP / "assets/defs"
defs_dir.mkdir(parents=True, exist_ok=True)
for name, url in DEFS.items():
    dest = defs_dir / name
    if not dest.exists() or dest.stat().st_size < 1000:
        urllib.request.urlretrieve(url, dest)
    print("[asset]", name, f"{dest.stat().st_size/1024:.0f} КБ")

(APP / "lib/maplab_link.dart").write_text(
    "import 'maps_lab/maps_lab_page.dart';\n"
    "export 'maps_lab/maps_lab_page.dart' show MapLabPage;\n"
    "typedef MapLabTab = MapLabPage;\n",
    encoding="utf-8",
)
print("\nГотово: 3D-вращение (EagerPan) + зум 2D таблицы. Запускайте ячейку 3/3.")