"""
TAPO Dashboard — a simple, phone-friendly control panel for the
TAPO_AUTOMATION_SCRIPT monitor.

WHAT THIS FILE IS
------------------
This is a NEW, separate file. It does NOT import-and-run monitor.py's
automation loop, and it never writes to monitor.py, devices.txt, or
.env. It only:

  1. Reads devices.txt (through monitor.load_devices(), read-only)
     to know which devices exist.
  2. Reads logs/monitor.log (read-only) to build a plain-English
     history and figure out "why is this device off right now".
  3. Talks directly to your Tapo plugs on the network (same as the
     Tapo phone app would) to show live ON/OFF + current power, and
     to let you turn a plug on/off with a button. monitor.py already
     treats an external on/off (from the Tapo app, or from here) as
     a normal "manual" action and handles it safely - it does not
     get confused or break anything.

WHERE TO RUN IT
----------------
Put this file in the SAME folder as your running monitor.py (the
same folder that has devices.txt, .env and the logs/ folder), on
whichever machine actually runs monitor.py continuously. See
DASHBOARD_README.md for full setup steps.

    streamlit run dashboard.py --server.address 0.0.0.0

Then open  http://<that machine's local IP>:8501  from your phone,
while your phone is on the same WiFi network.
"""

import asyncio
import os
import re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from kasa import Discover

# monitor.py is only ever IMPORTED, never run as a script - this means
# main()/asyncio.run(main()) never executes. We only reuse its config
# constants and its devices.txt reader. Nothing here writes to it.
import monitor

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# ==========================================================
# Optional PIN protection (completely separate from monitor's .env)
# ==========================================================
load_dotenv(Path(__file__).parent / ".env.dashboard")
DASHBOARD_PIN = os.environ.get("DASHBOARD_PIN", "").strip()

LIVE_STATUS_TIMEOUT = 6  # seconds to wait for a plug to respond


# ==========================================================
# Small helpers
# ==========================================================

def pretty(name: str) -> str:
    """Cosmetic-only: 'TAPO_OLDER_SWITCH' -> 'Tapo Older Switch'."""
    return name.replace("_", " ").replace("-", " ").title()


NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
WAIT_MIN_RE = re.compile(r"Waiting (\d+) minutes")


def extract_power(msg: str) -> str:
    m = NUM_RE.search(msg)
    return m.group(1) if m else "?"


def extract_wait_minutes(msg: str) -> int:
    m = WAIT_MIN_RE.search(msg)
    if m:
        return int(m.group(1))
    return monitor.OFF_DURATION // 60


LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s\[(?P<device>[^\]]+)\]\s(?P<msg>.*)$"
)

CATEGORY_LABELS = {
    "connected": "🔗 Connected",
    "emergency_off": "🚨 Emergency shut-off",
    "emergency_confirm": "🚨 Emergency (stays off)",
    "emergency_resumed": "✅ Resumed after emergency",
    "manual_on": "🟢 Turned ON manually",
    "manual_off": "🔴 Turned OFF manually",
    "cancel_auto_on": "ℹ️ Auto turn-on cancelled",
    "power_low_zone": "⚠️ Power dropped low",
    "power_normal_zone": "✅ Power back to normal",
    "auto_off_triggered": "🔻 Auto OFF (low power)",
    "auto_off_wait": "⏳ Waiting to auto turn-on",
    "auto_on_start": "🔼 Auto turning ON",
    "auto_on_done": "🟢 Auto turned ON",
    "error": "❗ Problem / error",
    "reconnecting": "🔄 Reconnecting",
    "system": "🖥️ System message",
    "other": "• Other",
}

# Categories that establish a device's current on/off state (most
# recent one of these, scanning backwards, wins).
STATE_ON = {"auto_on_done", "manual_on", "emergency_resumed"}
STATE_OFF_AUTO = {"auto_off_triggered"}
STATE_OFF_EMERGENCY = {"emergency_off"}
STATE_OFF_MANUAL = {"manual_off"}


