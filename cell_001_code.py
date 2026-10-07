# @title 01 | SSM2 1.0 — Окружение: Flutter 3.35.4 + AGP 8.11.1 + Gradle 8.14.3 + Kotlin 2.2.20 { display-mode: "form" }
# v0.8: окружение без изменений (Flutter 3.35.4 + AGP 8.11.1 + Gradle 8.14.3 + Kotlin 2.2.20).
# Комплект 0.8: 02 — нативный SPP без вендоринга pub-плагинов + реальный анализатор логов;
#               03/04 — Map Lab без deprecated-вызовов; 05 — dart fix, строгий анализ, подпись APK.
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

# Пусто: сохранить установленный Flutter; для новой среды взять 3.35.4.
# Можно указать точную stable-версию. Минимумы SDK проверяются ниже.
FLUTTER_VERSION = ""  # @param {type:"string"}
SDK = Path("/content/android-sdk")
FLUTTER = Path("/content/flutter")
JAVA = Path("/usr/lib/jvm/java-17-openjdk-amd64")
CONFIG = Path("/content/ssm2_fixed_env.json")
VERSIONS = {
    "agp": "8.11.1", "gradle": "8.14.3", "kotlin": "2.2.20",
    "compile_sdk": 36, "target_sdk": 35, "min_sdk": 24,
    "build_tools": "35.0.0", "ndk": "27.0.12077973",
}


def run(args, timeout=1800, input_text=None):
    print("\n>", " ".join(map(str, args)))
    p = subprocess.run(list(map(str, args)), text=True, input=input_text,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout)
    print(p.stdout[-5000:])
    if p.returncode:
        raise RuntimeError(f"Команда завершилась с кодом {p.returncode}")
    return p.stdout


def download(url, dest, sha256=None):
    dest = Path(dest)
    if not dest.exists():
        partial = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(url, timeout=180) as src, partial.open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        partial.replace(dest)
    if sha256:
        digest = hashlib.sha256()
        with dest.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            dest.unlink()
            raise RuntimeError("SHA256 архива не совпал; повторите загрузку")


def version_tuple(value):
    return tuple(int(x) for x in value.split("."))


t0 = time.monotonic()
print("SSM2 1.0 | Окружение. Старый проект и общие кэши не удаляются.")
run(["apt-get", "update", "-qq"], timeout=600)
run(["apt-get", "install", "-y", "-qq", "openjdk-17-jdk-headless",
     "xz-utils", "unzip", "zip", "curl", "git"], timeout=1200)
os.environ.update(JAVA_HOME=str(JAVA), ANDROID_HOME=str(SDK), ANDROID_SDK_ROOT=str(SDK))
paths = [str(JAVA / "bin"), str(FLUTTER / "bin"),
         str(SDK / "cmdline-tools/latest/bin"), str(SDK / "platform-tools")]
os.environ["PATH"] = os.pathsep.join(paths + [os.environ.get("PATH", "")])
run([JAVA / "bin/java", "-version"])

manager = SDK / "cmdline-tools/latest/bin/sdkmanager"
if not manager.exists():
    archive = Path("/content/android-command-tools.zip")
    download("https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip", archive)
    stage = Path("/content/ssm2_cmdline_unpack")
    shutil.rmtree(stage, ignore_errors=True)
    run(["unzip", "-q", "-o", archive, "-d", stage])
    manager.parent.parent.parent.mkdir(parents=True, exist_ok=True)
    target = SDK / "cmdline-tools/latest"
    if target.exists():
        target.rename(target.with_name(f"previous-{int(time.time())}"))
    shutil.move(str(stage / "cmdline-tools"), str(target))
run([manager, f"--sdk_root={SDK}", "--licenses"], timeout=900, input_text="y\n" * 150)
run([manager, f"--sdk_root={SDK}", "platform-tools", "platforms;android-36",
     "platforms;android-35", "build-tools;35.0.0", "ndk;27.0.12077973"], timeout=3600)

