# Running on a Raspberry Pi

Two containers, managed by `docker-compose.yml`:

- **controller** — scans the ThermoPro sensors over Bluetooth and drives the ACs
  (Dry above 65 % RH, Off at/below 54 %, hysteresis in between). Writes
  `status.json` + `control.log` to the shared `data` volume.
- **web** — UI at `http://<pi-ip>:8000` showing the latest sensor/AC state and
  logs, with buttons to turn the ACs Off / Dry / Cool (and Resume Auto).

## Prerequisites (on the Pi)

- 64-bit Raspberry Pi OS recommended (arm64 wheels for the Python deps).
- Docker + the Compose plugin: `curl -fsSL https://get.docker.com | sh`
- BlueZ running on the host (default on Pi OS): `systemctl status bluetooth`
- Pi on the **same Wi-Fi/VLAN** as the ACs, with UDP/7000 broadcast allowed.

## Run it

```bash
git clone https://github.com/rafaeljhr/home-climate.git
cd home-climate
cp .env.example .env        # then edit .env to set WEB_USER / WEB_PASSWORD
docker compose up -d --build
docker compose logs -f controller      # watch decisions
```

Open `http://<pi-ip>:8000`. To stop: `docker compose down`.
`restart: unless-stopped` brings both back on reboot.

## Bluetooth in Docker — the one tricky bit

The controller talks to the host's BlueZ over the mounted D-Bus socket
(`/run/dbus`) and uses host networking. If Bluetooth scanning fails with a
D-Bus / permission error, edit `docker-compose.yml`: under `controller`, replace
the `cap_add` block with `privileged: true` and `docker compose up -d`.

If sensors still don't appear: make sure the **ThermoPro phone app is closed**
(an active connection can stop a sensor broadcasting), and that the weak sensor
(Master Suite, ~-89 dBm) is in range — raise `--scan-window` in
`Dockerfile.controller` if needed.

## Per-room control (optional upgrade)

Right now it runs in **aggregate** mode (the most humid room drives all ACs),
because the AC→room mapping isn't known yet. To control each room's AC from its
own sensor, identify the ACs by flipping one at a time:

```bash
./run.sh --action cool --mac c03937acbef3 --yes   # which room gets cold?
./run.sh --action off  --mac c03937acbef3 --yes
```

Then fill in `AC_BY_ROOM` near the top of `core.py`, e.g.
`"Living Room": "c03937acbef3"`, and rebuild. The controller switches to
per-room mode automatically once that map is non-empty.

## Tuning (env vars on the `controller` service in `docker-compose.yml`)

- `HUMIDITY_ON` / `HUMIDITY_OFF` — thresholds in %RH (default 65 / 54): above ON
  → Dry, at/below OFF → Off.
- `SCHEDULE_TZ` — timezone for the schedule (default `Europe/Lisbon`).
- `OFF_WEEKDAY` / `OFF_WEEKEND` — **periods when the system is OFF** (ACs forced
  off); outside them the humidity automation runs. Comma-separated
  `HH:MM-HH:MM` ranges; add as many as you like; windows may cross midnight,
  e.g. `22:00-10:00`. Defaults:
  - weekday `13:00-17:00,22:00-10:00`
  - weekend `22:00-11:00,13:00-17:00`

Web UI auth (env vars on the `web` service):

- `WEB_USER` / `WEB_PASSWORD` — set **both** to require HTTP Basic auth on the UI.
  Leave unset to disable. Basic auth is not encrypted without HTTPS, so only rely
  on it within a trusted LAN.

Other:

- Poll interval / scan window: the `CMD` in `Dockerfile.controller`.
- Per-mode temperatures from the web UI: Dry/Cool 20–24 °C, Heat 26–30 °C.
- Manual buttons pause automation until **Resume Auto** is pressed.
