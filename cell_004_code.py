# @title 04 | Map Lab UI (финальный layout): большая карта + скролл всей страницы { display-mode: "form" }
from pathlib import Path
import json, re

CONFIG = Path("/content/ssm2_fixed_env.json")
CFG = json.loads(CONFIG.read_text(encoding="utf-8"))
APP = Path(CFG["app"])
pub = (APP / "pubspec.yaml").read_text(encoding="utf-8")
PKG = re.search(r"(?m)^name:\s*(\S+)", pub).group(1)

PAGE = r'''
/// Экран Map Lab: крупная карта + прокрутка всей страницы (журнал виден).
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
  /// Высота окна карты (крупнее, чем «сжатый» Expanded).
  static const double _mapHeight = 480;

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
      // v0.9: снимок исходной таблицы ДО правок — страховка для отката.
      final origRows = <String>[
        for (var ri = 0; ri < g.rows; ri++)
          '${g.y.values[ri].toStringAsFixed(2)}\t${[
            for (var ci = 0; ci < g.cols; ci++) g.data[ri][ci].toStringAsFixed(3),
          ].join('\t')}',
      ];
      final origFile =
          File('${outDir.path}/${e.key.replaceAll(RegExp(r'[/ ]'), '_')}_original.tsv')
            ..writeAsStringSync('$head\n${origRows.join('\n')}\n');
      files.add(XFile(origFile.path));
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
    await SharePlus.instance.share(
        ShareParams(files: files, text: 'Map Lab — рекомендации и снимки исходных карт'));
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final map = _maps == null ? null : _maps![_mapName];
    MapResult? res;
    if (map != null) res = _results?[map.name];

    // Весь экран снова в одном ListView — журнал всегда можно долистать.
    // Карта фиксированной большой высоты; 3D крутится через EagerPan внутри map3d_view.
    return Scaffold(
      appBar: AppBar(
        title: const Text('Map Lab · A2TB100B'),
        actions: [
          if (_results != null)
            IconButton(icon: const Icon(Icons.ios_share), onPressed: _export, tooltip: 'Экспорт'),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 24),
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
            const SizedBox(height: 10),
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
            const SizedBox(height: 6),
            Row(
              children: [
                Expanded(
                  child: Text(
                    map == null ? '' : '${map.name} · ${map.rows}×${map.cols}',
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
          if (map != null) ...[
            const SizedBox(height: 8),
            SizedBox(
              height: _mapHeight,
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
            if (_selRi != null && _selCi != null) ...[
              const SizedBox(height: 8),
              _inspector(map, res),
            ],
            const SizedBox(height: 8),
            Wrap(spacing: 12, runSpacing: 4, children: [
              _legend(const Color(0xFF4CAF50), 'низ'),
              _legend(const Color(0xFFFFEB3B), 'середина'),
              _legend(const Color(0xFFF44336), 'верх'),
              _legend(Colors.lightBlueAccent, 'правка'),
            ]),
          ],
          const SizedBox(height: 16),
          Text('Журнал', style: theme.textTheme.titleSmall),
          const SizedBox(height: 6),
          Container(
            width: double.infinity,
            constraints: const BoxConstraints(minHeight: 180),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: theme.colorScheme.surfaceContainerHighest.withValues(alpha: 0.35),
              borderRadius: BorderRadius.circular(8),
            ),
            child: _journal.isEmpty
                ? const Text('— пока пусто —', style: TextStyle(color: Colors.white38, fontSize: 12))
                : Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      for (final j in _journal)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 2),
                          child: Text(j, style: const TextStyle(fontFamily: 'monospace', fontSize: 11)),
                        ),
                    ],
                  ),
          ),
          const SizedBox(height: 12),
          const Text(
            'Листайте экран вниз, чтобы читать журнал. '
            'На 3D-карте тяните пальцем — страница при этом не уезжает.',
            style: TextStyle(fontSize: 11, color: Colors.white38),
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
'''

dest = APP / "lib/maps_lab/maps_lab_page.dart"
dest.write_text(PAGE.replace("__PKG__", PKG) + "\n", encoding="utf-8")
print("[ok] maps_lab_page.dart: карта 480px + ListView (журнал долистывается)")
print("Дальше: ячейка 3/3 (сборка APK).")