requested = FLUTTER_VERSION.strip()
if requested or not (FLUTTER / "bin/flutter").exists():
    chosen = requested or "3.35.4"
    with urllib.request.urlopen("https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json", timeout=60) as response:
        releases = json.load(response)
    release = next((r for r in releases["releases"]
                    if r["version"] == chosen and r["channel"] == "stable"
                    and r.get("dart_sdk_arch", "x64") == "x64"), None)
    if release is None:
        raise RuntimeError(f"Stable Flutter {chosen} для Linux x64 не найден")
    existing = ""
    if (FLUTTER / "bin/flutter").exists():
        run(["git", "config", "--global", "--add", "safe.directory", FLUTTER])
        existing = run([FLUTTER / "bin/flutter", "--version"])
    if not re.search(rf"Flutter\s+{re.escape(chosen)}\b", existing):
        archive = Path(f"/content/flutter-{chosen}.tar.xz")
        download(releases["base_url"] + "/" + release["archive"], archive, release["sha256"])
        stage = Path("/content/ssm2_flutter_unpack")
        stage.mkdir(exist_ok=True)
        run(["tar", "-xJf", archive, "-C", stage], timeout=2400)
        if FLUTTER.exists():
            FLUTTER.rename(Path(f"/content/flutter-backup-{int(time.time())}"))
        shutil.move(str(stage / "flutter"), str(FLUTTER))

run(["git", "config", "--global", "--add", "safe.directory", FLUTTER])
flutter_info = run([FLUTTER / "bin/flutter", "--version", "--machine"])
info = json.loads(flutter_info[flutter_info.index("{"):])
run([FLUTTER / "bin/flutter", "config", "--no-analytics"])
run([FLUTTER / "bin/flutter", "config", f"--android-sdk={SDK}", f"--jdk-dir={JAVA}"])

# Check the INSTALLED SDK, not an assumed Flutter version or a broad log match.
checker = FLUTTER / "packages/flutter_tools/gradle/src/main/kotlin/DependencyVersionChecker.kt"
if checker.exists():
    source = checker.read_text(encoding="utf-8")
    for name, key in [("errorAGPVersion", "agp"), ("errorGradleVersion", "gradle"), ("errorKGPVersion", "kotlin")]:
        match = re.search(rf"{name}\s*[^=]*=\s*(?:AndroidPluginVersion|Version)\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", source)
        if match:
            required = ".".join(match.groups())
            print(f"Flutter minimum {key}: {required}; выбрано: {VERSIONS[key]}")
            if version_tuple(VERSIONS[key]) < version_tuple(required):
                raise RuntimeError(f"Flutter {info['frameworkVersion']} требует {key} >= {required}. "
                                   "Этот набор версий не подходит. Укажите FLUTTER_VERSION = '3.35.4' и повторите ячейку. "
                                   "Проверка совместимости намеренно не отключается.")
run([FLUTTER / "bin/flutter", "precache", "--android"], timeout=2400)
required = [JAVA / "bin/java", manager, SDK / "platforms/android-36/android.jar",
            SDK / "build-tools/35.0.0/aapt", SDK / "build-tools/35.0.0/apksigner",
            SDK / "ndk/27.0.12077973/source.properties", FLUTTER / "bin/dart"]
for file in required:
    if not file.exists():
        raise RuntimeError(f"Не найден обязательный файл: {file}")
    print("[OK]", file)
config = {**VERSIONS, "sdk": str(SDK), "java": str(JAVA), "flutter": str(FLUTTER),
          "flutter_version": info["frameworkVersion"], "app": "/content/subaru_ssm2_fixed"}
CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
print(f"\nОкружение подготовлено за {time.monotonic() - t0:.0f} с. Конфигурация: {CONFIG}")
print("Далее выполните ячейку 2. Настоящая проверка Dart и APK будет в ячейке 3.")