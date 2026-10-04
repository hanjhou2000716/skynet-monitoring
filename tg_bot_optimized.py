"""Generate the public, market-only payload for Skynet Monitoring.

Portfolio accounting belongs to the Growth Dashboard.  Keeping Skynet free of
Google Sheets and portfolio secrets makes its health status describe the market
monitor itself, instead of failing whenever the accounting data is unavailable.
"""

import datetime
import calendar
import json
import math
import os
import time
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

import yfinance as yf
from market_data import (
    MarketDataError, TOTAL_TIMEOUT_SECONDS, compare_sources, fetch_twse_history, has_complete_range, has_complete_sessions,
    save_validated_cache, validated_cache,
)
from market_health import (
    CalendarUnavailable,
    build_calendar_contract,
    expected_taiwan_session,
    expected_us_session,
    load_calendars,
    market_contract,
    taiwan_next_due,
    us_next_due,
)


TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
MARKET_CACHE_KEYS = {"^TWII": "TWII", "006208.TW": "006208", "^VIX": "VIX"}


def _market_cache_paths(symbol, cache_dir):
    import os

    canonical = MARKET_CACHE_KEYS.get(symbol, symbol.replace("^", ""))
    names = [canonical + ".json"]
    legacy = symbol.replace("^", "") + ".json"
    if legacy not in names:
        names.append(legacy)
    return [os.path.join(cache_dir, name) for name in names]


def _verified_market_cache(symbol, expected_day, cache_dir):
    minimum = 200 if symbol == "^TWII" else 100 if symbol == "006208.TW" else 2
    paths = _market_cache_paths(symbol, cache_dir)
    valid = []
    for path in paths:
        frame = validated_cache(path, expected_day, minimum)
        if frame is not None:
            valid.append((path, frame))
    if len(valid) > 1 and compare_sources(valid[0][1], valid[1][1]) == "SOURCE_CONFLICT":
        return None, paths[0], None, "CONFLICT"
    if valid:
        path, frame = valid[0]
        migrated = path != paths[0]
        if migrated:
            saved = save_validated_cache(paths[0], frame, frame.attrs.get("cacheSource", "TWSE"),
                                         frame.attrs.get("cacheVerifiedAt"))
            if not saved:
                return None, paths[0], None, "MIGRATION_FAILED"
        source = frame.attrs.get("cacheSource", "TWSE" if symbol != "^VIX" else "Yahoo")
        return frame, paths[0], source, "MIGRATED" if migrated else "VALID"
    return None, paths[0], None, "MISSING_OR_INVALID"


def _session_day(value):
    return value.date() if hasattr(value, "date") else value


def write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def _history_for_completed_sessions(symbol, period, exchange_timezone, expected_date, deadline=None):
    """Fetch ordered, finite daily bars and remove sessions not yet completed."""
    history = _fetch_market_history(symbol, period, deadline)
    if history is None or history.empty or "Close" not in history:
        raise ValueError("empty or malformed history")
    dates = []
    for value in history.index:
        stamp = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(exchange_timezone)
        dates.append(stamp.date())
    if dates != sorted(dates):
        raise ValueError("history dates are not ordered")
    if len(set(dates)) != len(dates):
        raise ValueError("history contains duplicate sessions")
    history = history.copy()
    history["_sessionDate"] = dates
    history = history[history["_sessionDate"].map(lambda day: day <= expected_date)]
    if history.empty:
        raise ValueError("no completed session bars")
    for column in ("Close",):
        values = [float(value) for value in history[column]]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError(f"{column} contains a non-positive or non-finite value")
    for column in ("Open", "High", "Low"):
        if column in history:
            values = [float(value) for value in history[column]]
            if any(not math.isfinite(value) or value <= 0 for value in values):
                raise ValueError(f"{column} contains a non-positive or non-finite value")
    if {"Open", "High", "Low", "Close"}.issubset(history.columns):
        if any(
            float(row["Low"]) > min(float(row["Open"]), float(row["Close"]))
            or float(row["High"]) < max(float(row["Open"]), float(row["Close"]))
            for _, row in history.iterrows()
        ):
            raise ValueError("OHLC relationship is invalid")
    history.index = history["_sessionDate"]
    return history


