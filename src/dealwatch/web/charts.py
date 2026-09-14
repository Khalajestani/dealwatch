"""Inline SVG charts, no JS libraries. Colours are CSS custom properties (`.viz` tokens in style.css) so light and dark
mode swap in one place. Layout follows the data-viz method: thin 2px lines, >=8px markers with a surface ring,
hairline gridlines, sparse end labels, legend for >=2 series, and a hover crosshair (static/tracker.js).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape


@dataclass
class Series:
    name: str
    points: list[tuple[datetime, float]]


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Clean tick values (1/2/2.5/5 x 10^n steps) covering [lo, hi]."""
    if hi <= lo:
        hi = lo + max(abs(lo) * 0.1, 0.01)
    raw = (hi - lo) / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        step = factor * magnitude
        if raw <= step:
            break
    value = math.floor(lo / step) * step
    ticks = []
    while True:  # always cover `hi` so no point sits above the top gridline
        ticks.append(round(value, 6))
        if value >= hi - 1e-9 or len(ticks) > 20:
            break
        value += step
    return ticks


def _fmt_money(value: float) -> str:
    return f"€{value:.4f}" if 0 < abs(value) < 0.1 else f"€{value:,.2f}"


def _fmt_date(value: datetime) -> str:
    return value.strftime("%d %b")


def line_chart(series: list[Series], *, chart_id: str, title: str, width: int = 760, height: int = 260, unit_suffix: str = "") -> str:
    """Time-on-x line chart for up to two series. Returns HTML: svg + legend + data for the hover layer."""
    series = [s for s in series if s.points][:2]
    if not series:
        return '<p class="muted small">Not enough data for a chart yet.</p>'
    all_points = [p for s in series for p in s.points]
    xs_dt = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    x_min, x_max = min(xs_dt), max(xs_dt)
    if x_min == x_max:  # a single check: centre it on a one-day span
        x_min, x_max = x_min - timedelta(hours=12), x_max + timedelta(hours=12)
    span_seconds = (x_max - x_min).total_seconds()
    ticks = nice_ticks(min(ys) * 0.95, max(ys) * 1.05)
    y_lo, y_hi = ticks[0], ticks[-1]

    left, right, top, bottom = 56, 78, 14, 30
    plot_w, plot_h = width - left - right, height - top - bottom

    def sx(dt: datetime) -> float:
        if span_seconds == 0:
            return left + plot_w / 2
        return left + (dt - x_min).total_seconds() / span_seconds * plot_w

    def sy(value: float) -> float:
        return top + (y_hi - value) / (y_hi - y_lo) * plot_h if y_hi > y_lo else top + plot_h / 2

    parts: list[str] = []
    parts.append(f'<svg class="viz-svg" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{escape(title)}" data-chart="{escape(chart_id)}">')
    # gridlines + y labels
    for tick in ticks:
        y = sy(tick)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" class="viz-grid"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 4:.1f}" class="viz-tick" text-anchor="end">{_fmt_money(tick)}</text>')
    # x labels: first, last and up to 3 between (unique dates)
    unique_dates = sorted({p[0] for p in all_points})
    label_points = unique_dates if len(unique_dates) <= 5 else [unique_dates[round(i * (len(unique_dates) - 1) / 4)] for i in range(5)]
    for dt in label_points:
        parts.append(f'<text x="{sx(dt):.1f}" y="{height - 8}" class="viz-tick" text-anchor="middle">{_fmt_date(dt)}</text>')
    parts.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="viz-axis"/>')
    parts.append('<line class="viz-crosshair" x1="0" x2="0" y1="%d" y2="%d" style="opacity:0"/>' % (top, top + plot_h))

    end_labels: list[tuple[float, float, str, int]] = []
    for idx, s in enumerate(series, start=1):
        pts = sorted(s.points)
        coords = [(sx(dt), sy(v)) for dt, v in pts]
        path = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
        parts.append(f'<path d="{path}" class="viz-line viz-s{idx}"/>')
        for x, y in coords:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" class="viz-dot viz-s{idx}"/>')
        end_labels.append((coords[-1][0], coords[-1][1], _fmt_money(pts[-1][1]) + unit_suffix, idx))
    # sparse direct labels: the last value of each series, skipping a label that would collide with the first
    placed: list[float] = []
    for x, y, text, idx in end_labels:
        if any(abs(y - other) < 14 for other in placed):
            continue
        placed.append(y)
        parts.append(f'<text x="{x + 8:.1f}" y="{y + 4:.1f}" class="viz-label">{escape(text)}</text>')
    parts.append("</svg>")

    legend = "".join(f'<span class="viz-key"><i class="viz-s{i}"></i>{escape(s.name)}</span>' for i, s in enumerate(series, start=1))
    data = {
        "left": left,
        "right": left + plot_w,
        "labels": [dt.strftime("%a %d %b %Y") for dt in unique_dates],
        "xs": [round(sx(dt), 1) for dt in unique_dates],
        "series": [
            {"name": s.name, "cls": f"viz-s{i}", "values": {dt.strftime("%a %d %b %Y"): _fmt_money(v) + unit_suffix for dt, v in s.points}}
            for i, s in enumerate(series, start=1)
        ],
    }
    return (
        f'<figure class="viz viz-line-chart" id="{escape(chart_id)}">'
        f'<figcaption class="viz-title">{escape(title)}</figcaption>'
        + "".join(parts)
        + f'<div class="viz-legend">{legend}</div>'
        + '<div class="viz-tooltip" hidden></div>'
        + f'<script type="application/json" class="viz-data">{json.dumps(data)}</script>'
        + "</figure>"
    )


def sparkline(values: list[float], *, width: int = 120, height: int = 28) -> str:
    """Tiny single-series line (no axes) for table rows; last point marked."""
    if len(values) < 2:
        return '<span class="muted small">-</span>'
    lo, hi = min(values), max(values)
    span = hi - lo or max(abs(hi) * 0.05, 0.01)
    pad = 4
    step = (width - 2 * pad) / (len(values) - 1)
    coords = [(pad + i * step, pad + (hi - v) / span * (height - 2 * pad)) for i, v in enumerate(values)]
    path = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    lx, ly = coords[-1]
    return (
        f'<svg class="viz-spark viz" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<path d="{path}" class="viz-spark-line"/><circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" class="viz-dot viz-s1"/></svg>'
    )
