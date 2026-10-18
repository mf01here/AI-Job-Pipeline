"""
Dashboard charts drawn as plain SVG on the server: no JavaScript library, no internet, works on the iPhone.
Colours come from the stylesheet's CSS variables, so the charts follow the theme.
"""
from __future__ import annotations

from html import escape

from markupsafe import Markup

MAGENTA, VIOLET, SUN, ICE, DIM = "var(--hot)", "var(--uv)", "var(--sun)", "var(--ice)", "var(--dim)"
GREEN, RED = "var(--ok)", "var(--bad)"


def _svg(w: int, h: int, body: str, label: str) -> Markup:
    return Markup(f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}" '
                  f'preserveAspectRatio="xMidYMid meet" class="chart">{body}</svg>')


def funnel(stages: list[tuple[str, int]]) -> Markup:
    """The pipeline itself: found -> approved -> resume -> applied -> interview -> offer.
    Built from HTML rows (not SVG) so the labels stay readable on a phone."""
    top = max([n for _, n in stages] + [1])
    colours = [DIM, VIOLET, VIOLET, GREEN, GREEN, GREEN]
    rows = []
    for i, (name, n) in enumerate(stages):
        pct = max(1.5, 100 * n / top)
        rows.append(f'<div class="f-row"><span class="f-lbl">{escape(name)}</span>'
                    f'<span class="f-track"><span class="f-bar" style="width:{pct:.1f}%;background:{colours[i]}"></span></span>'
                    f'<span class="f-num">{n}</span></div>')
    return Markup(f'<div class="funnel" role="img" aria-label="Pipeline funnel">{"".join(rows)}</div>')


def histogram(scores: list[float], gate_low: float = 40, gate_high: float = 60) -> Markup:
    """How well jobs match: score buckets, coloured by the gate they fall into."""
    w, h, pad = 420, 220, 6
    buckets = [0] * 10
    for s in scores:
        buckets[min(9, max(0, int((s or 0) // 10)))] += 1
    top = max(buckets + [1])
    bw = (w - pad) / 10
    out = []
    for i, n in enumerate(buckets):
        bh = (h - 72) * n / top
        x, y = pad + i * bw + 3, h - 24 - bh
        fill = GREEN if i * 10 >= gate_high else (SUN if i * 10 >= gate_low else RED)
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 6:.1f}" height="{bh:.1f}" rx="4" fill="{fill}"/>')
        if n:
            out.append(f'<text x="{x + (bw - 6) / 2:.1f}" y="{y - 5:.1f}" class="num" text-anchor="middle">{n}</text>')
        out.append(f'<text x="{x + (bw - 6) / 2:.1f}" y="{h - 7}" class="lbl" text-anchor="middle">{i * 10}</text>')
    for g, name in ((gate_low, "review"), (gate_high, "approve")):
        gx = pad + g / 10 * bw
        out.append(f'<line x1="{gx:.1f}" x2="{gx:.1f}" y1="20" y2="{h - 22}" class="gate"/>'
                   f'<text x="{gx + 4:.1f}" y="14" class="lbl">{name} {g:.0f}%</text>')
    return _svg(w, h, "".join(out), "Match score distribution")


def bars(items: list[tuple[str, int]], colour: str = ICE, label: str = "Bar chart") -> Markup:
    """Horizontal bars, e.g. jobs per source or approved jobs per company."""
    if not items:
        return Markup('<p class="empty">No data yet.</p>')
    w, row, gap = 420, 22, 9
    h = len(items) * (row + gap)
    top = max(n for _, n in items) or 1
    out = []
    for i, (name, n) in enumerate(items):
        y = i * (row + gap)
        bw = max(4, (w - 185) * n / top)
        out.append(f'<text x="0" y="{y + row * .72}" class="lbl">{escape(name[:20])}</text>'
                   f'<rect x="150" y="{y}" width="{bw:.1f}" height="{row}" rx="5" fill="{colour}"/>'
                   f'<text x="{150 + bw + 7:.1f}" y="{y + row * .72}" class="num">{n}</text>')
    return _svg(w, h, "".join(out), label)


def runs(history: list[dict]) -> Markup:
    """New jobs per run (bars) and newly approved per run (dots) for the latest runs."""
    hist = history[-14:]
    if not hist:
        return Markup('<p class="empty">Runs appear here after the first orchestrated run.</p>')
    w, h, pad = 420, 220, 4
    top = max([r.get("jobs_new", 0) for r in hist] + [1])
    slot = (w - pad) / max(len(hist), 1)
    out = []
    for i, r in enumerate(hist):
        n, a = r.get("jobs_new", 0), r.get("approved_new", 0)
        bh = (h - 52) * n / top
        x = pad + i * slot + slot * .18
        bw = slot * .64
        out.append(f'<rect x="{x:.1f}" y="{h - 26 - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="4" '
                   f'fill="{VIOLET if r.get("ok", True) else RED}"/>')
        out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 30 - bh:.1f}" class="num" text-anchor="middle">{n}</text>')
        if a:
            out.append(f'<circle cx="{x + bw / 2:.1f}" cy="{h - 26 - bh / 2:.1f}" r="{6 + min(a, 6)}" fill="{GREEN}"/>'
                       f'<text x="{x + bw / 2:.1f}" y="{h - 22 - bh / 2:.1f}" class="dot" text-anchor="middle">{a}</text>')
        day = str(r.get("finished", ""))[5:10].replace("-", "/")
        out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" class="lbl" text-anchor="middle">{day}</text>')
    return _svg(w, h, "".join(out), "Runs: new jobs and newly approved")
