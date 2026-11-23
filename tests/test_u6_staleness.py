import subprocess
from pathlib import Path
from mesa_lore.store import Store


def test_git_staleness_hint(tmp_path):
    # Setup temporary git repo
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    foo_file = repo / "foo.py"
    foo_file.write_text("print('version 1')")
    subprocess.run(["git", "add", "foo.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True)

    db_path = tmp_path / "lore.db"
    st = Store(db_path)

    # Save note linked to foo.py
    note_id = st.save_note(
        project="test_repo",
        text="foo.py implements version 1 of the feature.",
        kind="decision",
        files=["foo.py"],
        repo=str(repo)
    )

    # Fresh note: not stale
    n = st.get(note_id)
    assert n["stale_hint"] is False
    res = st.search("version 1", "test_repo")
    assert res["status"] == "ok"
    assert res["results"][0]["stale_hint"] is False

    # Commit unrelated change
    bar_file = repo / "bar.py"
    bar_file.write_text("print('unrelated')")
    subprocess.run(["git", "add", "bar.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Add bar"], cwd=repo, check=True)

    # Still not stale
    assert st.get(note_id)["stale_hint"] is False

    # Modify foo.py and commit
    foo_file.write_text("print('version 2')")
    subprocess.run(["git", "add", "foo.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "Update foo"], cwd=repo, check=True)

    # Now stale_hint must be True
    n_updated = st.get(note_id)
    assert n_updated["stale_hint"] is True

    # Search should also reflect stale_hint
    res2 = st.search("version 1", "test_repo")
    assert res2["status"] == "ok"
    assert res2["results"][0]["stale_hint"] is True

    st.close()


def test_no_git_no_staleness(tmp_path):
    db_path = tmp_path / "lore.db"
    st = Store(db_path)
    note_id = st.save_note("proj", "Regular note without git files", "fact")
    assert st.get(note_id)["stale_hint"] is False
    st.close()
