#!/usr/bin/env python3
"""Single Windows-oriented launcher/menu for RU Max Clean."""
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

from version_info import BUILDER_VERSION, PUBLIC_VERSION

BASE = Path(__file__).resolve().parent
OUT = BASE / "RU-Max-Clean"
SOURCES = BASE / "sources"
STATE = BASE / ".ru_max_build_state.json"
VERSION = BUILDER_VERSION

READER_PACKS: dict[str, tuple[str, str]] = {
    "1": ("latin_classical.tsv", "Латынь: классические выражения"),
    "2": ("latin_wiktionary.tsv", "Латынь: расширенный слой"),
    "3": ("literary_archaic.tsv", "Архаика и церковнославянская лексика"),
    "4": ("literary_wiktionary.tsv", "Расширенная историческая лексика"),
    "5": ("french_literary.tsv", "Французская лексика в русской прозе"),
    "6": ("literary_names.tsv", "Литературные имена и названия"),
    "7": ("fantasy_terms.tsv", "Фэнтезийные термины"),
    "8": ("literary_terms.tsv", "Историко-культурные термины"),
    "9": ("phraseology.tsv", "Фразеологизмы"),
    "10": ("literary_abbreviations.tsv", "Литературные сокращения и пометы"),
}


def header() -> None:
    print("=" * 68)
    print(f" RU Max Clean v{PUBLIC_VERSION}  |  builder {BUILDER_VERSION}")
    print("=" * 68)
    print("Сборщик большого русско-русского словаря значений для KOReader.")
    print("Выберите профиль русского ядра и, при желании, дополнительные слои.")
    print("Лаунчер сам проверяет окружение, кэши, источники и StarDict.")
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
                # TextIOWrapper.read(size) waits for the full size on Windows,
                # which would hide the heartbeat while a child is quiet. Keep a
                # one-character read here; the launcher still processes queued
                # chunks when an alternate pipe reader supplies them.
                chunk = p.stdout.read(1)
                if chunk == "":
                    break
                chunks.put(chunk)
        finally:
            chunks.put(None)

    threading.Thread(target=_reader, name="ru-max-output", daemon=True).start()
    buf = ""
    progress_mode = False
    last_output = time.monotonic()
    heartbeat = 15.0
    live_width = 0
    heartbeat_line = False

    def clear_live_line() -> None:
        nonlocal live_width, heartbeat_line
        if live_width:
            sys.stdout.write("\r" + (" " * live_width) + "\r")
            sys.stdout.flush()
        live_width = 0
        heartbeat_line = False

    def write_live_line(text: str) -> None:
        nonlocal live_width
        # Carriage-return output is used by progress_ui.  Padding clears a
        # longer previous line on plain Windows cmd.exe without relying on ANSI
        # escape support, so heartbeat text never accumulates as fake log lines.
        width = len(text)
        padding = " " * max(0, live_width - width)
        sys.stdout.write("\r" + text + padding)
        sys.stdout.flush()
        live_width = max(width, live_width)

    while True:
        try:
            chunk = chunks.get(timeout=heartbeat)
        except queue.Empty:
            elapsed = int(time.monotonic() - last_output)
            write_live_line(
                f"[ОЖИДАНИЕ] {title or 'операция'} выполняется "
                f"({elapsed} с без нового вывода)..."
            )
            heartbeat_line = True
            continue
        if chunk is None:
            break
        for ch in chunk:
            last_output = time.monotonic()
            if ch == "\r":
                if heartbeat_line:
                    clear_live_line()
                if buf:
                    write_live_line(buf)
                buf = ""
                progress_mode = True
            elif ch == "\n":
                if progress_mode:
                    if buf:
                        write_live_line(buf)
                    elif heartbeat_line:
                        clear_live_line()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    buf = ""
                    progress_mode = False
                else:
                    clear_live_line()
                    print(buf, flush=True)
                    if log:
                        log.write(buf + "\n")
                        log.flush()
                    buf = ""
            else:
                buf += ch
    if heartbeat_line:
        clear_live_line()
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


def save_state(profile: str, reader_packs: list[str] | None = None) -> None:
    data = {
        "version": VERSION,
        "profile": profile,
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": fingerprints(read_manifest()),
        "local_build_fingerprint": local_build_fingerprint(),
        "reader_packs": sorted(reader_packs or []),
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


def choose_reader_packs() -> list[str]:
    """Ask once which optional dictionaries should accompany the core build."""
    print("\nДополнительные словари (русское ядро собирается всегда):")
    print("  0. Не собирать дополнительные слои")
    print("  A. Собрать все слои")
    for key, (_filename, label) in READER_PACKS.items():
        print(f"  {key}. {label}")
    choice = input("Выбор [0]: ").strip().casefold()
    if choice in {"a", "все", "all"}:
        return [filename for filename, _label in READER_PACKS.values()]
    if choice in {"", "0"}:
        return []
    selected: list[str] = []
    for key in choice.replace(",", " ").split():
        item = READER_PACKS.get(key)
        if item and item[0] not in selected:
            selected.append(item[0])
    if not selected:
        print("Не удалось распознать выбор; дополнительные слои пропущены.")
    return selected


def build_reader_layers(pack_names: list[str]) -> int:
    if not pack_names:
        return 0
    args = [
        sys.executable, "build_reader_packs.py",
        "--pack-dir", "reader_packs",
        "--output-dir", "RU-Reader-Packs",
    ]
    for name in pack_names:
        args.extend(["--pack", name])
    return run_stream(args, title="Сборка выбранных дополнительных словарей")


def guided_build() -> int:
    """One coherent user workflow: profile, layers, source mode, build, validate."""
    print("\nРусское ядро:")
    print("  1. MAX — полный профиль, включая Wikipedia")
    print("  2. Компактный — без большой Wikipedia")
    profile_choice = input("Выбор [1]: ").strip()
    profile = "lexical" if profile_choice == "2" else "max"
    pack_names = choose_reader_packs()
    mode = input("\nОбновлять источники перед сборкой? [Д/н]: ").strip().casefold()
    offline = mode in {"н", "n", "нет", "no"}
    print("\nСборка начинается. При повторном запуске неизменившиеся кэши будут переиспользованы.")
    rc = build(profile, offline=offline, smart=not offline)
    if rc:
        return rc
    rc = build_reader_layers(pack_names)
    if rc == 0:
        save_state(profile, pack_names)
        if pack_names:
            print("\nГОТОВО: русское ядро и выбранные дополнительные словари собраны.")
    return rc


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
        args += ["--download-kaikki", "--download-wikidata-lexemes", "--download-dal"]
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
        print("  1. Собрать словарь (профиль + дополнительные слои)")
        print("  2. Быстро пересобрать русское ядро из кэшей")
        print("  3. Проверить готовый русский словарь")
        print("  4. Проверить/обновить источники")
        print("  5. Диагностика компьютера и кэшей")
        print("  0. Выход")
        choice = input("\nВыбор: ").strip()
        actions = {
            "1": guided_build,
            "2": lambda: build("max", smart=False, quick=True),
            "3": validate,
            "4": check_sources,
            "5": diagnostics,
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
    ap.add_argument("--action", choices=["guided", "max", "lexical", "offline", "quick", "updates", "validate", "force", "diagnostics", "rebuild-caches", "benchmark"])
    args = ap.parse_args()
    if not args.action:
        return menu()
    return {
        "guided": guided_build,
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
