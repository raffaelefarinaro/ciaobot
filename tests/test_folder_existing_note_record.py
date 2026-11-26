"""#3626: the ordinary-folder existing-note limitation is recorded consistently."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "experiments" / "ciao-3626-folder-probe"


def _upkeep_row() -> str:
    rows = [
        line
        for line in (ROOT / "docs" / "UPKEEP.md").read_text(encoding="utf-8").splitlines()
        if "#3626" in line and line.startswith("|")
    ]
    assert len(rows) == 1
    return rows[0]


def test_probe_and_matrix_are_in_the_repo() -> None:
    assert (PROBE / "README.md").is_file()
    assert (PROBE / "CAPABILITY_MATRIX.md").is_file()


def test_upkeep_row_records_decision_and_unverified_scope() -> None:
    row = _upkeep_row()
    assert "2026-11-26" in row
    assert "experiments/ciao-3626-folder-probe" in row
    assert "DISABLED" in row
    for unverified in ("cloud", "ETag", "crash", "other platforms"):
        assert unverified in row
    assert "acceptance" in row and "release" in row


def test_architecture_and_api_docs_state_the_same_limitation() -> None:
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    api = (ROOT / "PWA_API.md").read_text(encoding="utf-8")
    assert "experiments/ciao-3626-folder-probe" in arch
    assert "refused until #3626" not in arch
    assert "experiments/ciao-3626-folder-probe" in api
