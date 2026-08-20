#!/usr/bin/env python3
"""Single Windows-oriented launcher/menu for RU Max Clean 4.9.1."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "RU-Max-Clean"
SOURCES = BASE / "sources"
STATE = BASE / ".ru_max_build_state.json"
VERSION = "4.9.1"


def header() -> None:
    print("=" * 68)
    print(" RU Max Clean 4.9.1 TURBO READER LAYERS QUALITY")
    print("=" * 68)
    print("Офлайн-словарь русского языка для KOReader: только значения,")
    print("миллионы словоформ, редкая/старая и научно-профессиональная лексика.")
    print("Лаунчер сам проверяет источники, окружение и валидирует StarDict.")
    print()


def hardware_summary() -> str:
    cpu = os.cpu_count() or 1
    ram_gib = 0.0
    try:
        import psutil
        ram_gib = psutil.virtual_memory().total / 1073741824
    except Exception:
        pass
    disk = shutil.disk_usage(BASE).free / 1073741824
    ram = f" | RAM {ram_gib:.1f} GiB" if ram_gib else ""
    return f"CPU threads {cpu}{ram} | free disk {disk:.1f} GiB"


def turbo_env() -> dict[str, str]:
    env = os.environ.copy()
    cpu = max(1, os.cpu_count() or 1)
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["RU_MAX_PROGRESS"] = "1"
    env.setdefault("RU_MAX_DOWNLOAD_WORKERS", str(max(2, min(4, cpu // 2 or 2))))
    return env


def set_above_normal_priority() -> None:
    try:
        import psutil
        if os.name == "nt":
            psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def run_stream(cmd: list[str], *, log_path: Path | None = None, title: str = "") -> int:
    if title:
        print(f"\n--- {title} ---")
    log = log_path.open("a", encoding="utf-8", errors="replace") if log_path else None
    if log:
        log.write("\n--- " + title + " ---\n")
        log.write("Command: " + " ".join(cmd) + "\n")
        log.flush()
    p = subprocess.Popen(
        cmd, cwd=BASE, env=turbo_env(), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=0,
    )
    assert p.stdout is not None
    # Reading one character directly from the pipe makes the launcher look dead
    # while a large parser is busy and provides no way to show a heartbeat.  A
    # tiny reader thread keeps the UI responsive without changing child output.
    chunks: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                ch = p.stdout.read(1)
                if ch == "":
                    break
                chunks.put(ch)
        finally:
            chunks.put(None)

    threading.Thread(target=_reader, name="ru-max-output", daemon=True).start()
    buf = ""
    progress_mode = False
    last_output = time.monotonic()
    heartbeat = 15.0
    while True:
        try:
            ch = chunks.get(timeout=heartbeat)
        except queue.Empty:
            elapsed = int(time.monotonic() - last_output)
            print(f"\n[ОЖИДАНИЕ] {title or 'операция'} выполняется ({elapsed} с без нового вывода)...", flush=True)
            continue
        if ch is None:
            break
        last_output = time.monotonic()
        if ch == "\r":
            if buf:
                sys.stdout.write("\r" + buf)
                sys.stdout.flush()
            buf = ""
            progress_mode = True
        elif ch == "\n":
            if progress_mode:
                if buf:
                    sys.stdout.write("\r" + buf)
                sys.stdout.write("\n")
                sys.stdout.flush()
                buf = ""
                progress_mode = False
            else:
                print(buf, flush=True)
                if log:
                    log.write(buf + "\n")
                    log.flush()
                buf = ""
        else:
            buf += ch
    if buf:
        print(buf)
        if log and not progress_mode:
            log.write(buf + "\n")
    rc = p.wait()
    if log:
        log.write(f"Exit code: {rc}\n")
        log.close()
    return rc


def local_build_fingerprint() -> str:
    h = hashlib.sha256()
    files = [BASE / "build_ru_max_clean.py", BASE / "source_manager.py", BASE / "stage_cache.py", BASE / "progress_ui.py", BASE / "human_report.py", BASE / "ru_max_launcher.py"]
    extras = BASE / "extras"
    if extras.exists():
        files.extend(sorted(p for p in extras.rglob("*") if p.is_file()))
    for path in files:
        if not path.exists():
            continue
        h.update(str(path.relative_to(BASE)).encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def fingerprints(manifest: dict) -> dict[str, dict[str, object]]:
    fields = ("url", "etag", "last_modified", "content_length", "sha256", "local_size")
    out: dict[str, dict[str, object]] = {}
    for name, meta in manifest.items():
        if isinstance(meta, dict):
            out[name] = {k: meta.get(k) for k in fields if meta.get(k) is not None}
    return out


def read_manifest() -> dict:
    try:
        return json.loads((SOURCES / "source_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(profile: str) -> None:
    data = {
        "version": VERSION,
        "profile": profile,
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": fingerprints(read_manifest()),
        "local_build_fingerprint": local_build_fingerprint(),
    }
    STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_files_exist() -> bool:
    return all((OUT / f"ru-max-clean.{ext}").exists() for ext in ("ifo", "idx", "dict"))


def is_current(profile: str) -> bool:
    st = read_state()
    return (
        build_files_exist()
        and st.get("version") == VERSION
        and st.get("profile") == profile
        and st.get("sources") == fingerprints(read_manifest())
        and st.get("local_build_fingerprint") == local_build_fingerprint()
    )


def bootstrap(offline: bool = False) -> int:
    cmd = [sys.executable, "bootstrap.py"]
    if offline:
        cmd.append("--offline")
    return subprocess.call(cmd, cwd=BASE, env=turbo_env())


def check_sources(profile: str = "max", force_refresh: bool = False) -> int:
    cmd = [sys.executable, "check_sources.py", "--profile", profile]
    if force_refresh:
        cmd.append("--force-refresh")
    return run_stream(cmd, title=f"Проверка/обновление источников ({profile})")


def validate(log_path: Path | None = None) -> int:
    if not build_files_exist():
        print("Словарь RU-Max-Clean ещё не собран.")
        return 11
    return run_stream(
        [sys.executable, "validate_stardict.py", str(OUT / "ru-max-clean")],
        log_path=log_path, title="Проверка StarDict",
    )


def build(
    profile: str,
    *,
    offline: bool = False,
    smart: bool = True,
    rebuild_stage_cache: bool = False,
    quick: bool = False,
) -> int:
    # Quick rebuilds must never contact the network.  The old menu item 6 called
    # the online update pass first, which both made it appear to hang and could
    # trigger rate limits before the cache-only rebuild even started.
    cache_only = offline or quick
    if bootstrap(offline=cache_only):
        return 10
    set_above_normal_priority()
    log_path = BASE / ("build.log" if profile == "max" else "build_lexical.log")
    log_path.write_text(
        f"RU Max Clean {VERSION} {profile} build log\nStarted: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n{hardware_summary()}\n",
        encoding="utf-8",
    )

    # Online smart build: update sources first, then avoid hours of work when both
    # source fingerprints and builder/profile are unchanged.
    if not cache_only:
        rc = check_sources(profile)
        if rc:
            print("Проверка источников завершилась с ошибкой; существующий кэш не удалён.")
        if smart and is_current(profile):
            print("\n[SMART BUILD] Источники и версия сборщика не изменились.")
            print("Полная пересборка не требуется; выполняется только валидация.")
            return validate(log_path)

    args = [sys.executable, "build_ru_max_clean.py"]
    # If the primary Kaikki dump is present, let the normal signature-aware
    # cache pipeline run: this applies new semantic code to old max caches and
    # restores a matching artifact when possible.  A machine that retained only
    # the final artifact (a supported low-disk setup) uses the direct restore.
    artifact_only = quick and not (SOURCES / "raw-wiktextract-data.jsonl.gz").exists()
    if artifact_only:
        args += ["--restore-artifact"]
    else:
        # Online builds already performed the update pass above. Reuse that exact
        # cache snapshot without issuing the same HEAD requests a second time.
        args += ["--offline"]
        args += ["--download-kaikki", "--download-opencorpora", "--download-wikidata-lexemes", "--download-dal"]
        if profile == "max":
            args += ["--download-wikipedia", "--wikipedia-quality-upgrade"]
    # Offline mode naturally uses the same cache-only path; artifact-only quick
    # mode has no raw source arguments by design.
    if rebuild_stage_cache:
        args += ["--rebuild-stage-cache"]
    args += ["--output-dir", "RU-Max-Clean"]
    rc = run_stream(args, log_path=log_path, title="Сборка словаря")
    if rc:
        return rc
    rc = validate(log_path)
    if rc == 0:
        save_state(profile)
        print(f"\nГОТОВО: {OUT}")
    return rc


def diagnostics() -> int:
    header()
    print(hardware_summary())
    subprocess.call([sys.executable, "bootstrap.py", "--check-only"], cwd=BASE)
    print(f"Sources: {SOURCES}")
    print(f"Dictionary: {OUT}")
    print(f"Cached source files: {sum(1 for p in SOURCES.glob('*') if p.is_file()) if SOURCES.exists() else 0}")
    stage_dir = SOURCES / "stage-cache"
    if stage_dir.exists():
        stage_files = sorted(stage_dir.glob("*.sqlite3"))
        print(f"Parsed stage caches: {len(stage_files)}")
        for p in stage_files:
            print(f"  {p.name}: {p.stat().st_size / (1024**2):.1f} MiB")
    else:
        print("Parsed stage caches: none yet")
    artifact_dir = SOURCES / "artifact-cache"
    if artifact_dir.exists():
        artifacts = sorted(p for p in artifact_dir.iterdir() if p.is_dir())
        print(f"StarDict artifact caches: {len(artifacts)}")
        for d in artifacts:
            total = sum(p.stat().st_size for p in d.iterdir() if p.is_file())
            print(f"  {d.name}: {total / (1024**2):.1f} MiB")
    else:
        print("StarDict artifact caches: none yet")
    return 0


def menu() -> int:
    while True:
        if os.name == "nt":
            os.system("cls")
        elif os.environ.get("TERM"):
            os.system("clear")
        header()
        print(hardware_summary())
        print()
        print("  1. Умная MAX-сборка (обновления + Wikipedia + Quality)")
        print("  2. Лексическая сборка (без 6-ГБ Wikipedia)")
        print("  3. Офлайн MAX-сборка (только уже скачанные источники)")
        print("  4. Только проверить и скачать обновления источников")
        print("  5. Проверить уже собранный словарь")
        print("  6. Быстрая MAX-пересборка из кэшей (включая готовый StarDict)")
        print("  7. Диагностика компонентов, железа и кэшей")
        print("  8. Полностью пересоздать кэши этапов (медленно)")
        print("  9. Тест скорости gzip/bzip2 на ваших исходниках")
        print("  0. Выход")
        choice = input("\nВыбор: ").strip()
        actions = {
            "1": lambda: build("max", smart=True),
            "2": lambda: build("lexical", smart=True),
            "3": lambda: build("max", offline=True, smart=False),
            "4": check_sources,
            "5": validate,
            "6": lambda: build("max", smart=False, quick=True),
            "7": diagnostics,
            "8": lambda: build("max", smart=False, rebuild_stage_cache=True),
            "9": lambda: subprocess.call([sys.executable, "benchmark_compression.py"], cwd=BASE, env=turbo_env()),
        }
        if choice == "0":
            return 0
        fn = actions.get(choice)
        if not fn:
            continue
        rc = fn()
        print(f"\nКод завершения: {rc}")
        input("Нажмите Enter для возврата в меню...")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", choices=["max", "lexical", "offline", "quick", "updates", "validate", "force", "diagnostics", "rebuild-caches", "benchmark"])
    args = ap.parse_args()
    if not args.action:
        return menu()
    return {
        "max": lambda: build("max", smart=True),
        "lexical": lambda: build("lexical", smart=True),
        "offline": lambda: build("max", offline=True, smart=False),
        "quick": lambda: build("max", smart=False, quick=True),
        "updates": check_sources,
        "validate": validate,
        "force": lambda: build("max", smart=False),
        "diagnostics": diagnostics,
        "rebuild-caches": lambda: build("max", smart=False, rebuild_stage_cache=True),
        "benchmark": lambda: subprocess.call([sys.executable, "benchmark_compression.py"], cwd=BASE, env=turbo_env()),
    }[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
