"""Inline-SVG trend charts for the web UI.

No external JS/CSS libraries — every chart is a self-contained <svg> string that
renders offline. Axis/grid/label colours use CSS custom properties (var(--…)) so
the charts follow the page's light/dark theme; each room gets a fixed palette
colour shared across charts and the legend.

Metric samples are dicts: {"t": <UTC iso>, "h": {room: %}, "ac": {room: mode|"off"}}.
Timestamps are UTC; a tz (the schedule timezone) is passed in for day-bucketing.
"""

import math
from datetime import datetime, timedelta, timezone

ROOM_COLORS = ["#0a74c4", "#1f9d57", "#c98a12", "#7b58c9", "#d24b3a", "#0bb7c4"]
_OFF_MODES = ("off", "err", "unreachable", "")


def color_map(rooms):
    return {room: ROOM_COLORS[i % len(ROOM_COLORS)] for i, room in enumerate(rooms)}


def _parsed(samples):
    """Return [(utc_datetime, sample), ...] sorted by time."""
    rows = []
    for s in samples:
        t = s.get("t")
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        rows.append((dt, s))
    rows.sort(key=lambda r: r[0])
    return rows


def _polyline(points, color):
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f'<circle cx="{x}" cy="{y}" r="1.6" fill="{color}"/>'
    pts = " ".join(f"{x},{y}" for x, y in points)
    return (f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>')


def humidity_svg(samples, rooms, tz, days, on_th, off_th, now):
    """Multi-line humidity chart over the last `days` days."""
    W, H, L, R, T, B = 960, 250, 32, 10, 12, 26
    x0, x1, y0, y1 = L, W - R, T, H - B
    t_end, t_start = now, now - timedelta(days=days)
    span = (t_end - t_start).total_seconds() or 1
    ylo, yhi = 30, 90

    def xp(dt):
        return round(x0 + (x1 - x0) * ((dt - t_start).total_seconds() / span), 1)

    def yp(h):
        h = max(ylo, min(yhi, h))
        return round(y1 - (y1 - y0) * ((h - ylo) / (yhi - ylo)), 1)

    p = [f'<svg viewBox="0 0 {W} {H}" class="chart" preserveAspectRatio="none" '
         'role="img" aria-label="Humidity over time">']
    for hv in range(40, 81, 10):  # horizontal grid + y labels
        y = yp(hv)
        p.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" '
                 'style="stroke:var(--border)" stroke-width="1"/>')
        p.append(f'<text x="{x0 - 4}" y="{y + 3}" text-anchor="end" '
                 f'style="fill:var(--muted)" font-size="9">{hv}</text>')
    for th, col in ((on_th, "var(--accent)"), (off_th, "var(--green)")):  # thresholds
        y = yp(th)
        p.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" style="stroke:{col}" '
                 'stroke-width="1" stroke-dasharray="4 3" opacity="0.65"/>')
    # vertical day gridlines at local midnights
    mid = t_start.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if mid < t_start.astimezone(tz):
        mid += timedelta(days=1)
    while mid <= t_end.astimezone(tz):
        x = xp(mid.astimezone(timezone.utc))
        p.append(f'<line x1="{x}" y1="{y0}" x2="{x}" y2="{y1}" '
                 'style="stroke:var(--border)" stroke-width="1" opacity="0.5"/>')
        p.append(f'<text x="{x + 2}" y="{y0 + 9}" style="fill:var(--muted)" '
                 f'font-size="9">{mid.strftime("%a %d")}</text>')
        mid += timedelta(days=1)
    cmap = color_map(rooms)
    rows = _parsed(samples)
    for room in rooms:  # one (possibly segmented) line per room
        col, seg, prev = cmap[room], [], None
        for dt, s in rows:
            h = s.get("h", {}).get(room)
            if h is None:
                if seg:
                    p.append(_polyline(seg, col))
                    seg = []
                prev = None
                continue
            if prev is not None and (dt - prev).total_seconds() > 1800:  # gap break
                if seg:
                    p.append(_polyline(seg, col))
                    seg = []
            seg.append((xp(dt), yp(h)))
            prev = dt
        if seg:
            p.append(_polyline(seg, col))
    p.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" '
             'style="stroke:var(--muted)" stroke-width="1"/>')
    p.append("</svg>")
    return "".join(p)


def compute_runtime(samples, rooms, tz, cap_min=10.0):
    """{local_date: {room: minutes_on}} by integrating consecutive samples.

    Each interval is credited to the earlier sample's state; gaps (restarts) are
    capped at `cap_min` so downtime can't inflate run-time.
    """
    rows = _parsed(samples)
    out = {}
    for i in range(len(rows) - 1):
        t0, s0 = rows[i]
        dt_min = (rows[i + 1][0] - t0).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        dt_min = min(dt_min, cap_min)
        day = t0.astimezone(tz).date()
        ac = s0.get("ac", {})
        for room in rooms:
            mode = ac.get(room)
            if mode and mode not in _OFF_MODES:
                out.setdefault(day, {}).setdefault(room, 0.0)
                out[day][room] += dt_min
    return out


def runtime_today(samples, rooms, tz, now):
    rt = compute_runtime(samples, rooms, tz)
    return rt.get(now.astimezone(tz).date(), {})


