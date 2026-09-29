"""Official exchange calendars and completed-session health contracts."""

import csv
import datetime as dt
import hashlib
import html.parser
import io
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
SCHEMA_VERSION = 1
CALENDAR_TTL = dt.timedelta(hours=24)
TWSE_URL = "https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=html"
CBOE_URL = "https://www.cboe.com/us/options/holidays/csv/"


class CalendarUnavailable(ValueError):
    """Raised when no fresh official calendar can safely validate sessions."""


class _TableRows(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _request_text(url):
    last_error = None
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "SkynetMarketHealth/1.0"})
            with urlopen(request, timeout=12) as response:
                return response.read().decode("utf-8-sig")
        except HTTPError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504):
                break
        except (TimeoutError, URLError, OSError) as error:
            last_error = error
        if attempt < 2:
            time.sleep(0.25 * (2 ** attempt))
    raise RuntimeError(f"official calendar request failed ({type(last_error).__name__})")


def parse_twse_calendar(body):
    parser = _TableRows()
    parser.feed(body)
    closed = set()
    years = set()
    for row in parser.rows:
        joined = " ".join(row)
        match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", joined)
        if not match:
            continue
        session_date = dt.date(*(int(part) for part in match.groups()))
        years.add(session_date.year)
        name = row[1] if len(row) > 1 else joined
        # The official annual table also lists the first/last trading day.
        # Only explicit no-trade/holiday rows close an otherwise weekday session.
        if "交易日" not in name or "無交易" in name or "休市" in name:
            closed.add(session_date.isoformat())
    if not years:
        raise ValueError("TWSE calendar format changed or contains no dated rows")
    return {"closedDates": sorted(closed), "years": sorted(years)}


def parse_cboe_calendar(body):
    reader = csv.DictReader(io.StringIO(body))
    required = {"Holiday Name", "Date", "Regular Trading Hours"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("Cboe holiday CSV schema changed")
    closed = set()
    years = set()
    rows = 0
    for row in reader:
        raw_date = (row.get("Date") or "").strip()
        if not raw_date:
            continue
        try:
            session_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            session_date = dt.datetime.strptime(raw_date, "%m/%d/%Y").date()
        years.add(session_date.year)
        rows += 1
        regular_hours = (row.get("Regular Trading Hours") or "").strip()
        if regular_hours.lower() in ("none", "closed"):
            closed.add(session_date.isoformat())
        elif not regular_hours:
            raise ValueError("Cboe holiday CSV has an ambiguous trading-hours row")
    if not years or rows == 0:
        raise ValueError("Cboe holiday CSV contains no dated rows")
    return {"closedDates": sorted(closed), "years": sorted(years)}


def _checked_at(now):
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parsed_calendar_hash(twse, cboe):
    content = json.dumps({"twse": twse, "cboe": cboe}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _valid_cached_calendar(path, now):
    try:
        with open(path, encoding="utf-8") as file:
            cached = json.load(file)
        if cached.get("schemaVersion") != SCHEMA_VERSION:
            return None
        twse = cached.get("twse")
        cboe = cached.get("cboe")
        if not isinstance(twse, dict) or not isinstance(cboe, dict):
            return None
        if cached.get("contentHash") != _parsed_calendar_hash(twse, cboe):
            return None
        for source in (twse, cboe):
            if not isinstance(source.get("years"), list) or not isinstance(source.get("closedDates"), list):
                return None
            for raw_date in source["closedDates"]:
                day = dt.date.fromisoformat(raw_date)
                if day.year not in source["years"]:
                    return None
        checked = dt.datetime.fromisoformat(cached["checkedAt"].replace("Z", "+00:00"))
        age = now.astimezone(UTC) - checked.astimezone(UTC)
        if age < dt.timedelta(0) or age > CALENDAR_TTL:
            return None
        if now.year not in cached["twse"]["years"] or now.year not in cached["cboe"]["years"]:
            return None
        return cached
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def load_calendars(now=None, cache_dir=".calendar-cache"):
    now = now or dt.datetime.now(TAIPEI)
    cache_path = os.path.join(cache_dir, "market-calendar.json")
    try:
        twse_text = _request_text(TWSE_URL)
        cboe_text = _request_text(CBOE_URL)
        twse = parse_twse_calendar(twse_text)
        cboe = parse_cboe_calendar(cboe_text)
        if now.year not in twse["years"] or now.year not in cboe["years"]:
            raise ValueError("official calendars do not cover the current year")
        evidence = (twse_text + "\n" + cboe_text).encode("utf-8")
        calendars = {
            "schemaVersion": SCHEMA_VERSION,
            "checkedAt": _checked_at(now),
            "sha256": hashlib.sha256(evidence).hexdigest(),
            "contentHash": _parsed_calendar_hash(twse, cboe),
            "twse": twse,
            "cboe": cboe,
        }
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as file:
            json.dump(calendars, file, ensure_ascii=False, sort_keys=True)
        calendars["cacheUsed"] = False
        return calendars
    except Exception as error:
        cached = _valid_cached_calendar(cache_path, now)
        if cached:
            cached["cacheUsed"] = True
            cached["cacheReason"] = type(error).__name__
            return cached
        raise CalendarUnavailable(f"official calendar unavailable: {type(error).__name__}") from error


def _is_open(day, calendar):
    if day.year not in calendar["years"]:
        raise CalendarUnavailable(f"calendar coverage missing for {day.year}")
    return day.weekday() < 5 and day.isoformat() not in set(calendar["closedDates"])


def _previous_open(day, calendar, *, include_day=True):
    candidate = day if include_day else day - dt.timedelta(days=1)
    for _ in range(370):
        if _is_open(candidate, calendar):
            return candidate
        candidate -= dt.timedelta(days=1)
    raise CalendarUnavailable("calendar has no previous session in its coverage")


def _next_open(day, calendar):
    candidate = day + dt.timedelta(days=1)
    for _ in range(370):
        if _is_open(candidate, calendar):
            return candidate
        candidate += dt.timedelta(days=1)
    raise CalendarUnavailable("calendar has no next session in its coverage")


def expected_taiwan_session(now, calendar):
    now = now.astimezone(TAIPEI)
    today = now.date()
    after_close_buffer = (now.hour, now.minute) >= (14, 30)
    if after_close_buffer and _is_open(today, calendar):
        return today
    return _previous_open(today, calendar, include_day=not _is_open(today, calendar))


def taiwan_next_due(now, expected, calendar):
    session = _next_open(expected, calendar)
    return dt.datetime.combine(session, dt.time(14, 30), TAIPEI)


def expected_us_session(now, calendar):
    now = now.astimezone(TAIPEI)
    candidate = now.date() - dt.timedelta(days=7)
    found = None
    # VIX's latest fully publishable session cannot be later than today's
    # Taiwan date; avoid probing unneeded future-year calendar coverage here.
    for offset in range(8):
        day = candidate + dt.timedelta(days=offset)
        if not _is_open(day, calendar):
            continue
        deadline = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6, 30), TAIPEI)
        if deadline <= now:
            found = day
    if found is None:
        raise CalendarUnavailable("no completed US session in verified calendar window")
    return found


