from pathlib import Path

from mesa_lore.cli import get_queue_path
from mesa_lore.store import get_db_path


def test_pytest_has_private_home_database_and_queue(tmp_path):
    isolated = tmp_path / "isolated"
    assert Path.home() == isolated / "home"
    assert get_db_path() == isolated / "lore.db"
    assert get_queue_path() == isolated / "queue.txt"
