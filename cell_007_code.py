# ===== Шапка 04.3: параметры, бэкап снаружи проекта, реестры =====
# @title 04.3 | CONFIG & UX | SSM2 0.11 >> 0.12 — конфиг мотора, свой дашборд, ориентация (вставить МЕЖДУ 04.2 и 05)
# Что делает:
#  1. РАЗБЛОКИРУЕТ 4-байтовые PID: ROM-профиль A2TB100B включается автоматически
#     (раньше extended PID молча вырезались, пока вручную не подтвердишь ROM).
#     Кнопка «Все 4-байтовые*» во вкладке PID + понятная причина блокировки.
#  2. CSV-заголовки RomRaider для всех 29 новых PID -> Map Lab их наконец видит.
#  3. lib/vehicle_config.dart: конфигурация железа (форсунки cc + латентность,
#     бензонасос л/ч, тип турбо: сток/single/twin-scroll/rotated/big single,
#     топливо 95/98/E30/E85, объём, редлайн, цель AFR, лимит наддува).
#     Расчёты: IDC форсунок, запас насоса, потолок мощности, советы Map Lab.
#  4. Вкладка «Мотор»: настройка + живые расчёты по текущей телеметрии.
#  5. Настраиваемый дашборд: выбор/порядок плиток (drag&drop), число колонок
#     отдельно для книжной и альбомной, компактный режим, мини-графики.
#  6. Ориентация: авто/книжная/альбомная + NavigationRail в альбомной.
# Идемпотентна, бэкап снаружи проекта, самопроверка маркеров.

import json
import re
import shutil
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")

STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_043_{STAMP}"
FILES = {}
REWRITES = []  # (файл, игла, замена, [сигнатуры..., optional_bool])

