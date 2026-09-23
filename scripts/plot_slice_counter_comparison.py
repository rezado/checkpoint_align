#!/usr/bin/env python3
"""Compare performance counters for corresponding slices in two run groups.

The input layout is the one used by the current mcf experiment::

    mcf_A/mcf_<point>_<weight>/simulator_err.txt
    mcf_B/mcf_<point>_<weight>/simulator_err.txt

Only the last ``[PERF][time=...]`` window is used.  This is important because
the simulator also dumps a warm-up window before resetting the counters.
The script writes tab-separated raw/normalized deltas and Pillow-only figures,
matching the visual style of the THP seed comparison scripts without requiring
matplotlib or pandas.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont


PERF_RE = re.compile(
    r"^\[PERF\s*\]\[time=\s*(?P<time>\d+)\] (?P<path>[^:]+): "
    r"(?P<name>[^,]+),\s*(?P<value>-?\d+)$"
)
SLICE_RE = re.compile(r"^mcf_(?P<point>[^_]+)_(?P<weight>.+)$")


@dataclass(frozen=True)
class CounterSpec:
    name: str
    label: str
    unit: str
    selector: Callable[[str, str], bool]
    additive: bool = False


def path_ends(suffix: str, name: str) -> Callable[[str, str], bool]:
    return lambda path, counter: path.endswith(suffix) and counter == name


def path_contains(fragment: str, name: str) -> Callable[[str, str], bool]:
    return lambda path, counter: fragment in path and counter == name


def specs() -> tuple[CounterSpec, ...]:
    rob = ".backend.inner.ctrlBlock.rob"
    return (
        CounterSpec("ipc", "IPC", "ratio", lambda p, n: p.endswith(rob) and n in {"commitInstr", "clock_cycle"}),
        CounterSpec("alu_instr", "ALU instructions", "events_per_minst", path_ends(rob, "alu_instr_cnt")),
        CounterSpec("branch_instr", "branch instructions", "events_per_minst", path_ends(rob, "brh_instr_cnt")),
        CounterSpec("load_instr", "load instructions", "events_per_minst", path_ends(rob, "load_instr_cnt")),
        CounterSpec("store_instr", "store instructions", "events_per_minst", path_ends(rob, "store_instr_cnt")),
        CounterSpec("branch_mispredict", "branch mispredict", "events_per_minst", path_ends(rob, "br_mis_pred")),
        CounterSpec("dtlb_l2tlb_req", "DTLB→L2TLB request", "events_per_minst", path_ends(".memBlock.inner.ptw.ptw", "req_count1")),
        CounterSpec("ptw_mem_req", "page-walk memory request", "events_per_minst", path_ends(".memBlock.inner.ptw.ptw", "mem_count")),
        CounterSpec("ptw_mem_cycles", "page-walk memory cycles", "cycles_per_kinst", path_ends(".memBlock.inner.ptw.ptw", "mem_cycle")),
        CounterSpec("load_tlb_miss", "load TLB miss", "events_per_minst", path_contains(".memBlock.inner.LoadUnit_", "tlb_miss_first_issue"), True),
        CounterSpec("store_tlb_miss", "store TLB miss", "events_per_minst", path_contains(".memBlock.inner.StoreUnit_", "s1_tlbMiss"), True),
        CounterSpec("load_ptw_req", "load PTW request", "events_per_minst", path_ends(".dtlbRepeater.load_filter_load_entry", "ptw_req_count")),
        CounterSpec("store_ptw_req", "store PTW request", "events_per_minst", path_ends(".dtlbRepeater.store_filter_store_entry", "ptw_req_count")),
        CounterSpec("superpage_hit", "superpage hit", "events_per_minst", path_ends(".memBlock.inner.ptw.ptw.cache", "sp_hit")),
        CounterSpec("superpage_refill", "superpage refill", "events_per_minst", path_ends(".memBlock.inner.ptw.ptw.cache", "spRefill")),
        CounterSpec("l1d_miss", "L1D miss first issue", "events_per_minst", path_contains(".memBlock.inner.LoadUnit_", "dcache_miss_first_issue"), True),
        CounterSpec("l1d_miss_allocate", "L1D miss allocate", "events_per_minst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "miss_req_load_allocate")),
        CounterSpec("l1d_miss_merge", "L1D miss merge", "events_per_minst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "miss_req_merge_load")),
        CounterSpec("l1d_miss_reject", "L1D miss reject", "events_per_minst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "miss_req_reject_load")),
        CounterSpec("l1d_mshr_no_free", "L1D MSHR no-free", "cycles_per_kinst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "no_free_entry")),
        CounterSpec("l1d_mlp", "L1D MLP area", "entry_cycles_per_kinst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "L1DMLP_CPUData_sum")),
        CounterSpec("l1d_refill_latency", "L1D refill latency", "entry_cycles_per_kinst", path_ends(".memBlock.inner.dcache.dcache.missQueue", "miss_load_refill_latency")),
        CounterSpec("rob_l2_miss_stall", "ROB blocked by L2 miss", "cycles_per_kinst", path_ends(".l2cache.topDown", "RobBlockByL2Miss")),
        CounterSpec("backend_exec_stall", "backend execute stall", "cycles_per_kinst", path_ends(".backend.inner.topDownMod", "exec_stall_cycle")),
        CounterSpec("backend_l2_stall", "backend L2-miss stall", "cycles_per_kinst", path_ends(".backend.inner.topDownMod", "mem_stall_l2miss")),
    )


def parse_log(path: Path) -> dict[str, float]:
    """Parse the final PERF window, summing counters with repeated units."""
    if not path.is_file():
        raise FileNotFoundError(path)
    latest_time = -1
    raw: dict[str, float] = {}
    matched_names: set[str] = set()
    all_specs = specs()
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = PERF_RE.match(line.rstrip("\n"))
            if not match:
                continue
            timestamp = int(match.group("time"))
            if timestamp > latest_time:
                latest_time = timestamp
                raw = {}
                matched_names = set()
            if timestamp != latest_time:
                continue
            path_text = match.group("path")
            counter = match.group("name")
            value = float(match.group("value"))
            for spec in all_specs:
                if spec.selector(path_text, counter):
                    if spec.name == "ipc":
                        raw[counter] = raw.get(counter, 0.0) + value
                    elif spec.additive:
                        raw[spec.name] = raw.get(spec.name, 0.0) + value
                    else:
                        raw[spec.name] = value
                    matched_names.add(spec.name)
    if latest_time < 0 or raw.get("commitInstr", 0.0) <= 0:
        raise ValueError(f"no complete PERF window with commitInstr: {path}")
    commit = raw["commitInstr"]
    cycles = raw.get("clock_cycle", 0.0)
    if cycles <= 0:
        raise ValueError(f"no clock_cycle in final PERF window: {path}")
    raw["ipc"] = commit / cycles
    raw["__time"] = float(latest_time)
    raw["__commitInstr"] = commit
    raw["__clock_cycle"] = cycles
    return raw


def normalize(raw: dict[str, float]) -> dict[str, float]:
    commit = raw["__commitInstr"]
    cycles = raw["__clock_cycle"]
    result = {"ipc": raw["ipc"], "commitInstr": commit, "clock_cycle": cycles}
    for spec in specs():
        if spec.name == "ipc" or spec.name not in raw:
            continue
        value = raw[spec.name]
        if spec.unit == "events_per_minst":
            result[spec.name] = value / commit * 1e6
        elif spec.unit in {"cycles_per_kinst", "entry_cycles_per_kinst"}:
            result[spec.name] = value / commit * 1e3
        elif spec.unit == "fraction":
            result[spec.name] = value / cycles
        else:
            result[spec.name] = value
    return result


def discover(group: Path) -> dict[str, tuple[str, Path]]:
    result: dict[str, tuple[str, Path]] = {}
    for directory in sorted(group.glob("mcf_*")):
        if not directory.is_dir():
            continue
        match = SLICE_RE.match(directory.name)
        log = directory / "simulator_err.txt"
        if match and log.is_file():
            point = match.group("point")
            result[point] = (directory.name, log)
    return result


def write_tables(out: Path, rows: list[dict[str, object]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    summary_fields = ["slice", "weight", "a_ipc", "b_ipc", "ipc_delta", "ipc_delta_pct", "a_cycles", "b_cycles", "cycles_delta_pct"]
    with (out / "slice_summary.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=summary_fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in summary_fields})
    detail_fields = ["slice", "weight", "counter", "label", "unit", "a_value", "b_value", "delta", "delta_pct"]
    with (out / "slice_counter_comparison.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=detail_fields, delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            for spec in specs():
                if spec.name not in row["a"] or spec.name not in row["b"]:
                    continue
                a = float(row["a"][spec.name])
                b = float(row["b"][spec.name])
                delta = b - a
                pct = delta / a * 100.0 if a else float("nan")
                writer.writerow({"slice": row["slice"], "weight": row["weight"], "counter": spec.name, "label": spec.label, "unit": spec.unit, "a_value": a, "b_value": b, "delta": delta, "delta_pct": pct})


BG = (247, 249, 252)
WHITE = (255, 255, 255)
INK = (34, 43, 56)
MUTED = (96, 110, 130)
GRID = (207, 217, 228)
BLUE = (13, 126, 184)
ORANGE = (220, 103, 20)


def font(size: int, bold: bool = False):
    names = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def draw_header(draw: ImageDraw.ImageDraw, width: int, heading: str, subtitle: str) -> None:
    draw.text((42, 26), heading, fill=INK, font=font(31, True))
    draw.text((44, 70), subtitle, fill=MUTED, font=font(17))
    draw.line((42, 103, width - 42, 103), fill=GRID, width=2)


def plot_ipc(out: Path, rows: list[dict[str, object]]) -> None:
    width, height = 1700, 820
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw_header(draw, width, "mcf 两组切片 IPC 逐切片对比", "蓝色=A，橙色=B；下方标注 B 相对 A 的变化")
    left, right, top, bottom = 100, 1580, 160, 680
    values = [float(r["a"]["ipc"]) for r in rows] + [float(r["b"]["ipc"]) for r in rows]
    low, high = min(values) * 0.96, max(values) * 1.04
    if high <= low:
        high = low + 1.0
    step = (right - left) / len(rows)
    def y(v: float) -> float:
        return bottom - (v - low) / (high - low) * (bottom - top)
    for i in range(5):
        value = low + (high - low) * i / 4
        py = y(value)
        draw.line((left, py, right, py), fill=GRID, width=1)
        draw.text((left - 12, py), f"{value:.3f}", fill=MUTED, font=font(13), anchor="rm")
    for i, row in enumerate(rows):
        cx = left + (i + 0.5) * step
        bar_w = min(20, step * 0.26)
        for offset, key, color in ((-bar_w * 0.7, "a", BLUE), (bar_w * 0.7, "b", ORANGE)):
            value = float(row[key]["ipc"])
            y_value = y(value)
            x0 = cx + offset - bar_w / 2
            x1 = cx + offset + bar_w / 2
            draw.rectangle((min(x0, x1), min(y_value, bottom), max(x0, x1), max(y_value, bottom)), fill=color)
        draw.text((cx, bottom + 12), str(row["slice"]), fill=INK, font=font(13), anchor="ma")
        delta = float(row["ipc_delta_pct"])
        draw.text((cx, bottom + 34), f"{delta:+.1f}%", fill=BLUE if delta <= 0 else ORANGE, font=font(12, True), anchor="ma")
    draw.line((left, bottom, right, bottom), fill=INK, width=2)
    draw.rectangle((left, 120, left + 20, 136), fill=BLUE)
    draw.text((left + 28, 128), "A", fill=INK, font=font(15), anchor="lm")
    draw.rectangle((left + 88, 120, left + 108, 136), fill=ORANGE)
    draw.text((left + 116, 128), "B", fill=INK, font=font(15), anchor="lm")
    image.save(out / "slice_ipc_comparison.png")


def plot_heatmap(out: Path, rows: list[dict[str, object]]) -> None:
    selected = [s for s in specs() if s.name not in {"ipc"}]
    cell_w, cell_h = 78, 30
    label_w, top = 280, 150
    width = label_w + cell_w * len(rows) + 70
    height = top + cell_h * len(selected) + 80
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw_header(draw, width, "mcf 性能计数器逐切片差异热图", "每格为 (B−A)/A；蓝色=下降，橙色=上升；极端值按 ±100% 截断")
    for i, row in enumerate(rows):
        x = label_w + i * cell_w + cell_w / 2
        draw.text((x, top - 28), str(row["slice"]), fill=INK, font=font(12), anchor="ma")
    for ridx, spec in enumerate(selected):
        y = top + ridx * cell_h
        draw.text((label_w - 10, y + cell_h / 2), spec.label, fill=INK, font=font(13), anchor="rm")
        for cidx, row in enumerate(rows):
            a = float(row["a"].get(spec.name, 0.0))
            b = float(row["b"].get(spec.name, 0.0))
            pct = (b - a) / a * 100.0 if a else 0.0
            clipped = max(-100.0, min(100.0, pct))
            color = mix((224, 241, 249), BLUE, abs(clipped) / 100) if clipped < 0 else mix((252, 235, 218), ORANGE, abs(clipped) / 100)
            x0 = label_w + cidx * cell_w
            draw.rectangle((x0, y, x0 + cell_w - 2, y + cell_h - 2), fill=color)
            draw.text((x0 + (cell_w - 2) / 2, y + (cell_h - 2) / 2), "n/a" if a == 0 else f"{pct:+.0f}%", fill=INK, font=font(11, abs(clipped) > 55), anchor="mm")
    draw.rectangle((label_w, top + len(selected) * cell_h + 18, label_w + 18, top + len(selected) * cell_h + 33), fill=BLUE)
    draw.text((label_w + 26, top + len(selected) * cell_h + 25), "B 低于 A", fill=MUTED, font=font(13), anchor="lm")
    draw.rectangle((label_w + 130, top + len(selected) * cell_h + 18, label_w + 148, top + len(selected) * cell_h + 33), fill=ORANGE)
    draw.text((label_w + 156, top + len(selected) * cell_h + 25), "B 高于 A", fill=MUTED, font=font(13), anchor="lm")
    image.save(out / "slice_counter_delta_heatmap.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-a", type=Path, default=Path("mcf_A"))
    parser.add_argument("--group-b", type=Path, default=Path("mcf_B"))
    parser.add_argument("--output", type=Path, default=Path("mcf_slice_counter_analysis"))
    args = parser.parse_args()
    group_a = discover(args.group_a)
    group_b = discover(args.group_b)
    points = sorted(set(group_a) & set(group_b), key=lambda x: int(x))
    if not points:
        raise SystemExit("no matching mcf slices found")
    rows: list[dict[str, object]] = []
    for point in points:
        name_a, path_a = group_a[point]
        name_b, path_b = group_b[point]
        raw_a = parse_log(path_a)
        raw_b = parse_log(path_b)
        norm_a, norm_b = normalize(raw_a), normalize(raw_b)
        weight = name_a.rsplit("_", 1)[-1]
        ipc_delta = norm_b["ipc"] - norm_a["ipc"]
        cycles_delta_pct = (norm_b["clock_cycle"] - norm_a["clock_cycle"]) / norm_a["clock_cycle"] * 100.0
        rows.append({"slice": point, "weight": weight, "a": norm_a, "b": norm_b, "a_ipc": norm_a["ipc"], "b_ipc": norm_b["ipc"], "ipc_delta": ipc_delta, "ipc_delta_pct": ipc_delta / norm_a["ipc"] * 100.0, "a_cycles": norm_a["clock_cycle"], "b_cycles": norm_b["clock_cycle"], "cycles_delta_pct": cycles_delta_pct})
    args.output.mkdir(parents=True, exist_ok=True)
    write_tables(args.output, rows)
    plot_ipc(args.output, rows)
    plot_heatmap(args.output, rows)
    print(f"compared {len(rows)} slices")
    print(f"wrote {args.output / 'slice_summary.tsv'}")
    print(f"wrote {args.output / 'slice_counter_comparison.tsv'}")
    print(f"wrote {args.output / 'slice_ipc_comparison.png'}")
    print(f"wrote {args.output / 'slice_counter_delta_heatmap.png'}")


if __name__ == "__main__":
    main()
