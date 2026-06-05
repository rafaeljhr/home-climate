import argparse
import asyncio
import logging
import textwrap
from ipaddress import IPv4Address

from greeclimate.device import Device, Mode, TemperatureUnits
from greeclimate.discovery import Discovery

# Allowed target-temperature range and default per heating/cooling action (°C).
TEMP_RANGES = {
    "dry": (20, 24),
    "cool": (20, 24),
    "heat": (26, 30),
}
DEFAULT_TEMP = {"dry": 20, "cool": 22, "heat": 26}
MODE_BY_ACTION = {"cool": Mode.Cool, "dry": Mode.Dry, "heat": Mode.Heat}


def clamp_temperature(action: str, temperature) -> int:
    """Clamp a requested temperature into the action's allowed range."""
    lo, hi = TEMP_RANGES[action]
    if temperature is None:
        return DEFAULT_TEMP[action]
    return max(lo, min(hi, int(round(float(temperature)))))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover and control Gree AC units on the local network.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Actions:
              discover  Only list discovered ACs. This is the default.
              off       Turn off all discovered ACs.
              cool      Turn on all discovered ACs in Cool mode (20-24 C, default 22).
              dry       Turn on all discovered ACs in Dry mode (20-24 C, default 20).
              heat      Turn on all discovered ACs in Heat mode (26-30 C, default 26).

            Examples:
              python test.py
              python test.py --action off
              python test.py --action cool
              python test.py --action dry
              python test.py --action cool --yes
              python test.py --wait 10 --broadcast 192.168.1.255
              python test.py --action off --wait 10 --yes

            Notes:
              - Control actions ask for confirmation unless --yes is used.
              - Your computer must be on the same Wi-Fi/VLAN as the ACs.
              - Disable VPNs that block local network access.
              - Discovery uses UDP port 7000 broadcast traffic.
            """
        ),
    )
    parser.add_argument(
        "-w",
        "--wait",
        type=int,
        default=5,
        metavar="SECONDS",
        help="seconds to wait for discovery replies (default: %(default)s)",
    )
    parser.add_argument(
        "-b",
        "--broadcast",
        action="append",
        type=IPv4Address,
        metavar="ADDRESS",
        help="broadcast address to scan; can be used multiple times",
    )
    parser.add_argument(
        "--no-limited-broadcast",
        action="store_true",
        help="do not add 255.255.255.255 to the discovered broadcast addresses",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "-a",
        "--action",
        choices=("discover", "off", "cool", "dry", "heat"),
        default="discover",
        help="action to run on all discovered devices (default: %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--temp",
        type=int,
        metavar="C",
        help="target temperature in C for cool/dry (20-24) or heat (26-30); "
        "clamped to range, defaults per mode",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="auto-approve the selected action without prompting",
    )
    parser.add_argument(
        "-m",
        "--mac",
        action="append",
        metavar="MAC",
        help="only act on devices with this MAC (repeatable); useful for "
        "identifying which AC is in which room",
    )
    return parser.parse_args()


def safe_close(protocol) -> None:
    transport = getattr(protocol, "_transport", None)
    if transport is not None:
        protocol.close()


def describe_device(device) -> None:
    print(f"- {device.name} @ {device.ip}:{device.port}")
    print(f"  mac: {device.mac}")
    if device.brand:
        print(f"  brand: {device.brand}")
    if device.model:
        print(f"  model: {device.model}")
    if device.version:
        print(f"  version: {device.version}")


def action_description(action: str, temp=None) -> str:
    if action == "off":
        return "turn off all discovered ACs"
    return f"turn on all discovered ACs in {action.capitalize()} mode at {clamp_temperature(action, temp)} C"


def confirm_action(action: str, devices, temp=None) -> bool:
    print()
    print(f"About to {action_description(action, temp)}:")
    for device in devices:
        print(f"- {device.name} @ {device.ip}:{device.port} (mac: {device.mac})")

    answer = input("Do you want to proceed with these ACs? [y/N] ").strip().lower()
    return answer in ("y", "yes")


async def discover(args: argparse.Namespace) -> int:
    discovery = Discovery(timeout=args.wait)
    try:
        broadcast_addresses = args.broadcast or discovery._get_broadcast_addresses()
        if not args.broadcast and not args.no_limited_broadcast:
            broadcast_addresses.append(IPv4Address("255.255.255.255"))
        broadcast_addresses = list(dict.fromkeys(broadcast_addresses))
        if not broadcast_addresses:
            print("No IPv4 broadcast addresses found. Try passing --broadcast <address>.")
            return 1

        print("Scanning:", ", ".join(str(address) for address in broadcast_addresses))
        devices = await discovery.scan(wait_for=args.wait, bcast_ifaces=broadcast_addresses)

        task_errors = [task.exception() for task in discovery.tasks if task.done() and task.exception()]
        if task_errors:
            for error in task_errors:
                print(f"Discovery error: {error}")
            return 1
    finally:
        safe_close(discovery)

    if args.mac:
        wanted = {m.lower().replace(":", "") for m in args.mac}
        devices = [d for d in devices if d.mac.lower().replace(":", "") in wanted]
        if not devices:
            print(f"No discovered ACs matched --mac {', '.join(args.mac)}.")
            return 1

    if not devices:
        print("No Gree ACs found.")
        print("Make sure your Mac is on the same Wi-Fi/VLAN as the ACs and UDP/7000 broadcast is allowed.")
        return 1

    print(f"Found {len(devices)} Gree device(s):")
    for device in devices:
        describe_device(device)

    if args.action == "discover":
        return 0

    if not args.yes and not confirm_action(args.action, devices, args.temp):
        print("Cancelled.")
        return 0

    print(f"Running action '{args.action}' on all discovered devices...")
    results = await asyncio.gather(
        *(apply_action(device, args.action, args.temp) for device in devices),
        return_exceptions=True,
    )

    failed = False
    for device, result in zip(devices, results):
        if isinstance(result, Exception):
            failed = True
            print(f"- {device.name} @ {device.ip}: failed: {result}")
        else:
            print(f"- {device.name} @ {device.ip}: {result}")

    return 1 if failed else 0


async def apply_action(device_info, action: str, temperature=None) -> str:
    device = Device(device_info, timeout=10, bind_timeout=10)
    try:
        await device.bind()
        await device.update_state()
        await asyncio.wait_for(device._valid_state.wait(), timeout=10)

        if action == "off":
            device.power = False
            message = "powered off"
        elif action in MODE_BY_ACTION:
            temp = clamp_temperature(action, temperature)
            device.power = True
            device.temperature_units = TemperatureUnits.C
            device.mode = MODE_BY_ACTION[action]
            device.target_temperature = temp
            message = f"set to {action} at {temp} C"
        else:
            raise ValueError(f"Unsupported action: {action}")

        await device.push_state_update()
        return message
    finally:
        safe_close(device)


def main() -> int:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.getLogger().setLevel(log_level)
    logging.getLogger("asyncio").setLevel(log_level)
    logging.getLogger("greeclimate").setLevel(log_level)
    return asyncio.run(discover(args))


if __name__ == "__main__":
    raise SystemExit(main())