def us_next_due(expected, calendar):
    session = _next_open(expected, calendar)
    return dt.datetime.combine(session + dt.timedelta(days=1), dt.time(6, 30), TAIPEI)


def market_contract(latest, expected, next_due, market_open_today, reason=None):
    if reason:
        return {
            "status": "unavailable", "latestSessionDate": latest.isoformat() if latest else None,
            "expectedSessionDate": expected.isoformat() if expected else None,
            "nextDueAt": next_due.isoformat() if next_due else None, "reasonCode": reason,
        }
    latest_text = latest.isoformat() if latest else None
    expected_text = expected.isoformat() if expected else None
    if latest != expected:
        return {
            "status": "stale", "latestSessionDate": latest_text,
            "expectedSessionDate": expected_text,
            "nextDueAt": next_due.isoformat() if next_due else None,
            "reasonCode": "MARKET_DATA_STALE",
        }
    return {
        "status": "fresh" if market_open_today else "market_closed",
        "latestSessionDate": latest_text, "expectedSessionDate": expected_text,
        "nextDueAt": next_due.isoformat() if next_due else None,
        "reasonCode": "OK" if market_open_today else "OFFICIAL_MARKET_CLOSED",
    }


def build_calendar_contract(calendars):
    checked_at = calendars["checkedAt"]
    def through(source):
        return f"{max(source['years'])}-12-31"
    return {
        "status": "verified",
        "version": calendars["sha256"][:16],
        "checkedAt": checked_at,
        "cacheUsed": bool(calendars.get("cacheUsed")),
        "twse": {"source": "TWSE", "checkedAt": checked_at, "coverageThrough": through(calendars["twse"])},
        "cboe": {"source": "Cboe", "checkedAt": checked_at, "coverageThrough": through(calendars["cboe"])},
    }
