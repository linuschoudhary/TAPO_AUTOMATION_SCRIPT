import asyncio
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from kasa import Discover
import os

import history
# ==========================
# Configuration (shared by all devices, same account)
# ==========================

load_dotenv()

EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")

MIN_POWER = 5               # Ignore below this
TRIGGER_POWER = 400         # Trigger below this
HIGH_POWER_LIMIT = 30000      # SAFETY: turn off INSTANTLY if power reaches/exceeds this (Watts)
                              # Check your plug's datasheet - most Tapo plugs are rated
                              # around 2300W (10A @ 230V). Keep this comfortably BELOW
                              # your plug's actual rated max, not at/above it.
CHECK_INTERVAL = 3          # Seconds
OFF_DURATION =  60*60      # 60 minutes

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

    NOTE ON THE OFF-WAIT PERIOD:
    We do NOT block the loop with asyncio.sleep(OFF_DURATION) anymore.
    Instead we record a resume_time timestamp and keep checking every
    CHECK_INTERVAL like normal. This means:
      - a network error during the wait can never get script_turned_off
        stuck True forever (the old bug),
      - the HIGH_POWER_LIMIT emergency cutoff keeps working even during
        the wait, instead of being blind for 10 minutes.
    """
    script_turned_off = False
    resume_time = 0        # timestamp (time.time()) at which we may auto turn back ON
    device = None

    last_is_on = None     # last known ON/OFF state
    last_zone = None      # last known power zone: "LOW" or "NORMAL"
    emergency_stop = False  # True once HIGH_POWER_LIMIT has tripped this device
    offline_logged = False  # True once we've logged the single "offline" message
                             # for the CURRENT outage - reset back to False the
                             # moment we successfully reconnect, so the next
                             # outage logs its own single message too.
    just_reconnected = False  # True for exactly one cycle right after we come
                               # back from a real outage - see below. While the
                               # device was unreachable we had NO visibility into
                               # its ON/OFF state (it may have lost power and come
                               # back defaulting to ON, or been flipped by hand
                               # while we couldn't see it), so last_is_on can be
                               # stale. This flag makes the very first reading
                               # after reconnecting log a fresh, explicit "here's
                               # the current state" line instead of silently
                               # assuming nothing happened during the gap.

    while True:
        try:
            if device is None:
                was_recovering = offline_logged  # True only if this reconnect follows a logged outage
                device = await connect(name, host)
                offline_logged = False  # back online - next outage logs again
                if was_recovering:
                    just_reconnected = True

            await device.update()

            energy = device.modules["Energy"]

            # IMPORTANT:
            # If this line throws an error, tell me the error.
            power = energy.current_consumption

            is_on = device.is_on

            # Keep the structured ON/OFF history (logs/usage_history.json)
            # in sync with whatever we just read from the device. This is
            # idempotent (a no-op if nothing actually changed), so it's
            # safe to call every single cycle as a catch-all - it covers
            # manual toggles from the Tapo app/dashboard and anything else
            # not explicitly handled below. The specific action points
            # further down (emergency cutoff, auto-off, auto-on) ALSO call
            # this directly so the recorded timestamp is exact rather than
            # waiting up to CHECK_INTERVAL seconds for this generic check
            # to catch up.
            if is_on:
                history.record_on(name)
            else:
                history.record_off(name)

            # We had NO visibility into this device's ON/OFF state for the
            # entire time it was unreachable - it might have lost power and
            # come back defaulting to ON, or someone flipped it by hand while
            # we couldn't see it. Rather than silently trust the old
            # last_is_on from before the outage (which would make the
            # dashboard think it's been continuously ON/OFF since way
            # earlier, hiding the gap entirely), log the freshly-confirmed
            # state as a clean new starting point.
            if just_reconnected:
                log_event(name, f"Back online - device is currently {'ON' if is_on else 'OFF'}.")
                last_is_on = is_on
                last_zone = None
                just_reconnected = False

            # --- SAFETY: instant emergency cutoff on dangerously high power ---
            # Checked first, before anything else, every single cycle - even
            # while we're in the middle of the auto-turn-on wait below.
            if is_on and power >= HIGH_POWER_LIMIT:
                log_event(name, f"EMERGENCY: power {power:.2f} W >= {HIGH_POWER_LIMIT} W limit. "
                                 f"Turning OFF immediately!")
                await device.turn_off()

                emergency_stop = True
                script_turned_off = False   # this is not the normal low-power cycle
                last_is_on = False
                last_zone = None
                history.record_off(name)

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

            # Log ON/OFF changes that we did NOT just cause ourselves.
            # Every time the script itself calls turn_off()/turn_on(), it
            # updates last_is_on immediately in the same breath, so this
            # block never fires for the script's own actions - only for
            # something external (you, flipping it in the Tapo app, or
            # a physical/other cause). That's why this is tagged "(manual)".
            if last_is_on is not None and is_on != last_is_on:
                log_event(name, f"Device is now {'ON' if is_on else 'OFF'} (manual).")

                # If we were mid-wait to auto turn it back on, and you
                # beat us to it by turning it ON yourself, cancel our plan.
                if script_turned_off and is_on:
                    log_event(name, "Device was manually turned ON during the wait period. "
                                     "Canceling scheduled auto turn-ON.")
                    script_turned_off = False

            last_is_on = is_on

            # --- AUTO-TURN-ON (non-blocking): we're waiting out OFF_DURATION ---
            if script_turned_off:
                if not is_on and time.time() >= resume_time:
                    log_event(name, "Turning ON...")
                    await device.turn_on()

                    script_turned_off = False
                    last_is_on = True
                    last_zone = None
                    history.record_on(name)

                    log_event(name, "Device turned back ON. Done.")

                # Whether we just turned it on or are still waiting, keep
                # polling at the normal short interval (so emergency cutoff
                # and manual-override detection keep working meanwhile).
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            # Plug already OFF (and not something we're auto-managing) -
            # nothing else to check this cycle
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

            # --- TRIGGER AUTO-OFF (non-blocking) ---
            if power < TRIGGER_POWER and not script_turned_off:

                log_event(name, f"Low power detected ({power:.2f} W). Turning OFF...")

                await device.turn_off()

                script_turned_off = True
                resume_time = time.time() + OFF_DURATION
                last_is_on = False
                last_zone = None
                history.record_off(name)

                log_event(name, f"Waiting {OFF_DURATION // 60} minutes before turning back ON...")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            # Errors for this device are isolated here - only this
            # device will pause/reconnect, other devices keep running.
            # NOTE: script_turned_off and resume_time are deliberately left
            # untouched here. If we errored out mid-wait, we still remember
            # to turn the device back on once reconnected and resume_time
            # has passed - this is the fix for the old "stuck OFF forever" bug.
            #
            # IMPORTANT: we do NOT log every failed reconnect attempt anymore.
            # A plug switched off at the wall fails to connect every single
            # cycle, which used to flood the log with an "ERROR: ..." +
            # "Reconnecting in 5 seconds..." pair every few seconds. Instead
            # we log ONE line the moment it first goes unreachable, then stay
            # silent and keep quietly retrying in the background. The
            # offline_logged flag (reset to False on the next successful
            # connect, above) is what lets the *next* real outage log again.
            if not offline_logged:
                log_event(name, "Not available right now - it is offline.")
                offline_logged = True
                # We've lost visibility into this device entirely - stop
                # crediting ON time until we get a fresh confirmation it's
                # actually back on (see just_reconnected / "Back online"
                # above). Idempotent - harmless if it was already off.
                history.record_off(name)

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
