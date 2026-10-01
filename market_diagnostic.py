"""Write a non-sensitive Actions artifact for market-data recovery diagnosis."""

import json
import os
import platform
import sys


def _market_summary(value):
    if not isinstance(value, dict):
        return {"status": "missing"}
    keys = ("status", "latestSessionDate", "expectedSessionDate", "nextDueAt", "reasonCode")
    return {key: value.get(key) for key in keys}


def _instrument_summary(value):
    if not isinstance(value, dict):
        return {"status": "missing"}
    keys = ("selectedSource", "sourceAttempts", "sampleCount", "latestSessionDate",
            "expectedSessionDate", "status", "reasonCode", "comparison", "cacheUsed")
    return {key: value.get(key) for key in keys}


def summarize(payload, expected_run_id=None, expected_commit=None, core_outcome=None,
              gate_reason=None, publication_reason=None, node_version=None):
    payload = payload if isinstance(payload, dict) else {}
    service = payload.get("service") if isinstance(payload.get("service"), dict) else {}
    markets = payload.get("markets") if isinstance(payload.get("markets"), dict) else {}
    instruments = payload.get("instruments") if isinstance(payload.get("instruments"), dict) else {}
    calendar = payload.get("calendar") if isinstance(payload.get("calendar"), dict) else {}
    current_run = (not expected_run_id or str(service.get("runId")) == str(expected_run_id))
    current_commit = (not expected_commit or service.get("commit") == expected_commit)
    generated_here = bool(payload and current_run and current_commit and core_outcome == "success")
    if generated_here:
        stage = "generated" if payload.get("status") == "ok" else "data_degraded"
    elif payload and not current_run:
        stage = "generation_not_verified"
    elif core_outcome in ("failure", "cancelled", "skipped"):
        stage = "core_" + str(core_outcome)
    else:
        stage = "market_data_not_generated"
    return {
        "schemaVersion": 1,
        "stage": stage,
        "dataStatus": payload.get("status", "missing"),
        "coreOutcome": core_outcome or "unknown",
        "gateReason": gate_reason,
        "publicationReason": publication_reason or "not_reached",
        "runtimeVersions": {"python": platform.python_version(), "node": node_version or "not_installed"},
        "generatedAt": service.get("generatedAt") or payload.get("generatedAt"),
        "windowDate": service.get("windowDate"),
        "window": service.get("window"),
        "commit": service.get("commit"),
        "runId": service.get("runId"),
        "calendarStatus": calendar.get("status", "missing"),
        "markets": {key: _market_summary(value) for key, value in markets.items()},
        "instruments": {key: _instrument_summary(value) for key, value in instruments.items()},
    }


def main(status_path=None, output_path=None):
    status_path = status_path or (sys.argv[1] if len(sys.argv) > 1 else "public/status.json")
    output_path = output_path or (sys.argv[2] if len(sys.argv) > 2 else "market-diagnostic.json")
    try:
        with open(status_path, encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError, json.JSONDecodeError):
        payload = None
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(summarize(
            payload,
            expected_run_id=os.getenv("EXPECTED_RUN_ID"),
            expected_commit=os.getenv("EXPECTED_COMMIT"),
            core_outcome=os.getenv("CORE_OUTCOME"),
            gate_reason=os.getenv("GATE_REASON"),
            publication_reason=os.getenv("PUBLICATION_REASON"),
            node_version=os.getenv("NODE_VERSION"),
        ), file, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
