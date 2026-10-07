# @title 04.1 | FIX | SSM2 0.9 >> 0.10.2 — все исправления (вставить МЕЖДУ ячейками 04 и 05)
# Все 25 дефектов аудита + LOG-04 + одометр/расход + автозонд endian для 4-byte PID.
# 11 файлов. Идемпотентна, бэкап, самопроверка. После — ячейка 05 (26 тестов).
# Проверено: Flutter 3.35.4 / Dart 3.9.2 — analyze «No issues found», 26/26.

import json
from pathlib import Path
import re
import shutil
import time

APP = Path("/content/subaru_ssm2_fixed")

FILES = {}

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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class MainActivity : FlutterActivity() {
    private val sppUuid: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

    // v0.10 FIX (дедлок): команды исполняются на commandIo, а блокирующее чтение —
    // на ОТДЕЛЬНОМ потоке. В 0.9 read() занимал единственный поток executor'а,
    // и ни одна запись после успешного connect больше не выполнялась.
    private val commandIo = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    private var socket: BluetoothSocket? = null
    @Volatile private var reading = false
    @Volatile private var readerThread: Thread? = null
    private var dataSink: EventChannel.EventSink? = null
    private var statusSink: EventChannel.EventSink? = null

    private fun btAdapter(): BluetoothAdapter? =
        (getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    // v0.10 FIX: адаптер выключен/запрещён — честная ошибка вместо тихо пустого списка.
    private fun requireAdapter(): BluetoothAdapter {
        val adapter = btAdapter() ?: throw IOException("Bluetooth недоступен на устройстве")
        if (!adapter.isEnabled) throw IOException("Bluetooth выключен: включите его в настройках")
        return adapter
    }

    private fun pairedDevices(): List<Map<String, String>> {
        val adapter = requireAdapter()
        return try {
            adapter.bondedDevices?.map { device ->
                mapOf("name" to (device.name ?: ""), "address" to device.address)
            } ?: emptyList()
        } catch (e: SecurityException) {
            throw IOException("Нет разрешения BLUETOOTH_CONNECT", e)
        }
    }

    private fun socketCandidates(address: String): List<BluetoothSocket> {
        val adapter = requireAdapter()
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

    // v0.10 FIX: BluetoothSocket.connect() не имеет таймаута. Оборачиваем в поток
    // с latch: по истечении budget'а сокет закрывается, connect разблокируется.
    private fun connectWithTimeout(candidate: BluetoothSocket, timeoutMs: Long): Boolean {
        val latch = CountDownLatch(1)
        var failure: Exception? = null
        val worker = Thread {
            try {
                candidate.connect()
            } catch (e: Exception) {
                failure = e
            } finally {
                latch.countDown()
            }
        }
        worker.isDaemon = true
        worker.start()
        val finished = latch.await(timeoutMs, TimeUnit.MILLISECONDS)
        if (!finished) {
            try { candidate.close() } catch (_: Exception) {}
            return false
        }
        failure?.let { throw IOException(it.message ?: "RFCOMM connect failed") }
        return true
    }

    private fun openSocket(address: String): BluetoothSocket {
        var lastError: Exception? = null
        for (candidate in socketCandidates(address)) {
            try {
                Thread.sleep(120)
                // 12 секунд на кандидата вместо блокировки без предела (Dart ждёт 25 с на всё).
                if (connectWithTimeout(candidate, 12000)) return candidate
                lastError = IOException("timeout 12s")
            } catch (e: Exception) {
                lastError = e
                try { candidate.close() } catch (_: Exception) {}
            }
        }
        throw IOException(lastError?.message ?: "RFCOMM connect failed")
    }

    // v0.10 FIX: читатель живёт на своём потоке — командный executor остаётся свободным.
    private fun startReader(active: BluetoothSocket) {
        reading = true
        val thread = Thread {
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
                // Разрыв линии или intentional close: уходим в status=false только при обрыве.
            }
            val wasReading = reading
            reading = false
            closeSocket()
            if (wasReading) main.post { statusSink?.success(false) }
        }
        thread.isDaemon = true
        thread.name = "spp-reader"
        readerThread = thread
        thread.start()
    }

    private fun closeSocket() {
        val current = socket
        socket = null
        if (current != null) {
            try { current.close() } catch (_: Exception) {}
        }
    }

    private fun stopReaderAndSocket() {
        reading = false
        closeSocket() // закрытие сокета разблокирует read() потока читателя
        try { readerThread?.join(400) } catch (_: Exception) {}
        readerThread = null
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
                "paired" -> commandIo.execute {
                    try {
                        val devices = pairedDevices()
                        main.post { result.success(devices) }
                    } catch (e: Exception) {
                        main.post { result.error("BT", e.message, null) }
                    }
                }
                "connect" -> {
                    val address = call.argument<String>("address")
                    if (address.isNullOrBlank()) {
                        result.error("ARG", "address required", null)
                    } else {
                        commandIo.execute {
                            try {
                                stopReaderAndSocket()
                                val opened = openSocket(address)
                                socket = opened
                                startReader(opened)
                                main.post {
                                    statusSink?.success(true)
                                    result.success(true)
                                }
                            } catch (e: Exception) {
                                stopReaderAndSocket()
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
                        // Свободный командный поток: запись больше не встаёт за читателем.
                        commandIo.execute {
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
                "disconnect" -> commandIo.execute {
                    stopReaderAndSocket()
                    main.post { result.success(null) }
                }
                else -> result.notImplemented()
            }
        }
    }

    override fun onDestroy() {
        stopReaderAndSocket()
        dataSink = null
        statusSink = null
        commandIo.shutdownNow() // v0.10 FIX: раньше потоки executor'а жили до смерти процесса
        super.onDestroy()
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
    _data = transport.data.listen(
      _onData,
      onError: (Object e) => _lost('RX ERROR: $e'),
    );
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

  // v0.10 FIX (ELM-01): транзиентный таймаут клона не роняет sync сразу —
  // деградация только после двух таймаутов подряд. Раньше первый же TIMEOUT
  // снимал sync, и ветка retry становилась мёртвым кодом навсегда.
  int _consecutiveTimeouts = 0;
  // v0.10 FIX (ELM-02): успешный CAN-профиль кэшируем — реконнект идёт сразу в него.
  List<String>? _cachedProfileCommands;
  String _cachedProfileName = '';
  // v0.10 FIX (ELM-04): хвост буфера после '>' отдаём следующему обмену.
  String _rxTail = '';

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
      if (!tail.contains('>')) _rxTail = tail; // v0.10
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
    if (command.contains('\r') ||
        command.contains('\n') ||
        command.trim().isEmpty) {
      throw ArgumentError('One nonempty command is required');
    }

    final p = Completer<String>();
    _pending = p;
    _rx = _rxTail; // v0.10: возможный хвост склейки с предыдущим ответом
    _rxTail = '';
    final watch = Stopwatch()..start();
    tx++;
    log('TX ${visibleText('$command\r')}');
    try {
      final values = await Future.wait<Object>(
        [
          transport.write('$command\r').then<Object>((_) {
            writeAccepted++;
            log('WRITE_OK');
            return true;
          }),
          p.future,
        ],
        eagerError: true,
      ).timeout(Duration(milliseconds: timeout ?? timeoutMs));
      final raw = values[1] as String;
      if (preserveSync ? !transport.connected : !ready) {
        throw ReplyError('LINK_LOST');
      }
      log('PROMPT ${watch.elapsedMilliseconds}ms');
      _consecutiveTimeouts = 0; // v0.10
      return raw;
    } on TimeoutException {
      timeouts++;
      _consecutiveTimeouts++; // v0.10
      lastError = 'TIMEOUT $command';
      // v0.10: sync вниз только после второго таймаута подряд,
      // иначе retry в _exchangeForRead обречён заранее.
      if (!preserveSync && _consecutiveTimeouts >= 2) {
        _setSync(false, 'TIMEOUT x$_consecutiveTimeouts');
      }
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
      final raw = await _exchange(
        command,
        timeout: timeoutMs + 800,
        preserveSync: true,
      );
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

    // v0.10: кэшированный профиль первым — реконнект за ~1 пробу вместо полного перебора.
    final cached = _cachedProfileCommands;
    final ordered = <_InitProfile>[
      if (cached != null) _InitProfile('$_cachedProfileName (кэш)', cached),
      ...profiles,
    ];

    for (final profile in ordered) {
      log('PROFILE TRY ${profile.name}');
      await _runInitCommands(profile.commands);
      for (final address in probeAddresses) {
        final value = await _probeAddress(address);
        if (value != null) {
          activeProfile = profile.name;
          _cachedProfileCommands = profile.commands; // v0.10
          _cachedProfileName = profile.name; // v0.10
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
        identity = (await _exchange(
          'ATI',
          preserveSync: true,
        )).replaceAll(RegExp(r'[\r\n>]'), ' ').trim();
      } catch (_) {
        identity = 'ELM327 / BtSsm';
      }

      try {
        voltage = (await _exchange(
          'ATRV',
          preserveSync: true,
        )).replaceAll(RegExp(r'[\r\n>]'), ' ').trim();
      } catch (_) {
        voltage = '13.8V';
      }

      final canOk = await _probeProfiles();
      if (!canOk) {
        throw ReplyError(
          'SPP открыт, но ECU не ответил на SSM2-over-CAN A8 запросы. '
          'Проверьте зажигание, CAN-адаптер и попробуйте 11/29-bit профиль. '
          'v0.10: автомобили с K-line диагностикой (примерно до 2008, '
          'ISO-9141/14230) эта сборка не поддерживает — там нужен другой '
          'транспорт, это не неисправность адаптера.',
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
    if (length < 1 ||
        length > 32 ||
        address < 0 ||
        address + length - 1 > 0xFFFFFF) {
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

  Future<PidRead> readPid(SubaruPidDef pid) =>
      readRange(pid.address, pid.bytesCount);

  Future<String> diagnostic(String command) {
    final normalized = command.trim().toUpperCase();
    if (!allowedDiagnostic(normalized)) {
      throw ArgumentError('Разрешены только безопасные AT-команды и A8-чтение');
    }
    return _lock.run(
      () => _exchange(normalized, preserveSync: normalized.startsWith('AT')),
    );
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
  bool _endianProbed = false;
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

  double tripDistanceKm = 0;
  double tripFuelLitres = 0;
  DateTime? _tripLastTick;
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
  bool _reconnecting = false,
      _resumeAfterReconnect = false,
      _linkWatchStarted = false;
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
    return sample == null
        ? null
        : DateTime.now().difference(sample.time).inMilliseconds;
  }

  bool stale(String id) => (ageMs(id) ?? 999999) > 3000;

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  void resetTrip() {
    tripDistanceKm = 0;
    tripFuelLitres = 0;
    _tripLastTick = null;
    _notify();
  }

  void _tickTrip(PidSample sample) {
    final now = sample.time;
    final prev = _tripLastTick;
    _tripLastTick = now;
    if (prev == null) return;
    final dt = now.difference(prev).inMilliseconds / 1000.0;
    if (dt <= 0 || dt > 5) return;
    final speed = latest['SPEED']?.value;
    if (speed != null && speed > 0.5) tripDistanceKm += speed / 3600.0 * dt;
    final maf = latest['MAF']?.value;
    if (maf != null && maf > 0) {
      final afr = latest['AFR']?.value;
      final ratio = (afr != null && afr >= 8 && afr <= 25) ? afr : 14.7;
      tripFuelLitres += maf / ratio / 745.0 * dt;
    }
  }

  void _publish(PidSample sample) {
    attempts[sample.pid.id] = sample;
    if (sample.good) {
      latest[sample.pid.id] = sample;
      final list = history.putIfAbsent(sample.pid.id, () => []);
      list.add(sample);
      if (list.length > 1200)
        list.removeAt(0); // v0.10: окно анализа 300→1200 точек
      goodCount++;
      _replyTimes.add(sample.time);
      if (_replyTimes.length > 300) _replyTimes.removeAt(0);
    } else {
      failedCount++;
    }
    _maybeAlert(sample);
    if (!_disposed) _events.add(sample);
    _tickTrip(sample);
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
          final base = p.priority == 1
              ? 1
              : p.priority == 2
                  ? 2
                  : 5;
          if (hot) {
            if (hotSet.contains(p.id)) return true;
            return round % (base * 2) == 0;
          }
          return round % base == 0;
        }).toList();

        final nowMs = DateTime.now().millisecondsSinceEpoch;
        // v0.10 FIX (ENGINE-01): протухшие mute-записи удаляем — раньше метрика
        // «N PID изолировано» росла вечно и чеклист «0 изоляций» был недостижим.
        _addrMutedUntilMs.removeWhere((address, until) => nowMs >= until);
        for (final pid in due) {
          if (generation != _generation || !running || !elm.ready) return;
          if (pid.addresses.any((a) => nowMs < (_addrMutedUntilMs[a] ?? 0)))
            continue;
          requests++;
          _requestTimes.add(DateTime.now());
          if (_requestTimes.length > 200) _requestTimes.removeAt(0);

          try {
            final read = await elm.readPid(pid);
            if (generation != _generation || _disposed) return;
            final value = pid.decode(read.bytes, endian: endian);
            if (!_endianProbed &&
                pid.floatFactor != null &&
                pid.bytesCount == 4 &&
                pid.id == 'IAM') {
              _endianProbed = true;
              final alt = endian == Endian.big ? Endian.little : Endian.big;
              final altVal = pid.decode(read.bytes, endian: alt);
              if ((value == null || value == 0) &&
                  altVal != null &&
                  altVal > 0 &&
                  altVal <= 1.5) {
                endian = alt;
              }
            }
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
      enabled.removeWhere(
          (id) => SubaruPidLibrary.all.any((p) => p.id == id && p.extended));
    }
    romId = rom.trim();
    endian = byteOrder;
    latest.clear();
    attempts.clear();
    history.clear();
    _replyTimes.clear();
    _addrFails.clear();
    _addrMutedUntilMs.clear();
    _endianProbed = false;
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

FILES["lib/analyzer.dart"] = r'''
import 'dart:math' as math;

import 'samples.dart';

enum FindingLevel { info, warning, critical }

class Finding {
  const Finding(
    this.level,
    this.title,
    this.detail,
    this.evidence,
    this.advice,
  );
  final FindingLevel level;
  final String title, detail, evidence, advice;
}

class LogAnalysis {
  const LogAnalysis(
    this.findings,
    this.sampleCount,
    this.spanSeconds,
    this.channels,
  );
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
  double get mean => samples.isEmpty
      ? 0
      : samples.fold<double>(0, (a, s) => a + s.v) / samples.length;
  double get min => samples.isEmpty
      ? 0
      : samples.fold<double>(double.infinity, (a, s) => math.min(a, s.v));
  double get max => samples.isEmpty
      ? 0
      : samples.fold<double>(
          double.negativeInfinity,
          (a, s) => math.max(a, s.v),
        );
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

  // v0.10 FIX (ANLZ-02): шкалы нагрузки различаем явно — 4-байт (г/об) и 1-байт (%).
  final loadGb = _series(history, const ['LOAD_4B']);
  final loadPct = _series(history, const ['LOAD']);

  // --- Детонация: FBKC -------------------------------------------------------------
  final fbkc = series['fbkc']!;
  if (!fbkc.empty) {
    final events = fbkc.samples.where((s) => s.v <= kFbkcEventDeg).toList();
    // v0.10: г/об доказываем порогом 1.5, проценты — порогом 70%. Раньше LOAD %
    // (0..100) делал hardLoad истинным всегда — критикал по любому событию FBKC.
    final hardLoad = !loadGb.empty
        ? loadGb.max >= 1.5 && series['rpm']!.max >= 3000
        : loadPct.max >= 70 && series['rpm']!.max >= 3000;
    if (events.isNotEmpty || fbkc.min <= 0) {
      if (events.length >= 5 ||
          fbkc.min <= kFbkcBadDeg ||
          (events.isNotEmpty && hardLoad)) {
        findings.add(
          Finding(
            FindingLevel.critical,
            'Детонация: ECU агрессивно снижает УОЗ',
            'Зафиксировано ${events.length} событий FBKC <= ${_f(kFbkcEventDeg)}°, '
                'минимум ${_f(fbkc.min)}°${hardLoad ? ' на нагрузке (${!loadGb.empty ? 'load >= 1.5 г/об' : 'load >= 70%'}, rpm >= 3000)' : ''}. '
                'Залейте топливо с октаном выше, затем снимите 1-2° в зоне кластера через Map Lab.',
            'fbkc: n=${fbkc.count} events=${events.length} min=${_f(fbkc.min)}° · '
                'rpm_max=${_f(series['rpm']!.max)} load_max=${_f2(series['load']!.max)}',
            'Map Lab -> timing/knockadv: кластерная коррекция -0.5..-1.0° с веером сглаживания.',
          ),
        );
      } else if (events.isNotEmpty || fbkc.min <= -0.5) {
        findings.add(
          Finding(
            FindingLevel.warning,
            'Единичные рывки FBKC',
            'Есть ${events.length} событий FBKC <= ${_f(kFbkcEventDeg)}°, минимум ${_f(fbkc.min)}°. '
                'Пока это штатная реакция, но повторяемость в одной зоне оборотов — повод для точечной коррекции.',
            'events=${events.length} min=${_f(fbkc.min)}°',
            'Повторный лог в той же нагрузке; если кластер — минус 0.5° в Map Lab.',
          ),
        );
      }
    }
  }

  // --- Детонация: FLKC (обученная коррекция) ---------------------------------------
  final flkc = series['flkc']!;
  if (!flkc.empty && flkc.min < 0) {
    final level = flkc.min <= kFlkcBadDeg
        ? FindingLevel.critical
        : FindingLevel.warning;
    if (flkc.min <= kFlkcWarnDeg) {
      findings.add(
        Finding(
          level,
          'Обученная коррекция зажигания удерживается',
          'FLKC достигала ${_f(flkc.min)}° и не возвращается к нулю: ECU запомнил детонацию '
              'в весовых зонах. Топливо/наддув/свечи — сначала причина, потом карта.',
          'flkc: min=${_f(flkc.min)}° mean=${_f(flkc.mean)}° n=${flkc.count}',
          'Сброс обучения и повторный лог только после устранения причины.',
        ),
      );
    }
  }

  // --- IAM (обучение грубых/тонких таблиц) -------------------------------------------
  // v0.10 FIX (ANLZ-01): 4-байтный IAM — float-множитель 0..1, а старые пороги
  // (kIamPoor=13, kIamFull=15.5) — в шагах 0..16. Автодетект шкалы и приведение
  // к шагам; иначе любой живой лог получал фейковый критикал «IAM критически низкий».
  var iam = series['iam']!;
  final iamScaled = !iam.empty && iam.max <= 1.2;
  if (iamScaled) {
    iam = _Series([for (final s in iam.samples) (v: s.v * 16.0, t: s.t)]);
  }
  if (!iam.empty) {
    final maxIam = iam.max;
    if (maxIam < kIamPoor) {
      findings.add(
        Finding(
          FindingLevel.critical,
          'IAM критически низкий',
          'Динамический шаг зажигания ограничен: IAM max ${_f(maxIam)} из 16. '
              'Двигатель работает по грубой (conservative) карте — мощность ниже, риск выше.',
          'iam: max=${_f(maxIam)} mean=${_f(iam.mean)} n=${iam.count}${iamScaled ? ' (0..1 → ×16)' : ''}',
          'Не снимать ограничение, пока FBKC/FLKC не чисты: это защита мотора.',
        ),
      );
    } else if (maxIam < kIamFull) {
      findings.add(
        Finding(
          FindingLevel.warning,
          'IAM не добрался до полного',
          'IAM max ${_f(maxIam)} из 16: обучение не завершено или ECU придерживает запас. '
              'Нормально после сброса адаптаций, иначе — слабый сигнал скрытого детона.',
          'iam: max=${_f(maxIam)} mean=${_f(iam.mean)} n=${iam.count}${iamScaled ? ' (0..1 → ×16)' : ''}',
          'Лог 15-20 мин спокойной езды после прогрева: IAM должен дорасти до 16.',
        ),
      );
    } else {
      findings.add(
        Finding(
          FindingLevel.info,
          'IAM полный',
          'Обучение зажигания завершено: IAM достигал ${_f(maxIam)} / 16.',
          'iam: max=${_f(maxIam)} mean=${_f(iam.mean)}',
          'Без действий.',
        ),
      );
    }
  }

  // --- Топливные коррекции -----------------------------------------------------------
  final stft = series['stft']!;
  final ltft = series['ltft']!;
  if (!stft.empty && !ltft.empty) {
    final combined = stft.mean + ltft.mean;
    final absSum = combined.abs();
    if (absSum >= kTrimBadPct) {
      findings.add(
        Finding(
          FindingLevel.critical,
          'Топливная коррекция за пределами',
          'STFT+LTFT ${combined >= 0 ? '+' : ''}${_f(combined)}%: смесь далека от цели. '
              'Типичные причины: подсос воздуха, MAF, датчик O2, давление топлива.',
          'stft_mean=${_f(stft.mean)}% ltft_mean=${_f(ltft.mean)}% sum=${_f(combined)}%',
          'Сначала диагностика железа. Картами VE это не лечится.',
        ),
      );
    } else if (absSum >= kTrimWarnPct) {
      findings.add(
        Finding(
          FindingLevel.warning,
          'Топливная коррекция повышена',
          'STFT+LTFT ${combined >= 0 ? '+' : ''}${_f(combined)}%: умеренное смещение смеси.',
          'stft_mean=${_f(stft.mean)}% ltft_mean=${_f(ltft.mean)}%',
          'Проверить подсос/фильтр и переснять лог с AFR.',
        ),
      );
    }
  }

  // --- Прогрев -----------------------------------------------------------------------
  final ect = series['ect']!;
  final rpm = series['rpm']!;
  if (!ect.empty && !rpm.empty && ect.min < kWarmEctC && rpm.max > kColdRpm) {
    findings.add(
      Finding(
        FindingLevel.warning,
        'Нагрузка до прогрева',
        'Минимум ECT ${_f(ect.min)}°C при пике ${_f(rpm.max)} об/мин: двигатель крутили холодным. '
            'Детонационный запас на холодную иной — выводы по FBKC из этой зоны не переносить в карту.',
        'ect_min=${_f(ect.min)}°C rpm_max=${_f(rpm.max)}',
        'Валидный тюнинг-лог: ECT > 80°C стабильно.',
      ),
    );
  }

  // --- Бортсеть ------------------------------------------------------------------------
  final batt = series['batt']!;
  if (!batt.empty && batt.min < kBattWarnV) {
    findings.add(
      Finding(
        FindingLevel.warning,
        'Просадка питания',
        'Минимум бортсети ${_f2(batt.min)}V: при <12V логируемые значения (особенно MAF/форсунки) '
            'уходят, а клоны ELM сыплют таймаутами.',
        'batt: min=${_f2(batt.min)}V mean=${_f2(batt.mean)}V n=${batt.count}',
        'Заряд/АКБ до повторного лога; адаптер снять с прикуривателя.',
      ),
    );
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
      findings.add(
        Finding(
          ratio >= 35 || worst >= 1.5
              ? FindingLevel.critical
              : FindingLevel.warning,
          'Смесь беднее цели',
          'В ${_f(ratio)}% сопоставленных точек AFR выше целевого на 0.7+ '
              '(максимум +${_f(worst)}). Беднение под наддувом — прямой путь к детонации '
              'и прогару; проверяйте топливоподачу до правок карт.',
          'pairs=${afrPairs.length} lean=${lean.length} (${_f(ratio)}%) worst=+${_f(worst)}',
          'Давление топлива, форсунки, MAF-скейлинг. Затем Map Lab -> fuel.',
        ),
      );
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
      findings.add(
        Finding(
          over >= 0.4 ? FindingLevel.critical : FindingLevel.warning,
          'Наддув не держит цель',
          'RMS ошибки ${_f2(rms)} бар, максимальный переброс +${_f2(over)} бар, '
              'просадка ${_f2(under)} бар. Переброс опаснее недобора: это незапланированная '
              'нагрузка на поршневую.',
          'pairs=${boostPairs.length} rms=${_f2(rms)} over=+${_f2(over)} under=${_f2(under)}',
          'Map Lab -> wgdc: снизить дьюти в зоне переброса, проверить вестгейт и шланги.',
        ),
      );
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
      findings.add(
        Finding(
          FindingLevel.info,
          'Выбросы канала ${entry.key}',
          'Канал ${entry.key}: $outliers из ${s.count} значений (${_f(ratio)}%) ушли дальше '
              '3.5σ от среднего. Частая причина — единичные сбои ELM-клона или mute '
              'проблемных адресов, а не физика двигателя.',
          'channel=${entry.key} outliers=$outliers/${s.count} mean=${_f2(mean)} std=${_f2(std)}',
          'Если выбросов > 5% — переподключите адаптер и снимите лог повторно.',
        ),
      );
    }
  }

  // --- Неравномерный холостой ход: признак пропусков/подсоса (v0.9.1) ---
  final idlePairs = _pair(
    series['rpm']!,
    series['tps']!,
  ).where((p) => p.b <= 3 && p.a >= 500 && p.a <= 1500).toList();
  if (idlePairs.length >= 10 && !ect.empty && ect.max >= 70) {
    final meanRpm =
        idlePairs.fold<double>(0, (a, p) => a + p.a) / idlePairs.length;
    var accR = 0.0;
    for (final p in idlePairs) {
      accR += (p.a - meanRpm) * (p.a - meanRpm);
    }
    final stdRpm = math.sqrt(accR / idlePairs.length);
    if (stdRpm >= 70) {
      findings.add(
        Finding(
          FindingLevel.warning,
          'Неравномерный холостой ход',
          'На тёплом двигателе при закрытом дросселе обороты гуляют σ=${_f(stdRpm)} об/мин '
              'вокруг ${_f(meanRpm)}. Похоже на пропуски воспламенения, подсос воздуха или '
              'грязный дроссель — чинить до тюнинга.',
          'idle_points=${idlePairs.length} rpm_mean=${_f(meanRpm)} rpm_std=${_f(stdRpm)}',
          'Свечи/катушки, проверка на подсос, LTFT на холостом.',
        ),
      );
    }
  }

  // --- Полнота данных -------------------------------------------------------------------
  if (channels < 8) {
    final failed = attempts.values.where((s) => !s.good).length;
    findings.add(
      Finding(
        FindingLevel.info,
        'Мало каналов для выводов',
        'Активно каналов: $channels. Для тюнинга базово нужны RPM/LOAD/TIMING/FBKC/FLKC/IAM/AFR/MAF. '
            '${failed > 0 ? 'Ошибок чтения за сессию: $failed.' : ''}',
        'channels=$channels failed_attempts=$failed',
        'Включите полный набор PID и/или расширенный набор (CALID-гейт).',
      ),
    );
  }

  findings.add(
    Finding(
      FindingLevel.info,
      'Сводка анализа',
      'Обработано $sampleCount значений по $channels каналам за ${_f(span)} с. '
          'Найдено проблем: ${findings.where((f) => f.level != FindingLevel.info).length}.',
      'samples=$sampleCount channels=$channels span=${_f(span)}s',
      findings.any((f) => f.level == FindingLevel.critical)
          ? 'Сначала критичные находки, потом Map Lab.'
          : 'Лог можно передавать в Map Lab для табличной коррекции.',
    ),
  );

  return LogAnalysis(
    List<Finding>.unmodifiable(findings),
    sampleCount,
    span,
    channels,
  );
}

// ======================= Сравнение логов «до/после» (v0.9.1) =======================

class ChannelDelta {
  const ChannelDelta(
    this.id,
    this.samplesBase,
    this.samplesAfter,
    this.meanBase,
    this.meanAfter,
    this.p95Base,
    this.p95After,
  );
  final String id;
  final int samplesBase, samplesAfter;
  final double meanBase, meanAfter, p95Base, p95After;
  double get meanDelta => meanAfter - meanBase;
}

class LogComparison {
  const LogComparison(
    this.channels,
    this.verdict,
    this.knockBase,
    this.knockAfter,
  );
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
    'RPM',
    'LOAD',
    'TIMING',
    'FBKC',
    'FKL',
    'IAM',
    'AFR',
    'STFT',
    'LTFT',
    'BOOST',
    'ECT',
  ],
}) {
  final deltas = <ChannelDelta>[];
  for (final id in channels) {
    final a = _series(baseline, [id]);
    final b = _series(after, [id]);
    if (a.count < 5 || b.count < 5) continue;
    deltas.add(
      ChannelDelta(id, a.count, b.count, a.mean, b.mean, a.p95, b.p95),
    );
  }
  deltas.sort((x, y) => y.meanDelta.abs().compareTo(x.meanDelta.abs()));
  final knockBase = _knockEvents(baseline);
  final knockAfter = _knockEvents(after);
  final verdict = knockAfter < knockBase
      ? 'Стало лучше: событий детонации $knockBase → $knockAfter'
      : knockAfter > knockBase
      ? 'Стало хуже: событий детонации $knockBase → $knockAfter — откатить последнюю правку'
      : 'Детонация без изменений ($knockBase события): правка нейтральна';
  return LogComparison(
    List<ChannelDelta>.unmodifiable(deltas),
    verdict,
    knockBase,
    knockAfter,
  );
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

  final StreamController<Uint8List> _data =
      StreamController<Uint8List>.broadcast();
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
    _dataSub ??= _dataEvents.receiveBroadcastStream().listen((dynamic chunk) {
      if (chunk is Uint8List && chunk.isNotEmpty) _data.add(chunk);
    }, onError: (Object e) => _data.addError(e));
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
    // v0.10 FIX (NATIVE-03): Bluetooth выключен/запрещён — внятная ошибка
    // вместо тихо пустого списка (до этого пользователь шёл пересопрягать исправное).
    final List<dynamic>? devices;
    try {
      devices = await _methods.invokeMethod<List<dynamic>>('paired');
    } on PlatformException catch (e) {
      throw StateError(e.message ?? 'Bluetooth недоступен');
    }
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
        throw StateError(
          'SPP не подключился: $address (сопряжение/питание/канал RFCOMM)',
        );
      }
      _status.add(true);
    } on PlatformException catch (e) {
      _connected = false;
      throw StateError('SPP connect failed: ${e.message ?? e.code}');
    } on TimeoutException {
      // v0.10 FIX (NATIVE-02): Dart-таймаут 25 с мог обогнать нативный connect,
      // который всё ещё выполнялся, — состояние разъезжалось. Гасим нативно и
      // требуем осознанный повтор, чтобы не было «призрачного» сокета.
      _connected = false;
      unawaited(_methods.invokeMethod<void>('disconnect').catchError((_) {}));
      throw StateError(
        'SPP: таймаут подключения (25 с). Нажмите «Отключить» и повторите.',
      );
    }
  }

  @override
  Future<void> write(String ascii) async {
    if (!_connected) throw StateError('SPP disconnected');
    await _methods.invokeMethod<void>('write', <String, dynamic>{
      'data': ascii,
    });
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

String csvCell(Object? value) =>
    '"${(value?.toString() ?? '').replaceAll('"', '""')}"';

class CsvLogger {
  IOSink? _sink;
  Future<void> _writes = Future<void>.value();
  File? file;
  bool active = false;
  int count = 0, _pending = 0;
  // v0.10 FIX (LOG-03): потерянные строки считаем и показываем в UI,
  // а логгер продолжает писать — раньше он молча умирал навсегда.
  int dropped = 0;
  String error = '';

  // v0.10 FIX (LOG-02): широкий RomRaider-формат «одна строка = один момент».
  // Файл напрямую открывается Map Lab (LogData.parse), конвертер больше не нужен.
  // Сетка 200 мс, окно свежести значения ±400 мс — как в exportRrCsv, но потоково.
  List<SubaruPidDef> _cols = const <SubaruPidDef>[];
  final Map<String, double?> _lastValue = <String, double?>{};
  final Map<String, DateTime> _lastTime = <String, DateTime>{};
  DateTime? _t0;
  int _gridMs = 0;
  int _lastSeenMs = -1; // v0.10.1: хвост сетки досписываем при stop()
  static const int _stepMs = 200, _skewMs = 400;

  Future<void> start(List<SubaruPidDef> columns) async {
    await stop();
    if (columns.isEmpty) throw StateError('Нет активных PID для записи');
    _cols = List<SubaruPidDef>.unmodifiable(columns);
    final dir = await getApplicationDocumentsDirectory();
    final f = File(
      '${dir.path}/ssm2_${DateTime.now().millisecondsSinceEpoch}.csv',
    );
    file = f;
    final sink = f.openWrite();
    _sink = sink;
    unawaited(
      sink.done.catchError((Object e) {
        error = '$e';
        active = false;
      }),
    );
    sink.writeln(['Time', ..._cols.map(rrHeader)].map(csvCell).join(','));
    await sink.flush();
    count = 0;
    dropped = 0;
    error = '';
    _lastValue.clear();
    _lastTime.clear();
    _t0 = null;
    _gridMs = 0;
    active = true;
  }

  void add(PidSample sample) {
    final sink = _sink;
    if (!active || sink == null) return;
    if (_pending >= 500) {
      dropped++;
      error = 'CSV backlog limit (потеряно: $dropped)';
      return;
    }
    if (sample.good) {
      _lastValue[sample.pid.id] = sample.value;
      _lastTime[sample.pid.id] = sample.time;
    }
    _t0 ??= sample.time;
    _lastSeenMs = sample.time.difference(_t0!).inMilliseconds; // v0.10.1
    // Слот сетки закрывается, когда от свежего сэмпла до него стало больше ско.
    final closable = sample.time.difference(_t0!).inMilliseconds - _skewMs;
    while (_gridMs <= closable) {
      _writeRow(_gridMs);
      _gridMs += _stepMs;
    }
  }

  void _writeRow(int t) {
    final stamp = _t0!.add(Duration(milliseconds: t));
    final row = <String>[(t / 1000).toStringAsFixed(1)];
    var any = false;
    for (final p in _cols) {
      final v = _lastValue[p.id];
      final pt = _lastTime[p.id];
      if (v != null &&
          pt != null &&
          stamp.difference(pt).inMilliseconds.abs() <= _skewMs) {
        row.add(v.toStringAsFixed(p.digits));
        any = true;
      } else {
        row.add('');
      }
    }
    if (!any) return;
    // v0.10.1 FIX (LOG-04): в 0.10 счётчик был инвертирован (slots - written) —
    // при густой записи сидел на нуле: UI «0 строк», экспорт заблокирован навсегда.
    count++;
    _pending++;
    _writes = _writes
        .then((_) async {
          _sink?.writeln(row.map(csvCell).join(','));
          if (count % 10 == 0) await _sink?.flush();
        })
        .catchError((Object e) {
          error = '$e';
          active = false;
        })
        .whenComplete(() {
          _pending--;
        });
  }

  Future<void> stop() async {
    active = false;
    // v0.10.1: хвост до последнего сэмпла досписываем (свежесть ±400 мс фильтрует).
    while (_lastSeenMs >= 0 && _gridMs <= _lastSeenMs) {
      _writeRow(_gridMs);
      _gridMs += _stepMs;
    }
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
      final json =
          jsonDecode(await file.readAsString()) as Map<String, dynamic>;
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
      final savedAdapter = json['adapter'] as String?; // v0.10
      if (savedAdapter != null && savedAdapter.isNotEmpty)
        selected = savedAdapter;
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
        'adapter': selected, // v0.10: MAC адаптера переживает перезапуск
      }),
      flush: true,
    );
  }

  Future<void> refreshDevices() => perform(() async {
    devices = await elm.transport.paired();
    if (!devices.any((d) => d.address == selected)) {
      selected = devices.isEmpty ? null : devices.first.address;
    }
    message = devices.isEmpty
        ? 'Нет сопряженных устройств'
        : 'Выберите адаптер';
  });

  Future<void> connect() => perform(() async {
    final address = selected;
    if (address == null) throw StateError('Выберите устройство');
    await elm.disconnect();
    await engine.stop();
    await logger.stop();
    await engine.configure(
      {...engine.enabled},
      engine.extendedConfirmed,
      engine.romId,
      engine.endian,
    );

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

    message =
        'Подключено · ${elm.activeProfile} · ${elm.identity.isEmpty ? 'ELM327' : elm.identity}';
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
    if (pids.isEmpty || engine.history.isEmpty)
      throw StateError('Нет записанных данных');
    final dir = await getApplicationDocumentsDirectory();
    final file = File(
      '${dir.path}/ssm2_rr_${DateTime.now().millisecondsSinceEpoch}.csv',
    );
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
        if (pv != null &&
            pt != null &&
            stamp.difference(pt).inMilliseconds.abs() <= skewMs) {
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
      ShareParams(
        files: [XFile(file.path)],
        text: 'SSM2 RR-совместимый CSV (${file.path})',
      ),
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
    await engine.configure(
      next,
      engine.extendedConfirmed,
      engine.romId,
      engine.endian,
    );
    await saveSettings();
    message = 'Выбор сохранен. Нажмите Старт для опроса.';
  });

  Future<void> preset(bool all) => perform(() async {
    await engine.configure(
      all
          ? SubaruPidLibrary.all
                .where((p) => !p.extended)
                .map((p) => p.id)
                .toSet()
          : {...SubaruPidLibrary.defaults},
      engine.extendedConfirmed,
      engine.romId,
      engine.endian,
    );
    await saveSettings();
    message = 'Набор сохранен; опрос на паузе';
  });

  Future<void> configureExtended(bool confirm, String rom, Endian endian) =>
      perform(() async {
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
    final address = int.parse(
      start.trim().replaceFirst(RegExp(r'^0[xX]'), ''),
      radix: 16,
    );
    final length = int.parse(count);
    if (address >= 0xFF0000 && !engine.extendedConfirmed) {
      throw StateError('Сначала подтвердите ROM');
    }
    await engine.stop();
    scanner = '';
    final result = await elm.readRange(address, length);
    scanner = List<String>.generate(
      result.bytes.length,
      (i) =>
          '0x${hexAddress(address + i)}   ${hex2(result.bytes[i])}   ${result.bytes[i]}',
    ).join('\n');
    message =
        'Прочитано ${result.bytes.length} байт за ${result.elapsedMs} мс. Опрос на паузе.';
  });

  Future<void> toggleLog() => perform(() async {
    if (logger.active) {
      await logger.stop();
    } else {
      if (!engine.running || !foreground) {
        throw StateError('Сначала запустите опрос в открытом приложении');
      }
      await logger.start(engine.active); // v0.10: колонки = активные PID
      if (!foreground) await logger.stop();
    }
  });

  Future<void> exportCsv() => perform(() async {
    final wasActive = logger.active; // v0.10
    await logger.stop();
    final file = logger.file;
    if (file == null || logger.count == 0) throw StateError('Нет записей CSV');
    await SharePlus.instance.share(
      ShareParams(files: [XFile(file.path)], text: 'SSM2 PID log (RR-формат)'),
    );
    // v0.10: экспорт раньше молча убивал запись — теперь состояние явное.
    message = wasActive
        ? 'CSV отправлен (RR-формат для Map Lab). Запись остановлена — нажмите «Записать», чтобы продолжить.'
        : 'CSV отправлен (RR-формат для Map Lab)';
    changed();
  });

  Future<void> exportTrace() => perform(() async {
    final dir = await getApplicationDocumentsDirectory();
    final file = File(
      '${dir.path}/ssm2_trace_${DateTime.now().millisecondsSinceEpoch}.txt',
    );
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
      ShareParams(
        files: [XFile(file.path)],
        text: 'SSM2 TX/RX diagnostic trace',
      ),
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

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const SsmApp());
}

const cyan = Color(0xFF22D3EE);
const muted = Color(0xFF9AAAC0);

class SsmApp extends StatelessWidget {
  const SsmApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'SSM2 Telemetry 0.10.2',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          colorScheme: ColorScheme.fromSeed(
            seedColor: cyan,
            brightness: Brightness.dark,
          ),
          scaffoldBackgroundColor: const Color(0xFF080D18),
          useMaterial3: true,
        ),
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
    timer = Timer.periodic(const Duration(milliseconds: 500), (_) {
      if (mounted) setState(() {});
    });
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
    timer.cancel();
    WidgetsBinding.instance.removeObserver(this);
    unawaited(model.shutdown());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: changes,
        builder: (context, _) {
          final engine = model.engine;
          final lastTimes = engine.latest.values.map((s) => s.time).toList()
            ..sort();
          final live = model.elm.ready &&
              lastTimes.isNotEmpty &&
              DateTime.now().difference(lastTimes.last).inSeconds < 3;
          final pages = <Widget>[
            AdapterPage(model),
            DashboardPage(model),
            PidPage(model),
            LoggerPage(model),
            GraphPage(model),
            AnalyzerPage(model),
            DiagnosticPage(model),
            const MapLabTab(),
          ];
          return Scaffold(
            appBar: AppBar(
              title: const Text(
                'SSM2 TELEMETRY 0.10.2',
                style: TextStyle(fontSize: 17, letterSpacing: 2),
              ),
              actions: [
                Icon(
                  Icons.circle,
                  size: 10,
                  color: live ? Colors.greenAccent : muted,
                ),
                const SizedBox(width: 16),
              ],
            ),
            body: SafeArea(
              child: Column(
                children: [
                  if (model.busy) const LinearProgressIndicator(minHeight: 2),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
                    child: Align(
                      alignment: Alignment.centerLeft,
                      child: Text(
                        model.message,
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: muted, fontSize: 12),
                      ),
                    ),
                  ),
                  Expanded(child: pages[tab]),
                ],
              ),
            ),
            bottomNavigationBar: NavigationBar(
              selectedIndex: tab,
              labelBehavior:
                  NavigationDestinationLabelBehavior.onlyShowSelected,
              onDestinationSelected: (i) => setState(() => tab = i),
              destinations: const [
                NavigationDestination(
                  icon: Icon(Icons.bluetooth),
                  label: 'Адаптер',
                ),
                NavigationDestination(
                    icon: Icon(Icons.speed), label: 'Дашборд'),
                NavigationDestination(icon: Icon(Icons.tune), label: 'PID'),
                NavigationDestination(
                  icon: Icon(Icons.fiber_manual_record_outlined),
                  label: 'CSV',
                ),
                NavigationDestination(
                  icon: Icon(Icons.show_chart),
                  label: 'График',
                ),
                NavigationDestination(
                    icon: Icon(Icons.insights), label: 'Анализ'),
                NavigationDestination(
                    icon: Icon(Icons.terminal), label: 'Диагн.'),
                NavigationDestination(
                  icon: Icon(Icons.table_view),
                  label: 'Map Lab',
                ),
              ],
            ),
          );
        },
      );
}

Widget section(String title, List<Widget> children) => Padding(
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(fontSize: 15, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 12),
          ...children,
        ],
      ),
    );
Widget detail(String key, String value) => Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            flex: 2,
            child:
                Text(key, style: const TextStyle(color: muted, fontSize: 12)),
          ),
          const SizedBox(width: 10),
          Expanded(
            flex: 3,
            child: Text(value, style: const TextStyle(fontSize: 12)),
          ),
        ],
      ),
    );
String ageText(int? age) =>
    age == null ? 'нет данных' : '${(age / 1000).toStringAsFixed(1)} с';

class AdapterPage extends StatelessWidget {
  const AdapterPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) => ListView(
        children: [
          section('Bluetooth SPP', [
            Text(
              model.elm.transport.name,
              style: const TextStyle(color: cyan, fontSize: 12),
            ),
            const SizedBox(height: 10),
            const Text(
              'Только CAN 11 bit / 500 kbit. Зажигание включено, автомобиль стоит.',
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: model.busy ? null : model.refreshDevices,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Сопряженные'),
                ),
                TextButton(
                  onPressed: () => model.perform(() async {
                    await const MethodChannel(
                      'ssm2/system',
                    ).invokeMethod<void>('bluetoothSettings');
                  }),
                  child: const Text('Настройки Android'),
                ),
              ],
            ),
            if (model.devices.isEmpty)
              const Padding(
                padding: EdgeInsets.all(12),
                child: Text('Обновите список устройств'),
              ),
            for (final device in model.devices)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.bluetooth, color: cyan),
                title: Text(device.name.isEmpty ? 'Без имени' : device.name),
                subtitle: Text(device.address),
                trailing: model.selected == device.address
                    ? const Icon(Icons.check, color: cyan)
                    : null,
                onTap: model.busy
                    ? null
                    : () {
                        model.selected = device.address;
                        model.changed();
                      },
              ),
            Wrap(
              spacing: 8,
              children: [
                FilledButton(
                  onPressed: model.busy || model.selected == null
                      ? null
                      : model.connect,
                  child:
                      Text(model.elm.ready ? 'Переподключить' : 'Подключить'),
                ),
                OutlinedButton(
                  onPressed: model.busy ? null : model.disconnect,
                  child: const Text('Отключить'),
                ),
              ],
            ),
          ]),
          section('Состояние', [
            detail(
              'Синхронизация',
              model.elm.ready ? 'ДА ✓' : 'нет — нажмите Подключить',
            ),
            detail(
              'Адаптер',
              model.elm.identity.isEmpty ? 'не опрошен' : model.elm.identity,
            ),
            detail(
              'ATRV',
              model.elm.voltage.isEmpty ? 'не опрошен' : model.elm.voltage,
            ),
            detail(
              'Watchdog',
              '${model.engine.mutedAddresses} PID изолировано · запросов: ${model.engine.requests}',
            ),
            detail('SPP', model.elm.transport.connected ? 'открыт' : 'закрыт'),
            detail(
              'Таймаут CAN',
              '${model.elm.timeoutMs} мс',
            ), // v0.10: вместо дубля строки sync
          ]),
        ],
      );
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
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  'SAFE Single Frame / ${pids.length} PID\n${engine.pidReadsPerSecond.toStringAsFixed(1)} PID-обновл./с · ${engine.requestsPerSecond.toStringAsFixed(1)} запросов/с',
                  style: const TextStyle(fontSize: 12, color: muted),
                ),
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
        FuelCard(
          fuel: fuel,
          mafStale: engine.stale('MAF'),
          tripKm: engine.tripDistanceKm,
          tripL: engine.tripFuelLitres,
          onReset: engine.resetTrip,
        ),
        if (pids.isEmpty)
          const Expanded(
            child: Center(child: Text('Выберите параметры во вкладке PID')),
          )
        else
          Expanded(
            child: GridView.builder(
              padding: const EdgeInsets.all(12),
              itemCount: pids.length,
              gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                maxCrossAxisExtent: 270,
                mainAxisExtent: 188,
                mainAxisSpacing: 10,
                crossAxisSpacing: 10,
              ),
              itemBuilder: (context, i) {
                final p = pids[i];
                final sample = engine.latest[p.id];
                final unsupported = sample?.allOnes ?? false;
                final value = unsupported ? null : sample?.value;
                final outdated = engine.stale(p.id);
                final failed = engine.attempts[p.id]?.error.isNotEmpty ?? false;
                final tone = outdated || failed || unsupported ? muted : cyan;
                return Material(
                  color: const Color(0xFF101A2B),
                  borderRadius: BorderRadius.circular(14),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(14),
                    onTap: () => showModalBottomSheet<void>(
                      context: context,
                      isScrollControlled: true,
                      builder: (context) => SafeArea(
                        child: SingleChildScrollView(
                          child: section(p.id, [
                            Text(p.desc),
                            detail(
                              'Адреса',
                              p.addresses
                                  .map((a) => '0x${hexAddress(a)}')
                                  .join(', '),
                            ),
                            detail('Формула', p.formulaText),
                            detail(
                              'Сырые байты',
                              engine.latest[p.id]?.raw.map(hex2).join(' ') ??
                                  'нет',
                            ),
                            detail('Возраст', ageText(engine.ageMs(p.id))),
                            detail(
                              'Частота этого PID',
                              '${engine.frequency(p.id).toStringAsFixed(2)} Hz',
                            ),
                            detail(
                              'Окно чтения',
                              '${engine.latest[p.id]?.readMs ?? 0} ms',
                            ),
                            detail(
                              'Последняя ошибка',
                              engine.attempts[p.id]?.error ?? '',
                            ),
                          ]),
                        ),
                      ),
                    ),
                    child: Padding(
                      padding: const EdgeInsets.all(14),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Row(
                            children: [
                              Expanded(
                                child: Text(
                                  p.id,
                                  style: const TextStyle(
                                    fontSize: 13,
                                    fontWeight: FontWeight.bold,
                                  ),
                                ),
                              ),
                              if (outdated && value != null)
                                const Icon(
                                  Icons.schedule,
                                  size: 14,
                                  color: muted,
                                ),
                            ],
                          ),
                          Text(
                            '${p.unit} / 0x${hexAddress(p.address)}',
                            style: const TextStyle(fontSize: 10, color: muted),
                          ),
                          const Spacer(),
                          SizedBox(
                            height: 46,
                            width: double.infinity,
                            child: FittedBox(
                              fit: BoxFit.scaleDown,
                              alignment: Alignment.centerLeft,
                              child: Text(
                                value?.toStringAsFixed(p.digits) ?? '--',
                                style: TextStyle(
                                  color: tone,
                                  fontSize: 38,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                            ),
                          ),
                          const SizedBox(height: 10),
                          LinearProgressIndicator(
                            value: value == null
                                ? 0
                                : ((value - p.minValue) /
                                        (p.maxValue - p.minValue))
                                    .clamp(0.0, 1.0)
                                    .toDouble(),
                            color: tone,
                            minHeight: 3,
                          ),
                          const SizedBox(height: 8),
                          Text(
                            unsupported
                                ? '0xFF: адрес не поддерживается'
                                : failed
                                    ? 'ошибка / ${ageText(engine.ageMs(p.id))}'
                                    : '${ageText(engine.ageMs(p.id))} · ${(sample?.raw ?? []).map(hex2).join(' ')}',
                            style: TextStyle(
                              color:
                                  unsupported ? const Color(0xFFD4B57F) : muted,
                              fontSize: 10,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                );
              },
            ),
          ),
      ],
    );
  }
}

class FuelCard extends StatelessWidget {
  const FuelCard({
    super.key,
    required this.fuel,
    required this.mafStale,
    this.tripKm = 0,
    this.tripL = 0,
    this.onReset,
  });
  final FuelEstimate? fuel;
  final bool mafStale;
  final double tripKm, tripL;
  final VoidCallback? onReset;

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
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  'МГНОВЕННЫЙ РАСХОД',
                  style: TextStyle(
                    fontSize: 11,
                    letterSpacing: 1.2,
                    color: muted,
                  ),
                ),
              ),
              Text(
                f == null ? 'нет MAF' : 'расчет по MAF',
                style: const TextStyle(fontSize: 10, color: muted),
              ),
            ],
          ),
          const SizedBox(height: 10),
          if (f == null)
            const Text(
              'Включите PID MAF и запустите опрос.',
              style: TextStyle(fontSize: 12, color: muted),
            )
          else ...[
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(
                  f.litresPerHour.toStringAsFixed(2),
                  style: TextStyle(
                    fontSize: 38,
                    fontWeight: FontWeight.w700,
                    color: mafStale ? muted : cyan,
                    height: 1,
                  ),
                ),
                const Padding(
                  padding: EdgeInsets.only(left: 8, bottom: 4),
                  child: Text(
                    'л/ч',
                    style: TextStyle(fontSize: 13, color: muted),
                  ),
                ),
                const Spacer(),
                Text(
                  f.litresPer100km == null
                      ? 'на месте'
                      : '${f.litresPer100km!.toStringAsFixed(1)} л/100км',
                  style: TextStyle(
                    fontSize: 16,
                    fontWeight: FontWeight.w600,
                    color: mafStale ? muted : Colors.white,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Text(
              'AFR ${f.afrUsed.toStringAsFixed(1)}'
              '${f.afrMeasured ? ' (из ECU)' : ' (стехиометрия)'}'
              ' · плотность $kPetrolDensityGramsPerLitre г/л',
              style: const TextStyle(fontSize: 10, color: muted, height: 1.5),
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                const Icon(Icons.speed, size: 14, color: muted),
                const SizedBox(width: 6),
                Text(
                  '${tripKm.toStringAsFixed(1)} км',
                  style: const TextStyle(
                    color: cyan,
                    fontSize: 13,
                    fontWeight: FontWeight.bold,
                    fontFamily: 'monospace',
                  ),
                ),
                const SizedBox(width: 16),
                const Icon(Icons.local_gas_station, size: 14, color: muted),
                const SizedBox(width: 6),
                Text(
                  '${tripL.toStringAsFixed(2)} л',
                  style: const TextStyle(
                    color: cyan,
                    fontSize: 13,
                    fontWeight: FontWeight.bold,
                    fontFamily: 'monospace',
                  ),
                ),
                if (tripKm >= 0.5) ...[
                  const SizedBox(width: 16),
                  Text(
                    '${(tripL / tripKm * 100).toStringAsFixed(1)} л/100км',
                    style: const TextStyle(color: muted, fontSize: 11),
                  ),
                ],
                const Spacer(),
                SizedBox(
                  height: 28,
                  child: OutlinedButton.icon(
                    onPressed: onReset,
                    icon: const Icon(Icons.restart_alt, size: 14),
                    label: const Text('Сброс', style: TextStyle(fontSize: 11)),
                    style: OutlinedButton.styleFrom(
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      foregroundColor: muted,
                    ),
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
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
        .where((s) => s.error.isNotEmpty)
        .map((s) => s.pid.id)
        .toList()
      ..sort();
    attemptsTotal = widget.model.engine.attempts.length;
    errors = withErrors.isEmpty
        ? '0'
        : withErrors.length <= 8
            ? withErrors.join(', ')
            : '${withErrors.take(8).join(', ')} +${withErrors.length - 8}';
    setState(() {
      report = analyzeLog(
        widget.model.engine.history,
        attempts: widget.model.engine.attempts,
      );
    });
  }

  Future<void> _exportReport() async {
    final engine = widget.model.engine;
    final result =
        report ?? analyzeLog(engine.history, attempts: engine.attempts);
    final md = StringBuffer('# SSM2 0.10 · Анализ журнала\n\n');
    md.writeln(
      'Значений: ${result.sampleCount} · длительность: '
      '${result.spanSeconds.toStringAsFixed(1)} с · каналов: ${result.channels}',
    );
    for (final f in result.findings) {
      md.writeln('\n## [${_levelName(f.level)}] ${f.title}\n');
      md.writeln('${f.detail}\n');
      md.writeln('```\n${f.evidence}\n```\n');
      md.writeln('> ${f.advice}');
    }
    final dir = await getApplicationDocumentsDirectory();
    final file = File(
      '${dir.path}/ssm2_report_${DateTime.now().millisecondsSinceEpoch}.md',
    );
    await file.writeAsString(md.toString(), flush: true);
    await SharePlus.instance.share(
      ShareParams(files: [XFile(file.path)], text: 'SSM2 анализ журнала (md)'),
    );
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
    return ListView(
      children: [
        section('Анализ журнала', [
          const Text(
            'Прозрачные правила с фиксированными порогами.',
            style: TextStyle(color: muted, height: 1.6),
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              FilledButton.icon(
                onPressed: engine.history.isEmpty ? null : _schedule,
                icon: const Icon(Icons.insights, size: 18),
                label: const Text('Проанализировать'),
              ),
              OutlinedButton.icon(
                onPressed: () => setState(() => auto = !auto),
                icon: Icon(
                  auto ? Icons.check_box : Icons.check_box_outline_blank,
                  size: 18,
                ),
                label: const Text('Авто'),
              ),
              OutlinedButton.icon(
                onPressed: engine.history.isEmpty || widget.model.busy
                    ? null
                    : _exportReport,
                icon: const Icon(Icons.share, size: 16),
                label: const Text('Отчёт.md'),
              ),
            ],
          ),
        ]),
        if (result != null) ...[
          section('Итог', [
            detail('Значений в памяти', '${result.sampleCount}'),
            detail(
              'Длительность',
              '${result.spanSeconds.toStringAsFixed(1)} с',
            ),
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
                  border: Border(
                    left: BorderSide(color: _levelColor(f.level), width: 3),
                  ),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      _levelName(f.level),
                      style: TextStyle(
                        fontSize: 9,
                        letterSpacing: 1.2,
                        color: _levelColor(f.level),
                      ),
                    ),
                    const SizedBox(height: 6),
                    Text(
                      f.title,
                      style: const TextStyle(
                        fontSize: 14,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      f.detail,
                      style: const TextStyle(fontSize: 12, height: 1.6),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ],
    );
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
    return ListView(
      children: [
        section('Библиотека / 28 PID', [
          const Text(
            'Сначала 8 базовых. Больше параметров — ниже частота каждого PID.',
            style: TextStyle(color: muted),
          ),
          Wrap(
            spacing: 8,
            children: [
              TextButton(
                onPressed: m.busy ? null : () => m.preset(false),
                child: const Text('8 базовых'),
              ),
              TextButton(
                onPressed: m.busy ? null : () => m.preset(true),
                child: const Text('Все 20 обычных'),
              ),
              TextButton(
                onPressed: m.busy ? null : () => _settings(context),
                child: const Text('ROM / Float32'),
              ),
            ],
          ),
          TextField(
            decoration: const InputDecoration(
              labelText: 'Поиск PID или адреса',
              prefixIcon: Icon(Icons.search),
            ),
            onChanged: (value) => setState(() => search = value.toUpperCase()),
          ),
          const SizedBox(height: 8),
          for (final p in SubaruPidLibrary.all.where(
            (p) => '${p.id} ${p.desc} 0x${hexAddress(p.address)}'
                .toUpperCase()
                .contains(search.trim()),
          ))
            CheckboxListTile(
              contentPadding: EdgeInsets.zero,
              controlAffinity: ListTileControlAffinity.leading,
              title: Text(
                '${p.id}${p.extended ? ' *' : ''} / ${p.unit}',
                style: const TextStyle(fontSize: 13),
              ),
              subtitle: Text(
                '0x${hexAddress(p.address)} / ${p.bytesCount} B / P${p.priority}\n${p.formulaText}',
                style: const TextStyle(fontSize: 10, color: muted),
              ),
              value: m.engine.enabled.contains(p.id),
              onChanged: m.busy || (p.extended && !m.engine.extendedConfirmed)
                  ? null
                  : (value) => m.selectPid(p.id, value ?? false),
            ),
        ]),
      ],
    );
  }

  Future<void> _settings(BuildContext context) async {
    final m = widget.model;
    final controller = TextEditingController(text: m.engine.romId);
    var confirm = m.engine.extendedConfirmed;
    var little = m.engine.endian == Endian.little;
    final accepted = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, change) => AlertDialog(
          title: const Text('Extended / ROM'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Введите ROM ID из своего def-файла для включения extended PID.',
                ),
                TextField(
                  controller: controller,
                  decoration: const InputDecoration(labelText: 'ROM ID'),
                ),
                CheckboxListTile(
                  title: const Text('Адреса сверены с моим ROM'),
                  value: confirm,
                  onChanged: (value) => change(() => confirm = value ?? false),
                ),
                SwitchListTile(
                  title: const Text('Float32 little-endian'),
                  value: little,
                  onChanged: (value) => change(() => little = value),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Отмена'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Сохранить'),
            ),
          ],
        ),
      ),
    );
    final rom = controller.text;
    controller.dispose();
    if (accepted == true)
      await m.configureExtended(
        confirm,
        rom,
        little ? Endian.little : Endian.big,
      );
  }
}

class LoggerPage extends StatelessWidget {
  const LoggerPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) => ListView(
        children: [
          section('Потоковый CSV', [
            Text(
              '${model.logger.count}',
              style: const TextStyle(
                fontSize: 54,
                fontWeight: FontWeight.bold,
                color: cyan,
              ),
            ),
            Text(
              model.logger.active
                  ? 'Идет запись (RR-формат)'
                  : 'Запись остановлена',
            ),
            // v0.10 FIX (LOG-03): ошибка логгера видна пользователю, а не тонет в поле объекта.
            if (model.logger.error.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  'Логгер: ${model.logger.error}',
                  style:
                      const TextStyle(color: Color(0xFFE08A7A), fontSize: 12),
                ),
              ),
            const SizedBox(height: 18),
            Wrap(
              spacing: 8,
              children: [
                FilledButton.icon(
                  onPressed: model.busy ? null : model.toggleLog,
                  icon: Icon(
                    model.logger.active
                        ? Icons.stop
                        : Icons.fiber_manual_record,
                  ),
                  label: Text(model.logger.active ? 'Стоп' : 'Записать'),
                ),
                OutlinedButton.icon(
                  onPressed: model.busy || model.logger.count == 0
                      ? null
                      : model.exportCsv,
                  icon: const Icon(Icons.share),
                  label: const Text('Экспорт CSV'),
                ),
                OutlinedButton.icon(
                  onPressed: model.busy || model.engine.history.isEmpty
                      ? null
                      : model.exportRrCsv,
                  icon: const Icon(Icons.table_chart),
                  label: const Text('RR CSV'),
                ),
              ],
            ),
          ]),
        ],
      );
}

class GraphPage extends StatelessWidget {
  const GraphPage(this.model, {super.key});
  final AppModel model;
  @override
  Widget build(BuildContext context) {
    final pid = SubaruPidLibrary.byId(model.chartId);
    final samples = List<PidSample>.of(model.engine.history[pid.id] ?? []);
    return ListView(
      children: [
        section('График', [
          DropdownButton<String>(
            value: model.chartId,
            isExpanded: true,
            items: SubaruPidLibrary.all
                .map(
                  (p) => DropdownMenuItem(
                    value: p.id,
                    child: Text('${p.id} / ${p.unit}'),
                  ),
                )
                .toList(),
            onChanged: (value) {
              if (value != null) {
                model.chartId = value;
                model.changed();
              }
            },
          ),
          const SizedBox(height: 18),
          SizedBox(
            height: 270,
            child: samples.length < 2
                ? const Center(child: Text('Нужны хотя бы два значения'))
                : CustomPaint(
                    painter: TelemetryPainter(samples, pid.digits),
                    size: Size.infinite,
                  ),
          ),
        ]),
      ],
    );
  }
}

class TelemetryPainter extends CustomPainter {
  TelemetryPainter(this.samples, this.digits);
  final List<PidSample> samples;
  final int digits;
  void label(Canvas canvas, String text, Offset point) {
    final painter = TextPainter(
      text: TextSpan(
        text: text,
        style: const TextStyle(color: muted, fontSize: 10),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
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
    final grid = Paint()
      ..color = const Color(0xFF233045)
      ..strokeWidth = 1;
    for (var i = 0; i <= 4; i++) {
      final y = 8 + height * i / 4;
      canvas.drawLine(Offset(52, y), Offset(size.width - 8, y), grid);
      label(
        canvas,
        (hi - (hi - lo) * i / 4).toStringAsFixed(digits > 1 ? 1 : digits),
        Offset(0, y - 5),
      );
    }
    final path = Path();
    for (var i = 0; i < samples.length; i++) {
      final sample = samples[i];
      final x =
          52 + width * (sample.time.millisecondsSinceEpoch - first) / span;
      final y = 8 + height * (hi - sample.value!) / (hi - lo);
      canvas.drawCircle(Offset(x, y), 2, Paint()..color = cyan);
      if (i == 0 ||
          sample.time.difference(samples[i - 1].time).inMilliseconds > 3000) {
        path.moveTo(x, y);
      } else {
        path.lineTo(x, y);
      }
    }
    canvas.drawPath(
      path,
      Paint()
        ..color = cyan
        ..strokeWidth = 2
        ..style = PaintingStyle.stroke,
    );
    label(canvas, '0 s', Offset(52, height + 20));
    label(
      canvas,
      '${(span / 1000).toStringAsFixed(1)} s',
      Offset(size.width - 55, height + 20),
    );
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
  void dispose() {
    command.dispose();
    address.dispose();
    count.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final m = widget.model;
    return ListView(
      children: [
        section('Диагностика', [
          TextField(
            controller: command,
            decoration: const InputDecoration(
              labelText: 'ATI / ATRV / A8 00 00 00 08',
            ),
          ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: m.busy || !m.elm.ready
                ? null
                : () => m.sendDiagnostic(command.text),
            child: const Text('Отправить'),
          ),
          SelectableText(
            m.terminal.isEmpty ? 'Команд еще нет' : m.terminal,
            style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
          ),
        ]),
        section('Сканер адресов A8', [
          Row(
            children: [
              Expanded(
                child: TextField(
                  controller: address,
                  decoration: const InputDecoration(labelText: 'Адрес HEX'),
                ),
              ),
              const SizedBox(width: 12),
              SizedBox(
                width: 90,
                child: TextField(
                  controller: count,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(labelText: '1..32 байт'),
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: m.busy || !m.elm.ready
                ? null
                : () => m.scan(address.text, count.text),
            child: const Text('Прочитать'),
          ),
          SelectableText(
            m.scanner.isEmpty ? 'Нет дампа' : m.scanner,
            style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
          ),
        ]),
      ],
    );
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
  // v0.10: сколько ближайших A8-запросов оставить «глухими» (без ответа) —
  // имитация транзиентного таймаута клона для регрессионного теста ELM-01.
  int silentA8 = 0;
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
  Future<List<BtDevice>> paired() async => [
    const BtDevice('Test', '00:00:00:00:00:00'),
  ];

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
    if (cmd.startsWith('A8') && silentA8 > 0) {
      silentA8--; // v0.10: «глухое окно» — драйвер уйдёт в таймаут
      return;
    }
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
      expect(
        parseAddressReply('SEARCHING...\r\n7E8 02 E8 6D\r>', command),
        109,
      );
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
    test(
      'initialize performs ECU probe and keeps conservative timeout',
      () async {
        final transport = FakeTransport(
          a8DelayMs: 420,
          a8Reply: '7E8 02 E8 6D\r>',
        );
        final elm = ElmDriver(transport);
        await elm.initialize('00:00:00:00:00:00');
        expect(elm.ready, isTrue);
        expect(elm.activeProfile, isNotEmpty);
        expect(elm.timeoutMs, greaterThanOrEqualTo(900));
        final read = await elm.readRange(0x000008, 1);
        expect(read.bytes, <int>[0x6D]);
        await elm.dispose();
      },
    );
  });

  group('ELM-01 retry (v0.10)', () {
    test(
      'одиночный таймаут не роняет sync, чтение доезжает со второго раза',
      () async {
        final transport = FakeTransport();
        final elm = ElmDriver(transport);
        await elm.initialize('00:11:22:33:44:55');
        expect(elm.ready, isTrue);
        transport.silentA8 =
            1; // в 0.9 этот таймаут убивал sync и весь дальнейший опрос
        final read = await elm.readRange(0x000008, 1);
        expect(read.bytes, <int>[0x6D]);
        expect(elm.ready, isTrue);
        await elm.dispose();
      },
    );
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
    List<PidSample> ramp(String id, double v) => [
      for (var i = 0; i < 10; i++) fake(id, v, 1000 - i * 100),
    ];

    test('детон-кластер под нагрузкой -> критичная находка', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'FBKC': ramp('FBKC', -3.0),
        'RPM': ramp('RPM', 4200),
        'LOAD_4B': ramp(
          'LOAD_4B',
          2.4,
        ), // v0.10: нагрузка в г/об — штатные единицы
      });
      expect(
        result.findings.any((f) => f.level == FindingLevel.critical),
        isTrue,
      );
      expect(result.sampleCount, greaterThan(0));
    });

    test('чистый лог -> без критичных находок', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'FBKC': ramp('FBKC', 0),
        'IAM': ramp('IAM', 1.0), // v0.10: штатная float-шкала множителя 0..1
        'RPM': ramp('RPM', 2500),
        'ECT': ramp('ECT', 88),
      });
      expect(
        result.findings.any((f) => f.level == FindingLevel.critical),
        isFalse,
      );
    });

    test('AFR беднее цели -> находка про смесь', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'AFR': ramp('AFR', 12.9),
        'CL_TARGET': ramp('CL_TARGET', 11.5),
      });
      expect(result.findings.any((f) => f.title.contains('Смесь')), isTrue);
    });

    test('выбросы по z-score -> информационная находка', () {
      final flat = [
        for (var i = 0; i < 40; i++) fake('RPM', 800, 5000 - i * 100),
      ];
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
        'RPM': [
          for (var i = 0; i < 12; i++)
            at('RPM', i.isEven ? 740.0 : 960.0, agos[i]),
        ],
        'TPS': [for (var i = 0; i < 12; i++) at('TPS', 2, agos[i])],
        'ECT': [for (var i = 0; i < 12; i++) at('ECT', 85, agos[i])],
      });
      final idle = result.findings.where((f) => f.title.contains('холост'));
      expect(idle.isNotEmpty, isTrue);
      expect(idle.first.level, FindingLevel.warning);
    });

    test('v0.10 IAM шкала 0..1: 0.98 -> без критичного, 0.5 -> критичный', () {
      final ok = analyzeLog(<String, List<PidSample>>{
        'IAM': ramp('IAM', 0.98),
      });
      expect(
        ok.findings.any(
          (f) => f.level == FindingLevel.critical && f.title.contains('IAM'),
        ),
        isFalse,
      );
      final bad = analyzeLog(<String, List<PidSample>>{
        'IAM': ramp('IAM', 0.5),
      });
      expect(
        bad.findings.any(
          (f) => f.level == FindingLevel.critical && f.title.contains('IAM'),
        ),
        isTrue,
      );
    });

    test('v0.10 процентная нагрузка 45% — недоказанная нагрузка (не г/об)', () {
      final result = analyzeLog(<String, List<PidSample>>{
        'FBKC': [fake('FBKC', -1.6, 1000), fake('FBKC', -1.6, 900)],
        'RPM': ramp('RPM', 4200),
        'LOAD': ramp('LOAD', 45),
      });
      // В 0.9 «hardLoad» был истинен при любом % (45 >= 1.5) — ложный критикал.
      expect(
        result.findings.any(
          (f) =>
              f.level == FindingLevel.critical && f.title.contains('Детонация'),
        ),
        isFalse,
      );
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
      expect(
        SubaruPidLibrary.byId('O2_F').decode([0, 200]),
        closeTo(1.0, 0.000001),
      );
    });
  });
}
'''

FILES["lib/maps_lab/maps_lab_log.dart"] = r'''
/// Map Lab: лог и правила.
library;

import 'dart:convert';
import 'dart:math' as math;

import 'maps_lab_core.dart';

class LogData {
  final cols = <String, List<double?>>{};
  // v0.10: true, если колонка нагрузки — проценты, а не г/об (детект по значениям).
  bool loadIsPercent = false;
  int get rows => cols.isEmpty ? 0 : cols.values.first.length;

  bool has(String k) => cols.containsKey(k);
  List<double?>? operator [](String k) => cols[k];

  static const aliases = <String, List<String>>{
    'time': ['time', 'timestamp', 'time s', 'elapsed'],
    'rpm': ['engine speed', 'rpm', 'engine speed rpm'],
    'load': [
      'engine load (4-byte)',
      'engine load g/rev',
      'load_4b',
      'engine load 4-byte',
      'calculated load',
      'engine load',
      'load',
      'engine load (relative)',
    ],
    'fbkc': ['feedback knock correction', 'fbkc'],
    'flkc': [
      'fine learning knock correction',
      'fkl',
      'fine learning knock advance',
      'flkc',
    ],
    'iam': ['iam', 'ignition advance multiplier'],
    'timing': ['total ignition timing', 'ignition timing', 'timing'],
    'afr': [
      'afr',
      'a/f sensor #1',
      'a/f sensor 1',
      'air/fuel ratio',
      'estimated afr',
      'lambda',
    ],
    'boost': ['manifold relative pressure', 'boost', 'boost_rel'],
    'tgt': ['target boost', 'boost_tgt'],
    'berr': ['boost error', 'boost_err'],
    'wgdc': [
      'primary wastegate duty',
      'primary wastegate duty cycle', // v0.10: фактическое имя колонки exportRrCsv
      'wastegate duty',
      'wgdc',
      'boost control solenoid duty',
    ],
    'tq': [
      'requested torque',
      'cl_target',
      'closed loop fueling target', // v0.10: фактическое имя колонки exportRrCsv
      'demand torque',
    ],
    'thr': [
      'throttle opening angle',
      'throttle plate',
      'throttle',
      'throttle position',
    ],
    'iat': ['intake air temperature', 'iat'],
    'ect': ['coolant temperature', 'ect', 'engine coolant temperature'],
    'loop': ['cl/ol', 'fueling status', 'closed loop', 'loop'],
  };

  static String _canon(String s) {
    // v0.10 FIX (MAPLAB-01): раньше строку обрубали по ПЕРВОЙ скобке, и
    // «Engine Load (4-Byte)» схлопывался с «Engine Load (Relative)» — колонку
    // нагрузки в г/об было невозможно отличить от процентной. Снимаем только
    // ОДНУ финальную группу (это единицы измерения), маркеры типа (4-Byte) живут.
    var c = s.toLowerCase().trim().replaceAll('*', '');
    if (c.endsWith(')')) {
      final open = c.lastIndexOf('(');
      if (open > 0) c = c.substring(0, open);
    }
    if (c.endsWith(']')) {
      final open = c.lastIndexOf('[');
      if (open > 0) c = c.substring(0, open);
    }
    return c.replaceAll(RegExp(r'\s+'), ' ').trim();
  }

  static LogData parse(String text) {
    final head = text.substring(0, math.min(text.length, 4096));
    final sep = ';'.allMatches(head).length > ','.allMatches(head).length
        ? ';'
        : ',';
    final decComma =
        sep == ';' && ','.allMatches(head).length > '.'.allMatches(head).length;

    final lines = const LineSplitter()
        .convert(text)
        .where((l) => l.trim().isNotEmpty)
        .toList();
    if (lines.isEmpty) return LogData();
    final header = lines.first
        .split(sep)
        .map((h) => h.trim().replaceAll('"', ''))
        .toList();

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
      // v0.10 FIX2: варианты НЕ гоняем через _canon — он стирает значимый
      // маркер '(4-byte)' и из вариантов тоже (первая версия фикса это упустила,
      // поймано flutter test в конвейере ячейки 05).
      final variants = entry.value.toSet();
      // v0.10: приоритет — порядок вариантов, а не порядок колонок в файле.
      // Иначе %-колонка «Engine Load (Relative)» перехватывала канал у (4-Byte).
      variantLoop:
      for (final variant in variants) {
        for (var i = 0; i < header.length; i++) {
          if (usedNames.contains(header[i])) continue;
          if (canonHead[i] == variant) {
            log.cols[canon] = raw[i];
            usedNames.add(header[i]);
            break variantLoop;
          }
        }
      }
    }

    // v0.10: шкала нагрузки (медиана > 8 — это проценты, г/об так не бывает).
    final loadCol = log.cols['load'];
    if (loadCol != null) {
      final lv = loadCol.whereType<double>().toList()..sort();
      if (lv.isNotEmpty && lv[lv.length ~/ 2] > 8) log.loadIsPercent = true;
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
    // v0.10.1: детон-каналы только в расширенном наборе — скажем об этом прямо.
    'fbkc':
        'Feedback Knock Correction (4-byte)* — нужен расширенный набор (CALID)',
    'flkc': 'Fine Learning Knock Correction* — нужен расширенный набор (CALID)',
    'load': 'Engine Load (4-Byte)* — г/об вместо процентной нагрузки',
    'timing': 'Total Ignition Timing',
  };

  static LogHealth check(LogData log) {
    final h = LogHealth();
    h.notes.add('строк: ${log.rows}');
    if (log.loadIsPercent) {
      h.notes.add(
        'нагрузка в % — приведена к оси г/об приближением 100% ≈ max; '
        'для точного биннинга логируйте Engine Load (4-Byte)',
      );
    }

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
      h.notes.add(
        'IAM min: ${iamMin.toStringAsFixed(2)}'
        '${iamMin < 0.99 ? ' — ЛОГ НЕ ГОДИТСЯ: сначала доучить ЭБУ' : ' — ок'}',
      );
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
        h.notes.add(
          'частота: медиана ${(1 / med).toStringAsFixed(1)} Гц · провалов >300 мс: $gaps',
        );
        if (med > 0.25)
          h.notes.add('!! реже 4 Гц — сократите набор PID до 10–12');
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
  final double afrErr,
      afrMaxCut,
      boostErr,
      wgdcMaxDelta,
      wotLoad,
      targetBoostGain;
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
    if (!{'timing', 'knockadv', 'fuel', 'wgdc', 'boost'}.contains(kind))
      return null;

    final xIsRpm =
        g.x.guess == 'rpm' || (g.y.guess != 'rpm' && g.x.values.last > 800);
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
    // v0.10 FIX (ANLZ-03): средние — по количеству НЕПУСТЫХ значений,
    // раньше деление на общий n занижало flkc/afr по ячейке.
    final flkcCnt = List<int>.filled(rows * cols, 0);
    final afrCnt = List<int>.filled(rows * cols, 0);
    final halfRows = (log.rows / 2).round();
    final iatMax = List<double>.filled(rows * cols, -999);
    final iamMin = List<double>.filled(rows * cols, 999);
    final afrSum = List<double>.filled(rows * cols, 0);
    final berrSum = List<double>.filled(rows * cols, 0);

    final fbkc = log['fbkc'], flkc = log['flkc'], iat = log['iat'];
    final iam = log['iam'], afr = log['afr'], berr = log['berr'];

    // v0.10 FIX (MAPLAB-01): процентную нагрузку приводим к диапазону оси
    // карты (100% ≈ максимум оси), иначе значения 0..100 прибивались к краю.
    final otherIsLoad =
        kind == 'timing' || kind == 'knockadv' || kind == 'fuel';
    final otherAxisMax = otherAxis.isEmpty ? 0.0 : otherAxis.reduce(math.max);
    final otherScale = otherIsLoad && log.loadIsPercent && otherAxisMax > 0
        ? otherAxisMax / 100.0
        : 1.0;
    for (var i = 0; i < log.rows; i++) {
      final rv = rpmCol[i], ovRaw = otherCol[i];
      if (rv == null || ovRaw == null) continue;
      final ov = ovRaw * otherScale;
      final ri = _binIdx(xIsRpm ? otherAxis : rpmAxis, xIsRpm ? ov : rv);
      final ci = _binIdx(xIsRpm ? rpmAxis : otherAxis, xIsRpm ? rv : ov);
      final idx = ri * cols + ci;
      cnt[idx]++;
      final f = fbkc?[i];
      if (f != null && (cnt[idx] == 1 || f < fbkcMin[idx])) fbkcMin[idx] = f;
      final fl = flkc?[i];
      if (fl != null) {
        flkcSum[idx] += fl;
        flkcCnt[idx]++;
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
      if (a != null) {
        afrSum[idx] += a;
        afrCnt[idx]++;
      }
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
        res.info[rr][cc] = CellNote()
          ..why = 'веер от соседнего кластера: ${d2.toStringAsFixed(1)}°';
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
          ..flkc = flkcCnt[idx] > 0
              ? flkcSum[idx] / flkcCnt[idx]
              : 0.0 // v0.10
          ..iat = iatMax[idx] < -900 ? null : iatMax[idx]
          ..iam = iamMin[idx] > 900 ? null : iamMin[idx]
          ..afr = afrCnt[idx] > 0
              ? afrSum[idx] / afrCnt[idx]
              : null // v0.10
          ..berr = berrSum[idx] != 0 ? berrSum[idx] / n : null;

        if (kind == 'timing' || kind == 'knockadv') {
          if (note.iam != null && note.iam! < 0.99) {
            res.info[ri][ci] = note
              ..why = 'IAM=${note.iam!.toStringAsFixed(2)} — сначала доучить';
            continue;
          }
          final lateCount = flkcLateCnt[idx];
          final lateFlkc = lateCount > 0
              ? flkcLateSum[idx] / lateCount
              : note.flkc!;
          final knock = math.min(
            note.fbkc!,
            math.min(note.flkc!, lateFlkc) * 1.4,
          );
          if (knock <= cfg.fbkcEvent) {
            final d = math.max(
              cfg.timingMaxCut,
              math.min(
                -cfg.timingStep,
                _roundStep(knock * 0.6, cfg.timingStep),
              ),
            );
            res.delta[ri][ci] = d;
            fanTiming(ri, ci, d, cfg);
            res.info[ri][ci] = note
              ..why =
                  'детон-кластер · FBKC ${note.fbkc!.toStringAsFixed(1)}°, '
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
              ..why =
                  'факт ${note.afr!.toStringAsFixed(2)} против цели ${target.toStringAsFixed(2)} '
                  '— обогатить на ${d.abs().toStringAsFixed(1)}; если не помогает — MAF/давление';
          } else if (knock <= cfg.fbkcEvent) {
            res.delta[ri][ci] = -0.3;
            res.info[ri][ci] = note
              ..why = 'детон при цели ~совпадает — запас −0.3 AFR';
          }
        } else {
          final e = note.berr;
          if (e == null) continue;
          if (kind == 'wgdc') {
            if (e.abs() > cfg.boostErr) {
              final d = _roundStep(-e * 45, 1)
                  .clamp(
                    -cfg.wgdcMaxDelta,
                    cfg.wgdcMaxDelta,
                  ) // v0.10: было n-1 (опечатка)
                  .toDouble();
              if (d != 0) {
                res.delta[ri][ci] = d;
                res.info[ri][ci] = note
                  ..why =
                      '${e > 0 ? 'овербуст' : 'недобор'} ${e.toStringAsFixed(2)} бар '
                      '→ WGDC ${d > 0 ? '+' : ''}${d.toStringAsFixed(0)}%';
              }
            }
          } else {
            if (e > cfg.boostErr) {
              res.info[ri][ci] = note
                ..why =
                    'овербуст ${e.toStringAsFixed(2)} бар — чинить через WGDC/TD, не таргет';
            }
            if (cfg.targetBoostGain > 0) {
              res.delta[ri][ci] = cfg.targetBoostGain;
              res.info[ri][ci] = note
                ..why =
                    'политика +${cfg.targetBoostGain} бар (после фикса WGDC и чистого детон-лога)';
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
'''

FILES["test/maps_lab_test.dart"] = r"""
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:subaru_ssm2/maps_lab/maps_lab_core.dart';
import 'package:subaru_ssm2/maps_lab/maps_lab_log.dart';

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
    final defs = DefSet.build({
      '32BITBASE': baseXml,
      'TEST_ROM': derivedXml,
    }, 'TEST_ROM');
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

    final defs = DefSet.build({
      '32BITBASE': baseXml,
      'TEST_ROM': derivedXml,
    }, 'TEST_ROM');
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
      final rpm = <double?>[],
          load = <double?>[],
          f = <double?>[],
          fl = <double?>[];
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

    test(
      'детон-кластер → отрицательная дельта в своей ячейке и веер сглаживания',
      () {
        final res = Analyzer(
          const AnalyzerConfig(),
        ).analyzeMap(smallGrid(), syntheticLog(fbkc: -4.0))!;
        final d = res.delta[1][1];
        expect(d, lessThan(0));
        expect(d, greaterThanOrEqualTo(-3.0));
        expect(res.delta[0][0], -0.5);
        expect(res.info[1][1]!.why, contains('детон'));
      },
    );

    test('чистый лог при выключенном allowTimingAdd → ноль правок', () {
      final res = Analyzer(
        const AnalyzerConfig(),
      ).analyzeMap(smallGrid(), syntheticLog(fbkc: 0))!;
      final anyDelta = res.delta.expand((r) => r).any((v) => v != 0);
      expect(anyDelta, isFalse);
    });

    test('CSV: автоопределение ; и десятичной запятой, алиасы колонок', () {
      const csv =
          'Time (s);Engine Speed (RPM);Feedback Knock Correction;Load_4B\r\n'
          '0,0;3000;0,0;3,00\r\n'
          '0,1;3100;-2,5;3,10\r\n';
      final log = LogData.parse(csv);
      expect(log['rpm']![1], 3100);
      expect(log['fbkc']![1], -2.5);
      expect(log['load']![1], 3.1);
    });

    test('v0.10 CSV: (4-Byte) не схлопывается с (Relative), г/об в приоритете', () {
      const csv =
          'Time,Engine Load (Relative) (%),Engine Load (4-Byte) (grams/rev)*,Engine Speed (rpm)\n'
          '0.0,64,2.90,3000\n'
          '0.2,66,3.05,3100\n';
      final log = LogData.parse(csv);
      // В 0.9 обе колонки канонизировались в 'engine load' и побеждала первая — %.
      expect(log['load']![0], 2.9);
      expect(log['load']![1], 3.05);
      expect(log.loadIsPercent, isFalse);
    });

    test('v0.10 Analyzer: процентная нагрузка приводится к оси карты', () {
      final log = syntheticLog(fbkc: -4.0);
      log.cols['load'] = [
        for (var i = 0; i < 600; i++) 100.0,
      ]; // 100% ≈ max оси (3.0)
      log.loadIsPercent = true;
      final res = Analyzer(
        const AnalyzerConfig(),
      ).analyzeMap(smallGrid(), log)!;
      // Строки прибиваются к верхней строке оси (3.0), и детон-кластер находится там же.
      expect(res.info[1][1]!.why, contains('детон'));
    });
  });
}
"""

FILES["lib/maps_lab/maps_lab_page.dart"] = r'''
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
    return Color.lerp(
      const Color(0xFF4CAF50),
      const Color(0xFFFFEB3B),
      t / 0.33,
    )!;
  } else if (t < 0.66) {
    return Color.lerp(
      const Color(0xFFFFEB3B),
      const Color(0xFFFF9800),
      (t - 0.33) / 0.33,
    )!;
  } else {
    return Color.lerp(
      const Color(0xFFFF9800),
      const Color(0xFFF44336),
      (t - 0.66) / 0.34,
    )!;
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
        await File(
          '${dir.path}/${e.key}.xml',
        ).writeAsString(e.value, flush: true);
      } catch (_) {}
    }
  }

  bool _applyDefs(Map<String, String> xmlById, String source) {
    if (xmlById.length < 3) return false;
    try {
      _defs = DefSet.build(xmlById, 'A2TB100B');
      _defsSource = source;
      _say(
        'дефиниции [$source]: ${_defs!.chain.join(' → ')} · '
        'ecuid ${_defs!.meta.ecuid} · ${_defs!.tables.length} таблиц',
      );
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
    final f = await _chooseFile([
      '.bin',
      '.hex',
      '.rom',
    ], 'Выберите прошивку (.bin)');
    if (f == null) return;
    try {
      final bytes = await f.readAsBytes();
      setState(() {
        _results = null;
        _selRi = _selCi = null;
      });
      final parser = RomParser(bytes);
      final idAddr =
          int.tryParse(_defs?.meta.internalIdAddress ?? '2000', radix: 16) ??
          0x2000;
      final romId = parser.readRomId(idAddr);
      final expected = _defs?.meta.internalIdString ?? '';
      final matched = expected.isEmpty || romId == expected;
      _say(
        'ROM: ${f.uri.pathSegments.last} · ${(bytes.length / 1024).toStringAsFixed(0)} КБ · '
        'ID="$romId"${matched ? '' : '  (ожидался $expected)'}',
      );
      _maps = parser.extractKeys(_defs!);
      _mapName = _maps!.keys.firstOrNull;
      _say('карт извлечено: ${_maps!.length} из ${RomParser.keyTables.length}');
      setState(() {});
    } catch (e) {
      _say('!! ошибка парсинга ROM: $e');
    }
  }

  // v0.10.1 SYNC: лог лежит в приватном каталоге приложения — разрешения не нужны.
  // Открываем записанный CSV напрямую: вкладка «CSV» → Map Lab в один тап.
  Future<File?> _latestAppLog() async {
    try {
      final dir = await getApplicationDocumentsDirectory();
      final files =
          dir
              .listSync()
              .whereType<File>()
              .where(
                (f) =>
                    f.path.toLowerCase().endsWith('.csv') &&
                    f.uri.pathSegments.last.startsWith('ssm2_'),
              )
              .toList()
            ..sort((a, b) => b.path.compareTo(a.path));
      return files.isEmpty ? null : files.first;
    } catch (_) {
      return null;
    }
  }

  Future<void> _readLogIntoLab(File f) async {
    final text = utf8.decode(await f.readAsBytes(), allowMalformed: true);
    _log = LogData.parse(text);
    final health = LogAudit.check(_log!);
    _say('лог: ${f.uri.pathSegments.last} · ${_log!.rows} строк');
    for (final n in health.notes) {
      _say('   $n');
    }
    setState(() {});
  }

  Future<void> _loadLatestLog() async {
    try {
      final f = await _latestAppLog();
      if (f == null) {
        _say(
          'нет логов приложения: сначала запишите CSV на вкладке «CSV» (кнопка «Записать»)',
        );
        return;
      }
      await _readLogIntoLab(f);
    } catch (e) {
      _say('!! ошибка чтения лога: $e');
    }
  }

  Future<void> _pickLog() async {
    final f = await _chooseFile([
      '.csv',
      '.txt',
      '.log',
    ], 'Выберите файл лога (.csv)');
    if (f == null) return;
    try {
      await _readLogIntoLab(f);
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
    _say(
      'анализ: вердикты по ${_results!.length} картам · убавить $dec, прибавить $inc ячеек',
    );
    setState(() {});
  }

  Future<void> _export() async {
    if (_maps == null || _results == null) return;
    final dir = await getApplicationDocumentsDirectory();
    final stamp = DateTime.now()
        .toIso8601String()
        .replaceAll(':', '-')
        .split('.')
        .first;
    final outDir = Directory('${dir.path}/maplab_$stamp')
      ..createSync(recursive: true);
    final files = <XFile>[];
    final md = StringBuffer('# Map Lab A2TB100B · рекомендации\n');
    for (final e in _maps!.entries) {
      final g = e.value;
      final res = _results![e.key];
      if (res == null) continue;
      final head =
          '\t${g.x.values.map((v) => v.toStringAsFixed(2)).join('\t')}';
      final rows = <String>[
        for (var ri = 0; ri < g.rows; ri++)
          '${g.y.values[ri].toStringAsFixed(2)}\t${[for (var ci = 0; ci < g.cols; ci++) (g.data[ri][ci] + res.delta[ri][ci]).toStringAsFixed(3)].join('\t')}',
      ];
      final tsv = '$head\n${rows.join('\n')}\n';
      final file = File(
        '${outDir.path}/${e.key.replaceAll(RegExp(r'[/ ]'), '_')}_recommended.tsv',
      )..writeAsStringSync(tsv);
      files.add(XFile(file.path));
      // v0.9: снимок исходной таблицы ДО правок — страховка для отката.
      final origRows = <String>[
        for (var ri = 0; ri < g.rows; ri++)
          '${g.y.values[ri].toStringAsFixed(2)}\t${[for (var ci = 0; ci < g.cols; ci++) g.data[ri][ci].toStringAsFixed(3)].join('\t')}',
      ];
      final origFile = File(
        '${outDir.path}/${e.key.replaceAll(RegExp(r'[/ ]'), '_')}_original.tsv',
      )..writeAsStringSync('$head\n${origRows.join('\n')}\n');
      files.add(XFile(origFile.path));
      md.writeln('\n## ${e.key} (${g.units})');
      for (var ri = 0; ri < g.rows; ri++) {
        for (var ci = 0; ci < g.cols; ci++) {
          final d = res.delta[ri][ci];
          if (d == 0) continue;
          final inf = res.info[ri][ci];
          md.writeln(
            '- ${g.x.values[ci]} × ${g.y.values[ri]}: ${g.data[ri][ci]} → '
            '${(g.data[ri][ci] + d).toStringAsFixed(2)} (${d > 0 ? '+' : ''}$d)'
            '${inf != null && inf.why.isNotEmpty ? ' — ${inf.why}' : ''}',
          );
        }
      }
    }
    final mf = File('${outDir.path}/recommendations.md')
      ..writeAsStringSync(md.toString());
    files.insert(0, XFile(mf.path));
    _say('экспорт: ${files.length} файлов → ${outDir.path}');
    await SharePlus.instance.share(
      ShareParams(
        files: files,
        text: 'Map Lab — рекомендации и снимки исходных карт',
      ),
    );
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
            IconButton(
              icon: const Icon(Icons.ios_share),
              onPressed: _export,
              tooltip: 'Экспорт',
            ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 24),
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _stepBtn(
                '0 · Дефиниции',
                Icons.folder_special,
                _pickDefs,
                ok: _defs != null,
              ),
              _stepBtn(
                '1 · Прошивка',
                Icons.memory,
                _pickRom,
                ok: _maps != null,
              ),
              _stepBtn(
                '2 · Лог',
                Icons.description,
                _pickLog,
                ok: _log != null,
              ),
              _stepBtn(
                '3 · Анализ',
                Icons.psychology,
                (_maps != null && _log != null) ? _runAnalysis : null,
                ok: _results != null,
              ),
              _stepBtn(
                'Экспорт',
                Icons.ios_share,
                _results != null ? _export : null,
              ),
            ],
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: _loadLatestLog, // v0.10.1 SYNC
              icon: const Icon(Icons.history, size: 16),
              label: const Text('последний лог приложения — в один тап'),
              style: TextButton.styleFrom(
                foregroundColor: const Color(0xFF22D3EE),
                textStyle: const TextStyle(fontSize: 12),
              ),
            ),
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
                        label: Text(
                          e.key,
                          style: const TextStyle(fontSize: 11),
                        ),
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
                    ButtonSegment(
                      value: false,
                      icon: Icon(Icons.table_chart, size: 16),
                      label: Text('2D'),
                    ),
                    ButtonSegment(
                      value: true,
                      icon: Icon(Icons.threed_rotation, size: 16),
                      label: Text('3D'),
                    ),
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
            Wrap(
              spacing: 12,
              runSpacing: 4,
              children: [
                _legend(const Color(0xFF4CAF50), 'низ'),
                _legend(const Color(0xFFFFEB3B), 'середина'),
                _legend(const Color(0xFFF44336), 'верх'),
                _legend(Colors.lightBlueAccent, 'правка'),
              ],
            ),
          ],
          const SizedBox(height: 16),
          Text('Журнал', style: theme.textTheme.titleSmall),
          const SizedBox(height: 6),
          Container(
            width: double.infinity,
            constraints: const BoxConstraints(minHeight: 180),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(
              color: theme.colorScheme.surfaceContainerHighest.withValues(
                alpha: 0.35,
              ),
              borderRadius: BorderRadius.circular(8),
            ),
            child: _journal.isEmpty
                ? const Text(
                    '— пока пусто —',
                    style: TextStyle(color: Colors.white38, fontSize: 12),
                  )
                : Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      for (final j in _journal)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 2),
                          child: Text(
                            j,
                            style: const TextStyle(
                              fontFamily: 'monospace',
                              fontSize: 11,
                            ),
                          ),
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

  Widget _stepBtn(
    String label,
    IconData icon,
    VoidCallback? onTap, {
    bool ok = false,
  }) {
    return FilledButton.tonalIcon(
      onPressed: onTap,
      icon: Icon(ok ? Icons.check_circle : icon, size: 18),
      label: Text(label, style: const TextStyle(fontSize: 12)),
    );
  }

  Widget _legend(Color c, String t) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Container(
        width: 12,
        height: 8,
        decoration: BoxDecoration(
          color: c,
          borderRadius: BorderRadius.circular(2),
        ),
      ),
      const SizedBox(width: 6),
      Text(t, style: const TextStyle(fontSize: 10.5)),
    ],
  );

  Widget _inspector(MapGrid g, MapResult? res) {
    final ri = _selRi!, ci = _selCi!;
    final v = g.data[ri][ci];
    final d = res?.delta[ri][ci] ?? 0;
    final inf = res?.info[ri][ci];
    final col = d < 0
        ? const Color(0xFFFF5C5C)
        : d > 0
        ? const Color(0xFFA8FF3E)
        : null;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'Ячейка: ${g.x.name}=${g.x.values[ci]} · ${g.y.name}=${g.y.values[ri]}',
              style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
            ),
            const SizedBox(height: 4),
            Row(
              children: [
                Text(
                  v.toStringAsFixed(2),
                  style: const TextStyle(
                    fontSize: 18,
                    fontFamily: 'monospace',
                    decoration: TextDecoration.lineThrough,
                  ),
                ),
                const SizedBox(width: 8),
                const Icon(Icons.arrow_forward, size: 16),
                const SizedBox(width: 8),
                Text(
                  '${(v + d).toStringAsFixed(2)} ${g.units}',
                  style: TextStyle(
                    fontSize: 22,
                    fontFamily: 'monospace',
                    color: col,
                  ),
                ),
              ],
            ),
            if (inf != null && inf.why.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(inf.why, style: const TextStyle(fontSize: 12)),
              ),
          ],
        ),
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
          if (widget.extensions.any((x) => name.toLowerCase().endsWith(x)))
            files.add(e);
        }
      }
      dirs.sort((a, b) => a.path.toLowerCase().compareTo(b.path.toLowerCase()));
      files.sort(
        (a, b) => b.lastModifiedSync().compareTo(a.lastModifiedSync()),
      );
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
                  child: Text(
                    widget.title,
                    style: const TextStyle(
                      fontWeight: FontWeight.bold,
                      fontSize: 16,
                    ),
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.close),
                  onPressed: () => Navigator.of(context).pop(),
                ),
              ],
            ),
          ),
          if (!_hasPermission)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 12),
              child: TextButton(
                onPressed: _requestPermission,
                child: const Text('РАЗРЕШИТЬ доступ к файлам'),
              ),
            ),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
            child: Row(
              children: [
                _chip('Память', '/storage/emulated/0'),
                _chip('Downloads', '/storage/emulated/0/Download'),
                _chip('Telegram', '/storage/emulated/0/Telegram'),
                _chip('Documents', '/storage/emulated/0/Documents'),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            color: Colors.black26,
            child: Row(
              children: [
                IconButton(
                  icon: const Icon(Icons.arrow_upward, size: 18),
                  onPressed: _goUp,
                ),
                Expanded(
                  child: Text(
                    _currentDir.path,
                    style: const TextStyle(
                      fontFamily: 'monospace',
                      fontSize: 11,
                      color: Colors.cyanAccent,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                if (widget.allowPickFolderDefs)
                  FilledButton.tonal(
                    onPressed: () => Navigator.of(context).pop(_currentDir),
                    child: const Text(
                      'Взять XML из ЭТОЙ папки',
                      style: TextStyle(fontSize: 11),
                    ),
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
                    leading: const Icon(
                      Icons.insert_drive_file,
                      color: Colors.lightBlueAccent,
                    ),
                    title: Text(
                      name,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 13,
                      ),
                    ),
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
  const _Table2D({
    required this.grid,
    this.result,
    this.selRi,
    this.selCi,
    this.onCell,
  });
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
                TableRow(
                  children: [
                    _head('${g.y.guess}↓ ${g.x.guess}→'),
                    for (final v in g.x.values)
                      _head(v.toStringAsFixed(0), accent: true),
                  ],
                ),
                for (var ri = 0; ri < g.rows; ri++)
                  TableRow(
                    children: [
                      _head(g.y.values[ri].toStringAsFixed(2), accent: true),
                      for (var ci = 0; ci < g.cols; ci++) _cell(ri, ci),
                    ],
                  ),
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
          child: Text(
            'pinch / кнопки = зум',
            style: TextStyle(fontSize: 10, color: Colors.white38),
          ),
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
    child: Text(
      t,
      style: TextStyle(
        fontSize: 9.5,
        fontFamily: 'monospace',
        color: accent ? const Color(0xCC4BE1FF) : Colors.white54,
      ),
    ),
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
            Text(
              v.toStringAsFixed(2),
              style: const TextStyle(
                fontSize: 10,
                fontFamily: 'monospace',
                color: Colors.white,
              ),
            ),
            if (hasFix)
              Text(
                (v + d).toStringAsFixed(2),
                style: const TextStyle(
                  fontSize: 11,
                  fontWeight: FontWeight.bold,
                  fontFamily: 'monospace',
                  color: Colors.lightBlueAccent,
                ),
              ),
          ],
        ),
      ),
    );
  }
}
'''

EXPECTED_MARKERS = {
    "android/app/src/main/kotlin/com/subaru/ssm2_fixed/MainActivity.kt": "spp-reader",
    "lib/elm.dart": "_consecutiveTimeouts",
    "lib/engine.dart": "tripDistanceKm",
    "lib/analyzer.dart": "iamScaled",
    "lib/native_spp.dart": "TimeoutException",
    "lib/model.dart": "_stepMs",
    "lib/main.dart": "SSM2 TELEMETRY 0.10.2",
    "test/protocol_test.dart": "ELM-01 retry",
    "lib/maps_lab/maps_lab_log.dart": "loadIsPercent",
    "lib/maps_lab/maps_lab_page.dart": "_loadLatestLog",
    "test/maps_lab_test.dart": "Analyzer: процентная",
}


def main():
    if not (APP / "lib/elm.dart").exists():
        raise RuntimeError("Проект не найден. Сначала выполните ячейки 01-04.")
    if not (APP / "lib/maps_lab/maps_lab_page.dart").exists():
        raise RuntimeError("Не найден Map Lab. Сначала выполните ячейку 04.")

    changed, skipped = [], []
    targets = {rel: body.strip("\n") + "\n" for rel, body in FILES.items()}

    todo = {rel: body for rel, body in targets.items()
            if (APP / rel).read_text(encoding="utf-8") != body}
    if todo:
        backup = APP.parent / f"ssm2_backup_0_10_2_{time.strftime('%Y%m%d_%H%M%S')}"
        for rel in todo:
            dst = backup / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(APP / rel, dst)
        print(f"[бэкап] {len(todo)} файлов -> {backup}")

    for rel, body in targets.items():
        path = APP / rel
        if path.read_text(encoding="utf-8") == body:
            skipped.append(rel)
        else:
            path.write_text(body, encoding="utf-8")
            changed.append(rel)
            print("[fix]", rel)

    problems = [f"{rel}: нет маркера {mk!r}" for rel, mk in EXPECTED_MARKERS.items()
                if mk not in (APP / rel).read_text(encoding="utf-8")]
    if problems:
        raise RuntimeError("Самопроверка:\n  - " + "\n  - ".join(problems))

    pub = APP / "pubspec.yaml"
    pub.write_text(re.sub(r"(?m)^version: .+$", "version: 0.10.2+14",
                          pub.read_text(encoding="utf-8")), encoding="utf-8")
    cfg = APP / "build_config.json"
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["revision"] = "0.10.2"
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"SSM2 0.10.2 | изменено: {len(changed)} · пропущено: {len(skipped)} · маркеры: OK")
    print("Новое в 0.10.2:")
    print("  - Автозонд endian для 4-byte PID (IAM): если BE = 0, пробует LE")
    print("  - Trip-одометр + потраченное топливо + л/100км + кнопка «Сброс»")
    print("  - Raw-байты на карточках PID (для диагностики нулей)")
    print("Дальше: ячейка 05 (26 тестов, APK 0.10.2).")


main()
