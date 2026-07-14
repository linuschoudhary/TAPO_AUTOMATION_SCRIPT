# TAPO_AUTOMATION

Automated power monitoring and safety control for TP-Link Tapo smart plugs
(energy-monitoring models, e.g. P110/P115).

The script watches each plug's live power draw and:

- **Low-power auto-off**: if power drops below a threshold (e.g. the
  connected appliance finished its job / went idle), turns the plug off,
  waits, then turns it back on automatically.
- **Emergency high-power cutoff**: if power spikes to a dangerous level,
  turns the plug off **instantly** and keeps it off until you manually
  turn it back on.
- Runs **multiple devices in parallel**, each fully independent - a
  problem with one device (error, disconnect, emergency stop) never
  affects any other device.
- Logs important events (not routine power readings) to a permanent log
  file, in addition to the terminal.

---

## Folder contents

| File / folder      | Purpose                                                              |
|---------------------|-----------------------------------------------------------------------|
| `monitor.py`        | Main script. Run this.                                               |
| `devices.txt`        | List of devices to monitor - add new devices here, no code changes.  |
| `.env`               | Your Tapo account credentials (not committed to git).                |
| `requirements.txt`   | Python dependencies to install.                                      |
| `logs/monitor.log`   | Permanent log of important events (created automatically).           |
| `venv/`              | Python virtual environment (not committed to git).                   |

---

## Setup (Ubuntu)

```bash
cd TAPO_AUTOMATION

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 1. Add your Tapo account credentials

Create/edit the `.env` file in this folder:

```
EMAIL=your_tapo_account_email@example.com
PASSWORD=your_tapo_account_password
```

This is the same email/password you use to log into the Tapo app. It's
used for every device in `devices.txt` (all devices must be on the same
Tapo account). `.env` is excluded from git via `.gitignore`, so your
credentials never get committed.

### 2. Add your devices

Edit `devices.txt`. One device per line, format `Name,IP_Address`:

```
TAPO_OLDER_SWITCH,192.168.31.100
TAPO_NEW_SWITCH,192.168.31.101
```

Lines starting with `#` are treated as comments and ignored.

**To add another device later:** just add a new line with its name and
IP address, save the file, and restart the script. No code changes
needed - it's picked up automatically.

> Tip: find a plug's IP address in the Tapo app under the device's
> settings, or check your router's connected-devices list.

### 3. Run it

```bash
python monitor.py
```

Leave it running (e.g. in a `tmux`/`screen` session, or as a systemd
service) for continuous monitoring.

---

## How the terminal output works

Every device runs as its own independent task. Output for all devices
streams into the same terminal, each line tagged with the device name
so you can tell them apart:

```
[14:02:11] [TAPO_OLDER_SWITCH] Connected (192.168.31.100).
[14:02:11] [TAPO_NEW_SWITCH] Connected (192.168.31.101).
[14:05:40] [TAPO_OLDER_SWITCH] Power dropped below 300W (now 210.40 W).
[14:05:40] [TAPO_OLDER_SWITCH] Low power detected (210.40 W). Turning OFF...
[14:05:41] [TAPO_NEW_SWITCH] Power back to normal (620.10 W).
```

Routine power readings are **not** printed continuously - only
meaningful changes (connects, on/off, threshold crossings, errors,
emergency stops) are shown and logged, so the output stays readable.

---

## Permanent log file

Every event printed to the terminal is also appended to
`logs/monitor.log`, so you have a permanent history even after closing
the terminal or restarting the script. The file is never overwritten -
new runs append to it.

---

## Configuration (top of `monitor.py`)

| Setting             | Default   | Meaning                                                                 |
|----------------------|-----------|--------------------------------------------------------------------------|
| `MIN_POWER`          | 5 W       | Readings below this are treated as idle/no appliance - ignored.         |
| `TRIGGER_POWER`      | 300 W     | If power drops below this (and above `MIN_POWER`), the low-power auto-off routine triggers. |
| `HIGH_POWER_LIMIT`   | 3000 W    | Safety cutoff - if power reaches/exceeds this, the plug turns off instantly and stays off until manually turned back on. |
| `CHECK_INTERVAL`     | 5 sec     | How often each device's power is checked.                              |
| `OFF_DURATION`       | 10 min    | How long the plug stays off after a low-power auto-off, before turning back on automatically. |

### About `HIGH_POWER_LIMIT`

This is a **software** safety net, not a substitute for real hardware
protection (fuse/MCB) - it can only react as fast as `CHECK_INTERVAL`
plus network latency, and a genuine short circuit can happen faster
than that.

- Check your specific Tapo plug's datasheet for its actual rated max
  load (commonly ~2300W for 10A-rated plugs) and set `HIGH_POWER_LIMIT`
  comfortably below that - not at or above it.
- Also enable Tapo's own built-in overload-protection feature in the
  Tapo app (Energy Monitoring settings) as your first line of defense -
  it reacts faster since it doesn't depend on WiFi polling from this
  script.
- After an emergency stop, the plug is **not** turned back on
  automatically. Check the appliance/wiring, then turn it back on
  yourself via the Tapo app - the script will detect this and resume
  normal monitoring.

---

## Fault isolation between devices

Each device runs in its own `asyncio` task with its own connection,
state, and error handling. If one device errors out, loses connection,
or trips the emergency cutoff, only that device is affected - it
reconnects/retries (or stays off) on its own while every other device
keeps running normally.

---

## Troubleshooting

- **`devices.txt not found`**: make sure you're running `python
  monitor.py` from inside the `TAPO_AUTOMATION` folder.
- **A device keeps erroring/reconnecting**: check that its IP address in
  `devices.txt` is correct and hasn't changed (consider setting a static
  IP / DHCP reservation for your plugs on your router).
- **`ERROR: ...` in the log for one device only**: that's expected
  behavior - it won't affect other devices. Check the specific error
  message for details (Wi-Fi/network issue, wrong credentials, plug
  offline, etc.).
