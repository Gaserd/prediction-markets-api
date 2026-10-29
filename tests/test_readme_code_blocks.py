"""Tests that README code blocks are runnable."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


def extract_python_code_blocks(readme_path: Path) -> list[tuple[int, str]]:
    """Extract all Python code blocks from README with their line numbers."""
    content = readme_path.read_text()
    blocks = []

    # Find all ```python blocks
    pattern = r"```python\n(.*?)\n```"
    for match in re.finditer(pattern, content, re.DOTALL):
        code = match.group(1)
        line_num = content[: match.start()].count("\n") + 1
        blocks.append((line_num, code))

    return blocks


@pytest.mark.parametrize("block_index", list(range(10)))
def test_readme_code_block_syntax(block_index):
    """Test that each README code block has valid Python syntax."""
    readme_path = Path(__file__).parent.parent / "README.md"
    blocks = extract_python_code_blocks(readme_path)

    assert len(blocks) == 10, f"Expected 10 code blocks, found {len(blocks)}"

    line_num, code = blocks[block_index]

    try:
        compile(code, f"<README line {line_num}>", "exec")
    except SyntaxError as e:
        pytest.fail(f"Code block at line {line_num} has syntax error: {e}\n\nCode:\n{code}")


@pytest.mark.live
@pytest.mark.parametrize("block_index", list(range(10)))
def test_readme_code_block_executes(block_index, tmp_path):
    """Test that each README code block executes without errors (live test)."""
    readme_path = Path(__file__).parent.parent / "README.md"
    blocks = extract_python_code_blocks(readme_path)

    line_num, code = blocks[block_index]

    # Write code to temporary file
    script_path = tmp_path / f"block_{block_index}.py"
    script_path.write_text(code)

    # Execute with timeout
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        pytest.fail(
            f"Code block at line {line_num} failed to execute:\n"
            f"Exit code: {result.returncode}\n"
            f"Stdout: {result.stdout}\n"
            f"Stderr: {result.stderr}\n\n"
            f"Code:\n{code}"
        )


@pytest.mark.parametrize("block_index", list(range(10)))
def test_readme_code_block_executes_mocked(block_index, respx_mock, tmp_path):
    """Test that README code blocks execute with mocked HTTP (for CI)."""
    import json
    from pathlib import Path as P

    import httpx

    readme_path = Path(__file__).parent.parent / "README.md"
    blocks = extract_python_code_blocks(readme_path)

    line_num, code = blocks[block_index]

    # Mock HTTP responses for all venue APIs
    # Polymarket list_markets
    fixtures_dir = P(__file__).parent / "fixtures" / "polymarket"
    with open(fixtures_dir / "markets_response.json") as f:
        markets_data = json.load(f)

    respx_mock.get(re.compile(r".*gamma-api\.polymarket\.com/markets.*")).mock(
        return_value=httpx.Response(200, json=markets_data)
    )

    # Polymarket order book
    with open(fixtures_dir / "orderbook_response.json") as f:
        orderbook_data = json.load(f)

    respx_mock.get(re.compile(r".*clob\.polymarket\.com/book.*")).mock(
        return_value=httpx.Response(200, json=orderbook_data)
    )

    respx_mock.get(re.compile(r".*clob\.polymarket\.com/price.*")).mock(
        return_value=httpx.Response(200, json={"price": "0.65"})
    )

    # Polymarket trades
    with open(fixtures_dir / "trades_response.json") as f:
        trades_data = json.load(f)

    respx_mock.get(re.compile(r".*gamma-api\.polymarket\.com/.*trades.*")).mock(
        return_value=httpx.Response(200, json=trades_data)
    )

    # Kalshi list_markets
    kalshi_fixtures = P(__file__).parent / "fixtures" / "kalshi"
    with open(kalshi_fixtures / "markets_response.json") as f:
        kalshi_markets = json.load(f)

    respx_mock.get(re.compile(r".*api\.elections\.kalshi\.com/.*markets.*")).mock(
        return_value=httpx.Response(200, json=kalshi_markets)
    )

    # NotFoundError test - return empty markets
    respx_mock.get(re.compile(r".*nonexistent.*")).mock(
        return_value=httpx.Response(200, json={"markets": []})
    )

    # Write code to temporary file
    script_path = tmp_path / f"block_{block_index}.py"
    script_path.write_text(code)

    # Execute with timeout
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        pytest.fail(
            f"Code block at line {line_num} failed to execute (mocked):\n"
            f"Exit code: {result.returncode}\n"
            f"Stdout: {result.stdout}\n"
            f"Stderr: {result.stderr}\n\n"
            f"Code:\n{code}"
        )
