"""CLI smoke tests — no network, no model calls."""
import subprocess
import sys


def test_import_cli():
    """The console-script module imports and exposes `main`."""
    import legumista_cli

    assert hasattr(legumista_cli, "main")


def test_help_via_module_exits_zero():
    """`python -m legumista_cli --help` (same entry point as the `legumista`
    console script) exits 0 and prints usage."""
    proc = subprocess.run(
        [sys.executable, "-m", "legumista_cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Usage" in proc.stdout or "usage" in proc.stdout


def test_help_via_clirunner_exits_zero():
    """Drive the Typer app in-process with its test runner."""
    from typer.testing import CliRunner
    import legumista_cli

    app = getattr(legumista_cli, "app", None)
    if app is None:  # pragma: no cover - depends on CLI shape
        import pytest

        pytest.skip("legumista_cli exposes no `app` object")
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
