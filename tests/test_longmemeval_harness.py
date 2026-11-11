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
import shlex
import sys
import time
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


# ── raw ingest stays byte-identical ─────────────────────────────────────────

GOLDEN = ROOT / "tests" / "fixtures" / "longmemeval_raw_golden.jsonl"
#: The operator commands below are POSIX shell (`cat`, `printf`, `VAR=value cmd`).
posix_shell = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")


def _stable(row: dict) -> dict:
    """A row without its wall-clock fields, as the golden file stores it."""
    row = json.loads(json.dumps(row, ensure_ascii=False, sort_keys=True))
    row["latency_ms"] = sorted(row["latency_ms"])
    if isinstance(row.get("judge"), dict):
        row["judge"].pop("latency_ms", None)
    return row


@posix_shell
def test_raw_rows_match_the_pre_curated_golden(tmp_path: Path) -> None:
    """Raw ingest writes exactly what the harness wrote before curated ingest.

    `tests/fixtures/longmemeval_raw_golden.jsonl` was produced by the harness
    as committed before `--ingest` existed. The answer command echoes the whole
    prompt back, so every byte of every context rendering is pinned too.
    """
    answer = "cat {prompt_file} > {out_file}"
    judge = "printf yes > {out_file}"
    rows = [
        _stable(HARNESS.retrieve_for_question(entry, dataset_sha256="abc123", temp_root=tmp_path))
        for entry in DATASET
    ]
    for mode in ("snippets", "notes", "agentic"):
        rows.extend(
            _stable(
                HARNESS.run_question(
                    entry,
                    mode=mode,
                    answer_cmd=answer,
                    judge_cmd=judge,
                    timeout=30,
                    dataset_sha256="abc123",
                    temp_root=tmp_path,
                )
            )
            for entry in DATASET
        )
    golden = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    assert rows == golden


@posix_shell
def test_raw_cli_has_no_curated_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    code = HARNESS.main(
        [
            "--dataset", str(dataset), "--allow-other-dataset",
            "--answer-cmd", "printf Miso > {out_file}",
            "--judge-cmd", "printf yes > {out_file}",
            "--out", str(out), "--temp-dir", str(tmp_path),
        ]
    )
    assert code == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert all(
        not {"ingest", "proposals", "curation", "retrieved_note_paths"} & set(row) for row in rows
    )
    assert all(set(row["latency_ms"]) == {"index", "search", "answer", "judge"} for row in rows)
    assert "curation" not in HARNESS.summarize(rows, qa=True)
    captured = capsys.readouterr().out
    assert "Ingest:" not in captured
    assert "Curated ingest" not in captured


# ── curated ingest ──────────────────────────────────────────────────────────

FAKE_EXTRACTOR = ROOT / "tests" / "fixtures" / "longmemeval_fake_extractor.py"


def _extract_cmd(prompt_log: Path | None = None) -> str:
    """The fake extractor as an operator template; optionally keep every prompt."""
    keep = f"cat {{prompt_file}} >> {shlex.quote(str(prompt_log))}; " if prompt_log else ""
    return (
        f"{keep}PYTHONPATH={shlex.quote(str(ROOT))} {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(FAKE_EXTRACTOR))} {{prompt_file}} > {{out_file}}"
    )


