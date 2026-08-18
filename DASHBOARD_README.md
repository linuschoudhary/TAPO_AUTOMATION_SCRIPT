# Power Monitor Dashboard (`dashboard.py`)

A simple screen — built for a phone browser — that shows what your
`monitor.py` automation is doing, in plain English, and lets you turn
plugs on/off with a button. Built for someone with **zero technical
background** to understand at a glance.

**This does not change how `monitor.py` runs.** It is a brand new,
separate file. It only *reads* `devices.txt` and `logs/monitor.log`,
and talks directly to your Tapo plugs the same way the Tapo phone app
does. `monitor.py`, `devices.txt`, `.env`, and `requirements.txt` were
not touched or modified in any way to build this.

---

## What you'll see on the dashboard

For **every device** in `devices.txt`, automatically (no setup needed
per device):

- 🟢 **ON**, with how many Watts it's using right now
- 🔴 **OFF automatically** (power was low / appliance idle) — with a
  live countdown: *"Turns back ON automatically in 6m 42s"*, plus a
  progress bar
- 🚨 **Emergency stopped** (power spiked too high / safety cutoff) —
  clearly flagged in red, with a note that it needs a manual restart
- 🔴 **OFF manually** (someone turned it off via the Tapo app or this
  dashboard)
- ⚪ **Can't be reached** (WiFi/power issue) — instead of the app just
  hanging or crashing
- A **Turn ON / Turn OFF** button for that device
- A **"Recent activity"** panel with the last events for that device,
  in plain English (not raw log lines)

At the top: a quick summary (how many devices are ON, OFF, or in an
emergency state right now).

Further down:

- **"How the automation is set up"** — the actual thresholds
  (`TRIGGER_POWER`, `HIGH_POWER_LIMIT`, `OFF_DURATION`, etc.) pulled
  live from `monitor.py`, explained in plain language, so this page
  never goes out of date if those numbers are changed later.
- **"Full History"** — every recorded event, for every device, with
  filters by device / event type / free-text search, plus a button to
  download the raw `monitor.log` file.

---

## One-time setup

Run these on the **same machine that runs `monitor.py`** (so the
dashboard can see the same `devices.txt`, `.env`, and `logs/` folder).
This file only needs to exist alongside `monitor.py` — it doesn't
need `monitor.py` to be running at that exact moment to open, but of
course automation only happens for real while `monitor.py` itself is
running as usual.

```bash
cd TAPO_AUTOMATION_SCRIPT
source venv/bin/activate        # the same venv you already made
pip install -r dashboard_requirements.txt
```

That's it — no other setup required. `devices.txt` and `.env` are
reused automatically.

---

## Running it

```bash
streamlit run dashboard.py --server.address 0.0.0.0
```

You'll see a line like:

```
Network URL: http://192.168.31.xxx:8501
```

That's the address to open **from your phone's browser**, as long as
your phone is on the **same WiFi network**. Bookmark it / add it to
your home screen for one-tap access.

Leave this running the same way you leave `monitor.py` running (e.g.
its own `tmux`/`screen` window, or a second systemd service) if you
want the dashboard available any time, not just while you happen to
have a terminal open.

> **Tip:** if you're not sure of the machine's local IP address, run
> `hostname -I` (Linux) on that machine.

---

## Optional: require a PIN to open it

By default, anyone on your WiFi who has the link can open the
dashboard and control your plugs — fine for most home/small-office
setups, but you may want a PIN if more people share that network.

1. Copy `.env.dashboard.example` to a new file called `.env.dashboard`
   (same folder as `dashboard.py`).
2. Open it and set your own PIN, e.g. `DASHBOARD_PIN=4821`.
3. Restart the dashboard (`streamlit run dashboard.py ...` again).

This file is completely separate from the `.env` file `monitor.py`
uses — it never touches your Tapo account credentials. If you use
git, consider adding `.env.dashboard` to your own `.gitignore` too
(it wasn't added automatically, to avoid touching that file).

If you skip this step, the dashboard just opens normally with no PIN.

---

## Good to know

- **It never edits `monitor.py`, `devices.txt`, or `.env`.** It only
  reads them.
- **Turning a device on/off from the dashboard is exactly the same**,
  as far as `monitor.py` is concerned, as turning it on/off from the
  Tapo app. `monitor.py` already detects and logs that as a normal
  "manual" action and carries on monitoring normally — nothing
  breaks.
- **Adding a new device** still only requires editing `devices.txt`
  as before (see the main `README.md`) and restarting `monitor.py`.
  The dashboard will pick up the new device automatically the next
  time you open/refresh it — no dashboard changes needed.
- If a plug is unreachable, the dashboard shows that clearly instead
  of crashing or hanging — refresh once it's back on the network.
- The dashboard reads the last 5,000 lines of `logs/monitor.log` for
  the history view (plenty for months of activity, since only real
  events are logged, not routine readings). The full file is always
  available via the "Download full raw log file" button either way.

---

## Troubleshooting

- **Page says "Can't find devices.txt"** — make sure `dashboard.py` is
  sitting in the exact same folder as `monitor.py`.
- **Every device shows "Can't be reached"** — check `.env` has the
  correct Tapo account email/password, and that this machine is on
  the same network as the plugs (same checks as in the main
  `README.md`'s troubleshooting section).
- **Countdown timer looks a little off** — it's calculated from the
  timestamps already in `logs/monitor.log`, so it's as accurate as
  that log. It updates every time you refresh or auto-refresh fires.
- **Can't open it from my phone** — confirm the phone is on the same
  WiFi, and that you started the app with `--server.address 0.0.0.0`
  (not the default, which only listens on the computer itself).
