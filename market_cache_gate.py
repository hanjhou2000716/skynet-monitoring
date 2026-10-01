"""Decide whether this run may publish validated market history cache files."""

import json
import os
import sys


def is_cache_eligible(payload):
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict) or calendar.get("status") != "verified":
        return False
    markets = payload.get("markets")
    if not isinstance(markets, dict) or not markets:
        return False
    for market in markets.values():
        if not isinstance(market, dict) or market.get("status") not in ("fresh", "market_closed"):
            return False
        if not market.get("latestSessionDate") or market.get("latestSessionDate") != market.get("expectedSessionDate"):
            return False
    instruments = payload.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        return False
    for instrument in instruments.values():
        if not isinstance(instrument, dict) or instrument.get("status") not in ("fresh", "market_closed"):
            return False
        if instrument.get("reasonCode") == "SOURCE_CONFLICT":
            return False
        if not instrument.get("latestSessionDate") or instrument.get("latestSessionDate") != instrument.get("expectedSessionDate"):
            return False
    quality = payload.get("dataQuality")
    sources = quality.get("sources") if isinstance(quality, dict) else None
    return isinstance(sources, dict) and bool(sources) and all(value == "ok" for value in sources.values())


def main(status_path=None, output_path=None):
    status_path = status_path or (sys.argv[1] if len(sys.argv) > 1 else "public/status.json")
    output_path = output_path or os.getenv("GITHUB_OUTPUT")
    try:
        with open(status_path, encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError, json.JSONDecodeError):
        payload = None
    eligible = is_cache_eligible(payload)
    if output_path:
        with open(output_path, "a", encoding="utf-8") as file:
            file.write(f"eligible={'true' if eligible else 'false'}\n")
    return eligible


if __name__ == "__main__":
    main()
