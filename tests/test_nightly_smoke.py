"""Unit tests for the nightly live-smoke workflow helper."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from prediction_markets_api.adapters.kalshi import KalshiClient

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

import nightly_smoke  # noqa: E402
from nightly_smoke import (  # noqa: E402
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


def test_kalshi_rejects_missing_demo_flag_or_base() -> None:
    """Kalshi stays on demo even if the workflow list says otherwise."""
    cases = [
        '{"venue": "kalshi", "required_secrets": "", "api_base": "", "demo_only": false}',
        '{"venue": "kalshi", "required_secrets": "", "api_base": "", "demo_only": true}',
        (
            '{"venue": "kalshi", "required_secrets": "", "api_base": "'
            + _DEMO
            + '", "demo_only": false}'
        ),
    ]
    for payload in cases:
        with pytest.raises(SmokeConfigError):
            parse_venues(f"[{payload}]")
    missing_flag = _gate(
        venue="kalshi",
        test_file=_ROOT / "tests" / "test_kalshi_live.py",
        required_secrets=(),
        demo_only=False,
        api_base="",
        collected=1,
    )
    assert missing_flag.config_error is True
    assert missing_flag.exit_code == 1
    assert missing_flag.run is False
    empty_base = _gate(
        venue="kalshi",
        test_file=_ROOT / "tests" / "test_kalshi_live.py",
        required_secrets=(),
        demo_only=True,
        api_base="",
        collected=1,
    )
    assert empty_base.config_error is True
    assert empty_base.exit_code == 1


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
    gate = _gate(venue="polymarket", test_file=Path("tests/test_polymarket_live.py"))
    assert gate.run is False
    assert gate.exit_code == 0
    assert gate.outcome == "no_key"
    assert gate.summary.splitlines()[2] == "skipped: no key"
    assert "PREDICTFUN_API_KEY" in gate.summary
    assert "not tested" in gate.summary
    ready = _gate(
        venue="polymarket",
        test_file=Path("tests/test_polymarket_live.py"),
        environ={"PREDICTFUN_API_KEY": "present"},
    )
    assert ready.run is True
    assert ready.summary == ""


def test_predict_fun_missing_key_still_runs_keyless_tests() -> None:
    gate = _gate()
    assert gate.run is True
    assert gate.exit_code == 0
    assert gate.outcome == "run"
    assert gate.summary == ""
    blocked = _gate(required_secrets=("PREDICTFUN_API_KEY", "OTHER_TOKEN"))
    assert blocked.run is False
    assert blocked.outcome == "no_key"
    assert blocked.summary.splitlines()[2] == "skipped: no key"
    assert "`OTHER_TOKEN`" in blocked.summary
    assert "`PREDICTFUN_API_KEY`" not in blocked.summary


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
    assert summary.splitlines()[2] == "mainnet skipped: no key"
    assert "passed:" not in summary
    other = summary_for_result("polymarket", result)
    assert other.splitlines()[2] == "skipped: no key"


def test_summary_clips_redacted_failure_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "predictfun-key-value-0123456789"
    monkeypatch.setenv("PREDICTFUN_API_KEY", secret)
    long_message = f"assert {secret} " + ("x" * 400)
    junit = tmp_path / "junit.xml"
    junit.write_text(
        f"""<?xml version="1.0" ?>
        <testsuite>
          <testcase file="tests/test_pascal_live.py" name="test_list_and_get_market_live">
            <failure message="{long_message}"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    result = parse_junit(junit)
    assert result.failure_details
    assert secret not in result.failure_details[0]
    assert len(result.failure_details[0]) <= 200
    assert result.failure_details[0].endswith("...")
    summary = summary_for_result("pascal", result)
    assert secret not in summary
    assert result.failure_details[0] in summary
    issue = failure_issue_body(
        venue="pascal",
        when="2026-10-29 00:00 UTC",
        run_url="https://example.test/run/1",
        status=status_line(result),
        failing_tests=result.failing_tests,
    )
    assert secret not in issue
    assert result.failure_details[0] not in issue


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
    assert "assert 0" in summary_for_result("kalshi", result)
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
    assert "assert 0" not in store.created[0]
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
    """Missing live file skips the job. Does not look at the checkout."""
    summary = tmp_path / "summary.md"
    output = tmp_path / "github_output.txt"
    missing = tmp_path / "test_predict_fun_live.py"
    monkeypatch.setenv("VENUE", "predict_fun")
    monkeypatch.setenv("REQUIRED_SECRETS", "PREDICTFUN_API_KEY")
    monkeypatch.setenv("DEMO_ONLY", "false")
    monkeypatch.delenv("PREDICTFUN_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_BASE", raising=False)
    monkeypatch.setattr(nightly_smoke, "test_file_for", lambda _venue: missing)

    def _unexpected(_path: Path) -> int:
        raise AssertionError("collection must not run when the live file is absent")

    monkeypatch.setattr(nightly_smoke, "live_test_count", _unexpected)
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
    assert "mainnet skipped: no key" not in text
    assert "run=false" in output.read_text(encoding="utf-8")