def classify(msg: str) -> dict:
    if msg.startswith("Connected ("):
        return dict(category="connected", icon="🔗", friendly="Connected to the plug")
    if msg.startswith("EMERGENCY:"):
        p = extract_power(msg)
        return dict(category="emergency_off", icon="🚨",
                     friendly=f"EMERGENCY shut-off — power hit {p} W")
    if msg.startswith("Device stopped for safety"):
        return dict(category="emergency_confirm", icon="🚨",
                     friendly="Stays OFF until turned back on manually")
    if msg.startswith("Device was manually turned back ON after an emergency stop"):
        return dict(category="emergency_resumed", icon="✅",
                     friendly="Manually turned back on after emergency stop — monitoring resumed")
    if re.match(r"^Device is now ON \(manual\)\.$", msg):
        return dict(category="manual_on", icon="🟢",
                     friendly="Turned ON manually (Tapo app / dashboard)")
    if re.match(r"^Device is now OFF \(manual\)\.$", msg):
        return dict(category="manual_off", icon="🔴",
                     friendly="Turned OFF manually (Tapo app / dashboard)")
    if msg.startswith("Device was manually turned ON during the wait period"):
        return dict(category="cancel_auto_on", icon="ℹ️",
                     friendly="Planned auto turn-on cancelled — someone turned it on early")
    if msg.startswith("Power dropped below"):
        p = extract_power(msg)
        return dict(category="power_low_zone", icon="⚠️",
                     friendly=f"Power dropped low ({p} W)")
    if msg.startswith("Power back to normal"):
        p = extract_power(msg)
        return dict(category="power_normal_zone", icon="✅",
                     friendly=f"Power back to normal ({p} W)")
    if msg.startswith("Low power detected"):
        p = extract_power(msg)
        return dict(category="auto_off_triggered", icon="🔻",
                     friendly=f"Low power ({p} W) — turning OFF automatically")
    if msg.startswith("Waiting"):
        mins = extract_wait_minutes(msg)
        return dict(category="auto_off_wait", icon="⏳",
                     friendly=f"Will turn back ON automatically after {mins} min")
    if msg.startswith("Turning ON..."):
        return dict(category="auto_on_start", icon="🔼",
                     friendly="Turning back ON automatically")
    if msg.startswith("Device turned back ON. Done."):
        return dict(category="auto_on_done", icon="🟢",
                     friendly="Turned back ON automatically")
    if msg.startswith("ERROR:"):
        return dict(category="error", icon="❗", friendly=f"Problem: {msg[len('ERROR:'):].strip()}")
    if msg.startswith("Reconnecting"):
        return dict(category="reconnecting", icon="🔄", friendly="Reconnecting…")
    if msg.startswith(("Starting monitor", "Logging important events",
                        "No devices found", "Skipping invalid line")):
        return dict(category="system", icon="🖥️", friendly=msg)
    return dict(category="other", icon="•", friendly=msg)


def read_last_log_lines(path: Path, max_lines: int = 5000):
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return list(deque(f, maxlen=max_lines))


def parse_log_lines(lines):
    events = []
    for raw in lines:
        raw = raw.rstrip("\n")
        m = LOG_LINE_RE.match(raw)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        device = m.group("device")
        msg = m.group("msg")
        ev = {"ts": ts, "device": device, "msg": msg, "raw": raw}
        ev.update(classify(msg))
        events.append(ev)
    events.sort(key=lambda e: e["ts"])
    return events


