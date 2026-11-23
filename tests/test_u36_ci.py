import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_matrix_and_commands():
    assert CI_PATH.exists(), f"Missing {CI_PATH}"
    txt = CI_PATH.read_text(encoding="utf-8")
    for ver in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert re.search(rf'["\']?{re.escape(ver)}["\']?', txt), f"Matrix missing Python {ver}"
    assert ".[dev]" in txt
    assert "pytest" in txt
    assert "bench/run.py --require all" in txt
    assert "MESA_LORE_DB" in txt


def test_python_310_to_314_syntax_and_stdlib_compat():
    py_files = sorted(list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")))
    assert py_files
    for p in py_files:
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(p))
        compile(tree, str(p), "exec")
        for node in ast.walk(tree):
            # Reject Python 3.11+ top-level imports & datetime.UTC, 3.12+ TypeAlias & PEP 701 f-strings
            if isinstance(node, ast.Attribute) and node.attr == "UTC":
                raise AssertionError(f"{p}:{node.lineno}: datetime.UTC requires >=3.11; use timezone.utc")
            if hasattr(ast, "TypeAlias") and isinstance(node, ast.TypeAlias):
                raise AssertionError(f"{p}:{node.lineno}: PEP 695 `type` statement requires >=3.12")
            if isinstance(node, ast.JoinedStr):
                seg = ast.get_source_segment(src, node) or ""
                outer_q = next((q for q in ('"""', "'''", '"', "'") if seg.startswith(("f" + q, "rf" + q, "fr" + q))), None)
                for val in node.values:
                    if isinstance(val, ast.FormattedValue):
                        eseg = ast.get_source_segment(src, val) or ""
                        assert "\\" not in eseg, f"{p}:{node.lineno}: backslash in f-string expr requires >=3.12: {eseg}"
                        if outer_q and len(outer_q) == 1:
                            assert outer_q not in eseg, f"{p}:{node.lineno}: nested quote {outer_q} in f-string requires >=3.12: {eseg}"
