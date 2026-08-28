"""
TAPO Dashboard — a simple, phone-friendly control panel for the
TAPO_AUTOMATION_SCRIPT monitor.

WHAT THIS FILE IS
------------------
This is a separate file. It does NOT import-and-run monitor.py's
automation loop, and it never writes to monitor.py, devices.txt, or
.env. It only:

  1. Reads devices.txt (through monitor.load_devices(), read-only)
     to know which devices exist.
  2. Reads logs/usage_history.json (through history.py, read-only) to
     show total ON time and the list of ON/OFF periods for any day -
     this is a small structured file that monitor.py writes to
     directly the moment a device's state changes, so the dashboard
     never has to parse the (potentially large) text log just to work
     out "how long was this on today".
  3. Reads logs/monitor.log (read-only, cached) only for the human-
     readable "Logs" section at the bottom of the page and to work
     out *why* a device is currently off (idle auto-off countdown,
     emergency stop, etc).
  4. Talks directly to your Tapo plugs on the network (same as the
     Tapo phone app would) to show live ON/OFF + current power, and
     to let you turn a plug on/off with a button.

WHERE TO RUN IT
----------------
Put this file in the SAME folder as your running monitor.py (the
same folder that has devices.txt, .env and the logs/ folder):

    streamlit run dashboard.py --server.address 0.0.0.0

Then open  http://<that machine's local IP>:8501  from your phone,
while your phone is on the same WiFi network.
"""

import asyncio
import os
import re
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from kasa import Discover

# monitor.py is only ever IMPORTED, never run as a script - this means
# main()/asyncio.run(main()) never executes. We only reuse its config
# constants and its devices.txt reader. Nothing here writes to it.
import monitor
import history

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

# Categories that mean "the device turned/became ON" vs "OFF" - used
# both to work out the current state and to build today's ON periods.
# "reconnect_on"/"reconnect_off" are the explicit state re-confirmation
# logged the moment a device comes back from an outage (see monitor.py) -
# they're treated exactly like a real ON/OFF change so a period never
# silently spans straight across a gap where the device was unreachable.
STATE_ON = {"auto_on_done", "manual_on", "emergency_resumed", "reconnect_on"}
STATE_OFF = {"auto_off_triggered", "emergency_off", "manual_off", "reconnect_off"}

# Categories bucketed as "a problem worth looking at" for the log filter.
PROBLEM_CATEGORIES = {"offline", "emergency_off", "emergency_confirm", "error"}


def classify(msg: str) -> dict:
    if msg.startswith("Connected ("):
        return dict(category="connected", icon="🔗", friendly="Connected to the plug")
    if msg.startswith("Back online - device is currently ON"):
        return dict(category="reconnect_on", icon="🔌",
                     friendly="Back online — confirmed ON")
    if msg.startswith("Back online - device is currently OFF"):
        return dict(category="reconnect_off", icon="🔌",
                     friendly="Back online — confirmed OFF")
    if msg.startswith("Not available right now"):
        return dict(category="offline", icon="🔌", friendly="Offline / unreachable")
    if msg.startswith("EMERGENCY:"):
        p = extract_power(msg)
        return dict(category="emergency_off", icon="🚨",
                     friendly=f"EMERGENCY shut-off — power hit {p} W")
    if msg.startswith("Device stopped for safety"):
        return dict(category="emergency_confirm", icon="🚨",
                     friendly="Stays OFF until turned back on manually")
    if msg.startswith("Device was manually turned back ON after an emergency stop"):
        return dict(category="emergency_resumed", icon="✅",
                     friendly="Manually turned back on after emergency stop")
    if re.match(r"^Device is now ON \(manual\)\.$", msg):
        return dict(category="manual_on", icon="🟢", friendly="Turned ON")
    if re.match(r"^Device is now OFF \(manual\)\.$", msg):
        return dict(category="manual_off", icon="🔴", friendly="Turned OFF")
    if msg.startswith("Device was manually turned ON during the wait period"):
        return dict(category="cancel_auto_on", icon="ℹ️",
                     friendly="Planned auto turn-on cancelled — turned on early")
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
                     friendly=f"Low power ({p} W) — turned OFF automatically")
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
    # Legacy lines from before the logging cleanup - old log files may
    # still contain these; keep them readable instead of erroring out.
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


def _log_fingerprint(path: Path):
    """
    Cheap way to tell whether the log file has actually changed since
    we last read it (modified time + size), without reading its
    contents. Used as the cache key below.
    """
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (0, 0)


