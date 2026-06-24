#!/usr/bin/env python3
"""A small Flask UI for the humidity / Gree-AC system.

Reads the controller's status.json and control.log for display, and issues
manual AC commands (off / dry / cool / heat, with a target temperature). A manual
command pauses the automation (by writing an override) until you press
"Resume Auto".

The page polls /api/state in the background (no full reloads) and posts control
actions via fetch, so it updates live without resetting the dropdowns.

Run locally:  python webapp.py   ->  http://localhost:8000
In Docker it listens on 0.0.0.0:8000 with host networking (http://<pi-ip>:8000).
"""

import asyncio
import base64
import hmac
import os
import time
from datetime import datetime, timedelta

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, url_for)

import core

app = Flask(__name__)

# Optional HTTP Basic auth — enforced only when BOTH env vars are set.
WEB_USER = os.environ.get("WEB_USER", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")
AUTH_ENABLED = bool(WEB_USER and WEB_PASSWORD)


@app.before_request
def _require_auth():
    if not AUTH_ENABLED or request.path == "/apple-touch-icon.png":
        return None
    auth = request.authorization
    if (auth and hmac.compare_digest(auth.username or "", WEB_USER)
            and hmac.compare_digest(auth.password or "", WEB_PASSWORD)):
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="Home Climate"'})

# Cache discovered ACs so button presses don't pay the discovery cost each time.
_ac_cache = {"acs": {}, "ts": 0.0}
AC_CACHE_TTL = 120  # seconds

# Optimistic AC states from just-issued manual commands, shown until the
# controller's status snapshot catches up. {mac: {power, mode, target_temp, ts}}
_optimistic = {}


def _expected_state(act, temp):
    if act == "off":
        return {"power": False}
    state = {"power": True, "mode": act.capitalize(),
             "target_temp": core.clamp_temperature(act, temp)}
    if act in ("cool", "dry"):  # xFan + Health are forced on for these
        state["xfan"] = True
        state["anion"] = True
    return state

TEMP_OPTIONS = {mode: list(range(lo, hi + 1)) for mode, (lo, hi) in core.TEMP_RANGES.items()}
TEMP_DEFAULTS = {mode: core.clamp_temperature(mode, None) for mode in core.TEMP_RANGES}
ROOM_BY_AC = {mac: room for room, mac in core.AC_BY_ROOM.items()}

# Laundry mode (Bedroom only): Dry at 16–24°C for 1–24h, overrides everything.
LAUNDRY_ROOM = "Bedroom"
LAUNDRY_MAC = core.AC_BY_ROOM.get(LAUNDRY_ROOM)
LAUNDRY_TEMPS = list(range(16, 25))      # 16..24 °C
LAUNDRY_HOURS = list(range(1, 25))       # 1..24 h
LAUNDRY_DEFAULT_TEMP = 16
LAUNDRY_DEFAULT_HOURS = 6


def get_acs(force=False):
    if force or not _ac_cache["acs"] or (time.time() - _ac_cache["ts"]) > AC_CACHE_TTL:
        _ac_cache["acs"] = asyncio.run(core.discover_acs())
        _ac_cache["ts"] = time.time()
    return _ac_cache["acs"]


def age_str(iso):
    if not iso:
        return "never"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    secs = (datetime.now(when.tzinfo) - when).total_seconds()
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago"


def tail_log(n=80):
    try:
        return core.LOG_FILE.read_text().splitlines()[-n:]
    except FileNotFoundError:
        return []


def humidity_class(humidity, thresholds):
    if humidity is None:
        return ""
    if humidity >= thresholds.get("on", core.ON_THRESHOLD):
        return "hi-dry"
    if humidity <= thresholds.get("off", core.OFF_THRESHOLD):
        return "hi-off"
    return "hi-hold"


def _power_pill(a):
    if a.get("error"):
        return "amber", "unreachable"
    if a.get("power"):
        return "green", "ON"
    return "off", "off"


def _status_pill(status):
    if status.get("mode") == "scheduled-off":
        win = status.get("schedule", {}).get("window", "")
        return "amber", f"SCHEDULED OFF · {win}".strip(" ·")
    return "green", "AUTO · humidity in control"


def collect():
    """Build the snapshot shared by the page render and the JSON API."""
    status = core.read_status()
    thresholds = status.get("thresholds", {"on": core.ON_THRESHOLD, "off": core.OFF_THRESHOLD})

    sensors = []
    for room, s in sorted(status.get("sensors", {}).items()):
        temp_c = s.get("temperature_c") or 0.0
        sensors.append({
            "room": room,
            "humidity": s.get("humidity"),
            "temp_c": round(temp_c, 1),
            "temp_f": round(temp_c * 9 / 5 + 32, 1),
            "battery": s.get("battery"),
            "rssi": s.get("rssi"),
            "age": age_str(s.get("last_seen_iso")),
            "cls": humidity_class(s.get("humidity"), thresholds),
        })

    try:
        status_epoch = datetime.fromisoformat(status["updated_at"]).timestamp()
    except (KeyError, ValueError, TypeError):
        status_epoch = 0.0

    acs = []
    for mac, a in sorted(status.get("acs", {}).items()):
        ov = _optimistic.get(mac)
        if ov and ov["ts"] > status_epoch:  # show our recent manual change first
            a = {**a, **{k: v for k, v in ov.items() if k != "ts"}}
            a.pop("error", None)
        cls, txt = _power_pill(a)
        acs.append({
            "mac": mac, "room": ROOM_BY_AC.get(mac, "—"), "ip": a.get("ip"),
            "mode": a.get("mode"), "target_temp": a.get("target_temp"),
            "xfan": bool(a.get("xfan")), "health": bool(a.get("anion")),
            "power_cls": cls, "power_text": txt,
        })

    if status:
        meta = (f"Updated {age_str(status.get('updated_at'))} · {status.get('control_mode', '')}"
                f" · dry ≥{thresholds['on']}% , off ≤{thresholds['off']}%")
        if status.get("dry_run"):
            meta += " · DRY-RUN"
    else:
        meta = "No status yet — is the controller running?"

    sc = status.get("schedule")
    schedule = ""
    if sc:
        schedule = (f"OFF schedule ({sc.get('tz')}): weekday [{sc.get('weekday_off') or '—'}]"
                    f" · weekend [{sc.get('weekend_off') or '—'}]"
                    f" · now {'OFF' if sc.get('off_now') else 'RUNNING'}")
    force_off_at = core.force_off_at_str()
    if schedule:
        schedule += f" · daily force-off {force_off_at or 'off'}"

    pill_cls, pill_text = _status_pill(status)
    return {
        "pill_cls": pill_cls, "pill_text": pill_text, "meta": meta, "schedule": schedule,
        "force_off_at": force_off_at,
        "laundry": core.laundry_status(),
        "sensors": sensors, "acs": acs, "log": tail_log(),
    }


# Self-contained inline SVG wordmarks (no external assets; render offline).
GREE_LOGO = (
    '<svg class="logo" viewBox="0 0 86 24" height="18" role="img" aria-label="GREE">'
    '<text x="0" y="19" font-family="Helvetica,Arial,sans-serif" font-size="22" '
    'font-weight="800" letter-spacing="1.5" fill="#0a74c4">GREE</text></svg>'
)
TEMPPRO_LOGO = (
    '<svg class="logo" viewBox="0 0 126 24" height="18" role="img" aria-label="TempPro">'
    '<g fill="#e23b2e"><rect x="2" y="2" width="6" height="13" rx="3"/>'
    '<circle cx="5" cy="18" r="4.2"/><rect x="3.5" y="9" width="3" height="9"/></g>'
    '<text x="15" y="19" font-family="Helvetica,Arial,sans-serif" font-size="20" '
    'font-weight="800" fill="#e23b2e">Temp<tspan font-weight="900" fill="#b62a1f">Pro</tspan>'
    '</text></svg>'
)

# 180x180 PNG home-screen icon (droplet on brand blue). iOS uses apple-touch-icon
# (PNG only — it ignores the SVG favicon), served at /apple-touch-icon.png.
APPLE_ICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAFQ0lEQVR4nO3dQXLTShSFYTsFE3oRLIQMWBSsAhbFABbCIuQJA6hQlSrbseWW1K2+59z/m76C2NL/TjqOE46HID58+fl39GPANqfvz8fDYMMeAAH7Ow0IfNcPSMR5nXaKu/sHIWLsGXe3v5iQMSLs5n8hIWNk2E+HhogZo7tp8n8GISPKWm9eaGJGS1t72hQ0MaOHLV2tmndCRtQjyOKFJmbsaWlvi4ImZoywpLvqoIkZI9X21/R1aGC0qqBZZ0RQ0+HDoIkZkTzqcTZoYkZEc11yhoaVu0GzzojsXp83gyZmKLjVKUcOWHkTNOsMJde9stCwchE06wxF592y0LBC0PAMmuMGlL32y0LDCkHDL2iOG/1N3z7t8FFye+mYhd4xZqLuj6BhhaA7u15lVrqvJ87PcMJCd3RvjVnpfggaVgi6k0crzEr3QdCwQtAd1K4vK90eQcMKQTe2dHVZ6bYIGlYIuqG1a8tKt0PQsELQjWxdWVa6DYKGFYJuoNW6stLbETSsEHSwVWWltyFoWCHogGvKSq9H0LBC0EFXlJVeh6BhhaADrycrvRxBwwpBB19NVnoZgoYVghZYS1a6HkHDCkGLrOToj6+CoGGFoIXWMcrjiIygYYWgxVYx2uOJhqBhhaAF1zDq44qAoGGFoEVXMPrjG4WgYYWghddP5XHu6ci/guURSPn6a/RDCIGgxUO+VpKHnf7I4RSz4/NZKnXQrjd/Mn1eNVIeOTLd8JLsCJJuoTPFnPH5pgo6283N+LzTBJ3ppmZ+/imCznIzH8lwHeyDznATl5jMr4d10O43b63J+LrYBu1801qYTK+PbdDIyTJo1/VpbTK8TnZBO96kniaz62UVtNvN2ctkdN2sggZsgnZamREmk+tnEzRgE7TLuow2GVxHi6ABm6AdViWSSfx6ygcN2AStviZRTcLXVTpo4BpBw4ps0MqfFhVMotdXNmjgFoKGFcmgVT8dqpkEr7Nk0MA9BA0rBA0rBA0rckErfqGibBK73nJBA3MIGlYIGlYIGlYIGlYIGlYIGlYIGlYIGlYIGlYIGlbejX4ADn78/nP3v33++H7Xx5Kd5D+NHOUNM3Mhu4RdxP5pZRa6c8jXf0Y1bBWcoXeIueWfxzyCHhAjUfdD0IMiJOo+JINW+0JFVRG8zpJB763XmrLS7RE0rMgGvdenw94rGnWli+BxQzpo4BaChhXpoFU/LUZXhK+rdNCAXdDKaxJREb+e8kEDdkH3XJXe746L9O67Ir7ONkEDdkErrjTr3J5N0IBd0EorzTr3YRW0StTE3I9d0NGjJua++CHZDVFm+KlvNZK/xiDarztQ/L0cxeA153RBR/odHpEU05htz9BZbt4axfx62Aed4SbWKgmuQ4qgs9zMOVme/9Pp+/PxkESWm5r5eadZ6Iw3N+Pz/b/O7q90ZHwFpCQLOe1CZ7jpxfR5VQed6RztfvOL2fNZ4qVjvvV9FoHyESRzyOcIWjxsQr50cdTI+sXhPZHDJuRLr8dmFlpssQl5HkEvjGhE3ERc782rGxw76vWMm4jrnb9Kx0I3jm5N5MTbzs3Xn1lpqLj+Hkrq7xTCz82gM3/nEDpudXp3oYkakd3rkyMHrMwGzUojorkuHy40USOSRz1WHTmIGhHUdMgZGlaqg2alMVJtf4sWmqgxwpLuFh85iBp7Wtrbpu8I8p4P9LJ2ODd9Uchao4ctXW1+lYOo0dLWnpq+CYkjCEYPY9PXoVlrjO6m29tEWWuMGMDu73smbOz5mXzXN/ITd16nnX5oZNhPphC3v9OAn3z6B7T02+FN5qIAAAAAAElFTkSuQmCC"
)


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return Response(APPLE_ICON_PNG, mimetype="image/png")


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <noscript><meta http-equiv="refresh" content="15"></noscript>
  <title>Home Climate</title>
  <!-- iOS home-screen icon (PNG; iOS ignores the SVG favicon) + web-app chrome. -->
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-title" content="Climate">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <!-- Self-contained tab icon: a humidity droplet in the brand blue (inline SVG, no external asset). -->
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzBhNzRjNCIvPjxwYXRoIGQ9Ik0xNiA1LjVjMCAwLTcuNSA4LjItNy41IDEzLjRhNy41IDcuNSAwIDAgMCAxNSAwQzIzLjUgMTMuNyAxNiA1LjUgMTYgNS41WiIgZmlsbD0iI2ZmZmZmZiIvPjxjaXJjbGUgY3g9IjEzIiBjeT0iMTkuNSIgcj0iMi4zIiBmaWxsPSIjYmZlMGZhIi8+PC9zdmc+">
  <style>
    :root {
      --bg:#eef1f6; --card:#ffffff; --text:#16202e; --muted:#67748a;
      --border:#e2e7ef; --shadow:0 1px 3px rgba(20,30,50,.08),0 1px 2px rgba(20,30,50,.04);
      --accent:#0a74c4; --green:#1f9d57; --green-bg:#1f9d5719;
      --amber:#c98a12; --amber-bg:#c98a1219; --red:#d24b3a; --red-bg:#d24b3a19;
      --track:#e7ebf2;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg:#0e131b; --card:#19212c; --text:#e8eef6; --muted:#90a0b6;
        --border:#28323f; --shadow:0 1px 2px rgba(0,0,0,.4);
        --accent:#46a3e6; --green:#34c884; --green-bg:#34c8841f;
        --amber:#e0a93b; --amber-bg:#e0a93b1f; --red:#ef6a59; --red-bg:#ef6a591f;
        --track:#28323f;
      }
    }
    * { box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
      margin:0; background:var(--bg); color:var(--text); line-height:1.45; }
    .wrap { max-width:940px; margin:0 auto; padding:1rem 1.1rem 2.5rem; }
    .logo { vertical-align:middle; }

    header { display:flex; align-items:center; justify-content:space-between;
      flex-wrap:wrap; gap:.6rem; padding:.4rem 0 1rem; }
    header h1 { font-size:1.35rem; margin:0; display:flex; align-items:center; gap:.5rem; }
    .powered { display:flex; align-items:center; gap:.7rem; font-size:.8rem; color:var(--muted); }

    .pill { display:inline-flex; align-items:center; gap:.4rem; padding:.25rem .7rem;
      border-radius:999px; font-size:.82rem; font-weight:700; border:1px solid transparent; }
    .pill.green { background:var(--green-bg); color:var(--green); border-color:var(--green); }
    .pill.amber { background:var(--amber-bg); color:var(--amber); border-color:var(--amber); }
    .pill.red { background:var(--red-bg); color:var(--red); border-color:var(--red); }
    .pill.off { color:var(--muted); border-color:var(--border); }
    .pill.dot::before { content:""; width:.5rem; height:.5rem; border-radius:50%;
      background:currentColor; }

    .meta { color:var(--muted); font-size:.85rem; margin:.15rem 0; }
    .card { background:var(--card); border:1px solid var(--border); border-radius:14px;
      box-shadow:var(--shadow); padding:1rem 1.1rem; }

    .statusbar { display:flex; align-items:center; justify-content:space-between;
      flex-wrap:wrap; gap:.6rem; margin-bottom:1rem; }
    .statusbar .info { display:flex; flex-direction:column; gap:.1rem; }
    .statusbar form { margin:0; }
    #live { font-size:.72rem; color:var(--muted); transition:opacity .3s; }
    #live::before { content:"● "; color:var(--green); }

    h2 { font-size:1rem; letter-spacing:.02em; text-transform:uppercase; color:var(--muted);
      margin:1.6rem .2rem .7rem; display:flex; align-items:center; gap:.55rem; }

    .grid { display:grid; gap:.8rem; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); }

    .sensor .top { display:flex; justify-content:space-between; align-items:baseline; }
    .sensor .room { font-weight:700; }
    .sensor .big { font-size:2rem; font-weight:800; line-height:1.1; margin:.3rem 0 .1rem; }
    .bar { height:7px; border-radius:999px; background:var(--track); overflow:hidden; margin:.3rem 0 .55rem; }
    .bar > i { display:block; height:100%; border-radius:999px; }
    .hi-dry { color:var(--red); } .hi-dry-bg { background:var(--red); }
    .hi-hold { color:var(--amber); } .hi-hold-bg { background:var(--amber); }
    .hi-off { color:var(--green); } .hi-off-bg { background:var(--green); }
    .sensor .sub { font-size:.82rem; color:var(--muted); }

    .ac .top { display:flex; justify-content:space-between; align-items:center; gap:.5rem; }
    .ac .room { font-weight:700; }
    .ac .mac { font-size:.74rem; color:var(--muted); }
    .ac .state { font-size:.84rem; color:var(--muted); margin:.45rem 0 .65rem; }
    .ac .state b { color:var(--text); font-weight:600; }

    .controls { display:flex; flex-direction:column; gap:.45rem; }
    .ctl { display:flex; align-items:center; gap:.5rem; margin:0; }
    .ctl.mode { flex-wrap:wrap; }
    .ctl.mode button { min-width:64px; text-align:center; }
    .ctl .feat { font-size:.72rem; color:var(--green); font-weight:700;
      white-space:nowrap; cursor:help; }
    .feats { display:flex; gap:.35rem; margin:.3rem 0 .1rem; }
    .chip { font-size:.68rem; font-weight:700; padding:.08rem .45rem; border-radius:999px;
      border:1px solid var(--border); letter-spacing:.02em; }
    .chip.on { color:var(--green); border-color:var(--green); background:var(--green-bg); }
    .chip.off { color:var(--muted); opacity:.55; }
    .help { font-size:.88rem; color:var(--muted); }
    .help p { margin:.55rem 0; line-height:1.55; }
    .help ul { margin:.4rem 0 .55rem 1.1rem; line-height:1.55; }
    .help b { color:var(--text); }
    .help .hl { color:var(--accent); font-weight:700; }
    .ctl.off button { width:100%; }
    .ctl .at { margin-left:auto; color:var(--muted); font-size:.8rem; }
    select { padding:.32rem .4rem; border-radius:8px; border:1px solid var(--border);
      background:var(--bg); color:var(--text); font-size:.82rem; min-width:64px; }
    button { padding:.4rem .7rem; border-radius:8px; border:1px solid var(--border);
      background:var(--bg); color:var(--text); cursor:pointer; font-size:.82rem; font-weight:600; }
    button:hover { border-color:var(--accent); }
    button.b-off { color:var(--green); } button.b-dry { color:var(--accent); }
    button.b-cool { color:#7b58c9; } button.b-heat { color:var(--amber); }
    button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    button:disabled { opacity:.5; cursor:default; }
    button.busy { color:transparent !important; opacity:1 !important; position:relative; }
    button.busy::after { content:""; position:absolute; top:50%; left:50%;
      width:13px; height:13px; margin:-7px 0 0 -7px; border-radius:50%;
      border:2px solid var(--muted); border-top-color:transparent;
      animation:spin .6s linear infinite; }
    @keyframes spin { to { transform:rotate(360deg); } }

    pre { background:#0b0f15; color:#c9d6e5; padding:.85rem 1rem; border-radius:12px;
      overflow:auto; max-height:300px; font-size:.78rem; line-height:1.5;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; border:1px solid var(--border); }
    footer { margin-top:1.6rem; text-align:center; color:var(--muted); font-size:.78rem; }
    .toast { position:fixed; left:50%; bottom:1.2rem; transform:translateX(-50%) translateY(2rem);
      background:var(--card); color:var(--text); border:1px solid var(--border);
      box-shadow:var(--shadow); padding:.6rem 1rem; border-radius:10px; font-size:.86rem;
      font-weight:600; opacity:0; pointer-events:none; max-width:90vw;
      transition:opacity .25s, transform .25s; z-index:10; }
    .toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
    .toast.ok { border-color:var(--green); }
    .toast.err { border-color:var(--red); color:var(--red); }
  </style>
</head>
<body>
<div class="wrap">
{% macro controls(mac) %}
  <form class="ctl off" method="post" action="/action">
    <input type="hidden" name="mac" value="{{ mac }}">
    <input type="hidden" name="action" value="off"><button class="b-off">Turn off</button>
  </form>
  {% for mode in ['dry', 'cool', 'heat'] %}
  <form class="ctl mode" method="post" action="/action">
    <input type="hidden" name="mac" value="{{ mac }}">
    <input type="hidden" name="action" value="{{ mode }}">
    <button class="b-{{ mode }}">{{ mode|capitalize }}</button>
    <span class="at">at</span>
    <select name="temp">{% for t in temp_options[mode] %}<option value="{{ t }}"{% if t == temp_defaults[mode] %} selected{% endif %}>{{ t }}&deg;C</option>{% endfor %}</select>
    {% if mode in ['dry', 'cool'] %}
    <span class="feat" title="xFan dries the coil after stopping; Health runs the ionizer. Always on in Cool/Dry.">&#10003; xFan + Health</span>
    {% endif %}
  </form>
  {% endfor %}
{% endmacro %}

  <header>
    <h1>&#127968; Home Climate</h1>
    <div class="powered"><span>powered by</span>{{ gree_logo|safe }}{{ temppro_logo|safe }}</div>
  </header>

  <div class="card statusbar">
    <div class="info">
      <span class="pill dot {{ snap.pill_cls }}" id="statuspill">{{ snap.pill_text }}</span>
      <span class="meta" id="meta">{{ snap.meta }}</span>
      <span class="meta" id="sched"{% if not snap.schedule %} hidden{% endif %}>{{ snap.schedule }}</span>
      <span id="live">live</span>
    </div>
  </div>

  <div class="card" style="margin-bottom:.8rem">
    <div class="top"><span class="room">&#9200; Daily safety shut-off</span></div>
    <div class="sub" style="margin:.35rem 0 .6rem">
      Forces <b>every</b> AC off once a day — overrides manual Cool/Heat and any pause.
      A backstop for units left on. Leave the time empty to disable.
    </div>
    <form id="settings" method="post" action="{{ url_for('settings') }}"
          style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap">
      <label class="sub">Force-off time
        <input type="time" name="force_off_at" value="{{ force_off_at }}"
               style="margin-left:.4rem;padding:.3rem .4rem">
      </label>
      <button class="primary" type="submit">Save</button>
    </form>
  </div>

  <h2>Sensors {{ temppro_logo|safe }}</h2>
  <div class="grid" id="sensors">
    {% for s in snap.sensors %}
      <div class="card sensor">
        <div class="top"><span class="room">{{ s.room }}</span><span class="sub">{{ s.age }}</span></div>
        <div class="big {{ s.cls }}">{{ s.humidity if s.humidity is not none else '—' }}<span style="font-size:1rem">% RH</span></div>
        <div class="bar"><i class="{{ s.cls }}-bg" style="width:{{ s.humidity or 0 }}%"></i></div>
        <div class="sub">&#127777;&#65039; {{ s.temp_c }}&deg;C / {{ s.temp_f }}&deg;F
          &nbsp;&middot;&nbsp; &#128267; {% if s.battery is not none %}~{{ s.battery }}%{% else %}&mdash;{% endif %}
          &nbsp;&middot;&nbsp; &#128246; {{ s.rssi }} dBm</div>
      </div>
    {% else %}
      <div class="card sub">No sensor data yet.</div>
    {% endfor %}
  </div>

  <h2>Air conditioners {{ gree_logo|safe }}</h2>
  <div class="card" style="margin-bottom:.8rem">
    <div class="top"><span class="room">All ACs</span></div>
    <div class="controls" style="margin-top:.6rem">{{ controls('all') }}</div>
  </div>

  <div class="card laundry" style="margin-bottom:.8rem">
    <div class="top"><span class="room">&#129532; Laundry mode &middot; Bedroom</span>
      <span class="pill {{ 'amber dot' if snap.laundry.active else 'off' }}" id="laundry-pill">{{ 'RUNNING' if snap.laundry.active else 'off' }}</span>
    </div>
    <div class="sub" style="margin:.35rem 0 .6rem">
      Dry at a low temperature for a set time. Overrides the schedule, the daily
      shut-off and humidity. When the timer ends it hands the AC back to automation
      (no forced off).
    </div>
    <div id="laundry-active"{% if not snap.laundry.active %} hidden{% endif %}>
      <div class="state" id="laundry-info">Dry <b>{{ snap.laundry.temp }}</b>&deg;C &middot; <b>{{ snap.laundry.remaining_min }}</b> min left</div>
      <form class="laundryform" method="post" action="/laundry/stop" style="margin-top:.5rem">
        <button class="b-off">Stop laundry</button>
      </form>
    </div>
    <form id="laundry-idle" class="laundryform" method="post" action="/laundry/start"{% if snap.laundry.active %} hidden{% endif %}
          style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
      <span class="sub">Dry at</span>
      <select name="temp">{% for t in laundry_temps %}<option value="{{ t }}"{% if t == laundry_default_temp %} selected{% endif %}>{{ t }}&deg;C</option>{% endfor %}</select>
      <span class="sub">for</span>
      <select name="hours">{% for h in laundry_hours %}<option value="{{ h }}"{% if h == laundry_default_hours %} selected{% endif %}>{{ h }}h</option>{% endfor %}</select>
      <button class="b-dry">Start laundry</button>
    </form>
  </div>

  <div class="grid">
    {% for a in snap.acs %}
      <div class="card ac">
        <div class="top">
          <div><span class="room">{{ a.room }}</span> {{ gree_logo|safe }}<br><span class="mac">{{ a.mac }} &middot; {{ a.ip or '—' }}</span></div>
          <span class="pill {{ a.power_cls }}{% if a.power_cls == 'green' %} dot{% endif %}" id="pill-{{ a.mac }}">{{ a.power_text }}</span>
        </div>
        <div class="state" id="state-{{ a.mac }}">Mode <b>{{ a.mode or '—' }}</b>{% if a.target_temp %} &middot; target <b>{{ a.target_temp }}&deg;C</b>{% endif %}</div>
        <div class="feats" id="feats-{{ a.mac }}"><span class="chip {{ 'on' if a.xfan else 'off' }}">xFan</span><span class="chip {{ 'on' if a.health else 'off' }}">Health</span></div>
        <div class="controls">{{ controls(a.mac) }}</div>
      </div>
    {% else %}
      <div class="card sub">No ACs in last snapshot.</div>
    {% endfor %}
  </div>

  <h2>Recent log</h2>
  <pre id="log">{% for line in snap.log %}{{ line }}
{% endfor %}</pre>

  <h2>How this works</h2>
  <div class="card help">
    <p><b>What it does.</b> This system reads the humidity in each room from the
    TempPro sensors and automatically runs the matching Gree AC in <b>Dry</b> mode
    to keep things comfortable. It runs on its own &mdash; you don't have to do
    anything.</p>

    <p><b>Status bar (top).</b> A green <span class="hl">AUTO</span> badge means the
    automation is in control. An amber <span class="hl">SCHEDULED OFF</span> means
    you're inside an off-window (ACs kept off). The line underneath shows the
    on/off schedule and the daily safety shut-off time.</p>

    <p><b>Sensors.</b> One card per room: current humidity (%RH), temperature,
    battery and signal. The colour reflects how humid the room is.</p>

    <p><b>The automation rules.</b></p>
    <ul>
      <li>Humidity rises above the ON threshold &rarr; the AC switches to <b>Dry</b>.
      It keeps drying until humidity falls to the OFF threshold, then turns off.</li>
      <li>It is <b>always on</b> &mdash; there is no pause button.</li>
      <li>If you set an AC to <b>Cool</b> or <b>Heat</b> (here or in the Gree app),
      the automation <b>leaves it alone</b> &mdash; that room is yours to control.</li>
      <li>Set it back to <b>Dry</b> or <b>Off</b> and the automation
      <b>takes over again</b> automatically.</li>
    </ul>

    <p><b>Air-conditioner controls.</b> Each AC card (and "All ACs") has buttons:
    <b>Turn off</b>, <b>Dry</b>, <b>Cool</b>, <b>Heat</b>, each with a target
    temperature. In <b>Cool</b> and <b>Dry</b>, <span class="hl">xFan</span> (dries
    the indoor coil to prevent mould) and <span class="hl">Health</span> (ionizer)
    are <b>always enabled automatically</b> &mdash; nothing to switch on.</p>

    <p><b>Daily safety shut-off.</b> Once a day at the configured time (a backstop
    for a unit left running), <b>every</b> AC is forced off &mdash; even one you set
    to Cool/Heat. Change the time in the box near the top; clear it to disable.</p>

    <p><b>Laundry mode.</b> Starts a fixed Dry session in the Bedroom for a set
    number of hours (to dry clothes), overriding the schedule until it ends or you
    stop it.</p>

    <p><b>Recent log.</b> A live trace of every decision the controller makes, newest
    at the bottom &mdash; handy to see why an AC is on or off right now.</p>
  </div>

  <footer>Sensors by {{ temppro_logo|safe }} &middot; ACs by {{ gree_logo|safe }} &middot; updates live in the background</footer>
</div>
<div id="toast" class="toast"></div>

<script>
const esc = (x) => { const d = document.createElement('div'); d.textContent = (x==null?'':x); return d.innerHTML; };

function sensorCard(s) {
  const hum = (s.humidity==null) ? '&mdash;' : s.humidity;
  const batt = (s.battery==null) ? '&mdash;' : ('~' + s.battery + '%');
  return `<div class="card sensor">
    <div class="top"><span class="room">${esc(s.room)}</span><span class="sub">${esc(s.age)}</span></div>
    <div class="big ${s.cls}">${hum}<span style="font-size:1rem">% RH</span></div>
    <div class="bar"><i class="${s.cls}-bg" style="width:${s.humidity||0}%"></i></div>
    <div class="sub">&#127777;&#65039; ${s.temp_c}&deg;C / ${s.temp_f}&deg;F
      &nbsp;&middot;&nbsp; &#128267; ${batt} &nbsp;&middot;&nbsp; &#128246; ${esc(s.rssi)} dBm</div>
  </div>`;
}

async function update() {
  let d;
  try { d = await (await fetch('/api/state', {cache:'no-store'})).json(); }
  catch (e) { const l = document.getElementById('live'); if (l) l.style.opacity = .3; return; }

  const sp = document.getElementById('statuspill');
  sp.className = 'pill dot ' + d.pill_cls;
  sp.textContent = d.pill_text;
  document.getElementById('meta').textContent = d.meta;
  const sched = document.getElementById('sched');
  sched.textContent = d.schedule; sched.hidden = !d.schedule;

  const grid = document.getElementById('sensors');
  grid.innerHTML = d.sensors.length ? d.sensors.map(sensorCard).join('')
                                    : '<div class="card sub">No sensor data yet.</div>';

  d.acs.forEach(a => {
    const pill = document.getElementById('pill-' + a.mac);
    if (pill) { pill.className = 'pill ' + a.power_cls + (a.power_cls==='green' ? ' dot' : ''); pill.textContent = a.power_text; }
    const st = document.getElementById('state-' + a.mac);
    if (st) st.innerHTML = 'Mode <b>' + esc(a.mode || '—') + '</b>'
        + (a.target_temp ? ' &middot; target <b>' + esc(a.target_temp) + '&deg;C</b>' : '');
    const ft = document.getElementById('feats-' + a.mac);
    if (ft) ft.innerHTML = '<span class="chip ' + (a.xfan ? 'on' : 'off') + '">xFan</span>'
        + '<span class="chip ' + (a.health ? 'on' : 'off') + '">Health</span>';
  });

  const L = d.laundry || { active: false };
  document.getElementById('laundry-active').hidden = !L.active;
  document.getElementById('laundry-idle').hidden = L.active;
  const lp = document.getElementById('laundry-pill');
  lp.className = 'pill ' + (L.active ? 'amber dot' : 'off');
  lp.textContent = L.active ? 'RUNNING' : 'off';
  if (L.active) {
    const mins = L.remaining_min || 0, h = Math.floor(mins / 60), m = mins % 60;
    document.getElementById('laundry-info').innerHTML =
      'Dry <b>' + esc(L.temp) + '</b>&deg;C &middot; <b>' + (h ? h + 'h ' : '') + m + 'm</b> left';
  }

  const log = document.getElementById('log');
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = d.log.join('\\n');
  if (atBottom) log.scrollTop = log.scrollHeight;

  const live = document.getElementById('live'); if (live) live.style.opacity = 1;
}

let toastTimer;
function toast(msg, ok) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok ? 'ok' : 'err');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = 'toast'; }, 3200);
}

