# coding: utf-8
# @title 04.8 | OP2 OTG FIX | SSM2 0.14.1 >> 0.15.0 (insert BETWEEN 04.7 and 05)
# MOTIVATION:
# Bluetooth ELM327 покрывает только CAN-машины. Tactrix OpenPort 2.0 по USB
# OTG (FTDI, VID 0x0403 / PID 0xCC4C) даёт тот же SSM-каскад проводом и
# открывает K-line для pre-CAN Subaru. BT-сборка по умолчанию НЕ меняется.
#
# Что делает ячейка:
#   [+] android/.../Op2Channel.kt        — нативный USB-host канал (FTDI bulk),
#                                          БЕЗ pub-плагинов и без JitPack
#   [+] res/xml/op2_device_filter.xml    — VID/PID OP2 и клонов
#   [+] patch AndroidManifest.xml        — uses-feature usb.host + attach-intent
#   [+] patch MainActivity.kt            — регистрация Op2Channel (якорь-вставка)
#   [+] lib/native_op2.dart              — Dart-обёртка, контракт как у SPP
#   [+] patch lib/transport_selected.dart— ветка native_op2 по --dart-define
#   [+] test/op2_transport_test.dart     — чистые тесты ID-таблицы
#   [+] tool/op2_j2534_notes.md          — стендовый чеклист J2534
import re
import shutil
import subprocess
import time
from pathlib import Path

APP = Path("/content/subaru_ssm2_fixed")
STAMP = time.strftime("%Y%m%d_%H%M%S")
BACKUP = APP.parent / f"ssm2_backup_048_{STAMP}"

# --- Параметры модуля ---
OP2_BAUD = 460800            # @param {type:"integer"}  # [HARDWARE-TUNE] стартовое
OP2_LATENCY_MS = 2           # @param {type:"integer"}  # FTDI latency timer
PATCH_TRANSPORT = True       # @param {type:"boolean"}  # ветка в transport_selected

if not (APP / "build_config.json").exists():
    raise RuntimeError("Не найден проект. Выполните ячейки 01 и 02.")
MAIN_ACTIVITY = APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/MainActivity.kt"
MANIFEST = APP / "android/app/src/main/AndroidManifest.xml"
TRANSPORT_SEL = APP / "lib/transport_selected.dart"
for need in (MAIN_ACTIVITY, MANIFEST, TRANSPORT_SEL, APP / "lib/native_spp.dart"):
    if not need.exists():
        raise RuntimeError(f"Нет файла: {need}. Сначала прогоните 02 и фиксы 04.1-04.7.")
BACKUP.mkdir(parents=True, exist_ok=True)

