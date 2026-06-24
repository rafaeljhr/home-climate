#!/usr/bin/env python3
"""Humidity-driven control of Gree AC units, using ThermoPro TP3xx sensors.

When a room's humidity rises above the ON threshold (default 65%), the AC is
switched to Dry mode. When it falls to the OFF threshold (default 54%) or below,
the AC is turned off. Between the two thresholds it holds its current state
(hysteresis), preventing rapid on/off flapping.

A single Bluetooth scan runs continuously for the whole life of the process, so
every sensor — including weak ones — keeps getting picked up. The control loop
refreshes the status snapshot (data/status.json) frequently from the latest
readings, and re-evaluates the ACs every --poll seconds. Logs go to
data/control.log. If the web UI has set a manual override, automation is paused
until "Resume Auto".

Modes (auto-selected): per-room when core.AC_BY_ROOM is filled in, else aggregate
(the most humid room drives every AC).

Run:
  ./run.sh control            # continuous loop
  ./run.sh control --once     # single check, then exit (for cron/launchd)
  ./run.sh control --dry-run  # decide & log, but never touch an AC
"""

import argparse
import asyncio
import time
from datetime import datetime, timedelta

from bleak import BleakScanner

from core import (
    AC_BY_ROOM, OFF_THRESHOLD, OFF_WEEKDAY, OFF_WEEKEND, ON_THRESHOLD,
    ROOM_BY_CODE, SCHEDULE_TZ,
    apply_action, decide, discover_acs, force_off_at_str, force_off_time,
    in_off_window, laundry_status, load_state, logger, now_iso, now_local,
    query_ac_states, read_laundry, read_status, room_for,
    save_state, write_laundry, write_status,
)
from humidity_sensors import DEFAULT_PREFIX, flush_bluez_cache, make_collector


def build_targets(rooms, acs):
    """Map humidity readings to control targets: [(key, label, humidity, [dev])]."""
    if AC_BY_ROOM:  # per-room mode
        targets = []
        for room, humidity in sorted(rooms.items()):
            mac = AC_BY_ROOM.get(room)
            device = acs.get(mac) if mac else None
            if device is not None:
                targets.append((mac, room, humidity, [device]))
        return targets

    if not rooms:  # aggregate mode: most humid room drives every AC
        return []
    driver = max(rooms, key=rooms.get)
    return [("ALL", f"all ACs (driven by {driver})", rooms[driver], list(acs.values()))]


def controlled_units(acs):
    """Every AC we manage, ignoring sensors: [(key, label, [device]), ...].

    Used when forcing the ACs off outside the automation schedule.
    """
    if AC_BY_ROOM:
        units = []
        for room, mac in AC_BY_ROOM.items():
            device = acs.get(mac)
            if device is not None:
                units.append((mac, room, [device]))
        return units
    return [("ALL", "all ACs", list(acs.values()))]


def _ac_in_state(desired, ac):
    """True if the AC's actual (queried) state already matches `desired`."""
    if not ac or ac.get("error"):
        return False  # unknown -> act, to be safe
    if desired == "off":
        return ac.get("power") is False
    # cool / dry / heat: must be powered on and in that mode
    return bool(ac.get("power")) and str(ac.get("mode", "")).lower() == desired


def _is_manual_hold(ac):
    """True if a person put this AC in a mode the automation never commands.

    Humidity control only ever sets 'dry' or 'off', so a unit powered on in any
    other mode (cool/heat/auto/fan) — e.g. Cool from the Gree app — was changed
    by a human. We leave it under their control and don't reconcile it. The hold
    is stateless: it clears itself as soon as the unit is back to dry or off.
    """
    if not ac or ac.get("error"):
        return False
    return bool(ac.get("power")) and str(ac.get("mode", "")).lower() != "dry"


def _state_from_ac(ac):
    """The controller state ('dry'/'off') a reachable AC is actually in, else None.

    Used to hand control back smoothly: if you manually switch a unit to Dry or
    Off, the hysteresis is re-seeded from that live state so automation continues
    from where you left it (Dry keeps drying until ≤OFF%, Off stays off until
    >ON%) instead of snapping to a stale earlier decision. Cool/Heat is treated as
    a manual hold elsewhere and never reaches here.
    """
    if not ac or ac.get("error"):
        return None
    if not ac.get("power"):
        return "off"
    return "dry" if str(ac.get("mode", "")).lower() == "dry" else None


