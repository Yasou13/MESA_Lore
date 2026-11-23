from pathlib import Path

import pytest

from mesa_lore import store


@pytest.fixture(autouse=True)
def private_lore_environment(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated"
    home = isolated / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("MESA_LORE_DB", str(isolated / "lore.db"))
    monkeypatch.delenv("MESA_LORE_QUEUE", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_BRAIN_DIR", raising=False)


@pytest.fixture(autouse=True)
def checkout_paths_for_legacy_fixtures(tmp_path, monkeypatch, request):
    # Pre-R9 retrieval fixtures declare checkouts with synthetic paths. They test
    # retrieval with those scopes. The R9 attribution tests use actual Git repos.
    if request.node.path.name.startswith("test_r9_"):
        return
    resolve = store._repo_at

    def fixture_checkout(cwd):
        repo = resolve(cwd)
        if repo is not None or not isinstance(cwd, str) or not cwd:
            return repo
        root = Path(cwd).resolve()
        synthetic = cwd.startswith(("/repo/", "/path/to/", "/home/dev/", "/r/", "/work/", "/projects/",
                                    "/home/u/Masaüstü/", "/home/user/code/"))
        if root.is_relative_to(tmp_path) or synthetic or cwd in ("/tmp/app1", "/tmp/depo-sync", "/tmp/other-proj"):
            return root, root.name
        return None

    monkeypatch.setattr(store, "_repo_at", fixture_checkout)
