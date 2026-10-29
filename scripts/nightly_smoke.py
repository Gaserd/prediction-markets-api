#!/usr/bin/env python3
"""Helpers for the nightly live-smoke workflow.

The venue list lives in ``.github/workflows/nightly-smoke.yml``. This script
filters that list, decides whether a venue can run, writes the job summary, and
opens or updates one GitHub issue per venue. It never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Public market data on these hosts does not need an API key. Production hosts
# are rejected so the smoke job cannot be pointed at a live Kalshi account.
KALSHI_DEMO_HOSTS = frozenset(
    {
        "external-api.demo.kalshi.co",
        "demo-api.kalshi.co",
    }
)
KALSHI_DEMO_PATH = "/trade-api/v2"

_VENUE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_SKIP = re.compile(
    r"(api[_-]?key|api[_-]?secret).{0,80}(not set|unset|missing|not configured)"
    r"|(not set|unset|missing|not configured).{0,80}(api[_-]?key|api[_-]?secret)",
    re.IGNORECASE,
)
_SECRET_ENV = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)$")


class SmokeConfigError(ValueError):
    """The smoke job is configured in a way that must not call a venue."""


@dataclass(frozen=True)
class Venue:
    """One matrix entry. ``api_base`` is the Kalshi demo root, or empty."""

    venue: str
    required_secrets: tuple[str, ...]
    api_base: str
    demo_only: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "venue": self.venue,
            "required_secrets": ",".join(self.required_secrets),
            "api_base": self.api_base,
            "demo_only": self.demo_only,
        }


@dataclass(frozen=True)
class JunitResult:
    passed: int
    failed: int
    skipped: int
    failing_tests: tuple[str, ...]
    key_skips: int
    # Redacted assertion text for the job summary only. Never copied into the issue.
    failure_details: tuple[str, ...] = ()

    @property
    def outcome(self) -> str:
        """Classify a finished pytest run.

        ``no_key`` is only used when nothing passed and every skip is a missing
        credential. That must not be reported as a successful test run.
        """
        if self.failed:
            return "failed"
        if self.passed == 0 and self.skipped == 0:
            return "no_tests"
        if self.passed == 0 and self.key_skips > 0:
            return "no_key"
        if self.passed == 0:
            return "all_skipped"
        return "passed"


@dataclass(frozen=True)
class Gate:
    run: bool
    exit_code: int
    summary: str
    outcome: str
    failing_tests: tuple[str, ...] = ()
    detail: str = ""
    # True when the workflow config itself is invalid. The job stays red.
    config_error: bool = False


class CollectionFailed(Exception):
    """``pytest --collect-only`` failed before any live test ran."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class IssueStore:
    """Open issues for one venue, keyed by the nightly-smoke labels."""

    def list_open(self, venue: str) -> list[int]:
        raise NotImplementedError

    def create(self, venue: str, body: str) -> None:
        raise NotImplementedError

    def comment(self, number: int, body: str) -> None:
        raise NotImplementedError

    def close(self, number: int, body: str) -> None:
        raise NotImplementedError


def kalshi_smoke_api_base(environ: Mapping[str, str] | None = None) -> str | None:
    """Return a demo Trade API root, or None when ``KALSHI_API_BASE`` is unset.

    A non-demo value raises. The error text does not include the rejected URL.
    """
    env = os.environ if environ is None else environ
    raw = env.get("KALSHI_API_BASE", "").strip()
    if not raw:
        return None
    parts = urlsplit(raw.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
        or host not in KALSHI_DEMO_HOSTS
        or path != KALSHI_DEMO_PATH
    ):
        raise SmokeConfigError(
            "KALSHI_API_BASE must be the https Kalshi demo Trade API "
            "(external-api.demo.kalshi.co or demo-api.kalshi.co, path /trade-api/v2)"
        )
    return f"https://{host}{KALSHI_DEMO_PATH}"