# ===== Конфигурация мотора: турбо, форсунки, насос, топливо + расчёты IDC =====
FILES["lib/vehicle_config.dart"] = r'''
import 'dart:convert';

/// Тип турбосистемы — влияет на ожидания по наддуву и советы Map Lab.
enum TurboKind {
  stockTwinScroll('Сток twin-scroll (TD04/VF)', 1.05, 0.08),
  singleScroll('Single-scroll апгрейд', 1.25, 0.12),
  twinScroll('Twin-scroll апгрейд', 1.35, 0.10),
  rotated('Rotated mount (большой single)', 1.60, 0.16),
  sequentialTwin('Sequential twin (VF/TD пара)', 1.10, 0.14),
  bigSingle('Big single (конкурсный)', 2.00, 0.22);

  const TurboKind(this.label, this.typicalMaxBoostBar, this.spoolSlopeBar);

  /// Человекочитаемое название для UI.
  final String label;

  /// Ориентир потолка наддува (бар, relative) для подсказок анализа.
  final double typicalMaxBoostBar;

  /// Допустимый коридор отклонения факт/цель на разгоне (бар).
  final double spoolSlopeBar;
}

/// Топливо: влияет на стехиометрию и запас по форсункам.
enum FuelKind {
  gasoline95('АИ-95', 14.7, 1.00),
  gasoline98('АИ-98/100', 14.7, 1.00),
  e30('E30 (30% этанол)', 13.6, 1.12),
  e85('E85', 9.8, 1.35);

  const FuelKind(this.label, this.stoich, this.flowFactor);
  final String label;

  /// Стехиометрическое соотношение (для пересчёта lambda <-> AFR).
  final double stoich;

  /// Во сколько раз больше топлива нужно против бензина (расход форсунок).
  final double flowFactor;
}

/// Конфигурация железа мотора. Сохраняется в ssm2_settings.json.
class VehicleConfig {
  const VehicleConfig({
    this.injectorCc = 565,
    this.injectorLatencyMs = 0.92,
    this.injectorCount = 4,
    this.fuelPumpLph = 190,
    this.pumpVoltageDerate = 0.88,
    this.turbo = TurboKind.stockTwinScroll,
    this.fuel = FuelKind.gasoline98,
    this.displacementL = 2.0,
    this.redlineRpm = 7000,
    this.targetAfrWot = 11.2,
    this.maxSafeBoostBar = 1.05,
    this.bspOffsetKpa = 0,
  });

  /// Производительность одной форсунки, cc/min @ базовом давлении.
  final int injectorCc;

  /// Латентность (мёртвое время) форсунки, мс @ 14 В.
  final double injectorLatencyMs;
  final int injectorCount;

  /// Производительность бензонасоса, л/ч @ рабочем давлении.
  final int fuelPumpLph;

  /// Поправка на просадку напряжения/давления (0.8..1.0).
  final double pumpVoltageDerate;

  final TurboKind turbo;
  final FuelKind fuel;
  final double displacementL;
  final int redlineRpm;

  /// Целевой AFR на полной нагрузке (для бензина; для E85 пересчитается).
  final double targetAfrWot;

  /// Личный потолок наддува (бар, relative) — для предупреждений.
  final double maxSafeBoostBar;

  /// Смещение базового давления топлива (кПа) — влияет на фактический расход.
  final double bspOffsetKpa;

  VehicleConfig copyWith({
    int? injectorCc,
    double? injectorLatencyMs,
    int? injectorCount,
    int? fuelPumpLph,
    double? pumpVoltageDerate,
    TurboKind? turbo,
    FuelKind? fuel,
    double? displacementL,
    int? redlineRpm,
    double? targetAfrWot,
    double? maxSafeBoostBar,
    double? bspOffsetKpa,
  }) =>
      VehicleConfig(
        injectorCc: injectorCc ?? this.injectorCc,
        injectorLatencyMs: injectorLatencyMs ?? this.injectorLatencyMs,
        injectorCount: injectorCount ?? this.injectorCount,
        fuelPumpLph: fuelPumpLph ?? this.fuelPumpLph,
        pumpVoltageDerate: pumpVoltageDerate ?? this.pumpVoltageDerate,
        turbo: turbo ?? this.turbo,
        fuel: fuel ?? this.fuel,
        displacementL: displacementL ?? this.displacementL,
        redlineRpm: redlineRpm ?? this.redlineRpm,
        targetAfrWot: targetAfrWot ?? this.targetAfrWot,
        maxSafeBoostBar: maxSafeBoostBar ?? this.maxSafeBoostBar,
        bspOffsetKpa: bspOffsetKpa ?? this.bspOffsetKpa,
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        'injectorCc': injectorCc,
        'injectorLatencyMs': injectorLatencyMs,
        'injectorCount': injectorCount,
        'fuelPumpLph': fuelPumpLph,
        'pumpVoltageDerate': pumpVoltageDerate,
        'turbo': turbo.name,
        'fuel': fuel.name,
        'displacementL': displacementL,
        'redlineRpm': redlineRpm,
        'targetAfrWot': targetAfrWot,
        'maxSafeBoostBar': maxSafeBoostBar,
        'bspOffsetKpa': bspOffsetKpa,
      };

  static VehicleConfig fromJson(Map<String, dynamic>? json) {
    if (json == null) return const VehicleConfig();
    T pick<T>(String key, T fallback) {
      final v = json[key];
      return v is T ? v : fallback;
    }

    return VehicleConfig(
      injectorCc: pick<num>('injectorCc', 565).toInt(),
      injectorLatencyMs: pick<num>('injectorLatencyMs', 0.92).toDouble(),
      injectorCount: pick<num>('injectorCount', 4).toInt(),
      fuelPumpLph: pick<num>('fuelPumpLph', 190).toInt(),
      pumpVoltageDerate: pick<num>('pumpVoltageDerate', 0.88).toDouble(),
      turbo: TurboKind.values.firstWhere(
        (t) => t.name == json['turbo'],
        orElse: () => TurboKind.stockTwinScroll,
      ),
      fuel: FuelKind.values.firstWhere(
        (f) => f.name == json['fuel'],
        orElse: () => FuelKind.gasoline98,
      ),
      displacementL: pick<num>('displacementL', 2.0).toDouble(),
      redlineRpm: pick<num>('redlineRpm', 7000).toInt(),
      targetAfrWot: pick<num>('targetAfrWot', 11.2).toDouble(),
      maxSafeBoostBar: pick<num>('maxSafeBoostBar', 1.05).toDouble(),
      bspOffsetKpa: pick<num>('bspOffsetKpa', 0).toDouble(),
    );
  }

  String encode() => jsonEncode(toJson());
}

/// ------------------------- Расчёты по конфигурации -------------------------

/// Суммарный расход форсунок, см³/мин (с учётом топлива).
double totalInjectorFlowCc(VehicleConfig c) =>
    c.injectorCc * c.injectorCount / c.fuel.flowFactor;

/// Требуемый расход топлива, см³/мин, по массовому расходу воздуха.
/// maf г/с → масса топлива = maf / afr; плотность бензина ≈ 0.745 г/см³.
double requiredFuelCcPerMin({
  required double mafGramsPerSec,
  required double afr,
}) {
  if (afr <= 0) return 0;
  final fuelGramsPerMin = mafGramsPerSec * 60 / afr;
  return fuelGramsPerMin / 0.745;
}

/// Расчётная загрузка форсунок (IDC), %.
/// Классическая формула: IDC = (pulseWidth / (60000 / (rpm/2))) * 100.
double injectorDutyFromPulse({
  required double pulseWidthMs,
  required double rpm,
}) {
  if (rpm <= 0) return 0;
  final cycleMs = 120000 / rpm; // период впрыска (4-такт, 1 впрыск / 2 оборота)
  if (cycleMs <= 0) return 0;
  return (pulseWidthMs / cycleMs) * 100;
}

/// Оценка IDC по MAF и AFR, когда PID ширины импульса недоступен.
double injectorDutyFromMaf({
  required double mafGramsPerSec,
  required double afr,
  required VehicleConfig config,
}) {
  final needCc = requiredFuelCcPerMin(mafGramsPerSec: mafGramsPerSec, afr: afr);
  final haveCc = totalInjectorFlowCc(config);
  if (haveCc <= 0) return 0;
  return (needCc / haveCc) * 100;
}

/// Запас бензонасоса, %: сколько ещё топлива он может дать сверх текущего.
double pumpHeadroomPct({
  required double mafGramsPerSec,
  required double afr,
  required VehicleConfig config,
}) {
  final needCcMin = requiredFuelCcPerMin(mafGramsPerSec: mafGramsPerSec, afr: afr);
  final needLph = needCcMin * 60 / 1000;
  final haveLph = config.fuelPumpLph * config.pumpVoltageDerate / config.fuel.flowFactor;
  if (haveLph <= 0) return 0;
  return (1 - needLph / haveLph) * 100;
}

/// Потолок мощности по форсункам (л.с., BSFC 0.6 для турбо-бензина).
double maxPowerByInjectors(VehicleConfig c) {
  final lbsPerHour = totalInjectorFlowCc(c) * 0.0952; // cc/min -> lb/h (бензин)
  return lbsPerHour / 0.6 * 0.85; // запас по IDC 85%
}

/// Потолок мощности по насосу (л.с.).
double maxPowerByPump(VehicleConfig c) {
  final lph = c.fuelPumpLph * c.pumpVoltageDerate / c.fuel.flowFactor;
  final lbsPerHour = lph * 0.745 * 2.2046; // л/ч -> lb/h
  return lbsPerHour / 0.6 * 0.9;
}

enum AdviceLevel { ok, warning, critical }

class ConfigAdvice {
  const ConfigAdvice(this.level, this.title, this.detail);
  final AdviceLevel level;
  final String title;
  final String detail;
}

/// Советы по конфигурации + живой телеметрии (значения могут быть null).
List<ConfigAdvice> buildConfigAdvice({
  required VehicleConfig config,
  double? maf,
  double? afr,
  double? rpm,
  double? boostBar,
  double? injPulseMs,
  double? requestedTorque,
}) {
  final out = <ConfigAdvice>[];

  // --- форсунки ---
  double? idc;
  if (injPulseMs != null && rpm != null && rpm > 500) {
    idc = injectorDutyFromPulse(pulseWidthMs: injPulseMs, rpm: rpm);
  } else if (maf != null && afr != null && afr > 5) {
    idc = injectorDutyFromMaf(mafGramsPerSec: maf, afr: afr, config: config);
  }
  if (idc != null) {
    if (idc >= 95) {
      out.add(ConfigAdvice(
        AdviceLevel.critical,
        'Форсунки на пределе: IDC ${idc.toStringAsFixed(0)}%',
        'При IDC ≥ 95% форсунка физически не успевает — смесь уходит в бедную '
            'на пике наддува. Нужны форсунки больше ${config.injectorCc} cc '
            'или снижение цели наддува.',
      ));
    } else if (idc >= 85) {
      out.add(ConfigAdvice(
        AdviceLevel.warning,
        'Высокая загрузка форсунок: IDC ${idc.toStringAsFixed(0)}%',
        'Рабочий максимум — 85%. Запас на жару и просадку напряжения почти исчерпан.',
      ));
    } else {
      out.add(ConfigAdvice(
        AdviceLevel.ok,
        'Форсунки в норме: IDC ${idc.toStringAsFixed(0)}%',
        '${config.injectorCc} cc × ${config.injectorCount}, топливо ${config.fuel.label}.',
      ));
    }
  }

  // --- насос ---
  if (maf != null && afr != null && afr > 5) {
    final head = pumpHeadroomPct(mafGramsPerSec: maf, afr: afr, config: config);
    if (head < 0) {
      out.add(ConfigAdvice(
        AdviceLevel.critical,
        'Бензонасос не вытягивает (дефицит ${(-head).toStringAsFixed(0)}%)',
        'Расчётная подача ${config.fuelPumpLph} л/ч ниже потребности. '
            'Ожидайте провал давления и обеднение на верхах.',
      ));
    } else if (head < 15) {
      out.add(ConfigAdvice(
        AdviceLevel.warning,
        'Малый запас насоса: ${head.toStringAsFixed(0)}%',
        'На жаре и при низком заряде АКБ подача просядет — держите запас ≥ 20%.',
      ));
    }
  }

  // --- турбо ---
  if (boostBar != null) {
    final cap = config.turbo.typicalMaxBoostBar;
    if (boostBar > config.maxSafeBoostBar + 0.05) {
      out.add(ConfigAdvice(
        AdviceLevel.critical,
        'Перебуст: ${boostBar.toStringAsFixed(2)} бар',
        'Выше вашего лимита ${config.maxSafeBoostBar.toStringAsFixed(2)} бар. '
            'Проверьте вестгейт, шланги и карту WGDC.',
      ));
    } else if (boostBar > cap) {
      out.add(ConfigAdvice(
        AdviceLevel.warning,
        'Наддув выше типичного для «${config.turbo.label}»',
        'Ориентир для этой турбосистемы — до ${cap.toStringAsFixed(2)} бар. '
            'Убедитесь, что хватает форсунок и топлива.',
      ));
    }
  }

  // --- топливо/цель ---
  if (afr != null && rpm != null && rpm > 3500 && afr > config.targetAfrWot + 0.8) {
    out.add(ConfigAdvice(
      AdviceLevel.warning,
      'Смесь беднее цели: ${afr.toStringAsFixed(2)} AFR',
      'Цель на полной нагрузке ${config.targetAfrWot.toStringAsFixed(2)}. '
          'Бедная смесь под наддувом — прямой путь к детонации.',
    ));
  }

  // --- общий потолок ---
  out.add(ConfigAdvice(
    AdviceLevel.ok,
    'Потолок топливной системы',
    'Форсунки ≈ ${maxPowerByInjectors(config).toStringAsFixed(0)} л.с., '
        'насос ≈ ${maxPowerByPump(config).toStringAsFixed(0)} л.с. '
        '(${config.fuel.label}, ${config.turbo.label}).',
  ));

  return out;
}
'''

