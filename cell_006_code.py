# @title 04.2 | FULL | SSM2 0.10.2 >> 0.11.0 — ошибки + сервис + loggerdefs A2TB100B (вставить МЕЖДУ 04.1 и 05)
# Что делает:
#  1. lib/pids.dart: исходные 28 PID + 27 параметров из RomRaider logger.xml,
#     отобранных по ECU ID 5204584007 (A2TB100B): Requested Torque (0xFF8058),
#     Knock Sum, KC Advance 4B, Base Timing, LTFT A-D (FF24B4..CC), INJ PW и др.
#  2. lib/expr.dart — безопасный эвалуатор выражений logger-дефиниций.
#  3. lib/identity.dart — профиль A2TB100B с адресами из logger.xml.
#  4. lib/loggerdef.dart — полный каталог 5204584007 (91 std + 59 ext + 68 switches).
#  5. Диагностика: lib/diag.dart (SSM2 18/14, OBD 03/04, UDS 19 02/14 через
#     существующий ElmDriver.diagnostic) + вкладка «DTC» + справочник кодов.
#  6. Расширение белого списка команд (protocol.dart), пауза опроса (model.dart),
#     алиасы Map Lab, protocol_test (28 -> 55 PID), calid_import VALID.
# Идемпотентна: бэкап _backup_042_<ts>, SKIP при повторном прогоне, самопроверка.
# EXPERIMENTAL=False: ECUReset/Clear Memory заблокированы до стенда.

import json
import re
import shutil
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
EXPERIMENTAL = False  # @param {type:"boolean"}  # UDS 11 01 / Clear Memory 04 gr

STAMP = time.strftime("%Y%m%d_%H%M%S")
# Бэкап СТРОГО снаружи APP (в /content/), чтобы flutter analyze не сканировал его
BACKUP = APP.parent / f"ssm2_backup_042_{STAMP}"
FILES = {}
REWRITES = []  # (path, needle, replacement)

# ===== Безопасный эвалуатор выражений logger-дефиниций =====
FILES["lib/expr.dart"] = r'''
import 'dart:typed_data';

/// Мини-эвалуатор выражений logger-дефиниций RomRaider: + - * / скобки,
/// числа, идентификатор x (= сырое значение из памяти ECU).
/// Покрывает все expr из SubaruDefs logger.xml: 'x', 'x*K', '(x*100)-100',
/// '14.7/(1+x)', 'x/.84', 'x+6' и т.п. Безопасен: никаких вызовов/имён, кроме x.
class ExprEval {
  ExprEval._(this.src);
  final String src;
  int pos = 0;

  static double? eval(String expr, double x) {
    try {
      final p = ExprEval._(expr);
      final v = p._expr(x);
      p._ws();
      if (p.pos != expr.length) return null;
      return v.isFinite ? v : null;
    } catch (_) {
      return null;
    }
  }

  void _ws() {
    while (pos < src.length && src[pos] == ' ') pos++;
  }

  bool _eat(String ch) {
    _ws();
    if (pos < src.length && src[pos] == ch) {
      pos++;
      return true;
    }
    return false;
  }

  double _expr(double x) {
    var v = _term(x);
    while (true) {
      if (_eat('+')) {
        v = v + _term(x);
      } else if (_eat('-')) {
        v = v - _term(x);
      } else {
        return v;
      }
    }
  }

  double _term(double x) {
    var v = _factor(x);
    while (true) {
      if (_eat('*')) {
        v = v * _factor(x);
      } else if (_eat('/')) {
        final d = _factor(x);
        if (d == 0) return double.nan;
        v = v / d;
      } else {
        return v;
      }
    }
  }

  double _factor(double x) {
    _ws();
    if (_eat('-')) return -_factor(x);
    if (_eat('(')) {
      final v = _expr(x);
      _eat(')');
      return v;
    }
    _ws();
    if (pos < src.length && src[pos] == 'x') {
      pos++;
      return x;
    }
    final start = pos;
    while (pos < src.length &&
        (src.codeUnitAt(pos) >= 0x30 && src.codeUnitAt(pos) <= 0x39 || src[pos] == '.')) {
      pos++;
    }
    if (start == pos) throw StateError('ожидалось число в "$src"');
    return double.parse(src.substring(start, pos));
  }
}

/// сырой X из байтов ответа по длине параметра
double exprRawX(List<int> bytes, Endian endian) {
  if (bytes.length == 4) {
    return ByteData.sublistView(Uint8List.fromList(bytes)).getFloat32(0, endian);
  }
  if (bytes.length == 2) return (bytes[0] * 256 + bytes[1]).toDouble();
  return bytes[0].toDouble();
}
'''