def compute_runtime_by_mode(samples, rooms, tz, cap_min=10.0):
    """{local_date: {room: {mode: minutes}}} — run-time split by AC mode."""
    rows = _parsed(samples)
    out = {}
    for i in range(len(rows) - 1):
        t0, s0 = rows[i]
        dt_min = (rows[i + 1][0] - t0).total_seconds() / 60.0
        if dt_min <= 0:
            continue
        dt_min = min(dt_min, cap_min)
        day = t0.astimezone(tz).date()
        ac = s0.get("ac", {})
        for room in rooms:
            mode = ac.get(room)
            if mode and mode not in _OFF_MODES:
                out.setdefault(day, {}).setdefault(room, {}).setdefault(mode, 0.0)
                out[day][room][mode] += dt_min
    return out


def compute_energy(samples, rooms, tz, watts):
    """{local_date: {room: kWh}} estimated from per-mode minutes x per-mode watts."""
    by_mode = compute_runtime_by_mode(samples, rooms, tz)
    default_w = watts.get("cool", 0.0)
    out = {}
    for day, rmap in by_mode.items():
        for room, modes in rmap.items():
            kwh = sum((mins / 60.0) * watts.get(mode, default_w) / 1000.0
                      for mode, mins in modes.items())
            out.setdefault(day, {})[room] = kwh
    return out


def energy_svg(samples, rooms, tz, days, now, watts):
    """Grouped bar chart: estimated energy (kWh) per day, one bar per room."""
    en = compute_energy(samples, rooms, tz, watts)
    today = now.astimezone(tz).date()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    W, H, L, R, T, B = 960, 230, 36, 10, 12, 26
    x0, x1, y0, y1 = L, W - R, T, H - B
    max_k = max([0.001] + [en.get(d, {}).get(r, 0.0) for d in day_list for r in rooms])
    cmap = color_map(rooms)

    p = [f'<svg viewBox="0 0 {W} {H}" class="chart" preserveAspectRatio="none" '
         'role="img" aria-label="Estimated energy per day">']
    nt = 4
    for k in range(nt + 1):
        v = max_k * k / nt
        y = round(y0 + (y1 - y0) * (1 - k / nt), 1)
        p.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" '
                 'style="stroke:var(--border)" stroke-width="1"/>')
        p.append(f'<text x="{x0 - 4}" y="{y + 3}" text-anchor="end" '
                 f'style="fill:var(--muted)" font-size="9">{v:.1f}</text>')
    n = len(day_list)
    gw = (x1 - x0) / n
    nb = max(1, len(rooms))
    bw = min(20.0, (gw * 0.72) / nb)
    for gi, d in enumerate(day_list):
        gx = x0 + gw * gi
        start = gx + (gw - bw * nb) / 2
        for bi, room in enumerate(rooms):
            val = en.get(d, {}).get(room, 0.0)
            bh = (y1 - y0) * (val / max_k)
            bx = round(start + bw * bi, 1)
            if bh >= 0.5:
                p.append(f'<rect x="{bx}" y="{round(y1 - bh, 1)}" '
                         f'width="{round(bw - 1, 1)}" height="{round(bh, 1)}" '
                         f'fill="{cmap[room]}" rx="1"/>')
        p.append(f'<text x="{round(gx + gw / 2, 1)}" y="{y1 + 14}" '
                 f'text-anchor="middle" style="fill:var(--muted)" '
                 f'font-size="9">{d.strftime("%a %d")}</text>')
    p.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" '
             'style="stroke:var(--muted)" stroke-width="1"/>')
    p.append("</svg>")
    return "".join(p)


def runtime_svg(samples, rooms, tz, days, now):
    """Grouped bar chart: AC run-time (hours) per day, one bar per room."""
    rt = compute_runtime(samples, rooms, tz)
    today = now.astimezone(tz).date()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    W, H, L, R, T, B = 960, 230, 28, 10, 12, 26
    x0, x1, y0, y1 = L, W - R, T, H - B
    max_min = max([1.0] + [rt.get(d, {}).get(r, 0.0)
                           for d in day_list for r in rooms])
    max_h = max(1, math.ceil(max_min / 60.0))
    step = 1 if max_h <= 8 else (2 if max_h <= 16 else 4)
    cmap = color_map(rooms)

    p = [f'<svg viewBox="0 0 {W} {H}" class="chart" preserveAspectRatio="none" '
         'role="img" aria-label="AC run-time per day">']
    for hh in range(0, max_h + 1, step):  # y grid + hour labels
        y = round(y0 + (y1 - y0) * (1 - hh / max_h), 1)
        p.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" '
                 'style="stroke:var(--border)" stroke-width="1"/>')
        p.append(f'<text x="{x0 - 4}" y="{y + 3}" text-anchor="end" '
                 f'style="fill:var(--muted)" font-size="9">{hh}h</text>')
    n = len(day_list)
    gw = (x1 - x0) / n
    nb = max(1, len(rooms))
    bw = min(20.0, (gw * 0.72) / nb)
    for gi, d in enumerate(day_list):
        gx = x0 + gw * gi
        start = gx + (gw - bw * nb) / 2
        for bi, room in enumerate(rooms):
            mins = rt.get(d, {}).get(room, 0.0)
            bh = (y1 - y0) * ((mins / 60.0) / max_h)
            bx = round(start + bw * bi, 1)
            by = round(y1 - bh, 1)
            if bh >= 0.5:
                p.append(f'<rect x="{bx}" y="{by}" width="{round(bw - 1, 1)}" '
                         f'height="{round(bh, 1)}" fill="{cmap[room]}" rx="1"/>')
        p.append(f'<text x="{round(gx + gw / 2, 1)}" y="{y1 + 14}" '
                 f'text-anchor="middle" style="fill:var(--muted)" '
                 f'font-size="9">{d.strftime("%a %d")}</text>')
    p.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" '
             'style="stroke:var(--muted)" stroke-width="1"/>')
    p.append("</svg>")
    return "".join(p)