# ===== Настройки дашборда и ориентации экрана =====
FILES["lib/dashboard_prefs.dart"] = r'''
import 'pids.dart';

/// Настройки дашборда: состав плиток, порядок, плотность, спарклайны.
class DashboardPrefs {
  DashboardPrefs({
    List<String>? tiles,
    this.columnsPortrait = 2,
    this.columnsLandscape = 4,
    this.compact = false,
    this.showSpark = true,
    this.showFuelCard = true,
  }) : tiles = tiles ?? List<String>.from(defaultTiles);

  /// Порядок и состав плиток (id PID).
  final List<String> tiles;
  final int columnsPortrait;
  final int columnsLandscape;
  final bool compact;
  final bool showSpark;
  final bool showFuelCard;

  static const List<String> defaultTiles = <String>[
    'RPM',
    'MAP_REL',
    'AFR',
    'TIMING',
    'ECT',
    'IAT',
    'FBKC',
    'IAM',
  ];

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
        .where((id) => SubaruPidLibrary.all.any((p) => p.id == id))
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

  /// Плитки, которые реально можно показать (PID включён в опрос).
  List<String> visibleTiles(Set<String> enabledPids) =>
      tiles.where(enabledPids.contains).toList();
}

/// Режим ориентации экрана.
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

# ===== Настраиваемый дашборд: drag&drop плитки, колонки, альбомный режим =====
FILES["lib/dashboard_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'dashboard_prefs.dart';
import 'derived.dart';
import 'model.dart';
import 'pids.dart';
import 'samples.dart';

const _muted = Color(0xFF8CA0BF);

/// Настраиваемый дашборд: выбор/порядок плиток, плотность, альбомный режим.
class CustomDashboardPage extends StatefulWidget {
  const CustomDashboardPage(this.model, {super.key});
  final AppModel model;

  @override
  State<CustomDashboardPage> createState() => _CustomDashboardPageState();
}

class _CustomDashboardPageState extends State<CustomDashboardPage> {
  AppModel get model => widget.model;

  @override
  Widget build(BuildContext context) {
    final engine = model.engine;
    final prefs = model.dashboardPrefs;
    final active = engine.active;
    final activeIds = active.map((p) => p.id).toSet();

    // плитки: пользовательский порядок, затем активные PID, которых нет в списке
    final chosen = prefs.tiles.where(activeIds.contains).toList();
    final rest = active.map((p) => p.id).where((id) => !chosen.contains(id));
    final ids = <String>[...chosen, ...rest];

    final isLandscape =
        MediaQuery.of(context).orientation == Orientation.landscape;
    final columns = isLandscape ? prefs.columnsLandscape : prefs.columnsPortrait;

    final fuel = estimateFuel(
      maf: engine.latest['MAF']?.value,
      afr: engine.latest['AFR']?.value,
      speed: engine.latest['SPEED']?.value,
      rpm: engine.latest['RPM']?.value,
      pedal: engine.latest['PEDAL']?.value,
    );

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
        if (prefs.showFuelCard && fuel != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 4, 12, 0),
            child: Card(
              margin: EdgeInsets.zero,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                child: Row(
                  children: [
                    const Icon(Icons.local_gas_station, size: 16, color: _muted),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        '${fuel.litresPerHour.toStringAsFixed(1)} л/ч'
                        ' · поездка ${engine.tripDistanceKm.toStringAsFixed(1)} км / '
                        '${engine.tripFuelLitres.toStringAsFixed(2)} л',
                        style: const TextStyle(fontSize: 12),
                      ),
                    ),
                    IconButton(
                      visualDensity: VisualDensity.compact,
                      tooltip: 'Сбросить поездку',
                      onPressed: engine.resetTrip,
                      icon: const Icon(Icons.restart_alt, size: 18),
                    ),
                  ],
                ),
              ),
            ),
          ),
        if (ids.isEmpty)
          const Expanded(
            child: Center(
              child: Padding(
                padding: EdgeInsets.all(24),
                child: Text(
                  'Нет активных PID.\nВкладка «PID» → выберите параметры '
                  '(для 4-байтовых нажмите «Все 4-байтовые*»).',
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
                final pid = SubaruPidLibrary.all.firstWhere(
                  (p) => p.id == ids[i],
                  orElse: () => SubaruPidLibrary.byId('RPM'),
                );
                return _Tile(
                  pid: pid,
                  sample: engine.latest[pid.id],
                  history: prefs.showSpark && !prefs.compact
                      ? engine.history[pid.id]
                      : null,
                  compact: prefs.compact,
                  ageMs: engine.ageMs(pid.id),
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
      builder: (_) => _DashboardConfigSheet(model: model),
    );
    if (result != null) {
      await model.updateDashboardPrefs(result);
      if (mounted) setState(() {});
    }
  }
}

class _Tile extends StatelessWidget {
  const _Tile({
    required this.pid,
    required this.sample,
    required this.history,
    required this.compact,
    required this.ageMs,
  });

  final SubaruPidDef pid;
  final PidSample? sample;
  final List<PidSample>? history;
  final bool compact;
  final int? ageMs;

  @override
  Widget build(BuildContext context) {
    final value = sample?.value;
    final stale = (ageMs ?? 999999) > 2500;
    final unsupported = sample?.allOnes ?? false;
    final text = value == null
        ? (unsupported ? 'н/д' : '—')
        : value.toStringAsFixed(pid.digits);

    return Card(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: EdgeInsets.all(compact ? 8 : 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    '${pid.id}${pid.extended ? ' *' : ''}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: compact ? 11 : 12,
                      fontWeight: FontWeight.bold,
                      color: stale ? _muted : null,
                    ),
                  ),
                ),
                if (stale)
                  const Icon(Icons.schedule, size: 12, color: _muted),
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
            Text(
              pid.unit,
              style: const TextStyle(fontSize: 10, color: _muted),
            ),
            if (history != null && history!.length > 2 && !compact) ...[
              const SizedBox(height: 4),
              Expanded(
                child: CustomPaint(
                  painter: _SparkPainter(history!, pid),
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
  _SparkPainter(this.samples, this.pid);
  final List<PidSample> samples;
  final SubaruPidDef pid;

  @override
  void paint(Canvas canvas, Size size) {
    final values = samples
        .where((s) => s.value != null)
        .map((s) => s.value!)
        .toList();
    if (values.length < 2) return;
    var lo = values.first, hi = values.first;
    for (final v in values) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if ((hi - lo).abs() < 1e-9) {
      hi = lo + 1;
    }
    final path = Path();
    for (var i = 0; i < values.length; i++) {
      final x = size.width * i / (values.length - 1);
      final y = size.height * (1 - (values[i] - lo) / (hi - lo));
      if (i == 0) {
        path.moveTo(x, y);
      } else {
        path.lineTo(x, y);
      }
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

/// Нижний лист настройки: порядок плиток, колонки, плотность.
class _DashboardConfigSheet extends StatefulWidget {
  const _DashboardConfigSheet({required this.model});
  final AppModel model;

  @override
  State<_DashboardConfigSheet> createState() => _DashboardConfigSheetState();
}

class _DashboardConfigSheetState extends State<_DashboardConfigSheet> {
  late DashboardPrefs prefs = widget.model.dashboardPrefs;

  @override
  Widget build(BuildContext context) {
    final active = widget.model.engine.active.map((p) => p.id).toList();
    final chosen = prefs.tiles.where(active.contains).toList();
    final available = active.where((id) => !chosen.contains(id)).toList();

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.8,
      maxChildSize: 0.95,
      builder: (context, scroll) => ListView(
        controller: scroll,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
        children: [
          const Text(
            'Настройка дашборда',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 4),
          const Text(
            'Перетаскивайте плитки за ручку, снимайте галочки чтобы скрыть.',
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
            subtitle: const Text('Больше параметров на экране, без графиков'),
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
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Карточка расхода'),
            value: prefs.showFuelCard,
            onChanged: (v) =>
                setState(() => prefs = prefs.copyWith(showFuelCard: v)),
          ),
          const Divider(height: 24),
          Text('На дашборде (${chosen.length})',
              style: const TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 8),
          ReorderableListView(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            buildDefaultDragHandles: false,
            onReorder: (oldIndex, newIndex) {
              setState(() {
                final list = List<String>.from(chosen);
                if (newIndex > oldIndex) newIndex -= 1;
                list.insert(newIndex, list.removeAt(oldIndex));
                final others =
                    prefs.tiles.where((id) => !chosen.contains(id)).toList();
                prefs = prefs.copyWith(tiles: [...list, ...others]);
              });
            },
            children: [
              for (var i = 0; i < chosen.length; i++)
                ListTile(
                  key: ValueKey('tile-${chosen[i]}'),
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: ReorderableDragStartListener(
                    index: i,
                    child: const Icon(Icons.drag_handle),
                  ),
                  title: Text(chosen[i]),
                  subtitle: Text(
                    SubaruPidLibrary.all
                        .firstWhere((p) => p.id == chosen[i])
                        .desc,
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
          if (available.isNotEmpty) ...[
            const Divider(height: 24),
            Text('Доступные PID (${available.length})',
                style: const TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final id in available)
                  ActionChip(
                    label: Text(id, style: const TextStyle(fontSize: 12)),
                    avatar: const Icon(Icons.add, size: 16),
                    onPressed: () => setState(() {
                      prefs = prefs.copyWith(tiles: [...prefs.tiles, id]);
                    }),
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

# ===== Вкладка «Мотор»: настройки железа + живые советы =====
FILES["lib/vehicle_config_page.dart"] = r'''
import 'package:flutter/material.dart';

