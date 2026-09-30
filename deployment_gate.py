"""Stop queued or delayed Skynet builds from replacing a newer Pages publication."""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_REPOSITORY = "hanjhou2000716/skynet-monitoring"
STATUS_URL = "https://hanjhou2000716.github.io/skynet-monitoring/status.json"


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing generatedAt")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI)
    return parsed.astimezone(dt.timezone.utc)


def _rank(payload):
    service = payload.get("service") if isinstance(payload, dict) else None
    if payload.get("schemaVersion") != 2 or not isinstance(service, dict):
        raise ValueError("status contract is not v2")
    day = dt.date.fromisoformat(service["windowDate"])
    window = service.get("window")
    if window not in ("morning", "afternoon"):
        raise ValueError("status window is invalid")
    return (day.toordinal(), 0 if window == "morning" else 1, _timestamp(service.get("generatedAt") or payload.get("generatedAt")))


def should_publish(candidate, run_sha, main_sha, live_status):
    """Return (publish, reason); a verifiably newer main/live publication wins."""
    service = candidate.get("service") if isinstance(candidate, dict) else None
    if not isinstance(service, dict) or candidate.get("schemaVersion") != 2:
        raise ValueError("candidate status contract is not v2")
    if service.get("commit") != run_sha:
        raise ValueError("candidate status commit does not match workflow commit")
    if run_sha != main_sha:
        return False, "SKIP_OLD_MAIN_COMMIT"
    if not isinstance(live_status, dict):
        return True, "RUN_NO_LIVE_STATUS"
    try:
        live_rank = _rank(live_status)
        candidate_rank = _rank(candidate)
    except (KeyError, TypeError, ValueError):
        return True, "RUN_LIVE_STATUS_UNVERIFIED"
    if live_rank > candidate_rank:
        return False, "SKIP_NEWER_LIVE_PUBLICATION"
    return True, "RUN_CANDIDATE_NOT_OLDER_THAN_LIVE"


def _headers(token=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "SkynetPublishGate/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def fetch_main_sha(repository=None, token=None):
    repository = repository or os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY)
    api = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    url = f"{api}/repos/{repository}/commits/main"
    request = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))["sha"]


def fetch_live_status(url=STATUS_URL):
    separator = "&" if "?" in url else "?"
    url = url + separator + "publish_gate=" + str(int(dt.datetime.now().timestamp()))
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def main(candidate_path=None, output_path=None, environ=None,
         main_sha_fetcher=fetch_main_sha, status_fetcher=fetch_live_status):
    environ = os.environ if environ is None else environ
    candidate_path = candidate_path or (sys.argv[1] if len(sys.argv) > 1 else "public/status.json")
    output_path = output_path or environ.get("GITHUB_OUTPUT")
    with open(candidate_path, encoding="utf-8") as file:
        candidate = json.load(file)
    run_sha = environ.get("GITHUB_SHA", "")
    main_sha = main_sha_fetcher(environ.get("GITHUB_REPOSITORY"), environ.get("GH_TOKEN"))
    try:
        live_status = status_fetcher()
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        live_status = None
    publish, reason = should_publish(candidate, run_sha, main_sha, live_status)
    values = {"deploy": "true" if publish else "false", "reason": reason}
    if output_path:
        with open(output_path, "a", encoding="utf-8") as file:
            for key, value in values.items():
                file.write(f"{key}={value}\n")
    print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