// Post forms via fetch so the page doesn't reload (keeps dropdowns).
// NOTE: use getAttribute('action') — a hidden <input name="action"> shadows the
// form's own .action property in the DOM, so f.action is NOT the URL.
async function postForm(f, btn) {
  const url = f.getAttribute('action') || '/action';
  // "thinking" UI: disable this card's controls + spinner on the clicked button
  const scope = f.closest('.card') || f;
  const buttons = scope.querySelectorAll('button');
  buttons.forEach((b) => { b.disabled = true; });
  if (btn) btn.classList.add('busy');
  toast('Working…', true);

  let data = {};
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'fetch' },
      body: new FormData(f),
    });
    data = await res.json();
  } catch (e) {
    data = { ok: false, message: 'Network error — is the server up?' };
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    if (btn) btn.classList.remove('busy');
  }
  toast(data.message || 'Done', data.ok !== false);
  update();
}
document.addEventListener('submit', (e) => {
  const f = e.target;
  if (!f.matches('form.ctl, .laundryform')) return;
  e.preventDefault();
  postForm(f, e.submitter);
});

document.addEventListener('DOMContentLoaded', () => { update(); setInterval(update, 10000); });
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(
        PAGE, snap=collect(), force_off_at=core.force_off_at_str(),
        temp_options=TEMP_OPTIONS, temp_defaults=TEMP_DEFAULTS,
        laundry_temps=LAUNDRY_TEMPS, laundry_hours=LAUNDRY_HOURS,
        laundry_default_temp=LAUNDRY_DEFAULT_TEMP, laundry_default_hours=LAUNDRY_DEFAULT_HOURS,
        gree_logo=GREE_LOGO, temppro_logo=TEMPPRO_LOGO,
    )


