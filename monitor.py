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
CHECK_INTERVAL = 5          # Seconds
OFF_DURATION =  10*60      # 10 minutes

DEVICES_FILE = Path(__file__).parent / "devices.txt"

# ==========================


def log(name, message):
    """Prints a timestamped log line tagged with the device name,
    so output from multiple devices is easy to tell apart in the terminal."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{name}] {message}", flush=True)


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
                log("SYSTEM", f"Skipping invalid line {line_no} in devices.txt: {raw_line!r}")
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
    log(name, f"Connected ({host}).")
    return device


async def monitor_device(name, host):
    """
    Full monitor loop for ONE device.

    IMPORTANT: everything for this device runs inside this function's
    own try/except. If this device errors out, disconnects, or gets
    turned off, it is handled entirely here and does NOT touch or
    affect any other device's task.
    """
    script_turned_off = False
    device = None

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

            log(name, f"Power = {power:.2f} W")

            # Plug already OFF
            if not is_on:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Ignore idle/off appliance
            if power < MIN_POWER:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Trigger
            if power < TRIGGER_POWER and not script_turned_off:

                log(name, "Low power detected.")
                log(name, "Turning OFF...")

                await device.turn_off()

                script_turned_off = True

                log(name, "Waiting 10 minutes...")

                await asyncio.sleep(OFF_DURATION)

                if script_turned_off:
                    log(name, "Turning ON...")

                    await device.turn_on()

                    script_turned_off = False

                    log(name, "Done.")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            # Errors for this device are isolated here - only this
            # device will pause/reconnect, other devices keep running.
            log(name, f"ERROR: {e}")
            log(name, "Reconnecting in 5 seconds...")

            device = None
            await asyncio.sleep(5)


async def main():
    devices = load_devices()

    if not devices:
        log("SYSTEM", "No devices found in devices.txt. Add at least one "
                       "line as 'Name,IP' and run again.")
        return

    device_list_str = ", ".join(f"{n} ({ip})" for n, ip in devices)
    log("SYSTEM", f"Starting monitor for {len(devices)} device(s): {device_list_str}")

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
