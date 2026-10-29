"""Unit tests for the nightly live-smoke workflow helper."""

import json
import os
import subprocess
import sys
from pathlib import Path

import nightly_smoke
import pytest
from nightly_smoke import (
    CollectionFailed,
    Gate,
    GhIssueStore,
    IssueStore,
    JunitResult,
    SmokeConfigError,
    decide_gate,
    failure_issue_body,
    filter_venues,
    kalshi_smoke_api_base,
    live_test_count,
    main,
    missing_secret_names,
    parse_junit,
    parse_venues,
    pass_issue_body,
    redact,
    result_from_pytest,
    status_line,
    summary_for_result,
    sync_issue,
)

from prediction_markets_api.adapters.kalshi import KalshiClient

_DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
_PRODUCTION = "https://api.elections.kalshi.com/trade-api/v2"

_VENUES = """
[
  {"venue": "polymarket", "required_secrets": "", "api_base": "", "demo_only": false},
  {"venue": "kalshi", "required_secrets": "", "api_base": "https://external-api.demo.kalshi.co/trade-api/v2", "demo_only": true},
  {"venue": "predict_fun", "required_secrets": "PREDICTFUN_API_KEY", "api_base": "", "demo_only": false}
]
"""


class MemoryIssues(IssueStore):
    def __init__(self) -> None:
        self.open: list[int] = []
        self.created: list[str] = []
        self.comments: list[tuple[int, str]] = []
        self.closed: list[tuple[int, str]] = []

    def list_open(self, venue: str) -> list[int]:
        return list(self.open)

    def create(self, venue: str, body: str) -> None:
        self.created.append(body)
        self.open.append(100 + len(self.created))

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close(self, number: int, body: str) -> None:
        self.closed.append((number, body))


def _gate(**overrides: object) -> Gate:
    params: dict[str, object] = {
        "venue": "predict_fun",
        "test_file": Path("tests/test_predict_fun_live.py"),
        "required_secrets": ("PREDICTFUN_API_KEY",),
        "environ": {},
        "demo_only": False,
        "api_base": "",
        "file_exists": True,
        "collected": 2,
        "collection_error": None,
    }
    params.update(overrides)
    return decide_gate(**params)  # type: ignore[arg-type]


def test_demo_base_accepts_documented_hosts() -> None:
    assert kalshi_smoke_api_base({"KALSHI_API_BASE": _DEMO + "/"}) == _DEMO
    other = "https://demo-api.kalshi.co/trade-api/v2"
    assert kalshi_smoke_api_base({"KALSHI_API_BASE": other}) == other
    assert kalshi_smoke_api_base({}) is None
    assert kalshi_smoke_api_base({"KALSHI_API_BASE": "  "}) is None


@pytest.mark.parametrize(
    "url",
    [
        _PRODUCTION,
        "https://external-api.kalshi.com/trade-api/v2",
        "http://external-api.demo.kalshi.co/trade-api/v2",
        "https://user:secret-token@external-api.demo.kalshi.co/trade-api/v2",
        "https://example.com/trade-api/v2",
    ],
)
def test_demo_base_rejects_non_demo_urls(url: str) -> None:
    with pytest.raises(SmokeConfigError) as exc:
        kalshi_smoke_api_base({"KALSHI_API_BASE": url})
    assert url not in str(exc.value)
    assert "secret-token" not in str(exc.value)


def test_non_live_test_keeps_production_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_API_BASE", _DEMO)
    assert KalshiClient.API_BASE == _PRODUCTION


def test_redact_replaces_secret_values_only() -> None:
    secret = "predictfun-key-value"
    text = f"failed with {secret} beside /usr/bin"
    redacted = redact(text, {"PREDICTFUN_API_KEY": secret, "PATH": "/usr/bin"})
    assert secret not in redacted
    assert "***" in redacted
    assert "/usr/bin" in redacted