import 'dashboard_prefs.dart';
import 'model.dart';
import 'vehicle_config.dart';

const _muted = Color(0xFF8CA0BF);

/// Вкладка «Мотор»: железо + живые расчёты топливной системы и наддува.
class VehicleConfigPage extends StatefulWidget {
  const VehicleConfigPage(this.model, {super.key});
  final AppModel model;

  @override
  State<VehicleConfigPage> createState() => _VehicleConfigPageState();
}

class _VehicleConfigPageState extends State<VehicleConfigPage> {
  AppModel get model => widget.model;
  VehicleConfig get cfg => model.vehicleConfig;

  Future<void> _set(VehicleConfig next) async {
    await model.updateVehicleConfig(next);
    if (mounted) setState(() {});
  }

  double? _live(String id) => model.engine.latest[id]?.value;

  @override
  Widget build(BuildContext context) {
    final advice = buildConfigAdvice(
      config: cfg,
      maf: _live('MAF'),
      afr: _live('AFR'),
      rpm: _live('RPM'),
      boostBar: _live('BOOST') ?? _live('MAP_REL'),
      injPulseMs: _live('INJPW'),
      requestedTorque: _live('REQ_TQ'),
    );

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        const Text('Конфигурация мотора',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
        const Text(
          'Эти параметры учитывают анализатор и Map Lab: лимиты форсунок, '
          'насоса и ожидания по наддуву зависят от вашего железа.',
          style: TextStyle(fontSize: 12, color: _muted),
        ),
        const SizedBox(height: 16),

        // --- турбо ---
        DropdownButtonFormField<TurboKind>(
          initialValue: cfg.turbo,
          decoration: const InputDecoration(labelText: 'Турбосистема'),
          items: [
            for (final t in TurboKind.values)
              DropdownMenuItem(
                value: t,
                child: Text(
                  '${t.label} · до ${t.typicalMaxBoostBar.toStringAsFixed(2)} бар',
                  style: const TextStyle(fontSize: 13),
                ),
              ),
          ],
          onChanged: (v) => v == null ? null : _set(cfg.copyWith(turbo: v)),
        ),
        const SizedBox(height: 12),

        // --- топливо ---
        DropdownButtonFormField<FuelKind>(
          initialValue: cfg.fuel,
          decoration: const InputDecoration(labelText: 'Топливо'),
          items: [
            for (final f in FuelKind.values)
              DropdownMenuItem(
                value: f,
                child: Text(
                  '${f.label} · стех ${f.stoich.toStringAsFixed(1)}',
                  style: const TextStyle(fontSize: 13),
                ),
              ),
          ],
          onChanged: (v) => v == null ? null : _set(cfg.copyWith(fuel: v)),
        ),
        const SizedBox(height: 8),

        _NumField(
          label: 'Форсунки, cc/min (на одну)',
          value: cfg.injectorCc.toDouble(),
          min: 150,
          max: 2200,
          digits: 0,
          onChanged: (v) => _set(cfg.copyWith(injectorCc: v.round())),
        ),
        _NumField(
          label: 'Латентность форсунки, мс @14В',
          value: cfg.injectorLatencyMs,
          min: 0.2,
          max: 3.0,
          digits: 2,
          onChanged: (v) => _set(cfg.copyWith(injectorLatencyMs: v)),
        ),
        _NumField(
          label: 'Бензонасос, л/ч',
          value: cfg.fuelPumpLph.toDouble(),
          min: 60,
          max: 600,
          digits: 0,
          onChanged: (v) => _set(cfg.copyWith(fuelPumpLph: v.round())),
        ),
        _NumField(
          label: 'Потолок наддува (свой лимит), бар',
          value: cfg.maxSafeBoostBar,
          min: 0.3,
          max: 2.5,
          digits: 2,
          onChanged: (v) => _set(cfg.copyWith(maxSafeBoostBar: v)),
        ),
        _NumField(
          label: 'Цель AFR на полной нагрузке',
          value: cfg.targetAfrWot,
          min: 9.0,
          max: 14.7,
          digits: 2,
          onChanged: (v) => _set(cfg.copyWith(targetAfrWot: v)),
        ),
        _NumField(
          label: 'Объём двигателя, л',
          value: cfg.displacementL,
          min: 1.0,
          max: 4.0,
          digits: 1,
          onChanged: (v) => _set(cfg.copyWith(displacementL: v)),
        ),

        const Divider(height: 28),
        const Text('Расчёт по текущей телеметрии',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        const SizedBox(height: 8),
        for (final a in advice)
          Card(
            margin: const EdgeInsets.only(bottom: 8),
            child: ListTile(
              dense: true,
              leading: Icon(
                a.level == AdviceLevel.critical
                    ? Icons.error
                    : a.level == AdviceLevel.warning
                        ? Icons.warning_amber
                        : Icons.check_circle,
                color: a.level == AdviceLevel.critical
                    ? Colors.redAccent
                    : a.level == AdviceLevel.warning
                        ? Colors.amberAccent
                        : Colors.greenAccent,
              ),
              title: Text(a.title, style: const TextStyle(fontSize: 14)),
              subtitle: Text(a.detail,
                  style: const TextStyle(fontSize: 12, color: _muted)),
            ),
          ),

        const Divider(height: 28),
        const Text('Экран',
            style: TextStyle(fontSize: 16, fontWeight: FontWeight.bold)),
        DropdownButtonFormField<OrientationMode>(
          initialValue: model.uiPrefs.orientation,
          decoration: const InputDecoration(labelText: 'Ориентация'),
          items: [
            for (final o in OrientationMode.values)
              DropdownMenuItem(
                value: o,
                child: Text(o.label, style: const TextStyle(fontSize: 13)),
              ),
          ],
          onChanged: (v) async {
            if (v == null) return;
            await model.updateUiPrefs(model.uiPrefs.copyWith(orientation: v));
            if (mounted) setState(() {});
          },
        ),
        const SizedBox(height: 24),
      ],
    );
  }
}