@st.cache_data(show_spinner=False)
def _load_events(fingerprint, max_lines: int = 5000):
    """
    Reads and parses the log file, cached and keyed on the file's
    fingerprint. Streamlit reruns this whole script on every refresh
    and every auto-refresh tick, so WITHOUT this cache we'd re-read
    and re-parse the entire log file from scratch every single time -
    that's what was making every refresh feel slow. With the cache,
    the (possibly expensive) read+parse only happens again when the
    log file has actually changed.
    """
    lines = read_last_log_lines(monitor.LOG_FILE, max_lines=max_lines)
    return parse_log_lines(lines)


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
        if cat == "auto_off_triggered":
            result["state"] = "off_auto"
            result["since"] = ev["ts"]
            mins = wait_minutes if wait_minutes is not None else (monitor.OFF_DURATION // 60)
            result["wait_minutes"] = mins
            result["resume_at"] = ev["ts"] + timedelta(minutes=mins)
            break
        if cat == "emergency_off":
            result["state"] = "off_emergency"
            result["since"] = ev["ts"]
            break
        if cat == "manual_off":
            result["state"] = "off_manual"
            result["since"] = ev["ts"]
            break
        if cat == "reconnect_off":
            # Came back online after being unreachable and is OFF right
            # now. We don't know exactly when during the gap it turned
            # off, only that it's confirmed OFF as of this reconnect -
            # treat it like a manual OFF for display purposes.
            result["state"] = "off_manual"
            result["since"] = ev["ts"]
            break

    return result


def format_duration(td: timedelta) -> str:
    total_seconds = max(int(td.total_seconds()), 0)
    if total_seconds == 0:
        return "0m"
    if total_seconds < 60:
        return f"{total_seconds}s"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


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

st.set_page_config(page_title="Tapo Control", page_icon="🔌",
                    layout="centered", initial_sidebar_state="collapsed")

st.markdown(
    """
    <style>
    div.stButton > button { font-size: 1rem; padding: 0.5rem 0.5rem; }
    [data-testid="stMetricValue"] { font-size: 1.3rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def require_pin():
    if not DASHBOARD_PIN:
        return
    if st.session_state.get("dashboard_pin_ok"):
        return
    st.markdown("#### 🔌 Enter PIN")
    pin_input = st.text_input("PIN", type="password", key="pin_input", label_visibility="collapsed")
    if st.button("Unlock"):
        if pin_input == DASHBOARD_PIN:
            st.session_state["dashboard_pin_ok"] = True
            st.rerun()
        else:
            st.error("That PIN isn't right — try again.")
    st.stop()


require_pin()


def run_dashboard():
    # ---- Top bar: refresh controls only, no big heading (this is opened on a phone) ----
    top_col1, top_col2 = st.columns([1, 1])
    with top_col1:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()
    with top_col2:
        if HAS_AUTOREFRESH:
            auto = st.checkbox("Auto-refresh", value=True)
            if auto:
                st_autorefresh(interval=20_000, key="auto_refresh")
        else:
            st.checkbox("Auto-refresh", value=False, disabled=True,
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

    events = _load_events(_log_fingerprint(monitor.LOG_FILE))
    events_by_device = {name: [e for e in events if e["device"] == name] for name, _ in devices}

    history_data = history.load_all()

    # ---- Date picker - browse any past day's ON/OFF record, not just today ----
    avail_dates = history.available_dates(history_data)
    min_date = min(avail_dates) if avail_dates else date.today()
    selected_date = st.date_input(
        "📅 Viewing history for", value=date.today(),
        min_value=min_date, max_value=date.today(),
    )
    is_today = selected_date == date.today()

    with st.spinner("Checking your devices…"):
        live = run_async(get_all_live_status(devices))

    st.caption(f"Last checked: {datetime.now().strftime('%I:%M:%S %p')}")

    # ---- Build combined state + today's ON-time per device (live status wins) ----
    rows = []
    counts = {"on": 0, "off_auto": 0, "off_emergency": 0, "off_manual": 0,
              "off_other": 0, "unreachable": 0}

    for name, ip in devices:
        li = live.get(name, {"ok": False, "error": "no data"})
        dev_events = events_by_device.get(name, [])
        log_state = resolve_device_state(dev_events)
        periods, total_on = history.get_periods_for_date(name, selected_date, data=history_data)

        # "Since" times for the live status badge come straight from the
        # structured history file (not the log) - it's the authoritative,
        # already-accurate record of exactly when the current ON/OFF
        # period started, regardless of which date is being browsed above.
        on_since = history.current_open_period_start(name, data=history_data)
        off_since = history.last_off_time(name, data=history_data)

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
                      "combined": combined, "periods": periods, "total_on": total_on,
                      "on_since": on_since, "off_since": off_since})

    # ---- Summary strip ----
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Devices", len(devices))
    m2.metric("🟢 ON", counts["on"])
    m3.metric("🔴 OFF", counts["off_auto"] + counts["off_manual"] + counts["off_other"])
    m4.metric("🚨 Emerg.", counts["off_emergency"])
    if counts["unreachable"]:
        st.warning(f"⚪ {counts['unreachable']} device(s) can't be reached right now.")

    # ---- Per-device compact cards ----
    date_label = "today" if is_today else selected_date.strftime("%d %b")
    for row in rows:
        name, ip, li, log_state, combined = (
            row["name"], row["ip"], row["live"], row["log_state"], row["combined"]
        )
        total_on_str = format_duration(row["total_on"])
        on_since = row["on_since"]
        off_since = row["off_since"]

        with st.container(border=True):
            st.markdown(f"**{pretty(name)}**  ({total_on_str} {date_label})")

            # ---- one-line status (always LIVE / right now, regardless of
            # which date is selected above - the date picker only changes
            # the historical record shown in the expander below) ----
            if combined == "unreachable":
                st.error("⚪ Unreachable right now")
            elif combined == "on":
                power = li.get("power", 0) or 0
                since_txt = f" · on since {on_since.strftime('%I:%M %p')}" if on_since else ""
                st.success(f"🟢 ON — {power:.0f} W{since_txt}")
            elif combined == "off_emergency":
                since = off_since or log_state["since"]
                since_txt = since.strftime("%I:%M %p") if since else "recently"
                st.error(f"🚨 Safety stop at {since_txt} — turn back on by hand once it's safe")
            elif combined == "off_auto":
                resume_at = log_state.get("resume_at")
                if resume_at:
                    remaining = (resume_at - datetime.now()).total_seconds()
                    if remaining > 0:
                        mins, secs = divmod(int(remaining), 60)
                        st.warning(f"🔴 OFF (idle) — back ON in {mins}m {secs}s")
                    else:
                        st.warning("🔴 OFF (idle) — turning back ON any moment")
                else:
                    st.warning("🔴 OFF (idle) — will resume automatically")
            elif combined == "off_manual":
                since = off_since or log_state["since"]
                since_txt = f" since {since.strftime('%I:%M %p')}" if since else ""
                st.warning(f"🔴 OFF{since_txt}")
            else:
                st.warning("🔴 OFF")

            # ---- control button ----
            can_control = li.get("ok", False)
            is_on_now = bool(li.get("is_on"))
            if can_control:
                label = "🔴 Turn OFF" if is_on_now else "🟢 Turn ON"
                btn_type = "secondary" if is_on_now else "primary"
                if st.button(label, key=f"toggle_{name}", use_container_width=True, type=btn_type):
                    try:
                        with st.spinner(f"Switching {pretty(name)}…"):
                            run_async(set_device_power(ip, not is_on_now))
                        st.toast(f"{pretty(name)} turned {'OFF' if is_on_now else 'ON'}",
                                 icon="🔴" if is_on_now else "🟢")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Couldn't switch it: {e}")
                    st.rerun()
            else:
                st.button("Turn ON/OFF", key=f"disabled_{name}",
                           use_container_width=True, disabled=True,
                           help="Can't control it while it's unreachable")

            # ---- full ON/OFF record for the selected date, collapsed to save space ----
            with st.expander(f"📅 Record for {date_label} — {total_on_str} ON total"):
                if not row["periods"]:
                    st.caption(f"No ON time recorded for {date_label}.")
                else:
                    for s, e in reversed(row["periods"]):
                        same_day_end = "" if e.date() == s.date() else " (next day)"
                        st.markdown(
                            f"🟢 {s.strftime('%I:%M %p')} – {e.strftime('%I:%M %p')}{same_day_end}"
                            f"  &nbsp; *({format_duration(e - s)})*"
                        )

    st.divider()

    # ---- One combined log section for both devices (simple dropdowns, not multiselect) ----
    st.markdown("**📜 Logs**")

    all_names = [n for n, _ in devices]
    f1, f2 = st.columns(2)
    with f1:
        device_choice = st.selectbox("Device", options=["All devices"] + [pretty(n) for n in all_names])
    with f2:
        type_choice = st.selectbox("Show", options=["Everything", "On / Off changes", "Problems only"])
    search = st.text_input("Search (optional)", placeholder="a date or word…")

    filtered = events
    if device_choice != "All devices":
        target = next(n for n in all_names if pretty(n) == device_choice)
        filtered = [e for e in filtered if e["device"] == target]

    if type_choice == "On / Off changes":
        filtered = [e for e in filtered if e["category"] in (STATE_ON | STATE_OFF)]
    elif type_choice == "Problems only":
        filtered = [e for e in filtered if e["category"] in PROBLEM_CATEGORIES]

    if search:
        s = search.lower()
        filtered = [e for e in filtered if s in e["msg"].lower() or s in e["device"].lower()]

    filtered = sorted(filtered, key=lambda e: e["ts"], reverse=True)

    st.caption(f"Showing {len(filtered)} of {len(events)} recorded events.")

    if filtered:
        show = filtered[:500]
        df = pd.DataFrame([{
            "Time": e["ts"].strftime("%d %b, %I:%M:%S %p"),
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


try:
    run_dashboard()
except Exception as e:  # noqa: BLE001
    st.error("Something went wrong loading the dashboard.")
    with st.expander("Technical details"):
        st.exception(e)
