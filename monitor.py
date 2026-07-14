import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from kasa import Discover
import os
# ==========================
# Configuration (shared by all devices, same account)
# ==========================

load_dotenv()

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

MIN_POWER = 5               # Ignore below this
TRIGGER_POWER = 300         # Trigger below this
HIGH_POWER_LIMIT = 3000      # SAFETY: turn off INSTANTLY if power reaches/exceeds this (Watts)
                              # Check your plug's datasheet - most Tapo plugs are rated
                              # around 2300W (10A @ 230V). Keep this comfortably BELOW
                              # your plug's actual rated max, not at/above it.
CHECK_INTERVAL = 5          # Seconds
OFF_DURATION =  10*60      # 10 minutes

DEVICES_FILE = Path(__file__).parent / "devices.txt"

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "monitor.log"

# ==========================


def log_event(name, message):
    """
    Prints a timestamped, device-tagged line to the console AND appends
    it to the permanent log file (logs/monitor.log).

    Only call this for IMPORTANT events (connects, errors, power state
    changes, turn off/on actions) - never for routine per-second power
    readings, so the log file stays small and useful.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{name}] {message}"

    print(line, flush=True)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[{timestamp}] [SYSTEM] Failed to write to log file: {e}", flush=True)


def load_devices():
    """
    Reads devices.txt and returns a list of (name, ip) tuples.
    Format per line: Name,IP
    Blank lines and lines starting with # are ignored.
    """
    if not DEVICES_FILE.exists():
        raise FileNotFoundError(f"devices.txt not found at: {DEVICES_FILE}")

    devices = []
    with open(DEVICES_FILE, "r") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 2 or not parts[0] or not parts[1]:
                log_event("SYSTEM", f"Skipping invalid line {line_no} in devices.txt: {raw_line!r}")
                continue

            name, ip = parts
            devices.append((name, ip))

    return devices


async def connect(name, host):
    device = await Discover.discover_single(
        host,
        username=EMAIL,
        password=PASSWORD,
    )
    await device.update()
    log_event(name, f"Connected ({host}).")
    return device


async def monitor_device(name, host):
    """
    Full monitor loop for ONE device.

    IMPORTANT: everything for this device runs inside this function's
    own try/except. If this device errors out, disconnects, or gets
    turned off, it is handled entirely here and does NOT touch or
    affect any other device's task.

    Power is checked every CHECK_INTERVAL seconds, but nothing is
    printed/logged for routine readings. We only log when something
    actually changes: the device turns on/off, or its power crosses
    the TRIGGER_POWER threshold.
    """
    script_turned_off = False
    device = None

    last_is_on = None     # last known ON/OFF state
    last_zone = None      # last known power zone: "LOW" or "NORMAL"
    emergency_stop = False  # True once HIGH_POWER_LIMIT has tripped this device

    while True:
        try:
            if device is None:
                device = await connect(name, host)

            await device.update()

            energy = device.modules["Energy"]

            # IMPORTANT:
            # If this line throws an error, tell me the error.
            power = energy.current_consumption

            is_on = device.is_on

            # --- SAFETY: instant emergency cutoff on dangerously high power ---
            # Checked first, before anything else, every single cycle.
            if is_on and power >= HIGH_POWER_LIMIT:
                log_event(name, f"EMERGENCY: power {power:.2f} W >= {HIGH_POWER_LIMIT} W limit. "
                                 f"Turning OFF immediately!")
                await device.turn_off()

                emergency_stop = True
                script_turned_off = False   # this is not the normal low-power cycle
                last_is_on = False
                last_zone = None

                log_event(name, "Device stopped for safety and will stay OFF. "
                                 "Turn it back on manually (Tapo app) once it's safe.")

                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # If a device that was emergency-stopped is manually turned back
            # on (via the Tapo app), notice it and resume normal monitoring.
            if emergency_stop and is_on:
                log_event(name, "Device was manually turned back ON after an emergency stop. "
                                 "Resuming normal monitoring.")
                emergency_stop = False

            # Log ON/OFF changes (e.g. someone flips it manually, or the
            # script itself flips it - either way it's worth recording).
            if last_is_on is not None and is_on != last_is_on:
                log_event(name, f"Device is now {'ON' if is_on else 'OFF'}.")
            last_is_on = is_on

            # Plug already OFF - nothing else to check this cycle
            if not is_on:
                last_zone = None
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Ignore idle/off appliance (device on, but nothing plugged
            # in / drawing negligible power) - not a "real" reading
            if power < MIN_POWER:
                last_zone = None
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Track LOW vs NORMAL power zone, but only log the transition
            current_zone = "LOW" if power < TRIGGER_POWER else "NORMAL"
            if current_zone != last_zone:
                log_event(name, f"Power dropped below {TRIGGER_POWER}W (now {power:.2f} W)."
                                 if current_zone == "LOW" else
                                 f"Power back to normal ({power:.2f} W).")
                last_zone = current_zone

            # Trigger
            if power < TRIGGER_POWER and not script_turned_off:

                log_event(name, f"Low power detected ({power:.2f} W). Turning OFF...")

                await device.turn_off()

                script_turned_off = True
                last_is_on = False

                log_event(name, "Waiting 10 minutes before turning back ON...")

                await asyncio.sleep(OFF_DURATION)

                if script_turned_off:
                    log_event(name, "Turning ON...")

                    await device.turn_on()

                    script_turned_off = False
                    last_is_on = True
                    last_zone = None

                    log_event(name, "Device turned back ON. Done.")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            # Errors for this device are isolated here - only this
            # device will pause/reconnect, other devices keep running.
            log_event(name, f"ERROR: {e}")
            log_event(name, "Reconnecting in 5 seconds...")

            device = None
            await asyncio.sleep(5)


async def main():
    devices = load_devices()

    if not devices:
        log_event("SYSTEM", "No devices found in devices.txt. Add at least one "
                             "line as 'Name,IP' and run again.")
        return

    device_list_str = ", ".join(f"{n} ({ip})" for n, ip in devices)
    log_event("SYSTEM", f"Starting monitor for {len(devices)} device(s): {device_list_str}")
    log_event("SYSTEM", f"Logging important events to: {LOG_FILE}")

    # Each device gets its own independent task. return_exceptions=True
    # means that even in the unlikely case a task raises outside its own
    # try/except, it will NOT crash the other devices' tasks.
    tasks = [
        asyncio.create_task(monitor_device(name, host), name=name)
        for name, host in devices
    ]

    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
