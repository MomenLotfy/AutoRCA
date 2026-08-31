"""
collectors/host_metrics_collector.py
-----------------------------------------------------------------------------
HostMetricsCollector — gathers host-level resource observations from
/proc and df. Optional: skipped gracefully if any source is unreadable.

The collector reads:
  - /proc/stat (jiffies -> rough CPU utilisation across the last sample)
  - /proc/meminfo (MemTotal, MemAvailable, MemFree, Buffers, Cached)
  - /proc/loadavg (1/5/15-minute load averages)
  - df -P (disk utilisation per mounted filesystem)

It NEVER spawns a shell (no subprocess). All sources are file reads.

If /proc is unavailable (e.g. macOS, Windows, sandbox), the collector
returns an empty result. The pipeline continues with no host metrics.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from collectors.base import (
    BaseCollector,
    CollectedItem,
    CollectorError,
)


_LOADAVG = Path("/proc/loadavg")
_MEMINFO = Path("/proc/meminfo")
_STAT = Path("/proc/stat")
_MOUNTS = Path("/proc/mounts")


class HostMetricsCollector(BaseCollector):
    """Read host resource metrics from /proc + df.

    The collector keeps an internal "previous CPU sample" so it can
    report CPU utilisation as a delta. The first sample reports
    `cpu_percent = None` (no baseline). Subsequent samples (created by
    calling `collect()` again after >= 1s) report the percentage.

    The collector never raises if /proc is absent; it just reports
    itself unavailable.
    """

    name = "host_metrics"

    def __init__(self) -> None:
        self._last_total: Optional[int] = None
        self._last_idle: Optional[int] = None
        self._last_sample_at: Optional[float] = None

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        return _MEMINFO.exists() and _STAT.exists()

    # ------------------------------------------------------------------
    def collect(self, ctx=None) -> List[CollectedItem]:  # noqa: ARG002
        if not self.is_available():
            return []

        record: Dict[str, object] = {
            "type": "host_metrics",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Memory
        try:
            mem = _read_meminfo()
            if mem:
                record["mem_total_bytes"] = mem.get("MemTotal")
                record["mem_available_bytes"] = mem.get("MemAvailable")
                record["mem_free_bytes"] = mem.get("MemFree")
                record["mem_buffers_bytes"] = mem.get("Buffers")
                record["mem_cached_bytes"] = mem.get("Cached")
                if mem.get("MemTotal") and mem.get("MemAvailable") is not None:
                    used = mem["MemTotal"] - mem["MemAvailable"]
                    record["mem_used_bytes"] = used
                    record["mem_percent"] = round(
                        100.0 * used / max(mem["MemTotal"], 1), 2
                    )
        except (OSError, ValueError) as exc:
            raise CollectorError(f"HostMetricsCollector: meminfo read failed: {exc}") from exc

        # CPU delta
        try:
            cpu_total, cpu_idle = _read_cpu_stat()
            now = time.time()
            if (
                self._last_total is not None
                and self._last_idle is not None
                and self._last_sample_at is not None
                and now > self._last_sample_at
            ):
                total_delta = cpu_total - self._last_total
                idle_delta = cpu_idle - self._last_idle
                if total_delta > 0:
                    record["cpu_percent"] = round(
                        100.0 * (total_delta - idle_delta) / total_delta, 2
                    )
                else:
                    record["cpu_percent"] = 0.0
            else:
                record["cpu_percent"] = None
            self._last_total = cpu_total
            self._last_idle = cpu_idle
            self._last_sample_at = now
        except (OSError, ValueError):
            record["cpu_percent"] = None

        # Load average
        try:
            load = _read_loadavg()
            if load:
                record["load1"], record["load5"], record["load15"] = load
        except (OSError, ValueError):
            pass

        # Disk pressure (per mounted filesystem)
        try:
            disks = _read_disk_usage()
            if disks:
                record["disk_filesystems"] = disks
                # Highest utilisation across non-virtual mounts.
                real = [
                    d
                    for d in disks
                    if not _is_virtual_fs(d.get("fs", ""))
                ]
                if real:
                    record["disk_max_percent"] = max(
                        (d.get("use_percent") or 0) for d in real
                    )
        except (OSError, ValueError):
            pass

        body = json.dumps([record], ensure_ascii=False)
        return [
            CollectedItem(
                source="host_metrics",
                raw_text=body,
                timestamp=record["timestamp"],  # type: ignore[arg-type]
                metadata={"host": _hostname()},
            )
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MEMINFO_LINE = re.compile(r"^([A-Za-z_]+):\s+(\d+)\s*([kKmM]?B?)?$")


def _read_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    text = _MEMINFO.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        m = _MEMINFO_LINE.match(raw)
        if not m:
            continue
        key, value, unit = m.group(1), int(m.group(2)), (m.group(3) or "").upper()
        # Linux meminfo is always in kB
        multiplier = 1
        if unit.startswith("K"):
            multiplier = 1024
        elif unit.startswith("M"):
            multiplier = 1024 * 1024
        out[key] = value * multiplier
    return out


def _read_cpu_stat() -> tuple[int, int]:
    """Return (total_jiffies, idle_jiffies) summed across all CPUs."""
    total = 0
    idle = 0
    with _STAT.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            nums = [int(x) for x in parts[1:]]
            row_total = sum(nums)
            row_idle = nums[3]  # idle
            row_iowait = nums[4] if len(nums) > 4 else 0
            total += row_total
            idle += row_idle + row_iowait
            # We only need the "cpu" aggregate line (not per-CPU lines).
            if parts[0] == "cpu":
                return row_total, row_idle + row_iowait
    return total, idle


def _read_loadavg() -> Optional[tuple[float, float, float]]:
    if not _LOADAVG.exists():
        return None
    parts = _LOADAVG.read_text(encoding="utf-8", errors="replace").split()
    if len(parts) < 3:
        return None
    return float(parts[0]), float(parts[1]), float(parts[2])


def _read_disk_usage() -> List[Dict[str, object]]:
    """Read `/proc/mounts` + parse `/proc/self/mountinfo`-like stat sizes
    without invoking df (no subprocess). Returns lightweight per-fs stats.

    For each real mount point we compute total/used from statvfs.
    """
    if not _MOUNTS.exists():
        return []
    out: List[Dict[str, object]] = []
    seen: set[str] = set()
    for raw in _MOUNTS.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split()
        if len(parts) < 3:
            continue
        mount_point = parts[1]
        fs = parts[2]
        if mount_point in seen:
            continue
        seen.add(mount_point)
        if _is_virtual_fs(fs):
            continue
        try:
            st = os.statvfs(mount_point)
        except (OSError, ValueError):
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = max(total - free, 0)
        if total <= 0:
            continue
        use_percent = round(100.0 * used / total, 2)
        out.append(
            {
                "fs": fs,
                "mount": mount_point,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "use_percent": use_percent,
            }
        )
    return out


_VIRTUAL_FS = frozenset(
    {
        "proc",
        "sysfs",
        "devpts",
        "tmpfs",
        "devtmpfs",
        "cgroup",
        "cgroup2",
        "overlay",
        "squashfs",
        "autofs",
        "binfmt_misc",
        "fusectl",
        "configfs",
        "debugfs",
        "tracefs",
        "mqueue",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "hugetlbfs",
        "securityfs",
        "nsfs",
        "fuse.gvfsd-fuse",
    }
)


def _is_virtual_fs(fs: str) -> bool:
    return fs.lower() in _VIRTUAL_FS


def _hostname() -> str:
    try:
        return os.uname().nodename
    except (AttributeError, OSError):
        return "unknown"


__all__ = ["HostMetricsCollector"]