def redact(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace credential-shaped environment values with ``***``."""
    env = os.environ if environ is None else environ
    secrets = sorted(
        (value for name, value in env.items() if _SECRET_ENV.search(name) and len(value) >= 8),
        key=len,
        reverse=True,
    )
    redacted = text
    for value in secrets:
        redacted = redacted.replace(value, "***")
    return redacted


def parse_venues(payload: str) -> list[Venue]:
    """Parse the workflow's venue list."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SmokeConfigError(f"Venue list is not JSON: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise SmokeConfigError("Venue list must be a non-empty JSON array")
    venues: list[Venue] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise SmokeConfigError("Each venue entry must be an object")
        venue = _required_str(item, "venue")
        if not _VENUE_NAME.fullmatch(venue):
            raise SmokeConfigError(f"Invalid venue name: {venue!r}")
        if venue in seen:
            raise SmokeConfigError(f"Duplicate venue: {venue}")
        seen.add(venue)
        required_raw = item.get("required_secrets", "")
        if not isinstance(required_raw, str):
            raise SmokeConfigError(f"{venue}: required_secrets must be a string")
        required = tuple(part for part in required_raw.split(",") if part)
        for name in required:
            if not _SECRET_NAME.fullmatch(name):
                raise SmokeConfigError(f"{venue}: invalid secret name {name!r}")
        api_base = item.get("api_base", "")
        if not isinstance(api_base, str):
            raise SmokeConfigError(f"{venue}: api_base must be a string")
        demo_only = item.get("demo_only", False)
        if not isinstance(demo_only, bool):
            raise SmokeConfigError(f"{venue}: demo_only must be a boolean")
        validated_base = _enforce_venue_api(venue, demo_only, api_base)
        venues.append(
            Venue(
                venue=venue,
                required_secrets=required,
                api_base=validated_base,
                demo_only=demo_only,
            )
        )
    return venues


def filter_venues(venues: Sequence[Venue], selected: str) -> list[Venue]:
    """Keep ``selected`` (comma or whitespace separated), or every venue when empty."""
    tokens = [part for part in re.split(r"[\s,]+", selected.strip()) if part]
    if not tokens:
        return list(venues)
    known = {venue.venue for venue in venues}
    unknown = sorted({token for token in tokens if token not in known})
    if unknown:
        names = ", ".join(venue.venue for venue in venues)
        raise SmokeConfigError(f"Unknown venue(s): {', '.join(unknown)}. Known venues: {names}")
    wanted = set(tokens)
    return [venue for venue in venues if venue.venue in wanted]


def _enforce_venue_api(venue: str, demo_only: bool, api_base: str) -> str:
    """Return the API root to export, or '' when the venue keeps its adapter default.

    Kalshi is demo-only even if the workflow list says otherwise. A production
    host, a missing base, or ``demo_only: false`` is rejected here.
    """
    if venue == "kalshi":
        if not demo_only:
            raise SmokeConfigError("kalshi must set demo_only true and a demo api_base")
        validated = kalshi_smoke_api_base({"KALSHI_API_BASE": api_base})
        if not validated:
            raise SmokeConfigError("kalshi requires a demo api_base")
        return validated
    if api_base.strip():
        validated = kalshi_smoke_api_base({"KALSHI_API_BASE": api_base})
        if not validated:
            raise SmokeConfigError(f"{venue}: api_base is empty")
        return validated
    if demo_only:
        raise SmokeConfigError(f"{venue}: demo_only venues need an api_base")
    return ""


# Predict.fun mainnet reads need this key. Testnet live tests do not, and the
# mainnet tests skip themselves. A missing key must not skip the whole job.
_OPTIONAL_MAINNET_SECRETS: dict[str, frozenset[str]] = {
    "predict_fun": frozenset({"PREDICTFUN_API_KEY"}),
}


def missing_secret_names(required: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Secret names that are unset or whitespace. Values are not returned."""
    return [name for name in required if not environ.get(name, "").strip()]


def blocking_secret_names(
    venue: str, required: Sequence[str], environ: Mapping[str, str]
) -> list[str]:
    """Secrets that skip the job. Predict.fun's mainnet key does not."""
    optional = _OPTIONAL_MAINNET_SECRETS.get(venue, frozenset())
    return [name for name in missing_secret_names(required, environ) if name not in optional]


def predict_fun_mainnet_key_skipped(venue: str, result: JunitResult) -> bool:
    """Predict.fun ran, nothing failed, and mainnet cases skipped for the key."""
    return venue == "predict_fun" and result.failed == 0 and result.key_skips > 0


def test_file_for(venue: str) -> Path:
    if not _VENUE_NAME.fullmatch(venue):
        raise SmokeConfigError(f"Invalid venue name: {venue!r}")
    return Path("tests") / f"test_{venue}_live.py"


def live_test_count(test_file: Path) -> int:
    """Count collected ``live`` tests. Does not execute them."""
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "live",
                str(test_file),
                "--collect-only",
                "-q",
                "--override-ini",
                "addopts=",
                "-p",
                "no:cacheprovider",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise CollectionFailed("pytest --collect-only timed out") from exc
    if completed.returncode == 5:
        return 0
    if completed.returncode != 0:
        detail = redact(f"{completed.stdout}\n{completed.stderr}").strip()
        raise CollectionFailed(detail[-2000:] or f"pytest collect exited {completed.returncode}")
    return sum(1 for line in completed.stdout.splitlines() if "::" in line)


def decide_gate(
    *,
    venue: str,
    test_file: Path,
    required_secrets: Sequence[str],
    environ: Mapping[str, str],
    demo_only: bool,
    api_base: str,
    file_exists: bool,
    collected: int | None,
    collection_error: str | None,
) -> Gate:
    """Decide whether pytest should run. Skips do not look like a green run."""
    if not _VENUE_NAME.fullmatch(venue):
        raise SmokeConfigError(f"Invalid venue name: {venue!r}")
    try:
        _enforce_venue_api(venue, demo_only, api_base)
    except SmokeConfigError as exc:
        summary = _summary(venue, "failed: smoke configuration", [str(exc)])
        return Gate(
            run=False,
            exit_code=1,
            summary=summary,
            outcome="failed",
            failing_tests=(f"configuration: {venue}",),
            detail=str(exc),
            config_error=True,
        )

    if not file_exists:
        summary = _summary(
            venue,
            "skipped: no live tests",
            [f"`{test_file.as_posix()}` is not in this revision, so nothing ran."],
        )
        return Gate(False, 0, summary, "no_tests")

    if collection_error:
        summary = _summary(
            venue,
            "failed: could not collect live tests",
            [f"`{test_file.as_posix()}` failed during collection.", collection_error[:500]],
        )
        return Gate(
            run=False,
            exit_code=1,
            summary=summary,
            outcome="failed",
            failing_tests=(f"collection: {test_file.as_posix()}",),
            detail=collection_error,
        )

    if not collected:
        summary = _summary(
            venue,
            "skipped: no live tests",
            [f"`pytest -m live {test_file.as_posix()}` collected 0 tests, so nothing ran."],
        )
        return Gate(False, 0, summary, "no_tests")

    missing = blocking_secret_names(venue, required_secrets, environ)
    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        summary = _summary(
            venue,
            "skipped: no key",
            [
                "This venue was not tested.",
                f"Missing repository secret: {names}.",
            ],
        )
        return Gate(False, 0, summary, "no_key")

    return Gate(True, 0, "", "run")


def parse_junit(path: Path) -> JunitResult:
    """Read pytest JUnit XML into counts and failing node ids."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise CollectionFailed(f"Could not read junit xml: {exc}") from exc
    passed = failed = skipped = key_skips = 0
    failing: list[str] = []
    details: list[str] = []
    for case in root.iter("testcase"):
        name = _node_id(case)
        if case.find("failure") is not None or case.find("error") is not None:
            failed += 1
            failing.append(name)
            details.append(_clip_failure_message(case))
            continue
        skip = case.find("skipped")
        if skip is not None:
            skipped += 1
            message = f"{skip.get('message') or ''}\n{skip.text or ''}"
            if _KEY_SKIP.search(message):
                key_skips += 1
            continue
        passed += 1
    return JunitResult(passed, failed, skipped, tuple(failing), key_skips, tuple(details))


def result_from_pytest(
    junit: Path | None,
    pytest_exit: int,
    *,
    exit_known: bool = True,
) -> JunitResult:
    """Prefer the JUnit file. A missing file is a failure, not a pass."""
    if junit is not None and junit.is_file():
        parsed = parse_junit(junit)
        unexpected = (not exit_known) or pytest_exit not in (0, 1, 5)
        if unexpected and parsed.outcome != "failed":
            note = (
                "pytest did not finish (no exit code)"
                if not exit_known
                else f"pytest exited {pytest_exit}"
            )
            return JunitResult(
                parsed.passed,
                parsed.failed + 1,
                parsed.skipped,
                parsed.failing_tests + (note,),
                parsed.key_skips,
                parsed.failure_details + ("",),
            )
        return parsed
    if pytest_exit == 5 and exit_known:
        return JunitResult(0, 0, 0, (), 0)
    if not exit_known:
        reason = (
            "pytest did not finish and smoke-junit.xml is missing (step timeout or cancellation)"
        )
    else:
        reason = f"smoke-junit.xml is missing after pytest exited {pytest_exit}"
    return JunitResult(0, 1, 0, (reason,), 0)


def status_line(result: JunitResult) -> str:
    if result.outcome == "no_key":
        return "skipped: no key"
    if result.outcome == "no_tests":
        return "skipped: no live tests"
    if result.outcome == "all_skipped":
        return f"skipped: all collected tests skipped ({result.skipped} skipped)"
    if result.outcome == "failed":
        return f"failed: {result.failed} failed, {result.passed} passed, {result.skipped} skipped"
    return f"passed: {result.passed} passed, {result.failed} failed, {result.skipped} skipped"


def summary_for_result(venue: str, result: JunitResult) -> str:
    if predict_fun_mainnet_key_skipped(venue, result):
        mainnet_lines = ["Mainnet tests skipped because `PREDICTFUN_API_KEY` is unset."]
        if result.passed:
            mainnet_lines.insert(0, f"{result.passed} test(s) that do not need a key passed.")
        return _summary(venue, "mainnet skipped: no key", mainnet_lines)
    lines: list[str] = []
    if result.outcome == "no_key":
        lines.append("This venue was not tested. Collected tests skipped because a key is unset.")
    elif result.failing_tests:
        lines.append("Failing tests:")
        lines.extend(_summary_failure_lines(result))
        if venue == "predict_fun" and result.key_skips:
            lines.append("Mainnet tests skipped because `PREDICTFUN_API_KEY` is unset.")
    if result.outcome == "passed" and result.skipped:
        lines.append(f"{result.skipped} test(s) skipped for a reason other than a missing key.")
    return _summary(venue, status_line(result), lines)


def failure_issue_body(
    *,
    venue: str,
    when: str,
    run_url: str,
    status: str,
    failing_tests: Sequence[str],
) -> str:
    lines = [
        f"Nightly live smoke failed for `{venue}`.",
        "",
        f"- Date: {when}",
        f"- Run: {run_url}",
        f"- Result: {status}",
        "",
        "Failing tests:",
    ]
    if failing_tests:
        lines.extend(f"- `{name}`" for name in failing_tests)
    else:
        lines.append("- `(no test names reported)`")
    lines.append("")
    return "\n".join(lines)


def pass_issue_body(*, venue: str, when: str, run_url: str, status: str) -> str:
    return "\n".join(
        [
            f"Nightly live smoke passed for `{venue}`.",
            "",
            f"- Date: {when}",
            f"- Run: {run_url}",
            f"- Result: {status}",
            "",
            "Closing this issue.",
            "",
        ]
    )


def sync_issue(
    store: IssueStore,
    *,
    venue: str,
    outcome: str,
    failure_body: str,
    pass_body: str,
) -> None:
    """Reuse the open issue for this venue. Close it only after a real pass."""
    open_issues = store.list_open(venue)
    if outcome == "failed":
        if open_issues:
            for number in open_issues:
                store.comment(number, failure_body)
        else:
            store.create(venue, failure_body)
        return
    if outcome == "passed":
        for number in open_issues:
            store.close(number, pass_body)


def _summary(venue: str, status: str, details: Sequence[str]) -> str:
    lines = [f"### {venue}", "", status, ""]
    lines.extend(detail for detail in details if detail)
    return "\n".join(lines).rstrip() + "\n"


def _summary_failure_lines(result: JunitResult) -> list[str]:
    """One summary bullet per failure. The short message stays out of the issue."""
    lines: list[str] = []
    details = result.failure_details
    for index, name in enumerate(result.failing_tests):
        message = details[index] if index < len(details) else ""
        if message and message != name:
            lines.append(f"- `{name}`: {message}")
        else:
            lines.append(f"- `{name}`")
    return lines


def _clip_failure_message(case: ET.Element) -> str:
    child = case.find("failure")
    if child is None:
        child = case.find("error")
    if child is None:
        return ""
    raw = child.get("message") or child.text or ""
    return _clip_text(redact(raw))


def _clip_text(text: str, limit: int = 200) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 3:
        return collapsed[:limit]
    return collapsed[: limit - 3] + "..."


def _node_id(case: ET.Element) -> str:
    name = case.get("name") or "unknown"
    file_name = case.get("file")
    if file_name:
        return f"{file_name}::{name}"
    classname = case.get("classname")
    if classname:
        return f"{classname}::{name}"
    return name


def _required_str(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SmokeConfigError(f"Venue entry is missing {key}")
    return value.strip()


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no", ""}:
        return False
    raise SmokeConfigError(f"Expected a boolean, got {raw!r}")


def _write(path: str | None, text: str) -> None:
    if not path:
        return
    destination = Path(path)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class GhIssueStore(IssueStore):
    """GitHub issues via ``gh``. The token stays in the environment, not argv."""

    def __init__(self, repo: str) -> None:
        if not repo or "/" not in repo:
            raise SmokeConfigError("GITHUB_REPOSITORY is not set")
        self.repo = repo

    def list_open(self, venue: str) -> list[int]:
        completed = self._run(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                "nightly-smoke",
                "--label",
                f"venue:{venue}",
                "--json",
                "number",
                "--limit",
                "20",
            ]
        )
        payload = json.loads(completed.stdout or "[]")
        return [int(item["number"]) for item in payload]

    def create(self, venue: str, body: str) -> None:
        self._ensure_label(
            "nightly-smoke",
            "B60205",
            "Nightly live smoke failure",
        )
        self._ensure_label(
            f"venue:{venue}",
            "FBCA04",
            f"Nightly live smoke for {venue}",
        )
        with self._body_file(body) as path:
            self._run(
                [
                    "issue",
                    "create",
                    "--title",
                    f"Nightly live smoke failed: {venue}",
                    "--label",
                    "nightly-smoke",
                    "--label",
                    f"venue:{venue}",
                    "--body-file",
                    str(path),
                ]
            )

    def comment(self, number: int, body: str) -> None:
        with self._body_file(body) as path:
            self._run(["issue", "comment", str(number), "--body-file", str(path)])

    def close(self, number: int, body: str) -> None:
        self.comment(number, body)
        self._run(["issue", "close", str(number), "--reason", "completed"])

    def _ensure_label(self, name: str, color: str, description: str) -> None:
        created = self._run(
            [
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            check=False,
        )
        if created.returncode == 0:
            return
        listed = self._run(
            ["label", "list", "--search", name, "--limit", "50", "--json", "name"],
            check=False,
        )
        if listed.returncode == 0:
            names = {item["name"] for item in json.loads(listed.stdout or "[]")}
            if name in names:
                return
        raise SmokeConfigError(self._failure_text(created))

    def _run(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["gh", *args, "--repo", self.repo],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if check and completed.returncode != 0:
            raise SmokeConfigError(self._failure_text(completed))
        return completed

    @staticmethod
    def _failure_text(completed: subprocess.CompletedProcess[str]) -> str:
        detail = redact(f"{completed.stderr}\n{completed.stdout}").strip()
        return detail[-1000:] or f"gh exited {completed.returncode}"

    @staticmethod
    def _body_file(body: str) -> _BodyFile:
        return _BodyFile(body)


class _BodyFile:
    """Temp file for ``gh --body-file``. Deleted when the ``with`` block ends."""

    def __init__(self, body: str) -> None:
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="nightly-smoke-",
            suffix=".md",
            delete=False,
        )
        try:
            handle.write(body)
        finally:
            handle.close()
        self.name = handle.name

    def __enter__(self) -> Path:
        return Path(self.name)

    def __exit__(self, *_exc: object) -> None:
        Path(self.name).unlink(missing_ok=True)


def _issue_store_from_env() -> GhIssueStore:
    return GhIssueStore(os.environ.get("GITHUB_REPOSITORY", ""))


def _apply_failure_or_pass(
    *,
    venue: str,
    outcome: str,
    status: str,
    failing_tests: Sequence[str],
    update_issues: bool,
) -> None:
    if outcome not in {"failed", "passed"} or not update_issues:
        return
    when = _utc_now()
    run_url = os.environ.get("RUN_URL", "").strip() or "(run url unavailable)"
    store = _issue_store_from_env()
    sync_issue(
        store,
        venue=venue,
        outcome=outcome,
        failure_body=failure_issue_body(
            venue=venue,
            when=when,
            run_url=run_url,
            status=status,
            failing_tests=failing_tests,
        ),
        pass_body=pass_issue_body(venue=venue, when=when, run_url=run_url, status=status),
    )


def cmd_select(github_output: str) -> int:
    try:
        venues = parse_venues(sys.stdin.read())
        chosen = filter_venues(venues, os.environ.get("SELECTED_VENUES", ""))
    except SmokeConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    payload = json.dumps([venue.as_dict() for venue in chosen], separators=(",", ":"))
    _write(github_output, f"venues<<VENUES_END\n{payload}\nVENUES_END\n")
    print("Selected venues: " + ", ".join(venue.venue for venue in chosen), file=sys.stderr)
    return 0


def cmd_gate(summary_path: str, github_output: str, update_issues: bool) -> int:
    venue = os.environ.get("VENUE", "").strip()
    required = tuple(
        part for part in os.environ.get("REQUIRED_SECRETS", "").split(",") if part.strip()
    )
    try:
        test_file = test_file_for(venue)
        demo_only = _parse_bool(os.environ.get("DEMO_ONLY", ""))
        api_base = os.environ.get("KALSHI_API_BASE", "")
    except SmokeConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    file_exists = test_file.is_file()
    collected: int | None = None
    collection_error: str | None = None
    # Collect only after the demo host (required for Kalshi) is valid.
    # decide_gate repeats that check and is what records a config failure.
    config_ok = True
    try:
        _enforce_venue_api(venue, demo_only, api_base)
    except SmokeConfigError:
        config_ok = False
    if file_exists and config_ok:
        try:
            collected = live_test_count(test_file)
        except CollectionFailed as exc:
            collection_error = exc.detail

    try:
        gate = decide_gate(
            venue=venue,
            test_file=test_file,
            required_secrets=required,
            environ=os.environ,
            demo_only=demo_only,
            api_base=api_base,
            file_exists=file_exists,
            collected=collected,
            collection_error=collection_error,
        )
    except SmokeConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if gate.summary:
        _write(summary_path, gate.summary)
    _write(github_output, f"run={'true' if gate.run else 'false'}\n")
    if gate.outcome == "no_key":
        print("::warning title=nightly-smoke::skipped: no key")
    if gate.outcome != "failed":
        return gate.exit_code
    status = gate.summary.splitlines()[2] if gate.summary else "failed"
    # A broken gate stays red even after the issue is filed. A venue failure
    # (for example collection) is green once the issue or comment exists.
    if gate.config_error:
        try:
            _apply_failure_or_pass(
                venue=venue,
                outcome="failed",
                status=status,
                failing_tests=gate.failing_tests,
                update_issues=update_issues,
            )
        except SmokeConfigError as exc:
            print(redact(str(exc)), file=sys.stderr)
        return 1
    return _record_venue_failure(
        venue=venue,
        status=status,
        failing_tests=gate.failing_tests,
        update_issues=update_issues,
    )


def _record_venue_failure(
    *,
    venue: str,
    status: str,
    failing_tests: Sequence[str],
    update_issues: bool,
) -> int:
    """Exit 0 after the venue issue is opened or commented. Exit 1 otherwise."""
    if not update_issues:
        print(
            f"{venue}: live smoke failed and the venue issue was not updated",
            file=sys.stderr,
        )
        return 1
    try:
        _apply_failure_or_pass(
            venue=venue,
            outcome="failed",
            status=status,
            failing_tests=failing_tests,
            update_issues=True,
        )
    except SmokeConfigError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    return 0


def cmd_report(summary_path: str, junit_path: str, update_issues: bool) -> int:
    venue = os.environ.get("VENUE", "").strip()
    if not _VENUE_NAME.fullmatch(venue):
        print("VENUE is not set to a venue name", file=sys.stderr)
        return 1
    raw_exit = os.environ.get("PYTEST_EXIT", "").strip()
    exit_known = False
    pytest_exit = 1
    if raw_exit:
        try:
            pytest_exit = int(raw_exit)
            exit_known = True
        except ValueError:
            exit_known = False
    junit = Path(junit_path) if junit_path else None
    try:
        result = result_from_pytest(junit, pytest_exit, exit_known=exit_known)
    except CollectionFailed as exc:
        message = _clip_text(redact(str(exc)))
        result = JunitResult(0, 1, 0, (message,), 0, (message,))
    summary = summary_for_result(venue, result)
    _write(summary_path, summary)
    # A Predict.fun run with mainnet key-skips is not a pass and not a failure.
    # Leave any open issue as-is: testnet did not prove mainnet.
    if predict_fun_mainnet_key_skipped(venue, result):
        print("::warning title=nightly-smoke::mainnet skipped: no key")
        outcome = "mainnet_no_key"
        status = "mainnet skipped: no key"
    else:
        if result.outcome == "no_key":
            print("::warning title=nightly-smoke::skipped: no key")
        outcome = result.outcome
        status = status_line(result)
    if outcome != "failed":
        try:
            _apply_failure_or_pass(
                venue=venue,
                outcome=outcome,
                status=status,
                failing_tests=result.failing_tests,
                update_issues=update_issues,
            )
        except SmokeConfigError as exc:
            print(redact(str(exc)), file=sys.stderr)
            return 1
        return 0
    return _record_venue_failure(
        venue=venue,
        status=status_line(result),
        failing_tests=result.failing_tests,
        update_issues=update_issues,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly live-smoke workflow helper")
    sub = parser.add_subparsers(dest="command", required=True)

    select = sub.add_parser("select", help="Filter the venue list from stdin")
    select.add_argument("--github-output", required=True)

    gate = sub.add_parser("gate", help="Skip when tests or a required key are missing")
    gate.add_argument("--summary", required=True)
    gate.add_argument("--github-output", required=True)
    gate.add_argument("--update-issues", action="store_true")

    report = sub.add_parser("report", help="Write the summary and update the venue issue")
    report.add_argument("--summary", required=True)
    report.add_argument("--junit", default="")
    report.add_argument("--update-issues", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "select":
        return cmd_select(args.github_output)
    if args.command == "gate":
        return cmd_gate(args.summary, args.github_output, args.update_issues)
    return cmd_report(args.summary, args.junit, args.update_issues)


if __name__ == "__main__":
    sys.exit(main())