def test_missing_secret_names_treat_whitespace_as_missing() -> None:
    assert missing_secret_names(["PREDICTFUN_API_KEY"], {}) == ["PREDICTFUN_API_KEY"]
    assert missing_secret_names(["PREDICTFUN_API_KEY"], {"PREDICTFUN_API_KEY": "  "}) == [
        "PREDICTFUN_API_KEY"
    ]
    assert missing_secret_names(["PREDICTFUN_API_KEY"], {"PREDICTFUN_API_KEY": "set"}) == []
    assert missing_secret_names([], {}) == []


def test_venue_list_filters_and_rejects_unknown() -> None:
    venues = parse_venues(_VENUES)
    assert [venue.venue for venue in filter_venues(venues, "")] == [
        "polymarket",
        "kalshi",
        "predict_fun",
    ]
    chosen = filter_venues(venues, "predict_fun, kalshi")
    assert [venue.venue for venue in chosen] == ["kalshi", "predict_fun"]
    assert chosen[0].demo_only is True
    assert chosen[0].api_base == _DEMO
    with pytest.raises(SmokeConfigError, match="Unknown venue"):
        filter_venues(venues, "nope")


def test_production_kalshi_host_is_rejected_in_the_venue_list() -> None:
    payload = """
    [{"venue": "kalshi", "required_secrets": "", "api_base":
      "https://api.elections.kalshi.com/trade-api/v2", "demo_only": true}]
    """
    with pytest.raises(SmokeConfigError) as exc:
        parse_venues(payload)
    assert "elections.kalshi" not in str(exc.value)


def test_missing_test_file_is_not_a_missing_key() -> None:
    gate = _gate(file_exists=False, collected=None)
    assert gate.run is False
    assert gate.exit_code == 0
    assert gate.outcome == "no_tests"
    assert "skipped: no live tests" in gate.summary
    assert "skipped: no key" not in gate.summary


def test_zero_collected_wins_over_a_missing_key() -> None:
    gate = _gate(collected=0)
    assert gate.outcome == "no_tests"
    assert gate.exit_code == 0
    assert "skipped: no live tests" in gate.summary
    assert "skipped: no key" not in gate.summary


def test_missing_key_is_explicit_and_does_not_run() -> None:
    gate = _gate()
    assert gate.run is False
    assert gate.exit_code == 0
    assert gate.outcome == "no_key"
    assert gate.summary.splitlines()[2] == "skipped: no key"
    assert "PREDICTFUN_API_KEY" in gate.summary
    assert "not tested" in gate.summary
    ready = _gate(environ={"PREDICTFUN_API_KEY": "present"})
    assert ready.run is True
    assert ready.summary == ""


def test_kalshi_misconfiguration_fails_before_calling_production() -> None:
    gate = _gate(
        venue="kalshi",
        test_file=Path("tests/test_kalshi_live.py"),
        required_secrets=(),
        demo_only=True,
        api_base=_PRODUCTION,
        collected=8,
    )
    assert gate.run is False
    assert gate.exit_code == 1
    assert gate.outcome == "failed"
    assert "elections.kalshi" not in gate.summary
    assert gate.failing_tests == ("configuration: kalshi",)


def test_junit_key_skip_is_not_a_pass(tmp_path: Path) -> None:
    xml = """<?xml version="1.0" ?>
    <testsuite>
      <testcase classname="tests.test_predict_fun_live" name="test_list_and_get_market_live"
                file="tests/test_predict_fun_live.py">
        <skipped message="PREDICTFUN_API_KEY not set; Predict.fun mainnet reads require it"/>
      </testcase>
    </testsuite>
    """
    junit = tmp_path / "junit.xml"
    junit.write_text(xml, encoding="utf-8")
    result = parse_junit(junit)
    assert result.outcome == "no_key"
    assert status_line(result) == "skipped: no key"
    summary = summary_for_result("predict_fun", result)
    assert "\nskipped: no key\n" in f"\n{summary}"
    assert "passed:" not in summary