@pytest.fixture
def live_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An operator shell pointed at a live install; curated ingest must not touch it."""
    live = tmp_path / "live-install"
    monkeypatch.setenv("CIAO_WORKSPACE", str(live))
    monkeypatch.setenv("CIAO_VAULT_ROOT", str(live / "memory-vault"))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(live / ".runtime"))
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(live / ".ciao"))
    return live


@posix_shell
def test_curated_ingest_answers_from_what_the_memory_pass_kept(
    tmp_path: Path, live_install: Path
) -> None:
    prompts = tmp_path / "extract-prompts.log"
    (tmp_path / "runs").mkdir()
    before ={key: value for key, value in os.environ.items() if key.startswith("CIAO_")}
    row = HARNESS.run_question(
        DATASET[1],
        mode="notes",
        answer_cmd="cat {prompt_file} > {out_file}",
        judge_cmd="printf yes > {out_file}",
        timeout=60,
        dataset_sha256="abc123",
        temp_root=tmp_path / "runs",
        ingest="curated",
        extract_cmd=_extract_cmd(prompts),
        proposals="accept",
    )

    # Isolation: the operator's environment is restored and the live install
    # it named was never created, let alone written.
    assert {key: value for key, value in os.environ.items() if key.startswith("CIAO_")} == before
    assert not live_install.exists()
    assert list((tmp_path / "runs").iterdir()) == []

    assert row["error"] is None, row
    assert row["ingest"] == "curated"
    assert row["proposals"] == "accept"
    curation = row["curation"]
    assert curation["haystack_sessions"] == curation["sessions"] == 3
    assert curation["passes_ok"] == 3
    assert curation["passes_failed"] == 0
    assert curation["engine_model_calls_blocked"] == 0
    # The Lisbon fact was queued by the pass and accepted into ciao:memory; the
    # cat went straight to ciao:profile; the job became a people note.
    assert curation["proposals"]["filed"] == 1
    assert curation["proposals"]["accepted"] == 1
    assert curation["proposals"]["pending_at_end"] == 0
    store = curation["store"]
    assert store["region_entries"] == {"memory": 1, "profile": 1}
    assert store["notes"] == curation["baseline_store"]["notes"] + 1
    assert curation["raw_chars"] > 0
    assert curation["evidence_retained"] is True
    assert curation["evidence_sessions_retained"] == ["real-1", "real-2"]
    assert curation["evidence_in_context"] is True
    assert curation["contradictions"] is None
    assert [item["session"] for item in curation["passes"]] == ["s00", "s01", "s02"]

    # The answer prompt (echoed back as the answer) holds the regions every chat
    # loads, and no raw transcript.
    assert "### Workspace memory (loaded in every chat):" in row["answer"]
    assert "I live in Zurich now, I moved here from Lisbon." in row["answer"]
    assert "My cat is called Miso." in row["answer"]
    assert "Data engineer" not in row["answer"]
    assert "### User" not in row["answer"]
    assert row["judge"]["verdict"] is True
    assert "curate" in row["latency_ms"]

    # The real pass prompt ran, once per session, dated at the session.
    log = prompts.read_text(encoding="utf-8")
    assert log.count("A conversation in this workspace just ended") == 3
    assert "today=2023-05-20" in log
    assert "today=2023-05-22" in log
    assert "Logs/Chats/" in log


@posix_shell
@pytest.mark.parametrize(
    ("proposals", "accepted", "dropped", "pending", "memory_entries", "retained"),
    [
        # real-1's only output is a proposal: dropped, nothing of it survives,
        # even though the engine's queue file (title, template, timestamped
        # heading) stays behind; left, its queued row is still stored.
        ("drop", 0, 1, 0, 0, ["real-2"]),
        ("leave", 0, 0, 1, 0, ["real-1", "real-2"]),
    ],
)
def test_curated_proposal_modes_are_explicit(
    tmp_path: Path,
    proposals: str,
    accepted: int,
    dropped: int,
    pending: int,
    memory_entries: int,
    retained: list[str],
) -> None:
    row = HARNESS.retrieve_for_question(
        DATASET[1],
        temp_root=tmp_path,
        ingest="curated",
        extract_cmd=_extract_cmd(),
        proposals=proposals,
        timeout=60,
    )
    assert row["error"] is None, row
    counts = row["curation"]["proposals"]
    assert counts["mode"] == proposals
    assert counts["filed"] == 1
    assert (counts["accepted"], counts["dropped"], counts["pending_at_end"]) == (
        accepted,
        dropped,
        pending,
    )
    assert row["curation"]["store"]["region_entries"]["memory"] == memory_entries
    assert row["curation"]["evidence_sessions_retained"] == retained
    assert "retrieved_note_paths" in row


@posix_shell
def test_abstention_has_no_evidence_to_retain(tmp_path: Path) -> None:
    """An `_abs` question is never scored for retained evidence, gold ids or not."""
    entry = {**DATASET[2], "answer_session_ids": ["real-0"]}
    row = HARNESS.retrieve_for_question(
        entry,
        temp_root=tmp_path,
        ingest="curated",
        extract_cmd=_extract_cmd(),
        proposals="accept",
        timeout=60,
    )
    assert row["error"] is None, row
    assert row["curation"]["evidence_retained"] is None
    assert row["curation"]["evidence_sessions_retained"] == []
    assert row["curation"]["evidence_in_context"] is None


@posix_shell
def test_timed_out_extractor_is_killed_with_its_children(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    started = time.perf_counter()
    row = HARNESS.retrieve_for_question(
        DATASET[0],
        temp_root=tmp_path,
        ingest="curated",
        extract_cmd=f"sleep 60 & echo $! > {shlex.quote(str(pidfile))}; wait",
        proposals="leave",
        timeout=1,
    )
    assert time.perf_counter() - started < 30
    assert "command timed out after 1s" in row["error"]
    child = int(pidfile.read_text().strip())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(child, 9)
        pytest.fail("the extractor's child outlived the timeout")


def test_curated_refuses_a_temp_dir_under_a_guide(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# a live workspace guide\n", encoding="utf-8")
    nested = tmp_path / "runs"
    nested.mkdir()
    with pytest.raises(ValueError, match="refuses a temp directory under"):
        HARNESS.refuse_guide_above(nested)
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET), encoding="utf-8")
    with pytest.raises(SystemExit, match="refuses a temp directory under"):
        HARNESS.main(
            [
                "--dataset", str(dataset), "--allow-other-dataset",
                "--ingest", "curated", "--extract-cmd", "true", "--proposals", "drop",
                "--out", str(tmp_path / "o.jsonl"), "--temp-dir", str(nested),
            ]
        )


@posix_shell
def test_failed_memory_pass_makes_the_question_incomplete(tmp_path: Path) -> None:
    row = HARNESS.run_question(
        DATASET[0],
        mode="notes",
        answer_cmd="printf never > {out_file}",
        judge_cmd="printf yes > {out_file}",
        timeout=60,
        temp_root=tmp_path,
        ingest="curated",
        extract_cmd="echo boom >&2; exit 3",
        proposals="leave",
    )
    assert row["error"].startswith("curate: 1 of 1 memory passes failed: command exited 3")
    assert row["answer"] == ""
    assert row["judge"]["error"] == "skipped: curation failed"
    curation = row["curation"]
    # The first failure stops the replay: later passes would cost model calls
    # for a store that is not what Ciaobot would hold.
    assert curation["sessions"] == 1
    assert curation["haystack_sessions"] == 3
    assert curation["passes"][0]["stderr"] == "boom"
    assert "_note_sessions" not in curation
    summary = HARNESS.summarize([row], qa=True)
    assert summary["incomplete"][0]["reason"].startswith("curate:")
    assert summary["curation"]["passes_failed"] == 1


def test_cli_curated_needs_explicit_flags(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET), encoding="utf-8")
    base = ["--dataset", str(dataset), "--allow-other-dataset", "--out", str(tmp_path / "o.jsonl")]
    with pytest.raises(SystemExit, match="--proposals"):
        HARNESS.main([*base, "--ingest", "curated", "--extract-cmd", "true"])
    with pytest.raises(SystemExit, match="--extract-cmd"):
        HARNESS.main([*base, "--ingest", "curated", "--proposals", "drop"])
    with pytest.raises(SystemExit, match="curated only"):
        HARNESS.main([*base, "--proposals", "drop"])


@posix_shell
def test_cli_curated_retrieval_only_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(DATASET), encoding="utf-8")
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(["q-iguana_abs"]), encoding="utf-8")
    out = tmp_path / "out.jsonl"
    code = HARNESS.main(
        [
            "--dataset", str(dataset), "--allow-other-dataset",
            "--questions", str(questions), "--retrieval-only",
            "--ingest", "curated", "--extract-cmd", _extract_cmd(), "--proposals", "accept",
            "--out", str(out), "--temp-dir", str(tmp_path),
        ]
    )
    assert code == 0
    (row,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert row["mode"] == "retrieval"
    # Abstention: no gold sessions, so nothing to have retained.
    assert row["curation"]["evidence_retained"] is None
    assert row["curation"]["evidence_in_context"] is None
    captured = capsys.readouterr().out
    assert "Ingest: curated (proposals: accept)" in captured
    assert "Curated ingest (proposals: accept)" in captured
    assert "memory passes: 3 ok, 0 failed, 0 skipped, of 3 sessions" in captured
    assert "contradictory or superseded facts left behind: not measured" in captured


def test_curated_summary_splits_knowledge_update_and_abstention() -> None:
    def row(qid: str, stratum: str, verdict: bool, retained: bool | None) -> dict:
        base = _judged_row(qid, stratum, verdict=verdict)
        base["proposals"] = "accept"
        base["curation"] = {
            "sessions": 2, "passes_ok": 2, "passes_failed": 0, "passes_skipped": 0,
            "extract_latency_ms": 100.0,
            "proposals": {"filed": 2, "accepted": 1, "dropped": 0, "failed": 0,
                          "unaccepted": {"review": 1}, "pending_at_end": 1},
            "store": {"notes": 3, "note_chars": 300, "workspace_chars": 50,
                      "region_chars": {"memory": 40, "profile": 10}},
            "raw_chars": 4000,
            "evidence_retained": retained,
            "evidence_in_context": retained,
            "engine_model_calls_blocked": 0,
        }
        return base

    failed = row("q-failed", "multi-session", False, True)
    failed["error"] = "curate: 1 of 1 memory passes failed: command exited 3"
    failed["curation"]["error"] = "1 of 1 memory passes failed: command exited 3"
    failed["curation"]["passes_failed"] = 1
    failed["curation"]["store"]["note_chars"] = 99_999
    rows = [
        row("q-ku", "knowledge-update", True, True),
        row("q-ku2", "knowledge-update", False, False),
        row("q-x_abs", "abstention", True, None),
        failed,
    ]
    summary = HARNESS.summarize(rows, qa=True)
    curation = summary["curation"]
    # The failed question counts as a failed pass but not in what was stored.
    assert (curation["n"], curation["complete"], curation["passes_failed"]) == (4, 3, 1)
    assert curation["note_chars"] == 300
    assert curation["knowledge_update"] == {"n": 2, "correct": 1, "judged": 2, "fraction": 0.5}
    assert curation["abstention"]["correct"] == 1
    assert (curation["evidence_retained"], curation["evidence_answerable"]) == (1, 2)
    assert curation["stored_to_raw"] == pytest.approx(400 / 4000)
    assert curation["proposals_unaccepted"] == {"review": 3}
    text = HARNESS.format_summary(summary, mode="notes")
    assert "knowledge-update accuracy: 50.0% (1/2, 2 judged)" in text
    assert "abstention accuracy: 100.0% (1/1, 1 judged)" in text
    assert "left queued, not auto-acceptable: review 3" in text
    assert "retained evidence (an answer session's pass wrote a line still stored): 50.0% (1/2)" in text


def test_curated_helpers() -> None:
    import datetime as dt

    assert HARNESS.parse_session_date("2023/05/20 (Sat) 02:21") == dt.datetime(
        2023, 5, 20, 2, 21, tzinfo=dt.UTC
    )
    assert HARNESS.parse_session_date("not a date") is None

    # Only role and content are read; has_answer never reaches the pass.
    turns = [
        {"role": "user", "content": "a", "has_answer": True},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": " "},
    ]
    assert HARNESS._session_pairs(turns) == [("a", ""), ("b", "c"), ("", "d")]

    lines = HARNESS._content_lines("---\nupdated: 2026-11-11\ntype: person\n---\n- Lives in Zurich\n§\n\n")
    assert lines == {"type: person", "- Lives in Zurich"}
    # The engine's own queue template and batch headings are not a session's.
    queue = "# Memory Proposals\n\nAwaiting a decision.\n\n## 2026-11-11T17:55:56+00:00\n- [memory] Lives in Zurich\n"
    assert HARNESS._content_lines(queue, entries_only=True) == {"- [memory] Lives in Zurich"}

    notes = {"a.md": ["real-1"], "b.md": ["real-2", "real-3"]}
    assert HARNESS.score_curated_retrieval(["b.md"], notes, ["real-1", "real-2"]) == {
        "gold": 2, "hits": 1, "strict": 0.0, "fractional": 0.5, "any": 1.0,
    }
    assert HARNESS.score_curated_retrieval(["a.md"], notes, []) is None


def test_session_clock_dates_turns_and_the_capsule() -> None:
    import datetime as dt

    from ciao import transcripts
    from ciao.context import capsule

    when = dt.datetime(2023, 5, 20, 9, 0, tzinfo=dt.UTC)
    with HARNESS.session_clock(when):
        assert transcripts._now_iso() == "2023-05-20T09:00:00Z"
        assert capsule.datetime.now(dt.UTC).date().isoformat() == "2023-05-20"
    assert transcripts._now_iso() != "2023-05-20T09:00:00Z"
    assert capsule.datetime is dt.datetime


async def test_engine_model_calls_are_refused() -> None:
    from ciao.providers import oneshot

    blocked: list[str] = []
    original = oneshot.run_oneshot
    with (
        HARNESS.refuse_engine_model_calls(blocked),
        pytest.raises(RuntimeError, match="outside --extract-cmd"),
    ):
        await oneshot.run_oneshot("prompt")
    assert blocked == ["run_oneshot"]
    assert oneshot.run_oneshot is original


def test_extractor_env_drops_inherited_ciao_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_VAULT_ROOT", "/live/vault")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    env = HARNESS.extractor_env({"CIAO_WORKSPACE": "/tmp/install", "CIAO_AGENT_TOKEN": "t"})
    assert "CIAO_VAULT_ROOT" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CIAO_WORKSPACE"] == "/tmp/install"
    assert env["CIAO_AGENT_TOKEN"] == "t"