class _NumField extends StatelessWidget {
  const _NumField({
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.digits,
    required this.onChanged,
  });
  final String label;
  final double value, min, max;
  final int digits;
  final ValueChanged<double> onChanged;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(label, style: const TextStyle(fontSize: 13)),
                ),
                Text(
                  value.toStringAsFixed(digits),
                  style: const TextStyle(
                      fontSize: 14, fontWeight: FontWeight.bold),
                ),
              ],
            ),
            Slider(
              value: value.clamp(min, max).toDouble(),
              min: min,
              max: max,
              onChanged: onChanged,
            ),
          ],
        ),
      );
}
'''

# ===== 16 тестов: IDC, насос, потолки, советы, префы дашборда =====
FILES["test/config_test.dart"] = r'''
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/dashboard_prefs.dart';
import 'package:subaru_ssm2/vehicle_config.dart';

void main() {
  group('VehicleConfig', () {
    test('сериализация туда-обратно', () {
      const c = VehicleConfig(
        injectorCc: 1050,
        fuelPumpLph: 255,
        turbo: TurboKind.rotated,
        fuel: FuelKind.e85,
      );
      final back = VehicleConfig.fromJson(c.toJson());
      expect(back.injectorCc, 1050);
      expect(back.fuelPumpLph, 255);
      expect(back.turbo, TurboKind.rotated);
      expect(back.fuel, FuelKind.e85);
    });

    test('дефолты при пустом json', () {
      final c = VehicleConfig.fromJson(null);
      expect(c.injectorCc, 565);
      expect(c.turbo, TurboKind.stockTwinScroll);
    });
  });

  group('Расчёты топливной системы', () {
    test('IDC по ширине импульса', () {
      // 6000 об/мин -> период 20 мс; импульс 10 мс = 50%
      expect(injectorDutyFromPulse(pulseWidthMs: 10, rpm: 6000),
          closeTo(50.0, 0.001));
      expect(injectorDutyFromPulse(pulseWidthMs: 5, rpm: 0), 0);
    });

    test('IDC по MAF растёт с расходом воздуха', () {
      const c = VehicleConfig(injectorCc: 565, injectorCount: 4);
      final low = injectorDutyFromMaf(mafGramsPerSec: 50, afr: 11.5, config: c);
      final high = injectorDutyFromMaf(mafGramsPerSec: 200, afr: 11.5, config: c);
      expect(high, greaterThan(low));
      expect(low, greaterThan(0));
    });

    test('E85 требует больше топлива -> меньше эффективный расход форсунок', () {
      const gas = VehicleConfig(fuel: FuelKind.gasoline98);
      const e85 = VehicleConfig(fuel: FuelKind.e85);
      expect(totalInjectorFlowCc(e85), lessThan(totalInjectorFlowCc(gas)));
    });

    test('запас насоса падает при большом расходе', () {
      const c = VehicleConfig(fuelPumpLph: 190);
      final calm = pumpHeadroomPct(mafGramsPerSec: 30, afr: 14.7, config: c);
      final wot = pumpHeadroomPct(mafGramsPerSec: 250, afr: 11.0, config: c);
      expect(calm, greaterThan(wot));
    });

    test('потолки мощности положительны и растут с железом', () {
      const small = VehicleConfig(injectorCc: 380, fuelPumpLph: 150);
      const big = VehicleConfig(injectorCc: 1050, fuelPumpLph: 340);
      expect(maxPowerByInjectors(big), greaterThan(maxPowerByInjectors(small)));
      expect(maxPowerByPump(big), greaterThan(maxPowerByPump(small)));
      expect(maxPowerByInjectors(small), greaterThan(0));
    });
  });

  group('Советы по конфигурации', () {
    test('критично при IDC >= 95%', () {
      const c = VehicleConfig(injectorCc: 380, injectorCount: 4);
      final advice = buildConfigAdvice(
        config: c,
        injPulseMs: 19.5,
        rpm: 6000,
      );
      expect(
        advice.any((a) =>
            a.level == AdviceLevel.critical && a.title.contains('Форсунки')),
        isTrue,
      );
    });

    test('перебуст выше личного лимита', () {
      const c = VehicleConfig(maxSafeBoostBar: 1.0);
      final advice = buildConfigAdvice(config: c, boostBar: 1.4);
      expect(advice.any((a) => a.title.startsWith('Перебуст')), isTrue);
    });

    test('чистая конфигурация без критичных советов', () {
      const c = VehicleConfig(injectorCc: 1050, fuelPumpLph: 340);
      final advice = buildConfigAdvice(
        config: c,
        maf: 120,
        afr: 11.2,
        rpm: 5000,
        boostBar: 0.9,
      );
      expect(advice.any((a) => a.level == AdviceLevel.critical), isFalse);
    });
  });

  group('DashboardPrefs', () {
    test('дефолтный набор плиток не пуст', () {
      expect(DashboardPrefs().tiles, isNotEmpty);
      expect(DashboardPrefs().tiles, contains('RPM'));
    });

    test('сериализация и ограничение колонок', () {
      final p = DashboardPrefs(
        tiles: ['RPM', 'AFR'],
        columnsPortrait: 99,
        columnsLandscape: 0,
        compact: true,
      );
      final back = DashboardPrefs.fromJson(p.toJson());
      expect(back.tiles, ['RPM', 'AFR']);
      expect(back.columnsPortrait, lessThanOrEqualTo(6));
      expect(back.columnsLandscape, greaterThanOrEqualTo(1));
      expect(back.compact, isTrue);
    });

    test('мусорные id отбрасываются, пустой список -> дефолт', () {
      final back = DashboardPrefs.fromJson({'tiles': ['NOPE', 'ZZZ']});
      expect(back.tiles, DashboardPrefs.defaultTiles);
    });

    test('visibleTiles фильтрует по активным PID', () {
      final p = DashboardPrefs(tiles: ['RPM', 'AFR', 'FBKC']);
      expect(p.visibleTiles({'RPM', 'FBKC'}), ['RPM', 'FBKC']);
    });
  });

  group('UiPrefs', () {
    test('ориентация сохраняется', () {
      const u = UiPrefs(orientation: OrientationMode.landscape);
      expect(UiPrefs.fromJson(u.toJson()).orientation,
          OrientationMode.landscape);
    });
    test('дефолт — авто', () {
      expect(UiPrefs.fromJson(null).orientation, OrientationMode.auto);
    });
  });
}
'''

# ===== Правки: разблокировка 4-байтовых, CSV-имена, вкладки, ориентация =====
# ===== REWRITES =====

# --- 1. model.dart: импорты новых модулей ---
REWRITES.append((
    "lib/model.dart",
    "import 'bt_transport.dart';\nimport 'diag.dart';",
    "import 'bt_transport.dart';\nimport 'dashboard_prefs.dart';\nimport 'diag.dart';",
    ["import 'dashboard_prefs.dart';", False],
))
REWRITES.append((
    "lib/model.dart",
    "import 'pids.dart';\nimport 'protocol.dart';",
    "import 'pids.dart';\nimport 'protocol.dart';\nimport 'vehicle_config.dart';",
    ["import 'vehicle_config.dart';", False],
))

# --- 2. model.dart: RomRaider-имена для новых PID (Map Lab видит колонки) ---
REWRITES.append((
    "lib/model.dart",
    "  'CL_TARGET': 'Closed Loop Fueling Target (AFR)*',\n};",
    """  'CL_TARGET': 'Closed Loop Fueling Target (AFR)*',
  // v0.12: имена logger-дефиниций RomRaider для PID из патча 04.2
  'REQ_TQ': "Requested Torque*",
  'KC_ADV_4B': "Knock Correction Advance (4-byte) (degrees)*",
  'KC_IAM': "Knock Correction Advance (IAM only) (degrees)*",
  'BTIMING': "Ignition Base Timing (degrees)*",
  'KCMAX': "Knock Correction Advance Max Primary (degrees)*",
  'KNOCK_SUM': "Knock Sum (count)*",
  'AFL_A': "A/F Learning #1 A (Stored) (%)*",
  'AFL_B': "A/F Learning #1 B (Stored) (%)*",
  'AFL_C': "A/F Learning #1 C (Stored) (%)*",
  'AFL_D': "A/F Learning #1 D (Stored) (%)*",
  'AFL_4B': "A/F Learning #1 (4-byte) (%)*",
  'AFCOR1_4B': "A/F Correction #1 (4-byte) (%)*",
  'TIPIN': "Tip-in Throttle (%)*",
  'TTH_TGT': "Target Throttle Plate Position (%)*",
  'THROTTLE_4B': "Throttle Plate Opening Angle (4-byte) (%)*",
  'CLOL': "CL/OL Fueling*",
  'FL_OFFSET': "Fine Learning Table Offset*",
  'GEAR_CALC': "Gear (Calculated)*",
  'TDIG_INT': "Turbo Dynamics Integral (4-byte) (absolute %)*",
  'TDIG_PROP': "Turbo Dynamics Proportional (4-byte) (absolute %)*",
  'INJPW': "Fuel Injector #1 Pulse Width (4-byte) (ms)*",
  'INJ_LAT': "Fuel Injector #1 Latency (4-byte) (ms)*",
  'OLE_ENRICH': "Primary Open Loop Map Enrichment (4-byte) (estimated AFR)*",
  'ENRICH_FINAL': "Primary Enrichment Final (4-byte) (estimated AFR)*",
  'FUELBASE': "Final Fueling Base (4-byte) (estimated AFR)*",
  'MAP_4B': "Manifold Absolute Pressure (4-byte) (bar absolute)*",
  'TBOOST_REL': "Target Boost Relative (4-byte) (bar relative)*",
  'IDLE_SEL': "Idle Speed Map Selection*",
  'AFL_RANGE': "A/F Learning Airflow Range (Current)*",
};""",
    ["'REQ_TQ':", "'KNOCK_SUM':", False],
))

# --- 3. model.dart: поля конфигурации + методы обновления ---
REWRITES.append((
    "lib/model.dart",
    "  late final DiagSession diagSession =\n      DiagSession(elm, experimental: experimental);",
    """  late final DiagSession diagSession =
      DiagSession(elm, experimental: experimental);

  // v0.12 (04.3): конфигурация железа, дашборда и экрана
  VehicleConfig vehicleConfig = const VehicleConfig();
  DashboardPrefs dashboardPrefs = DashboardPrefs();
  UiPrefs uiPrefs = const UiPrefs();

  Future<void> updateVehicleConfig(VehicleConfig next) async {
    vehicleConfig = next;
    await saveSettings();
    changed();
  }

  Future<void> updateDashboardPrefs(DashboardPrefs next) async {
    dashboardPrefs = next;
    await saveSettings();
    changed();
  }

  Future<void> updateUiPrefs(UiPrefs next) async {
    uiPrefs = next;
    await saveSettings();
    changed();
  }

  /// v0.12: включить все ROM-зависимые (4-байтовые) PID разом.
  /// Раньше они молча вырезались, пока вручную не подтвердишь ROM ID.
  Future<void> enableExtendedPids() => perform(() async {
        final rom = engine.romId.trim().isEmpty
            ? kDefaultRomId
            : engine.romId.trim();
        await engine.configure(
          {
            ...engine.enabled,
            ...SubaruPidLibrary.all.where((p) => p.extended).map((p) => p.id),
          },
          true,
          rom,
          engine.endian,
        );
        await saveSettings();
        message = 'ROM $rom подтверждён: 4-байтовые PID активны';
      });""",
    ["updateVehicleConfig", "enableExtendedPids", False],
))

# --- 4. model.dart: ROM по умолчанию ---
REWRITES.append((
    "lib/model.dart",
    "const Map<String, String> kRrNames = <String, String>{",
    """/// v0.12: ROM по умолчанию — профиль из identity.dart (адреса подтверждены
/// по logger-дефинициям RomRaider для ECU 5204584007).
const String kDefaultRomId = 'A2TB100B';

const Map<String, String> kRrNames = <String, String>{""",
    ["const String kDefaultRomId", False],
))

# --- 5. model.dart: restore — применять дефолты даже без файла настроек ---
REWRITES.append((
    "lib/model.dart",
    "      if (!await file.exists() || _disposed) return;",
    """      if (_disposed) return;
      if (!await file.exists()) {
        // v0.12: первый запуск — сразу открываем 4-байтовые PID по профилю ROM
        await engine.configure(
          {...SubaruPidLibrary.defaults},
          true,
          kDefaultRomId,
          engine.endian,
        );
        changed();
        return;
      }""",
    ["первый запуск — сразу открываем", False],
))

# --- 6. model.dart: restore — читать конфиги + ROM по умолчанию ---
REWRITES.append((
    "lib/model.dart",
    """      await engine.configure(
        ids,
        json['confirmed'] == true,
        json['rom'] as String? ?? '',
        json['endian'] == 'little' ? Endian.little : Endian.big,
      );""",
    """      final savedRom = (json['rom'] as String? ?? '').trim();
      await engine.configure(
        ids,
        json['confirmed'] as bool? ?? true,
        savedRom.isEmpty ? kDefaultRomId : savedRom,
        json['endian'] == 'little' ? Endian.little : Endian.big,
      );
      vehicleConfig =
          VehicleConfig.fromJson(json['vehicle'] as Map<String, dynamic>?);
      dashboardPrefs =
          DashboardPrefs.fromJson(json['dashboard'] as Map<String, dynamic>?);
      uiPrefs = UiPrefs.fromJson(json['ui'] as Map<String, dynamic>?);""",
    ["VehicleConfig.fromJson(json['vehicle']", False],
))

# --- 7. model.dart: saveSettings — писать конфиги ---
REWRITES.append((
    "lib/model.dart",
    "        'adapter': selected, // v0.10: MAC адаптера переживает перезапуск",
    """        'adapter': selected, // v0.10: MAC адаптера переживает перезапуск
        'vehicle': vehicleConfig.toJson(), // v0.12
        'dashboard': dashboardPrefs.toJson(),
        'ui': uiPrefs.toJson(),""",
    ["'vehicle': vehicleConfig.toJson()", False],
))

# --- 8. main.dart: импорты новых страниц ---
REWRITES.append((
    "lib/main.dart",
    "import 'engine.dart';\nimport 'dtc_service_page.dart';",
    "import 'dashboard_page.dart';\nimport 'dashboard_prefs.dart';\nimport 'engine.dart';\nimport 'dtc_service_page.dart';\nimport 'vehicle_config_page.dart';",
    ["import 'dashboard_page.dart';", False],
))

# --- 9. main.dart: включить все ориентации явно ---
REWRITES.append((
    "lib/main.dart",
    "void main() {\n  WidgetsFlutterBinding.ensureInitialized();\n  runApp(const SsmApp());",
    """void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // v0.12 (04.3): книжная И альбомная — ориентацию выбирает пользователь
  // во вкладке «Мотор» (Авто / Только книжная / Только альбомная).
  SystemChrome.setPreferredOrientations(const <DeviceOrientation>[
    DeviceOrientation.portraitUp,
    DeviceOrientation.portraitDown,
    DeviceOrientation.landscapeLeft,
    DeviceOrientation.landscapeRight,
  ]);
  runApp(const SsmApp());""",
    ["DeviceOrientation.landscapeLeft", False],
))

# --- 10. main.dart: дашборд -> настраиваемый + вкладка «Мотор» ---
REWRITES.append((
    "lib/main.dart",
    "            DashboardPage(model),",
    "            CustomDashboardPage(model),",
    ["CustomDashboardPage(model)", False],
))
REWRITES.append((
    "lib/main.dart",
    "            DtcServicePage(model: model),\n            const MapLabTab(),",
    "            DtcServicePage(model: model),\n            VehicleConfigPage(model),\n            const MapLabTab(),",
    ["VehicleConfigPage(model)", False],
))
REWRITES.append((
    "lib/main.dart",
    """                NavigationDestination(
                    icon: Icon(Icons.build_circle_outlined), label: 'DTC'),""",
    """                NavigationDestination(
                    icon: Icon(Icons.build_circle_outlined), label: 'DTC'),
                NavigationDestination(
                    icon: Icon(Icons.settings_suggest), label: 'Мотор'),""",
    ["label: 'Мотор'", False],
))

# --- 11. main.dart: применение выбранной ориентации ---
REWRITES.append((
    "lib/main.dart",
    "          final pages = <Widget>[",
    """          // v0.12: пользовательский режим ориентации
          final mode = model.uiPrefs.orientation;
          SystemChrome.setPreferredOrientations(
            mode == OrientationMode.portrait
                ? const <DeviceOrientation>[
                    DeviceOrientation.portraitUp,
                    DeviceOrientation.portraitDown,
                  ]
                : mode == OrientationMode.landscape
                    ? const <DeviceOrientation>[
                        DeviceOrientation.landscapeLeft,
                        DeviceOrientation.landscapeRight,
                      ]
                    : const <DeviceOrientation>[
                        DeviceOrientation.portraitUp,
                        DeviceOrientation.portraitDown,
                        DeviceOrientation.landscapeLeft,
                        DeviceOrientation.landscapeRight,
                      ],
          );
          final pages = <Widget>[""",
    ["пользовательский режим ориентации", False],
))

# --- 12. main.dart: заголовок версии ---
REWRITES.append((
    "lib/main.dart",
    "SSM2 TELEMETRY 0.11",
    "SSM2 TELEMETRY 0.12",
    ["SSM2 TELEMETRY 0.12", True],
))

# --- 13. main.dart: PID-страница — кнопка разблокировки 4-байтовых ---
REWRITES.append((
    "lib/main.dart",
    """              TextButton(
                onPressed: m.busy ? null : () => _settings(context),
                child: const Text('ROM / Float32'),
              ),""",
    """              TextButton(
                onPressed: m.busy ? null : () => _settings(context),
                child: const Text('ROM / Float32'),
              ),
              // v0.12 (04.3): разблокировка ROM-зависимых PID одной кнопкой
              FilledButton.tonal(
                onPressed: m.busy ? null : m.enableExtendedPids,
                child: const Text('Все 4-байтовые*'),
              ),""",
    ["Все 4-байтовые*", False],
))
REWRITES.append((
    "lib/main.dart",
    "        section('Библиотека / 28 PID', [",
    "        section('Библиотека / ${SubaruPidLibrary.all.length} PID', [",
    ["Библиотека / ${SubaruPidLibrary.all.length} PID", False],
))
REWRITES.append((
    "lib/main.dart",
    """          const Text(
            'Сначала 8 базовых. Больше параметров — ниже частота каждого PID.',
            style: TextStyle(color: muted),
          ),""",
    """          Text(
            m.engine.extendedAllowed
                ? 'ROM ${m.engine.romId} подтверждён: 4-байтовые (*) доступны. '
                    'Больше PID — ниже частота каждого.'
                : '4-байтовые (*) PID заблокированы: нажмите «Все 4-байтовые*» '
                    'или укажите ROM ID в «ROM / Float32».',
            style: const TextStyle(color: muted),
          ),""",
    ["4-байтовые (*) PID заблокированы", False],
))

# --- 14. Map Lab: алиасы для новых каналов из CSV ---
REWRITES.append((
    "lib/maps_lab/maps_lab_log.dart",
    "    'wgdc': [",
    """    'idc': ['injector duty', 'idc', 'injector duty cycle'],
    'injpw': [
      'fuel injector #1 pulse width',
      'injector pulse width',
      'injpw',
    ],
    'knocksum': ['knock sum', 'knock_sum', 'knocksum'],
    'wgdc': [""",
    ["'knocksum'", False],
))

# ===== Запись, правки, самопроверка, чек-лист стенда =====
# ===== запись файлов =====
bad_keys = [rel for rel in FILES if "(" in rel or ")" in rel]
if bad_keys:
    raise RuntimeError(f"Невалидные FILES-ключи: {bad_keys}")
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
        raise RuntimeError(f"Нет файла {rel} — сначала прогоните 02–04.2")
    text = path.read_text(encoding="utf-8")
    # СНАЧАЛА сигнатура: правка уже применена -> ничего не делаем
    # (игла может сохраняться в тексте после вставки — тогда повтор дублировал бы код)
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
    raise RuntimeError(
        f"Маркер не найден в {rel}:\n{needle[:100]!r}")
print(f"[OK] правок: {patched}, пропущено: {skipped}")

# ===== самопроверка =====
checks = {
    "lib/vehicle_config.dart": ["class VehicleConfig", "TurboKind", "injectorDutyFromPulse", "pumpHeadroomPct"],
    "lib/dashboard_prefs.dart": ["class DashboardPrefs", "OrientationMode", "class UiPrefs"],
    "lib/dashboard_page.dart": ["CustomDashboardPage", "ReorderableListView"],
    "lib/vehicle_config_page.dart": ["VehicleConfigPage", "buildConfigAdvice"],
    "lib/model.dart": ["const String kDefaultRomId", "enableExtendedPids", "updateVehicleConfig", "'REQ_TQ':"],
    "lib/main.dart": ["CustomDashboardPage", "VehicleConfigPage", "DeviceOrientation.landscapeLeft", "Все 4-байтовые*"],
    "lib/maps_lab/maps_lab_log.dart": ["'knocksum'", "'injpw'"],
    "test/config_test.dart": ["injectorDutyFromPulse", "DashboardPrefs"],
}
for rel, needles in checks.items():
    text = (APP / rel).read_text(encoding="utf-8")
    missing = [m for m in needles if m not in text]
    if missing:
        raise RuntimeError(f"Самопроверка {rel}: нет маркеров {missing}")
print("[OK] самопроверка маркеров пройдена")

print("\n=== Готово: SSM2 0.12. Далее — ячейка 05 (сборка). ===")
print("Что нового:")
print("  · 4-байтовые PID разблокированы автоматически (ROM A2TB100B)")
print("  · вкладка «Мотор»: форсунки, насос, турбо, топливо + живые расчёты IDC")
print("  · дашборд настраивается: состав, порядок, колонки, компактность")
print("  · ориентация: авто / книжная / альбомная")
print("Чек-лист стенда 04.3:")
print(" [ ] на дашборде видны FBKC/IAM/REQ_TQ (4-байтовые)")
print(" [ ] CSV-экспорт содержит колонки Requested Torque* и Knock Sum*")
print(" [ ] поворот телефона — раскладка перестраивается, колонок больше")
print(" [ ] вкладка «Мотор»: IDC растёт с газом, потолки мощности адекватны")
print(" [ ] перетаскивание плиток сохраняется после перезапуска приложения")
