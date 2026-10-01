"""Official Taiwan daily bars with bounded Yahoo fallback and validation."""

import datetime as dt
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd


TAIPEI = dt.timezone(dt.timedelta(hours=8))
TWSE_BASE = "https://www.twse.com.tw"
PRICE_TOLERANCE = 0.005
REQUEST_TIMEOUT_SECONDS = 12
TOTAL_TIMEOUT_SECONDS = 180


class MarketDataError(ValueError):
    def __init__(self, reason_code, attempts=0):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.attempts = attempts


def _months(end_date, count):
    year, month = end_date.year, end_date.month
    result = []
    for _ in range(count):
        result.append((year, month))
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return list(reversed(result))


def _session_date(value):
    value = str(value).strip()
    year, month, day = (int(part) for part in value.split("/"))
    if year < 1911:
        year += 1911
    return dt.date(year, month, day)


def _number(value):
    parsed = float(str(value).replace(",", "").strip())
    if parsed <= 0 or parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise ValueError("invalid numeric bar")
    return parsed


def _request_month(symbol, year, month, deadline):
    if symbol == "^TWII":
        path = "/rwd/zh/TAIEX/MI_5MINS_HIST"
        params = {"date": f"{year}{month:02d}01", "response": "json"}
    elif symbol == "006208.TW":
        path = "/exchangeReport/STOCK_DAY"
        params = {"date": f"{year}{month:02d}01", "stockNo": "006208", "response": "json"}
    else:
        raise ValueError("unsupported official Taiwan instrument")
    url = TWSE_BASE + path + "?" + urllib.parse.urlencode(params)
    attempts = 0
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MarketDataError("MARKET_REQUEST_DEADLINE", attempts)
        attempts += 1
        request = urllib.request.Request(url, headers={"User-Agent": "SkynetMarketData/1.0"})
        try:
            context = ssl.create_default_context()
            strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
            if strict_flag:
                context.verify_flags &= ~strict_flag
            with urllib.request.urlopen(request, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining), context=context) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            if payload.get("stat") != "OK":
                raise MarketDataError("SOURCE_FORMAT_INVALID", attempts)
            fields = payload.get("fields") or []
            date_index = fields.index("日期")
            if symbol == "^TWII":
                close_index = fields.index("收盤指數")
                high_index = fields.index("最高指數")
                low_index = fields.index("最低指數")
                open_index = fields.index("開盤指數")
            else:
                close_index = fields.index("收盤價")
                high_index = fields.index("最高價")
                low_index = fields.index("最低價")
                open_index = fields.index("開盤價")
            parsed = []
            for row in payload.get("data") or []:
                if len(row) <= max(date_index, close_index, high_index, low_index, open_index):
                    raise MarketDataError("SOURCE_FORMAT_INVALID", attempts)
                day = _session_date(row[date_index])
                parsed.append((day, _number(row[open_index]), _number(row[high_index]),
                               _number(row[low_index]), _number(row[close_index])))
            if parsed != sorted(parsed, key=lambda bar: bar[0]):
                raise MarketDataError("SOURCE_DATES_UNORDERED", attempts)
            if len({bar[0] for bar in parsed}) != len(parsed):
                raise MarketDataError("SOURCE_DUPLICATE_DATE", attempts)
            return parsed, attempts
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise MarketDataError(f"SOURCE_HTTP_{error.code}", attempts) from error
            if attempt == 2:
                raise MarketDataError(f"SOURCE_HTTP_{error.code}", attempts) from error
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            if attempt == 2:
                raise MarketDataError("SOURCE_UNAVAILABLE", attempts) from error
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            if isinstance(error, MarketDataError):
                raise
            raise MarketDataError("SOURCE_FORMAT_INVALID", attempts) from error
        if attempt < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MarketDataError("MARKET_REQUEST_DEADLINE", attempts)
            time.sleep(min((2, 4)[attempt], remaining))
    raise MarketDataError("SOURCE_UNAVAILABLE", attempts)


def fetch_twse_history(symbol, expected_date, months_back, deadline=None):
    """Fetch monthly official archives, bounded to three attempts per request."""
    if isinstance(expected_date, dt.datetime):
        expected_date = expected_date.date()
    deadline = deadline or (time.monotonic() + TOTAL_TIMEOUT_SECONDS)
    records = {}
    attempts = 0
    for year, month in _months(expected_date, months_back):
        try:
            rows, request_attempts = _request_month(symbol, year, month, deadline)
        except MarketDataError as error:
            raise MarketDataError(error.reason_code, attempts + error.attempts) from error
        attempts += request_attempts
        for day, open_price, high, low, close in rows:
            if day <= expected_date:
                if not (low <= min(open_price, close) <= max(open_price, close) <= high):
                    raise MarketDataError("SOURCE_OHLC_INVALID", attempts)
                records[day] = {"Open": open_price, "High": high, "Low": low, "Close": close}
    if not records:
        raise MarketDataError("SOURCE_EMPTY", attempts)
    frame = pd.DataFrame.from_dict(records, orient="index").sort_index()
    frame.index = pd.Index(list(frame.index), dtype=object)
    return frame, attempts


