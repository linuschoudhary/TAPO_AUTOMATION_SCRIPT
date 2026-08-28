"""
Structured ON/OFF usage history for each Tapo device.

WHY THIS FILE EXISTS
---------------------
Working out "how long was this device on today" (or on any past day)
used to mean reading and regex-parsing the ENTIRE monitor.log text
file every time the dashboard loaded - that only gets slower as the
log grows, and only really answered "today" well.

Now monitor.py writes each ON/OFF period DIRECTLY to a small,
structured JSON file (logs/usage_history.json) the moment it happens
- no text parsing involved. The dashboard just reads this file, which
stays tiny (a handful of short entries per device per day) no matter
how big monitor.log gets, and can look up ANY past day, not just
today - which is what makes the dashboard's date picker possible.

monitor.log remains the place for human-readable "what happened and
why" messages (manual vs automatic vs emergency, reconnects, etc).
This file is purely "when was it on" - the two are separate concerns
on purpose.

FILE FORMAT
------------
{
  "TAPO_OLDER_SWITCH": [
    {"start": "2026-08-27T13:49:00", "end": "2026-08-27T14:30:00"},
    {"start": "2026-08-27T14:55:00", "end": null}
  ],
  "TAPO_NEW_SWITCH": [ ... ]
}

Each entry is one continuous ON period for that device, in full
ISO-8601 timestamps (so periods spanning midnight are handled
naturally - no per-day bucketing headaches). "end": null means the
device is currently ON and that period is still open; the next
record_off() call will fill it in.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

HISTORY_FILE = Path(__file__).parent / "logs" / "usage_history.json"


def _load():
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable file - don't crash the monitor loop or
        # the dashboard over it, just treat it as empty. A fresh
        # record_on/record_off call will recreate it cleanly.
        return {}


def _save(data):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(HISTORY_FILE)  # atomic swap - a concurrent reader (the
                                # dashboard, in a separate process) never
                                # sees a half-written file.


def load_all():
    """
    Public loader for external callers (the dashboard). Reads the
    whole file ONCE - pass the result into get_periods_for_date's
    `data` argument for every device/date you need, instead of
    re-reading the file each time.
    """
    return _load()


def record_on(name, ts=None):
    """
    Call the moment a device is confirmed ON. Idempotent: if a period
    is already open for this device (its last entry has end=None),
    this does nothing - safe to call from multiple places in
    monitor.py without double-counting.
    """
    ts = ts or datetime.now()
    data = _load()
    periods = data.setdefault(name, [])
    if periods and periods[-1]["end"] is None:
        return
    periods.append({"start": ts.isoformat(timespec="seconds"), "end": None})
    _save(data)


def record_off(name, ts=None):
    """
    Call the moment a device is confirmed OFF, or goes offline /
    unreachable (we lose visibility into its real state, so we stop
    crediting ON time until we get a fresh ON confirmation).
    Idempotent: if nothing is open for this device, does nothing.
    """
    ts = ts or datetime.now()
    data = _load()
    periods = data.get(name)
    if not periods or periods[-1]["end"] is not None:
        return
    periods[-1]["end"] = ts.isoformat(timespec="seconds")
    _save(data)


def get_periods_for_date(name, target_date, data=None):
    """
    Returns (periods, total) for ONE calendar date:
      periods    - list of (start, end) datetime tuples, clipped to
                   [midnight, midnight+1day) for that date (or to
                   "now" if target_date is today and a period is
                   still open/ongoing).
      total      - a timedelta of total ON time within that date.

    `target_date` is a datetime.date. Pass an already-loaded `data`
    dict (from load_all()) to avoid re-reading the file for every
    device/date on the same page load.
    """
    if data is None:
        data = _load()
    raw = data.get(name, [])

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    now = datetime.now()
    clip_end = min(day_end, now) if target_date == now.date() else day_end

    periods = []
    for p in raw:
        start = datetime.fromisoformat(p["start"])
        end = datetime.fromisoformat(p["end"]) if p["end"] else now
        s = max(start, day_start)
        e = min(end, clip_end)
        if e > s:
            periods.append((s, e))

    total = sum((e - s for s, e in periods), timedelta())
    return periods, total


def current_open_period_start(name, data=None):
    """
    If this device currently has an open (ongoing) ON period, returns
    its start time (a datetime) - i.e. "on since when", regardless of
    what date is being browsed elsewhere. Returns None if it's not
    currently tracked as ON.
    """
    if data is None:
        data = _load()
    periods = data.get(name, [])
    if periods and periods[-1]["end"] is None:
        return datetime.fromisoformat(periods[-1]["start"])
    return None


def last_off_time(name, data=None):
    """
    The end time of the most recently CLOSED period for this device -
    i.e. "off since when". Returns None if there's no closed period
    yet (or the device has never been recorded).
    """
    if data is None:
        data = _load()
    periods = data.get(name, [])
    if periods and periods[-1]["end"] is not None:
        return datetime.fromisoformat(periods[-1]["end"])
    return None


def available_dates(data=None):
    """
    Every calendar date (across all devices) that has at least one
    recorded period touching it - used to bound the dashboard's date
    picker to days that actually have data.
    """
    if data is None:
        data = _load()
    dates = set()
    now = datetime.now()
    for periods in data.values():
        for p in periods:
            start = datetime.fromisoformat(p["start"]).date()
            end = (datetime.fromisoformat(p["end"]) if p["end"] else now).date()
            d = start
            while d <= end:
                dates.add(d)
                d += timedelta(days=1)
    return dates
