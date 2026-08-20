#!/usr/bin/env python3
"""Compact carriage-return progress bars for RU Max Clean."""
from __future__ import annotations
import os
import sys
import time
from dataclasses import dataclass

_BAR = 28
_last_emit: dict[str, float] = {}
_state: dict[str, tuple[float, float]] = {}  # label -> (start_time, start_value)
_last_value: dict[str, tuple[float, float | None, str]] = {}


def _enabled() -> bool:
    value = os.environ.get("RU_MAX_PROGRESS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _fmt_num(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}G"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def _fmt_time(seconds: float) -> str:
    if seconds < 0 or seconds > 365 * 86400:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def render(label: str, current: int | float, total: int | float | None = None, *, unit: str = "", force: bool = False) -> None:
    if not _enabled():
        return
    now = time.monotonic()
    last = _last_emit.get(label, 0.0)
    if not force and now - last < 0.12:
        return
    _last_emit[label] = now
    _last_value[label] = (float(current), float(total) if total is not None else None, unit)
    if label not in _state:
        _state[label] = (now, float(current))
    start_t, start_v = _state[label]
    elapsed = max(0.001, now - start_t)
    rate = max(0.0, (float(current) - start_v) / elapsed)
    rate_text = f" | {_fmt_num(rate)}/s" if elapsed >= 0.5 and rate > 0 else ""

    suffix_unit = f" {unit}" if unit else ""
    if total and total > 0:
        ratio = max(0.0, min(1.0, float(current) / float(total)))
        filled = int(round(_BAR * ratio))
        bar = "#" * filled + "-" * (_BAR - filled)
        eta = (float(total) - float(current)) / rate if rate > 0 else -1
        eta_text = f" | ETA {_fmt_time(eta)}" if rate > 0 and ratio < 1 else ""
        text = (
            f"[{bar}] {ratio * 100:6.2f}%  {label}  "
            f"{_fmt_num(float(current))}/{_fmt_num(float(total))}{suffix_unit}{rate_text}{eta_text}"
        )
    else:
        pos = int(now * 8) % _BAR
        chars = ["-"] * _BAR
        chars[pos] = "#"
        text = f"[{''.join(chars)}]  {label}  {_fmt_num(float(current))}{suffix_unit}{rate_text}"
    sys.stderr.write("\r" + text)
    sys.stderr.flush()


def finish(label: str, current: int | float, total: int | float | None = None, *, unit: str = "") -> None:
    if not _enabled():
        return
    final_total = total or current
    previous = _last_value.get(label)
    final_state = (float(current), float(final_total) if final_total is not None else None, unit)
    if previous != final_state:
        render(label, current, final_total, unit=unit, force=True)
    sys.stderr.write("\n")
    sys.stderr.flush()
    _last_emit.pop(label, None)
    _state.pop(label, None)
    _last_value.pop(label, None)


@dataclass
class ProgressTotals:
    """Persist rough stage totals so later builds have determinate progress bars."""
    values: dict[str, int]

    def expected(self, key: str, fallback: int = 0) -> int:
        value = self.values.get(key, fallback)
        try:
            return max(0, int(value))
        except Exception:
            return max(0, int(fallback))

    def record(self, key: str, value: int) -> None:
        if value > 0:
            self.values[key] = int(value)
