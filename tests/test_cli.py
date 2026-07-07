"""CLI smoke test — the single console-script entry point imports its whole command
tree and renders help (catches import-time breakage in legumista_cli and everything it
pulls in)."""
import subprocess
import sys


def test_cli_help_launches():
    proc = subprocess.run(
        [sys.executable, "-m", "legumista_cli", "--help"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Usage" in proc.stdout or "usage" in proc.stdout
