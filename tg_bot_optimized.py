"""Generate the public, market-only payload for Skynet Monitoring.

Portfolio accounting belongs to the Growth Dashboard.  Keeping Skynet free of
Google Sheets and portfolio secrets makes its health status describe the market
monitor itself, instead of failing whenever the accounting data is unavailable.
"""

import datetime
import json
import math
import os
import time
from zoneinfo import ZoneInfo

import yfinance as yf
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


def write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def _history_for_completed_sessions(symbol, period, exchange_timezone, expected_date):
    """Fetch ordered, finite daily bars and remove sessions not yet completed."""
    history = _fetch_market_history(symbol, period)
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
    if "High" in history:
        highs = [float(value) for value in history["High"]]
        if any(not math.isfinite(value) or value <= 0 for value in highs):
            raise ValueError("High contains a non-positive or non-finite value")
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


def _fetch_market_history(symbol, period):
    """Retry transient Yahoo transport failures at most three times."""
    ticker = yf.Ticker(symbol)
    last_error = None
    for attempt in range(3):
        try:
            history = ticker.history(period=period, auto_adjust=False, timeout=12)
            if history is not None and not history.empty:
                return history
            last_error = ValueError("empty history response")
        except Exception as error:
            last_error = error
            if not _is_transient_market_error(error):
                raise
        if attempt < 2:
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"market source exhausted transient retries ({type(last_error).__name__})")


def consecutive_true(values):
    count = 0
    for value in reversed(values):
        if not bool(value):
            break
        count += 1
    return count


def _failed_source(error):
    # Diagnostics are intentionally categorical; do not publish provider payloads.
    return f"unavailable:{type(error).__name__}"


def market_snapshot(now=None, cache_dir=".calendar-cache"):
    now = now or datetime.datetime.now(TAIPEI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    else:
        now = now.astimezone(TAIPEI)
    sources = {"taiex": "unavailable", "vix": "unavailable", "006208": "unavailable"}
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
    try:
        calendars = load_calendars(now, cache_dir)
        expected_tw = expected_taiwan_session(now, calendars["twse"])
        expected_us = expected_us_session(now, calendars["cboe"])
        calendar_contract = build_calendar_contract(calendars)
    except CalendarUnavailable as error:
        calendars = None
        expected_tw = expected_us = None
        calendar_contract = {"status": "unavailable", "reasonCode": "CALENDAR_UNVERIFIED"}

    taiwan_latest = []
    try:
        taiex_history = _history_for_completed_sessions(
            "^TWII", "400d", TAIPEI, expected_tw
        ) if expected_tw else None
        if taiex_history is None or len(taiex_history) < 200:
            raise ValueError("fewer than 200 completed TAIEX sessions")
        close = taiex_history["Close"].astype(float)
        average = close.rolling(200, min_periods=200).mean()
        if not math.isfinite(float(average.iloc[-1])):
            raise ValueError("200-session moving average unavailable")
        values["taiex"] = round(float(close.iloc[-1]), 2)
        values["ma200"] = round(float(average.iloc[-1]), 2)
        below = (close < average).fillna(False).tolist()
        values["daysBelowMa"] = consecutive_true(below)
        taiwan_latest.append(taiex_history.index[-1])
        sources["taiex"] = "ok"
    except Exception as error:
        sources["taiex"] = _failed_source(error)

    us_latest = None
    try:
        vix_history = _history_for_completed_sessions("^VIX", "90d", NEW_YORK, expected_us) if expected_us else None
        if vix_history is None or len(vix_history) < 2:
            raise ValueError("fewer than two completed VIX sessions")
        vix = vix_history["Close"].astype(float)
        values["vix"] = round(float(vix.iloc[-1]), 2)
        values["daysVixAbove20"] = consecutive_true((vix > 20).tolist())
        us_latest = vix_history.index[-1]
        sources["vix"] = "ok"
    except Exception as error:
        sources["vix"] = _failed_source(error)

    fund_latest = None
    try:
        fund = _history_for_completed_sessions("006208.TW", "6mo", TAIPEI, expected_tw) if expected_tw else None
        if fund is None or "High" not in fund:
            raise ValueError("completed 006208 history unavailable")
        values["peak_006208"] = round(float(fund["High"].max()), 2)
        values["asset_006208"] = round(float(fund["Close"].iloc[-1]), 2)
        fund_latest = fund.index[-1]
        taiwan_latest.append(fund_latest)
        sources["006208"] = "ok"
    except Exception as error:
        sources["006208"] = _failed_source(error)

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
            tw_reason = "SOURCE_UNAVAILABLE" if any(sources[k] != "ok" for k in ("taiex", "006208")) else None
            markets["taiwan"] = market_contract(
                tw_latest, expected_tw, taiwan_next_due(now, expected_tw, calendars["twse"]),
                now.date().weekday() < 5 and now.date().isoformat() not in calendars["twse"]["closedDates"], tw_reason,
            )
            us_reason = None if sources["vix"] == "ok" else "SOURCE_UNAVAILABLE"
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
    return values, sources, markets, calendar_contract


def main():
    now = datetime.datetime.now(TAIPEI)
    values, sources, markets, calendar = market_snapshot(now)
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
    }
    payload = {
        **values,
        "lastUpdated": now.strftime("%Y/%m/%d %H:%M:%S"),
        "status": status,
        "schemaVersion": 2,
        "generatedAt": generated_at,
        "service": service,
        "markets": markets,
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
        "markets": markets,
        "calendar": calendar,
    })
    print(f"Skynet market data generated: status={status} taiwan={markets['taiwan']['status']} us={markets['us']['status']}")


if __name__ == "__main__":
    main()