# ============================================================
# 1. Kotlin: нативный FTDI-канал OP2 (UsbManager, без библиотек)
# ============================================================
OP2_CHANNEL_KT = r'''package com.subaru.ssm2_fixed

// v0.15.0 (OP2 OTG): нативный USB-host транспорт для Tactrix OpenPort 2.0.
// FTDI-клон чипа: VID 0x0403, PID 0xCC4C (оригинал), H-series контроллер.
// Канал "ssm2/op2" (методы) + "ssm2/op2_rx" (поток байтов).
// J2534-PAYLOAD-MAP: маппинг SSM>A8 00.. на PassThru-кадры подтверждается
// стендом по tool/op2_j2534_notes.md ДО снятия HARDWARE-гейта.

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
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

object Op2Ids {
    const val VID_TACTRIX = 0x0403
    val KNOWN_PIDS = intArrayOf(0xCC4C, 0xCC48, 0x6001) // OP2, клон, FTDI-default

    fun isOp2(d: UsbDevice): Boolean =
        d.vendorId == VID_TACTRIX && KNOWN_PIDS.contains(d.productId)
}

/** Минимальный FTDI H-series драйвер: reset/baud/data/latency/DTR/RTS + bulk IO. */
class FtdiPipe(
    private val conn: UsbDeviceConnection,
    private val iface: UsbInterface,
    private val epIn: UsbEndpoint,
    private val epOut: UsbEndpoint,
) {
    private fun ctrl(req: Int, value: Int, index: Int): Int =
        conn.controlTransfer(0x40, req, value, index, null, 0, 300)

    fun open(baud: Int, latencyMs: Int) {
        val idx = iface.id
        ctrl(0x00, 0x0000, idx)          // RESET
        ctrl(0x00, 0x0001, idx)          // purge RX
        ctrl(0x00, 0x0002, idx)          // purge TX
        setBaud(baud, idx)
        ctrl(0x04, 0x0008, idx)          // 8 data | no parity | 1 stop
        ctrl(0x09, latencyMs.coerceIn(1, 255), idx) // LATENCY_TIMER, мс
        ctrl(0x01, 0x0303, idx)          // DTR=1 RTS=1
        ctrl(0x02, 0x0000, idx)          // flow control off
    }

    private fun setBaud(baud: Int, ifIdx: Int) {
        // FTDI: divisor = 3 МГц / baud (48 МГц / 16). Доли - по таблице 1/8.
        val fracEnc = intArrayOf(0, 3, 2, 4, 1, 5, 6, 7)
        val raw = 3_000_000.0 / baud
        var div = raw.toInt()
        var frac = Math.round((raw - div) * 8).toInt()
        if (frac == 8) { div += 1; frac = 0 }
        val value = div and 0x3FFF
        val index = (fracEnc[frac] shl 8) or (ifIdx + 1) // H-серия: low byte = iface+1
        ctrl(0x03, value, index)
    }

    fun write(bytes: ByteArray, timeoutMs: Int): Int =
        conn.bulkTransfer(epOut, bytes, bytes.size, timeoutMs)

    fun read(buf: ByteArray, timeoutMs: Int): Int =
        conn.bulkTransfer(epIn, buf, buf.size, timeoutMs)

    fun close() = runCatching { conn.releaseInterface(iface); conn.close() }
}

object Op2Channel : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private const val TAG = "ssm2/op2"
    private lateinit var context: Context
    private lateinit var manager: UsbManager
    private var pipe: FtdiPipe? = null
    private var reader: Thread? = null
    @Volatile private var running = false
    @Volatile private var baud = 460800
    @Volatile private var latencyMs = 2
    private var sink: EventChannel.EventSink? = null
    private var permissionResult: MethodChannel.Result? = null

    private val attachReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            if (intent.action == "com.subaru.ssm2_fixed.OP2_PERMISSION") {
                val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                if (granted) openPipe()?.let { permissionResult?.success(it) }
                    ?: permissionResult?.error("op2", "open failed", null)
                else permissionResult?.success(mapOf("granted" to false))
                permissionResult = null
            }
        }
    }

    fun register(context: Context, engine: FlutterEngine) {
        this.context = context.applicationContext
        this.manager = context.getSystemService(Context.USB_SERVICE) as UsbManager
        val filter = IntentFilter("com.subaru.ssm2_fixed.OP2_PERMISSION")
        if (Build.VERSION.SDK_INT >= 33) {
            context.registerReceiver(attachReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            context.registerReceiver(attachReceiver, filter)
        }
        MethodChannel(engine.dartExecutor.binaryMessenger, TAG).setMethodCallHandler(this)
        EventChannel(engine.dartExecutor.binaryMessenger, "ssm2/op2_rx").setStreamHandler(this)
    }

    override fun onMethodCall(call: io.flutter.plugin.common.MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "op2/baud" -> {
                baud = (call.argument<Int>("baud") ?: 460800)
                latencyMs = (call.argument<Int>("latencyMs") ?: 2)
                result.success(true)
            }
            "op2/descriptor" -> result.success(descriptor())
            "op2/open" -> {
                baud = (call.argument<Int>("baud") ?: baud)
                latencyMs = (call.argument<Int>("latencyMs") ?: latencyMs)
                val dev = manager.deviceList.values.firstOrNull(Op2Ids::isOp2)
                if (dev == null) { result.success(mapOf("granted" to true, "found" to false)); return }
                if (manager.hasPermission(dev)) openPipe()?.let { result.success(it) }
                    ?: result.error("op2", "open failed", null)
                else {
                    permissionResult = result
                    val pi = PendingIntent.getBroadcast(
                        context, 0, Intent("com.subaru.ssm2_fixed.OP2_PERMISSION"),
                        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                    manager.requestPermission(dev, pi)
                }
            }
            "op2/write" -> {
                val data = call.arguments as? ByteArray
                val n = data?.let { pipe?.write(it, 250) } ?: -1
                if (n >= 0) result.success(n) else result.error("op2", "write failed", null)
            }
            "op2/close" -> { stopReader(); result.success(true) }
            else -> result.notImplemented()
        }
    }

    private fun descriptor(): Map<String, Any?> {
        val dev = manager.deviceList.values.firstOrNull(Op2Ids::isOp2)
        return mapOf(
            "found" to (dev != null),
            "vid" to dev?.vendorId?.let { "0x%04X".format(it) },
            "pid" to dev?.productId?.let { "0x%04X".format(it) },
            "name" to dev?.deviceName,
            "product" to dev?.productName,
            "hasPermission" to (dev?.let(manager::hasPermission) ?: false),
        )
    }

    private fun openPipe(): Map<String, Any?>? {
        val dev = manager.deviceList.values.firstOrNull(Op2Ids::isOp2) ?: return null
        val conn = manager.openDevice(dev) ?: return null
        // Канал J2534 - интерфейс 0 (MI_00); [HARDWARE-TUNE] проверить на клоне
        val iface = dev.getInterface(0)
        if (!conn.claimInterface(iface, true)) { conn.close(); return null }
        var epIn: UsbEndpoint? = null
        var epOut: UsbEndpoint? = null
        for (i in 0 until iface.endpointCount) {
            val ep = iface.getEndpoint(i)
            if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
            if (ep.direction == UsbConstants.USB_DIR_IN) epIn = ep else epOut = ep
        }
        if (epIn == null || epOut == null) { conn.close(); return null }
        val p = FtdiPipe(conn, iface, epIn, epOut)
        p.open(baud, latencyMs)
        pipe = p
        startReader(p)
        return mapOf(
            "granted" to true, "found" to true,
            "vid" to "0x%04X".format(dev.vendorId),
            "pid" to "0x%04X".format(dev.productId),
            "baud" to baud, "latencyMs" to latencyMs,
        )
    }

    private fun startReader(p: FtdiPipe) {
        running = true
        reader = Thread {
            val buf = ByteArray(4096)
            while (running) {
                val n = runCatching { p.read(buf, 100) }.getOrDefault(-1)
                if (n > 0) sink?.success(buf.copyOfRange(0, n))
            }
        }.also { it.isDaemon = true; it.start() }
    }

    private fun stopReader() {
        running = false
        reader?.join(300)
        reader = null
        pipe?.close()
        pipe = null
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink?) { sink = events }
    override fun onCancel(arguments: Any?) { sink = null }
}
'''