def test_junit_failure_names_and_issue_reuse(tmp_path: Path) -> None:
    xml = """<?xml version="1.0" ?>
    <testsuites>
      <testsuite>
        <testcase file="tests/test_kalshi_live.py" name="test_list_markets_live">
          <failure message="assert 0"/>
        </testcase>
        <testcase file="tests/test_kalshi_live.py" name="test_get_market_live"/>
      </testsuite>
    </testsuites>
    """
    junit = tmp_path / "junit.xml"
    junit.write_text(xml, encoding="utf-8")
    result = result_from_pytest(junit, 1)
    assert result.outcome == "failed"
    assert result.failing_tests == ("tests/test_kalshi_live.py::test_list_markets_live",)
    body = failure_issue_body(
        venue="kalshi",
        when="2026-10-29 00:00 UTC",
        run_url="https://example.test/run/9",
        status=status_line(result),
        failing_tests=result.failing_tests,
    )
    store = MemoryIssues()
    sync_issue(store, venue="kalshi", outcome="failed", failure_body=body, pass_body="pass")
    sync_issue(store, venue="kalshi", outcome="failed", failure_body=body, pass_body="pass")
    assert len(store.created) == 1
    assert "test_list_markets_live" in store.created[0]
    assert "https://example.test/run/9" in store.created[0]
    assert "2026-10-29 00:00 UTC" in store.created[0]
    assert len(store.comments) == 1
    assert store.closed == []

    passed = pass_issue_body(
        venue="kalshi",
        when="2026-10-30 00:00 UTC",
        run_url="https://example.test/run/10",
        status="passed: 8 passed, 0 failed, 0 skipped",
    )
    green = JunitResult(8, 0, 0, (), 0)
    assert green.outcome == "passed"
    assert "skipped: no key" not in status_line(green)
    sync_issue(store, venue="kalshi", outcome="passed", failure_body=body, pass_body=passed)
    assert store.closed == [(101, passed)]
    assert "Closing this issue." in passed


def test_no_key_does_not_open_or_close_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    nightly_smoke._apply_failure_or_pass(
        venue="predict_fun",
        outcome="no_key",
        status="skipped: no key",
        failing_tests=(),
        update_issues=True,
    )


def test_select_command_writes_one_venue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("SELECTED_VENUES", "polymarket, predict_fun")
    monkeypatch.setattr(sys, "stdin", _Stdin(_VENUES))
    assert main(["select", "--github-output", str(output)]) == 0
    text = output.read_text(encoding="utf-8")
    payload = text.split("VENUES_END\n", 1)[1].rsplit("\nVENUES_END", 1)[0]
    chosen = json.loads(payload)
    assert [item["venue"] for item in chosen] == ["polymarket", "predict_fun"]


def test_gate_command_missing_predict_fun_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("VENUE", "predict_fun")
    monkeypatch.setenv("REQUIRED_SECRETS", "PREDICTFUN_API_KEY")
    monkeypatch.setenv("DEMO_ONLY", "false")
    monkeypatch.delenv("PREDICTFUN_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_BASE", raising=False)
    assert (
        main(
            [
                "gate",
                "--summary",
                str(summary),
                "--github-output",
                str(output),
            ]
        )
        == 0
    )
    text = summary.read_text(encoding="utf-8")
    assert "skipped: no live tests" in text
    assert "skipped: no key" not in text
    assert "run=false" in output.read_text(encoding="utf-8")


