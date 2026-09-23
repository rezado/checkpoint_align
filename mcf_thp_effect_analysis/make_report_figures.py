#!/usr/bin/env python3
"""Generate report figures from the checked-in counter and performance CSVs.

The script intentionally uses only the Python standard library so the figures
can be regenerated in the minimal analysis environment.
"""
import csv
import math
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_csv(name):
    with (ROOT / name).open(newline="") as f:
        return list(csv.DictReader(f))


def pct(a, b):
    return None if a == 0 else 100.0 * (b - a) / a


def pearson(xs, ys, weights=None):
    if weights is None:
        weights = [1.0] * len(xs)
    sw = sum(weights)
    mx = sum(w * x for w, x in zip(weights, xs)) / sw
    my = sum(w * y for w, y in zip(weights, ys)) / sw
    cov = sum(w * (x - mx) * (y - my) for w, x, y in zip(weights, xs, ys))
    vx = sum(w * (x - mx) ** 2 for w, x in zip(weights, xs))
    vy = sum(w * (y - my) ** 2 for w, y in zip(weights, ys))
    return cov / math.sqrt(vx * vy) if vx and vy else float("nan")


def color(value, limit=100.0):
    """Blue-white-red diverging color, with the heatmap range clipped."""
    v = max(-limit, min(limit, value)) / limit
    if v < 0:
        t = v + 1.0
        r, g, b = int(45 + 210 * t), int(105 + 150 * t), 220
    else:
        t = 1.0 - v
        r, g, b = 220, int(105 + 150 * t), int(45 + 175 * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def text(x, y, value, size=12, anchor="start", weight="normal", fill="#243447", rotate=None):
    tr = f' transform="rotate({rotate} {x} {y})"' if rotate else ""
    return f'<text x="{x}" y="{y}" font-family="Arial,sans-serif" font-size="{size}px" text-anchor="{anchor}" font-weight="{weight}" fill="{fill}"{tr}>{escape(str(value))}</text>'


def load_data():
    perf = read_csv("slice_performance.csv")
    perf_by_slice = {r["slice"]: r for r in perf}
    cause = read_csv("cause_metrics.csv")
    wanted = [r["metric"] for r in cause]
    labels = {r["metric"]: r["label"] for r in cause}
    categories = {r["metric"]: r["category"] for r in cause}
    counters = read_csv("counter_comparison.csv")
    # Aggregate repeated counter instances within a slice before calculating a
    # percentage change. This preserves the report's stated instance policy.
    agg = {}
    for r in counters:
        # Raw names include the component path, e.g. ``dispatch:LoadL2Stall``.
        metric = r["counter"].rsplit(":", 1)[-1]
        if metric not in wanted:
            continue
        key = (r["slice"], metric)
        a, b = agg.get(key, (0.0, 0.0))
        agg[key] = (a + float(r["A_thp_off"]), b + float(r["B_thp_on"]))
    slices = [r["slice"] for r in perf]
    rows = []
    for metric in wanted:
        changes = []
        ipc = []
        weights = []
        for s in slices:
            a, b = agg.get((s, metric), (0.0, 0.0))
            changes.append(pct(a, b))
            pr = perf_by_slice[s]
            ipc.append(float(pr["ipc_delta_pct"]))
            weights.append(float(pr["weight"]))
        valid = [(x, y, w) for x, y, w in zip(changes, ipc, weights) if x is not None]
        xs, ys, ws = zip(*valid)
        rows.append({
            "metric": metric, "label": labels[metric], "category": categories[metric],
            "changes": changes, "ipc": ipc, "weights": weights,
            "r": pearson(xs, ys), "weighted_r": pearson(xs, ys, ws),
        })
    return slices, perf, rows


def heatmap(slices, rows):
    left, top, cell_w, cell_h = 245, 92, 61, 27
    width = left + cell_w * len(slices) + 35
    height = top + cell_h * len(rows) + 75
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>']
    out.append(text(18, 28, "THP 开启后性能计数器变化热力图（B - A）", 19, weight="bold"))
    out.append(text(18, 51, "按切片聚合重复实例；色阶截断为 ±100%，单元格数值为百分比。", 12, fill="#52606d"))
    for j, s in enumerate(slices):
        sim = s.split("_")[1]
        x = left + j * cell_w + cell_w / 2
        out.append(text(x, top - 10, sim, 10, anchor="middle", fill="#52606d", rotate=-55))
    for i, row in enumerate(rows):
        y = top + i * cell_h
        out.append(text(left - 8, y + 18, row["label"], 11, anchor="end"))
        for j, v in enumerate(row["changes"]):
            x = left + j * cell_w
            if v is None:
                fill, label = "#f3f4f6", "NA"
            else:
                fill, label = color(v), f"{v:+.0f}%"
            out.append(f'<rect x="{x}" y="{y}" width="{cell_w-1}" height="{cell_h-1}" fill="{fill}" stroke="white"/>')
            out.append(text(x + cell_w / 2, y + 17, label, 9, anchor="middle", fill="#17202a"))
    legend_y = height - 40
    for k, v in enumerate(range(-100, 101, 20)):
        x = left + k * 31
        out.append(f'<rect x="{x}" y="{legend_y}" width="31" height="12" fill="{color(v)}"/>')
        if v in (-100, 0, 100):
            out.append(text(x + 15, legend_y + 27, f"{v:+d}%", 10, anchor="middle", fill="#52606d"))
    out.append(text(left - 8, legend_y + 10, "图例", 10, anchor="end", fill="#52606d"))
    out.append("</svg>")
    (ROOT / "counter_delta_heatmap.svg").write_text("\n".join(out))


def scatter(slices, rows):
    selected = ["tlb_miss", "l1DemandMiss", "l2demandMiss", "LoadL2Stall", "MemNotReadyStall", "branchMispredicts"]
    by_metric = {r["metric"]: r for r in rows}
    panel_w, panel_h = 365, 235
    width, height = panel_w * 3, panel_h * 2
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           text(18, 25, "计数器变化与 IPC 变化的关系", 19, weight="bold"),
           text(18, 46, "每点为一个切片；r 为未加权 Pearson 相关系数，r_w 为 SimPoint 权重相关系数。", 12, fill="#52606d")]
    for n, metric in enumerate(selected):
        row = by_metric[metric]
        px, py = (n % 3) * panel_w, 55 + (n // 3) * panel_h
        x0, y0, pw, ph = px + 53, py + 38, 285, 145
        xs = [x for x in row["changes"] if x is not None]
        ys = row["ipc"]
        xmin, xmax = min(xs + [-100]), max(xs + [100])
        xmin, xmax = min(xmin, -10), max(xmax, 10)
        ymin, ymax = min(ys + [-20]), max(ys + [20])
        def tx(v): return x0 + (v - xmin) / (xmax - xmin) * pw
        def ty(v): return y0 + ph - (v - ymin) / (ymax - ymin) * ph
        out.append(text(px + 12, py + 19, row["label"], 13, weight="bold"))
        out.append(text(px + 12, py + 35, f"r={row['r']:+.2f}, r_w={row['weighted_r']:+.2f}", 11, fill="#52606d"))
        out.append(f'<line x1="{x0}" y1="{ty(0)}" x2="{x0+pw}" y2="{ty(0)}" stroke="#d1d5db"/>')
        out.append(f'<line x1="{tx(0)}" y1="{y0}" x2="{tx(0)}" y2="{y0+ph}" stroke="#d1d5db"/>')
        out.append(f'<rect x="{x0}" y="{y0}" width="{pw}" height="{ph}" fill="none" stroke="#9aa5b1"/>')
        out.append(text(x0 + pw / 2, y0 + ph + 25, "计数器变化 (%)", 10, anchor="middle", fill="#52606d"))
        out.append(text(x0 - 36, y0 + ph / 2, "IPC变化 (%)", 10, anchor="middle", fill="#52606d", rotate=-90))
        for s, x, y in zip(slices, row["changes"], row["ipc"]):
            if x is None: continue
            out.append(f'<circle cx="{tx(x):.1f}" cy="{ty(y):.1f}" r="4" fill="#1976a8" stroke="white" stroke-width="1"><title>{escape(s)}: counter {x:+.2f}%, IPC {y:+.2f}%</title></circle>')
    out.append("</svg>")
    (ROOT / "counter_ipc_relationship.svg").write_text("\n".join(out))


def main():
    slices, perf, rows = load_data()
    heatmap(slices, rows)
    scatter(slices, rows)
    with (ROOT / "counter_ipc_correlation.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "label", "category", "pearson_r", "weighted_pearson_r"])
        for r in rows:
            w.writerow([r["metric"], r["label"], r["category"], f'{r["r"]:.6f}', f'{r["weighted_r"]:.6f}'])
    print("generated counter_delta_heatmap.svg, counter_ipc_relationship.svg, counter_ipc_correlation.csv")
    for r in sorted(rows, key=lambda x: abs(x["weighted_r"]), reverse=True):
        print(f'{r["label"]}: r={r["r"]:+.3f}, r_w={r["weighted_r"]:+.3f}')


if __name__ == "__main__":
    main()