def resolve_device_state(events_for_device):
    """
    Scan a single device's events backwards (most recent first) and
    figure out the current state PURELY from the log history:
      "on", "off_auto", "off_emergency", "off_manual", or "unknown".
    Also works out the auto-resume time if applicable.
    This is later cross-checked against the LIVE reading from the
    plug itself, which always wins if the two disagree.
    """
    result = {"state": "unknown", "since": None, "resume_at": None, "wait_minutes": None}
    wait_minutes = None

    for ev in reversed(events_for_device):
        cat = ev["category"]

        if cat == "auto_off_wait" and wait_minutes is None:
            wait_minutes = extract_wait_minutes(ev["msg"])
            continue

        if cat in STATE_ON:
            result["state"] = "on"
            result["since"] = ev["ts"]
            break
        if cat in STATE_OFF_AUTO:
            result["state"] = "off_auto"
            result["since"] = ev["ts"]
            mins = wait_minutes if wait_minutes is not None else (monitor.OFF_DURATION // 60)
            result["wait_minutes"] = mins
            result["resume_at"] = ev["ts"] + timedelta(minutes=mins)
            break
        if cat in STATE_OFF_EMERGENCY:
            result["state"] = "off_emergency"
            result["since"] = ev["ts"]
            break
        if cat in STATE_OFF_MANUAL:
            result["state"] = "off_manual"
            result["since"] = ev["ts"]
            break

    return result


# ==========================================================
# Live device communication (direct to the plug - same as the Tapo app)
# ==========================================================

async def _get_live_status_one(name, ip):
    try:
        device = await asyncio.wait_for(
            Discover.discover_single(ip, username=monitor.EMAIL, password=monitor.PASSWORD),
            timeout=LIVE_STATUS_TIMEOUT,
        )
        await device.update()
        energy = device.modules["Energy"]
        power = energy.current_consumption
        return {"ok": True, "is_on": device.is_on, "power": power}
    except Exception as e:  # noqa: BLE001 - we want to surface any error, per-device
        return {"ok": False, "error": str(e)}


async def get_all_live_status(devices):
    results = await asyncio.gather(*[_get_live_status_one(n, ip) for n, ip in devices])
    return {devices[i][0]: results[i] for i in range(len(devices))}


async def set_device_power(ip, turn_on: bool):
    device = await asyncio.wait_for(
        Discover.discover_single(ip, username=monitor.EMAIL, password=monitor.PASSWORD),
        timeout=LIVE_STATUS_TIMEOUT,
    )
    await device.update()
    if turn_on:
        await device.turn_on()
    else:
        await device.turn_off()


def run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ==========================================================
# Page setup
# ==========================================================

st.set_page_config(page_title="Power Monitor", page_icon="🔌",
                    layout="centered", initial_sidebar_state="collapsed")

st.markdown(
    """
    <style>
    div.stButton > button { font-size: 1.05rem; padding: 0.6rem 0.5rem; }
    [data-testid="stMetricValue"] { font-size: 1.6rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def require_pin():
    if not DASHBOARD_PIN:
        return
    if st.session_state.get("dashboard_pin_ok"):
        return
    st.title("🔌 Power Monitor")
    st.write("Enter the PIN to continue.")
    pin_input = st.text_input("PIN", type="password", key="pin_input")
    if st.button("Unlock"):
        if pin_input == DASHBOARD_PIN:
            st.session_state["dashboard_pin_ok"] = True
            st.rerun()
        else:
            st.error("That PIN isn't right — try again.")
    st.stop()


require_pin()


def run_dashboard():
    st.title("🔌 Power Monitor")
    st.caption("Live status and history for your smart plugs")

    top_col1, top_col2 = st.columns([1, 1])
    with top_col1:
        if st.button("🔄 Refresh now", use_container_width=True):
            st.rerun()
    with top_col2:
        if HAS_AUTOREFRESH:
            auto = st.checkbox("Auto-refresh (20s)", value=True)
            if auto:
                st_autorefresh(interval=20_000, key="auto_refresh")
        else:
            st.checkbox("Auto-refresh (20s)", value=False, disabled=True,
                         help="Run: pip install streamlit-autorefresh to enable this")

    try:
        devices = monitor.load_devices()
    except FileNotFoundError:
        st.error("Can't find devices.txt. Make sure this dashboard is in the same "
                  "folder as monitor.py.")
        st.stop()
        return

    if not devices:
        st.warning("No devices are listed in devices.txt yet. Add one line per "
                    "device (Name,IP_Address) and refresh this page.")
        st.stop()
        return

    log_lines = read_last_log_lines(monitor.LOG_FILE, max_lines=5000)
    events = parse_log_lines(log_lines)
    events_by_device = {name: [e for e in events if e["device"] == name] for name, _ in devices}

    with st.spinner("Checking your devices…"):
        live = run_async(get_all_live_status(devices))

    st.caption(f"Last checked: {datetime.now().strftime('%d %b, %I:%M:%S %p')}")

    # ---- Build combined state per device (live status wins over log guess) ----
    rows = []
    counts = {"on": 0, "off_auto": 0, "off_emergency": 0, "off_manual": 0,
              "off_other": 0, "unreachable": 0}

    for name, ip in devices:
        li = live.get(name, {"ok": False, "error": "no data"})
        log_state = resolve_device_state(events_by_device.get(name, []))

        if not li.get("ok"):
            combined = "unreachable"
            counts["unreachable"] += 1
        elif li["is_on"]:
            combined = "on"
            counts["on"] += 1
        else:
            guess = log_state["state"]
            if guess == "off_emergency":
                combined = "off_emergency"
                counts["off_emergency"] += 1
            elif guess == "off_auto":
                combined = "off_auto"
                counts["off_auto"] += 1
            elif guess == "off_manual":
                combined = "off_manual"
                counts["off_manual"] += 1
            else:
                combined = "off_other"
                counts["off_other"] += 1

        rows.append({"name": name, "ip": ip, "live": li, "log_state": log_state,
                      "combined": combined})

    # ---- Summary strip ----
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Devices", len(devices))
    m2.metric("🟢 ON", counts["on"])
    m3.metric("🔴 OFF", counts["off_auto"] + counts["off_manual"] + counts["off_other"])
    m4.metric("🚨 Emergency", counts["off_emergency"])
    if counts["unreachable"]:
        st.warning(f"⚪ {counts['unreachable']} device(s) can't be reached right now "
                    "(check their WiFi / power).")

    st.divider()

    # ---- Per-device cards ----
    for row in rows:
        name, ip, li, log_state, combined = (
            row["name"], row["ip"], row["live"], row["log_state"], row["combined"]
        )
        dev_events = events_by_device.get(name, [])

        with st.container(border=True):
            st.subheader(pretty(name))
            st.caption(f"`{name}` · {ip}")

            if combined == "unreachable":
                st.error("⚪ Can't reach this plug right now")
                err = li.get("error", "")[:160]
                st.caption(f"({err})" if err else "")
                st.caption("Check its WiFi / power. monitor.py keeps retrying automatically "
                            "in the background.")
            elif combined == "on":
                power = li.get("power", 0) or 0
                st.success(f"🟢 ON — using {power:.0f} W right now")
            elif combined == "off_emergency":
                st.error("🚨 Stopped for SAFETY — power spiked too high")
                since = log_state["since"]
                since_txt = since.strftime("%d %b, %I:%M %p") if since else "recently"
                st.caption(f"Tripped: {since_txt}. This will stay OFF until turned back on "
                            "by hand — check the appliance/wiring is safe first, then use "
                            "the button below (or the Tapo app).")
            elif combined == "off_auto":
                st.warning("🔴 OFF automatically — power was low (device idle/finished)")
                resume_at = log_state.get("resume_at")
                if resume_at:
                    remaining = (resume_at - datetime.now()).total_seconds()
                    total = (log_state.get("wait_minutes") or (monitor.OFF_DURATION // 60)) * 60
                    if remaining > 0:
                        mins, secs = divmod(int(remaining), 60)
                        st.info(f"⏳ Turns back ON automatically in **{mins}m {secs}s** "
                                f"(around {resume_at.strftime('%I:%M %p')})")
                        frac = 0.0
                        if total > 0:
                            frac = min(max((total - remaining) / total, 0.0), 1.0)
                        st.progress(frac)
                    else:
                        st.info("⏳ About to turn back ON automatically (any moment now)")
                else:
                    st.caption("It should turn back on automatically before long.")
            elif combined == "off_manual":
                since = log_state["since"]
                since_txt = f" at {since.strftime('%d %b, %I:%M %p')}" if since else ""
                st.warning(f"🔴 OFF — turned off manually{since_txt}")
            else:
                st.warning("🔴 OFF")

            # ---- Control button ----
            can_control = li.get("ok", False)
            is_on_now = bool(li.get("is_on"))
            if can_control:
                if is_on_now:
                    if st.button(f"🔴 Turn {pretty(name)} OFF", key=f"off_{name}",
                                  use_container_width=True):
                        try:
                            with st.spinner(f"Turning {pretty(name)} off…"):
                                run_async(set_device_power(ip, False))
                            st.toast(f"{pretty(name)} turned OFF", icon="🔴")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Couldn't turn it off: {e}")
                        st.rerun()
                else:
                    if st.button(f"🟢 Turn {pretty(name)} ON", key=f"on_{name}",
                                  use_container_width=True, type="primary"):
                        try:
                            with st.spinner(f"Turning {pretty(name)} on…"):
                                run_async(set_device_power(ip, True))
                            st.toast(f"{pretty(name)} turned ON", icon="🟢")
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Couldn't turn it on: {e}")
                        st.rerun()
            else:
                st.button(f"Turn {pretty(name)} ON/OFF", key=f"disabled_{name}",
                           use_container_width=True, disabled=True,
                           help="Can't control it while it's unreachable")

            with st.expander(f"📜 Recent activity — {pretty(name)}"):
                if not dev_events:
                    st.caption("No history recorded yet for this device.")
                else:
                    for ev in reversed(dev_events[-15:]):
                        st.markdown(
                            f"{ev['icon']} **{ev['ts'].strftime('%d %b, %I:%M:%S %p')}** — "
                            f"{ev['friendly']}"
                        )

    st.divider()

    # ---- Plain-English settings summary ----
    with st.expander("⚙️ How the automation is set up"):
        st.markdown(
            f"""
- **Auto-off (low power):** if a plug's power draw drops below
  **{monitor.TRIGGER_POWER} W**, it's treated as *"device finished / idle"*
  and that plug turns **OFF** automatically.
- **Auto turn-back-on:** after an auto-off, the plug waits
  **{monitor.OFF_DURATION // 60} minutes**, then turns itself back **ON**.
- **Emergency safety cutoff:** if power reaches **{monitor.HIGH_POWER_LIMIT} W**,
  the plug turns OFF *instantly* and **stays off** — it will **not** come back
  on by itself. Someone needs to check it's safe, then turn it back on by
  hand (button above, or the Tapo app).
- **Check frequency:** each plug's power is checked every
  **{monitor.CHECK_INTERVAL} seconds**.
- **Ignored readings:** anything under **{monitor.MIN_POWER} W** is treated as
  "nothing plugged in / idle" and ignored.

These numbers come directly from the running automation script - if they're
ever changed there, this page will show the new values automatically.
            """
        )

    # ---- Full history ----
    st.subheader("📚 Full History")

    all_names = [n for n, _ in devices]
    f_col1, f_col2 = st.columns(2)
    with f_col1:
        sel_devices = st.multiselect("Devices", options=all_names, default=all_names)
    with f_col2:
        present_cats = sorted(set(e["category"] for e in events))
        sel_cats = st.multiselect(
            "Event type", options=present_cats, default=present_cats,
            format_func=lambda c: CATEGORY_LABELS.get(c, c),
        )
    search = st.text_input("Search (optional)", placeholder="e.g. a date, a word from the message…")

    filtered = [e for e in events if e["device"] in sel_devices and e["category"] in sel_cats]
    if search:
        s = search.lower()
        filtered = [e for e in filtered if s in e["msg"].lower() or s in e["device"].lower()]
    filtered = sorted(filtered, key=lambda e: e["ts"], reverse=True)

    st.caption(f"Showing {len(filtered)} of {len(events)} recorded events "
                f"(most recent {len(log_lines)} log lines scanned).")

    if filtered:
        show = filtered[:500]
        df = pd.DataFrame([{
            "Time": e["ts"].strftime("%d %b %Y, %I:%M:%S %p"),
            "Device": pretty(e["device"]),
            "Event": f"{e['icon']} {e['friendly']}",
        } for e in show])
        st.dataframe(df, use_container_width=True, hide_index=True)
        if len(filtered) > 500:
            st.caption(f"Only showing the most recent 500 of {len(filtered)} matching events. "
                        "Narrow the filters above to see something more specific, or download "
                        "the full log below.")
    else:
        st.caption("No matching events.")

    log_bytes = monitor.LOG_FILE.read_bytes() if monitor.LOG_FILE.exists() else b""
    st.download_button("⬇️ Download full raw log file", data=log_bytes,
                        file_name="monitor.log", mime="text/plain",
                        disabled=not log_bytes, use_container_width=True)

    st.divider()
    st.caption("This dashboard only talks to your own plugs on your own network. "
                "Nothing is sent anywhere else. It never edits monitor.py, devices.txt, "
                "or your .env file.")


try:
    run_dashboard()
except Exception as e:  # noqa: BLE001
    st.error("Something went wrong loading the dashboard.")
    with st.expander("Technical details"):
        st.exception(e)