# ===== Библиотека 55 PID: 28 исходных + 27 loggerdef · REQ_TQ @ 0xFF8058 =====
FILES["lib/pids.dart"] = r'''
// v0.11 (патч 04.2): исходные 28 PID из ячейки 02 + 27 параметров logger-дефиниций
// RomRaider SubaruDefs Stable, отфильтрованных по ECU ID 5204584007 (A2TB100B).
// Новые параметры декодируются через ExprEval (exprText), float32 BE/LE — автозонд engine.

import 'dart:typed_data';

import 'expr.dart';

typedef PidFormula = double Function(List<int> bytes);

class SubaruPidDef {
  const SubaruPidDef({
    required this.id,
    required this.desc,
    required this.unit,
    required this.category,
    required this.address,
    required this.bytesCount,
    required this.priority,
    required this.formulaText,
    required this.minValue,
    required this.maxValue,
    required this.digits,
    this.formula,
    this.floatFactor,
    this.exprText,
  });

  final String id, desc, unit, category, formulaText;
  final int address, bytesCount, priority, digits;
  final double minValue, maxValue;
  final PidFormula? formula;
  final double? floatFactor;

  /// v0.11: выражение loggerdef (x = raw из ECU). Задано у ROM-зависимых PID.
  final String? exprText;

  String get name => id;

  /// ROM-зависимый (расширенный) параметр — под CALID-гейтом опроса.
  bool get extended => floatFactor != null || exprText != null;

  List<int> get addresses => List<int>.generate(bytesCount, (i) => address + i);

  double? decode(List<int> bytes, {Endian endian = Endian.big}) {
    if (bytes.length != bytesCount || bytes.any((b) => b < 0 || b > 255)) {
      return null;
    }
    final expr = exprText;
    final factor = floatFactor;
    final double value;
    if (expr != null) {
      final v = ExprEval.eval(expr, exprRawX(bytes, endian));
      if (v == null) return null;
      value = v;
    } else if (factor == null) {
      value = formula!(bytes);
    } else {
      value = ByteData.sublistView(Uint8List.fromList(bytes)).getFloat32(0, endian) * factor;
    }
    return value.isFinite ? value : null;
  }
}

class SubaruPidLibrary {
  static final List<SubaruPidDef> all = <SubaruPidDef>[
    SubaruPidDef(
      id: 'LOAD',
      desc: "Engine Load (Relative)",
      unit: '%',
      category: 'engine',
      address: 0x000007,
      bytesCount: 1,
      priority: 1,
      formulaText: "A*100/255",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 1,
      formula: (b) => b[0]*100/255,),
    SubaruPidDef(
      id: 'ECT',
      desc: "Coolant Temperature",
      unit: 'C',
      category: 'temp',
      address: 0x000008,
      bytesCount: 1,
      priority: 1,
      formulaText: "A-40",
      minValue: -40.0,
      maxValue: 130.0,
      digits: 0,
      formula: (b) => b[0]-40.0,),
    SubaruPidDef(
      id: 'STFT',
      desc: "A/F Correction #1",
      unit: '%',
      category: 'fuel',
      address: 0x000009,
      bytesCount: 1,
      priority: 1,
      formulaText: "(A-128)*100/128",
      minValue: -100.0,
      maxValue: 100.0,
      digits: 2,
      formula: (b) => (b[0]-128)*100/128,),
    SubaruPidDef(
      id: 'LTFT',
      desc: "A/F Learning #1",
      unit: '%',
      category: 'fuel',
      address: 0x00000A,
      bytesCount: 1,
      priority: 1,
      formulaText: "(A-128)*100/128",
      minValue: -100.0,
      maxValue: 100.0,
      digits: 2,
      formula: (b) => (b[0]-128)*100/128,),
    SubaruPidDef(
      id: 'MAP_ABS',
      desc: "Manifold Absolute Pressure",
      unit: 'bar',
      category: 'air',
      address: 0x00000D,
      bytesCount: 1,
      priority: 2,
      formulaText: "A*37/255/14.50377",
      minValue: 0.0,
      maxValue: 3.0,
      digits: 3,
      formula: (b) => b[0]*37/255/14.50377,),
    SubaruPidDef(
      id: 'RPM',
      desc: "Engine Speed",
      unit: 'rpm',
      category: 'engine',
      address: 0x00000E,
      bytesCount: 2,
      priority: 1,
      formulaText: "(A*256+B)/4",
      minValue: 0.0,
      maxValue: 8000.0,
      digits: 0,
      formula: (b) => (b[0]*256+b[1])/4,),
    SubaruPidDef(
      id: 'SPEED',
      desc: "Vehicle Speed",
      unit: 'kph',
      category: 'engine',
      address: 0x000010,
      bytesCount: 1,
      priority: 1,
      formulaText: "A",
      minValue: 0.0,
      maxValue: 240.0,
      digits: 0,
      formula: (b) => b[0].toDouble(),),
    SubaruPidDef(
      id: 'TIMING',
      desc: "Total Ignition Timing",
      unit: 'degrees',
      category: 'ignition',
      address: 0x000011,
      bytesCount: 1,
      priority: 1,
      formulaText: "(A-128)/2",
      minValue: -64.0,
      maxValue: 64.0,
      digits: 1,
      formula: (b) => (b[0]-128)/2,),
    SubaruPidDef(
      id: 'IAT',
      desc: "Intake Air Temperature",
      unit: 'C',
      category: 'temp',
      address: 0x000012,
      bytesCount: 1,
      priority: 1,
      formulaText: "A-40",
      minValue: -40.0,
      maxValue: 130.0,
      digits: 0,
      formula: (b) => b[0]-40.0,),
    SubaruPidDef(
      id: 'MAF',
      desc: "Mass Airflow",
      unit: 'g/s',
      category: 'air',
      address: 0x000013,
      bytesCount: 2,
      priority: 1,
      formulaText: "(A*256+B)/100",
      minValue: 0.0,
      maxValue: 400.0,
      digits: 2,
      formula: (b) => (b[0]*256+b[1])/100,),
    SubaruPidDef(
      id: 'TPS',
      desc: "Throttle Opening Angle",
      unit: '%',
      category: 'throttle',
      address: 0x000015,
      bytesCount: 1,
      priority: 1,
      formulaText: "A*100/255",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 1,
      formula: (b) => b[0]*100/255,),
    SubaruPidDef(
      id: 'O2_F',
      desc: "Front O2 #1",
      unit: 'V',
      category: 'fuel',
      address: 0x000016,
      bytesCount: 2,
      priority: 3,
      formulaText: "(A*256+B)/200",
      minValue: 0.0,
      maxValue: 2.0,
      digits: 2,
      formula: (b) => (b[0]*256+b[1])/200.0,),
    SubaruPidDef(
      id: 'BATT',
      desc: "Battery Voltage",
      unit: 'V',
      category: 'electric',
      address: 0x00001C,
      bytesCount: 1,
      priority: 2,
      formulaText: "A*8/100",
      minValue: 8.0,
      maxValue: 18.0,
      digits: 2,
      formula: (b) => b[0]*8/100,),
    SubaruPidDef(
      id: 'KNOCK_ADV',
      desc: "Knock Correction Advance",
      unit: 'degrees',
      category: 'ignition',
      address: 0x000022,
      bytesCount: 1,
      priority: 1,
      formulaText: "(A-128)/2",
      minValue: -64.0,
      maxValue: 64.0,
      digits: 1,
      formula: (b) => (b[0]-128)/2,),
    SubaruPidDef(
      id: 'BARO',
      desc: "Atmospheric Pressure",
      unit: 'bar',
      category: 'air',
      address: 0x000023,
      bytesCount: 1,
      priority: 3,
      formulaText: "A*37/255/14.50377",
      minValue: 0.0,
      maxValue: 2.0,
      digits: 3,
      formula: (b) => b[0]*37/255/14.50377,),
    SubaruPidDef(
      id: 'MAP_REL',
      desc: "Manifold Relative Pressure",
      unit: 'bar',
      category: 'turbo',
      address: 0x000024,
      bytesCount: 1,
      priority: 1,
      formulaText: "(A-128)*37/255/14.50377",
      minValue: -1.3,
      maxValue: 1.3,
      digits: 3,
      formula: (b) => (b[0]-128)*37/255/14.50377,),
    SubaruPidDef(
      id: 'PEDAL',
      desc: "Accelerator Pedal Angle",
      unit: '%',
      category: 'throttle',
      address: 0x000029,
      bytesCount: 1,
      priority: 1,
      formulaText: "A*100/255",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 1,
      formula: (b) => b[0]*100/255,),
    SubaruPidDef(
      id: 'WG_PRIM',
      desc: "Primary Wastegate Duty Cycle",
      unit: '%',
      category: 'turbo',
      address: 0x000030,
      bytesCount: 1,
      priority: 1,
      formulaText: "A*100/255",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 1,
      formula: (b) => b[0]*100/255,),
    SubaruPidDef(
      id: 'AFR',
      desc: "A/F Sensor #1",
      unit: 'AFR',
      category: 'fuel',
      address: 0x000046,
      bytesCount: 1,
      priority: 1,
      formulaText: "A/128*14.7",
      minValue: 0.0,
      maxValue: 30.0,
      digits: 2,
      formula: (b) => b[0]/128*14.7,),
    SubaruPidDef(
      id: 'GEAR',
      desc: "Gear Position",
      unit: 'gear',
      category: 'engine',
      address: 0x00004A,
      bytesCount: 1,
      priority: 2,
      formulaText: "A+1",
      minValue: 1.0,
      maxValue: 8.0,
      digits: 0,
      formula: (b) => b[0]+1.0,),
    SubaruPidDef(
      id: 'IAM',
      desc: "IAM (4-byte)*",
      unit: 'multiplier',
      category: 'ignition',
      address: 0xFF2538,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32",
      minValue: 0.0,
      maxValue: 1.0,
      digits: 3,
      floatFactor: 1,),
    SubaruPidDef(
      id: 'LOAD_4B',
      desc: "Engine Load (4-Byte)*",
      unit: 'g/rev',
      category: 'engine',
      address: 0xFF6C9C,
      bytesCount: 4,
      priority: 2,
      formulaText: "float32",
      minValue: 0.0,
      maxValue: 5.0,
      digits: 3,
      floatFactor: 1,),
    SubaruPidDef(
      id: 'BOOST_ERR',
      desc: "Boost Error*",
      unit: 'bar',
      category: 'turbo',
      address: 0xFF6450,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32*0.001333224",
      minValue: -2.0,
      maxValue: 3.0,
      digits: 3,
      floatFactor: 0.001333224,),
    SubaruPidDef(
      id: 'BOOST_TGT',
      desc: "Target Boost (4-byte)*",
      unit: 'bar',
      category: 'turbo',
      address: 0xFF6454,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32*0.001333224",
      minValue: -2.0,
      maxValue: 3.0,
      digits: 3,
      floatFactor: 0.001333224,),
    SubaruPidDef(
      id: 'FBKC',
      desc: "Feedback Knock Correction (4-byte)*",
      unit: 'degrees',
      category: 'ignition',
      address: 0xFF7D4C,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32",
      minValue: -20.0,
      maxValue: 20.0,
      digits: 2,
      floatFactor: 1,),
    SubaruPidDef(
      id: 'FKL',
      desc: "Fine Learning Knock Correction*",
      unit: 'degrees',
      category: 'ignition',
      address: 0xFF7DD0,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32",
      minValue: -20.0,
      maxValue: 20.0,
      digits: 2,
      floatFactor: 1,),
    SubaruPidDef(
      id: 'BOOST',
      desc: "MRP (Boost) (4-byte)*",
      unit: 'bar',
      category: 'turbo',
      address: 0xFF6AE0,
      bytesCount: 4,
      priority: 1,
      formulaText: "float32*0.001333224",
      minValue: -2.0,
      maxValue: 3.0,
      digits: 3,
      floatFactor: 0.001333224,),
    SubaruPidDef(
      id: 'CL_TARGET',
      desc: "Closed Loop Fuel Target*",
      unit: 'AFR',
      category: 'fuel',
      address: 0xFF73B4,
      bytesCount: 4,
      priority: 2,
      formulaText: "float32*14.7",
      minValue: 0.0,
      maxValue: 30.0,
      digits: 2,
      floatFactor: 14.7,),
    // --- v0.11: logger-дефиниции RomRaider для ECU 5204584007 (A2TB100B) ---
    SubaruPidDef(
      id: 'REQ_TQ',
      desc: "Requested Torque*",
      unit: 'Nm*',
      category: 'engine',
      address: 0xFF8058,
      bytesCount: 4,
      priority: 1,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 500.0,
      digits: 1,
      exprText: "x"),
    SubaruPidDef(
      id: 'KC_ADV_4B',
      desc: "Knock Correction Advance (4-byte)*",
      unit: '°',
      category: 'ignition',
      address: 0xFF7D48,
      bytesCount: 4,
      priority: 1,
      formulaText: "x",
      minValue: -64.0,
      maxValue: 64.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'KC_IAM',
      desc: "Knock Correction Advance (IAM only)*",
      unit: '°',
      category: 'ignition',
      address: 0xFF7DB0,
      bytesCount: 4,
      priority: 2,
      formulaText: "x",
      minValue: -64.0,
      maxValue: 64.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'BTIMING',
      desc: "Ignition Base Timing*",
      unit: '°',
      category: 'ignition',
      address: 0xFF7B98,
      bytesCount: 4,
      priority: 2,
      formulaText: "x",
      minValue: -64.0,
      maxValue: 64.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'KCMAX',
      desc: "Knock Correction Advance Max Primary*",
      unit: '°',
      category: 'ignition',
      address: 0xFF7DA8,
      bytesCount: 4,
      priority: 2,
      formulaText: "x",
      minValue: -20.0,
      maxValue: 70.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'KNOCK_SUM',
      desc: "Knock Sum*",
      unit: 'cnt',
      category: 'ignition',
      address: 0xFF7D24,
      bytesCount: 4,
      priority: 2,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 10000.0,
      digits: 0,
      exprText: "x"),
    SubaruPidDef(
      id: 'AFL_A',
      desc: "A/F Learning #1 A (Stored)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF24B4,
      bytesCount: 4,
      priority: 2,
      formulaText: "x*100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "x*100"),
    SubaruPidDef(
      id: 'AFL_B',
      desc: "A/F Learning #1 B (Stored)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF24BC,
      bytesCount: 4,
      priority: 2,
      formulaText: "x*100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "x*100"),
    SubaruPidDef(
      id: 'AFL_C',
      desc: "A/F Learning #1 C (Stored)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF24C4,
      bytesCount: 4,
      priority: 2,
      formulaText: "x*100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "x*100"),
    SubaruPidDef(
      id: 'AFL_D',
      desc: "A/F Learning #1 D (Stored)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF24CC,
      bytesCount: 4,
      priority: 2,
      formulaText: "x*100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "x*100"),
    SubaruPidDef(
      id: 'AFL_4B',
      desc: "A/F Learning #1 (4-byte)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF768C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x*100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "x*100"),
    SubaruPidDef(
      id: 'AFCOR1_4B',
      desc: "A/F Correction #1 (4-byte)*",
      unit: '%',
      category: 'fuel',
      address: 0xFF73A4,
      bytesCount: 4,
      priority: 2,
      formulaText: "(x*100)-100",
      minValue: -50.0,
      maxValue: 50.0,
      digits: 2,
      exprText: "(x*100)-100"),
    SubaruPidDef(
      id: 'TIPIN',
      desc: "Tip-in Throttle*",
      unit: '%',
      category: 'throttle',
      address: 0xFF6B8C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x",
      minValue: -10.0,
      maxValue: 120.0,
      digits: 1,
      exprText: "x"),
    SubaruPidDef(
      id: 'TTH_TGT',
      desc: "Target Throttle Plate Position*",
      unit: '%',
      category: 'throttle',
      address: 0xFF8040,
      bytesCount: 4,
      priority: 2,
      formulaText: "x/.84",
      minValue: 0.0,
      maxValue: 110.0,
      digits: 1,
      exprText: "x/.84"),
    SubaruPidDef(
      id: 'THROTTLE_4B',
      desc: "Throttle Plate Opening Angle (4-byte)*",
      unit: '%',
      category: 'throttle',
      address: 0xFF6B7C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x/.84",
      minValue: 0.0,
      maxValue: 110.0,
      digits: 1,
      exprText: "x/.84"),
    SubaruPidDef(
      id: 'CLOL',
      desc: "CL/OL Fueling*",
      unit: 'st',
      category: 'fuel',
      address: 0xFF9679,
      bytesCount: 4,
      priority: 3,
      formulaText: "x+6",
      minValue: 0.0,
      maxValue: 16.0,
      digits: 0,
      exprText: "x+6"),
    SubaruPidDef(
      id: 'FL_OFFSET',
      desc: "Fine Learning Table Offset*",
      unit: 'idx',
      category: 'ignition',
      address: 0xFF7DD6,
      bytesCount: 4,
      priority: 3,
      formulaText: "x+1",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 0,
      exprText: "x+1"),
    SubaruPidDef(
      id: 'GEAR_CALC',
      desc: "Gear (Calculated)*",
      unit: 'gear',
      category: 'engine',
      address: 0xFF7079,
      bytesCount: 4,
      priority: 2,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 8.0,
      digits: 0,
      exprText: "x"),
    SubaruPidDef(
      id: 'TDIG_INT',
      desc: "Turbo Dynamics Integral (4-byte)*",
      unit: '%',
      category: 'turbo',
      address: 0xFF645C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'TDIG_PROP',
      desc: "Turbo Dynamics Proportional (4-byte)*",
      unit: '%',
      category: 'turbo',
      address: 0xFF6458,
      bytesCount: 4,
      priority: 3,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 100.0,
      digits: 2,
      exprText: "x"),
    SubaruPidDef(
      id: 'INJPW',
      desc: "Fuel Injector #1 Pulse Width (4-byte)*",
      unit: 'ms',
      category: 'fuel',
      address: 0xFF7A48,
      bytesCount: 4,
      priority: 2,
      formulaText: "x*.001",
      minValue: 0.0,
      maxValue: 40.0,
      digits: 3,
      exprText: "x*.001"),
    SubaruPidDef(
      id: 'INJ_LAT',
      desc: "Fuel Injector #1 Latency (4-byte)*",
      unit: 'ms',
      category: 'fuel',
      address: 0xFF7A5C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x*.001",
      minValue: 0.0,
      maxValue: 10.0,
      digits: 3,
      exprText: "x*.001"),
    SubaruPidDef(
      id: 'OLE_ENRICH',
      desc: "Primary Open Loop Map Enrichment (4-byte)*",
      unit: 'AFR',
      category: 'fuel',
      address: 0xFF7778,
      bytesCount: 4,
      priority: 3,
      formulaText: "14.7/(1+x)",
      minValue: 5.0,
      maxValue: 30.0,
      digits: 2,
      exprText: "14.7/(1+x)"),
    SubaruPidDef(
      id: 'ENRICH_FINAL',
      desc: "Primary Enrichment Final (4-byte)*",
      unit: 'AFR',
      category: 'fuel',
      address: 0xFF731C,
      bytesCount: 4,
      priority: 2,
      formulaText: "14.7/(1+x)",
      minValue: 5.0,
      maxValue: 30.0,
      digits: 2,
      exprText: "14.7/(1+x)"),
    SubaruPidDef(
      id: 'FUELBASE',
      desc: "Final Fueling Base (4-byte)*",
      unit: 'AFR',
      category: 'fuel',
      address: 0xFF72EC,
      bytesCount: 4,
      priority: 3,
      formulaText: "14.7/x",
      minValue: 5.0,
      maxValue: 30.0,
      digits: 2,
      exprText: "14.7/x"),
    SubaruPidDef(
      id: 'MAP_4B',
      desc: "Manifold Absolute Pressure (4-byte)*",
      unit: 'bar',
      category: 'air',
      address: 0xFF6ADC,
      bytesCount: 4,
      priority: 3,
      formulaText: "x*0.001333224",
      minValue: 0.0,
      maxValue: 3.0,
      digits: 3,
      exprText: "x*0.001333224"),
    SubaruPidDef(
      id: 'TBOOST_REL',
      desc: "Target Boost Relative (4-byte)*",
      unit: 'bar',
      category: 'turbo',
      address: 0xFF649C,
      bytesCount: 4,
      priority: 3,
      formulaText: "x*0.001333224",
      minValue: -2.0,
      maxValue: 2.0,
      digits: 3,
      exprText: "x*0.001333224"),
    SubaruPidDef(
      id: 'IDLE_SEL',
      desc: "Idle Speed Map Selection*",
      unit: 'raw',
      category: 'engine',
      address: 0xFF8408,
      bytesCount: 4,
      priority: 3,
      formulaText: "x",
      minValue: 0.0,
      maxValue: 16.0,
      digits: 0,
      exprText: "x"),
    SubaruPidDef(
      id: 'AFL_RANGE',
      desc: "A/F Learning Airflow Range (Current)*",
      unit: 'rng',
      category: 'fuel',
      address: 0xFF7695,
      bytesCount: 4,
      priority: 3,
      formulaText: "x+1",
      minValue: 0.0,
      maxValue: 10.0,
      digits: 0,
      exprText: "x+1"),
  ];

  static SubaruPidDef byId(String id) => all.firstWhere((p) => p.id == id);
  static const Set<String> defaults = {
    'RPM',
    'ECT',
    'TIMING',
    'MAF',
    'TPS',
    'BATT',
    'MAP_REL',
    'AFR',
  };
}
'''

