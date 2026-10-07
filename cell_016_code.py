# @title 05 | SSM2 0.9 - guard зависимостей, строгий анализ, тесты, подпись APK { display-mode: "form" }
# v0.8 changelog:
#  - prepare_bt.py и вендоринг pub-плагинов УДАЛЕНЫ: транспорт — нативный RFCOMM-канал.
#  - Ячейка "автофикс импортов" удалена: вместо неё штатный `dart fix --apply`.
#  - flutter analyze снова строгий: предупреждения = ошибка сборки.
#  - Поддержка настоящей подписи release APK: keystore в base64 (параметр или Colab Secret
#    SSM2_KEYSTORE_B64) + apksigner --print-certs (SHA-256 сертификата идёт в отчёт).
#  - HARDWARE_CONFIRMED: чеклист стендовой проверки фиксируется в report.json.

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

BUILD_VARIANT = "release"  # @param ["release", "debug", "profile"]
CLEAN_BEFORE = True  # @param {type:"boolean"}
STRICT_ANALYZE = True  # @param {type:"boolean"}
HARDWARE_CONFIRMED = False  # @param {type:"boolean"}

# --- Подпись: заполните параметры ИЛИ создайте Colab Secrets с теми же именами ---
KEYSTORE_B64 = ""  # @param {type:"string"}
KEYSTORE_PASSWORD = ""  # @param {type:"string"}
KEY_ALIAS = "ssm2"  # @param {type:"string"}
KEY_PASSWORD = ""  # @param {type:"string"}


def colab_secret(name):
    """Секреты Colab (иконка ключа слева). Вне Colab — просто пусто."""
    try:
        from google.colab import userdata
        value = userdata.get(name)
        return value or ""
    except Exception:
        return ""


# --- 0. Очистка предыдущих сборок ---
print("=== 0. Очистка предыдущих сборок ===")
content_dir = Path("/content")
for old_apk in content_dir.glob("ssm2_fixed_*.apk"):
    try:
        old_apk.unlink()
        print(f"[Удалён старый APK] {old_apk.name}")
    except Exception as e:
        print(f"[Ошибка удаления {old_apk.name}]: {e}")
for old_item in content_dir.glob("ssm2_build_*"):
    try:
        if old_item.is_dir():
            shutil.rmtree(old_item, ignore_errors=True)
        else:
            old_item.unlink(missing_ok=True)
        print(f"[Удалён старый отчёт] {old_item.name}")
    except Exception as e:
        print(f"[Ошибка очистки {old_item.name}]: {e}")

APP = Path("/content/subaru_ssm2_fixed")
CONFIG = APP / "build_config.json"
if not CONFIG.exists():
    raise RuntimeError("Не найден проект. Выполните ячейки 01 и 02.")
cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
FLUTTER = Path(cfg["flutter"])
SDK = Path(cfg["sdk"])
JAVA = Path(cfg["java"])
os.environ.update(JAVA_HOME=str(JAVA), ANDROID_HOME=str(SDK), ANDROID_SDK_ROOT=str(SDK))
os.environ["PATH"] = os.pathsep.join([
    str(JAVA / "bin"), str(FLUTTER / "bin"),
    str(SDK / "cmdline-tools/latest/bin"), os.environ.get("PATH", "")])
stamp = time.strftime("%Y%m%d_%H%M%S")
REPORT = Path(f"/content/ssm2_build_{stamp}")
REPORT.mkdir(parents=True, exist_ok=True)
report = {"revision": "0.9.2", "transport": cfg.get("bt_package", "native_spp"),
          "variant": BUILD_VARIANT, "flutter": cfg.get("flutter_version", "?"),
          "stages": {}, "signing": "debug"} | {"hardware_tested": HARDWARE_CONFIRMED}


