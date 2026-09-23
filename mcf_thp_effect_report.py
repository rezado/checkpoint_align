#!/usr/bin/env python3
"""Re-label the mcf comparison as A=THP off, B=THP on and explain the delta."""
from __future__ import annotations

import csv
import html
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "mcf_thp_analysis"
OUT = ROOT / "mcf_thp_effect_analysis"


def fnum(value):
    return float(value) if value not in (None, "") else None


def pct(a, b):
    if a == 0:
        return None if b == 0 else math.inf
    return (b - a) / abs(a) * 100.0


def pct_text(value):
    if value is None:
        return "0.00%"
    if math.isinf(value):
        return "+inf%" if value > 0 else "-inf%"
    return f"{value:+.2f}%"


def metric_name(counter):
    return counter.split("#instance_", 1)[0].rsplit(":", 1)[-1]


def svg_chart(rows, field_a, field_b, mode_a, mode_b, metric, path):
    W, H = 1120, 640
    left, right, top, bottom = 90, 30, 60, 90
    vals = [max(float(r[field_a]), float(r[field_b])) for r in rows]
    scale = max(vals) or 1
    bar_w = 13
    group_w = (W - left - right) / len(rows)
    base = H - bottom
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">', '<rect width="100%" height="100%" fill="#ffffff"/>', '<style>text{font-family:Arial,sans-serif;fill:#243447}.axis{stroke:#7b8794;stroke-width:1}.grid{stroke:#e5e7eb;stroke-width:1}.title{font-size:20px;font-weight:700}.label{font-size:11px}</style>', f'<text x="90" y="30" class="title">mcf per-slice {metric}: A THP off vs B THP on</text>']
    for i in range(5):
        y = top + i * (base - top) / 4
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W-right}" y2="{y:.1f}" class="grid"/>')
        tick = scale * (4 - i) / 4
        tick_text = f"{tick:.2f}" if metric == "IPC" else f"{tick/1e6:.1f}M"
        out.append(f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" class="label">{tick_text}</text>')
    out.append(f'<line x1="{left}" y1="{base}" x2="{W-right}" y2="{base}" class="axis"/>')
    for i, row in enumerate(rows):
        x = left + group_w * i + group_w / 2
        for off, value, color in [(-bar_w - 2, float(row[field_a]), "#64748b"), (2, float(row[field_b]), "#dc2626")]:
            height = (base - top) * value / scale
            out.append(f'<rect x="{x+off:.1f}" y="{base-height:.1f}" width="{bar_w}" height="{height:.1f}" fill="{color}"/>')
        out.append(f'<text x="{x:.1f}" y="{base+18}" text-anchor="middle" class="label">{row["simpoint"]}</text>')
        out.append(f'<text x="{x:.1f}" y="{base+33}" text-anchor="middle" class="label">{float(row["weight"]):.3f}</text>')
    out += [f'<rect x="850" y="45" width="12" height="12" fill="#64748b"/><text x="868" y="55" class="label">A {mode_a}</text>', f'<rect x="1000" y="45" width="12" height="12" fill="#dc2626"/><text x="1018" y="55" class="label">B {mode_b}</text>', f'<text x="90" y="615" class="label">x-axis: simpoint id / slice weight; y-axis: {metric}</text>', '</svg>']
    path.write_text("\n".join(out))


def main():
    OUT.mkdir(exist_ok=True)
    old_perf = list(csv.DictReader((SOURCE / "slice_performance.csv").open()))
    old_counters = list(csv.DictReader((SOURCE / "counter_comparison.csv").open()))
    perf = []
    for old in old_perf:
        # Source files used A=THP on and B=THP off. Swap both labels and values.
        row = {"slice": old["slice"], "simpoint": old["simpoint"], "weight": old["weight"], "A_mode": "THP off", "B_mode": "THP on"}
        for key in ("instr_cnt", "cycle_cnt", "ipc", "host_time_ms"):
            row[f"A_{key}"] = old[f"B_{key}"]
            row[f"B_{key}"] = old[f"A_{key}"]
        row["cycle_delta_pct"] = pct(float(row["A_cycle_cnt"]), float(row["B_cycle_cnt"]))
        row["ipc_delta_pct"] = pct(float(row["A_ipc"]), float(row["B_ipc"]))
        row["host_delta_pct"] = pct(float(row["A_host_time_ms"]), float(row["B_host_time_ms"]))
        row["A_perf_time"] = old["B_perf_time"]
        row["B_perf_time"] = old["A_perf_time"]
        perf.append(row)

    counters = []
    for old in old_counters:
        av, bv = fnum(old["B_thp_off"]), fnum(old["A_thp_on"])
        d = None if av is None or bv is None else bv - av
        dp = None if av is None or bv is None else pct(av, bv)
        counters.append({"slice": old["slice"], "simpoint": old["simpoint"], "weight": old["weight"], "counter": old["counter"], "A_thp_off": av, "B_thp_on": bv, "delta_B_on_minus_A_off": d, "delta_pct": dp})

    with (OUT / "slice_performance.csv").open("w", newline="") as f:
        fields = list(perf[0]); writer = csv.DictWriter(f, fields); writer.writeheader(); writer.writerows(perf)
    with (OUT / "counter_comparison.csv").open("w", newline="") as f:
        fields = ["slice", "simpoint", "weight", "counter", "A_thp_off", "B_thp_on", "delta_B_on_minus_A_off", "delta_pct"]
        writer = csv.DictWriter(f, fields); writer.writeheader(); writer.writerows(counters)

    weighted = {}
    total_weight = sum(float(r["weight"]) for r in perf)
    for metric in ("cycle_cnt", "ipc", "host_time_ms"):
        a = sum(float(r["weight"]) * float(r[f"A_{metric}"]) for r in perf) / total_weight
        b = sum(float(r["weight"]) * float(r[f"B_{metric}"]) for r in perf) / total_weight
        weighted[metric] = (a, b, pct(a, b))

    # High-signal counters used to form a causal evidence chain.
    specs = [
        ("TLB misses", "tlb_miss", "address-translation pressure"),
        ("TLB requests", "tlb_req_count", "address-translation pressure"),
        ("Page-table-walk requests", "ptw_req_count", "page-table-walk traffic"),
        ("Page-table-walk responses", "ptw_resp_count", "page-table-walk traffic"),
        ("Superpage cache hits", "sp_hit", "superpage-cache activity"),
        ("Superpage cache refills", "spRefill", "superpage-cache activity"),
        ("PTW-originated L2 misses", "E2_L2AReqSource_PTW_Miss", "page-table-walk traffic"),
        ("L1 demand misses", "l1DemandMiss", "data-cache pressure"),
        ("L2 demand misses", "l2demandMiss", "data-cache pressure"),
        ("D-cache misses", "dcache_miss", "data-cache pressure"),
        ("Memory cycles", "mem_cycle", "memory-system activity"),
        ("Load L2 stalls", "LoadL2Stall", "load latency"),
        ("Load L3 stalls", "LoadL3Stall", "load latency"),
        ("Load memory stalls", "LoadMemStall", "load latency"),
        ("Memory-not-ready stalls", "MemNotReadyStall", "backend readiness"),
        ("Backend stall cycles", "backend_stall_cycle", "backend readiness"),
        ("Execute stall cycles", "exec_stall_cycle", "backend readiness"),
        ("Fetch IFU-not-ready stalls", "stallCycles_fetch_ifuNotReady", "front-end readiness"),
        ("Instruction-buffer-full stalls", "stallCycles_ibufferFull", "front-end readiness"),
        ("IQ stall cycles", "stall_cycle_iq", "front-end/backend queues"),
        ("Decode-full stalls", "stallCycles_decodeFull", "front-end/backend queues"),
        ("Branch mispredicts", "branchMispredicts", "control flow"),
    ]
    cause_rows = []
    for label, metric, category in specs:
        matched = [r for r in counters if metric_name(r["counter"]) == metric and r["A_thp_off"] is not None and r["B_thp_on"] is not None]
        if not matched:
            continue
        a = sum(float(r["A_thp_off"]) for r in matched); b = sum(float(r["B_thp_on"]) for r in matched)
        cause_rows.append({"category": category, "label": label, "metric": metric, "slices": len(set(r["slice"] for r in matched)), "A_thp_off_sum": a, "B_thp_on_sum": b, "delta_pct": pct(a, b)})
    with (OUT / "cause_metrics.csv").open("w", newline="") as f:
        fields = list(cause_rows[0]); writer = csv.DictWriter(f, fields); writer.writeheader(); writer.writerows(cause_rows)

    svg_chart(perf, "A_cycle_cnt", "B_cycle_cnt", "THP off", "THP on", "cycle count", OUT / "performance_cycles.svg")
    # IPC needs a separate scale; use the same chart helper with values in raw units.
    svg_chart(perf, "A_ipc", "B_ipc", "THP off", "THP on", "IPC", OUT / "performance_ipc.svg")

    cycles = [r["cycle_delta_pct"] for r in perf]; ipcs = [r["ipc_delta_pct"] for r in perf]; hosts = [r["host_delta_pct"] for r in perf]
    slower = sum(x > 0 for x in cycles)
    report = ["# mcf THP 性能影响分析（配置标注已对调）", "", "- **A：未开启 THP**（原始目录 `mcf_B`）。", "- **B：开启 THP**（原始目录 `mcf_A`）。", "- 本报告的变化量统一为 `B - A`，即“开启 THP 相对未开启 THP”。正的周期/停顿变化表示 THP 开启后增加，正的 IPC 变化表示 THP 开启后提升。", "- 计数器来自每个切片的最终 `[PERF]` 快照；warmup 后计数器已 reset；重复的路径/计数器实例保留为 `#instance_N`。", "", "## 结论", "", f"- THP 开启后，**{slower}/{len(perf)} 个切片周期数增加**；周期变化中位数 **{statistics.median(cycles):+.2f}%**，按 SimPoint 权重加权 **{weighted['cycle_cnt'][2]:+.2f}%**。", f"- IPC 变化中位数 **{statistics.median(ipcs):+.2f}%**，按权重加权 **{weighted['ipc'][2]:+.2f}%**；主机仿真时间按权重变化 **{weighted['host_time_ms'][2]:+.2f}%**（仅作辅助参考）。", "- 这不是“THP 让所有地址转换指标都变差”：THP 显著减少了 TLB miss 和页表遍历，但 superpage cache refill 很高；本次 workload 的总执行时间主要受后端/访存就绪和 L2 访问行为影响，地址转换收益没有抵消这些代价。", "", "## 归因边界", "", "- 这组数据支持“THP 开启时出现了以下计数器变化”的结论，但还不能把全部周期差异严格归因于 THP 本身：原始日志的随机 seed 不同，且两组日志的 emulator 编译时间/参考模型路径不同。", "- 输入镜像路径也不同：原始 `mcf_A` 使用路径名含 `thp_g` 的 checkpoint，原始 `mcf_B` 使用路径名含 `novec_g` 的 checkpoint。因此，若要做严格因果结论，应使用同一 emulator build、同一 seed/重复多 seed，并只切换 THP 配置后重复运行。", "", "## 为什么 THP 开启后仍可能变慢", "", "1. **地址转换收益明确存在，但不是关键瓶颈。** TLB miss、TLB request 和 PTW request/response 均下降，说明大页减少了页表层级遍历。", "2. **superpage cache 仍承受高 refill 压力。** THP on 下 superpage hit 和 refill 同时达到高位；应先确认最终 elaborated 的 `spSize`，再做容量和 page-level 分区扫描。", "3. **数据访问行为发生变化。** L1 demand miss 增加，而 L2 demand miss 下降；这说明 THP 改变了页/地址布局和缓存索引映射，必须结合 L2 set/slice、MSHR 和 latency 数据判断。", "4. **后端等待成为主要代价。** `MemNotReadyStall`、`LoadL2Stall` 等计数器增加；这些 top-down 计数器不是可加的延迟分解，当前证据支持关键 load 就绪/依赖时序变差。", "5. **不是分支预测导致。** branch mispredicts 下降，性能下降更像是内存层级/后端就绪状态变化，而不是控制流恶化。", "", "## 关键计数器", "", "| 类别 | 计数器 | 覆盖切片 | A 未开启 THP | B 开启 THP | THP 开启相对变化 |", "|---|---|---:|---:|---:|---:|"]
    for r in cause_rows:
        report.append(f'| {r["category"]} | `{r["metric"]}` | {r["slices"]} | {r["A_thp_off_sum"]:,.0f} | {r["B_thp_on_sum"]:,.0f} | {pct_text(r["delta_pct"])} |')
    report += ["", "## 逐切片性能", "", "| 切片 | 权重 | A cycles | B cycles | THP 开启周期变化 | A IPC | B IPC | THP 开启 IPC 变化 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in perf:
        report.append(f'| `{r["slice"]}` | {float(r["weight"]):.6f} | {int(float(r["A_cycle_cnt"])):,} | {int(float(r["B_cycle_cnt"])):,} | {pct_text(r["cycle_delta_pct"])} | {float(r["A_ipc"]):.6f} | {float(r["B_ipc"]):.6f} | {pct_text(r["ipc_delta_pct"])} |')
    report += ["", "## 文件", "", "- `cause_metrics.csv`：用于解释性能变化的关键计数器汇总。", "- `counter_comparison.csv`：全部 457,080 条逐切片计数器记录，A=THP off、B=THP on。", "- `slice_performance.csv`：对调后的逐切片周期、IPC、主机时间。", "- `performance_cycles.svg`、`performance_ipc.svg`：对调标注后的可视化。", ""]
    (OUT / "report.md").write_text("\n".join(report))

    perf_table = "".join(f'<tr><td>{html.escape(r["slice"])}</td><td>{int(float(r["A_cycle_cnt"])):,}</td><td>{int(float(r["B_cycle_cnt"])):,}</td><td>{pct_text(r["cycle_delta_pct"])}</td><td>{float(r["A_ipc"]):.6f}</td><td>{float(r["B_ipc"]):.6f}</td><td>{pct_text(r["ipc_delta_pct"])}</td></tr>' for r in perf)
    cause_table = "".join(f'<tr><td>{html.escape(r["category"])}</td><td>{html.escape(r["metric"])}</td><td>{r["A_thp_off_sum"]:,.0f}</td><td>{r["B_thp_on_sum"]:,.0f}</td><td>{pct_text(r["delta_pct"])}</td></tr>' for r in cause_rows)
    dashboard = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>mcf THP effect analysis</title><style>body{{font-family:Arial,sans-serif;margin:32px;color:#243447}}h1{{margin-bottom:4px}}h2{{margin-top:32px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d8dee4;padding:6px 8px;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#f1f5f9}}.note{{color:#52606d}}img{{max-width:100%;border:1px solid #e5e7eb}}</style><h1>mcf THP 影响分析：A 未开启 vs B 开启</h1><p class="note">所有变化均为 B（开启 THP）相对 A（未开启 THP）；计数器来自最终快照，重复实例保留。</p><img src="performance_cycles.svg" alt="cycle comparison"><img src="performance_ipc.svg" alt="IPC comparison"><h2>逐切片性能</h2><table><tr><th>切片</th><th>A cycles</th><th>B cycles</th><th>THP 开启周期变化</th><th>A IPC</th><th>B IPC</th><th>THP 开启 IPC 变化</th></tr>{perf_table}</table><h2>关键计数器</h2><table><tr><th>类别</th><th>计数器</th><th>A 未开启 THP</th><th>B 开启 THP</th><th>变化</th></tr>{cause_table}</table><p class="note">详细解释见 report.md；完整数据见 counter_comparison.csv。</p></html>'''
    (OUT / "dashboard.html").write_text(dashboard)
    print(f"slices={len(perf)} counters={len(counters)} cause_metrics={len(cause_rows)} output={OUT}")


if __name__ == "__main__":
    main()