# ===== Профиль A2TB100B: все адреса подтверждены по logger.xml =====
FILES["lib/identity.dart"] = r'''
// v0.11 (патч 04.2): профиль A2TB100B расширен адресами из logger-дефиниций
// RomRaider SubaruDefs Stable, отфильтрованных по ECU ID 5204584007.
// Источник подтверждён: RomRaider/logger/metric/logger.xml (GitHub, ветка Stable).

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
      'Legacy GT (BP/BL) EJ20X JDM 2008 · TD04HL-19T · Dual AVCS · 5EAT (logger-defs verified)',
      <String, String>{
        'IAM': '0xFF2538',
        'LOAD_4B': '0xFF6C9C',
        'BOOST_ERR': '0xFF6450',
        'BOOST_TGT': '0xFF6454',
        'FBKC': '0xFF7D4C',
        'FKL': '0xFF7DD0',
        'BOOST': '0xFF6AE0',
        'CL_TARGET': '0xFF73B4',
        'REQ_TQ': '0xFF8058',
        'KC_ADV_4B': '0xFF7D48',
        'KC_IAM': '0xFF7DB0',
        'BTIMING': '0xFF7B98',
        'KCMAX': '0xFF7DA8',
        'KNOCK_SUM': '0xFF7D24',
        'AFL_A': '0xFF24B4',
        'AFL_B': '0xFF24BC',
        'AFL_C': '0xFF24C4',
        'AFL_D': '0xFF24CC',
        'AFL_4B': '0xFF768C',
        'AFCOR1_4B': '0xFF73A4',
        'TIPIN': '0xFF6B8C',
        'TTH_TGT': '0xFF8040',
        'THROTTLE_4B': '0xFF6B7C',
        'CLOL': '0xFF9679',
        'FL_OFFSET': '0xFF7DD6',
        'GEAR_CALC': '0xFF7079',
        'TDIG_INT': '0xFF645C',
        'TDIG_PROP': '0xFF6458',
        'INJPW': '0xFF7A48',
        'INJ_LAT': '0xFF7A5C',
        'OLE_ENRICH': '0xFF7778',
        'ENRICH_FINAL': '0xFF731C',
        'FUELBASE': '0xFF72EC',
        'MAP_4B': '0xFF6ADC',
        'TBOOST_REL': '0xFF649C',
        'IDLE_SEL': '0xFF8408',
        'AFL_RANGE': '0xFF7695',
      },
      'ECU defs: A2TB100B -> A2TB100K -> 32BITBASE (TD-D/SubaruDefs); '
      'logger.xml @ SubaruDefs Stable, ecu=5204584007'),
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

# ===== Полный каталог ECU 5204584007: 91 std + 59 ext + 68 switches =====
FILES["lib/loggerdef.dart"] = r'''
// v0.11 (патч 04.2): полный каталог параметров logger-дефиниций RomRaider
// для ECU ID 5204584007 (A2TB100B). Источник: RomRaider/SubaruDefs Stable,
// RomRaider/logger/metric/logger.xml. Используется для справки/подбора PID;
// активная библиотека опроса — lib/pids.dart.

class LoggerdefParam {
  const LoggerdefParam({
    required this.pid,
    required this.name,
    required this.address,
    required this.length,
    required this.units,
    required this.expr,
  });
  final String pid, name, address;
  final int length;
  final String units, expr;
  bool get isExtended => address.toUpperCase().startsWith('0XFF');
}

class LoggerdefSwitch {
  const LoggerdefSwitch({
    required this.sid,
    required this.name,
    required this.byte,
    required this.bit,
  });
  final String sid, name, byte;
  final int bit;
}

/// Стандартные параметры SSM (байтовые адреса), поддерживаемые этим ECU.
const List<LoggerdefParam> loggerdefParams = <LoggerdefParam>[
  const LoggerdefParam(
      pid: 'P1', name: "Engine Load (Relative)",
      address: '0x000007', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P2', name: "Coolant Temperature",
      address: '0x000008', length: 1,
      units: "C", expr: "x-40"),
  const LoggerdefParam(
      pid: 'P3', name: "A/F Correction #1",
      address: '0x000009', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P4', name: "A/F Learning #1",
      address: '0x00000A', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P5', name: "A/F Correction #2",
      address: '0x00000B', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P6', name: "A/F Learning #2",
      address: '0x00000C', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P7', name: "Manifold Absolute Pressure",
      address: '0x00000D', length: 1,
      units: "bar", expr: "x*37/255/14.50377"),
  const LoggerdefParam(
      pid: 'P8', name: "Engine Speed",
      address: '0x00000E', length: 2,
      units: "rpm", expr: "x/4"),
  const LoggerdefParam(
      pid: 'P9', name: "Vehicle Speed",
      address: '0x000010', length: 1,
      units: "kph", expr: "x"),
  const LoggerdefParam(
      pid: 'P10', name: "Ignition Total Timing",
      address: '0x000011', length: 1,
      units: "degrees", expr: "(x-128)/2"),
  const LoggerdefParam(
      pid: 'P11', name: "Intake Air Temperature",
      address: '0x000012', length: 1,
      units: "C", expr: "x-40"),
  const LoggerdefParam(
      pid: 'P12', name: "Mass Airflow",
      address: '0x000013', length: 2,
      units: "g/s", expr: "x/100"),
  const LoggerdefParam(
      pid: 'P13', name: "Throttle Opening Angle",
      address: '0x000015', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P14', name: "Front O2 Sensor #1",
      address: '0x000016', length: 2,
      units: "V", expr: "x/200"),
  const LoggerdefParam(
      pid: 'P15', name: "Rear O2 Sensor",
      address: '0x000018', length: 2,
      units: "V", expr: "x/200"),
  const LoggerdefParam(
      pid: 'P16', name: "Front O2 Sensor #2",
      address: '0x00001A', length: 2,
      units: "V", expr: "x/200"),
  const LoggerdefParam(
      pid: 'P17', name: "Battery Voltage",
      address: '0x00001C', length: 1,
      units: "V", expr: "x*8/100"),
  const LoggerdefParam(
      pid: 'P18', name: "Mass Airflow Sensor Voltage",
      address: '0x00001D', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P19', name: "Throttle Sensor Voltage",
      address: '0x00001E', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P20', name: "Differential Pressure Sensor Voltage",
      address: '0x00001F', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P21', name: "Fuel Injector #1 Pulse Width",
      address: '0x000020', length: 1,
      units: "ms", expr: "x*256/1000"),
  const LoggerdefParam(
      pid: 'P22', name: "Fuel Injector #2 Pulse Width",
      address: '0x000021', length: 1,
      units: "ms", expr: "x*256/1000"),
  const LoggerdefParam(
      pid: 'P23', name: "Knock Correction Advance",
      address: '0x000022', length: 1,
      units: "degrees", expr: "(x-128)/2"),
  const LoggerdefParam(
      pid: 'P24', name: "Atmospheric Pressure",
      address: '0x000023', length: 1,
      units: "bar", expr: "x*37/255/14.50377"),
  const LoggerdefParam(
      pid: 'P25', name: "Manifold Relative Pressure",
      address: '0x000024', length: 1,
      units: "bar", expr: "(x-128)*37/255/14.50377"),
  const LoggerdefParam(
      pid: 'P26', name: "Pressure Differential Sensor",
      address: '0x000025', length: 1,
      units: "bar", expr: "(x-128)*37/255/14.50377"),
  const LoggerdefParam(
      pid: 'P27', name: "Fuel Tank Pressure",
      address: '0x000026', length: 1,
      units: "bar", expr: "(x-128)*35/10000/14.50377"),
  const LoggerdefParam(
      pid: 'P28', name: "CO Adjustment",
      address: '0x000027', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P29', name: "Learned Ignition Timing",
      address: '0x000028', length: 1,
      units: "degrees", expr: "(x-128)/2"),
  const LoggerdefParam(
      pid: 'P30', name: "Accelerator Pedal Angle",
      address: '0x000029', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P31', name: "Fuel Temperature",
      address: '0x00002A', length: 1,
      units: "C", expr: "x-40"),
  const LoggerdefParam(
      pid: 'P32', name: "Front O2 Heater Current #1",
      address: '0x00002B', length: 1,
      units: "A", expr: "x*1004/25600"),
  const LoggerdefParam(
      pid: 'P33', name: "Rear O2 Heater Current",
      address: '0x00002C', length: 1,
      units: "A", expr: "x*1004/25600"),
  const LoggerdefParam(
      pid: 'P34', name: "Front O2 Heater Current #2",
      address: '0x00002D', length: 1,
      units: "A", expr: "x*1004/25600"),
  const LoggerdefParam(
      pid: 'P35', name: "Fuel Level",
      address: '0x00002E', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P36', name: "Primary Wastegate Duty Cycle",
      address: '0x000030', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P37', name: "Secondary Wastegate Duty Cycle",
      address: '0x000031', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P38', name: "CPC Valve Duty Ratio",
      address: '0x000032', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P39', name: "Tumble Valve Position Sensor Right",
      address: '0x000033', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P40', name: "Tumble Valve Position Sensor Left",
      address: '0x000034', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P41', name: "Idle Speed Control Valve Duty Ratio",
      address: '0x000035', length: 1,
      units: "%", expr: "x/2"),
  const LoggerdefParam(
      pid: 'P42', name: "A/F Lean Correction",
      address: '0x000036', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P43', name: "A/F Heater Duty",
      address: '0x000037', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P44', name: "Idle Speed Control Valve Step",
      address: '0x000038', length: 1,
      units: "steps", expr: "x"),
  const LoggerdefParam(
      pid: 'P45', name: "Number of Exh. Gas Recirc. Steps",
      address: '0x000039', length: 1,
      units: "steps", expr: "x"),
  const LoggerdefParam(
      pid: 'P46', name: "Alternator Duty",
      address: '0x00003A', length: 1,
      units: "%", expr: "x"),
  const LoggerdefParam(
      pid: 'P47', name: "Fuel Pump Duty",
      address: '0x00003B', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P48', name: "Intake VVT Advance Angle Right",
      address: '0x00003C', length: 1,
      units: "degrees", expr: "x-50"),
  const LoggerdefParam(
      pid: 'P49', name: "Intake VVT Advance Angle Left",
      address: '0x00003D', length: 1,
      units: "degrees", expr: "x-50"),
  const LoggerdefParam(
      pid: 'P50', name: "Intake OCV Duty Right",
      address: '0x00003E', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P51', name: "Intake OCV Duty Left",
      address: '0x00003F', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P52', name: "Intake OCV Current Right",
      address: '0x000040', length: 1,
      units: "mA", expr: "x/32"),
  const LoggerdefParam(
      pid: 'P53', name: "Intake OCV Current Left",
      address: '0x000041', length: 1,
      units: "mA", expr: "x/32"),
  const LoggerdefParam(
      pid: 'P54', name: "A/F Sensor #1 Current",
      address: '0x000042', length: 1,
      units: "mA", expr: "(x-128)/8"),
  const LoggerdefParam(
      pid: 'P55', name: "A/F Sensor #2 Current",
      address: '0x000043', length: 1,
      units: "mA", expr: "(x-128)/8"),
  const LoggerdefParam(
      pid: 'P56', name: "A/F Sensor #1 Resistance",
      address: '0x000044', length: 1,
      units: "ohms", expr: "x"),
  const LoggerdefParam(
      pid: 'P57', name: "A/F Sensor #2 Resistance",
      address: '0x000045', length: 1,
      units: "ohms", expr: "x"),
  const LoggerdefParam(
      pid: 'P58', name: "A/F Sensor #1",
      address: '0x000046', length: 1,
      units: "AFR", expr: "x/128*14.7"),
  const LoggerdefParam(
      pid: 'P59', name: "A/F Sensor #2",
      address: '0x000047', length: 1,
      units: "AFR", expr: "x/128*14.7"),
  const LoggerdefParam(
      pid: 'P60', name: "Gear Position",
      address: '0x00004A', length: 1,
      units: "gear", expr: "x+1"),
  const LoggerdefParam(
      pid: 'P61', name: "A/F Sensor #1 Heater Current",
      address: '0x000053', length: 1,
      units: "A", expr: "x/10"),
  const LoggerdefParam(
      pid: 'P62', name: "A/F Sensor #2 Heater Current",
      address: '0x000054', length: 1,
      units: "A", expr: "x/10"),
  const LoggerdefParam(
      pid: 'P63', name: "Roughness Monitor Cylinder #1",
      address: '0x0000CE', length: 1,
      units: "misfire count", expr: "x"),
  const LoggerdefParam(
      pid: 'P64', name: "Roughness Monitor Cylinder #2",
      address: '0x0000CF', length: 1,
      units: "misfire count", expr: "x"),
  const LoggerdefParam(
      pid: 'P65', name: "A/F Correction #3 (16-bit ECU)",
      address: '0x0000D0', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P66', name: "A/F Learning #3",
      address: '0x0000D1', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P67', name: "Rear O2 Heater Voltage",
      address: '0x0000D2', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P68', name: "A/F Adjustment Voltage",
      address: '0x0000D3', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P69', name: "Roughness Monitor Cylinder #3",
      address: '0x0000D8', length: 1,
      units: "misfire count", expr: "x"),
  const LoggerdefParam(
      pid: 'P70', name: "Roughness Monitor Cylinder #4",
      address: '0x0000D9', length: 1,
      units: "misfire count", expr: "x"),
  const LoggerdefParam(
      pid: 'P71', name: "Throttle Motor Duty",
      address: '0x0000FA', length: 1,
      units: "%", expr: "(x-128)*100/128"),
  const LoggerdefParam(
      pid: 'P72', name: "Throttle Motor Voltage",
      address: '0x0000FB', length: 1,
      units: "V", expr: "x*8/100"),
  const LoggerdefParam(
      pid: 'P73', name: "Sub Throttle Sensor",
      address: '0x000100', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P74', name: "Main Throttle Sensor",
      address: '0x000101', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P75', name: "Sub Accelerator Sensor",
      address: '0x000102', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P76', name: "Main Accelerator Sensor",
      address: '0x000103', length: 1,
      units: "V", expr: "x/50"),
  const LoggerdefParam(
      pid: 'P77', name: "Brake Booster Pressure",
      address: '0x000104', length: 1,
      units: "bar", expr: "x*37/255/14.50377"),
  const LoggerdefParam(
      pid: 'P78', name: "Fuel Pressure (High)",
      address: '0x000105', length: 1,
      units: "bar", expr: "x/25*10"),
  const LoggerdefParam(
      pid: 'P79', name: "Exhaust Gas Temperature",
      address: '0x000106', length: 1,
      units: "C", expr: "(x+40)*5"),
  const LoggerdefParam(
      pid: 'P80', name: "Cold Start Injector (Air Pump)",
      address: '0x000108', length: 1,
      units: "ms", expr: "x*256/1000"),
  const LoggerdefParam(
      pid: 'P81', name: "SCV Step",
      address: '0x000109', length: 1,
      units: "steps", expr: "x"),
  const LoggerdefParam(
      pid: 'P82', name: "Memorised Cruise Speed",
      address: '0x00010A', length: 1,
      units: "kph", expr: "x"),
  const LoggerdefParam(
      pid: 'P83', name: "Exhaust VVT Advance Angle Right",
      address: '0x000118', length: 1,
      units: "degrees", expr: "x-50"),
  const LoggerdefParam(
      pid: 'P84', name: "Exhaust VVT Advance Angle Left",
      address: '0x000119', length: 1,
      units: "degrees", expr: "x-50"),
  const LoggerdefParam(
      pid: 'P85', name: "Exhaust OCV Duty Right",
      address: '0x00011A', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P86', name: "Exhaust OCV Duty Left",
      address: '0x00011B', length: 1,
      units: "%", expr: "x*100/255"),
  const LoggerdefParam(
      pid: 'P87', name: "Exhaust OCV Current Right",
      address: '0x00011C', length: 1,
      units: "mA", expr: "x*32"),
  const LoggerdefParam(
      pid: 'P88', name: "Exhaust OCV Current Left",
      address: '0x00011D', length: 1,
      units: "mA", expr: "x*32"),
  const LoggerdefParam(
      pid: 'P89', name: "A/F Correction #3 (32-bit ECU)",
      address: '0x0000D0', length: 1,
      units: "%", expr: "(x*.078125)-5"),
  const LoggerdefParam(
      pid: 'P90', name: "IAM",
      address: '0x0000F9', length: 1,
      units: "multiplier", expr: "x/16"),
  const LoggerdefParam(
      pid: 'P91', name: "Fine Learning Knock Correction",
      address: '0x000199', length: 1,
      units: "degrees", expr: "(x*0.25)-32"),
];

