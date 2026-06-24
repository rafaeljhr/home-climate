"""Shared config, storage and Gree-AC helpers for the humidity control system.

Imported by both humidity_control.py (the automation loop) and webapp.py (the
web UI). It must NOT import bleak — only the controller touches Bluetooth — so
the web image stays slim and Bluetooth-free.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, time as dtime, timezone
from ipaddress import IPv4Address
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from greeclimate.device import Device, Mode
from greeclimate.discovery import Discovery

from test import (  # local module (CLI helpers, no bleak)
    TEMP_RANGES, apply_action, apply_features, clamp_temperature, safe_close,
)

# --- Configuration -----------------------------------------------------------

# The 4-hex code shown in each sensor's name -> room label.
ROOM_BY_CODE = {
    "4D7E": "Master Suite",
    "1E12": "Bedroom",
    "CC1B": "Living Room",
}

# Room label -> AC MAC. Empty => aggregate mode (most humid room drives all ACs).
AC_BY_ROOM = {
    "Master Suite": "c03937ab6236",
    "Bedroom": "c03937acbef3",
    "Living Room": "580d0d322db4",
}

# Humidity thresholds (%RH), configurable via env on the controller.
ON_THRESHOLD = int(os.environ.get("HUMIDITY_ON", "65"))   # at or above -> Dry
OFF_THRESHOLD = int(os.environ.get("HUMIDITY_OFF", "55"))  # at or below -> Off

# --- OFF schedule (configurable via env vars on the controller) --------------
#
# These windows are the periods when the system is OFF (ACs forced off). Outside
# them, the humidity automation runs normally. Specs are comma-separated
# HH:MM-HH:MM ranges in SCHEDULE_TZ; add as many as you like. Windows may cross
# midnight (e.g. 22:00-10:00). An empty spec means "never off" for that day type.
SCHEDULE_TZ = os.environ.get("SCHEDULE_TZ", "Europe/Lisbon")
OFF_WEEKDAY = os.environ.get("OFF_WEEKDAY", "22:00-10:00")
OFF_WEEKEND = os.environ.get("OFF_WEEKEND", "22:00-11:00")

# Daily hard shutdown (HH:MM in SCHEDULE_TZ): forces EVERY AC off once at this
# time, overriding manual Cool/Heat and any pause — a backstop for units left on
# (e.g. Cool forgotten during the day). "" disables it. Adjustable live from the
# web UI (stored in settings.json), which takes precedence over this env default.
FORCE_OFF_AT = os.environ.get("FORCE_OFF_AT", "02:00")

# All persisted state lives under DATA_DIR (a shared Docker volume in production).
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).with_name("data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "control_state.json"   # hysteresis state, keyed per target
STATUS_FILE = DATA_DIR / "status.json"         # snapshot the web UI reads
OVERRIDE_FILE = DATA_DIR / "override.json"      # manual/auto mode, written by the web
SETTINGS_FILE = DATA_DIR / "settings.json"     # user-tweakable settings from the web
LAUNDRY_FILE = DATA_DIR / "laundry.json"       # active laundry-mode session (web-driven)
LOG_FILE = DATA_DIR / "control.log"

CODE_RE = re.compile(r"\(([0-9A-Fa-f]{4})\)")

# --- Logging (shared file the web tails) -------------------------------------

logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("greeclimate").setLevel(logging.WARNING)
logging.getLogger("bleak").setLevel(logging.WARNING)

logger = logging.getLogger("humctl")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    # Roll the decision log at local midnight and keep 7 days of history, so it's
    # bounded (~1 MB/day here) and can never fill the disk.
    _file = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7)
    _file.setFormatter(_fmt)
    _stream = logging.StreamHandler()
    _stream.setFormatter(_fmt)
    logger.addHandler(_file)
    logger.addHandler(_stream)
    logger.propagate = False


# --- Small helpers -----------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def room_for(name):
    match = CODE_RE.search(name or "")
    return ROOM_BY_CODE.get(match.group(1).upper()) if match else None


def decide(humidity, previous):
    """Hysteresis: 'dry', 'off', or hold `previous` in the dead band."""
    if humidity >= ON_THRESHOLD:
        return "dry"
    if humidity <= OFF_THRESHOLD:
        return "off"
    return previous  # may be None on first run while inside the dead band


# --- Schedule ----------------------------------------------------------------

def _parse_hhmm(text):
    hour, minute = text.strip().split(":")
    return dtime(int(hour), int(minute))


def _parse_windows(spec):
    windows = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start, end = part.split("-")
        windows.append((_parse_hhmm(start), _parse_hhmm(end)))
    return windows


WEEKDAY_OFF_WINDOWS = _parse_windows(OFF_WEEKDAY)
WEEKEND_OFF_WINDOWS = _parse_windows(OFF_WEEKEND)


def _schedule_tz():
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(SCHEDULE_TZ)
    except Exception:
        logger.warning("Unknown timezone %r; falling back to local time.", SCHEDULE_TZ)
        return None


def now_local():
    """Current time in the schedule timezone (drives the schedule + daily shutdown)."""
    return datetime.now(_schedule_tz())


def in_off_window(dt=None):
    """Return (off: bool, label: str) — whether the system should be OFF now."""
    if dt is None:
        dt = datetime.now(_schedule_tz())
    windows = WEEKEND_OFF_WINDOWS if dt.weekday() >= 5 else WEEKDAY_OFF_WINDOWS
    if not windows:
        return False, "no off-windows"
    now_t = dt.time()
    for start, end in windows:
        within = (start <= now_t < end) if start <= end else (now_t >= start or now_t < end)
        if within:
            return True, f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
    return False, "running"


# --- JSON storage (atomic writes; two processes share these files) -----------

def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_name = tmp.name
    os.replace(tmp_name, path)  # atomic on POSIX


def load_state():
    return _read_json(STATE_FILE, {})


def save_state(state):
    _write_json(STATE_FILE, state)


def read_status():
    return _read_json(STATUS_FILE, {})


def write_status(status):
    _write_json(STATUS_FILE, status)


def read_override():
    return _read_json(OVERRIDE_FILE, {"mode": "auto"})


def write_override(mode, action=None):
    _write_json(OVERRIDE_FILE, {"mode": mode, "action": action, "ts": now_iso()})


def read_settings():
    return _read_json(SETTINGS_FILE, {})


def write_settings(settings):
    _write_json(SETTINGS_FILE, settings)


def read_laundry():
    return _read_json(LAUNDRY_FILE, {"active": False})


def write_laundry(laundry):
    _write_json(LAUNDRY_FILE, laundry)


def laundry_status():
    """Current laundry session as a display dict; {'active': False} if none/expired."""
    laundry = read_laundry()
    if not laundry.get("active"):
        return {"active": False}
    try:
        until_dt = datetime.fromisoformat(laundry["until"])
    except (KeyError, TypeError, ValueError):
        return {"active": False}
    remaining = (until_dt - now_local()).total_seconds()
    if remaining <= 0:
        return {"active": False}
    return {
        "active": True,
        "room": laundry.get("room"),
        "temp": laundry.get("temp"),
        "until": laundry["until"],
        "remaining_min": int(remaining // 60),
    }


def force_off_at_str():
    """Effective daily force-off time as 'HH:MM' (web UI overrides env), '' if off."""
    val = read_settings().get("force_off_at")
    if val is None:  # not set in the UI -> fall back to the env default
        val = FORCE_OFF_AT
    return (val or "").strip()


def force_off_time():
    """Daily force-off time as a datetime.time, or None if disabled/unparseable."""
    val = force_off_at_str()
    if not val:
        return None
    try:
        return _parse_hhmm(val)
    except (ValueError, AttributeError):
        logger.warning("Invalid force_off_at %r; daily shutdown disabled.", val)
        return None


# --- Gree AC helpers ---------------------------------------------------------

async def discover_acs(wait=6):
    """Return {mac: device_info} for all Gree ACs found on the LAN."""
    discovery = Discovery(timeout=wait)
    try:
        bcast = discovery._get_broadcast_addresses()
        bcast.append(IPv4Address("255.255.255.255"))
        bcast = list(dict.fromkeys(bcast))
        devices = await discovery.scan(wait_for=wait, bcast_ifaces=bcast)
    finally:
        safe_close(discovery)
    return {d.mac: d for d in devices}


async def query_ac_states(acs):
    """Best-effort read of each AC's current power/mode (for display)."""
    async def one(mac, info):
        device = Device(info, timeout=10, bind_timeout=10)
        try:
            await device.bind()
            await device.update_state()
            await asyncio.wait_for(device._valid_state.wait(), timeout=10)
            try:
                mode = Mode(device.mode).name
            except Exception:
                mode = str(device.mode)
            return mac, {
                "ip": str(info.ip),
                "power": bool(device.power),
                "mode": mode,
                "target_temp": device.target_temperature,
                "current_temp": getattr(device, "current_temperature", None),
                "xfan": bool(device.xfan),
                "anion": bool(device.anion),
            }
        except Exception as exc:
            return mac, {"ip": str(info.ip), "error": str(exc)}
        finally:
            safe_close(device)

    pairs = await asyncio.gather(*(one(mac, info) for mac, info in acs.items()))
    return dict(pairs)


# Re-export so callers can `from core import apply_action`.
__all__ = [
    "ROOM_BY_CODE", "AC_BY_ROOM", "ON_THRESHOLD", "OFF_THRESHOLD",
    "DATA_DIR", "STATE_FILE", "STATUS_FILE", "OVERRIDE_FILE", "SETTINGS_FILE",
    "LOG_FILE", "SCHEDULE_TZ", "OFF_WEEKDAY", "OFF_WEEKEND", "FORCE_OFF_AT",
    "TEMP_RANGES", "clamp_temperature", "in_off_window", "now_local",
    "logger", "now_iso", "room_for", "decide",
    "load_state", "save_state", "read_status", "write_status",
    "read_override", "write_override", "read_settings", "write_settings",
    "read_laundry", "write_laundry", "laundry_status",
    "force_off_at_str", "force_off_time",
    "discover_acs", "query_ac_states", "apply_action", "apply_features", "safe_close",
]
