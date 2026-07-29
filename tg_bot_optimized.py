"""Generate the public, market-only payload for Skynet Monitoring.

Portfolio accounting belongs to the Growth Dashboard.  Keeping Skynet free of
Google Sheets and portfolio secrets makes its health status describe the market
monitor itself, instead of failing whenever the accounting data is unavailable.
"""

import datetime
import json
import os

import yfinance as yf


TAIPEI = datetime.timezone(datetime.timedelta(hours=8), name="Asia/Taipei")


def write_json(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def recent_closes(symbol, period):
    """Return valid close prices only; an unavailable feed is never faked."""
    history = yf.Ticker(symbol).history(period=period, auto_adjust=False)
    closes = history["Close"].dropna()
    if closes.empty:
        raise RuntimeError(f"No close data returned for {symbol}")
    return closes


def consecutive_true(values):
    count = 0
    for value in reversed(values):
        if not bool(value):
            break
        count += 1
    return count


def market_snapshot():
    sources = {}
    values = {
        "taiex": None,
        "ma200": None,
        "daysBelowMa": 0,
        "vix": None,
        "daysVixAbove20": 0,
        "peak_006208": None,
        "asset_006208": None,
    }

    try:
        taiex = recent_closes("^TWII", "400d")
        ma200 = taiex.rolling(200).mean()
        values["taiex"] = round(float(taiex.iloc[-1]), 2)
        values["ma200"] = round(float(ma200.iloc[-1]), 2)
        values["daysBelowMa"] = consecutive_true((taiex >= ma200).fillna(True).eq(False).tolist())
        sources["taiex"] = "ok"
    except Exception as error:
        sources["taiex"] = f"unavailable: {error}"

    try:
        vix = recent_closes("^VIX", "90d")
        values["vix"] = round(float(vix.iloc[-1]), 2)
        values["daysVixAbove20"] = consecutive_true((vix > 20).tolist())
        sources["vix"] = "ok"
    except Exception as error:
        sources["vix"] = f"unavailable: {error}"

    try:
        fund = yf.Ticker("006208.TW").history(period="6mo", auto_adjust=False)
        high = fund["High"].dropna()
        close = fund["Close"].dropna()
        if high.empty or close.empty:
            raise RuntimeError("No 006208 price data returned")
        values["peak_006208"] = round(float(high.max()), 2)
        values["asset_006208"] = round(float(close.iloc[-1]), 2)
        sources["006208"] = "ok"
    except Exception as error:
        sources["006208"] = f"unavailable: {error}"

    return values, sources


def main():
    now = datetime.datetime.now(TAIPEI)
    values, sources = market_snapshot()
    status = "ok" if all(value == "ok" for value in sources.values()) else "degraded"
    payload = {
        **values,
        "lastUpdated": now.strftime("%Y/%m/%d %H:%M:%S"),
        "status": status,
        "generatedAt": now.isoformat(),
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
    })
    print(f"Skynet market data generated: status={status}")


if __name__ == "__main__":
    main()