/// Расширенные параметры (RAM float16/32, адрес персонален для 5204584007).
const List<LoggerdefParam> loggerdefExt = <LoggerdefParam>[
  const LoggerdefParam(
      pid: 'E31', name: "IAM (4-byte)*",
      address: '0xFF2538', length: 4,
      units: "multiplier", expr: "x"),
  const LoggerdefParam(
      pid: 'E32', name: "Engine Load (4-Byte)*",
      address: '0xFF6C9C', length: 4,
      units: "g/rev", expr: "x"),
  const LoggerdefParam(
      pid: 'E33', name: "CL/OL Fueling*",
      address: '0xFF9679', length: 4,
      units: "status", expr: "x+6"),
  const LoggerdefParam(
      pid: 'E34', name: "Turbo Dynamics Integral (4-byte)*",
      address: '0xFF645C', length: 4,
      units: "absolute %", expr: "x"),
  const LoggerdefParam(
      pid: 'E35', name: "Boost Error*",
      address: '0xFF6450', length: 4,
      units: "bar", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E36', name: "Target Boost (4-byte)*",
      address: '0xFF6454', length: 4,
      units: "bar absolute", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E37', name: "Turbo Dynamics Proportional (4-byte)*",
      address: '0xFF6458', length: 4,
      units: "absolute %", expr: "x"),
  const LoggerdefParam(
      pid: 'E38', name: "Throttle Plate Opening Angle (4-byte)*",
      address: '0xFF6B7C', length: 4,
      units: "%", expr: "x/.84"),
  const LoggerdefParam(
      pid: 'E39', name: "Feedback Knock Correction (4-byte)*",
      address: '0xFF7D4C', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E40', name: "Knock Correction Advance (IAM only)*",
      address: '0xFF7DB0', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E41', name: "Fine Learning Knock Correction (4-byte)*",
      address: '0xFF7DD0', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E43', name: "Knock Correction Advance (4-byte)*",
      address: '0xFF7D48', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E44', name: "A/F Learning #1 A (Stored)*",
      address: '0xFF24B4', length: 4,
      units: "%", expr: "x*100"),
  const LoggerdefParam(
      pid: 'E45', name: "A/F Learning #1 B (Stored)*",
      address: '0xFF24BC', length: 4,
      units: "%", expr: "x*100"),
  const LoggerdefParam(
      pid: 'E46', name: "A/F Learning #1 C (Stored)*",
      address: '0xFF24C4', length: 4,
      units: "%", expr: "x*100"),
  const LoggerdefParam(
      pid: 'E47', name: "A/F Learning #1 D (Stored)*",
      address: '0xFF24CC', length: 4,
      units: "%", expr: "x*100"),
  const LoggerdefParam(
      pid: 'E48', name: "A/F Learning #1 (4-byte)*",
      address: '0xFF768C', length: 4,
      units: "%", expr: "x*100"),
  const LoggerdefParam(
      pid: 'E49', name: "Idle Speed Map Selection*",
      address: '0xFF8408', length: 4,
      units: "raw ecu value", expr: "x"),
  const LoggerdefParam(
      pid: 'E50', name: "Fuel Injector #1 Latency (4-byte)*",
      address: '0xFF7A5C', length: 4,
      units: "ms", expr: "x*.001"),
  const LoggerdefParam(
      pid: 'E51', name: "Manifold Absolute Pressure (4-byte)*",
      address: '0xFF6ADC', length: 4,
      units: "bar absolute", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E52', name: "Manifold Relative Sea Level Pressure (4-byte)*",
      address: '0xFF6ADC', length: 4,
      units: "bar relative sea level", expr: "(x-760)*0.001333224"),
  const LoggerdefParam(
      pid: 'E53', name: "Ignition Base Timing*",
      address: '0xFF7B98', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E54', name: "Tip-in Throttle*",
      address: '0xFF6B8C', length: 4,
      units: "%", expr: "x"),
  const LoggerdefParam(
      pid: 'E55', name: "Tip-in Enrichment (Last Calculated)*",
      address: '0xFF7924', length: 4,
      units: "raw ecu value", expr: "x"),
  const LoggerdefParam(
      pid: 'E56', name: "Requested Torque*",
      address: '0xFF8058', length: 4,
      units: "raw ecu value", expr: "x"),
  const LoggerdefParam(
      pid: 'E57', name: "Target Throttle Plate Position*",
      address: '0xFF8040', length: 4,
      units: "%", expr: "x/.84"),
  const LoggerdefParam(
      pid: 'E58', name: "Fine Learning Table Offset*",
      address: '0xFF7DD6', length: 4,
      units: "index position", expr: "x+1"),
  const LoggerdefParam(
      pid: 'E59', name: "Gear (Calculated)*",
      address: '0xFF7079', length: 4,
      units: "position", expr: "x"),
  const LoggerdefParam(
      pid: 'E60', name: "Fuel Injector #1 Pulse Width (4-byte)*",
      address: '0xFF7A48', length: 4,
      units: "ms", expr: "x*.001"),
  const LoggerdefParam(
      pid: 'E61', name: "A/F Learning Airflow Range (Current)*",
      address: '0xFF7695', length: 4,
      units: "offset", expr: "x+1"),
  const LoggerdefParam(
      pid: 'E70', name: "Primary Wastegate Duty Maximum* (4-byte)*",
      address: '0xFF6478', length: 4,
      units: "%", expr: "x"),
  const LoggerdefParam(
      pid: 'E77', name: "Primary Wastegate Duty Maximum* (4-byte)*",
      address: '0xFF6478', length: 4,
      units: "%", expr: "x"),
  const LoggerdefParam(
      pid: 'E78', name: "Primary Wastegate Duty Maximum* (2-byte)**",
      address: '0xFF708C', length: 2,
      units: "%", expr: "x*.00390625"),
  const LoggerdefParam(
      pid: 'E79', name: "Turbo Dynamics Integral (2-byte)**",
      address: '0xFF708E', length: 2,
      units: "absolute %", expr: "(x*.00390625)-50"),
  const LoggerdefParam(
      pid: 'E80', name: "Turbo Dynamics Proportional (2-byte)**",
      address: '0xFF7090', length: 2,
      units: "absolute %", expr: "(x*.00390625)-50"),
  const LoggerdefParam(
      pid: 'E81', name: "A/F Correction #1 (4-byte)*",
      address: '0xFF73A4', length: 4,
      units: "%", expr: "(x*100)-100"),
  const LoggerdefParam(
      pid: 'E82', name: "A/F Correction #1 (2-byte)**",
      address: '0xFF7094', length: 2,
      units: "%", expr: "(x*.01220703)-100"),
  const LoggerdefParam(
      pid: 'E83', name: "A/F Learning #1 (2-byte)**",
      address: '0xFF7098', length: 2,
      units: "%", expr: "(x*.04882812)-50"),
  const LoggerdefParam(
      pid: 'E84', name: "Primary Open Loop Map Enrichment (4-byte)*",
      address: '0xFF7778', length: 4,
      units: "estimated AFR", expr: "14.7/(1+x)"),
  const LoggerdefParam(
      pid: 'E85', name: "Primary Open Loop Map Enrichment (2-byte)**",
      address: '0xFF709A', length: 2,
      units: "estimated AFR", expr: "14.7/(1+(x*.00003051758))"),
  const LoggerdefParam(
      pid: 'E87', name: "Engine Load (2-byte)**",
      address: '0xFF70A0', length: 2,
      units: "g/rev", expr: "x*.00006103516"),
  const LoggerdefParam(
      pid: 'E88', name: "Manifold Absolute Pressure (2-byte)**",
      address: '0xFF70AE', length: 2,
      units: "bar absolute", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E89', name: "Manifold Relative Sea Level Pressure (2-byte)**",
      address: '0xFF70AE', length: 2,
      units: "bar relative sea level", expr: "(x-760)*0.001333224"),
  const LoggerdefParam(
      pid: 'E90', name: "Target Boost (2-byte)**",
      address: '0xFF70B2', length: 2,
      units: "bar absolute", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E91', name: "A/F Sensor #1 (4-byte)*",
      address: '0xFF6DD4', length: 4,
      units: "estimated AFR", expr: "x*14.7"),
  const LoggerdefParam(
      pid: 'E92', name: "A/F Sensor #1 (2-byte)**",
      address: '0xFF70B4', length: 2,
      units: "estimated AFR", expr: "(x*.0001220703)*14.7"),
  const LoggerdefParam(
      pid: 'E93', name: "Throttle Plate Opening Angle (2-byte)**",
      address: '0xFF70BC', length: 2,
      units: "%", expr: "x*0.0022706535"),
  const LoggerdefParam(
      pid: 'E94', name: "Feedback Knock Correction (1-byte)**",
      address: '0xFF70C6', length: 4,
      units: "degrees", expr: "(x*.3515625)-45"),
  const LoggerdefParam(
      pid: 'E95', name: "Fine Learning Knock Correction (1-byte)**",
      address: '0xFF70C9', length: 4,
      units: "degrees", expr: "(x*.3515625)-45"),
  const LoggerdefParam(
      pid: 'E96', name: "IAM (1-byte)**",
      address: '0xFF70CB', length: 4,
      units: "multiplier", expr: "x*.0625"),
  const LoggerdefParam(
      pid: 'E113', name: "Manifold Relative Pressure (4-byte)*",
      address: '0xFF6AE0', length: 4,
      units: "bar relative", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E114', name: "Knock Sum*",
      address: '0xFF7D24', length: 4,
      units: "count", expr: "x"),
  const LoggerdefParam(
      pid: 'E115', name: "Primary Enrichment Final (4-byte)*",
      address: '0xFF731C', length: 4,
      units: "estimated AFR", expr: "14.7/(1+x)"),
  const LoggerdefParam(
      pid: 'E118', name: "Knock Correction Advance Max Primary*",
      address: '0xFF7DA8', length: 4,
      units: "degrees", expr: "x"),
  const LoggerdefParam(
      pid: 'E120', name: "Target Boost Relative (4-byte)*",
      address: '0xFF649C', length: 4,
      units: "bar relative", expr: "x*0.001333224"),
  const LoggerdefParam(
      pid: 'E121', name: "Closed Loop Fueling Target (4-byte)*",
      address: '0xFF73B4', length: 4,
      units: "estimated AFR", expr: "x*14.7"),
  const LoggerdefParam(
      pid: 'E122', name: "Closed Loop Fueling Target (2-byte)*",
      address: '0xFF70BA', length: 2,
      units: "estimated AFR", expr: "x*.001794433"),
  const LoggerdefParam(
      pid: 'E123', name: "Final Fueling Base (4-byte)*",
      address: '0xFF72EC', length: 4,
      units: "estimated AFR", expr: "14.7/x"),
  const LoggerdefParam(
      pid: 'E124', name: "Final Fueling Base (2-byte)*",
      address: '0xFF709E', length: 2,
      units: "estimated AFR", expr: "14.7/(x*.0004882812)"),
];

