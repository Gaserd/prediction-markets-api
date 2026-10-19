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


@pytest.mark.parametrize("block_index", list(range(9)))
def test_readme_code_block_syntax(block_index):
    """Test that each README code block has valid Python syntax."""
    readme_path = Path(__file__).parent.parent / "README.md"
    blocks = extract_python_code_blocks(readme_path)

    assert len(blocks) == 9, f"Expected 9 code blocks, found {len(blocks)}"

    line_num, code = blocks[block_index]

    try:
        compile(code, f"<README line {line_num}>", "exec")
    except SyntaxError as e:
        pytest.fail(f"Code block at line {line_num} has syntax error: {e}\n\nCode:\n{code}")


@pytest.mark.live
@pytest.mark.parametrize("block_index", list(range(9)))
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
