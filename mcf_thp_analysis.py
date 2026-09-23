#!/usr/bin/env python3
"""Compare mcf_A (THP on) and mcf_B (THP off) simulator slices."""
from __future__ import annotations

import csv
import html
import math
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "mcf_thp_analysis"
PERF_RE = re.compile(r"^\[PERF \]\[time=(\d+)\] (.*): ([^,]+), (-?\d+(?:\.\d+)?)\s*$")
CORE_RE = re.compile(r"Core-0 instrCnt\s*=\s*([\d,]+), cycleCnt\s*=\s*([\d,]+), IPC\s*=\s*([0-9.]+)")
HOST_RE = re.compile(r"Host time spent:\s*([\d,]+)ms")
SLICE_RE = re.compile(r"mcf_(\d+)_([0-9.]+)$")


def number(s: str):
    x = float(s.replace(",", ""))
    return int(x) if x.is_integer() else x


def parse_out(path: Path):
    text = path.read_text(errors="replace")
    m = CORE_RE.search(text)
    h = HOST_RE.search(text)
    if not m or not h:
        raise ValueError(f"missing performance summary in {path}")
    return {
        "instr_cnt": int(m.group(1).replace(",", "")),
        "cycle_cnt": int(m.group(2).replace(",", "")),
        "ipc": float(m.group(3)),
        "host_time_ms": int(h.group(1).replace(",", "")),
    }


def parse_perf(path: Path):
    # The log contains two dumps. The final dump is the one closest to the
    # terminating cycle count and is selected by its largest timestamp.
    rows = []
    times = []
    for line in path.open(errors="replace"):
        m = PERF_RE.match(line.rstrip("\r\n"))
        if m:
            t = int(m.group(1))
            times.append(t)
            rows.append((t, f"{m.group(2)}:{m.group(3)}", number(m.group(4))))
    if not rows:
        raise ValueError(f"no PERF records in {path}")
    final_time = max(times)
    final_rows = [(key, value) for t, key, value in rows if t == final_time]
    # Some simulator components emit multiple counters with the same printed
    # path/name. Preserve those records instead of silently overwriting them;
    # the occurrence suffix is the only stable identity available in the log.
    counts = Counter(key for key, _ in final_rows)
    seen = Counter()
    result = {}
    for key, value in final_rows:
        seen[key] += 1
        out_key = key if counts[key] == 1 else f"{key}#instance_{seen[key]}"
        result[out_key] = value
    return result, final_time


def fmt(v):
    if isinstance(v, int):
        return f"{v:,}"
    return f"{v:,.6g}"


def pct(a, b):
    if a == 0:
        return None if b == 0 else math.inf
    return (b - a) / abs(a) * 100.0


def pct_text(x):
    if x is None:
        return "0.00%"
    if math.isinf(x):
        return "+inf%" if x > 0 else "-inf%"
    return f"{x:+.2f}%"


def slice_id(path: Path):
    m = SLICE_RE.match(path.parent.name)
    return path.parent.name, (int(m.group(1)), float(m.group(2))) if m else (path.parent.name, (0, 0.0))