/// Битовые флаги состояния (читаются байтом по адресу + маска).
const List<LoggerdefSwitch> loggerdefSwitches = <LoggerdefSwitch>[
  const LoggerdefSwitch(
      sid: 'S1', name: "AT Vehicle ID",
      byte: '0x000061', bit: 6),
  const LoggerdefSwitch(
      sid: 'S2', name: "Test Mode Connector",
      byte: '0x000061', bit: 5),
  const LoggerdefSwitch(
      sid: 'S3', name: "Read Memory Connector",
      byte: '0x000061', bit: 4),
  const LoggerdefSwitch(
      sid: 'S4', name: "Neutral Position Switch",
      byte: '0x000062', bit: 7),
  const LoggerdefSwitch(
      sid: 'S5', name: "Idle Switch",
      byte: '0x000062', bit: 6),
  const LoggerdefSwitch(
      sid: 'S6', name: "Intercooler AutoWash Switch",
      byte: '0x000062', bit: 4),
  const LoggerdefSwitch(
      sid: 'S7', name: "Ignition Switch",
      byte: '0x000062', bit: 3),
  const LoggerdefSwitch(
      sid: 'S8', name: "Power Steering Switch",
      byte: '0x000062', bit: 2),
  const LoggerdefSwitch(
      sid: 'S9', name: "Air Conditioning Switch",
      byte: '0x000062', bit: 1),
  const LoggerdefSwitch(
      sid: 'S10', name: "Handle Switch",
      byte: '0x000063', bit: 7),
  const LoggerdefSwitch(
      sid: 'S11', name: "Starter Switch",
      byte: '0x000063', bit: 6),
  const LoggerdefSwitch(
      sid: 'S12', name: "Front O2 Rich Signal",
      byte: '0x000063', bit: 5),
  const LoggerdefSwitch(
      sid: 'S13', name: "Rear O2 Rich Signal",
      byte: '0x000063', bit: 4),
  const LoggerdefSwitch(
      sid: 'S14', name: "Front O2 #2 Rich Signal",
      byte: '0x000063', bit: 3),
  const LoggerdefSwitch(
      sid: 'S15', name: "Knock Signal 1",
      byte: '0x000063', bit: 2),
  const LoggerdefSwitch(
      sid: 'S16', name: "Knock Signal 2",
      byte: '0x000063', bit: 1),
  const LoggerdefSwitch(
      sid: 'S17', name: "Electrical Load Signal",
      byte: '0x000063', bit: 0),
  const LoggerdefSwitch(
      sid: 'S18', name: "Crank Position Sensor",
      byte: '0x000064', bit: 7),
  const LoggerdefSwitch(
      sid: 'S19', name: "Cam Position Sensor",
      byte: '0x000064', bit: 6),
  const LoggerdefSwitch(
      sid: 'S20', name: "Defogger Switch",
      byte: '0x000064', bit: 5),
  const LoggerdefSwitch(
      sid: 'S21', name: "Blower Switch",
      byte: '0x000064', bit: 4),
  const LoggerdefSwitch(
      sid: 'S22', name: "Interior Light Switch",
      byte: '0x000064', bit: 3),
  const LoggerdefSwitch(
      sid: 'S23', name: "Wiper Switch",
      byte: '0x000064', bit: 2),
  const LoggerdefSwitch(
      sid: 'S24', name: "Air-Con Lock Signal",
      byte: '0x000064', bit: 1),
  const LoggerdefSwitch(
      sid: 'S25', name: "Air-Con Mid Pressure Switch",
      byte: '0x000064', bit: 0),
  const LoggerdefSwitch(
      sid: 'S26', name: "Air-Con Compressor Signal",
      byte: '0x000065', bit: 7),
  const LoggerdefSwitch(
      sid: 'S27', name: "Radiator Fan Relay #3",
      byte: '0x000065', bit: 6),
  const LoggerdefSwitch(
      sid: 'S28', name: "Radiator Fan Relay #1",
      byte: '0x000065', bit: 5),
  const LoggerdefSwitch(
      sid: 'S29', name: "Radiator Fan Relay #2",
      byte: '0x000065', bit: 4),
  const LoggerdefSwitch(
      sid: 'S30', name: "Fuel Pump Relay",
      byte: '0x000065', bit: 3),
  const LoggerdefSwitch(
      sid: 'S31', name: "Intercooler Auto-Wash Relay",
      byte: '0x000065', bit: 2),
  const LoggerdefSwitch(
      sid: 'S32', name: "CPC Solenoid Valve",
      byte: '0x000065', bit: 1),
  const LoggerdefSwitch(
      sid: 'S33', name: "Blow-By Leak Connector",
      byte: '0x000065', bit: 0),
  const LoggerdefSwitch(
      sid: 'S34', name: "PCV Solenoid Valve",
      byte: '0x000066', bit: 7),
  const LoggerdefSwitch(
      sid: 'S35', name: "TGV Output",
      byte: '0x000066', bit: 6),
  const LoggerdefSwitch(
      sid: 'S36', name: "TGV Drive",
      byte: '0x000066', bit: 5),
  const LoggerdefSwitch(
      sid: 'S37', name: "Variable Intake Air Solenoid",
      byte: '0x000066', bit: 4),
  const LoggerdefSwitch(
      sid: 'S38', name: "Pressure Sources Change",
      byte: '0x000066', bit: 3),
  const LoggerdefSwitch(
      sid: 'S39', name: "Vent Solenoid Valve",
      byte: '0x000066', bit: 2),
  const LoggerdefSwitch(
      sid: 'S40', name: "P/S Solenoid Valve",
      byte: '0x000066', bit: 1),
  const LoggerdefSwitch(
      sid: 'S41', name: "Assist Air Solenoid Valve",
      byte: '0x000066', bit: 0),
  const LoggerdefSwitch(
      sid: 'S42', name: "Tank Sensor Control Valve",
      byte: '0x000067', bit: 7),
  const LoggerdefSwitch(
      sid: 'S43', name: "Relief Valve Solenoid 1",
      byte: '0x000067', bit: 6),
  const LoggerdefSwitch(
      sid: 'S44', name: "Relief Valve Solenoid 2",
      byte: '0x000067', bit: 5),
  const LoggerdefSwitch(
      sid: 'S45', name: "TCS Relief Valve Solenoid",
      byte: '0x000067', bit: 4),
  const LoggerdefSwitch(
      sid: 'S46', name: "Ex. Gas Positive Pressure",
      byte: '0x000067', bit: 3),
  const LoggerdefSwitch(
      sid: 'S47', name: "Ex. Gas Negative Pressure",
      byte: '0x000067', bit: 2),
  const LoggerdefSwitch(
      sid: 'S48', name: "Intake Air Solenoid",
      byte: '0x000067', bit: 1),
  const LoggerdefSwitch(
      sid: 'S49', name: "Muffler Control",
      byte: '0x000067', bit: 0),
  const LoggerdefSwitch(
      sid: 'S50', name: "Retard Signal from AT",
      byte: '0x000068', bit: 3),
  const LoggerdefSwitch(
      sid: 'S51', name: "Fuel Cut Signal from AT",
      byte: '0x000068', bit: 2),
  const LoggerdefSwitch(
      sid: 'S52', name: "Ban of Torque Down",
      byte: '0x000068', bit: 1),
  const LoggerdefSwitch(
      sid: 'S53', name: "Request Torque Down VDC",
      byte: '0x000068', bit: 0),
  const LoggerdefSwitch(
      sid: 'S54', name: "Torque Control Signal #1",
      byte: '0x000069', bit: 7),
  const LoggerdefSwitch(
      sid: 'S55', name: "Torque Control Signal #2",
      byte: '0x000069', bit: 6),
  const LoggerdefSwitch(
      sid: 'S56', name: "Torque Permission Signal",
      byte: '0x000069', bit: 5),
  const LoggerdefSwitch(
      sid: 'S57', name: "EAM Signal",
      byte: '0x000069', bit: 4),
  const LoggerdefSwitch(
      sid: 'S58', name: "AT coop. lock up signal",
      byte: '0x000069', bit: 3),
  const LoggerdefSwitch(
      sid: 'S59', name: "AT coop. lean burn signal",
      byte: '0x000069', bit: 2),
  const LoggerdefSwitch(
      sid: 'S60', name: "AT coop. rich spike signal",
      byte: '0x000069', bit: 1),
  const LoggerdefSwitch(
      sid: 'S61', name: "AET Signal",
      byte: '0x000069', bit: 0),
  const LoggerdefSwitch(
      sid: 'S62', name: "ETC Motor Relay",
      byte: '0x000120', bit: 6),
  const LoggerdefSwitch(
      sid: 'S63', name: "Clutch Switch",
      byte: '0x000121', bit: 7),
  const LoggerdefSwitch(
      sid: 'S64', name: "Stop Light Switch",
      byte: '0x000121', bit: 6),
  const LoggerdefSwitch(
      sid: 'S65', name: "Set/Coast Switch",
      byte: '0x000121', bit: 5),
  const LoggerdefSwitch(
      sid: 'S66', name: "Resume/Accelerate Switch",
      byte: '0x000121', bit: 4),
  const LoggerdefSwitch(
      sid: 'S67', name: "Brake Switch",
      byte: '0x000121', bit: 3),
  const LoggerdefSwitch(
      sid: 'S68', name: "Accelerator Switch",
      byte: '0x000121', bit: 1),
];
'''

# ===== Справочник кодов неисправностей Subaru (RU) =====
FILES["lib/dtc_dict.dart"] = r'''
// v0.11 (патч 04.2): справочник кодов неисправностей Subaru.
// code -> (название RU, система). Используется страницей DTC.