# ============================================================
# 2. USB device filter
# ============================================================
DEVICE_FILTER_XML = '''<?xml version="1.0" encoding="utf-8"?>
<!-- v0.15.0 (OP2 OTG): Tactrix OpenPort 2.0 и FTDI-клоны -->
<resources>
    <usb-device vendor-id="1027" product-id="52300" />  <!-- 0x0403:0xCC4C оригинал -->
    <usb-device vendor-id="1027" product-id="52296" />  <!-- 0x0403:0xCC48 клон -->
    <usb-device vendor-id="1027" product-id="24577" />  <!-- 0x0403:0x6001 FTDI default -->
</resources>
'''

# ============================================================
# 3. Dart: обёртка + чистые хелперы (тестируются без канала)
# ============================================================
NATIVE_OP2_DART = r'''// v0.15.0 (OP2 OTG): Dart-сторона J2534-транспорта OpenPort 2.0.
// Контракт совпадает с native_spp.dart: open / write / bytes / close,
// поэтому elm.dart и engine.dart не замечают подмены среды.
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/services.dart';

/// Известные сигнатуры OpenPort 2.0 (FTDI).
const int kOp2Vid = 0x0403; // Tactrix/FTDI
const List<int> kOp2Pids = [0xCC4C, 0xCC48, 0x6001];

String hex16(int v) =>
    '0x' + v.toRadixString(16).toUpperCase().padLeft(4, '0');

/// Описание устройства для UI/диагностики.
Map<String, String> describeOp2(Map<String, Object?> d) {
  final out = <String, String>{'found': d['found'].toString()};
  for (final key in const ['vid', 'pid', 'product']) {
    final value = d[key];
    if (value != null) out[key] = value.toString();
  }
  return out;
}

/// true, если пара VID/PID похожа на OP2 (оригинал или FTDI-клон).
bool looksLikeOp2(int vid, int pid) => vid == kOp2Vid && kOp2Pids.contains(pid);

class NativeOp2Transport {
  static const MethodChannel _tx = MethodChannel('ssm2/op2');
  static const EventChannel _rx = EventChannel('ssm2/op2_rx');

  final int baud;
  final int latencyMs;
  NativeOp2Transport({this.baud = 460800, this.latencyMs = 2});

  /// Дескриптор подключённого устройства (found/vid/pid/product).
  Future<Map<String, Object?>> descriptor() async {
    final d = await _tx.invokeMethod<Map<dynamic, dynamic>>('op2/descriptor');
    return d?.cast<String, Object?>() ?? const {'found': false};
  }

  /// Открыть FTDI-канал: permission (системный диалог при первом OTG),
  /// claim interface 0, FTDI init (reset/baud/8N1/latency/DTR/RTS).
  Future<Map<String, Object?>> open() async {
    final r = await _tx.invokeMethod<Map<dynamic, dynamic>>(
      'op2/open', {'baud': baud, 'latencyMs': latencyMs});
    return r?.cast<String, Object?>() ?? const {'granted': false};
  }

  Stream<Uint8List> get bytes => _rx
      .receiveBroadcastStream()
      .where((e) => e is Uint8List)
      .cast<Uint8List>();

  Future<int> write(Uint8List frame) async =>
      (await _tx.invokeMethod<int>('op2/write', frame)) ?? -1;

  Future<void> close() => _tx.invokeMethod<void>('op2/close');
}
'''