def main():
    OUT.mkdir(exist_ok=True)
    a_dirs = {p.parent.name: p.parent for p in (ROOT / "mcf_A").glob("*/simulator_out.txt")}
    b_dirs = {p.parent.name: p.parent for p in (ROOT / "mcf_B").glob("*/simulator_out.txt")}
    names = sorted(set(a_dirs) & set(b_dirs), key=lambda n: slice_id(a_dirs[n] / "simulator_out.txt")[1])
    if not names:
        raise SystemExit("No matching slices found")

    perf_rows = []
    counter_rows = []
    all_top = []
    for name in names:
        a = parse_out(a_dirs[name] / "simulator_out.txt")
        b = parse_out(b_dirs[name] / "simulator_out.txt")
        ac, at = parse_perf(a_dirs[name] / "simulator_err.txt")
        bc, bt = parse_perf(b_dirs[name] / "simulator_err.txt")
        seed, weight = slice_id(a_dirs[name] / "simulator_out.txt")[1]
        perf_rows.append({"slice": name, "simpoint": seed, "weight": weight, "A_mode": "THP on", "B_mode": "THP off", **{f"A_{k}": v for k, v in a.items()}, **{f"B_{k}": v for k, v in b.items()}, "cycle_delta_pct": pct(a["cycle_cnt"], b["cycle_cnt"]), "ipc_delta_pct": pct(a["ipc"], b["ipc"]), "host_delta_pct": pct(a["host_time_ms"], b["host_time_ms"]), "A_perf_time": at, "B_perf_time": bt})
        keys = sorted(set(ac) | set(bc))
        for key in keys:
            av, bv = ac.get(key), bc.get(key)
            d = None if av is None or bv is None else bv - av
            dp = None if av is None or bv is None else pct(av, bv)
            counter_rows.append({"slice": name, "simpoint": seed, "weight": weight, "counter": key, "A_thp_on": av, "B_thp_off": bv, "delta_B_minus_A": d, "delta_pct": dp})
            if av is not None and bv is not None and (av != 0 or bv != 0):
                score = abs(dp) if dp is not None and math.isfinite(dp) else 1e12
                all_top.append((score, name, key, av, bv, d, dp))

    with (OUT / "slice_performance.csv").open("w", newline="") as f:
        fields = list(perf_rows[0])
        w = csv.DictWriter(f, fields); w.writeheader(); w.writerows(perf_rows)
    with (OUT / "counter_comparison.csv").open("w", newline="") as f:
        fields = ["slice", "simpoint", "weight", "counter", "A_thp_on", "B_thp_off", "delta_B_minus_A", "delta_pct"]
        w = csv.DictWriter(f, fields); w.writeheader(); w.writerows(counter_rows)

    # Select counters that are both non-trivial and consistently present for a compact report.
    by_key = {}
    for r in counter_rows:
        if r["A_thp_on"] is not None and r["B_thp_off"] is not None and (r["A_thp_on"] != 0 or r["B_thp_off"] != 0):
            by_key.setdefault(r["counter"], []).append(r)
    aggregate = []
    for key, vals in by_key.items():
        dps = [r["delta_pct"] for r in vals if r["delta_pct"] is not None and math.isfinite(r["delta_pct"])]
        # Aggregate raw counts before calculating the percentage. This avoids
        # promoting counters whose A baseline is only a handful of events.
        sum_a = sum(float(r["A_thp_on"]) for r in vals)
        sum_b = sum(float(r["B_thp_off"]) for r in vals)
        if len(dps) < max(3, len(names) // 2) or max(sum_a, sum_b) < 1000:
            continue
        agg_pct = pct(sum_a, sum_b)
        aggregate.append((abs(agg_pct) if math.isfinite(agg_pct) else 1e12, agg_pct, key, len(vals), statistics.mean(dps), sum_a, sum_b))
    aggregate.sort(reverse=True)
    top_aggregate = aggregate[:30]

    # Write a small, dependency-free SVG with the key performance chart.
    W, H = 1120, 640
    left, right, top, bottom = 90, 30, 60, 90
    max_cycle = max(max(r["A_cycle_cnt"], r["B_cycle_cnt"]) for r in perf_rows)
    max_ipc = max(max(r["A_ipc"], r["B_ipc"]) for r in perf_rows)
    bar_w = 13
    group_w = (W - left - right) / len(perf_rows)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', '<rect width="100%" height="100%" fill="#ffffff"/>', '<style>text{font-family:Arial,sans-serif;fill:#243447} .axis{stroke:#7b8794;stroke-width:1} .grid{stroke:#e5e7eb;stroke-width:1} .title{font-size:20px;font-weight:700} .label{font-size:11px}</style>', '<text x="90" y="30" class="title">mcf per-slice performance: A THP on vs B THP off</text>']
    base = H - bottom
    for i in range(5):
        y = top + i * (base - top) / 4
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W-right}" y2="{y:.1f}" class="grid"/>')
        svg.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" class="label">{max_cycle*(4-i)/4/1e6:.0f}M</text>')
    svg.append(f'<line x1="{left}" y1="{base}" x2="{W-right}" y2="{base}" class="axis"/>')
    for i, r in enumerate(perf_rows):
        x = left + group_w * i + group_w / 2
        for off, val, color in [(-bar_w-2, r["A_cycle_cnt"], "#d97706"), (2, r["B_cycle_cnt"], "#2563eb")]:
            h = (base-top) * val / max_cycle
            svg.append(f'<rect x="{x+off:.1f}" y="{base-h:.1f}" width="{bar_w}" height="{h:.1f}" fill="{color}"/>')
        svg.append(f'<text x="{x:.1f}" y="{base+18}" text-anchor="middle" class="label">{r["simpoint"]}</text>')
        svg.append(f'<text x="{x:.1f}" y="{base+33}" text-anchor="middle" class="label">{r["weight"]:.3f}</text>')
    svg += ['<rect x="910" y="45" width="12" height="12" fill="#d97706"/><text x="928" y="55" class="label">A THP on</text>', '<rect x="1000" y="45" width="12" height="12" fill="#2563eb"/><text x="1018" y="55" class="label">B THP off</text>', '<text x="90" y="615" class="label">x-axis labels: simpoint id / slice weight; y-axis: cycle count</text>', '</svg>']
    (OUT / "performance_cycles.svg").write_text("\n".join(svg))
    svg2 = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', '<rect width="100%" height="100%" fill="#ffffff"/>', '<style>text{font-family:Arial,sans-serif;fill:#243447} .axis{stroke:#7b8794;stroke-width:1} .grid{stroke:#e5e7eb;stroke-width:1} .title{font-size:20px;font-weight:700} .label{font-size:11px}</style>', '<text x="90" y="30" class="title">mcf per-slice IPC: A THP on vs B THP off</text>']
    for i in range(5):
        y = top + i * (base - top) / 4
        svg2.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W-right}" y2="{y:.1f}" class="grid"/>')
        svg2.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" class="label">{max_ipc*(4-i)/4:.2f}</text>')
    svg2.append(f'<line x1="{left}" y1="{base}" x2="{W-right}" y2="{base}" class="axis"/>')
    for i, r in enumerate(perf_rows):
        x = left + group_w * i + group_w / 2
        for off, val, color in [(-bar_w-2, r["A_ipc"], "#d97706"), (2, r["B_ipc"], "#2563eb")]:
            h = (base-top) * val / max_ipc
            svg2.append(f'<rect x="{x+off:.1f}" y="{base-h:.1f}" width="{bar_w}" height="{h:.1f}" fill="{color}"/>')
        svg2.append(f'<text x="{x:.1f}" y="{base+18}" text-anchor="middle" class="label">{r["simpoint"]}</text>')
        svg2.append(f'<text x="{x:.1f}" y="{base+33}" text-anchor="middle" class="label">{r["weight"]:.3f}</text>')
    svg2 += ['<rect x="910" y="45" width="12" height="12" fill="#d97706"/><text x="928" y="55" class="label">A THP on</text>', '<rect x="1000" y="45" width="12" height="12" fill="#2563eb"/><text x="1018" y="55" class="label">B THP off</text>', '<text x="90" y="615" class="label">x-axis labels: simpoint id / slice weight; y-axis: IPC</text>', '</svg>']
    (OUT / "performance_ipc.svg").write_text("\n".join(svg2))

    # HTML dashboard: sortable-enough static tables and inline SVG.
    perf_table = "".join(f'<tr><td>{html.escape(r["slice"])}</td><td>{r["A_cycle_cnt"]:,}</td><td>{r["B_cycle_cnt"]:,}</td><td>{pct_text(r["cycle_delta_pct"])}</td><td>{r["A_ipc"]:.6f}</td><td>{r["B_ipc"]:.6f}</td><td>{pct_text(r["ipc_delta_pct"])}</td><td>{r["A_host_time_ms"]:,}</td><td>{r["B_host_time_ms"]:,}</td><td>{pct_text(r["host_delta_pct"])}</td></tr>' for r in perf_rows)
    counter_table = "".join(f'<tr><td>{html.escape(key)}</td><td>{n}</td><td>{a:,.0f}</td><td>{b:,.0f}</td><td>{med:+.2f}%</td><td>{mean:+.2f}%</td></tr>' for _, med, key, n, mean, a, b in top_aggregate)
    dashboard = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>mcf THP A/B 性能对比</title><style>body{{font-family:Arial,sans-serif;margin:32px;color:#243447}}h1{{margin-bottom:4px}}h2{{margin-top:34px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d8dee4;padding:6px 8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#f1f5f9}}.note{{color:#52606d}}img{{max-width:100%;border:1px solid #e5e7eb}}</style><h1>mcf 性能对比：A（开启 THP） vs B（未开启 THP）</h1><p class="note">每个切片使用 simulator_err.txt 的第二次（最终）性能计数器快照；重复计数器实例以 <code>#instance_N</code> 保留；完整计数器见 counter_comparison.csv。</p><img src="performance_cycles.svg" alt="cycle comparison"><img src="performance_ipc.svg" alt="IPC comparison"><h2>逐切片性能</h2><table><tr><th>切片</th><th>A cycles</th><th>B cycles</th><th>B vs A cycles</th><th>A IPC</th><th>B IPC</th><th>B vs A IPC</th><th>A host ms</th><th>B host ms</th><th>B vs A host</th></tr>{perf_table}</table><h2>跨切片重点计数器变化（按汇总原始计数的相对变化排序）</h2><table><tr><th>计数器</th><th>覆盖切片数</th><th>A 汇总</th><th>B 汇总</th><th>汇总 B-A</th><th>各切片变化平均值</th></tr>{counter_table}</table></html>'''
    (OUT / "dashboard.html").write_text(dashboard)

    # Markdown report with concise conclusions and all slice rows.
    cycle_d = [r["cycle_delta_pct"] for r in perf_rows]
    ipc_d = [r["ipc_delta_pct"] for r in perf_rows]
    host_d = [r["host_delta_pct"] for r in perf_rows]
    faster = sum(x < 0 for x in cycle_d)
    wsum = sum(r["weight"] for r in perf_rows)
    weighted_cycle = sum(r["weight"] * r["cycle_delta_pct"] for r in perf_rows) / wsum
    weighted_ipc = sum(r["weight"] * r["ipc_delta_pct"] for r in perf_rows) / wsum
    weighted_host = sum(r["weight"] * r["host_delta_pct"] for r in perf_rows) / wsum
    report = ["# mcf A/B 性能对比报告", "", "- **A：开启 THP**；**B：未开启 THP**。", "- 对比对象：13 个同名 SimPoint 切片，按目录名中的 `mcf_<simpoint>_<weight>` 配对。", "- 计数器：每个日志有两次 `[PERF]` dump，本报告使用与终止周期对应的第二次/最终快照。", "- `[PERF]` 计数器在 warmup 后被 reset；因此计数器表示最终测量窗口，而 `simulator_out.txt` 的 cycle/IPC 表示完整的 40M 指令运行，两者不能直接相加。", "- 对于日志中重复的层级路径/计数器名，使用 `#instance_N` 保留每个实例，避免覆盖；实例按日志出现顺序与另一配置对齐。", "- `delta` 定义为 `B - A`；百分比定义为 `(B-A)/|A|`，因此周期/主机时间为负表示 B 更快，IPC 为正表示 B 更高。", "", "## 总体结论", "", f"- B 相比 A 在 **{faster}/{len(names)}** 个切片周期数更低；周期变化中位数为 **{statistics.median(cycle_d):+.2f}%**，按 SimPoint 权重加权为 **{weighted_cycle:+.2f}%**。", f"- IPC 变化中位数为 **{statistics.median(ipc_d):+.2f}%**（加权 **{weighted_ipc:+.2f}%**）；主机时间变化中位数为 **{statistics.median(host_d):+.2f}%**（加权 **{weighted_host:+.2f}%**）。", "- 由于每个切片均固定执行约 4,000 万条指令，周期数和 IPC 是主要的客观性能指标；主机时间受仿真器/宿主机负载影响，作为辅助指标。", "", "## 逐切片性能", "", "| 切片 | 权重 | A cycles | B cycles | cycles B-A | A IPC | B IPC | IPC B-A | A host ms | B host ms | host B-A |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in perf_rows:
        report.append(f'| `{r["slice"]}` | {r["weight"]:.6f} | {r["A_cycle_cnt"]:,} | {r["B_cycle_cnt"]:,} | {pct_text(r["cycle_delta_pct"])} | {r["A_ipc"]:.6f} | {r["B_ipc"]:.6f} | {pct_text(r["ipc_delta_pct"])} | {r["A_host_time_ms"]:,} | {r["B_host_time_ms"]:,} | {pct_text(r["host_delta_pct"])} |')
    report += ["", "## 重点性能计数器", "", "下表先跨切片汇总原始计数，再计算 `B-A` 的相对变化，并按变化绝对值排序；过滤了总量低于 1,000 的稀疏计数器。完整逐切片、逐计数器数据在 `counter_comparison.csv`。", "", "| 计数器 | 覆盖切片数 | A 汇总 | B 汇总 | 汇总相对变化 | 各切片变化平均值 |", "|---|---:|---:|---:|---:|---:|"]
    for _, med, key, n, mean, a, b in top_aggregate:
        report.append(f"| `{key}` | {n} | {a:,.0f} | {b:,.0f} | {med:+.2f}% | {mean:+.2f}% |")
    report += ["", "## 文件说明", "", "- `slice_performance.csv`：逐切片周期、IPC、主机时间及变化百分比。", "- `counter_comparison.csv`：逐切片完整性能计数器 A/B 原值、差值和相对变化。", "- `performance_cycles.svg`：周期数柱状图（橙色 A/THP on，蓝色 B/THP off）。", "- `performance_ipc.svg`：IPC 柱状图。", "- `dashboard.html`：浏览器可直接打开的汇总可视化。", ""]
    (OUT / "report.md").write_text("\n".join(report))
    print(f"slices={len(names)} counters={len(counter_rows)} aggregate={len(top_aggregate)} output={OUT}")


if __name__ == "__main__":
    main()
