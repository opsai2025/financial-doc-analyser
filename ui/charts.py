"""Plotly figures of the results dashboard (D-13.7): v1's seven charts plus the overview distributions.

Vazirmatn everywhere (the font is served by the app, see .streamlit/config.toml); years on axes
in Persian digits (D-9.8); v1's meaning kept (median trend lines, stacked bars, sunbursts, heatmap).
"""

from __future__ import annotations

from collections import Counter
from statistics import median

import plotly.graph_objects as go

from core.fa import fa_digits, fa_number
from services.dashboard import STATUSES

FONT = "Vazirmatn, Tahoma, sans-serif"
INK, GRID = "#0f172a", "#e2e8f0"
RISK_COLORS = ("#16a34a", "#eab308", "#ea580c", "#dc2626")
OPINION_COLORS = ("#16a34a", "#d97706", "#dc2626", "#64748b")
CATEGORY_COLORS = ("#3498db", "#e74c3c", "#2ecc71", "#f39c12", "#9b59b6", "#1abc9c", "#e67e22", "#34495e")
HEATMAP_COLORS = ("#E0E7FF", "#8EA4E9", "#A970DE", "#F46E52")  # v1's four status colours
CONFIG = {"displaylogo": False, "modeBarButtonsToRemove": ["select2d", "lasso2d"]}


def _style(fig: go.Figure, height: int = 420, legend_title: str | None = None) -> go.Figure:
    fig.update_layout(
        height=height, font={"family": FONT, "size": 13, "color": INK},
        margin={"t": 20, "r": 20, "b": 40, "l": 20}, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#ffffff",
        hoverlabel={"font": {"family": FONT, "size": 13}, "align": "right"},
        legend={"title": {"text": legend_title or ""}, "font": {"family": FONT}, "traceorder": "normal"},
    )
    fig.update_xaxes(gridcolor=GRID, zeroline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def _year_axis(fig: go.Figure, years: list[int]) -> None:
    fig.update_xaxes(tickmode="array", tickvals=years, ticktext=[fa_digits(y) for y in years], title="سال مالی")


def trend(points: list[tuple[int, int]], scale: tuple[str, ...], color: str, axis_title: str) -> go.Figure:
    """Per-year median (rounded like v1) as the line; every document as a faint dot."""
    by_year: dict[int, list[int]] = {}
    for year, value in points:
        by_year.setdefault(year, []).append(value)
    years = sorted(by_year)
    medians = [int(round(median(by_year[y]))) for y in years]
    fig = go.Figure()
    fig.add_scatter(x=[p[0] for p in points], y=[p[1] for p in points], mode="markers", showlegend=False,
                    marker={"size": 11, "color": color, "opacity": 0.2}, hoverinfo="skip")
    fig.add_scatter(x=years, y=medians, mode="lines+markers", showlegend=False, line={"width": 3, "color": color},
                    marker={"size": 13, "color": color}, customdata=[scale[m] for m in medians],
                    text=[fa_digits(y) for y in years], hovertemplate="%{text}: %{customdata}<extra></extra>")
    fig.update_yaxes(tickmode="array", tickvals=list(range(len(scale))), ticktext=list(scale),
                     range=[-0.5, len(scale) - 0.5], title=axis_title, side="right")
    _year_axis(fig, years)
    return _style(fig, 380)


def category_colors(categories: tuple[str, ...], seen) -> dict[str, str]:
    """One fixed colour per main category, shared by the bar chart and the sunburst (D-14.11).

    Schema order first (stable across companies), then anything else the model wrote.
    """
    order = list(categories) + sorted(set(seen) - set(categories))
    return {name: CATEGORY_COLORS[index % len(CATEGORY_COLORS)] for index, name in enumerate(order)}


def _lighter(color: str, amount: float = 0.45) -> str:
    """Blend a #rrggbb colour with white (sunburst outer ring)."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(c + (255 - c) * amount):02x}" for c in (r, g, b))


def _wrap(text: str, width: int = 18) -> str:
    """Full label on several lines (D-14.11); Plotly hides it only where it doesn't fit."""
    lines, line = [], ""
    for word in text.replace("<", "‹").replace(">", "›").split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return "<br>".join(lines)


def stacked_bars(rows: list[dict], category: str, categories: tuple[str, ...], legend_title: str,
                 axis_title: str) -> go.Figure:
    """Item count per year, stacked by category (schema order first, then anything else the model wrote)."""
    counts = Counter((r["year"], r.get(category) or "نامشخص") for r in rows if r.get("year") is not None)
    years = sorted({y for y, _ in counts})
    seen = {c for _, c in counts}
    colors = category_colors(categories, seen)
    fig = go.Figure()
    for name in (c for c in colors if c in seen):
        values = [counts.get((y, name), 0) for y in years]
        fig.add_bar(x=years, y=values, name=name, marker_color=colors[name],
                    text=[fa_number(v) if v else "" for v in values], textposition="inside",
                    insidetextfont={"color": "#ffffff", "family": FONT},
                    hovertemplate=f"{name}: " + "%{y}<extra></extra>")
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title=axis_title, side="right", dtick=1, tickformat="d")
    _year_axis(fig, years)
    fig = _style(fig, 500, legend_title)
    # Below the plot: on the right it would cover the (right-hand) value axis
    fig.update_layout(legend={"orientation": "h", "x": 1, "xanchor": "right", "y": -0.22, "yanchor": "top"},
                      margin={"b": 120})
    return fig


