"""Conditional GitHub Pages fallback gate for Skynet updates."""

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
STATUS_URL = "https://hanjhou2000716.github.io/skynet-monitoring/status.json"
GITHUB_RUN_URL = "https://api.github.com/repos/hanjhou2000716/skynet-monitoring/actions/runs/{}"
GITHUB_RUN_ARTIFACTS_URL = "https://api.github.com/repos/hanjhou2000716/skynet-monitoring/actions/runs/{}/artifacts?per_page=100"
GITHUB_RUNS_URL = "https://api.github.com/repos/hanjhou2000716/skynet-monitoring/actions/workflows/deploy.yml/runs?branch=main&per_page=100"


def target_window(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=TAIPEI)
    else:
        now = now.astimezone(TAIPEI)
    minutes = now.hour * 60 + now.minute
    if minutes < 6 * 60 + 40:
        return (now.date() - dt.timedelta(days=1)).isoformat(), "afternoon"
    window = "afternoon" if minutes >= 14 * 60 + 45 else "morning"
    return now.date().isoformat(), window


def status_matches(payload, target_date, target, commit, now):
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
        return False
    service = payload.get("service") or {}
    calendar = payload.get("calendar") or {}
    markets = payload.get("markets") or {}
    if payload.get("status") != "ok" or service.get("status") != "ok":
        return False
    if (service.get("windowDate"), service.get("window"), service.get("commit")) != (
        target_date, target, commit
    ):
        return False
    if not service.get("runId"):
        return False
    if calendar.get("status") != "verified":
        return False
    for market in markets.values():
        if not isinstance(market, dict) or market.get("status") not in ("fresh", "market_closed"):
            return False
        latest = market.get("latestSessionDate")
        expected = market.get("expectedSessionDate")
        if not latest or latest != expected:
            return False
        try:
            due = dt.datetime.fromisoformat(market["nextDueAt"].replace("Z", "+00:00"))
            if due <= now:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return set(markets) >= {"taiwan", "us"}


def fetch_run_success(run_id, commit):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SkynetFallbackGate/1.0"}
    token = os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(GITHUB_RUN_URL.format(run_id), headers=headers)
    with urllib.request.urlopen(request, timeout=8) as response:
        run = json.loads(response.read().decode("utf-8"))
    if not (
        run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_sha") == commit
    ):
        return False
    artifacts_request = urllib.request.Request(GITHUB_RUN_ARTIFACTS_URL.format(run_id), headers=headers)
    with urllib.request.urlopen(artifacts_request, timeout=8) as response:
        artifact_payload = json.loads(response.read().decode("utf-8"))
    artifacts = artifact_payload.get("artifacts", []) if isinstance(artifact_payload, dict) else []
    return any(
        isinstance(item, dict)
        and item.get("name") == "skynet-publication-verification"
        and item.get("expired") is not True
        for item in artifacts
    )


def fetch_window_has_no_newer_failures(payload, target_date, target, now):
    service = payload.get("service") or {}
    try:
        published_at = dt.datetime.fromisoformat(service["generatedAt"].replace("Z", "+00:00"))
        published_at = published_at.astimezone(TAIPEI)
    except (KeyError, TypeError, ValueError):
        return False
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SkynetFallbackGate/1.0"}
    token = os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(GITHUB_RUNS_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=8) as response:
        runs = json.loads(response.read().decode("utf-8")).get("workflow_runs", [])
    current_run_id = os.getenv("GITHUB_RUN_ID")
    target_minutes = 14 * 60 + 45
    for run in runs:
        if str(run.get("id")) == str(current_run_id):
            continue
        if run.get("head_sha") != service.get("commit") or run.get("status") != "completed":
            continue
        created = run.get("created_at")
        try:
            created_at = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TAIPEI)
        except (AttributeError, TypeError, ValueError):
            continue
        if created_at.date().isoformat() != target_date or created_at <= published_at:
            continue
        minutes = created_at.hour * 60 + created_at.minute
        same_window = minutes < target_minutes if target == "morning" else minutes >= target_minutes
        if same_window and run.get("conclusion") != "success":
            return False
    return True


def already_succeeded(payload, target_date, target, commit, now, run_success=False):
    return bool(run_success and status_matches(payload, target_date, target, commit, now))


def fetch_public_status(url=STATUS_URL):
    request = urllib.request.Request(url + "?gate=" + str(int(dt.datetime.now().timestamp())),
                                     headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def decide(event_name, commit, now, payload=None, fetcher=fetch_public_status,
           run_checker=fetch_run_success, window_checker=fetch_window_has_no_newer_failures):
    target_date, window = target_window(now)
    if event_name in ("push", "workflow_dispatch"):
        return True, "RUN_MANUAL_OR_PUSH", target_date, window
    if payload is None:
        try:
            payload = fetcher()
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return True, "RUN_STATUS_UNVERIFIED", target_date, window
    if status_matches(payload, target_date, window, commit, now):
        try:
            run_success = run_checker((payload.get("service") or {}).get("runId"), commit)
            window_clean = window_checker(payload, target_date, window, now) if run_success else False
            if run_success and window_clean:
                return False, "SKIP_ALREADY_SUCCEEDED", target_date, window
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return True, "RUN_DEPLOYMENT_RECORD_UNVERIFIED", target_date, window
    return True, "RUN_UPDATE_MISSING_OR_DEGRADED", target_date, window


def main():
    now = dt.datetime.now(TAIPEI)
    event_name = os.getenv("GITHUB_EVENT_NAME", "schedule")
    commit = os.getenv("GITHUB_SHA", "")
    should_run, reason, target_date, window = decide(event_name, commit, now)
    output = os.getenv("GITHUB_OUTPUT")
    values = {
        "run": "true" if should_run else "false",
        "reason": reason,
        "window_date": target_date or now.date().isoformat(),
        "window": window or "manual",
    }
    if output:
        with open(output, "a", encoding="utf-8") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
