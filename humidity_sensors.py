"""Passively read temperature & humidity from ThermoPro TP3xx BLE sensors.

TP3xx sensors (TP350/TP350S, TP357, TP358, ...) broadcast their readings in the
BLE advertisement's manufacturer data. We never connect to them; we listen
passively with a continuous scan and keep the latest reading seen per device.
"""

import argparse
import asyncio
import struct
import time
from datetime import datetime

from bleak import BleakScanner

# bleak splits the advertisement into {company_id: payload}. ThermoPro doesn't
# use a real company id, so those 2 bytes are actually sensor data. We re-prepend
# them (little-endian) to rebuild the original payload, then parse from offset 1.
# Byte layout mirrors the `thermopro-ble` library.
DEFAULT_PREFIX = "TP3"
BATTERY_MAP = {0: 1, 1: 50, 2: 100}


def parse_thermopro(manufacturer_data):
    if not manufacturer_data:
        return None

    company_id = list(manufacturer_data)[-1]
    raw = company_id.to_bytes(2, "little") + manufacturer_data[company_id]
    if len(raw) < 6:
        return None
    if raw[1:4] == b"\xff\xff\xff":  # sensor warming up / no reading yet
        return None

    temp_raw, humidity = struct.unpack("<hB", raw[1:4])
    return {
        "temperature_c": temp_raw / 10,
        "humidity": humidity,
        "battery": BATTERY_MAP.get(raw[4] & 0x03),
    }


def make_collector(prefix):
    """Return (sensors_dict, detection_callback).

    The dict is updated in place with the latest reading per device whose
    advertised name starts with `prefix`. Shared by the live display and by
    collect_readings() so the parsing/matching logic lives in one place.
    """
    sensors = {}

    def callback(device, adv):
        name = device.name or adv.local_name or ""
        if not name.startswith(prefix):
            return
        reading = parse_thermopro(adv.manufacturer_data)
        if reading is None:
            return
        sensors[device.address] = {
            "name": name,
            "rssi": adv.rssi,
            "last_seen": time.monotonic(),
            **reading,
        }

    return sensors, callback


async def collect_readings(window=30.0, interval=0.5, prefix=DEFAULT_PREFIX, expected=0):
    """Scan up to `window` seconds and return {address: reading}.

    Stops early once `expected` distinct sensors are seen (0 = full window).
    Intended for programmatic use (e.g. the humidity controller).
    """
    sensors, callback = make_collector(prefix)
    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    deadline = time.monotonic() + window
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            if expected and len(sensors) >= expected:
                break
    finally:
        await scanner.stop()
    return sensors


async def flush_bluez_cache(prefix=DEFAULT_PREFIX, adapter="hci0"):
    """Evict matching sensors from BlueZ's device cache (return removed names).

    On a long-running scan BlueZ refreshes a device's RSSI on every advertisement
    but caches its ManufacturerData, so bleak keeps handing us a *stale* payload
    while last_seen looks fresh — readings silently freeze. Removing the device
    forces BlueZ to repopulate ManufacturerData from the next advertisement (which
    the continuous scan picks up within a second or two). Best-effort: any error
    (e.g. device already gone) is ignored.
    """
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType
    from dbus_fast import Message

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    removed = []
    try:
        reply = await bus.call(Message(
            destination="org.bluez", path="/",
            interface="org.freedesktop.DBus.ObjectManager",
            member="GetManagedObjects"))
        adapter_path = f"/org/bluez/{adapter}"
        for path, ifaces in reply.body[0].items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not path.startswith(adapter_path):
                continue
            nv = dev.get("Name") or dev.get("Alias")
            name = nv.value if nv is not None else ""
            if not name.startswith(prefix):
                continue
            await bus.call(Message(
                destination="org.bluez", path=adapter_path,
                interface="org.bluez.Adapter1", member="RemoveDevice",
                signature="o", body=[path]))
            removed.append(name)
    finally:
        bus.disconnect()
    return removed


def render(sensors, elapsed, window):
    ts = datetime.now().strftime("%H:%M:%S")
    now = time.monotonic()
    print(f"[{ts}]  {len(sensors)} sensor(s)  "
          f"({elapsed:.0f}s / {window:.0f}s elapsed)")
    for _, s in sorted(sensors.items(), key=lambda kv: kv[1]["name"]):
        temp_c = s["temperature_c"]
        age = now - s["last_seen"]
        batt = f"   battery ~{s['battery']}%" if s["battery"] is not None else ""
        print(
            f"  {s['name']:<14} "
            f"{temp_c:5.1f} °C / {temp_c * 9 / 5 + 32:5.1f} °F   "
            f"{s['humidity']:3d} %RH   "
            f"RSSI {s['rssi']:>4} dBm   "
            f"(seen {age:.0f}s ago){batt}"
        )
    print()


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Tip: close the ThermoPro phone app while scanning — an active "
               "connection can stop a sensor from broadcasting.",
    )
    p.add_argument(
        "-w", "--window", type=float, default=300.0, metavar="SECONDS",
        help="Total scan window in seconds (default: 300 = 5 minutes).",
    )
    p.add_argument(
        "-i", "--interval", type=float, default=1.0, metavar="SECONDS",
        help="Seconds between on-screen refreshes (default: 1).",
    )
    p.add_argument(
        "-n", "--count", type=int, default=3, metavar="N",
        help="Stop early once N distinct sensors are seen. 0 = scan the full "
             "window (default: 3).",
    )
    p.add_argument(
        "-p", "--prefix", default=DEFAULT_PREFIX, metavar="STR",
        help=f"Device name prefix to match (default: {DEFAULT_PREFIX!r}).",
    )
    return p.parse_args()


async def main():
    args = parse_args()
    sensors, callback = make_collector(args.prefix)

    print(f"Scanning for {args.prefix}xx sensors for up to {args.window:.0f}s "
          f"(refresh every {args.interval:.0f}s)...\n")

    scanner = BleakScanner(detection_callback=callback)
    await scanner.start()
    start = time.monotonic()
    deadline = start + args.window
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(min(args.interval, deadline - time.monotonic()))
            render(sensors, time.monotonic() - start, args.window)
            if args.count and len(sensors) >= args.count:
                print(f"All {args.count} sensor(s) found — stopping early.\n")
                break
    finally:
        await scanner.stop()

    if not sensors:
        print("No sensors found. Make sure they're powered on, nearby, and that "
              "the ThermoPro phone app isn't connected to them.")


if __name__ == "__main__":
    asyncio.run(main())
