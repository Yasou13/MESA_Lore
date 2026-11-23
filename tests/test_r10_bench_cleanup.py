import os, subprocess, sys
from pathlib import Path


def test_bench_leaves_no_temp_folders(tmp_path):
    work = tmp_path / "bench-tmp"
    work.mkdir()
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, str(root / "bench" / "run.py")], cwd=root, capture_output=True, text=True,
                          env={**os.environ, "TMPDIR": str(work), "TEMP": str(work), "TMP": str(work)}, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert list(work.iterdir()) == []