def sunburst(rows: list[dict], category: str, subcategory: str, root: str,
             categories: tuple[str, ...] = ()) -> go.Figure:
    """Category → sub-category, coloured like the bar chart; outer ring in a lighter shade (D-14.11)."""
    pairs = Counter((r.get(category) or "نامشخص", r.get(subcategory) or "نامشخص") for r in rows)
    totals = Counter()
    for (cat, _), n in pairs.items():
        totals[cat] += n
    colors = category_colors(categories, totals)
    ids, labels, parents, values, fills = [root], [root], [""], [sum(totals.values())], ["#f1f5f9"]
    for cat, n in totals.items():
        ids.append(f"c|{cat}")
        labels.append(cat)
        parents.append(root)
        values.append(n)
        fills.append(colors[cat])
    for (cat, sub), n in pairs.items():
        ids.append(f"s|{cat}|{sub}")
        labels.append(sub)
        parents.append(f"c|{cat}")
        values.append(n)
        fills.append(_lighter(colors[cat]))
    fig = go.Figure(go.Sunburst(
        ids=ids, labels=labels, parents=parents, values=values, branchvalues="total",
        text=[_wrap(label) for label in labels], texttemplate="%{text}", insidetextorientation="auto",
        marker={"colors": fills, "line": {"color": "#ffffff", "width": 2}},
        hovertemplate="<b>%{label}</b><br>تعداد: %{value}<br>نسبت: %{percentRoot:.1%}<extra></extra>",
    ))
    fig.update_layout(uniformtext={"minsize": 10, "mode": "hide"})
    return _style(fig, 500)


def status_heatmap(topics: list[str], years: list[int], matrix: list[list[int]]) -> go.Figure:
    """Checklist topic × year coloured by v1's four statuses (worst per cell)."""
    steps = []
    for index, color in enumerate(HEATMAP_COLORS):
        steps += [[index / 4, color], [(index + 1) / 4, color]]
    fig = go.Figure(go.Heatmap(
        z=matrix, x=[fa_digits(y) for y in years], y=topics, zmin=-0.5, zmax=3.5, colorscale=steps,
        xgap=3, ygap=3, customdata=[[STATUSES[v] for v in row] for row in matrix],
        hovertemplate="%{y} — %{x}<br>%{customdata}<extra></extra>",
        colorbar={"tickvals": [0, 1, 2, 3], "ticktext": list(STATUSES), "orientation": "h", "x": 0.5,
                  "xanchor": "center", "y": -0.08, "yanchor": "top", "thickness": 14, "tickangle": 0,
                  "tickfont": {"size": 11},
                  "len": 1, "lenmode": "fraction"},
    ))
    fig.update_yaxes(autorange="reversed", side="right", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(title="سال مالی", type="category", gridcolor="rgba(0,0,0,0)")
    fig = _style(fig, max(360, 32 * len(topics) + 170))
    fig.update_layout(margin={"b": 110})
    return fig


def distribution(counts: dict[str, int], colors: tuple[str, ...]) -> go.Figure:
    labels = list(counts)
    values = [counts[k] for k in labels]
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=list(colors[:len(labels)]),
                           text=[fa_number(v) for v in values], textposition="outside",
                           hovertemplate="%{x}: %{y}<extra></extra>"))
    fig.update_yaxes(title="تعداد سند", side="right", dtick=1, tickformat="d")
    return _style(fig, 320)