OP2_TEST_DART = r'''// v0.15.0: чистые тесты OP2 без платформенного канала.
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2_fixed/native_op2.dart';

void main() {
  group('OP2 ID table', () {
    test('оригинал Tactrix', () {
      expect(looksLikeOp2(0x0403, 0xCC4C), isTrue);
    });
    test('клон и FTDI default', () {
      expect(looksLikeOp2(0x0403, 0xCC48), isTrue);
      expect(looksLikeOp2(0x0403, 0x6001), isTrue);
    });
    test('чужой VID/PID отбрасывается', () {
      expect(looksLikeOp2(0x10C4, 0xEA60), isFalse); // CP210x
      expect(looksLikeOp2(0x0403, 0x9999), isFalse);
    });
  });

  group('helpers', () {
    test('hex16', () => expect(hex16(0xCC4C), '0xCC4C'));
    test('describeOp2 сохраняет vid/pid/product', () {
      final m = describeOp2(const {
        'found': true, 'vid': '0x0403', 'pid': '0xCC4C', 'product': 'OpenPort 2.0',
      });
      expect(m['pid'], '0xCC4C');
      expect(m['product'], contains('OpenPort'));
    });
  });
}
'''

OP2_NOTES_MD = '''# OP2 OTG — стендовый чеклист (HARDWARE-гейт)

Модуль 04.8 даёт USB-провод до OpenPort 2.0. Маппинг SSM-кадров (A8 00 …)
на J2534 PassThru-вызывы подтверждается на железе ДО постановки
HARDWARE_CONFIRMED=True:

1. [ ] OTG: descriptor() видит vid=0x0403 pid=0xCC4C, permission ≤ 2 диалога
2. [ ] Снять лог J2534-вызовов EcuFlash (op20pt32 logging) на своей машине
3. [ ] Сверить PassThruConnect(ISO15765, 500000) + фильтр 0x7E8 с elm.dart init
4. [ ] A8-опрос базового набора: качество >= 95% / 10 мин больше, чем по BT
5. [ ] K-line канал (ISO9141 fast-init) для pre-CAN блока — отдельным билдом
6. [ ] Клоны: зафиксировать VID/PID/строку product в report перед выкладкой

Сборка OP2-апк после ячейки 05 вручную:
  /content/flutter/bin/flutter build apk --release \
      --dart-define=SSM_TRANSPORT=native_op2
'''

def write(rel, body, executable=False):
    p = APP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copyfile(p, BACKUP / (rel.replace("/", "__") + ".bak"))
    p.write_text(body, encoding="utf-8")
    if executable:
        p.chmod(p.stat().st_mode | 0o111)
    print("[+]", rel)

