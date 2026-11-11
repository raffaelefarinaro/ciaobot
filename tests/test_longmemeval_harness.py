"""Tests for the LongMemEval-S harness script.

No network, no model, no real dataset: a three-session synthetic haystack in the
official shape exercises the retrieval path end to end, the JSONL rows, the @5
metrics, and the incomplete-case counting. The dataset-hash refusal is pinned
too. `scripts/memory-eval-longmemeval.py` has a hyphen in its name, so it is
loaded by path, the same way this repo tests its other operator scripts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "memory-eval-longmemeval.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("memory_eval_longmemeval", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _load()


def _turns(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "content": content} for role, content in pairs]


# Three sessions, two answerable questions of different types, one abstention.
# The multi-session question's gold list holds a second session the query cannot
# reach, which is what makes fractional@5 differ from strict@5 here.
SESSIONS = [
    _turns(("user", "My cat is called Miso."), ("assistant", "Noted.")),
    _turns(
        ("user", "I live in Zurich now, I moved here from Lisbon."),
        ("assistant", "Understood."),
    ),
    _turns(
        ("user", "I work as a data engineer at Helvetia Labs."),
        ("assistant", "Okay."),
    ),
]
SESSION_IDS = ["real-0", "real-1", "real-2"]
DATES = [
    "2023/05/20 (Sat) 09:00",
    "2023/05/21 (Sun) 09:00",
    "2023/05/22 (Mon) 09:00",
]


def _entry(
    question_id: str,
    question_type: str,
    question: str,
    answer: str,
    answer_session_ids: list[str],
) -> dict:
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": question,
        "answer": answer,
        "question_date": "2023/05/24 (Wed) 10:00",
        "haystack_session_ids": list(SESSION_IDS),
        "haystack_dates": list(DATES),
        "haystack_sessions": [list(session) for session in SESSIONS],
        "answer_session_ids": list(answer_session_ids),
    }


DATASET = [
    _entry(
        "q-single-user",
        "single-session-user",
        "What is my cat called?",
        "Miso",
        ["real-0"],
    ),
    _entry(
        "q-multi",
        "multi-session",
        "Which city was home before Zurich?",
        "Lisbon",
        ["real-1", "real-2"],
    ),
    _entry(
        "q-iguana_abs",
        "single-session-user",
        "What is the name of my pet iguana?",
        "No information about an iguana is available.",
        [],
    ),
]


def test_retrieval_rows_and_at_five_metrics(tmp_path: Path) -> None:
    out = tmp_path / "rows.jsonl"
    before = os.environ.get("CIAO_MEMORY_DIR")
    rows = HARNESS.run_retrieval_only(
        DATASET, out, dataset_sha256="abc123", temp_root=tmp_path
    )

    # The per-question override is restored, and the configured index path (the
    # test suite's stand-in for a live install) was never opened.
    assert os.environ.get("CIAO_MEMORY_DIR") == before
    assert not (tmp_path / ".ciao" / "vault-fts.db").exists()

    written = [
        json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["question_id"] for row in written] == [
        "q-single-user",
        "q-multi",
        "q-iguana_abs",
    ]
    assert written == rows
    for row in written:
        assert row["dataset_sha256"] == "abc123"
        assert row["mode"] == "retrieval"
        assert row["error"] is None
        assert row["judge"] is None

    # The cat question reaches its one gold session.
    assert written[0]["retrieved_session_ids"] == ["real-0"]
    assert written[0]["retrieval"] == {
        "gold": 1,
        "hits": 1,
        "strict": 1.0,
        "fractional": 1.0,
        "any": 1.0,
    }

    # Only one of this question's two gold sessions shares a word with the
    # query: strict fails, fractional is a half, any-hit succeeds.
    assert written[1]["retrieved_session_ids"] == ["real-1"]
    assert written[1]["retrieval"] == {
        "gold": 2,
        "hits": 1,
        "strict": 0.0,
        "fractional": 0.5,
        "any": 1.0,
    }

    # Abstention questions have no gold sessions and are not part of retrieval.
    assert written[2]["stratum"] == "abstention"
    assert written[2]["gold_session_ids"] == []
    assert written[2]["retrieval"] is None

    summary = HARNESS.summarize(written, qa=False)
    assert summary["n"] == 3
    assert summary["accuracy"] is None
    assert summary["retrieval"]["answerable"] == 2
    assert summary["retrieval"]["n"] == 2
    assert summary["retrieval"]["strict"] == pytest.approx(0.5)
    assert summary["retrieval"]["fractional"] == pytest.approx(0.75)
    assert summary["retrieval"]["any"] == pytest.approx(1.0)
    assert summary["incomplete"] == []


def _judged_row(
    question_id: str,
    stratum: str,
    *,
    verdict: bool | None,
    error: str | None = None,
) -> dict:
    return {
        "question_id": question_id,
        "question_type": stratum,
        "stratum": stratum,
        "gold_session_ids": ["real-0"],
        "retrieved_session_ids": ["real-0"],
        "retrieval": {
            "gold": 1,
            "hits": 1,
            "strict": 1.0,
            "fractional": 1.0,
            "any": 1.0,
        },
        "answer": "Miso",
        "judge": {"verdict": verdict, "reply": "yes" if verdict else "no"},
        "context_chars": 100,
        "context_tokens_est": 25,
        "answer_chars": 4,
        "latency_ms": {"answer": 10.0, "judge": 5.0},
        "error": error,
    }


def test_summary_counts_missing_and_failed_judges() -> None:
    rows = [
        _judged_row("q-good", "single-session-user", verdict=True),
        _judged_row("q-missing", "single-session-user", verdict=None),
        _judged_row(
            "q-failed",
            "knowledge-update",
            verdict=False,
            error="judge: command exited 1",
        ),
    ]

    summary = HARNESS.summarize(rows, qa=True)
    assert summary["n"] == 3
    assert summary["accuracy"] == {
        "n": 3,
        "correct": 1,
        "judged": 1,
        "fraction": pytest.approx(1 / 3),
    }
    assert summary["judge_missing"] == 2

    # Per-type fractions use n, so an incomplete case lowers the score instead
    # of vanishing from the denominator.
    assert summary["by_type"]["single-session-user"] == {
        "n": 2,
        "correct": 1,
        "judged": 1,
        "fraction": pytest.approx(0.5),
    }
    assert summary["by_type"]["knowledge-update"] == {
        "n": 1,
        "correct": 0,
        "judged": 0,
        "fraction": 0.0,
    }

    assert {item["question_id"] for item in summary["incomplete"]} == {
        "q-missing",
        "q-failed",
    }
    reasons = {item["question_id"]: item["reason"] for item in summary["incomplete"]}
    assert reasons["q-missing"] == "judge verdict missing"
    assert reasons["q-failed"] == "judge: command exited 1"

    table = HARNESS.format_summary(summary, mode="notes")
    assert "Incomplete: 2" in table
    assert "Judge missing or failed: 2" in table
    assert "q-missing" in table


def test_clean_env_drops_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("UNRELATED_SETTING", "kept")

    env = HARNESS.clean_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["UNRELATED_SETTING"] == "kept"


def test_dataset_hash_is_enforced(tmp_path: Path) -> None:
    dataset = tmp_path / "longmemeval_s_cleaned.json"
    dataset.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit, match="sha256 mismatch"):
        HARNESS.verify_dataset(dataset)

    actual = HARNESS.verify_dataset(dataset, allow_other=True)
    assert actual == hashlib.sha256(b"[]").hexdigest()
    assert actual != HARNESS.EXPECTED_SHA256

    with pytest.raises(SystemExit, match="Dataset not found"):
        HARNESS.verify_dataset(tmp_path / "missing.json", allow_other=True)


def test_stratified_sample_matches_published_sizes() -> None:
    entries = []
    counts = {
        "knowledge-update": 20,
        "temporal-reasoning": 20,
        "multi-session": 20,
        "single-session-user": 20,
        "single-session-assistant": 20,
        "single-session-preference": 20,
    }
    for question_type, count in counts.items():
        for index in range(count):
            entries.append(
                {
                    "question_id": f"{question_type}-{index:03d}",
                    "question_type": question_type,
                }
            )
    for index in range(30):
        entries.append(
            {
                "question_id": f"single-session-user-{index:03d}_abs",
                "question_type": "single-session-user",
            }
        )

    first = HARNESS.select_questions(entries, 60, HARNESS.DEFAULT_SEED)
    second = HARNESS.select_questions(entries, 60, HARNESS.DEFAULT_SEED)
    ids = [entry["question_id"] for entry in first]
    assert ids == [entry["question_id"] for entry in second]
    assert len(ids) == 60

    strata: dict[str, int] = {}
    for entry in first:
        stratum = HARNESS.question_stratum(entry)
        strata[stratum] = strata.get(stratum, 0) + 1
    assert strata == {
        "knowledge-update": 10,
        "temporal-reasoning": 12,
        "multi-session": 12,
        "single-session-user": 6,
        "single-session-assistant": 6,
        "single-session-preference": 6,
        "abstention": 8,
    }

    other = HARNESS.select_questions(entries, 60, HARNESS.DEFAULT_SEED + 1)
    assert [entry["question_id"] for entry in other] != ids


def test_qa_row_uses_the_answer_and_judge_prompts(tmp_path: Path) -> None:
    answer_cmd = (
        "grep -q 'Answer (step by step):' {prompt_file} && "
        "printf 'Miso' > {out_file}"
    )
    judge_cmd = (
        "grep -q 'Is the model response correct?' {prompt_file} && "
        "printf 'yes' > {out_file}"
    )

    row = HARNESS.run_question(
        DATASET[0],
        mode="notes",
        answer_cmd=answer_cmd,
        judge_cmd=judge_cmd,
        timeout=30,
        dataset_sha256="abc123",
        temp_root=tmp_path,
    )

    assert row["error"] is None
    assert row["mode"] == "notes"
    assert row["answer"] == "Miso"
    assert row["judge"]["verdict"] is True
    assert row["retrieval"]["strict"] == 1.0
    assert row["context_tokens_est"] > 0
    assert row["latency_ms"]["answer"] is not None
    assert row["latency_ms"]["judge"] is not None


def test_failed_answer_is_counted_and_not_judged(tmp_path: Path) -> None:
    row = HARNESS.run_question(
        DATASET[0],
        mode="notes",
        answer_cmd="exit 2",
        judge_cmd="printf 'yes' > {out_file}",
        timeout=30,
        temp_root=tmp_path,
    )

    assert row["error"].startswith("answer:")
    assert row["answer"] == ""
    assert row["judge"]["verdict"] is None
    assert row["judge"]["error"] == "skipped: answer failed"

    summary = HARNESS.summarize([row], qa=True)
    assert summary["judge_missing"] == 1
    assert summary["accuracy"]["judged"] == 0
    assert summary["incomplete"][0]["question_id"] == "q-single-user"


def test_cli_retrieval_only_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET), encoding="utf-8")
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps(["q-multi", "q-single-user"]), encoding="utf-8"
    )
    out = tmp_path / "out.jsonl"

    code = HARNESS.main(
        [
            "--dataset",
            str(dataset),
            "--allow-other-dataset",
            "--questions",
            str(questions),
            "--retrieval-only",
            "--out",
            str(out),
            "--questions-out",
            str(tmp_path / "selected.json"),
            "--temp-dir",
            str(tmp_path),
        ]
    )

    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [row["question_id"] for row in rows] == ["q-multi", "q-single-user"]
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert {row["dataset_sha256"] for row in rows} == {digest}
    assert json.loads((tmp_path / "selected.json").read_text()) == [
        "q-multi",
        "q-single-user",
    ]
    captured = capsys.readouterr()
    assert "Retrieval @5" in captured.out
    assert "Incomplete: 0" in captured.out
