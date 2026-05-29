"""
fi_host.analysis.engine
Post-sweep analysis: fault clustering, heatmap generation,
voltage distribution, and report generation.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np

from fi_host.core import GlitchRecord, GlitchResult, SessionStats


# ─────────────────────────── HEATMAP ────────────────────────────────────────

def build_heatmap(
    records: list[GlitchRecord],
) -> dict:
    """
    Returns a JSON-serialisable heatmap structure:
      {delays, widths, grid (RESULT int per cell), fault_coords}
    """
    if not records:
        return {"delays": [], "widths": [], "grid": [], "fault_coords": []}

    delays = sorted(set(r.trigger_delay_ns for r in records))
    widths = sorted(set(r.glitch_width_ns  for r in records))

    d_idx = {v: i for i, v in enumerate(delays)}
    w_idx = {v: i for i, v in enumerate(widths)}

    grid = [[None] * len(delays) for _ in range(len(widths))]
    fault_coords = []

    for r in records:
        di = d_idx.get(r.trigger_delay_ns)
        wi = w_idx.get(r.glitch_width_ns)
        if di is not None and wi is not None:
            grid[wi][di] = int(r.result)
            if r.is_fault:
                fault_coords.append({"d": r.trigger_delay_ns, "w": r.glitch_width_ns})

    return {
        "delays":       delays,
        "widths":       widths,
        "grid":         grid,
        "fault_coords": fault_coords,
    }


# ─────────────────────────── CLUSTERING ─────────────────────────────────────

def find_fault_clusters(
    records: list[GlitchRecord],
    eps_delay: int  = 2000,
    eps_width: int  = 200,
    min_samples: int = 2,
) -> list[dict]:
    """
    Simple density-based clustering on fault points.
    Returns list of cluster dicts with centroid and bounding box.
    """
    faults = [r for r in records if r.is_fault]
    if not faults:
        return []

    visited  = set()
    clusters = []

    def neighbours(idx: int) -> list[int]:
        f = faults[idx]
        return [
            j for j, g in enumerate(faults)
            if abs(f.trigger_delay_ns - g.trigger_delay_ns) <= eps_delay
            and abs(f.glitch_width_ns  - g.glitch_width_ns)  <= eps_width
        ]

    for i in range(len(faults)):
        if i in visited:
            continue
        nbrs = neighbours(i)
        if len(nbrs) < min_samples:
            continue
        cluster_pts: list[int] = []
        stack = list(nbrs)
        while stack:
            j = stack.pop()
            if j in visited:
                continue
            visited.add(j)
            cluster_pts.append(j)
            jnbrs = neighbours(j)
            if len(jnbrs) >= min_samples:
                stack.extend(jnbrs)

        if not cluster_pts:
            continue

        pts = [faults[k] for k in cluster_pts]
        delays = [p.trigger_delay_ns for p in pts]
        widths = [p.glitch_width_ns  for p in pts]
        mvs    = [p.adc_min_mv       for p in pts]

        clusters.append({
            "size":           len(pts),
            "centroid_delay": int(np.mean(delays)),
            "centroid_width": int(np.mean(widths)),
            "delay_range":    [min(delays), max(delays)],
            "width_range":    [min(widths),  max(widths)],
            "avg_mv_drop":    int(np.mean(mvs)) if mvs else 0,
        })

    return sorted(clusters, key=lambda c: -c["size"])


# ─────────────────────────── VOLTAGE STATS ──────────────────────────────────

def voltage_distribution(records: list[GlitchRecord]) -> dict:
    ok_mv    = [r.adc_min_mv for r in records if r.is_ok    and r.adc_min_mv > 0]
    fault_mv = [r.adc_min_mv for r in records if r.is_fault and r.adc_min_mv > 0]
    crash_mv = [r.adc_min_mv for r in records if r.is_crash and r.adc_min_mv > 0]

    def stats(arr: list[int]) -> dict:
        if not arr:
            return {}
        a = np.array(arr)
        return {
            "min":    int(a.min()),
            "max":    int(a.max()),
            "mean":   float(round(a.mean(), 1)),
            "std":    float(round(a.std(), 1)),
            "p5":     int(np.percentile(a, 5)),
            "p95":    int(np.percentile(a, 95)),
            "hist":   np.histogram(a, bins=20)[0].tolist(),
            "edges":  [round(x, 1) for x in np.histogram(a, bins=20)[1].tolist()],
        }

    return {"ok": stats(ok_mv), "fault": stats(fault_mv), "crash": stats(crash_mv)}


# ─────────────────────────── REPORT ─────────────────────────────────────────

def generate_report(records: list[GlitchRecord], sweep_params: Optional[dict] = None) -> dict:
    stats    = SessionStats()
    for r in records:
        stats.record(r)

    clusters = find_fault_clusters(records)
    heatmap  = build_heatmap(records)
    volt     = voltage_distribution(records)

    top_faults = sorted(
        [r for r in records if r.is_fault],
        key=lambda r: r.adc_min_mv,
    )[:10]

    return {
        "summary": {
            "total":       stats.total,
            "ok":          stats.ok,
            "fault":       stats.fault,
            "crash":       stats.crash,
            "timeout":     stats.timeout,
            "fault_rate":  round(stats.fault_rate, 2),
            "crash_rate":  round(stats.crash_rate, 2),
        },
        "clusters":    clusters,
        "heatmap":     heatmap,
        "voltage":     volt,
        "top_faults": [r.model_dump() for r in top_faults],
        "sweep_params": sweep_params or {},
    }


# ─────────────────────────── LOG I/O ────────────────────────────────────────

class ResultLog:
    """Append-only JSONL result log with streaming read support."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(path, "a", buffering=1)

    def write(self, record: GlitchRecord):
        self._f.write(record.model_dump_json() + "\n")

    def close(self):
        self._f.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    @classmethod
    def load(cls, path: Path) -> list[GlitchRecord]:
        records = []
        with open(path) as f:
            for line in f:
                r = GlitchRecord.from_json_line(line)
                if r:
                    records.append(r)
        return records