# ============================================================
# 4. Патч AndroidManifest.xml (idempotent, anchor-based)
# ============================================================
def patch_manifest():
    text = MANIFEST.read_text(encoding="utf-8")
    if "OP2_DEVICE_ATTACHED_OK" in text or "USB_DEVICE_ATTACHED" in text:
        print("[SKIP already applied] AndroidManifest.xml"); return
    shutil.copyfile(MANIFEST, BACKUP / "AndroidManifest.xml.bak")
    if "android.hardware.usb.host" not in text:
        # required=false: BT-only сборка остаётся видна устройствам без OTG
        text = text.replace(
            "<application",
            '<uses-feature android:name="android.hardware.usb.host" '
            'android:required="false" />\n\n    <application', 1)
    hook = (
        '\n            <intent-filter>\n'
        '                <action android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED" />\n'
        '            </intent-filter>\n'
        '            <meta-data\n'
        '                android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED"\n'
        '                android:resource="@xml/op2_device_filter" />\n'
        '            <!-- OP2_DEVICE_ATTACHED_OK -->')
    m = re.search(r'(<activity[^>]*android:name="\.MainActivity"[^>]*>)', text)
    if not m:
        raise RuntimeError("Не нашёл activity .MainActivity в манифесте")
    text = text[:m.end(1)] + hook + text[m.end(1):]
    MANIFEST.write_text(text, encoding="utf-8")
    print("[OK] patched AndroidManifest.xml (usb.host + attach)")

# ============================================================
# 5. Патч MainActivity.kt — регистрация Op2Channel
# ============================================================
def patch_main_activity():
    text = MAIN_ACTIVITY.read_text(encoding="utf-8")
    if "Op2Channel.register" in text:
        print("[SKIP already applied] MainActivity.kt"); return
    shutil.copyfile(MAIN_ACTIVITY, BACKUP / "MainActivity.kt.bak")
    anchor = re.search(r'super\.configureFlutterEngine\(flutterEngine\)', text)
    if not anchor:
        raise RuntimeError(
            "Якорь configureFlutterEngine не найден — пришлите MainActivity.kt, "
            "патч руками: Op2Channel.register(this, flutterEngine)")
    text = text[:anchor.end()] + "\n        Op2Channel.register(this, flutterEngine)" + text[anchor.end():]
    MAIN_ACTIVITY.write_text(text, encoding="utf-8")
    print("[OK] patched MainActivity.kt (register Op2Channel)")

# ============================================================
# 6. Патч lib/transport_selected.dart — ветка по --dart-define
# ============================================================
def patch_transport_selected():
    if not PATCH_TRANSPORT:
        print("[SKIP] PATCH_TRANSPORT=False"); return
    text = TRANSPORT_SEL.read_text(encoding="utf-8")
    if "native_op2" in text or "SSM_TRANSPORT" in text:
        print("[SKIP already applied] transport_selected.dart"); return
    shutil.copyfile(TRANSPORT_SEL, BACKUP / "lib__transport_selected.dart.bak")

    # 6.1 импорт после последнего import-а
    imports = list(re.finditer(r'(?m)^import[^;]+;$', text))
    if not imports:
        text = "import 'native_op2.dart';\n" + text
    else:
        last = imports[-1]
        text = text[:last.end()] + "\nimport 'native_op2.dart';" + text[last.end():]

    # 6.2 константа выбора (по умолчанию BT — сборка 0.14.1 не меняется)
    const_src = ("\n/// v0.15.0: 'native_spp' (по умолчанию) | 'native_op2' (USB OTG).\n"
                 "const String kTransportName =\n"
                 "    String.fromEnvironment('SSM_TRANSPORT', defaultValue: 'native_spp');\n")
    first_decl = re.search(r'(?m)^(class|final|const|[A-Za-z_]+\s+createTransport)', text)
    if first_decl:
        text = text[:first_decl.start()] + const_src.strip("\n") + "\n\n" + text[first_decl.start():]
    else:
        text += const_src

    # 6.3 оборачиваем фабрику createTransport (=> и { } формы)
    m = re.search(r'createTransport\s*\([^)]*\)\s*=>\s*', text)
    if m:
        # => form: оборачиваем исходное выражение в else-ветку
        end = text.index(';', m.end())
        original = text[m.end():end]
        text = (text[:m.start()] + "createTransport() {\n"
                "  if (kTransportName == 'native_op2') return NativeOp2Transport();\n"
                "  return " + original + ";\n}" + text[end + 1:])
        print("[OK] patched createTransport (=> form)")
    else:
        m = re.search(r'createTransport\s*\([^)]*\)\s*\{', text)
        if not m:
            raise RuntimeError(
                "Фабрика createTransport не найдена в transport_selected.dart — "
                "добавьте ветку вручную: if (kTransportName == 'native_op2') "
                "return NativeOp2Transport();")
        text = text[:m.end()] + ("\n  if (kTransportName == 'native_op2') "
                                  "return NativeOp2Transport();") + text[m.end():]
        print("[OK] patched createTransport ({ } form)")
    TRANSPORT_SEL.write_text(text, encoding="utf-8")