const Map<String, (String, String)> kSubaruDtcDict = <String, (String, String)>{
  'P0030': ("Цепь подогрева O2-датчика B1S1", 'engine'),
  'P0031': ("Низкий уровень подогрева O2 B1S1", 'engine'),
  'P0032': ("Высокий уровень подогрева O2 B1S1", 'engine'),
  'P0037': ("Цепь подогрева O2 B1S2", 'engine'),
  'P0110': ("Датчик температуры впускного воздуха (IAT)", 'engine'),
  'P0115': ("Датчик температуры ОЖ (ECT)", 'engine'),
  'P0120': ("Датчик положения дросселя A", 'engine'),
  'P0130': ("Цепь O2-датчика B1S1", 'engine'),
  'P0171': ("Слишком бедная смесь (банк 1)", 'engine'),
  'P0172': ("Слишком богатая смесь (банк 1)", 'engine'),
  'P0183': ("Датчик температуры топлива А", 'engine'),
  'P0244': ("Wastegate: давление наддува выше лимита", 'engine'),
  'P0245': ("Цепь соленоида Wastegate (низкий уровень)", 'engine'),
  'P0246': ("Цепь соленоида Wastegate (высокий уровень)", 'engine'),
  'P0300': ("Случайные/множественные пропуски зажигания", 'engine'),
  'P0301': ("Пропуски зажигания, цилиндр 1", 'engine'),
  'P0303': ("Пропуски зажигания, цилиндр 3", 'engine'),
  'P0325': ("Цепь датчика детонации №1", 'engine'),
  'P0328': ("Высокий уровень сигнала датчика детонации", 'engine'),
  'P0335': ("Датчик положения коленвала", 'engine'),
  'P0340': ("Датчик положения распредвала", 'engine'),
  'P0420': ("Эффективность катализатора ниже порога B1", 'engine'),
  'P0441': ("Система продувки адсорбера: неверный поток", 'engine'),
  'P0455': ("EVAP: большая утечка", 'engine'),
  'P0500': ("Датчик скорости автомобиля (VSS)", 'engine'),
  'P0600': ("Ошибка последовательной связи", 'engine'),
  'P0604': ("Внутренняя ошибка RAM ЭБУ", 'engine'),
  'P0700': ("Неисправность системы управления АКПП", 'trans'),
  'P0715': ("Датчик частоты входного вала АКПП", 'trans'),
  'P0720': ("Датчик частоты выходного вала АКПП", 'trans'),
  'P0731': ("Неверное передаточное число 1-й передачи", 'trans'),
  'P0741': ("Гидротрансформатор: блокировка не включается", 'trans'),
  'P0810': ("Ошибка управления сцеплением", 'trans'),
  'P0841': ("Датчик давления масла АКПП A", 'trans'),
  'P0851': ("Цепь нейтрали (низкий уровень)", 'trans'),
  'P1700': ("Датчик положения дросселя (сигнал в TCU)", 'trans'),
  'P1710': ("Соленоид №2 АКПП", 'trans'),
  'C0057': ("VDC: ошибка связи с ЭБУ двигателя", 'chassis'),
  'C0071': ("Датчик угла поворота руля", 'chassis'),
  'B1771': ("SRS: цепь водительской подушки", 'body'),
};
'''

# ===== DiagSession: SSM2 0x18/0x14, OBD 03/04, UDS 19/14 + декодеры =====
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
  DiagDtc(this.code, this.statusByte, this.proto);
  final String code; // P0110, C0057...
  final int statusByte;
  final DiagProto proto;

  /// SSM2 (наследие FreeSSM): bit5 (0x20) — активна сейчас; bit7 (0x80) — MIL.
  /// UDS 19 02: bit0 testFailed, bit3 confirmedDTC, bit6 warningIndicator.
  bool get active => proto == DiagProto.uds
      ? (statusByte & 0x01) != 0
      : proto == DiagProto.obd
          ? true
          : (statusByte & 0x20) != 0;
  bool get confirmed => proto == DiagProto.uds ? (statusByte & 0x08) != 0 : true;
  bool get milOn => proto == DiagProto.uds
      ? (statusByte & 0x40) != 0
      : proto == DiagProto.obd
          ? false
          : (statusByte & 0x80) != 0;

  String get statusText {
    if (proto == DiagProto.uds) {
      final f = <String>[];
      if (statusByte & 0x01 != 0) f.add('есть сейчас');
      if (statusByte & 0x08 != 0) f.add('подтверждена');
      if (statusByte & 0x40 != 0) f.add('MIL');
      return f.isEmpty
          ? 'пассивная (0x${hex2(statusByte)})'
          : f.join(' · ');
    }
    if (proto == DiagProto.obd) return 'сохранённая (mode 03)';
    return active ? (milOn ? 'активная · MIL' : 'активная') : 'сохранённая';
  }

  /// Название из справочника Subaru ( если код известен).
  String get titleRu => kSubaruDtcDict[code]?.$1 ?? '';
}

/// Декодер двухбайтового DTC (общий для SSM2/OBD/UDS).
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
    out.add(DiagDtc(decodeDtcBytes(b1, b2), st, DiagProto.ssm2));
  }
  return out;
}

/// OBD mode 03: 43 <b1> <b2> × n
List<DiagDtc> parseObdMode3Reply(List<int> bytes) {
  final out = <DiagDtc>[];
  if (bytes.isEmpty || bytes[0] != 0x43) return out;
  for (var i = 1; i + 1 < bytes.length; i += 2) {
    final b1 = bytes[i], b2 = bytes[i + 1];
    if (b1 == 0 && b2 == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b1, b2), 0x20, DiagProto.obd));
  }
  return out;
}

/// UDS 19 02: 59 02 <availability-mask> <b1> <b2> <status> × n
List<DiagDtc> parseUdsDtcReply(List<int> bytes) {
  final out = <DiagDtc>[];
  if (bytes.length < 3 || bytes[0] != 0x59) return out;
  for (var i = 3; i + 2 < bytes.length; i += 3) {
    final b1 = bytes[i], b2 = bytes[i + 1], st = bytes[i + 2];
    if (b1 == 0 && b2 == 0) continue;
    out.add(DiagDtc(decodeDtcBytes(b1, b2), st, DiagProto.uds));
  }
  return out;
}

/// Универсальный парсер «сырого» ответа ELM (строки hex-токенов).
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
  // single-frame PCI (старший нибл 0, длина 1..7)
  if (b.length > 1 && (b[0] & 0xF0) == 0 && (b[0] & 0x0F) <= 7) {
    final len = b[0] & 0x0F;
    b = b.sublist(1, 1 + len <= b.length ? 1 + len : b.length);
  }
  // CAN ID 0x7E8 при ATH1
  if (b.length >= 3 && b[0] == 0x07 && (b[1] & 0xF8) == 0xE8) {
    b = b.sublist(2);
  }
  return b.isEmpty ? null : b;
}

const Map<int, String> kNrMeanings = <int, String>{
  0x10: 'generalReject',
  0x11: 'serviceNotSupported',
  0x12: 'subFunctionNotSupported',
  0x21: 'busyRepeatRequest',
  0x22: 'conditionsNotCorrect',
  0x31: 'requestOutOfRange',
  0x33: 'securityAccessDenied',
  0x78: 'responsePending',
};

/// Сессия диагностики поверх ElmDriver.diagnostic (штатный whitelist+lock).
class DiagSession {
  DiagSession(this.elm, {this.experimental = false});
  final ElmDriver elm;
  final bool experimental;

  Future<String> _atEcu(EcuSlot slot) async {
    await elm.diagnostic('ATSH ${slot.header}');
    await elm.diagnostic('ATCRA ${slot.responseId}');
    await Future<void>.delayed(const Duration(milliseconds: 20));
    return slot.name;
  }

  Future<List<int>?> _payload(String command) async {
    final reply = await elm.diagnostic(command);
    return parseDiagPayload(reply);
  }

  Future<List<DiagDtc>> readDtc(EcuSlot slot) async {
    await _atEcu(slot);
    if (slot.proto == DiagProto.ssm2) {
      try {
        final bytes = await _payload('18 00 FF 00') ?? const <int>[];
        if (bytes.isNotEmpty && bytes[0] == 0x58) {
          return parseSsm2DtcReply(bytes);
        }
      } catch (_) {}
      final bytes = await _payload('03') ?? const <int>[];
      return parseObdMode3Reply(bytes);
    }
    final bytes = await _payload('19 02 AF') ?? const <int>[];
    if (bytes.isNotEmpty && bytes[0] == 0x59) return parseUdsDtcReply(bytes);
    try {
      return parseObdMode3Reply(await _payload('03') ?? const <int>[]);
    } catch (_) {
      return const [];
    }
  }

  /// Сброс + автоматический повторный запрос (возвращает остаток).
  Future<List<DiagDtc>> clearDtc(EcuSlot slot) async {
    await _atEcu(slot);
    if (slot.proto == DiagProto.ssm2) {
      final bytes = await _payload('14 FF FF 00') ?? const <int>[];
      if (bytes.isEmpty || bytes[0] != 0x54) {
        final obd = await _payload('04') ?? const <int>[];
        if (obd.isEmpty || obd[0] != 0x44) {
          throw ReplyError('ECU отказал в сбросе DTC');
        }
      }
    } else {
      final bytes = await _payload('14 FF FF FF') ?? const <int>[];
      if (bytes.isEmpty || bytes[0] != 0x54) {
        throw ReplyError('ECU отказал в сбросе DTC (UDS 14)');
      }
    }
    await Future<void>.delayed(const Duration(milliseconds: 400));
    return readDtc(slot);
  }

  /// UDS ECUReset 11 01 — глушит мотор! EXPERIMENTAL + стоянка.
  Future<void> ecuReset(EcuSlot slot) async {
    if (!experimental) {
      throw StateError('ECUReset заблокирован (EXPERIMENTAL=false в 04.2)');
    }
    await _atEcu(slot);
    final bytes = await _payload('11 01') ?? const <int>[];
    if (bytes.isEmpty || bytes[0] != 0x51) {
      throw ReplyError('ECUReset не подтверждён (ожидался 51 01)');
    }
  }

  /// SSM3 Clear Memory 04 gr. Коды групп зависят от ECU — EXPERIMENTAL.
  Future<void> clearMemory(int group) async {
    if (!experimental) {
      throw StateError('Clear Memory заблокирован (EXPERIMENTAL=false в 04.2)');
    }
    if (group < 1 || group > 7) throw RangeError.range(group, 1, 7);
    final bytes = await _payload('04 ${hex2(group)}') ?? const <int>[];
    if (bytes.isEmpty || bytes[0] != 0x44) {
      throw ReplyError('Clear Memory group=$group отклонён');
    }
  }

  /// Вернуть заголовки на двигатель после операций.
  Future<void> restore() async {
    try {
      await elm.diagnostic('ATSH 7E0');
      await elm.diagnostic('ATCRA 7E8');
    } catch (_) {}
  }
}
'''