def save_report():
    (REPORT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def stage(name, args, timeout=3600):
    logfile = REPORT / f"{name}.log"
    print(f"\n=== {name} ===\n> {' '.join(map(str, args))}\nЖурнал: {logfile}")
    started = time.monotonic()
    try:
        with logfile.open("w", encoding="utf-8") as out:
            p = subprocess.run(list(map(str, args)), cwd=APP, text=True,
                               stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
        text = logfile.read_text(encoding="utf-8", errors="replace")
        print(text[-7000:])
        report["stages"][name] = {"exit_code": p.returncode,
                                  "seconds": round(time.monotonic() - started, 1)}
        save_report()
        if p.returncode:
            raise RuntimeError(f"Этап {name} не пройден. Полный журнал: {logfile}")
        return text
    except subprocess.TimeoutExpired:
        report["stages"][name] = {"error": "timeout"}
        save_report()
        raise RuntimeError(f"Таймаут этапа {name}. Журнал: {logfile}")


print("SSM2 0.9.2 | Проверка перед сборкой")
required = [
    FLUTTER / "bin/flutter", FLUTTER / "bin/dart", JAVA / "bin/java",
    SDK / "platforms/android-36/android.jar", SDK / "build-tools/35.0.0/aapt",
    SDK / "build-tools/35.0.0/apksigner", SDK / "ndk/27.0.12077973/source.properties",
    APP / "lib/native_spp.dart", APP / "lib/transport_selected.dart",
    APP / "lib/analyzer.dart", APP / "lib/derived.dart", APP / "lib/identity.dart",
    APP / "lib/elm.dart", APP / "lib/engine.dart", APP / "lib/pids.dart",
    APP / "lib/maplab_link.dart", APP / "lib/maps_lab/maps_lab_page.dart",
    APP / "test/protocol_test.dart", APP / "test/maps_lab_test.dart",
    APP / "tool/calid_import.py", APP / "android/app/build.gradle.kts"]
for file in required:
    if not file.exists():
        raise RuntimeError(f"Отсутствует: {file}. Перегенерируйте проект ячейкой 02.")
    print("[OK]", file)

version_log = stage("flutter_version",
                    [FLUTTER / "bin/flutter", "--version", "--machine"], timeout=600)
actual = json.JSONDecoder().raw_decode(version_log[version_log.index("{"):])[0]
report["flutter"] = actual["frameworkVersion"]
save_report()

# --- 1. Подпись release APK ---
print("\n=== signing_setup ===")
keystore_b64 = KEYSTORE_B64.strip() or colab_secret("SSM2_KEYSTORE_B64").strip()
store_password = KEYSTORE_PASSWORD or colab_secret("SSM2_STORE_PASSWORD")
key_password = KEY_PASSWORD or colab_secret("SSM2_KEY_PASSWORD")
key_alias = (KEY_ALIAS or colab_secret("SSM2_KEY_ALIAS") or "ssm2").strip()
key_props = APP / "android/key.properties"
keystore_file = APP / "android/keystore_upload.jks"
key_props.unlink(missing_ok=True)
if keystore_b64:
    keystore_file.write_bytes(base64.b64decode(keystore_b64))
    key_props.write_text(
        f"storeFile=keystore_upload.jks\nstorePassword={store_password}\n"
        f"keyAlias={key_alias}\nkeyPassword={key_password}\n", encoding="utf-8")
    report["signing"] = "upload"
    print(f"[OK] Upload-keystore готов ({keystore_file.stat().st_size} байт), alias={key_alias}")
else:
    print("!!! ВНИМАНИЕ: keystore не задан — release APK будет подписан DEBUG-ключом.")
    print("!!! Прошивки/обновления это не сломает, но публикации и смены ключа не переживёт.")
    print("!!! Как сделать ключ: keytool -genkey -v -keystore ssm2.jks -alias ssm2 \\")
    print("!!!     -keyalg RSA -keysize 2048 -validity 10000 && base64 -w0 ssm2.jks")
    # v0.8.1: свежий Colab-рантайм может не иметь ~/.android/debug.keystore
    # (validateSigningRelease падает, если файла нет) — создаём сами стандартными параметрами.
    debug_keystore = Path.home() / ".android/debug.keystore"
    if debug_keystore.exists():
        print(f"[OK] Найден debug.keystore: {debug_keystore}")
    else:
        debug_keystore.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run([
            str(JAVA / "bin/keytool"), "-genkeypair", "-v",
            "-keystore", str(debug_keystore),
            "-storepass", "android", "-keypass", "android",
            "-alias", "androiddebugkey",
            "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
            "-dname", "CN=Android Debug,O=Android,C=US"],
            capture_output=True, text=True, timeout=300)
        if p.returncode or not debug_keystore.exists():
            raise RuntimeError(f"Не удалось создать debug.keystore: {p.stdout[-1500:]}")
        print(f"[OK] Создан debug.keystore: {debug_keystore}")
save_report()

properties_file = APP / "android/local.properties"
properties = {}
if properties_file.exists():
    for line in properties_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            properties[key.strip()] = value
properties.update({"sdk.dir": str(SDK), "flutter.sdk": str(FLUTTER)})
properties_file.write_text(
    "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n", encoding="utf-8")
gradlew = APP / "android/gradlew"
gradlew.chmod(gradlew.stat().st_mode | 0o111)

DART = FLUTTER / "bin/dart"
FLUTTER_BIN = FLUTTER / "bin/flutter"

if CLEAN_BEFORE:
    stage("clean", [FLUTTER_BIN, "clean"], timeout=600)

# --- v0.9: pubspec guard. Проверяем sdk-ограничения ДО pub get, чтобы не ловить
# "requires SDK version >=3.10" уже в середине сборки.
print("\n=== pubspec_guard ===")
dart_version = actual.get("dartSdkVersion", "0.0.0")


def _tuple(v):
    parts = re.findall(r"\d+", v)[:3]
    return tuple(int(x) for x in parts) + (0,) * (3 - len(parts))


def _min_sdk(constraint):
    m = re.search(r"\^\s*([\d.]+)", constraint) or re.search(r">=\s*([\d.]+)", constraint)
    return m.group(1) if m else "0.0.0"


pinned = {}
for line in (APP / "pubspec.yaml").read_text(encoding="utf-8").splitlines():
    m = re.match(r"^  ([a-z_0-9]+):\s*([\d][\w.+-]*)\s*$", line)
    if m:
        pinned[m.group(1)] = m.group(2)

violations = []
print(f"  Dart SDK рантайма: {dart_version}")
for name, version in sorted(pinned.items()):
    try:
        import urllib.request
        url = f"https://pub.dev/api/packages/{name}/versions/{version}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
        constraint = meta.get("pubspec", {}).get("environment", {}).get("sdk", "?")
        need = _min_sdk(constraint)
        ok = _tuple(dart_version) >= _tuple(need)
        print(f"  {'OK ' if ok else 'НЕТ'} {name} {version} -> Dart {constraint}")
        if not ok:
            violations.append(f"{name} {version} требует Dart {constraint}, есть {dart_version}")
    except Exception as e:
        print(f"  ??  {name} {version}: проверка недоступна ({e})")
report["pubspec_guard"] = {"dart": dart_version, "violations": violations}
save_report()
if violations:
    raise RuntimeError("Несовместимые зависимости:\n  - " + "\n  - ".join(violations))

stage("pub_get", [FLUTTER_BIN, "pub", "get"], timeout=1200)

# v0.8: штатный автофикс вместо regex-вырезания импортов
stage("dart_fix", [DART, "fix", "--apply"], timeout=900)
stage("format", [DART, "format", "lib", "test"], timeout=600)

# v0.8: строгий анализ — предупреждения = провал этапа (infos не фатальны)
analyze_args = [FLUTTER_BIN, "analyze", "--no-pub"]
if not STRICT_ANALYZE:
    analyze_args += ["--no-fatal-infos", "--no-fatal-warnings"]
else:
    analyze_args += ["--no-fatal-infos"]
stage("analyze", analyze_args, timeout=1200)

stage("tests", [FLUTTER_BIN, "test", "--no-pub", "--reporter", "expanded"], timeout=1200)

apk = APP / f"build/app/outputs/flutter-apk/app-{BUILD_VARIANT}.apk"
apk.unlink(missing_ok=True)
stage("build", [FLUTTER_BIN, "build", "apk", f"--{BUILD_VARIANT}", "--no-pub"], timeout=4800)
if not apk.exists() or apk.stat().st_size < 1024 * 1024:
    raise RuntimeError(f"Новый APK не найден: {apk}")

verify_log = stage("apk_signature",
                   [SDK / "build-tools/35.0.0/apksigner", "verify",
                    "--verbose", "--print-certs", apk], timeout=300)
if "Verifies" not in verify_log:
    raise RuntimeError("APK не прошёл проверку подписи")
if BUILD_VARIANT == "release":
    scheme_v2 = re.search(r"v2 scheme.*?: (true|false)", verify_log)
    if not scheme_v2 or scheme_v2.group(1) != "true":
        raise RuntimeError("Нет подписи v2 scheme — APK непригоден для установки")
digest_match = re.search(r"Signer #1 certificate SHA-256 digest: ([0-9a-f]+)", verify_log)
cert_sha256 = digest_match.group(1) if digest_match else None
report["cert_sha256"] = cert_sha256
print(f"[ПОДПИСЬ] {report['signing'].upper()} · cert SHA-256: {cert_sha256}")

digest = hashlib.sha256(apk.read_bytes()).hexdigest()
destination = Path(f"/content/ssm2_fixed_v09_{BUILD_VARIANT}.apk")
shutil.copy2(apk, destination)
report.update({"apk": str(destination), "sha256": digest, "apk_verified": True})

# --- v0.9: манифест воспроизводимости — sha256 всех исходников проекта ---
manifest = {}
for source in sorted(APP.rglob("*")):
    if not source.is_file():
        continue
    rel = source.relative_to(APP).as_posix()
    if rel.startswith(("build/", ".dart_tool/", "android/.gradle/")) or rel.endswith(".jks"):
        continue
    if source.suffix in {".dart", ".kts", ".gradle", ".yaml", ".yml", ".xml", ".kt", ".py", ".json", ".properties"}:
        manifest[rel] = hashlib.sha256(source.read_bytes()).hexdigest()
(REPORT / "sources_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
report["sources"] = {"files": len(manifest),
                     "manifest": str(REPORT / "sources_manifest.json")}
print(f"[манифест] {len(manifest)} исходников · {REPORT / 'sources_manifest.json'}")
save_report()

print(f"\n[OK] Проверки и сборка завершены: {destination}")
print(f"SHA256 APK: {digest}")

print("\n=== Чеклист стендовой проверки (hardware_tested) ===")
print("  [ ] 1. Адаптер сопряжён, подключение ≤ 10 с")
print("  [ ] 2. Базовый набор PID: качество ≥ 95% за 10 мин")
print("  [ ] 3. Ни одного mute-изоляции адресов за сессию")
print("  [ ] 4. Стоп/старт опроса без переподключения адаптера")
print("  [ ] 5. Расширенный набор активен при вводе своего ROM ID (CALID-гейт)")
print(f"Подтверждаете прохождение? Установите HARDWARE_CONFIRMED=True и пересоберите: "
      f"сейчас report.hardware_tested = {HARDWARE_CONFIRMED}")

# --- Автоматическое скачивание готового APK ---
try:
    from google.colab import files
    print(f"\n[АВТО-СКАЧИВАНИЕ] {destination.name} отправлен на ваш ПК/телефон...")
    files.download(str(destination))
except Exception:
    print(f"\nСкачайте файл из боковой панели «Файлы» Colab: {destination.name}")
