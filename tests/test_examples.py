"""Tests for examples to ensure they are valid Python code and can be imported."""

import importlib.util
import sys
from pathlib import Path

import pytest


def load_example(name: str):
    """Load an example file as a module."""
    examples_dir = Path(__file__).parent.parent / "examples"
    example_path = examples_dir / f"{name}.py"

    if not example_path.exists():
        pytest.skip(f"Example {name} not found at {example_path}")

    spec = importlib.util.spec_from_file_location(f"example_{name}", example_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"Could not load example {name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[f"example_{name}"] = module
    spec.loader.exec_module(module)
    return module


def test_compare_venues_imports():
    """Test that compare_venues.py can be imported without errors."""
    module = load_example("compare_venues")
    assert hasattr(module, "main")
    assert callable(module.main)


def test_price_history_imports():
    """Test that price_history.py can be imported without errors."""
    module = load_example("price_history")
    assert hasattr(module, "main")
    assert callable(module.main)


def test_export_to_pandas_imports():
    """Test that export_to_pandas.py can be imported without errors."""
    module = load_example("export_to_pandas")
    assert hasattr(module, "main")
    assert callable(module.main)


@pytest.mark.live
@pytest.mark.asyncio
async def test_compare_venues_runs_live(capsys):
    """Test that compare_venues.py executes and produces output against live APIs."""
    module = load_example("compare_venues")

    try:
        await module.main()
    except Exception as e:
        # Allow network-related failures
        if "timeout" in str(e).lower() or "connection" in str(e).lower():
            pytest.skip(f"Network issue during live test: {e}")
        # Only skip on documented geo-restriction errors (403 with geo message)
        if "403" in str(e) and ("geo" in str(e).lower() or "blocked" in str(e).lower()):
            pytest.skip(f"Geo-restriction: {e}")
        # Don't mask other errors - they should fail the test
        raise

    # Verify output was produced
    captured = capsys.readouterr()
    assert "Comparing Bitcoin Markets" in captured.out or len(captured.out) > 0, (
        "Example should produce output"
    )


@pytest.mark.live
@pytest.mark.asyncio
async def test_price_history_runs_live(capsys):
    """Test that price_history.py executes and produces output against live APIs."""
    module = load_example("price_history")

    try:
        await module.main()
    except Exception as e:
        if "timeout" in str(e).lower() or "connection" in str(e).lower():
            pytest.skip(f"Network issue during live test: {e}")
        if "403" in str(e) and ("geo" in str(e).lower() or "blocked" in str(e).lower()):
            pytest.skip(f"Geo-restriction: {e}")
        raise

    # Verify output was produced
    captured = capsys.readouterr()
    assert "Price History" in captured.out or len(captured.out) > 0, "Example should produce output"


@pytest.mark.live
@pytest.mark.asyncio
async def test_export_to_pandas_runs_live(capsys):
    """Test that export_to_pandas.py executes and produces output against live APIs."""
    module = load_example("export_to_pandas")

    try:
        await module.main()
    except Exception as e:
        # pandas may not be installed, which is fine for this optional feature
        if "pandas" in str(e).lower() or "not installed" in str(e).lower():
            pytest.skip("pandas not installed (optional dependency)")
        if "timeout" in str(e).lower() or "connection" in str(e).lower():
            pytest.skip(f"Network issue during live test: {e}")
        if "403" in str(e) and ("geo" in str(e).lower() or "blocked" in str(e).lower()):
            pytest.skip(f"Geo-restriction: {e}")
        raise

    # Verify output was produced
    captured = capsys.readouterr()
    assert (
        "Export to Pandas" in captured.out or "DataFrame" in captured.out or len(captured.out) > 0
    ), "Example should produce output"
