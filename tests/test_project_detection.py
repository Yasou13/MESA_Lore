import subprocess

from mesa_lore.store import get_project


def test_empty_git_dir_is_not_a_repo(tmp_path):
    (tmp_path / "parent" / ".git").mkdir(parents=True)
    cwd = tmp_path / "parent" / "app"
    cwd.mkdir()
    assert get_project(str(cwd)) == "app"


def test_empty_git_dir_inside_repo_uses_real_repo(tmp_path):
    repo = tmp_path / "real-repo"
    (repo / "pkg" / ".git").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert get_project(str(repo / "pkg")) == "real-repo"