@app.route("/api/state")
def api_state():
    return jsonify(collect())


def _respond(ok, message):
    """JSON for fetch() callers; redirect for plain (no-JS) form posts."""
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok, message=message)
    return redirect(url_for("index"))


@app.route("/action", methods=["POST"])
def action():
    act = request.form.get("action")
    mac = request.form.get("mac", "all")

    if act not in ("off", "dry", "cool", "heat"):
        return _respond(False, "Unknown action")

    temp = request.form.get("temp", type=int)
    # xFan (coil blow-dry) + Health (anion) are always on in Cool/Dry (policy,
    # not user-toggleable). Left untouched for Heat/Off.
    if act in ("cool", "dry"):
        xfan = health = True
    else:
        xfan = health = None

    acs = get_acs()
    if mac != "all" and mac not in acs:
        acs = get_acs(force=True)  # cache may be stale — re-discover once
    targets = acs if mac == "all" else ({mac: acs[mac]} if mac in acs else {})

    label = act if act == "off" else f"{act} {core.clamp_temperature(act, temp)}°C"

    if not targets:
        core.logger.info("Web: manual '%s' on %s -> AC not found", label, mac)
        return _respond(False, f"Could not find that AC on the network ({mac})")

    async def run():
        return await asyncio.gather(
            *(core.apply_action(info, act, temp, xfan=xfan, health=health)
              for info in targets.values()),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    errors = [str(r) for r in results if isinstance(r, Exception)]
    oks = [r for r in results if not isinstance(r, Exception)]

    # Optimistically reflect the new state in the UI until the controller catches up.
    expected = _expected_state(act, temp)
    for (target_mac, _info), result in zip(targets.items(), results):
        if not isinstance(result, Exception):
            _optimistic[target_mac] = {**expected, "ts": time.time()}

    # No global pause: the controller stays on and reconciles per-room — it leaves
    # Cool/Heat alone and reclaims a unit once it's back in Dry/Off.
    core.logger.info("Web: manual '%s' on %s -> %s", label, mac,
                     "; ".join(str(r) for r in results))

    if errors and not oks:
        return _respond(False, "Could not reach the AC: " + "; ".join(errors))
    if errors:
        return _respond(True, f"{label} sent (some ACs failed)")
    return _respond(True, f"{label} sent")


@app.route("/laundry/start", methods=["POST"])
def laundry_start():
    if not LAUNDRY_MAC:
        return _respond(False, "No Bedroom AC configured")
    temp = max(16, min(24, request.form.get("temp", type=int) or LAUNDRY_DEFAULT_TEMP))
    hours = max(1, min(24, request.form.get("hours", type=int) or LAUNDRY_DEFAULT_HOURS))

    acs = get_acs()
    if LAUNDRY_MAC not in acs:
        acs = get_acs(force=True)
    if LAUNDRY_MAC not in acs:
        return _respond(False, "Bedroom AC not found on the network")

    try:
        asyncio.run(core.apply_action(acs[LAUNDRY_MAC], "laundry", temp))
    except Exception as exc:
        return _respond(False, f"Could not start laundry: {exc}")

    until = (core.now_local() + timedelta(hours=hours)).isoformat(timespec="seconds")
    core.write_laundry({"active": True, "room": LAUNDRY_ROOM, "mac": LAUNDRY_MAC,
                        "temp": temp, "until": until, "started": core.now_iso()})
    _optimistic[LAUNDRY_MAC] = {"power": True, "mode": "Dry", "target_temp": temp,
                                "ts": time.time()}
    core.logger.info("Web: laundry START %s Dry %s°C for %sh", LAUNDRY_ROOM, temp, hours)
    return _respond(True, f"Laundry started — Dry {temp}°C for {hours}h")


@app.route("/laundry/stop", methods=["POST"])
def laundry_stop():
    core.write_laundry({"active": False})
    core.logger.info("Web: laundry STOP — back to automation")
    return _respond(True, "Laundry stopped — automation resumes")


@app.route("/settings", methods=["POST"])
def settings():
    """Set the daily force-off time (HH:MM), or empty to disable. Picked up by the
    controller on its next decision cycle."""
    val = (request.form.get("force_off_at") or "").strip()
    if val:
        try:
            core._parse_hhmm(val)  # validate HH:MM
        except (ValueError, AttributeError):
            return _respond(False, "Enter a time as HH:MM (e.g. 02:00) or leave it empty")
    s = core.read_settings()
    s["force_off_at"] = val
    core.write_settings(s)
    core.logger.info("Web: daily force-off time set to %s", val or "(disabled)")
    return _respond(True, f"Daily force-off {'set to ' + val if val else 'disabled'}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