# ===== Вкладка DTC: блоки, чтение/сброс, сервисные процедуры =====
FILES["lib/dtc_service_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'diag.dart';
import 'model.dart';

/// Вкладка «DTC»: чтение/сброс ошибок по блокам + сервисные процедуры.
class DtcServicePage extends StatefulWidget {
  const DtcServicePage({super.key, required this.model});
  final AppModel model;

  @override
  State<DtcServicePage> createState() => _DtcServicePageState();
}

class _DtcServicePageState extends State<DtcServicePage> {
  final Map<String, List<DiagDtc>> _results = {};
  bool _busy = false;
  String _log = '';

  Future<void> _run(String what, Future<void> Function() op) =>
      widget.model.diagRun(what, () async {
        setState(() { _busy = true; _log = what; });
        try {
          await op();
        } finally {
          if (mounted) setState(() => _busy = false);
        }
      });

  Future<void> _read(EcuSlot slot) => _run('Чтение ${slot.name}', () async {
        final list = await widget.model.diagSession.readDtc(slot);
        if (mounted) setState(() => _results[slot.name] = list);
      });

  Future<void> _clear(EcuSlot slot) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Сбросить ошибки: ${slot.name}?'),
        content: const Text(
            'Стоп-кадры и мониторы readiness очистятся. CEL погаснет после '
            'перезапуска зажигания. Адаптации (IAM, LTFT) это НЕ трогает.'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('ОТМЕНА')),
          FilledButton.tonal(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('СБРОСИТЬ')),
        ],
      ),
    );
    if (ok != true) return;
    await _run('Сброс ${slot.name}', () async {
      final rest = await widget.model.diagSession.clearDtc(slot);
      if (mounted) {
        setState(() {
          _results[slot.name] = rest;
          _log = rest.isEmpty
              ? '${slot.name}: очищено'
              : '${slot.name}: осталось ${rest.length}';
        });
      }
    });
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
        for (final slot in EcuSlot.all)
          Card(
            child: ExpansionTile(
              leading: Icon(
                _results.containsKey(slot.name)
                    ? (_results[slot.name]!.isEmpty
                        ? Icons.check_circle
                        : Icons.warning_amber)
                    : Icons.memory,
              ),
              title: Text(slot.name),
              subtitle: Text(
                  'CAN ${slot.header}→${slot.responseId} · ${slot.proto.name.toUpperCase()}'),
              children: [
                for (final d in _results[slot.name] ?? const <DiagDtc>[])
                  ListTile(
                    dense: true,
                    leading: Icon(
                      d.active ? Icons.error : Icons.history,
                      color: d.active
                          ? theme.colorScheme.error
                          : theme.colorScheme.outline,
                    ),
                    title: Text(
                      '${d.code}${d.titleRu.isEmpty ? '' : ' · ${d.titleRu}'}',
                      style: const TextStyle(fontFamily: 'monospace'),
                    ),
                    subtitle: Text(d.statusText),
                    trailing: d.milOn
                        ? const Icon(Icons.lightbulb, color: Colors.amber)
                        : null,
                  ),
                OverflowBar(children: [
                  TextButton.icon(
                    onPressed: _busy ? null : () => _read(slot),
                    icon: const Icon(Icons.search),
                    label: const Text('ПРОЧИТАТЬ'),
                  ),
                  if ((_results[slot.name] ?? const []).isNotEmpty)
                    FilledButton.tonalIcon(
                      onPressed: _busy ? null : () => _clear(slot),
                      icon: const Icon(Icons.cleaning_services),
                      label: const Text('СБРОС'),
                    ),
                ]),
              ],
            ),
          ),
        const Divider(height: 32),
        Text('Сервисные процедуры', style: theme.textTheme.titleMedium),
        const ListTile(
          leading: Icon(Icons.restart_alt),
          title: Text('Сброс адаптаций (SSM2-эра)'),
          subtitle: Text(
              'Клемма АКБ 10–30 мин; контроль IAM до/после в логгер и дообучение 15–20 мин.'),
        ),
        const ListTile(
          leading: Icon(Icons.speed),
          title: Text('Обучение дросселя (E-Gas)'),
          subtitle: Text(
              'Зажигание ON 15–20 с без запуска → OFF → запуск и прогрев до 82 °C.'),
        ),
        ListTile(
          leading: const Icon(Icons.science),
          title: const Text('ECUReset 11 01 / Clear Memory 04 (UDS)'),
          subtitle: Text(widget.model.experimental
              ? 'EXPERIMENTAL: разблокировано. Глушит мотор — только стоянка.'
              : 'Заблокировано: EXPERIMENTAL=false в ячейке 04.2.'),
          enabled: widget.model.experimental,
        ),
      ],
    );
  }
}
'''

# ===== 16 тестов: ExprEval, декодеры, payload, whitelist =====
FILES["test/diag_test.dart"] = r'''
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/diag.dart';
import 'package:subaru_ssm2/expr.dart';
import 'package:subaru_ssm2/protocol.dart';