def _is_transient_market_error(error):
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None)
    if status in (429, 500, 502, 503, 504):
        return True
    kind = type(error).__name__.lower()
    message = str(error).lower()
    return any(token in kind or token in message for token in (
        "timeout", "connectionerror", "connecterror", "ratelimit", "429", "500", "502", "503", "504"
    ))


def _fetch_market_history(symbol, period, deadline=None):
    """Retry transient Yahoo transport failures at most three times."""
    ticker = yf.Ticker(symbol)
    last_error = None
    for attempt in range(3):
        try:
            remaining = deadline - time.monotonic() if deadline is not None else 12
            if remaining <= 0:
                raise TimeoutError("market acquisition deadline exceeded")
            history = ticker.history(period=period, auto_adjust=False, timeout=min(12, remaining))
            if history is not None and not history.empty:
                history.attrs["sourceAttempts"] = attempt + 1
                return history
            raise ValueError("empty history response")
        except Exception as error:
            last_error = error
            if not _is_transient_market_error(error):
                try:
                    error.attempts = attempt + 1
                except Exception:
                    pass
                raise
        if attempt < 2:
            delay = 0.5 * (2 ** attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            time.sleep(delay)
    error = RuntimeError(f"market source exhausted transient retries ({type(last_error).__name__})")
    error.attempts = 3
    raise error


def consecutive_true(values):
    count = 0
    for value in reversed(values):
        if not bool(value):
            break
        count += 1
    return count


def _source_error_reason(error):
    """Normalize provider exceptions to stable, non-sensitive reason codes."""
    reason = getattr(error, "reason_code", None)
    if reason:
        return str(reason)
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "SOURCE_TIMEOUT"
    status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"SOURCE_HTTP_{status}"
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "SOURCE_RESPONSE_INVALID"
    return "SOURCE_UNAVAILABLE"


def _failed_source(error):
    # Diagnostics are intentionally categorical; do not publish provider payloads.
    return f"unavailable:{_source_error_reason(error)}"


def _instrument_failure(existing, error, expected_date):
    """Keep source/date evidence when downstream sample checks fail closed."""
    details = dict(existing) if isinstance(existing, dict) else {}
    has_observed_date = bool(details.get("latestSessionDate"))
    reason = (
        details.get("reasonCode") if details.get("status") in ("stale", "unavailable") else None
    ) or getattr(error, "reason_code", None) or (
        "HISTORY_SESSION_GAP" if has_observed_date else "SOURCE_UNAVAILABLE"
    )
    details.update(
        status="stale" if has_observed_date else "unavailable",
        reasonCode=reason,
        expectedSessionDate=expected_date.isoformat() if expected_date else None,
        cacheUsed=bool(details.get("cacheUsed", False)),
    )
    details.setdefault("selectedSource", None)
    details.setdefault("sourceAttempts", getattr(error, "attempts", 0))
    details.setdefault("sampleCount", 0)
    details.setdefault("latestSessionDate", None)
    source_results = getattr(error, "source_results", None)
    if source_results:
        details["sourceResults"] = source_results
    if getattr(error, "cache_status", None):
        details["cacheStatus"] = error.cache_status
    return details


def _resolve_history(symbol, period, timezone, expected_date, lookback_months, cache_dir, budget_deadline=None):
    """Prefer complete official TWSE history; use Yahoo or verified cache on failure."""
    import os

    expected_day = expected_date.date() if isinstance(expected_date, datetime.datetime) else expected_date
    cached_history, cache_path, cached_source, cache_status = _verified_market_cache(symbol, expected_day, cache_dir)
    attempts = 0
    official_error = None
    official_result = {"status": "PENDING"}
    official = None
    yahoo = None
    comparison = "NO_COMMON_SESSION"
    official_started = time.monotonic()
    try:
        official, official_attempts = fetch_twse_history(
            symbol, expected_day, lookback_months, deadline=budget_deadline,
        )
        official = _trim_history_period(official, expected_day, period)
        attempts += official_attempts
        official_result = {"status": "AVAILABLE", "attempts": official_attempts,
                           "latestSessionDate": _session_day(official.index[-1]).isoformat(),
                           "durationMs": round((time.monotonic() - official_started) * 1000)}
    except Exception as error:
        official_error = error
        attempts += getattr(error, "attempts", 1)
        official_result = {"status": "UNAVAILABLE", "reasonCode": _source_error_reason(error),
                           "errorType": type(error).__name__, "attempts": getattr(error, "attempts", 1),
                           "durationMs": round((time.monotonic() - official_started) * 1000)}

    yahoo_started = time.monotonic()
    try:
        yahoo = _history_for_completed_sessions(symbol, period, timezone, expected_day, budget_deadline)
        yahoo = _trim_history_period(yahoo, expected_day, period)
        yahoo_attempts = yahoo.attrs.get("sourceAttempts", 1)
        yahoo_result = {"status": "AVAILABLE", "attempts": yahoo_attempts,
                        "latestSessionDate": _session_day(yahoo.index[-1]).isoformat(),
                        "durationMs": round((time.monotonic() - yahoo_started) * 1000)}
    except Exception as error:
        yahoo_attempts = getattr(error, "attempts", 1)
        attempts += yahoo_attempts
        yahoo_error = error
        yahoo_result = {"status": "UNAVAILABLE", "reasonCode": _source_error_reason(error),
                        "errorType": type(error).__name__, "attempts": yahoo_attempts,
                        "durationMs": round((time.monotonic() - yahoo_started) * 1000)}
    else:
        attempts += yahoo_attempts
        yahoo_error = None

    source_results = {"TWSE": official_result, "Yahoo": yahoo_result}

    if official is not None and yahoo is not None:
        comparison = compare_sources(official, yahoo)
        if comparison == "SOURCE_CONFLICT":
            chosen, source, reason, status, cache_used = official, "TWSE", "SOURCE_CONFLICT", "unavailable", False
            return chosen, {
                "selectedSource": source, "sourceAttempts": attempts,
                "sampleCount": len(chosen), "comparison": comparison,
                "latestSessionDate": _session_day(chosen.index[-1]).isoformat(),
                "expectedSessionDate": expected_day.isoformat(), "cacheUsed": False,
                "status": status, "reasonCode": reason, "sourceResults": source_results,
                "cacheStatus": "CONFLICT_BLOCKED" if cache_status == "CONFLICT" or cached_history is not None else "NOT_USED",
            }
        elif _session_day(official.index[-1]) == expected_day:
            chosen, source, cache_used = official, "TWSE", False
            reason = "SECONDARY_LAGGING" if comparison == "SECONDARY_LAGGING" else (
                "NO_COMMON_SESSION" if comparison == "NO_COMMON_SESSION" else "OK"
            )
            status = "fresh"
        elif _session_day(yahoo.index[-1]) == expected_day:
            chosen, source, reason, status, cache_used = yahoo, "Yahoo", "OFFICIAL_LAGGING", "fresh", False
        elif _session_day(yahoo.index[-1]) > _session_day(official.index[-1]):
            chosen, source, reason, status, cache_used = yahoo, "Yahoo", "SOURCE_LAGGING", "stale", False
        else:
            chosen, source, reason, status, cache_used = official, "TWSE", "SOURCE_LAGGING", "stale", False
        selected_status, selected_reason, selected_cache = status, reason, cache_used
        if status != "fresh":
            stale_cache = cached_history
            if stale_cache is not None:
                cached_comparison = compare_sources(stale_cache, chosen)
                if cached_comparison == "SOURCE_CONFLICT":
                    return stale_cache, {
                        "selectedSource": "verified_cache", "sourceAttempts": attempts,
                        "sampleCount": len(stale_cache), "comparison": "SOURCE_CONFLICT",
                        "latestSessionDate": _session_day(stale_cache.index[-1]).isoformat(),
                        "expectedSessionDate": expected_day.isoformat(), "cacheUsed": True,
                        "status": "unavailable", "reasonCode": "SOURCE_CONFLICT",
                        "sourceResults": source_results, "cacheStatus": "CONFLICT_BLOCKED",
                    }
                if _session_day(stale_cache.index[-1]) > _session_day(chosen.index[-1]):
                    chosen, source, selected_cache = stale_cache, "verified_cache", True
                    selected_status = "fresh" if _session_day(chosen.index[-1]) == expected_day else "stale"
                    selected_reason = "VERIFIED_CACHE" if selected_status == "fresh" else "CACHE_STALE"
        return chosen, {
            "selectedSource": source, "sourceAttempts": attempts,
            "sampleCount": len(chosen), "comparison": comparison,
            "latestSessionDate": _session_day(chosen.index[-1]).isoformat(),
            "expectedSessionDate": expected_day.isoformat(), "cacheUsed": selected_cache,
            "status": selected_status, "reasonCode": selected_reason,
            "sourceResults": source_results,
            "cacheStatus": "CONFLICT_BLOCKED" if cache_status == "CONFLICT" else cache_status,
        }

    if official is not None:
        chosen, source = official, "TWSE"
    elif yahoo is not None:
        chosen, source = yahoo, "Yahoo"
    else:
        chosen, source = None, None

    cached = cached_history
    if cached is not None:
        if chosen is not None:
            comparison = compare_sources(cached, chosen)
            if comparison == "SOURCE_CONFLICT":
                reason, status, cache_used = "SOURCE_CONFLICT", "unavailable", False
            elif _session_day(cached.index[-1]) > _session_day(chosen.index[-1]):
                chosen, source, reason, status, cache_used = cached, "verified_cache", "VERIFIED_CACHE", "fresh", True
            else:
                reason = ("SECONDARY_UNAVAILABLE" if official is not None and yahoo is None else
                          "OFFICIAL_UNAVAILABLE" if official_error else "SOURCE_LAGGING")
                status = "fresh" if _session_day(chosen.index[-1]) == expected_day else "stale"
                cache_used = False
        else:
            is_current = _session_day(cached.index[-1]) == expected_day
            chosen, source, reason, status, cache_used = (
                cached, "verified_cache", "VERIFIED_CACHE" if is_current else "CACHE_STALE",
                "fresh" if is_current else "stale", True,
            )
    elif chosen is not None:
        is_current = _session_day(chosen.index[-1]) == expected_day
        reason = "SECONDARY_UNAVAILABLE" if official is not None and yahoo_error and is_current else (
            "OFFICIAL_UNAVAILABLE" if official_error and is_current else (
                "SOURCE_LAGGING" if not is_current else "OK"
            )
        )
        status, cache_used = ("fresh" if is_current else "stale"), False
    else:
        error = MarketDataError("SOURCES_UNAVAILABLE", attempts)
        error.source_results = source_results
        error.cache_status = "CONFLICT_BLOCKED" if cache_status == "CONFLICT" else cache_status
        raise error from (yahoo_error or official_error)

    return chosen, {
        "selectedSource": source, "sourceAttempts": attempts,
        "sampleCount": len(chosen), "comparison": comparison if cached is not None else (
            "SECONDARY_UNAVAILABLE" if official is not None and yahoo is None and yahoo_error else
            "OFFICIAL_UNAVAILABLE" if official is None else "NO_COMMON_SESSION"
        ),
        "latestSessionDate": _session_day(chosen.index[-1]).isoformat(),
        "expectedSessionDate": expected_day.isoformat(), "cacheUsed": cache_used,
        "status": status, "reasonCode": reason,
        "sourceResults": source_results,
        "cacheStatus": "CONFLICT_BLOCKED" if cache_status == "CONFLICT" else cache_status,
    }


def _call_history_resolver(history_resolver, arguments, budget_deadline):
    if history_resolver is _resolve_history:
        return history_resolver(*arguments, budget_deadline=budget_deadline)
    return history_resolver(*arguments)


def _fetch_vix_history(expected_us, budget_deadline):
    history = _history_for_completed_sessions("^VIX", "90d", NEW_YORK, expected_us, budget_deadline)
    return _trim_history_period(history, expected_us, "90d")


def _trim_history_period(frame, expected_day, period):
    if period.endswith("d"):
        days = int(period[:-1])
        start = expected_day - datetime.timedelta(days=days)
    elif period.endswith("mo"):
        months = int(period[:-2])
        month_index = expected_day.year * 12 + expected_day.month - 1 - months
        year, month = divmod(month_index, 12)
        month += 1
        day = min(expected_day.day, calendar.monthrange(year, month)[1])
        start = datetime.date(year, month, day)
    else:
        raise ValueError("unsupported history interval")
    day_index = [value.date() if hasattr(value, "date") else value for value in frame.index]
    return frame.loc[[day >= start for day in day_index]].copy()


def market_snapshot(now=None, cache_dir=".calendar-cache", history_cache_dir=".market-cache",
                    history_resolver=None):
    now = now or datetime.datetime.now(TAIPEI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    else:
        now = now.astimezone(TAIPEI)
    sources = {"taiex": "unavailable", "vix": "unavailable", "006208": "unavailable"}
    budget_deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    values = {
        "taiex": None,
        "ma200": None,
        "daysBelowMa": 0,
        "vix": None,
        "daysVixAbove20": 0,
        "peak_006208": None,
        "asset_006208": None,
    }
    markets = {}
    instruments = {}
    history_resolver = history_resolver or _resolve_history
    try:
        calendars = load_calendars(now, cache_dir, deadline=budget_deadline)
        expected_tw = expected_taiwan_session(now, calendars["twse"])
        expected_us = expected_us_session(now, calendars["cboe"])
        calendar_contract = build_calendar_contract(calendars)
    except CalendarUnavailable as error:
        calendars = None
        expected_tw = expected_us = None
        calendar_contract = {"status": "unavailable", "reasonCode": "CALENDAR_UNVERIFIED"}

    # Taiwan index, Taiwan ETF, and VIX are independent acquisitions. Start
    # them together so one slow official history cannot consume the shared
    # 180-second deadline before the ETF fallback gets a chance to run.
    market_pool = ThreadPoolExecutor(max_workers=3)
    taiex_future = vix_future = fund_future = None
    if calendars is not None and expected_tw:
        taiex_future = market_pool.submit(
            _call_history_resolver, history_resolver,
            ("^TWII", "400d", TAIPEI, expected_tw, 14, history_cache_dir), budget_deadline,
        )
        fund_future = market_pool.submit(
            _call_history_resolver, history_resolver,
            ("006208.TW", "6mo", TAIPEI, expected_tw, 7, history_cache_dir), budget_deadline,
        )
    if calendars is not None and expected_us:
        vix_future = market_pool.submit(_fetch_vix_history, expected_us, budget_deadline)

    taiwan_latest = []
    taiwan_reference_sessions = set()
    taiex_history = None
    try:
        if expected_tw:
            taiex_history, instruments["^TWII"] = taiex_future.result()
        else:
            taiex_history, instruments["^TWII"] = None, {}
        taiwan_reference_sessions = {
            _session_day(value) for value in taiex_history.index
        } if taiex_history is not None else set()
        if taiex_history is None or taiex_history.empty:
            raise MarketDataError("HISTORY_SESSION_GAP", instruments.get("^TWII", {}).get("sourceAttempts", 0))
        close = taiex_history["Close"].astype(float)
        values["taiex"] = round(float(close.iloc[-1]), 2)
        taiwan_latest.append(taiex_history.index[-1])
        if (len(taiex_history) < 200 or not has_complete_sessions(
                taiex_history, expected_tw, calendars["twse"], 200,
                reference_sessions=taiwan_reference_sessions,
        )):
            raise MarketDataError("HISTORY_SESSION_GAP", instruments.get("^TWII", {}).get("sourceAttempts", 0))
        average = close.rolling(200, min_periods=200).mean()
        if not math.isfinite(float(average.iloc[-1])):
            raise ValueError("200-session moving average unavailable")
        values["ma200"] = round(float(average.iloc[-1]), 2)
        below = (close < average).fillna(False).tolist()
        values["daysBelowMa"] = consecutive_true(below)
        instruments["^TWII"].update(
            latestSessionDate=_session_day(taiex_history.index[-1]).isoformat(),
            expectedSessionDate=expected_tw.isoformat(), sampleCount=len(taiex_history),
        )
        sources["taiex"] = "ok" if instruments["^TWII"].get("status") == "fresh" else "stale"
        if (instruments["^TWII"].get("status") == "fresh" and
                instruments["^TWII"].get("selectedSource") in ("TWSE", "Yahoo")):
            save_validated_cache(
                os.path.join(history_cache_dir, "TWII.json"), taiex_history,
                instruments["^TWII"].get("selectedSource"), now.isoformat(),
            )
    except Exception as error:
        instruments["^TWII"] = _instrument_failure(instruments.get("^TWII"), error, expected_tw)
        sources["taiex"] = "stale" if instruments["^TWII"]["status"] == "stale" else _failed_source(error)

    us_latest = None
    try:
        vix_history = vix_future.result() if expected_us else None
        vix_cache_path = os.path.join(history_cache_dir, "VIX.json")
        vix_history = _trim_history_period(vix_history, expected_us, "90d") if vix_history is not None else None
        vix_reference_sessions = {_session_day(value) for value in vix_history.index} if vix_history is not None else set()
        if (vix_history is None or len(vix_history) < 2 or not has_complete_range(
                vix_history, expected_us - datetime.timedelta(days=89), expected_us,
                calendars["cboe"], reference_sessions=vix_reference_sessions,
        )):
            raise MarketDataError("HISTORY_SESSION_GAP", 1)
        vix_cache = validated_cache(vix_cache_path, expected_us, 2)
        vix_cache_used = False
        if vix_cache is not None:
            if compare_sources(vix_cache, vix_history) == "SOURCE_CONFLICT":
                raise MarketDataError("SOURCE_CONFLICT", 1)
            if _session_day(vix_cache.index[-1]) > _session_day(vix_history.index[-1]):
                vix_history, vix_cache_used = vix_cache, True
        vix = vix_history["Close"].astype(float)
        values["vix"] = round(float(vix.iloc[-1]), 2)
        values["daysVixAbove20"] = consecutive_true((vix > 20).tolist())
        us_latest = vix_history.index[-1]
        sources["vix"] = "ok"
        instruments["^VIX"] = {
            "selectedSource": "Yahoo", "sourceAttempts": 1, "sampleCount": len(vix_history),
            "latestSessionDate": _session_day(vix_history.index[-1]).isoformat(),
            "expectedSessionDate": expected_us.isoformat() if expected_us else None,
            "status": "fresh" if _session_day(us_latest) == expected_us else "stale",
            "reasonCode": "SOURCE_CONFLICT" if vix_cache is not None and compare_sources(vix_cache, vix_history) == "SOURCE_CONFLICT" else (
                "VERIFIED_CACHE" if vix_cache_used else
                "OK" if _session_day(us_latest) == expected_us else "SOURCE_LAGGING"
            ),
            "cacheUsed": vix_cache_used,
        }
        if instruments["^VIX"]["status"] != "fresh":
            sources["vix"] = "stale"
        else:
            save_validated_cache(vix_cache_path, vix_history, "Yahoo", now.isoformat())
    except Exception as error:
        vix_cache = validated_cache(os.path.join(history_cache_dir, "VIX.json"), expected_us, 2) if expected_us else None
        if vix_cache is not None:
            vix = vix_cache["Close"].astype(float)
            values["vix"] = round(float(vix.iloc[-1]), 2)
            values["daysVixAbove20"] = consecutive_true((vix > 20).tolist())
            us_latest = vix_cache.index[-1]
            is_current = _session_day(us_latest) == expected_us
            sources["vix"] = "ok" if is_current else "stale"
            instruments["^VIX"] = {
                "selectedSource": "verified_cache", "sourceAttempts": 1,
                "sampleCount": len(vix_cache), "latestSessionDate": _session_day(us_latest).isoformat(),
                "expectedSessionDate": expected_us.isoformat(), "status": "fresh" if is_current else "stale",
                "reasonCode": "VERIFIED_CACHE" if is_current else "CACHE_STALE", "cacheUsed": True,
            }
        else:
            instruments["^VIX"] = _instrument_failure(instruments.get("^VIX"), error, expected_us)
            sources["vix"] = "stale" if instruments["^VIX"]["status"] == "stale" else _failed_source(error)

    fund_latest = None
    try:
        if expected_tw:
            fund, instruments["006208"] = fund_future.result()
        else:
            fund, instruments["006208"] = None, {}
        month_index = expected_tw.year * 12 + expected_tw.month - 1 - 6
        start_year, start_month = divmod(month_index, 12)
        start_month += 1
        start_day = min(expected_tw.day, calendar.monthrange(start_year, start_month)[1])
        fund_start = datetime.date(start_year, start_month, start_day)
        if fund is None or "High" not in fund:
            raise ValueError("completed 006208 history unavailable")
        values["peak_006208"] = round(float(fund["High"].max()), 2)
        values["asset_006208"] = round(float(fund["Close"].iloc[-1]), 2)
        fund_latest = fund.index[-1]
        taiwan_latest.append(fund_latest)
        if not has_complete_range(
                fund, fund_start, expected_tw, calendars["twse"],
                reference_sessions=taiwan_reference_sessions,
        ):
            raise MarketDataError("HISTORY_SESSION_GAP", instruments.get("006208", {}).get("sourceAttempts", 0))
        instruments["006208"].update(
            latestSessionDate=_session_day(fund.index[-1]).isoformat(),
            expectedSessionDate=expected_tw.isoformat(), sampleCount=len(fund),
        )
        sources["006208"] = "ok" if instruments["006208"].get("status") == "fresh" else "stale"
        if (instruments["006208"].get("status") == "fresh" and
                instruments["006208"].get("selectedSource") in ("TWSE", "Yahoo")):
            save_validated_cache(
                os.path.join(history_cache_dir, "006208.json"), fund,
                instruments["006208"].get("selectedSource"), now.isoformat(),
            )
    except Exception as error:
        instruments["006208"] = _instrument_failure(instruments.get("006208"), error, expected_tw)
        sources["006208"] = "stale" if instruments["006208"]["status"] == "stale" else _failed_source(error)

    market_pool.shutdown(wait=True)

    if calendars is None:
        markets["taiwan"] = {"status": "calendar_unverified", "latestSessionDate": None,
                             "expectedSessionDate": None, "nextDueAt": None,
                             "reasonCode": "CALENDAR_UNVERIFIED"}
        markets["us"] = {"status": "calendar_unverified", "latestSessionDate": None,
                         "expectedSessionDate": None, "nextDueAt": None,
                         "reasonCode": "CALENDAR_UNVERIFIED"}
    else:
        try:
            tw_latest = min(taiwan_latest) if len(taiwan_latest) == 2 else (taiwan_latest[0] if taiwan_latest else None)
            tw_reason = "SOURCE_CONFLICT" if any(
                instruments.get(key, {}).get("reasonCode") == "SOURCE_CONFLICT"
                for key in ("^TWII", "006208")
            ) else None
            markets["taiwan"] = market_contract(
                tw_latest, expected_tw, taiwan_next_due(now, expected_tw, calendars["twse"]),
                now.date().weekday() < 5 and now.date().isoformat() not in calendars["twse"]["closedDates"], tw_reason,
            )
            us_reason = None if sources["vix"] == "ok" else instruments.get("^VIX", {}).get("reasonCode", "SOURCE_UNAVAILABLE")
            ny_today = now.astimezone(NEW_YORK).date()
            markets["us"] = market_contract(
                us_latest, expected_us, us_next_due(expected_us, calendars["cboe"]),
                ny_today.weekday() < 5 and ny_today.isoformat() not in calendars["cboe"]["closedDates"], us_reason,
            )
        except CalendarUnavailable:
            calendar_contract = {"status": "unavailable", "reasonCode": "CALENDAR_UNVERIFIED"}
            markets = {
                "taiwan": {"status": "calendar_unverified", "latestSessionDate": None,
                           "expectedSessionDate": None, "nextDueAt": None,
                           "reasonCode": "CALENDAR_UNVERIFIED"},
                "us": {"status": "calendar_unverified", "latestSessionDate": None,
                       "expectedSessionDate": None, "nextDueAt": None,
                       "reasonCode": "CALENDAR_UNVERIFIED"},
            }
    return values, sources, markets, calendar_contract, instruments


def main():
    now = datetime.datetime.now(TAIPEI)
    values, sources, markets, calendar, instruments = market_snapshot(now)
    markets_ok = all(market.get("status") in ("fresh", "market_closed") for market in markets.values())
    status = "ok" if all(value == "ok" for value in sources.values()) and markets_ok and calendar.get("status") == "verified" else "degraded"
    local_minutes = now.hour * 60 + now.minute
    window = os.getenv("TARGET_WINDOW") or (
        "afternoon" if local_minutes >= 14 * 60 + 45 else
        "morning" if local_minutes >= 6 * 60 + 40 else "manual"
    )
    window_date = os.getenv("TARGET_WINDOW_DATE") or now.date().isoformat()
    generated_at = now.isoformat()
    service = {
        "status": "ok", "generatedAt": generated_at,
        "windowDate": window_date, "window": window,
        "commit": os.getenv("GITHUB_SHA", "local"),
        "runId": os.getenv("GITHUB_RUN_ID", "local"),
        "dataStatus": status,
    }
    payload = {
        **values,
        "lastUpdated": now.strftime("%Y/%m/%d %H:%M:%S"),
        "status": status,
        "schemaVersion": 2,
        "generatedAt": generated_at,
        "service": service,
        "markets": markets,
        "instruments": instruments,
        "calendar": calendar,
        "dataQuality": {
            "status": status,
            "expectedCadenceHours": 12,
            "staleAfterHours": 18,
            "timezone": "Asia/Taipei",
            "sources": sources,
        },
    }
    write_json("public/data.json", payload)
    write_json("public/status.json", payload["dataQuality"] | {
        "generatedAt": payload["generatedAt"],
        "lastUpdated": payload["lastUpdated"],
        "schemaVersion": payload["schemaVersion"],
        "service": service,
        "instruments": instruments,
        "markets": markets,
        "calendar": calendar,
    })
    print(f"Skynet market data generated: status={status} taiwan={markets['taiwan']['status']} us={markets['us']['status']}")


if __name__ == "__main__":
    main()