def _force_off_due(prev_wall, now_wall):
    """True exactly once, when the wall clock crosses the daily force-off time.

    Edge-triggered on the configured HH:MM so it fires a single shot per day and
    never spuriously re-fires (or catches up hours later) after a restart.
    """
    ft = force_off_time()
    if ft is None or prev_wall is None:
        return False
    target = now_wall.replace(hour=ft.hour, minute=ft.minute, second=0, microsecond=0)
    return prev_wall < target <= now_wall


async def apply_target(key, label, desired, devices, state, dry_run, ac_states):
    """Drive a target toward `desired`, comparing against the ACs' ACTUAL state.

    `state` is the automation's last *decision* (used by decide() for dead-band
    holds); whether to actually send a command is decided from the live AC state,
    so manual changes made elsewhere (e.g. the web UI) are reconciled correctly.
    """
    if desired is None:
        return f"{label} — dead-band, no prior state — hold"
    if not devices:
        return f"{label} — want '{desired}' but no AC available"
    if all(_ac_in_state(desired, ac_states.get(d.mac)) for d in devices):
        state[key] = desired
        return f"{label} — already '{desired}'"
    if dry_run:
        state[key] = desired
        return f"{label} => WOULD set '{desired}' (dry-run)"
    results = await asyncio.gather(
        *(apply_action(d, desired) for d in devices), return_exceptions=True
    )
    errors = [str(r) for r in results if isinstance(r, Exception)]
    if errors:
        return f"{label} => '{desired}' FAILED: {'; '.join(errors)}"
    state[key] = desired
    return f"{label} => set '{desired}'"


def snapshot_sensors(sensors, last_sensors, max_age):
    """Read the live (continuously scanned) sensor dict.

    Updates `last_sensors` (for the status display) with every known reading and
    returns {room: humidity} for readings fresh enough (<= max_age seconds) to
    drive decisions. A continuously-running scanner keeps these fresh.
    """
    now_mono = time.monotonic()
    now_wall = datetime.now().astimezone()
    fresh = {}
    for reading in list(sensors.values()):
        age = now_mono - reading["last_seen"]
        room = room_for(reading["name"]) or reading["name"]
        last_sensors[room] = {
            "humidity": reading["humidity"],
            "temperature_c": reading["temperature_c"],
            "battery": reading["battery"],
            "rssi": reading["rssi"],
            "last_seen_iso": (now_wall - timedelta(seconds=age)).isoformat(timespec="seconds"),
        }
        if age <= max_age:
            fresh[room] = reading["humidity"]
    return fresh