void main() {
  group('ExprEval', () {
    test('линейные', () {
      expect(ExprEval.eval('x*100/255', 128), closeTo(50.196, 0.001));
      expect(ExprEval.eval('(x*100)-100', 1.205), closeTo(20.5, 0.001));
      expect(ExprEval.eval('x+6', 2.0), 8.0);
      expect(ExprEval.eval('x/.84', 84), closeTo(100.0, 0.001));
    });
    test('дробные AFR-формы', () {
      expect(ExprEval.eval('14.7/(1+x)', 0.0), closeTo(14.7, 0.001));
      expect(ExprEval.eval('14.7/x', 1.0), closeTo(14.7, 0.001));
      expect(ExprEval.eval('x*0.001333224', 750.0), closeTo(1.0, 0.0001));
    });
    test('защита от мусора', () {
      expect(ExprEval.eval('xZXy', 1), isNull);
      expect(ExprEval.eval('14/0', 3), isNull); // деление на 0 -> NaN -> null
    });
  });

  group('decodeDtcBytes', () {
    test('коды', () {
      expect(decodeDtcBytes(0x01, 0x10), 'P0110');
      expect(decodeDtcBytes(0x03, 0x03), 'P0303');
      expect(decodeDtcBytes(0x40, 0x57), 'C0057');
      expect(decodeDtcBytes(0xD0, 0x00), 'U1000');
    });
  });

  group('parseSsm2DtcReply', () {
    test('две ошибки со статусами', () {
      final d = parseSsm2DtcReply(
          const [0x58, 0x02, 0x01, 0x10, 0xA0, 0x03, 0x03, 0x00]);
      expect(d, hasLength(2));
      expect(d[0].code, 'P0110');
      expect(d[0].active, isTrue);
      expect(d[0].milOn, isTrue);
      expect(d[1].code, 'P0303');
      expect(d[1].active, isFalse);
    });
    test('чисто', () => expect(parseSsm2DtcReply(const [0x58, 0x00]), isEmpty));
    test('негатив пропускаем', () {
      expect(parseSsm2DtcReply(const [0x7F, 0x18, 0x11]), isEmpty);
    });
  });

  group('parseObdMode3Reply', () {
    test('пары без статусов; нули отбрасываются', () {
      final d = parseObdMode3Reply(const [0x43, 0x00, 0x00, 0x01, 0x10, 0x03, 0x03]);
      expect(d.map((x) => x.code), ['P0110', 'P0303']);
    });
  });

  group('parseUdsDtcReply', () {
    test('59 02 FF за DTC', () {
      final d = parseUdsDtcReply(
          const [0x59, 0x02, 0xFF, 0x01, 0x10, 0x2F, 0x03, 0x03, 0x00]);
      expect(d, hasLength(2));
      expect(d[0].code, 'P0110');
      expect(d[0].active, isTrue);
      expect(d[0].confirmed, isTrue);
      expect(d[1].code, 'P0303');
    });
  });

  group('parseDiagPayload', () {
    test('plain', () {
      expect(parseDiagPayload('58 01 03 03 00'), [0x58, 0x01, 0x03, 0x03, 0x00]);
    });
    test('PCI отрезается', () {
      expect(
          parseDiagPayload('05 58 02 01 10 A0 03 00'), [0x58, 0x02, 0x01, 0x10, 0xA0]);
    });
    test('frame lines склеиваются', () {
      expect(parseDiagPayload('0: 58 02 01\n1: 10 A0'),
          [0x58, 0x02, 0x01, 0x10, 0xA0]);
    });
    test('пусто -> null', () => expect(parseDiagPayload('NO DATA'), isNull));
  });

  group('allowedDiagnostic (whitelist)', () {
    test('старые команды не сломаны', () {
      expect(allowedDiagnostic('A8 00 00 00 08'), isTrue);
      expect(allowedDiagnostic('ATSH 7E0'), isTrue);
      expect(allowedDiagnostic('ATCRA 7E8'), isTrue);
    });
    test('новые сервисы разрешены', () {
      for (final c in ['18 00 FF 00', '14 FF FF 00', '14 FF FF FF', '19 02 AF', '03', '04', '07', '0A', '09 02', '01 01']) {
        expect(allowedDiagnostic(c), isTrue, reason: c);
      }
    });
    test('произвольная запись запрещена', () {
      expect(allowedDiagnostic('2E F1 90 AA'), isFalse);
    });
  });
}
'''

# ===== Точечные правки: protocol/model/main/aliases/tests/calid_import =====
# ===== REWRITES: consumable-иглы + сигнатуры idempotency =====
# Кортеж: (файл, игла, замена, сигнатура, optional). Сигнатура = короткие маркеры, которые
# есть в тексте, если правка УЖЕ применена (устойчивы к dart format и эволюции блоков).
# optional=True: апгрейд-замены (не обязательны к применению, если иглы нет).

# --- protocol.dart: расширение белого списка (игла включает '}' функции) ---
REWRITES.append((
    "lib/protocol.dart",
    """      RegExp(r'^A8 00 [0-9A-F]{2} [0-9A-F]{2} [0-9A-F]{2}$').hasMatch(c);
}""",
    """      RegExp(r'^A8 00 [0-9A-F]{2} [0-9A-F]{2} [0-9A-F]{2}$').hasMatch(c) ||
      _diagServiceCommands.contains(c) ||
      _diagServicePatterns.any((p) => p.hasMatch(c));
}

// v0.11 (04.2): сервисные команды диагностики (SSM2 0x18/0x14, OBD-II,
// UDS 19/14, readiness, VIN). Произвольная запись по-прежнему запрещена.
const Set<String> _diagServiceCommands = <String>{
  '18 00 FF 00', // SSM2 ReadDTC (ECM/TCM)
  '14 FF FF 00', // SSM2 ClearDTC
  '14 FF FF FF', // UDS ClearDiagnosticInformation
  '19 02 AF', // UDS ReadDTCInformation (all)
  '03', '04', '07', '0A', // OBD DTC stored/clear/pending/permanent
  '01 01', // OBD readiness
  '09 02', // OBD VIN
  '11 01', // UDS ECUReset (глушится EXPERIMENTAL в diag.dart)
};
final List<RegExp> _diagServicePatterns = <RegExp>[
  // SSM3 Clear Memory: 04 XX (под EXPERIMENTAL-гейтом DiagSession)
  RegExp(r'^04 0[1-7]$'),
];""",
    ["_diagServiceCommands", "'19 02 AF'", False],
))

# --- upgrade: ранняя редакция 04.2 писала const-список RegExp (ошибка analyze) ---
REWRITES.append((
    "lib/protocol.dart",
    "const List<RegExp> _diagServicePatterns",
    "final List<RegExp> _diagServicePatterns",
    ["final List<RegExp> _diagServicePatterns", True],
))

# upgrade: на случай, если прошлый прогон записал 55 вместо 57
REWRITES.append((
    "test/protocol_test.dart",
    "expect(all.length, 55); // v0.11: 28 + 27 loggerdef (A2TB100B)",
    "expect(all.length, 57); // v0.11: 28 + 29 loggerdef (A2TB100B)",
    ["all.length, 57", True],
))

# --- model.dart: импорт (игла = 3-строчный блок) + DiagSession/diagRun ---
REWRITES.append((
    "lib/model.dart",
    "import 'bt_transport.dart';\nimport 'elm.dart';\nimport 'engine.dart';",
    "import 'bt_transport.dart';\nimport 'diag.dart';\nimport 'elm.dart';\nimport 'engine.dart';",
    ["import 'diag.dart';", False],
))
REWRITES.append((
    "lib/model.dart",
    "  late final SsmEngine engine;\n  final logger = CsvLogger();",
    """  late final SsmEngine engine;
  final bool experimental =
      const bool.fromEnvironment('SSM2_EXPERIMENTAL', defaultValue: false);
  late final DiagSession diagSession =
      DiagSession(elm, experimental: experimental);
  final logger = CsvLogger();

  /// Диагностическая операция: опрос PID на паузе, восстановление 7E0/7E8,
  /// возврат опроса, если он работал.
  Future<void> diagRun(String label, Future<void> Function() op) =>
      perform(() async {
        final wasPolling = engine.running;
        await engine.stop();
        try {
          await op();
          message = label;
        } finally {
          await diagSession.restore();
          if (wasPolling) await engine.start();
        }
      });""",
    ["diagSession", "diagRun", False],
))

# --- main.dart: импорт + вкладка (иглы = парные строки, consumable) ---
REWRITES.append((
    "lib/main.dart",
    "import 'engine.dart';\nimport 'maplab_link.dart';\nimport 'model.dart';",
    "import 'engine.dart';\nimport 'dtc_service_page.dart';\nimport 'maplab_link.dart';\nimport 'model.dart';",
    ["import 'dtc_service_page.dart';", False],
))
REWRITES.append((
    "lib/main.dart",
    "            DiagnosticPage(model),\n            const MapLabTab(),",
    "            DiagnosticPage(model),\n            DtcServicePage(model: model),\n            const MapLabTab(),",
    ["DtcServicePage(model: model)", False],
))
REWRITES.append((
    "lib/main.dart",
    """                NavigationDestination(
                    icon: Icon(Icons.terminal), label: 'Диагн.'),
                NavigationDestination(
                  icon: Icon(Icons.table_view),""",
    """                NavigationDestination(
                    icon: Icon(Icons.terminal), label: 'Диагн.'),
                NavigationDestination(
                    icon: Icon(Icons.build_circle_outlined), label: 'DTC'),
                NavigationDestination(
                  icon: Icon(Icons.table_view),""",
    ["label: 'DTC'", False],
))
REWRITES.append((
    "lib/main.dart",
    "SSM2 TELEMETRY 0.10.2",
    "SSM2 TELEMETRY 0.11",
    ["SSM2 TELEMETRY 0.11", False],
))

# --- maps_lab_log.dart: алиасы tq (игла = 3 строки incl. cl_target) ---
REWRITES.append((
    "lib/maps_lab/maps_lab_log.dart",
    "    'tq': [\n      'requested torque',\n      'cl_target',",
    "    'tq': [\n      'requested torque',\n      'req_tq',\n      'requested torque nm',\n      'cl_target',",
    ["'req_tq'", False],
))

# --- protocol_test.dart: в библиотеке теперь 28 + 27 = 55 PID ---
REWRITES.append((
    "test/protocol_test.dart",
    "expect(all.length, 28);",
    "expect(all.length, 57); // v0.11: 28 + 29 loggerdef (A2TB100B)",
    ["all.length, 57", True],
))

# --- calid_import.py: разрешить новые параметры ---
NEW_VALID = '''VALID = {"IAM", "LOAD_4B", "BOOST_ERR", "BOOST_TGT", "FBKC", "FKL", "BOOST", "CL_TARGET", "REQ_TQ", "KC_ADV_4B", "KC_IAM", "BTIMING", "KCMAX", "KNOCK_SUM", "AFL_A", "AFL_B", "AFL_C", "AFL_D", "AFL_4B", "AFCOR1_4B", "TIPIN", "TTH_TGT", "THROTTLE_4B", "CLOL", "FL_OFFSET", "GEAR_CALC", "TDIG_INT", "TDIG_PROP", "INJPW", "INJ_LAT", "OLE_ENRICH", "ENRICH_FINAL", "FUELBASE", "MAP_4B", "TBOOST_REL", "IDLE_SEL", "AFL_RANGE"}'''
REWRITES.append((
    "tool/calid_import.py",
    'VALID = {"IAM", "LOAD_4B", "BOOST_ERR", "BOOST_TGT", "FBKC", "FKL", "BOOST", "CL_TARGET"}',
    NEW_VALID,
    ["REQ_TQ"],
))
REWRITES.append((
    "tool/calid_import.py",
    'Допустимые param: IAM, LOAD_4B, BOOST_ERR, BOOST_TGT, FBKC, FKL, BOOST, CL_TARGET.',
    'Допустимые param: IAM, LOAD_4B, BOOST_ERR, BOOST_TGT, FBKC, FKL, BOOST, CL_TARGET, REQ_TQ, KC_ADV_4B, KC_IAM, BTIMING, KCMAX, KNOCK_SUM, AFL_A..D, AFL_4B, AFCOR1_4B, TIPIN, TTH_TGT, THROTTLE_4B, CLOL, FL_OFFSET, GEAR_CALC, TDIG_INT, TDIG_PROP, INJPW, INJ_LAT, OLE_ENRICH, ENRICH_FINAL, FUELBASE, MAP_4B, TBOOST_REL, IDLE_SEL, AFL_RANGE.',
    ["AFL_A..D", False],
))

# ===== санитария: удаление бэкапов и мусора внутри APP =====
# flutter analyze сканирует ВСЁ дерево проекта — бэкапы внутри APP приводят к ошибкам
for old_bak in list(APP.glob("_backup_*")) + list(APP.glob("*backup*")):
    if old_bak.is_dir():
        try:
            shutil.rmtree(old_bak, ignore_errors=True)
            print(f"[CLEAN] удалена бэкап-папка внутри проекта: {old_bak.name}")
        except Exception as e:
            print(f"[CLEAN] ошибка удаления {old_bak}: {e}")

# Удаление файлов с суффиксами от ранних прогонов
for stray in [p for p in APP.glob("lib/**/*") if "(" in p.name or ")" in p.name]:
    try:
        stray.unlink()
        print(f"[CLEAN] удалён брошенный файл: {stray.relative_to(APP)}")
    except Exception as e:
        print(f"[CLEAN] не удалось удалить {stray}: {e}")

# ===== запись новых файлов =====
# страж: в FILES-ключах не должно быть скобок (суффиксы "(полная замена)"
# предназначены только для UI-отображения частей)
bad_keys = [rel for rel in FILES if "(" in rel or ")" in rel]
if bad_keys:
    raise RuntimeError(f"Невалидные FILES-ключи с суффиксами: {bad_keys}")
BACKUP.mkdir(parents=True, exist_ok=True)
written = 0
for rel, body in FILES.items():
    dest = APP / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.copyfile(dest, BACKUP / (rel.replace("/", "__") + ".bak"))
    dest.write_text(body.strip("\n") + "\n", encoding="utf-8")
    written += 1
    print(f"[OK] {rel}")
print(f"[OK] файлов записано/перезаписано: {written}")

# ===== точечные правки (идемпотентно, по сигнатурам) =====
patched, skipped = 0, 0
for rel, needle, replacement, sig in REWRITES:
    # обратная совместимость: если в сигнатуре хвост True/False — это optional
    optional = False
    if sig and sig[-1] is True:
        sig, optional = sig[:-1], True
    elif sig and sig[-1] is False:
        sig, optional = sig[:-1], False
    path = APP / rel
    if not path.exists():
        raise RuntimeError(f"Нет файла {rel} — сначала прогоните 02–04.1")
    text = path.read_text(encoding="utf-8")
    if needle in text:
        shutil.copyfile(path, BACKUP / (rel.replace("/", "__") + ".bak"))
        path.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
        patched += 1
        print(f"[OK] правка {rel}")
        continue
    if all(s in text for s in sig):
        skipped += 1
        print(f"[SKIP уже применено] {rel}: {sig[0][:60]}")
        continue
    if optional:
        skipped += 1
        print(f"[SKIP необязательная] {rel}")
        continue
    raise RuntimeError(
        f"Маркер не найден в {rel} (и правка не применена):\n{needle[:90]!r}")
print(f"[OK] правок: {patched}, пропущено (уже применено): {skipped}")

# ===== самопроверка маркеров =====
def markers_check():
    checks = {
        "lib/expr.dart": ["class ExprEval", "exprRawX"],
        "lib/pids.dart": ["'REQ_TQ'", "0xFF8058", "exprText", "class SubaruPidLibrary"],
        "lib/identity.dart": ["'REQ_TQ': '0xFF8058'", "5204584007"],
        "lib/loggerdef.dart": ["loggerdefParams", "loggerdefExt", "loggerdefSwitches"],
        "lib/dtc_dict.dart": ["kSubaruDtcDict", "P0300"],
        "lib/diag.dart": ["class DiagSession", "18 00 FF 00", "14 FF FF 00", "parseUdsDtcReply"],
        "lib/dtc_service_page.dart": ["class DtcServicePage", "EcuSlot.all"],
        "lib/protocol.dart": ["_diagServiceCommands", "19 02 AF"],
        "lib/model.dart": ["diagSession", "diagRun"],
        "lib/main.dart": ["DtcServicePage", "label: 'DTC'"],
        "lib/maps_lab/maps_lab_log.dart": ["'req_tq'"],
        "test/diag_test.dart": ["ExprEval.eval", "allowedDiagnostic"],
        "test/protocol_test.dart": ["all.length, 57"],
        "tool/calid_import.py": ['"REQ_TQ"'],
    }
    for rel, needles in checks.items():
        text = (APP / rel).read_text(encoding="utf-8")
        missing = [m for m in needles if m not in text]
        if missing:
            raise RuntimeError(f"Самопроверка {rel}: нет маркеров {missing}")
markers_check()
print("[OK] самопроверка маркеров пройдена")

print("\n=== Готово: SSM2 0.11. Далее — ячейка 05 (сборка). ===")
print("Новое в приложении:")
print("  · PID-библиотека: 57 параметров (28 + 29 loggerdef для 5204584007)")
print("  · вкладка DTC: чтение/сброс ошибок ECM/TCM/VDC/SRS/EPS/BIU")
print("  · Requested Torque (0xFF8058) — включите REQ_TQ в наборе PID")
print("Чек-лист стенда 04.2:")
print(" [ ] REQ_TQ в логе > 0 при нажатии газа (raw-Nm по шкале вашего ROM)")
print(" [ ] DTC: чтение ECM при 5 активных PID (опрос вернётся сам)")
print(" [ ] Сброс: re-read пуст, CEL гаснет после OFF/ON")
print(" [ ] TCU (ATSH 7E1): чтение + возврат на 7E0 без переподключения")
print(" [ ] AFL_A..D не все нули одновременно на прогретом моторе")
