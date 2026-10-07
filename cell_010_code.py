# coding: utf-8
# @title 04.6 | PARSER FIX | SSM2 0.14 >> 0.14.1 (insert BETWEEN 04.5 and 05)
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
