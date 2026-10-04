"""Decide whether this run may publish validated market history cache files."""

import json
import os
import sys


def cacheable_instruments(payload):
    if not isinstance(payload, dict):
        return set()
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict) or calendar.get("status") != "verified":
        return set()
    instruments = payload.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        return set()
    # Each instrument cache is written only after its own full session and
    # OHLC validation. A failure in another instrument must not discard a
    # good cache; conflicts are never eligible for cache refresh.
    return {
        symbol for symbol, instrument in instruments.items()
        if isinstance(instrument, dict)
        and instrument.get("status") in ("fresh", "market_closed")
        and instrument.get("reasonCode") != "SOURCE_CONFLICT"
        and instrument.get("selectedSource") in ("TWSE", "Yahoo", "verified_cache")
        and instrument.get("latestSessionDate")
        and instrument.get("latestSessionDate") == instrument.get("expectedSessionDate")
    }


def is_cache_eligible(payload):
    return bool(cacheable_instruments(payload))


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
