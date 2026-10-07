# coding: utf-8
# @title 04.8 | OpenPort 2.0 OTG REAL | SSM2 0.15.0 (REPLACE старую 04.8)
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