def risk_distribution(counts: dict[str, int]) -> go.Figure:
    return distribution(counts, RISK_COLORS)


def opinion_distribution(counts: dict[str, int]) -> go.Figure:
    return distribution(counts, OPINION_COLORS)


# v1's chart explanations (collapsed under each chart, D-13.7)
EXPLAIN = {
    "risk_trend": [
        "**محور عمودی:** سطح ریسک کلی (پایین، متوسط، بالا، بحرانی)؛ **محور افقی:** سال مالی.",
        "خط پررنگ: میانه سطح ریسک اسناد هر سال؛ نقطه‌های کم‌رنگ: تک‌تک اسناد.",
        "روند رو به بالا یعنی افزایش ریسک؛ رو به پایین یعنی بهبود کنترل‌ها و کاهش ریسک؛ ثبات یعنی وضعیت پایدار.",
    ],
    "opinion_trend": [
        "**محور عمودی:** نوع اظهارنظر (مقبول، مشروط، مردود، عدم اظهارنظر)؛ **محور افقی:** سال مالی.",
        "**مقبول:** صورت‌های مالی عاری از تحریف بااهمیت؛ **مشروط:** محدودیت یا انحراف قابل توجه؛ "
        "**مردود:** عدم انطباق با استانداردها؛ **عدم اظهارنظر:** عدم امکان رسیدگی کافی.",
    ],
    "risk_bars": [
        "**ستون‌ها:** تعداد ریسک‌های برجسته‌شده (بند تأکید بر مطالب خاص) در هر سال؛ **رنگ‌ها:** دسته اصلی ریسک.",
        "افزایش ارتفاع ستون یعنی افزایش تعداد ریسک‌ها؛ تغییر ترکیب رنگ‌ها یعنی تغییر نوع ریسک‌های غالب.",
        "افزایش ناگهانی یک دسته ریسک نیاز به توجه ویژه دارد.",
    ],
    "risk_sunburst": [
        "**مرکز:** همه ریسک‌های برجسته؛ **حلقه داخلی:** دسته اصلی؛ **حلقه بیرونی:** زیرشاخه.",
        "اندازه هر بخش متناسب با تعداد موارد است؛ با کلیک روی هر بخش بزرگ‌نمایی می‌شود.",
    ],
    "legal_bars": [
        "**ستون‌ها:** تعداد موارد عدم رعایت الزامات قانونی در هر سال؛ **رنگ‌ها:** دسته اصلی تخلف.",
        "افزایش تخلفات نشانه ضعف در رعایت الزامات است؛ تکرار در یک دسته نیاز به اقدام اصلاحی اساسی دارد.",
    ],
    "legal_sunburst": [
        "**مرکز:** همه تخلفات؛ **حلقه داخلی:** دسته اصلی قوانین؛ **حلقه بیرونی:** زیرشاخه.",
        "تمرکز تخلفات در یک دسته (مثلاً الزامات بانک مرکزی) نشان‌دهنده نیاز به توجه ویژه به آن حوزه است.",
    ],
    "heatmap": [
        "**ردیف‌ها:** موضوعات کلیدی چک‌لیست؛ **ستون‌ها:** سال مالی؛ **رنگ:** وضعیت موضوع در آن سال.",
        "به‌طور پیش‌فرض فقط موضوعاتی نمایش داده می‌شوند که دست‌کم در یک سال وضعیتی غیر از «مصداق ندارد» دارند.",
        "برای شناسایی موضوعات تکراری و روند بهبود یا بدتر شدن هر موضوع.",
    ],
}

