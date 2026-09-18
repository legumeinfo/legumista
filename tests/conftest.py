"""Suite-wide isolation.

Two globals would otherwise leak between the tests and the developer's machine: the
catalog cache directory (a real one exists at ~/.cache/legumista once the server has run
once) and the webhook secret. A test that expects "no catalog" would quietly pass or fail
depending on whether the machine happened to have a cached one, which is exactly the kind
of failure that only shows up in CI.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_catalog_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGUMISTA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("LEGUMISTA_CATALOG_URL", raising=False)
    monkeypatch.delenv("LEGUMISTA_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("LEGUMISTA_CATALOG_POLL", "0")   # never start a poller in tests