def compare_sources(primary, secondary):
    """Compare recent common closes; return a categorical conflict only."""
    def day(value):
        return value.date() if hasattr(value, "date") else value

    primary_closes = {day(index): float(row["Close"]) for index, row in primary.iterrows()}
    secondary_closes = {day(index): float(row["Close"]) for index, row in secondary.iterrows()}
    common = sorted(set(primary_closes).intersection(secondary_closes))[-5:]
    if len(common) == 0:
        return "NO_COMMON_SESSION"
    for day in common:
        a, b = primary_closes[day], secondary_closes[day]
        if abs(a - b) / max(abs(a), abs(b)) > PRICE_TOLERANCE:
            return "SOURCE_CONFLICT"
    if max(secondary_closes) < max(primary_closes):
        return "SECONDARY_LAGGING"
    return "MATCH"


def has_complete_sessions(frame, expected_date, calendar, count, reference_sessions=None):
    """Require expected sessions, using verified market-history dates for prior years.

    TWSE and Cboe publish the current-year calendar. For an older year in a rolling
    window, a verified official index history is the session-date reference; an
    instrument is complete only if it contains every corresponding reference date.
    """
    present = {value.date() if hasattr(value, "date") else value for value in frame.index}
    closed = set(calendar.get("closedDates", []))
    reference = {
        value.date() if hasattr(value, "date") else value
        for value in (reference_sessions or [])
    }
    covered_years = set(calendar.get("years", []))
    cursor = expected_date
    required = []
    while len(required) < count:
        if cursor.year in covered_years:
            if cursor.weekday() < 5 and cursor.isoformat() not in closed:
                required.append(cursor)
        else:
            prior_sessions = sorted(
                (day for day in reference if day.year == cursor.year and day <= cursor),
                reverse=True,
            )
            if not prior_sessions:
                return False
            required.extend(prior_sessions[:count - len(required)])
            cursor = dt.date(cursor.year, 1, 1) - dt.timedelta(days=1)
            continue
        cursor -= dt.timedelta(days=1)
    return all(day in present for day in required)


def has_complete_range(frame, start_date, end_date, calendar, reference_sessions=None):
    """Check every expected exchange session in a calculation's date interval."""
    present = {value.date() if hasattr(value, "date") else value for value in frame.index}
    closed = set(calendar.get("closedDates", []))
    reference = {
        value.date() if hasattr(value, "date") else value
        for value in (reference_sessions or [])
    }
    covered_years = set(calendar.get("years", []))
    required = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.year in covered_years:
            if cursor.weekday() < 5 and cursor.isoformat() not in closed:
                required.append(cursor)
        elif cursor in reference:
            required.append(cursor)
        cursor += dt.timedelta(days=1)
    return bool(required) and required[-1] == end_date and all(day in present for day in required)


def validated_cache(path, expected_date, min_sessions):
    """Load a hashed, schema-checked history cache. It never makes old data fresh."""
    import hashlib

    try:
        with open(path, encoding="utf-8") as file:
            payload = json.load(file)
        bars = payload["bars"]
        canonical = json.dumps(bars, sort_keys=True, separators=(",", ":"))
        if payload.get("schemaVersion") != 1 or hashlib.sha256(canonical.encode()).hexdigest() != payload.get("sha256"):
            return None
        frame = pd.DataFrame(bars).T
        frame.index = pd.DatetimeIndex([dt.date.fromisoformat(day) for day in frame.index])
        frame = frame.sort_index()
        verified = dt.datetime.fromisoformat(payload["verifiedAt"])
        if verified.tzinfo is None or payload.get("source") not in ("TWSE", "Yahoo"):
            return None
        age = dt.datetime.now(dt.timezone.utc) - verified.astimezone(dt.timezone.utc)
        # A cache remains session-fresh through weekends and market holidays only
        # when its last bar is still the calendar's latest expected session. The
        # callers enforce that exact-date rule; here reject only future validation.
        if age < dt.timedelta(0):
            return None
        if len(frame) < min_sessions or frame.index[-1].date() > expected_date:
            return None
        for column in ("High", "Low", "Close"):
            if column not in frame:
                return None
            values = frame[column].astype(float)
            if not values.map(lambda value: value > 0 and value < float("inf")).all():
                return None
        if not {"Open", "High", "Low"}.issubset(frame.columns):
            return None
        if not ((frame["Low"].astype(float) <= frame[["Open", "Close"]].astype(float).min(axis=1)) &
                (frame["High"].astype(float) >= frame[["Open", "Close"]].astype(float).max(axis=1))).all():
            return None
        return frame
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def save_validated_cache(path, frame, source, verified_at):
    import hashlib
    import os

    bars = {}
    for index, row in frame.iterrows():
        values = {key: float(value) for key, value in row.items() if not key.startswith("_")}
        if {"Open", "High", "Low", "Close"}.issubset(values):
            session = index.date() if hasattr(index, "date") else index
            bars[session.isoformat()] = values
    if not bars:
        return False
    canonical = json.dumps(bars, sort_keys=True, separators=(",", ":"))
    payload = {"schemaVersion": 1, "source": source, "verifiedAt": verified_at,
               "bars": bars, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, sort_keys=True, separators=(",", ":"))
    return True
