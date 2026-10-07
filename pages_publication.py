"""Unique Skynet Pages publication and live readback verification."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


API_ROOT = "https://api.github.com"
MANIFEST = "publication.json"


class PublicationError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def prepare_manifest(site_dir: str | Path, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    root = Path(site_dir)
    for required in ("index.html", "data.json", "status.json"):
        if not (root / required).is_file():
            raise PublicationError(f"Pages artifact is missing {required}")
    repo, run_id, attempt, commit = (env.get(key, "").strip() for key in (
        "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA",
    ))
    if not all((repo, run_id, attempt, commit)):
        raise PublicationError("workflow repository, run identity, or source commit is missing")
    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("generated public status is missing or invalid") from error
    service = status.get("service") if isinstance(status, Mapping) else None
    if not isinstance(service, Mapping):
        raise PublicationError("generated public status has no service evidence")
    digests = {
        item.relative_to(root).as_posix(): _hash(item.read_bytes())
        for item in sorted(root.rglob("*")) if item.is_file() and item.name != MANIFEST
    }
    critical = {name: digests[name] for name in ("index.html", "data.json", "status.json")}
    value = {
        "schemaVersion": 1,
        "publicationId": f"{repo}:{run_id}:{attempt}:1",
        "publicationAttempt": 1,
        "repository": repo,
        "runId": run_id,
        "runAttempt": attempt,
        "sourceCommit": commit,
        "windowDate": service.get("windowDate"),
        "window": service.get("window"),
        "generatedAt": service.get("generatedAt") or status.get("generatedAt"),
        "dataStatus": status.get("status"),
        "files": digests,
        "criticalFiles": critical,
    }
    value["contentHash"] = _hash(_canonical(value))
    (root / MANIFEST).write_bytes(_canonical(value) + b"\n")
    return value


def pages_build_version(publication_id: str, deployment_attempt: int) -> str:
    return _hash(f"{publication_id}:deployment:{deployment_attempt}".encode())


def _read_bytes(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 20) -> bytes:
    request = Request(url, headers={"User-Agent": "skynet-pages-publication-verifier/1", **dict(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def _request_json(method: str, url: str, token: str, body: Mapping[str, Any] | None = None) -> Any:
    request = Request(
        url,
        data=_canonical(body) if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "skynet-pages-publication-verifier/1",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise PublicationError(f"Pages API {method} failed: {type(error).__name__}") from error
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError("Pages API returned invalid JSON") from error


def _oidc(env: Mapping[str, str]) -> str:
    endpoint, request_token = env.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""), env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not endpoint or not request_token:
        raise PublicationError("GitHub Actions OIDC endpoint is unavailable")
    separator = "&" if "?" in endpoint else "?"
    url = endpoint + separator + urlencode({"audience": f"https://github.com/{env.get('GITHUB_REPOSITORY', '')}"})
    try:
        payload = json.loads(_read_bytes(url, headers={"Authorization": f"Bearer {request_token}"}))
    except (OSError, ValueError, HTTPError, URLError) as error:
        raise PublicationError(f"OIDC request failed: {type(error).__name__}") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("value"), str):
        raise PublicationError("GitHub Actions OIDC response is invalid")
    return payload["value"]


def _artifact_id(repo: str, run_id: str, token: str) -> int:
    payload = _request_json("GET", f"{API_ROOT}/repos/{repo}/actions/artifacts?per_page=100", token)
    artifacts = payload.get("artifacts", []) if isinstance(payload, Mapping) else []
    matches = [
        item for item in artifacts if isinstance(item, Mapping)
        and item.get("name") == "github-pages" and item.get("expired") is not True
        and str((item.get("workflow_run") or {}).get("id", "")) == str(run_id)
    ]
    if not matches:
        raise PublicationError("current workflow has no github-pages artifact")
    return int(max(matches, key=lambda item: int(item.get("id", 0)))["id"])


def _deploy(repo: str, artifact_id: int, publication_id: str, attempt: int, token: str, oidc_token: str) -> tuple[str, str | None]:
    result = _request_json("POST", f"{API_ROOT}/repos/{repo}/pages/deployments", token, {
        "artifact_id": artifact_id,
        "environment": "github-pages",
        "pages_build_version": pages_build_version(publication_id, attempt),
        "oidc_token": oidc_token,
    })
    if not isinstance(result, Mapping) or not result.get("status_url"):
        raise PublicationError("Pages API returned no deployment status URL")
    return str(result["status_url"]), result.get("page_url")


def _wait_deployment(status_url: str, token: str, deadline: float, wait: Callable[[float], None], clock: Callable[[], float]) -> None:
    while clock() < deadline:
        result = _request_json("GET", status_url, token)
        status = str(result.get("status", "")).lower() if isinstance(result, Mapping) else ""
        if status in {"succeed", "success", "succeeded"}:
            return
        if status in {"error", "failure", "failed"}:
            raise PublicationError("Pages API reports deployment failure")
        wait(5)
    raise PublicationError("Pages deployment status timed out")


def _live_url(base_url: str, relative: str, nonce: str) -> str:
    path = "/".join(quote(piece, safe="") for piece in relative.split("/"))
    full = urljoin(base_url.rstrip("/") + "/", path)
    parsed = urlsplit(full)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"publication_check": nonce}), ""))


def verify_live(site_dir: str | Path, base_url: str, *, reader: Callable[..., bytes] = _read_bytes,
                wait: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                max_wait: float = 300) -> str:
    try:
        expected = json.loads((Path(site_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublicationError("publication manifest is missing") from error
    unsigned = dict(expected)
    content_hash = unsigned.pop("contentHash", None)
    if content_hash != _hash(_canonical(unsigned)):
        raise PublicationError("local publication manifest hash mismatch")
    nonce = _hash(f"{expected.get('publicationId')}:{clock()}".encode())[:16]
    deadline = clock() + max_wait
    last_error = "old Pages content remains visible"
    while clock() <= deadline:
        try:
            live_bytes = reader(_live_url(base_url, MANIFEST, nonce), headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})
            live = json.loads(live_bytes)
            if not isinstance(live, Mapping):
                raise PublicationError("live manifest is invalid")
            if live.get("publicationId") != expected.get("publicationId"):
                try:
                    newer = (int(live.get("runId", 0)), int(live.get("runAttempt", 0))) > (int(expected.get("runId", 0)), int(expected.get("runAttempt", 0)))
                except (TypeError, ValueError):
                    newer = False
                if newer:
                    live_unsigned = dict(live)
                    live_hash = live_unsigned.pop("contentHash", None)
                    critical = live.get("criticalFiles")
                    integrity_ok = live_hash == _hash(_canonical(live_unsigned)) and isinstance(critical, Mapping)
                    for relative, expected_hash in (critical or {}).items():
                        data = reader(_live_url(base_url, str(relative), nonce), headers={"Cache-Control": "no-cache, no-store"})
                        if _hash(data) != expected_hash:
                            integrity_ok = False
                            break
                    if integrity_ok:
                        return "SUPERSEDED"
                    raise PublicationError("newer publication does not pass integrity verification")
                raise PublicationError("live Pages is serving an older publication")
            critical = expected.get("criticalFiles")
            if not isinstance(critical, Mapping):
                raise PublicationError("critical content hashes are missing")
            for relative, expected_hash in critical.items():
                data = reader(_live_url(base_url, str(relative), nonce), headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})
                if _hash(data) != expected_hash:
                    raise PublicationError(f"live content hash mismatch: {relative}")
            return "VERIFIED"
        except PublicationError as error:
            last_error = str(error)
        except Exception as error:  # noqa: BLE001 - bounded retry for CDN/cache propagation
            last_error = f"live readback failed: {type(error).__name__}"
        wait(min(15, max(0, deadline - clock())))
    raise PublicationError(f"PUBLICATION_NOT_VISIBLE: {last_error}")


def publish_and_verify(manifest_path: str | Path, *, env: Mapping[str, str] | None = None,
                       request: Callable[..., Any] = _request_json, oidc_provider: Callable[[Mapping[str, str]], str] = _oidc,
                       verify: Callable[..., str] = verify_live, wait: Callable[[float], None] = time.sleep,
                       clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    env = env or os.environ
    repo, run_id, token, base = (env.get(key, "").strip() for key in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GH_TOKEN", "PAGES_BASE_URL"))
    if not all((repo, run_id, token)):
        raise PublicationError("repository, run ID, or GitHub token missing")
    if not base:
        pages = _request_json("GET", f"{API_ROOT}/repos/{repo}/pages", token)
        base = str(pages.get("html_url", "")).strip() if isinstance(pages, Mapping) else ""
        if not base:
            raise PublicationError("GitHub Pages API returned no live site URL")
    manifest_path = Path(manifest_path)
    site_dir = manifest_path.parent
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_id = _artifact_id(repo, run_id, token)
    for attempt in (1, 2):
        try:
            status_url, page_url = _deploy(repo, artifact_id, expected["publicationId"], attempt, token, oidc_provider(env))
            _wait_deployment(status_url, token, clock() + 600, wait, clock)
            result = verify(site_dir, base, wait=wait, clock=clock, max_wait=300)
            return {
                "status": result,
                "publicationId": expected["publicationId"],
                "deploymentAttempt": attempt,
                "artifactId": artifact_id,
                "pagesBuildVersion": pages_build_version(expected["publicationId"], attempt),
                "pageUrl": page_url or base,
                "deploymentAccepted": True,
                "liveReadback": result,
                "dataStatus": expected.get("dataStatus"),
            }
        except PublicationError as error:
            if "SUPERSEDED" in str(error):
                return {"status": "SUPERSEDED", "publicationId": expected.get("publicationId"), "deploymentAttempt": attempt}
            if attempt == 2:
                raise
    raise PublicationError("Pages deployment failed")


def _write_summary(result: Mapping[str, Any] | None, error: str | None, env: Mapping[str, str]) -> None:
    output = Path(env.get("PUBLICATION_SUMMARY_PATH", ".private-build/pages-publication-summary.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    value = dict(result or {})
    value.update({
        "schemaVersion": 1,
        "status": value.get("status") or ("FAILED" if error else "UNKNOWN"),
        "reasonCode": "PUBLICATION_NOT_VISIBLE" if error and "PUBLICATION_NOT_VISIBLE" in error else "PUBLICATION_FAILED" if error else None,
        "verifiedAt": datetime.now(timezone.utc).isoformat(),
        "error": error,
    })
    output.write_bytes(_canonical(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("site_dir")
    publish = commands.add_parser("publish-and-verify")
    publish.add_argument("manifest_path")
    verify = commands.add_parser("verify-live")
    verify.add_argument("site_dir")
    verify.add_argument("--base-url", default=os.getenv("PAGES_BASE_URL", ""))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            value = prepare_manifest(args.site_dir)
            print(json.dumps({"status": "PREPARED", "publicationId": value["publicationId"], "contentHash": value["contentHash"]}))
            return 0
        if args.command == "verify-live":
            if not args.base_url:
                raise PublicationError("Pages base URL is required for independent verification")
            result = verify_live_publication(args.site_dir, args.base_url)
            manifest = json.loads((Path(args.site_dir) / MANIFEST).read_text(encoding="utf-8"))
            value = {"status": result, "publicationId": manifest.get("publicationId")}
            _write_summary(value, None, os.environ)
            print(json.dumps(value, sort_keys=True))
            return 0 if result in {"VERIFIED", "SUPERSEDED"} else 1
        result = publish_and_verify(args.manifest_path)
        _write_summary(result, None, os.environ)
        page_url = result.get("pageUrl")
        if page_url and os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"page_url={page_url}\n")
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") in {"VERIFIED", "SUPERSEDED"} else 1
    except Exception as error:  # noqa: BLE001 - always create a private failure diagnostic
        _write_summary(None, str(error), os.environ)
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