def test_gate_command_predict_fun_runs_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing Predict.fun key does not skip tests that need no key."""
    summary = tmp_path / "summary.md"
    output = tmp_path / "github_output.txt"
    live = tmp_path / "test_predict_fun_live.py"
    live.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    monkeypatch.setenv("VENUE", "predict_fun")
    monkeypatch.setenv("REQUIRED_SECRETS", "PREDICTFUN_API_KEY")
    monkeypatch.setenv("DEMO_ONLY", "false")
    monkeypatch.delenv("PREDICTFUN_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_BASE", raising=False)
    monkeypatch.setattr(nightly_smoke, "test_file_for", lambda _venue: live)
    monkeypatch.setattr(nightly_smoke, "live_test_count", lambda _path: 3)
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
    assert not summary.exists()
    assert output.read_text(encoding="utf-8") == "run=true\n"


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
    assert "boom" in text
    assert text.splitlines()[2].startswith("failed:")


def test_report_exits_0_after_the_issue_is_updated(
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
    monkeypatch.setenv("GITHUB_REPOSITORY", "Gaserd/prediction-markets-api")
    monkeypatch.setenv("RUN_URL", "https://example.test/run/3")

    def _updated(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nightly_smoke, "_apply_failure_or_pass", _updated)
    assert (
        main(
            [
                "report",
                "--summary",
                str(summary),
                "--junit",
                str(junit),
                "--update-issues",
            ]
        )
        == 0
    )


def test_predict_fun_report_marks_mainnet_skipped_and_does_not_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version="1.0" ?>
        <testsuite>
          <testcase file="tests/test_predict_fun_live.py"
                    name="test_testnet_list_book_and_trades_live"/>
          <testcase file="tests/test_predict_fun_live.py" name="test_list_and_get_market_live">
            <skipped message="PREDICTFUN_API_KEY not set; Predict.fun mainnet reads require it"/>
          </testcase>
          <testcase file="tests/test_predict_fun_live.py"
                    name="test_order_book_price_and_trades_live">
            <skipped message="PREDICTFUN_API_KEY not set; Predict.fun mainnet reads require it"/>
          </testcase>
        </testsuite>
        """,
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("VENUE", "predict_fun")
    monkeypatch.setenv("PYTEST_EXIT", "0")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    seen: list[str] = []

    def _spy(**kwargs: object) -> None:
        seen.append(str(kwargs["outcome"]))

    monkeypatch.setattr(nightly_smoke, "_apply_failure_or_pass", _spy)
    assert (
        main(
            [
                "report",
                "--summary",
                str(summary),
                "--junit",
                str(junit),
                "--update-issues",
            ]
        )
        == 0
    )
    text = summary.read_text(encoding="utf-8")
    assert text.splitlines()[2] == "mainnet skipped: no key"
    assert "1 test(s) that do not need a key passed." in text
    assert "passed:" not in text
    assert seen == ["mainnet_no_key"]
    assert "mainnet skipped: no key" in capsys.readouterr().out
    store = MemoryIssues()
    store.open = [7]
    sync_issue(
        store,
        venue="predict_fun",
        outcome="mainnet_no_key",
        failure_body="failure",
        pass_body="pass",
    )
    assert store.closed == []
    assert store.created == []
    assert store.comments == []


def test_missing_junit_is_a_failed_run_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("VENUE", "limitless")
    monkeypatch.delenv("PYTEST_EXIT", raising=False)
    assert (
        main(
            [
                "report",
                "--summary",
                str(summary),
                "--junit",
                str(tmp_path / "missing.xml"),
            ]
        )
        == 1
    )
    text = summary.read_text(encoding="utf-8")
    assert "smoke-junit.xml is missing" in text
    assert "step timeout or cancellation" in text


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
    assert live_test_count(_ROOT / "tests" / "test_kalshi_live.py") >= 1
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
            str(_ROOT / "tests" / "test_kalshi_live.py")
            + "::test_api_base_follows_demo_smoke_setting",
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
        cwd=_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_workflow_venue_list_and_boundaries() -> None:
    text = (_ROOT / ".github" / "workflows" / "nightly-smoke.yml").read_text(encoding="utf-8")
    assert "pull_request" not in text
    assert "\n  push:" not in text
    assert "fail-fast: false" in text
    assert "group: nightly-smoke-${{ matrix.venue }}" in text
    assert "cancel-in-progress: false" in text
    assert "always() && steps.gate.outputs.run == 'true'" in text
    install = text.split("Install dependencies", 1)[1].split("- name:", 1)[0]
    assert "PREDICTFUN_API_KEY" not in install
    assert "KALSHI_API_SECRET" not in install
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