# ============================================================
# 7. Записываем файлы модуля
# ============================================================
print("SSM2 0.14.1 >> 0.15.0 | OP2 OTG module")
write("android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt", OP2_CHANNEL_KT)
write("android/app/src/main/res/xml/op2_device_filter.xml", DEVICE_FILTER_XML)
write("lib/native_op2.dart", NATIVE_OP2_DART)
write("test/op2_transport_test.dart", OP2_TEST_DART)
write("tool/op2_j2534_notes.md", OP2_NOTES_MD)

patch_manifest()
patch_main_activity()
patch_transport_selected()

# ============================================================
# 8. Self-check
# ============================================================
checks = [
    (MAIN_ACTIVITY, "Op2Channel.register", "MainActivity не пропатчен"),
    (MANIFEST, "USB_DEVICE_ATTACHED", "манифест без attach-фильтра"),
    (MANIFEST, "android.hardware.usb.host", "манифест без uses-feature"),
    (TRANSPORT_SEL, "SSM_TRANSPORT", "transport_selected без ветки"),
    (TRANSPORT_SEL, "native_op2.dart", "transport_selected без импорта"),
    (APP / "lib/native_op2.dart", "class NativeOp2Transport", "dart-обёртка пуста"),
    (APP / "android/app/src/main/kotlin/com/subaru/ssm2_fixed/Op2Channel.kt",
     "class FtdiPipe", "Op2Channel.kt пуст"),
]
for path, needle, msg in checks:
    if needle not in path.read_text(encoding="utf-8"):
        raise RuntimeError(f"SELF-CHECK FAIL: {msg}")
print("[OK] self-check passed (" + str(len(checks)) + " проверок)")

# ============================================================
# 9. Дымовая проверка Dart-стороны (строгий гейт остаётся в 05)
# ============================================================
import json
cfg = json.loads((APP / "build_config.json").read_text(encoding="utf-8"))
FLUTTER_BIN = Path(cfg["flutter"]) / "bin/flutter"
DART_BIN = Path(cfg["flutter"]) / "bin/dart"
env_path = None
try:
    subprocess.run([str(DART_BIN), "format", "lib/native_op2.dart",
                    "lib/transport_selected.dart", "test/op2_transport_test.dart"],
                   cwd=APP, capture_output=True, text=True, timeout=300)
    print("[OK] dart format")
    smoke = subprocess.run([str(FLUTTER_BIN), "analyze", "--no-pub",
                            "--no-fatal-infos", "--no-fatal-warnings"],
                           cwd=APP, capture_output=True, text=True, timeout=600)
    tail = (smoke.stdout + smoke.stderr).strip().splitlines()[-3:]
    print("[SMOKE analyze]", " | ".join(tail))
except Exception as e:
    print("[SMOKE skipped]", e, "— строгая проверка будет в ячейке 05")

print()
print("=== SSM2 0.15.0 | OP2 OTG установлен. Дальше — ячейка 05 ===")
print("  * APK из ячейки 05 остаётся BLUETOOTH-сборкой (default native_spp)")
print("  * OP2-сборка вручную: flutter build apk --release \\")
print("      --dart-define=SSM_TRANSPORT=native_op2")
print("  * Перед HARDWARE_CONFIRMED пройдите tool/op2_j2534_notes.md")
print(f"  * Бэкапы затронутых файлов: {BACKUP}")