def test_gate_command_rejects_production_kalshi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("VENUE", "kalshi")
    monkeypatch.setenv("REQUIRED_SECRETS", "")
    monkeypatch.setenv("DEMO_ONLY", "true")
    monkeypatch.setenv("KALSHI_API_BASE", _PRODUCTION)
    assert main(["gate", "--summary", str(summary), "--github-output", str(output)]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "failed: smoke configuration" in text
    assert "elections.kalshi" not in text
    assert "run=false" in output.read_text(encoding="utf-8")


def test_gate_command_missing_key_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("VENUE", "polymarket")
    monkeypatch.setenv("REQUIRED_SECRETS", "PREDICTFUN_API_KEY")
    monkeypatch.setenv("DEMO_ONLY", "false")
    monkeypatch.setenv("PREDICTFUN_API_KEY", "   ")
    assert main(["gate", "--summary", str(summary), "--github-output", str(output)]) == 0
    assert summary.read_text(encoding="utf-8").splitlines()[2] == "skipped: no key"
    assert "run=false" in output.read_text(encoding="utf-8")
    assert "skipped: no key" in capsys.readouterr().out


def test_report_command_fails_without_calling_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" ?>
        <testsuite>
          <testcase file="tests/test_pascal_live.py" name="test_list_and_get_market_live">
            <failure message="boom"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("VENUE", "pascal")
    monkeypatch.setenv("PYTEST_EXIT", "1")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main(["report", "--summary", str(summary), "--junit", str(junit)]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "test_list_and_get_market_live" in text
    assert text.splitlines()[2].startswith("failed:")


def test_gh_create_does_not_put_secrets_on_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "super-secret-predictfun-value"
    token = "gh-token-value-should-stay-in-env"
    monkeypatch.setenv("PREDICTFUN_API_KEY", secret)
    monkeypatch.setenv("GH_TOKEN", token)
    seen: list[list[str]] = []
    bodies: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is not True
        seen.append(list(args))
        for index, arg in enumerate(args):
            if arg == "--body-file":
                bodies.append(Path(args[index + 1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(args, 0, stdout="{}\n", stderr="")

    monkeypatch.setattr(nightly_smoke.subprocess, "run", fake_run)
    store = GhIssueStore("Gaserd/prediction-markets-api")
    store.create("predict_fun", "body does not include the key")
    flat = "\n".join(" ".join(args) for args in seen)
    assert secret not in flat
    assert token not in flat
    assert secret not in "\n".join(bodies)
    assert "nightly-smoke" in flat
    assert "venue:predict_fun" in flat
    assert "--repo" in flat
    assert "Gaserd/prediction-markets-api" in flat


def test_live_test_count_for_kalshi_and_empty_file(tmp_path: Path) -> None:
    assert live_test_count(Path("tests/test_kalshi_live.py")) == 8
    empty = tmp_path / "test_empty_live.py"
    empty.write_text("def test_recorded():\n    assert True\n", encoding="utf-8")
    assert live_test_count(empty) == 0


def test_collection_error_redacts_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "collect-time-secret-value"
    monkeypatch.setenv("NIGHTLY_TEST_SECRET", secret)
    target = tmp_path / "test_boom_live.py"
    target.write_text(
        "import os\nraise RuntimeError(os.environ['NIGHTLY_TEST_SECRET'])\n",
        encoding="utf-8",
    )
    with pytest.raises(CollectionFailed) as exc:
        live_test_count(target)
    assert secret not in exc.value.detail
    assert "***" in exc.value.detail


def test_live_canary_follows_demo_base() -> None:
    env = os.environ.copy()
    env["KALSHI_API_BASE"] = _DEMO
    env.pop("PYTEST_ADDOPTS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "live",
            "tests/test_kalshi_live.py::test_api_base_follows_demo_smoke_setting",
            "--override-ini",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_workflow_venue_list_and_boundaries() -> None:
    text = Path(".github/workflows/nightly-smoke.yml").read_text(encoding="utf-8")
    assert "pull_request" not in text
    assert "\n  push:" not in text
    assert "fail-fast: false" in text
    assert "issues: write" in text
    assert "contents: read" in text
    assert 'cron: "0 0 * * *"' in text
    assert 'python-version: "3.13"' in text
    match_at = text.index("<<'JSON'")
    body = text[match_at:].split("\n", 1)[1]
    raw, _, _ = body.partition("\n          JSON")
    venues = parse_venues(raw)
    names = [venue.venue for venue in venues]
    assert names == [
        "polymarket",
        "kalshi",
        "limitless",
        "hyperliquid",
        "pascal",
        "predict_fun",
    ]
    kalshi = next(venue for venue in venues if venue.venue == "kalshi")
    assert kalshi.demo_only is True
    assert kalshi.api_base == _DEMO
    assert kalshi.required_secrets == ()
    predict_fun = next(venue for venue in venues if venue.venue == "predict_fun")
    assert predict_fun.required_secrets == ("PREDICTFUN_API_KEY",)
    assert "api.elections.kalshi.com" not in text
    assert "external-api.kalshi.com" not in text


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
