#!/usr/bin/env python3
"""Bootstrap/diagnostics for the RU Max Clean builder.

The core builder uses only the Python standard library. Native packages below are
optional accelerators; on an online Windows machine the launcher installs the
safe missing ones automatically, while offline mode keeps working with stdlib
fallbacks. rapidgzip is deliberately not auto-installed because some Windows
wheels make line-oriented Kaikki parsing slower.
"""
from __future__ import annotations
import argparse
import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path

PACKAGES = {
    "orjson": "orjson>=3.10",
    "lxml": "lxml>=5.0",
    "psutil": "psutil>=6.0",
}
# indexed_bzip2 can speed large Wikimedia .bz2 streams dramatically, but its
# published Windows wheels may lag behind a brand-new CPython release. Treat it
# as an optional accelerator instead of making the builder depend on a compiler.
OPTIONAL_PACKAGES = {
    "rapidgzip": "rapidgzip>=0.16",
    "indexed_bzip2": "indexed_bzip2>=1.7",
}


def probe() -> dict[str, object]:
    found: dict[str, str | bool] = {}
    for mod in {**PACKAGES, **OPTIONAL_PACKAGES}:
        try:
            m = importlib.import_module(mod)
            found[mod] = str(getattr(m, "__version__", "installed"))
        except Exception:
            found[mod] = False
    return {
        "python": sys.version.split()[0],
        "python_64bit": sys.maxsize > 2**32,
        "executable": sys.executable,
        "cpu_threads": os.cpu_count() or 1,
        "platform": platform.platform(),
        "packages": found,
    }


def ensure_pip() -> bool:
    try:
        import pip  # noqa: F401
        return True
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
            return True
        except Exception:
            return False


def install_missing(*, offline: bool = False) -> list[str]:
    info = probe()
    packages = info["packages"]
    required_missing = [PACKAGES[k] for k in PACKAGES if not packages.get(k)]
    optional_missing: list[str] = []
    # Do not auto-install rapidgzip.  Several Windows wheels have very poor
    # readline throughput on the line-oriented Kaikki dump and can turn a
    # minute-scale build into an hours-long run.  The builder keeps a guarded
    # opt-in for expert benchmarking via RU_MAX_ENABLE_RAPIDGZIP=1.
    # indexed_bzip2 wheels may lag behind a brand-new CPython release. On 3.14+
    # avoid a surprise MSVC source build; stdlib bz2 remains the fallback.
    can_try_indexed = not (os.name == "nt" and sys.version_info >= (3, 14))
    if can_try_indexed and not packages.get("indexed_bzip2"):
        optional_missing.append(OPTIONAL_PACKAGES["indexed_bzip2"])
    missing = required_missing + optional_missing
    if not missing or offline:
        return required_missing
    if not ensure_pip():
        return required_missing
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", *missing]
    print("[SETUP] Installing native accelerators: " + ", ".join(missing), flush=True)
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        print(f"[SETUP WARNING] pip returned {exc.returncode}; stdlib fallbacks remain available.", flush=True)
    importlib.invalidate_caches()
    after = probe()["packages"]
    return [PACKAGES[k] for k in PACKAGES if not after.get(k)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()
    if sys.version_info < (3, 10) or sys.maxsize <= 2**32:
        print("ERROR: 64-bit Python 3.10+ is required.")
        return 10
    if not args.check_only:
        remaining = install_missing(offline=args.offline)
    else:
        packages = probe()["packages"]
        remaining = [PACKAGES[k] for k in PACKAGES if not packages.get(k)]
    info = probe()
    print(f"Python {info['python']} 64-bit | CPU threads: {info['cpu_threads']}")
    for name, status in info["packages"].items():
        note = status or "not installed (fallback will be used)"
        if name == "rapidgzip" and status:
            note = f"{status} (installed, disabled by default; opt-in only)"
        if name == "indexed_bzip2" and not status and os.name == "nt" and sys.version_info >= (3, 14):
            note = "not installed (no auto-install on CPython 3.14+; stdlib bz2 fallback)"
        print(f"  {name:14s}: {note}")
    if remaining and not args.offline:
        print("WARNING: some accelerators could not be installed; build will still work, but slower.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
