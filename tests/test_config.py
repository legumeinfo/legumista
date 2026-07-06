"""Config-resolution tests.

`config` snapshots its values at import time (WORKSPACE / legumista.yml are read
once), so each scenario runs in a fresh subprocess with a controlled environment
rather than relying on import order within this process.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "lupinus"

_PROBE = (
    "import config, json;"
    "print(json.dumps({"
    "'subject': config.subject(),"
    "'lexical_strong': config.lexical_strong(),"
    "'contact_email': config.contact_email(),"
    "}))"
)


def _probe(env_overrides, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # Ensure no ambient project pointer leaks into the neutral-defaults case.
    env.pop("LEGUMISTA_HOME", None)
    env.pop("LEGUMISTA_PROJECT_FILE", None)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    return json.loads(proc.stdout)


def test_neutral_defaults(tmp_path):
    """With no project in scope, config falls back to the topic-free skeleton."""
    out = _probe({}, cwd=tmp_path)
    assert out["subject"] == "the research subject"
    assert out["lexical_strong"] == []
    assert out["contact_email"] == "you@example.org"


def test_example_project_resolves():
    """LEGUMISTA_HOME pointed at the bundled example yields the Lupinus values."""
    out = _probe({"LEGUMISTA_HOME": str(EXAMPLE)}, cwd=REPO_ROOT)
    assert "Lupinus" in out["subject"]
    assert out["lexical_strong"]  # non-empty