async def handle_laundry(args, acs, ac_states):
    """Keep the laundry room's AC in Dry @ its set temp while a session is active.

    Laundry mode is the highest precedence and is Bedroom-only by default: it
    overrides the OFF schedule, the daily force-off and humidity logic. It does
    NOT auto-off — when the timer ends it just clears, and normal automation
    takes the unit back over from its live state. Returns (laundry_mac, log_line):
    laundry_mac is the unit the controller should leave out of normal logic this
    cycle (None when no session is active).
    """
    laundry = read_laundry()
    if not laundry.get("active"):
        return None, None

    room = laundry.get("room", "Bedroom")
    mac = AC_BY_ROOM.get(room)
    try:
        until_dt = datetime.fromisoformat(laundry["until"])
    except (KeyError, TypeError, ValueError):
        until_dt = None

    if until_dt is None or now_local() >= until_dt:
        write_laundry({"active": False})
        return None, f"laundry ({room}) finished — back to automation"

    remaining = int((until_dt - now_local()).total_seconds() // 60)
    temp = int(laundry.get("temp", 16))
    device = acs.get(mac)
    if device is None:
        return mac, f"laundry ({room}) — AC not found ({remaining}min left)"

    ac = ac_states.get(mac) or {}
    in_state = (bool(ac.get("power")) and str(ac.get("mode", "")).lower() == "dry"
                and ac.get("target_temp") == temp)
    if in_state:
        return mac, f"laundry ({room}) — Dry {temp}°C, {remaining}min left"
    if args.dry_run:
        return mac, f"laundry ({room}) => WOULD set Dry {temp}°C ({remaining}min left)"
    try:
        msg = await apply_action(device, "laundry", temp)
        return mac, f"laundry ({room}) => {msg} ({remaining}min left)"
    except Exception as exc:
        return mac, f"laundry ({room}) => FAILED: {exc}"


async def decide_and_act(args, acs, state, rooms, ac_states, enforce_off=True,
                         force_off=False):
    """Run the control logic once and return the list of decision log lines.

    Precedence: laundry > daily force-off > OFF schedule > humidity logic.
    Actuation is reconciled against the live AC states. Automation is always on;
    a unit a human put in Cool/Heat is left alone per-room (see _is_manual_hold),
    and reclaimed automatically once it's back in Dry/Off — there is no global
    pause.

    `enforce_off` is True only on the cycle the OFF window is first entered: the
    schedule forces every unit off then. On later cycles it's False, so a unit a
    human switched back on (e.g. via the Gree app) is left alone instead of being
    fought every poll — manual control wins inside the window once it's started.

    `force_off` is True for the single cycle the daily force-off time is crossed:
    every AC is turned off unconditionally, overriding manual Cool/Heat — the
    backstop for units left on (laundry mode excepted).
    """
    decisions = []

    # Laundry mode (Bedroom-only) outranks everything; its unit is left out of
    # the normal logic below for this cycle.
    laundry_mac, laundry_line = await handle_laundry(args, acs, ac_states)
    if laundry_line:
        decisions.append(laundry_line)
    if laundry_mac is not None:
        acs = {m: d for m, d in acs.items() if m != laundry_mac}
        rooms = {r: h for r, h in rooms.items() if AC_BY_ROOM.get(r) != laundry_mac}

    off_now, _ = in_off_window()

    if force_off:
        hhmm = force_off_at_str()
        for key, label, devices in controlled_units(acs):
            decisions.append(await apply_target(
                key, f"{label} [daily force-off {hhmm}]", "off", devices, state,
                args.dry_run, ac_states))
    elif off_now:
        _, window_label = in_off_window()
        for key, label, devices in controlled_units(acs):
            states = [ac_states.get(d.mac) for d in devices]
            if devices and all(_is_manual_hold(ac) for ac in states):
                mode = str((states[0] or {}).get("mode", "")).lower()
                decisions.append(
                    f"{label} [off-window {window_label}] — manual {mode}, leaving alone")
                continue
            powered_on = any(bool((ac or {}).get("power")) for ac in states)
            if not enforce_off and powered_on:
                decisions.append(
                    f"{label} [off-window {window_label}] — manual ON, respecting")
                continue
            decisions.append(await apply_target(
                key, f"{label} [off-window {window_label}]", "off", devices, state,
                args.dry_run, ac_states))
    else:
        targets = build_targets(rooms, acs)
        if not targets:
            decisions.append("no fresh sensor readings — ACs unchanged")
        for key, label, humidity, devices in targets:
            states = [ac_states.get(d.mac) for d in devices]
            if devices and all(_is_manual_hold(ac) for ac in states):
                mode = str((states[0] or {}).get("mode", "")).lower()
                decisions.append(f"{label}: {humidity}% — manual {mode}, leaving alone")
                continue
            implied = {_state_from_ac(ac) for ac in states} - {None}
            if len(implied) == 1:  # take over from the unit's live dry/off state
                state[key] = implied.pop()
            desired = decide(humidity, state.get(key))
            decisions.append(await apply_target(
                key, f"{label}: {humidity}%", desired, devices, state,
                args.dry_run, ac_states))

    for line in decisions:
        logger.info(line)
    if not args.dry_run:
        save_state(state)
    return decisions


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--once", action="store_true",
                   help="run a single check and exit (for cron/launchd)")
    p.add_argument("--poll", type=float, default=30.0, metavar="SECONDS",
                   help="seconds between AC re-evaluations (default: 30)")
    p.add_argument("--refresh", type=float, default=1.0, metavar="SECONDS",
                   help="seconds between status/sensor refreshes (default: 1)")
    p.add_argument("--max-age", type=float, default=900.0, metavar="SECONDS",
                   help="ignore sensor readings older than this for decisions "
                        "(default: 900 = 15 min)")
    p.add_argument("--flush-interval", type=float, default=10.0, metavar="SECONDS",
                   help="evict sensors from the BlueZ cache this often so their "
                        "readings can't freeze on a stale advertisement "
                        "(default: 10; 0 = never)")
    p.add_argument("--ac-wait", type=int, default=6, metavar="SECONDS",
                   help="seconds to wait for AC discovery (default: 6)")
    p.add_argument("--prefix", default=DEFAULT_PREFIX,
                   help=f"sensor name prefix (default: {DEFAULT_PREFIX!r})")
    p.add_argument("--dry-run", action="store_true",
                   help="decide and log actions but don't change any AC")
    return p.parse_args()


def _status_mode(off_now):
    return "scheduled-off" if off_now else "auto"


async def main_async():
    args = parse_args()
    mode = "per-room" if AC_BY_ROOM else "aggregate"
    logger.info("Humidity control starting (mode=%s, on>%s%%, off<=%s%%%s)",
                mode, ON_THRESHOLD, OFF_THRESHOLD, ", DRY-RUN" if args.dry_run else "")
    logger.info("OFF schedule (%s): weekday off=[%s] weekend off=[%s]",
                SCHEDULE_TZ, OFF_WEEKDAY, OFF_WEEKEND)

    # One continuous Bluetooth scan for the whole process — always picking up sensors.
    sensors, callback = make_collector(args.prefix)
    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    logger.info("Continuous BLE scan started (prefix=%s)", args.prefix)

    acs = await discover_acs(args.ac_wait)
    logger.info("Discovered %d AC(s): %s", len(acs), ", ".join(acs) or "none")

    state = load_state()
    last_sensors = dict(read_status().get("sensors", {}))  # survive restarts
    ac_states = {}
    decisions = []
    next_decision = 0.0  # decide immediately on first tick
    next_flush = 0.0 if args.flush_interval else None  # flush once at startup too
    was_off_window = False  # to detect the moment we enter an OFF window
    last_decision_wall = None  # wall clock at the previous decision (force-off edge)

    try:
        while True:
            if next_flush is not None and time.monotonic() >= next_flush:
                try:
                    removed = await flush_bluez_cache(args.prefix)
                    if removed:
                        logger.debug("Flushed BlueZ cache for: %s", ", ".join(removed))
                except Exception as exc:  # never let a flush error kill the loop
                    logger.warning("BlueZ cache flush failed: %s", exc)
                next_flush = time.monotonic() + args.flush_interval

            rooms = snapshot_sensors(sensors, last_sensors, args.max_age)

            if time.monotonic() >= next_decision:
                try:
                    acs.update(await discover_acs(args.ac_wait))  # DHCP-safe refresh
                    ac_states = await query_ac_states(acs)
                    off_now_decision, _ = in_off_window()
                    enforce_off = off_now_decision and not was_off_window  # window just entered
                    now_wall = now_local()
                    force_off = _force_off_due(last_decision_wall, now_wall)
                    decisions = await decide_and_act(
                        args, acs, state, rooms, ac_states, enforce_off, force_off)
                    was_off_window = off_now_decision
                    last_decision_wall = now_wall
                except Exception as exc:  # keep the loop alive across transient errors
                    logger.exception("Decision error: %s", exc)
                next_decision = time.monotonic() + args.poll

            off_now, window_label = in_off_window()
            write_status({
                "updated_at": now_iso(),
                "mode": _status_mode(off_now),
                "dry_run": args.dry_run,
                "control_mode": mode,
                "thresholds": {"on": ON_THRESHOLD, "off": OFF_THRESHOLD},
                "schedule": {
                    "off_now": off_now, "window": window_label, "tz": SCHEDULE_TZ,
                    "weekday_off": OFF_WEEKDAY, "weekend_off": OFF_WEEKEND,
                    "force_off_at": force_off_at_str(),
                },
                "laundry": laundry_status(),
                "sensors": last_sensors,
                "acs": ac_states,
                "decisions": decisions,
            })

            if args.once:
                break
            await asyncio.sleep(args.refresh)
    finally:
        await scanner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
