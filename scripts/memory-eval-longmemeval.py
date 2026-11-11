#!/usr/bin/env python3
"""Run the LongMemEval-S memory benchmark against Ciaobot's FTS index.

Why this exists
---------------
docs/evals/memory-comparison-2026-11.md reports retrieval recall and answer
accuracy for Ciaobot's memory on LongMemEval-S. The harness that produced those
numbers was never committed, so the report could not be re-run or checked. This
script is that harness: one Markdown note per session, indexed and searched with
`ciao.fts_search` (the engine's real search path), answered and judged by
whatever command the operator points it at.

`--ingest curated` replaces the dump with what Ciaobot actually keeps: each
session is archived in a throwaway install and run through the real
post-archive memory pass (driven by `--extract-cmd`), and answering reads only
the notes and bounded memory regions the passes left behind.

It never bundles or downloads the dataset, never touches a live vault, and never
writes to the live FTS database: every question gets its own temporary vault and
its own temporary `CIAO_MEMORY_DIR` (curated ingest: its own temporary install).

Protocol and limits: docs/evals/longmemeval-protocol.md.

Typical use
-----------
    .venv/bin/python scripts/memory-eval-longmemeval.py \
        --dataset ~/longmemeval/longmemeval_s_cleaned.json \
        --sample 60 --seed 20261108 --mode notes --out runs/ciao-notes.jsonl

    # model-free retrieval over every answerable question
    .venv/bin/python scripts/memory-eval-longmemeval.py \
        --dataset ~/longmemeval/longmemeval_s_cleaned.json \
        --retrieval-only --out runs/retrieval.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import random
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ciao import fts_search

# The cleaned LongMemEval-S snapshot the November 2026 run used. The dataset is
# never bundled: fetch it yourself and let this hash be the check.
EXPECTED_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/"
    "longmemeval_s_cleaned.json"
)

DEFAULT_SEED = 20261108
# The published draw: 10/12/12/6/6/6/8 over its seven strata. Order matters for
# `random.Random` state, so selection walks these strata in this exact order.
PUBLISHED_SAMPLE = 60
STRATA: tuple[tuple[str, int], ...] = (
    ("knowledge-update", 10),
    ("temporal-reasoning", 12),
    ("multi-session", 12),
    ("single-session-user", 6),
    ("single-session-assistant", 6),
    ("single-session-preference", 6),
    ("abstention", 8),
)
ABSTENTION = "abstention"

# Search 10 and score the first 5: the November protocol's extra five results
# are not part of any metric and exist only so a future variant can score deeper
# without re-indexing.
SEARCH_LIMIT = 10
SCORED_AT = 5

# Initial search plus at most three model-issued searches, as measured.
AGENTIC_MAX_SEARCHES = 4

# The November invocation. The prompt is passed as the argument, the reply is
# written to {out_file}; a template that prints to stdout instead also works.
_CLAUDE_FLAGS = (
    '--tools "" --setting-sources "" --strict-mcp-config '
    "--no-session-persistence --system-prompt \"You are a helpful assistant.\""
)
DEFAULT_ANSWER_CMD = (
    "claude -p --model claude-haiku-5-5 " + _CLAUDE_FLAGS + " "
    '"$(cat {prompt_file})" > {out_file}'
)
DEFAULT_JUDGE_CMD = (
    "claude -p --model claude-sonnet-5-5 " + _CLAUDE_FLAGS + " "
    '"$(cat {prompt_file})" > {out_file}'
)

# LongMemEval's chain-of-thought answer template, copied from run_generation.py
# in xiaowu0162/LongMemEval (`answer_prompt_template`, cot=True, no-retrieval
# variant excluded). Do not reword it: the published numbers used it verbatim.
ANSWER_TEMPLATE = (
    "I will give you several history chats between you and a user. Please answer "
    "the question based on the relevant chat history. Answer the question step by "
    "step: first extract all the relevant information, and then reason over the "
    "information to get the answer.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: "
    "{}\nQuestion: {}\nAnswer (step by step):"
)

# The judge templates below are copied verbatim from
# src/evaluation/evaluate_qa.py (`get_anscheck_prompt`), including the abstention
# variant. Keep the wording and whitespace: the official evaluator scores a
# question correct when the reply contains "yes".
_JUDGE_SHARED = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel "
    "Response: {}\n\nIs the model response correct? Answer yes or no only."
)
_JUDGE_TEMPORAL = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response is equivalent to the correct answer or contains "
    "all the intermediate steps to get the correct answer, you should also answer "
    "yes. If the response only contains a subset of the information required by "
    "the answer, answer no. In addition, do not penalize off-by-one errors for "
    "the number of days. If the question asks for the number of days/weeks/"
    "months, etc., and the model makes off-by-one errors (e.g., predicting 19 "
    "days when the answer is 18), the model's response is still correct. "
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the "
    "model response correct? Answer yes or no only."
)
_JUDGE_KNOWLEDGE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, "
    "answer no. If the response contains some previous information along with an "
    "updated answer, the response should be considered as correct as long as the "
    "updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: "
    "{}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no "
    "only."
)
_JUDGE_PREFERENCE = (
    "I will give you a question, a rubric for desired personalized response, and "
    "a response from a model. Please answer yes if the response satisfies the "
    "desired response. Otherwise, answer no. The model does not need to reflect "
    "all the points in the rubric. The response is correct as long as it recalls "
    "and utilizes the user's personal information correctly.\n\nQuestion: {}\n\n"
    "Rubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer "
    "yes or no only."
)
_JUDGE_ABSTENTION = (
    "I will give you an unanswerable question, an explanation, and a response "
    "from a model. Please answer yes if the model correctly identifies the "
    "question as unanswerable. The model could say that the information is "
    "incomplete, or some other information is given but the asked information is "
    "not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the "
    "model correctly identify the question as unanswerable? Answer yes or no only."
)

# The agentic mode has no official prompt: LongMemEval only ships one-shot
# answering. This wrapper is the November rule written down, so it is part of the
# protocol this harness defines, not a reproduction of someone else's template.
AGENTIC_INSTRUCTIONS = (
    "I will give you several history chats between you and a user. Please answer "
    "the question based on the relevant chat history. Answer the question step by "
    "step: first extract all the relevant information, and then reason over the "
    "information to get the answer.\n\n"
    "You can search the same memory again before you answer. The search is "
    "lexical: it matches words, it does not understand synonyms, so try two or "
    "three different wordings before you conclude that the answer is not there. "
    "To search, reply with exactly one line and nothing else:\n\n"
    "SEARCH: <your query>\n\n"
    "You have {remaining} more search(es). They are searches of the stored notes; "
    "there are no other tools.\n"
    "When you are ready to answer, reply with:\n\n"
    "FINAL ANSWER: <your answer>"
)

REQUIRED_FIELDS = (
    "question_id",
    "question_type",
    "question",
    "answer",
    "question_date",
    "haystack_session_ids",
    "haystack_dates",
    "haystack_sessions",
    "answer_session_ids",
)

_FINAL_RE = re.compile(r"FINAL ANSWER\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)
_SEARCH_RE = re.compile(r"SEARCH\s*:\s*(.+)", re.IGNORECASE)


# ── dataset and selection ───────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_dataset(path: Path, *, allow_other: bool = False) -> str:
    """Return the file's sha256, refusing anything but the documented snapshot.

    `--allow-other-dataset` exists so an operator can benchmark a refreshed
    snapshot on purpose. The actual hash then has to travel with the report,
    which is why this returns it instead of only checking it.
    """
    if not path.is_file():
        raise SystemExit(
            f"Dataset not found: {path}\n"
            f"Download longmemeval_s_cleaned.json from {DATASET_URL} and pass "
            "--dataset. The dataset is never bundled with Ciaobot."
        )
    actual = sha256_file(path)
    if actual != EXPECTED_SHA256 and not allow_other:
        raise SystemExit(
            "Dataset sha256 mismatch; refusing to benchmark a different file.\n"
            f"  expected {EXPECTED_SHA256}\n"
            f"  actual   {actual}\n"
            f"Expected the cleaned LongMemEval-S snapshot from {DATASET_URL}.\n"
            "Pass --allow-other-dataset to override; the report will carry the "
            "actual hash."
        )
    return actual


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """Load the official JSON array (or JSONL, as LongMemEval's own scripts do)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Cannot read dataset {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{number}: not valid JSON") from exc
    if not isinstance(data, list) or not data:
        raise SystemExit(f"{path}: expected a non-empty JSON array of questions")
    for position, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise SystemExit(f"{path}: entry {position} is not an object")
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            raise SystemExit(
                f"{path}: entry {position} is missing {', '.join(missing)}"
            )
    return data


def question_stratum(entry: dict[str, Any]) -> str:
    """The stratum a question belongs to for sampling and reporting.

    LongMemEval marks an abstention question by the `_abs` suffix on its id; its
    `question_type` is the type it would have had if answerable. The November
    report has a separate abstention column, so abstention is its own stratum.
    The official evaluate_qa.py does the opposite and groups `_abs` questions
    under their nominal type; do not compare per-type columns without allowing
    for that.
    """
    if str(entry["question_id"]).endswith("_abs"):
        return ABSTENTION
    return str(entry["question_type"])


def stratum_allocations(size: int) -> dict[str, int]:
    """Split `size` over the published strata, exactly reproducing 60."""
    total = sum(count for _, count in STRATA)
    quotas = {name: size * count / total for name, count in STRATA}
    allocation = {name: int(quota) for name, quota in quotas.items()}
    remaining = size - sum(allocation.values())
    order = sorted(
        quotas,
        key=lambda name: (
            -(quotas[name] - allocation[name]),
            [n for n, _ in STRATA].index(name),
        ),
    )
    for name in order:
        if remaining <= 0:
            break
        allocation[name] += 1
        remaining -= 1
    return allocation


def select_questions(
    entries: list[dict[str, Any]], size: int, seed: int
) -> list[dict[str, Any]]:
    """The published stratified draw: sorted ids per stratum, one seeded RNG."""
    if size > len(entries):
        raise SystemExit(
            f"Cannot draw {size} questions from a dataset of {len(entries)}"
        )
    known = {name for name, _ in STRATA}
    unknown = sorted({question_stratum(entry) for entry in entries} - known)
    if unknown:
        raise SystemExit(
            "Unknown question type(s) in the dataset: " + ", ".join(unknown)
        )
    by_stratum: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in STRATA}
    for entry in entries:
        by_stratum[question_stratum(entry)].append(entry)
    for bucket in by_stratum.values():
        bucket.sort(key=lambda item: str(item["question_id"]))

    allocation = stratum_allocations(size)
    # A short stratum means the file is not the documented snapshot (or was
    # truncated). Redistributing would quietly change the published sample, so
    # refuse instead.
    for name, wanted in allocation.items():
        available = len(by_stratum.get(name, []))
        if wanted > available:
            raise SystemExit(
                f"Cannot draw the published sample: stratum {name!r} needs "
                f"{wanted} questions but the dataset has {available}"
            )

    rng = random.Random(seed)
    chosen: list[dict[str, Any]] = []
    for name, _ in STRATA:
        ids = [str(entry["question_id"]) for entry in by_stratum.get(name, [])]
        wanted = allocation.get(name, 0)
        if wanted:
            picked = set(rng.sample(ids, wanted))
            chosen.extend(
                entry for entry in by_stratum[name] if str(entry["question_id"]) in picked
            )
    return chosen


def load_question_ids(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"Cannot read question list {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise SystemExit(f"{path}: expected a JSON array of question ids (strings)")
    if len(set(data)) != len(data):
        raise SystemExit(f"{path}: duplicate question ids")
    return data


def select_by_ids(
    entries: list[dict[str, Any]], question_ids: list[str]
) -> list[dict[str, Any]]:
    index = {str(entry["question_id"]): entry for entry in entries}
    missing = [qid for qid in question_ids if qid not in index]
    if missing:
        raise SystemExit(
            "Question ids not in the dataset: " + ", ".join(missing)
        )
    return [index[qid] for qid in question_ids]


# ── one isolated haystack ───────────────────────────────────────────────────


def _slug(text: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z]+", "-", text).strip("-")
    return slug[:48] or "session"


def _session_note(neutral_id: str, date: str, turns: list[dict[str, Any]]) -> str:
    """One note per session, in the shape the November run indexed.

    Only `role` and `content` are read. LongMemEval's `has_answer` turn flag would
    tell the search which turn holds the answer, so it is deliberately dropped.
    """
    lines = [
        "---",
        "type: chat-session",
        f"title: Chat session {neutral_id}",
        f"updated: {date}",
        "---",
        f"# Chat session {neutral_id}",
        "",
    ]
    for turn in turns:
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{turn.get('role', 'user')}: {content}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_haystack(workdir: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Write one note per unique session and return the lookup tables.

    Repeated session ids (the cleaned set has a handful) are one session with one
    note: indexing the same content twice would let it occupy several of the five
    result slots. Session ids in the notes are neutral (`s00`, `s01`, …) because
    LongMemEval's own ids reveal which sessions hold the answer.
    """
    session_ids = list(entry["haystack_session_ids"])
    dates = list(entry["haystack_dates"])
    sessions = list(entry["haystack_sessions"])
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise ValueError(
            f"{entry['question_id']}: haystack session ids, dates and sessions "
            "have different lengths"
        )

    vault = workdir / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    prefix = fts_search.vault_key_prefix(vault, workdir)
    path_to_session: dict[str, str] = {}
    session_date: dict[str, str] = {}
    note_text: dict[str, str] = {}

    seen: set[str] = set()
    for session_id, date, turns in zip(session_ids, dates, sessions):
        if session_id in seen:
            continue
        seen.add(session_id)
        neutral_id = f"s{len(session_date):02d}"
        name = f"{_slug(str(date))}-chat-{neutral_id}.md"
        text = _session_note(neutral_id, str(date), turns)
        (vault / name).write_text(text, encoding="utf-8")
        path_to_session[prefix + name] = str(session_id)
        session_date[str(session_id)] = str(date)
        note_text[str(session_id)] = text

    return {
        "vault": vault,
        "prefix": prefix,
        "path_to_session": path_to_session,
        "session_date": session_date,
        "note_text": note_text,
    }


@contextlib.contextmanager
def question_index(
    workdir: Path, entry: dict[str, Any]
) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
    """A throwaway vault and FTS database for one question.

    `CIAO_MEMORY_DIR` is set explicitly because it wins over every runtime
    directory in `fts_search.get_db_path`; without it a dev checkout could open
    (and rebuild) the live install's index. It is restored afterwards so an
    in-process caller never leaves the process pointed at a deleted directory.
    """
    haystack = write_haystack(workdir, entry)
    memory_dir = workdir / "fts"
    memory_dir.mkdir(parents=True, exist_ok=True)

    previous = os.environ.get("CIAO_MEMORY_DIR")
    os.environ["CIAO_MEMORY_DIR"] = str(memory_dir)
    try:
        db_path = fts_search.get_db_path(memory_dir)
        conn = sqlite3.connect(db_path)
        try:
            fts_search.init_db(conn)
            fts_search.index_vault(conn, haystack["vault"], path_base=workdir)
            yield conn, haystack
        finally:
            conn.close()
    finally:
        if previous is None:
            os.environ.pop("CIAO_MEMORY_DIR", None)
        else:
            os.environ["CIAO_MEMORY_DIR"] = previous


def _temporary_dir(temp_root: Path | None) -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(
        prefix="ciao-longmemeval-", dir=str(temp_root) if temp_root else None
    )


def search_hits(
    conn: sqlite3.Connection,
    query: str,
    prefix: str,
    path_to_session: dict[str, str],
) -> list[dict[str, Any]]:
    """Search one question's index, in rank order, mapped back to session ids."""
    rows = fts_search.search_vault(
        conn, query, limit=SEARCH_LIMIT, path_prefix=prefix
    )
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        session_id = path_to_session.get(row["path"])
        if session_id is None or session_id in seen:
            continue
        seen.add(session_id)
        hits.append({**row, "session_id": session_id})
    return hits


def score_retrieval(
    retrieved: list[str], gold: list[str]
) -> dict[str, Any] | None:
    """Strict/fractional/any-hit at 5; abstention questions have no gold."""
    unique_gold = list(dict.fromkeys(str(item) for item in gold))
    if not unique_gold:
        return None
    top = retrieved[:SCORED_AT]
    hits = sum(1 for session_id in unique_gold if session_id in top)
    return {
        "gold": len(unique_gold),
        "hits": hits,
        "strict": 1.0 if hits == len(unique_gold) else 0.0,
        "fractional": hits / len(unique_gold),
        "any": 1.0 if hits else 0.0,
    }


# ── curated ingest ──────────────────────────────────────────────────────────
#
# Raw ingest stores every session as a note, which measures search over a full
# dump. Ciaobot does not keep a dump: when a chat is archived, a memory pass
# decides what becomes a note, what becomes a bounded-memory entry, what waits
# in the proposal queue and what is dropped. Curated ingest replays each
# haystack session through that real step, in date order, in a throwaway
# install, and answers over what is left.
#
# Everything below is engine code except two seams:
#   * the provider. `HeadlessChatManager._get_provider` hands every chat a
#     `CommandProvider`, which runs the operator's `--extract-cmd` with the
#     prompt, environment and working directory the engine built for that
#     turn. That is how the extracting model is chosen, the same way
#     `--answer-cmd` chooses the answering one;
#   * the clock. Archived turns and the pass's `today=` line carry the
#     session's LongMemEval date rather than the wall clock.
# The archive (`ProjectChatManager.archive_chat`), the memory-pass gating and
# queueing (`run_archive_postprocess`), the pass prompt, the context capsule,
# the agent control plane the pass's `ciao` commands call, the workspace seed
# (`sync_workspace_skills`), and the proposal accept/dismiss route
# (`proposals_batch`) all run unmodified.

PROPOSAL_MODES = ("accept", "drop", "leave")

#: Queue kinds `--proposals accept` promotes. Their accept is a deterministic
#: file write. The others either call a model through the engine's own provider
#: (`project` folds into a doc with a one-shot call) or need a person to choose
#: something (`review`, `category`, `rehome`, `note_edit`, `repo`, `skill`), so
#: they stay queued and are counted as `unaccepted`.
ACCEPTABLE_PROPOSAL_KINDS = frozenset({"memory", "profile", "user", "people", "learnings"})

#: Frontmatter keys whose value changes on every rewrite of a note. A line that
#: only re-stamps one of them is not evidence of anything a session said.
_BOOKKEEPING_KEYS = frozenset(
    {"updated", "created", "modified", "date", "last_updated", "last_reviewed", "reviewed"}
)
_SESSION_DATE_RE = re.compile(
    r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\s*\([^)]*\))?(?:\s+(\d{1,2}):(\d{2}))?"
)


def parse_session_date(text: str) -> Any:
    """LongMemEval's `2023/05/20 (Sat) 02:21` as an aware UTC datetime, or None."""
    import datetime as dt

    match = _SESSION_DATE_RE.match(str(text or ""))
    if not match:
        return None
    year, month, day, hour, minute = match.groups()
    try:
        return dt.datetime(
            int(year), int(month), int(day), int(hour or 0), int(minute or 0),
            tzinfo=dt.UTC,
        )
    except ValueError:
        return None


@contextlib.contextmanager
def session_clock(when: Any) -> Iterator[None]:
    """Date the archived turns and the pass's `today=` line at `when`.

    The engine stamps a turn with `ciao.transcripts._now_iso` and the context
    capsule with `datetime.now`, so without this every session would read as
    happening today and the pass could not record when anything was said.
    Other engine stamps (a proposal's queue heading, a learned-at stamp written
    by an accept) keep the wall clock; see the protocol's limits.
    """
    if when is None:
        yield
        return
    import datetime as dt

    from ciao import transcripts
    from ciao.context import capsule

    frozen = when.replace(microsecond=0)

    class _Frozen(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> dt.datetime:  # type: ignore[override]
            return frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)

    stamp = frozen.isoformat().replace("+00:00", "Z")
    previous_now_iso = transcripts._now_iso
    previous_datetime = capsule.datetime
    transcripts._now_iso = lambda: stamp  # type: ignore[assignment]
    capsule.datetime = _Frozen  # type: ignore[misc]
    try:
        yield
    finally:
        transcripts._now_iso = previous_now_iso  # type: ignore[assignment]
        capsule.datetime = previous_datetime  # type: ignore[misc]


@contextlib.contextmanager
def isolated_ciao_env(install_root: Path, memory_dir: Path) -> Iterator[None]:
    """Strip every inherited `CIAO_*` variable and point the engine at the temp install.

    An operator shell may carry `CIAO_WORKSPACE`, `CIAO_VAULT_ROOT` or
    `CIAO_RUNTIME_ROOT` for their live install; any one of them would let an
    engine helper that reads the environment reach it. `CIAO_MEMORY_DIR` also
    names the legacy `~/.ciao` memory files the workspace seed would migrate,
    so pointing it at the temp directory keeps the operator's own memory out of
    the haystack. Restored afterwards.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith("CIAO_")}
    for key in saved:
        del os.environ[key]
    os.environ["CIAO_WORKSPACE"] = str(install_root)
    os.environ["CIAO_MEMORY_DIR"] = str(memory_dir)
    try:
        yield
    finally:
        for key in [key for key in os.environ if key.startswith("CIAO_")]:
            del os.environ[key]
        os.environ.update(saved)


@contextlib.contextmanager
def refuse_engine_model_calls(blocked: list[str]) -> Iterator[None]:
    """Fail any one-shot model call the engine makes on its own.

    The extracting model must be the operator's command and nothing else. A few
    engine paths (a `[project]` fold, an opt-in reconcile, an LLM chat title)
    call a provider directly; during curated ingest they raise instead, and the
    attempt is recorded so the question is reported incomplete.
    """
    from ciao.providers import oneshot

    previous = oneshot.run_oneshot

    async def _refused(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        blocked.append("run_oneshot")
        raise RuntimeError(
            "curated ingest: the engine tried a model call outside --extract-cmd"
        )

    oneshot.run_oneshot = _refused  # type: ignore[assignment]
    try:
        yield
    finally:
        oneshot.run_oneshot = previous  # type: ignore[assignment]


def guide_above(path: Path) -> Path | None:
    """The nearest `AGENTS.md` or `CLAUDE.md` in `path` or any parent, or None."""
    for folder in [path.resolve(), *path.resolve().parents]:
        for name in ("AGENTS.md", "CLAUDE.md"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


def refuse_guide_above(path: Path) -> None:
    """Refuse a temp directory inside a repository or vault that has a guide.

    The extractor runs in the temp install, and an agent CLI such as Claude
    Code reads every `CLAUDE.md` / `AGENTS.md` from its working directory up.
    Under a live workspace that would hand the pass the operator's own guide,
    memory regions included, as if it were the temp install's.
    """
    found = guide_above(path)
    if found is not None:
        raise ValueError(
            f"curated ingest refuses a temp directory under {found}: the extracting "
            "agent would load that guide. Use the system temp or another --temp-dir."
        )


def extractor_env(extra: dict[str, str]) -> dict[str, str]:
    """`clean_env()` without any inherited `CIAO_*`, plus the turn's own environment."""
    env = {key: value for key, value in clean_env().items() if not key.startswith("CIAO_")}
    env.update(extra)
    return env


def render_extract_command(template: str, files: dict[str, Path], tools: str) -> str:
    """Literal placeholder substitution, as `run_template` does it."""
    command = template
    for name, path in files.items():
        command = command.replace("{" + name + "}", shlex.quote(str(path)))
    return command.replace("{disallowed_tools}", shlex.quote(tools))


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill an extractor and everything it started, then reap it."""
    if proc.returncode is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows has no process groups to signal
                proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


class CommandProvider:
    """Stand in for the chat's provider: run `--extract-cmd` for one turn.

    Receives the `AgentRequest` the engine built — the context capsule plus the
    memory-pass prompt, the turn's environment (the agent control-plane URL and
    token included, so `ciao memory update` and friends reach this install),
    and the tools the engine would deny. The command runs in the workspace's
    agent root, where the seeded `AGENTS.md` and skills are.
    """

    current_session_id = None
    provider = None

    def __init__(self, template: str, cwd: Path, workdir: Path, timeout: float) -> None:
        self._template = template
        self._cwd = cwd
        self._workdir = workdir
        self._timeout = timeout
        self.calls: list[dict[str, Any]] = []

    async def execute_streaming(self, request: Any) -> Any:
        from ciao.core_prompt import system_prompt_payload
        from ciao.models import ResultEvent

        call_dir = self._workdir / f"extract-{len(self.calls) + 1:03d}"
        call_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "prompt_file": call_dir / "prompt.txt",
            "out_file": call_dir / "response.txt",
            "system_prompt_file": call_dir / "system.txt",
        }
        files["prompt_file"].write_text(request.prompt, encoding="utf-8")
        # What the Claude provider appends to Claude Code's own system prompt.
        payload = system_prompt_payload("") or {}
        files["system_prompt_file"].write_text(
            str(payload.get("append") or ""), encoding="utf-8"
        )
        command = render_extract_command(
            self._template, files, ",".join(request.disallowed_tools or [])
        )
        started = time.perf_counter()
        info: dict[str, Any] = {}
        reply = ""
        try:
            # Its own process group, so a timeout or a cancelled turn stops the
            # agent itself and not only the `sh` that started it: an orphaned
            # agent would keep spending model calls on a deleted install.
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(self._cwd),
                env=extractor_env(dict(request.extra_env or {})),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except TimeoutError:
                await _kill_process_group(proc)
                info["error"] = f"command timed out after {self._timeout:g}s"
            except BaseException:
                await _kill_process_group(proc)
                raise
            else:
                reply = stdout.decode("utf-8", errors="replace")
                out_file = files["out_file"]
                if out_file.is_file():
                    written = out_file.read_text(encoding="utf-8", errors="replace")
                    if written.strip():
                        reply = written
                reply = reply.strip()
                info["exit_code"] = proc.returncode
                info["stderr"] = stderr.decode("utf-8", errors="replace").strip()[-500:]
                if proc.returncode != 0:
                    info["error"] = f"command exited {proc.returncode}"
                elif not reply:
                    info["error"] = "command produced no output"
        except OSError as exc:
            info["error"] = f"could not start the command: {exc}"
        info["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        self.calls.append(info)
        yield ResultEvent(
            type="result",
            result=reply or info.get("error", ""),
            is_error=bool(info.get("error")),
        )


def _session_pairs(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(user, assistant) exchanges in order; only `role` and `content` are read.

    LongMemEval's `has_answer` flag is never read, so the pass cannot see which
    turn holds the answer.
    """
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for turn in turns:
        content = str(turn.get("content", "")).strip()
        if not content:
            continue
        if turn.get("role", "user") == "user":
            if pending is not None:
                pairs.append((pending, ""))
            pending = content
        else:
            pairs.append((pending or "", content))
            pending = None
    if pending is not None:
        pairs.append((pending, ""))
    return pairs


def _content_lines(text: str, *, entries_only: bool = False) -> set[str]:
    """The lines of a stored file that can carry a fact.

    Blank lines, rules, region separators and bookkeeping frontmatter stamps
    (`updated:` and the like) are skipped: rewriting a note re-stamps them, and
    counting that as retained evidence would credit a pass that dropped the
    fact but touched the note. `entries_only` keeps only list items, for the
    engine-owned `Workspace/` files (the proposal queue, observations,
    learnings): their title, template paragraphs and timestamped batch headings
    are written by the engine whatever the pass said, and survive an accept or
    a dismissal, so only the entries are anything a session contributed.
    """
    lines: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if len(re.sub(r"[\W_]+", "", line)) < 3:
            continue
        if entries_only and not line.startswith(("- ", "* ")):
            continue
        key = line.split(":", 1)[0].strip().lower() if ":" in line else ""
        if key in _BOOKKEEPING_KEYS:
            continue
        lines.add(line)
    return lines


def _is_workspace_file(key: str) -> bool:
    return "/Workspace/" in f"/{key}"


def _region_key(guide: Path, region: str) -> str:
    """A snapshot key for a memory region. Never a vault key: those end in `.md`."""
    return f"{guide.name}#{region}"


def _vault_files(vault: Path, base: Path) -> Iterator[tuple[str, Path]]:
    """The files `fts_search.index_vault` would index, keyed as it keys them."""
    if not vault.is_dir():
        return
    from ciao import vault_index

    for root_rel, entry in fts_search._walk_notes(
        str(vault), ".md", fts_search.EXCLUDED_VAULT_DIRS
    ):
        if vault_index.is_generated_vault_file(entry.name):
            continue
        path = Path(entry.path)
        yield path.relative_to(base).as_posix(), path
    del root_rel


def _region_entries(guide: Path) -> dict[str, list[str]]:
    from ciao import memory_tool

    return {
        region: memory_tool.read_region(guide, region)[0]
        for region in memory_tool.REGIONS
    }


def store_snapshot(vault: Path, base: Path, guide: Path) -> dict[str, set[str]]:
    """Every fact-bearing line the store holds, by file (regions as `AGENTS.md#memory`)."""
    snapshot: dict[str, set[str]] = {}
    for key, path in _vault_files(vault, base):
        try:
            snapshot[key] = _content_lines(
                path.read_text(encoding="utf-8"), entries_only=_is_workspace_file(key)
            )
        except (OSError, UnicodeError):
            continue
    for region, entries in _region_entries(guide).items():
        snapshot[_region_key(guide, region)] = _content_lines("\n".join(entries))
    return snapshot


def store_size(vault: Path, base: Path, guide: Path) -> dict[str, Any]:
    """What the store holds: notes, Workspace bookkeeping files, and the regions."""
    size = {
        "notes": 0,
        "note_chars": 0,
        "note_bytes": 0,
        "workspace_files": 0,
        "workspace_chars": 0,
        "workspace_bytes": 0,
    }
    for key, path in _vault_files(vault, base):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        bucket = "workspace" if _is_workspace_file(key) else "note"
        if bucket == "workspace":
            size["workspace_files"] += 1
        else:
            size["notes"] += 1
        size[f"{bucket}_chars"] += len(data.decode("utf-8", errors="replace"))
        size[f"{bucket}_bytes"] += len(data)
    regions = _region_entries(guide)
    size["region_chars"] = {
        region: sum(len(entry) for entry in entries) for region, entries in regions.items()
    }
    size["region_entries"] = {region: len(entries) for region, entries in regions.items()}
    return size


def regions_block(guide: Path) -> str:
    """The bounded memory regions, as every real chat loads them, or ""."""
    from ciao import memory_tool

    headings = {"memory": "Agent memory", "profile": "User profile"}
    parts = []
    for region, entries in _region_entries(guide).items():
        if entries:
            separator = f"\n{memory_tool.SECTION_SEP}\n"
            parts.append(f"## {headings.get(region, region)}\n{separator.join(entries)}")
    if not parts:
        return ""
    return "### Workspace memory (loaded in every chat):\n" + "\n\n".join(parts)


def score_curated_retrieval(
    note_keys: list[str], note_sessions: dict[str, list[str]], gold: list[str]
) -> dict[str, Any] | None:
    """`score_retrieval`'s shape over notes: a gold session is retrieved when
    one of the top notes holds a line written while its pass ran."""
    unique_gold = list(dict.fromkeys(str(item) for item in gold))
    if not unique_gold:
        return None
    reached = {
        session for key in note_keys[:SCORED_AT] for session in note_sessions.get(key, [])
    }
    hits = sum(1 for session_id in unique_gold if session_id in reached)
    return {
        "gold": len(unique_gold),
        "hits": hits,
        "strict": 1.0 if hits == len(unique_gold) else 0.0,
        "fractional": hits / len(unique_gold),
        "any": 1.0 if hits else 0.0,
    }


class HeadlessEngine:
    """A throwaway Ciaobot install that runs the real post-archive memory pass.

    Built from the same pieces `ciao.main` wires together, in a temp directory,
    with no PWA and no provider: the project/chat registry, the transcript
    store, the agent control plane (served on an ephemeral loopback port, which
    the pass's `ciao` commands need), and the proposal routes (called
    in-process).
    """

    def __init__(
        self, install_root: Path, *, extract_cmd: str, workdir: Path, timeout: float
    ) -> None:
        self.root = install_root
        self.extract_cmd = extract_cmd
        self.workdir = workdir
        self.timeout = timeout
        self.providers: list[CommandProvider] = []

    async def start(self) -> None:
        import socket

        import httpx
        import uvicorn
        from starlette.applications import Starlette
        from starlette.routing import Route

        from ciao.config import CiaoConfig
        from ciao.control_plane import CiaoControlPlane
        from ciao.mcp_server import CiaoMcpService
        from ciao.sessions import StateStore
        from ciao.sync_skills import sync_workspace_skills
        from ciao.transcripts import TranscriptStore
        from ciao.web.project_chats import ProjectChatManager
        from ciao.web.routes_agent import agent_dispatch_endpoint
        from ciao.web.routes_api import list_proposals, proposals_batch
        from ciao.workspace_guide import guide_path

        engine = self

        class HeadlessChatManager(ProjectChatManager):
            def _get_provider(self, chat_id: str) -> Any:  # type: ignore[override]
                provider = CommandProvider(
                    engine.extract_cmd,
                    self._agent_root_for_chat(chat_id),
                    engine.workdir / f"pass-{len(engine.providers) + 1:03d}",
                    engine.timeout,
                )
                engine.providers.append(provider)
                return provider

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        runtime = self.root / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        config = CiaoConfig(
            pwa_auth_token=hashlib.sha256(os.urandom(16)).hexdigest(),
            workspace_root=self.root,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
            pwa_port=port,
            pwa_host="127.0.0.1",
        )
        self.config = config
        self.workspace = config.primary_workspace()
        # The seed every install's agent root gets at startup (stock guide with
        # empty memory regions, stock skills and commands), minus the upstream
        # refresh, which is network.
        with contextlib.redirect_stdout(sys.stderr):
            sync_workspace_skills(
                config.agent_root(self.workspace),
                refresh_upstream=False,
                workspace_name=self.workspace,
            )
        self.guide = guide_path(config.agent_root(self.workspace))
        self.vault = Path(config.workspace_vault_root(self.workspace))

        transcripts = TranscriptStore(runtime, config.logs_root / "Chats")
        pcm = HeadlessChatManager(
            config,
            state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
            transcript_store=transcripts,
            path=runtime / "web_projects.json",
        )
        transcripts.set_archive_root_resolver(pcm.chat_archive_root)
        service = CiaoMcpService(config)
        service.bind(CiaoControlPlane(config, project_chat_manager=pcm, schedule_manager=None))
        pcm._mcp_service = service
        self.pcm = pcm
        self.transcripts = transcripts
        self.project = pcm.create_project("General", self.workspace)

        agent_app = Starlette(
            routes=[Route("/agent/v1/{op}", agent_dispatch_endpoint, methods=["POST"])]
        )
        agent_app.state.mcp_service = service
        self._server = uvicorn.Server(
            uvicorn.Config(
                agent_app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self._serve = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._serve.done():
                self._serve.result()
                raise RuntimeError("the agent control plane did not start")
            await asyncio.sleep(0.01)

        api_app = Starlette(
            routes=[
                Route("/api/proposals", list_proposals, methods=["GET"]),
                Route("/api/proposals/batch", proposals_batch, methods=["POST"]),
            ]
        )
        api_app.state.config = config
        api_app.state.project_chat_manager = pcm
        self._api = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://engine"
        )

    async def close(self) -> None:
        await self._settle()
        await self._api.aclose()
        self._server.should_exit = True
        await self._serve

    async def _settle(self) -> None:
        """Let detached hooks a turn spawned finish (as the engine's tests do)."""
        while True:
            pending = [task for task in self.pcm._detached_tasks if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    async def pending_proposals(self) -> list[dict[str, Any]]:
        response = await self._api.get("/api/proposals")
        response.raise_for_status()
        return list(response.json().get("rows") or [])

    async def resolve_proposals(self, mode: str) -> dict[str, Any]:
        """Apply `--proposals` to what is queued now, through the review route."""
        rows = await self.pending_proposals()
        result: dict[str, Any] = {"accepted": 0, "dropped": 0, "unaccepted": {}, "failed": 0}
        if mode == "leave" or not rows:
            return result
        if mode == "accept":
            ids = [row["id"] for row in rows if row.get("kind") in ACCEPTABLE_PROPOSAL_KINDS]
            for row in rows:
                if row.get("kind") not in ACCEPTABLE_PROPOSAL_KINDS:
                    kind = str(row.get("kind") or "?")
                    result["unaccepted"][kind] = result["unaccepted"].get(kind, 0) + 1
            action = "accept"
        else:
            ids = [row["id"] for row in rows]
            action = "dismiss"
        if not ids:
            return result
        await self._api.post("/api/proposals/batch", json={"action": action, "ids": ids})
        remaining = {row["id"] for row in await self.pending_proposals()}
        done = sum(1 for pid in ids if pid not in remaining)
        result["accepted" if action == "accept" else "dropped"] = done
        result["failed"] = len(ids) - done
        return result

    async def ingest_session(
        self, title: str, turns: list[dict[str, Any]], when: Any
    ) -> dict[str, Any]:
        """Archive one conversation the way the PWA's archive route does, and
        wait for the memory pass it queues to settle."""
        from ciao.models import AgentRequest, ChatContext

        pcm = self.pcm
        record: dict[str, Any] = {"status": "", "latency_ms": 0.0}
        with session_clock(when):
            chat = pcm.create_chat(self.project.project_id, title=title)
            ctx = ChatContext.for_web(chat.chat_id)
            for prompt, reply in _session_pairs(turns):
                self.transcripts.record_turn(
                    AgentRequest(prompt=prompt, model=chat.model, mode=chat.mode),
                    ctx=ctx,
                    response_text=reply,
                    effective_model=chat.model,
                    session_id=None,
                    usage={},
                    quota={},
                    input_kind="text",
                    context_label=title,
                    provider=chat.provider,
                )
            chat_meta = pcm.get_chat(chat.chat_id)
            project_meta = pcm.get_project(chat.project_id)
            outcome = await pcm.archive_chat(chat.chat_id)
            if outcome is None:
                record["status"] = "empty"
                return record
            before = len(self.providers)
            started = time.perf_counter()
            pcm.run_archive_postprocess(chat.chat_id, outcome, chat_meta, project_meta)
            pass_chat = next(
                (
                    other
                    for other in pcm._chats.values()
                    if (other.helper or {}).get("kind") == "memory_pass"
                    and (other.helper or {}).get("source_chat_id") == chat.chat_id
                ),
                None,
            )
            if pass_chat is None:
                await self._settle()
                record["status"] = "skipped"
                return record
            while True:
                turn = pcm._streaming.drive_task(pass_chat.chat_id)
                if turn is not None:
                    await turn
                await self._settle()
                state = (pass_chat.helper or {}).get("state")
                if state not in {"queued", "running"} or turn is None:
                    break
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            state = (pass_chat.helper or {}).get("state")
            record["status"] = "ok" if state == "done" else "failed"
            calls = [call for provider in self.providers[before:] for call in provider.calls]
            if record["status"] != "ok":
                failure = next((call for call in calls if call.get("error")), {})
                record["error"] = str(
                    failure.get("error")
                    or pass_chat.last_response.strip()[-300:]
                    or f"memory pass ended in state {state!r}"
                )
                if failure.get("stderr"):
                    record["stderr"] = failure["stderr"]
        return record


async def _curate(
    install_root: Path,
    entry: dict[str, Any],
    *,
    extract_cmd: str,
    proposals: str,
    workdir: Path,
    timeout: float,
) -> dict[str, Any]:
    """Feed one question's haystack through the memory pass, oldest session first."""
    session_ids = list(entry["haystack_session_ids"])
    dates = list(entry["haystack_dates"])
    sessions = list(entry["haystack_sessions"])
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise ValueError(
            f"{entry['question_id']}: haystack session ids, dates and sessions "
            "have different lengths"
        )
    unique: list[tuple[str, str, str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for session_id, date, turns in zip(session_ids, dates, sessions):
        if str(session_id) in seen:
            continue
        seen.add(str(session_id))
        unique.append((str(session_id), f"s{len(unique):02d}", str(date), turns))
    raw_chars = sum(len(_session_note(neutral, date, turns)) for _, neutral, date, turns in unique)
    raw_bytes = sum(
        len(_session_note(neutral, date, turns).encode("utf-8"))
        for _, neutral, date, turns in unique
    )
    # Chronological, as the conversations happened; an unparseable date keeps
    # the dataset's order (sorted is stable).
    ordered = sorted(
        enumerate(unique),
        key=lambda item: (
            parse_session_date(item[1][2]) is None,
            parse_session_date(item[1][2]) or 0,
            item[0],
        ),
    )

    engine = HeadlessEngine(
        install_root, extract_cmd=extract_cmd, workdir=workdir, timeout=timeout
    )
    blocked: list[str] = []
    passes: list[dict[str, Any]] = []
    attribution: dict[tuple[str, str], str] = {}
    filed = 0
    resolved: dict[str, Any] = {"accepted": 0, "dropped": 0, "failed": 0, "unaccepted": {}}
    with refuse_engine_model_calls(blocked):
        await engine.start()
        try:
            base = engine.config.workspace_root
            baseline = store_size(engine.vault, base, engine.guide)
            snapshot = store_snapshot(engine.vault, base, engine.guide)
            for _, (session_id, neutral, date, turns) in ordered:
                known = {row["id"] for row in await engine.pending_proposals()}
                record = await engine.ingest_session(
                    f"Chat session {neutral}", turns, parse_session_date(date)
                )
                queued = await engine.pending_proposals()
                new = [row for row in queued if row["id"] not in known]
                filed += len(new)
                outcome = await engine.resolve_proposals(proposals)
                for key in ("accepted", "dropped", "failed"):
                    resolved[key] += outcome[key]
                for kind, count in outcome["unaccepted"].items():
                    resolved["unaccepted"][kind] = resolved["unaccepted"].get(kind, 0) + count
                after = store_snapshot(engine.vault, base, engine.guide)
                added = 0
                for key, lines in after.items():
                    for line in lines - snapshot.get(key, set()):
                        attribution[(key, line)] = session_id
                        added += 1
                snapshot = after
                passes.append(
                    {
                        "session": neutral,
                        "date": date,
                        **record,
                        "proposals_filed": len(new),
                        "lines_added": added,
                    }
                )
                if record["status"] == "failed":
                    # The question is incomplete from here on; later passes
                    # would only spend model calls on a store that is not
                    # what Ciaobot would hold.
                    break
            final = store_size(engine.vault, base, engine.guide)
            preamble = regions_block(engine.guide)
            pending_at_end = len(await engine.pending_proposals())
        finally:
            await engine.close()

    note_sessions: dict[str, list[str]] = {}
    region_sessions: list[str] = []
    surviving: set[str] = set()
    region_keys = {_region_key(engine.guide, region) for region in _region_entries(engine.guide)}
    for key, lines in snapshot.items():
        owners = [attribution[(key, line)] for line in sorted(lines) if (key, line) in attribution]
        surviving.update(owners)
        if key in region_keys:
            region_sessions.extend(owners)
        else:
            note_sessions[key] = list(dict.fromkeys(owners))
    # An abstention question has no answer to retain, whatever its
    # `answer_session_ids` hold.
    gold = (
        []
        if question_stratum(entry) == ABSTENTION
        else [str(item) for item in dict.fromkeys(entry.get("answer_session_ids") or [])]
    )

    failed = [item for item in passes if item["status"] == "failed"]
    error = None
    if failed:
        error = f"{len(failed)} of {len(passes)} memory passes failed: {failed[0].get('error', '')}"
    elif blocked:
        error = f"the engine attempted {len(blocked)} model call(s) outside --extract-cmd"
    return {
        "haystack_sessions": len(unique),
        "sessions": len(passes),
        "passes_ok": sum(1 for item in passes if item["status"] == "ok"),
        "passes_failed": len(failed),
        "passes_skipped": sum(1 for item in passes if item["status"] in {"skipped", "empty"}),
        "sessions_with_writes": sum(1 for item in passes if item["lines_added"]),
        "extract_latency_ms": round(sum(float(item.get("latency_ms") or 0) for item in passes), 1),
        "proposals": {
            "mode": proposals,
            "filed": filed,
            **resolved,
            "pending_at_end": pending_at_end,
        },
        "store": final,
        "baseline_store": baseline,
        "raw_chars": raw_chars,
        "raw_bytes": raw_bytes,
        "evidence_retained": bool(set(gold) & surviving) if gold else None,
        "evidence_sessions_retained": [item for item in gold if item in surviving],
        "evidence_in_context": None,
        "contradictions": None,
        "engine_model_calls_blocked": len(blocked),
        "error": error,
        "passes": passes,
        "_note_sessions": note_sessions,
        "_region_sessions": list(dict.fromkeys(region_sessions)),
        "_preamble": preamble,
        "_vault": engine.vault,
        "_base": engine.config.workspace_root,
    }


@contextlib.contextmanager
def curated_index(
    workdir: Path,
    entry: dict[str, Any],
    *,
    extract_cmd: str,
    proposals: str,
    timeout: float,
) -> Iterator[tuple[sqlite3.Connection, dict[str, Any], str, dict[str, Any]]]:
    """Curate one question's haystack, then index only what the store kept.

    The index is the workspace vault as `fts_search.index_vault` sees it, so
    `Logs/` (the archived transcripts, raw sessions included) is not in it:
    answering reads the notes the passes left and the bounded memory regions,
    which are returned as the preamble every chat loads. The yielded
    `haystack` has the raw-ingest shape, keyed by note path, so the answer
    modes run unchanged.
    """
    if proposals not in PROPOSAL_MODES:
        raise ValueError(f"--proposals must be one of {', '.join(PROPOSAL_MODES)}")
    if not extract_cmd.strip():
        raise ValueError("curated ingest needs --extract-cmd")
    refuse_guide_above(workdir)
    install_root = workdir / "install"
    install_root.mkdir(parents=True, exist_ok=True)
    memory_dir = workdir / "fts"
    memory_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with isolated_ciao_env(install_root, memory_dir):
        curation = asyncio.run(
            _curate(
                install_root,
                entry,
                extract_cmd=extract_cmd,
                proposals=proposals,
                workdir=workdir / "extract",
                timeout=timeout,
            )
        )
    curation["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    preamble = curation.pop("_preamble")
    vault: Path = curation.pop("_vault")
    base: Path = curation.pop("_base")
    note_text = {
        key: path.read_text(encoding="utf-8", errors="replace")
        for key, path in _vault_files(vault, base)
    }
    conn = sqlite3.connect(workdir / "answer-fts.db")
    try:
        fts_search.init_db(conn)
        fts_search.index_vault(conn, vault, path_base=base)
        haystack = {
            "vault": vault,
            "prefix": fts_search.vault_key_prefix(vault, base),
            "path_to_session": {key: key for key in note_text},
            "session_date": {key: "" for key in note_text},
            "note_text": note_text,
        }
        yield conn, haystack, preamble, curation
    finally:
        conn.close()


# ── answering and judging ───────────────────────────────────────────────────


def clean_env() -> dict[str, str]:
    """The November run's environment rule: no provider API keys anywhere near
    the answerer or judge, so a subscription CLI cannot silently become an API
    call."""
    env = dict(os.environ)
    for key in list(env):
        if key.endswith("_API_KEY"):
            del env[key]
    return env


def run_template(
    template: str, prompt: str, workdir: Path, timeout: float
) -> tuple[str, dict[str, Any]]:
    """Run one operator command with the prompt in a file and read its answer.

    The template may write the reply to `{out_file}` (as the defaults do) or
    print it; a file wins when it is non-empty. Placeholder substitution is a
    literal replace, not `str.format`, so a template containing braces or JSON
    survives.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    prompt_file = workdir / "prompt.txt"
    out_file = workdir / "response.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    if out_file.exists():
        out_file.unlink()
    command = template.replace("{prompt_file}", shlex.quote(str(prompt_file))).replace(
        "{out_file}", shlex.quote(str(out_file))
    )

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            env=clean_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", {
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"command timed out after {timeout:g}s",
        }

    reply = completed.stdout
    if out_file.is_file():
        written = out_file.read_text(encoding="utf-8", errors="replace")
        if written.strip():
            reply = written
    reply = reply.strip()
    info: dict[str, Any] = {
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "exit_code": completed.returncode,
        "stderr": completed.stderr.strip()[-2000:],
    }
    if completed.returncode != 0:
        info["error"] = f"command exited {completed.returncode}"
    elif not reply:
        info["error"] = "command produced no output"
    return reply, info


def answer_prompt(history: str, question_date: str, question: str) -> str:
    return ANSWER_TEMPLATE.format(history, question_date, question)


def _chronological(
    hits: list[dict[str, Any]], haystack: dict[str, Any]
) -> list[dict[str, Any]]:
    # run_generation.py sorts the retrieved sessions by date before rendering.
    return sorted(
        hits, key=lambda hit: haystack["session_date"].get(hit["session_id"], "")
    )


def _note_block(
    label: str, hit: dict[str, Any], haystack: dict[str, Any]
) -> str:
    session_id = hit["session_id"]
    date = haystack["session_date"].get(session_id, "")
    return (
        f"### Session {label}:\nSession Date: {date}\nSession Content:\n"
        f"{haystack['note_text'][session_id]}"
    )


def build_context(
    haystack: dict[str, Any],
    hits: list[dict[str, Any]],
    mode: str,
    preamble: str = "",
) -> str:
    """The retrieved context handed to the answer model, in one call.

    `snippets` sends the FTS-highlighted lines (Ciaobot's `vault search` view);
    `notes` sends the full Markdown note. Both use LongMemEval's
    "### Session / Session Date / Session Content" wrapper. `preamble` is
    context every chat loads before any search (curated ingest's bounded
    memory regions); raw ingest has none and passes nothing.
    """
    blocks = [preamble] if preamble else []
    for number, hit in enumerate(_chronological(hits, haystack), start=1):
        if mode == "snippets":
            body = hit["title"].strip()
            snippet = hit["snippet"].strip()
            if snippet:
                body = f"{body}: {snippet}"
        else:
            body = haystack["note_text"][hit["session_id"]]
        blocks.append(
            f"### Session {number}:\nSession Date: "
            f"{haystack['session_date'].get(hit['session_id'], '')}\n"
            f"Session Content:\n{body}"
        )
    return "\n\n".join(blocks)


def agentic_prompt(
    history: str, question_date: str, question: str, remaining: int
) -> str:
    instructions = AGENTIC_INSTRUCTIONS.replace("{remaining}", str(remaining))
    return (
        f"{instructions}\n\nHistory Chats:\n\n{history}\n\n"
        f"Current Date: {question_date}\nQuestion: {question}\n"
    )


def parse_agent_reply(text: str) -> tuple[str, str]:
    final = _FINAL_RE.search(text)
    if final:
        return "final", final.group(1).strip()
    search = _SEARCH_RE.search(text)
    if search:
        return "search", search.group(1).strip().splitlines()[0].strip()
    # No marker: treat the whole reply as the answer rather than losing it.
    return "final", text.strip()


def _search_page(
    hits: list[dict[str, Any]],
    haystack: dict[str, Any],
    shown: set[str],
) -> str:
    parts = []
    for hit in _chronological(hits, haystack):
        session_id = hit["session_id"]
        if session_id in shown:
            parts.append(
                f"### Session {session_id}:\n(already shown above; not repeated)"
            )
            continue
        shown.add(session_id)
        parts.append(_note_block(session_id, hit, haystack))
    return "\n\n".join(parts)


def agentic_answer(
    entry: dict[str, Any],
    haystack: dict[str, Any],
    conn: sqlite3.Connection,
    initial_hits: list[dict[str, Any]],
    *,
    answer_cmd: str,
    workdir: Path,
    timeout: float,
    preamble: str = "",
) -> tuple[str, dict[str, Any]]:
    """Up to four searches in all, answering from the full notes.

    Blocks are labelled with the neutral session id (`s03`) rather than a
    position number so the model can name them across turns. A repeated hit is
    marked as already shown instead of being pasted again. `preamble` is the
    always-loaded context of curated ingest; raw ingest passes none.
    """
    shown: set[str] = set()
    pages = [preamble] if preamble else []
    pages.append(_search_page(initial_hits, haystack, shown))
    searches = 1
    calls: list[dict[str, Any]] = []
    error: str | None = None
    answer = ""

    while True:
        remaining = AGENTIC_MAX_SEARCHES - searches
        prompt = agentic_prompt(
            "\n\n".join(pages),
            str(entry.get("question_date", "")),
            str(entry["question"]),
            remaining,
        )
        reply, info = run_template(
            answer_cmd, prompt, workdir / f"call-{len(calls) + 1}", timeout
        )
        calls.append(info)
        if info.get("error") and error is None:
            error = f"answer: {info['error']}"

        kind, payload = parse_agent_reply(reply)
        if kind == "search" and remaining > 0:
            searches += 1
            hits = search_hits(
                conn, payload, haystack["prefix"], haystack["path_to_session"]
            )
            page = f"Search {searches} for: {payload}\n\n"
            page += _search_page(hits, haystack, shown) if hits else "No notes matched."
            pages.append(page)
            continue

        answer = payload
        if kind == "search":
            error = error or (
                "agentic reply asked for a search after the last allowed one"
            )
        break

    return answer, {
        "calls": calls,
        "searches": searches,
        "context_chars": len("\n\n".join(pages)),
        "latency_ms": round(
            sum(float(call.get("latency_ms", 0.0)) for call in calls), 1
        ),
        "error": error,
    }


def judge_template(question_type: str, abstention: bool) -> str:
    if abstention:
        return _JUDGE_ABSTENTION
    if question_type in {
        "single-session-user",
        "single-session-assistant",
        "multi-session",
    }:
        return _JUDGE_SHARED
    if question_type == "temporal-reasoning":
        return _JUDGE_TEMPORAL
    if question_type == "knowledge-update":
        return _JUDGE_KNOWLEDGE
    if question_type == "single-session-preference":
        return _JUDGE_PREFERENCE
    raise ValueError(f"no official judge prompt for question type {question_type!r}")


def judge_answer(
    entry: dict[str, Any],
    answer: str,
    judge_cmd: str,
    workdir: Path,
    timeout: float,
) -> dict[str, Any]:
    """Score one answer with LongMemEval's per-type prompt.

    The official evaluator's rule is `"yes" in response.lower()`. A reply that
    contains neither "yes" nor "no" is recorded as a missing verdict, not as a
    wrong answer: it is a failed judge call and makes the run non-zero.
    """
    abstention = str(entry["question_id"]).endswith("_abs")
    template = judge_template(str(entry["question_type"]), abstention)
    prompt = template.format(
        str(entry["question"]), str(entry.get("answer", "")), answer
    )
    reply, info = run_template(judge_cmd, prompt, workdir, timeout)
    lowered = reply.lower()
    verdict: bool | None
    if "yes" in lowered:
        verdict = True
    elif "no" in lowered:
        verdict = False
    else:
        verdict = None
        info.setdefault("error", "judge reply was neither yes nor no")
    return {
        "verdict": verdict,
        "reply": reply,
        "latency_ms": info.get("latency_ms"),
        "exit_code": info.get("exit_code"),
        "error": info.get("error"),
    }


# ── rows and summary ────────────────────────────────────────────────────────


def _base_row(entry: dict[str, Any], dataset_sha256: str) -> dict[str, Any]:
    return {
        "question_id": str(entry["question_id"]),
        "question_type": str(entry["question_type"]),
        "stratum": question_stratum(entry),
        "question": str(entry["question"]),
        "question_date": str(entry.get("question_date", "")),
        "dataset_sha256": dataset_sha256,
        "mode": "",
        "gold_session_ids": list(
            dict.fromkeys(str(item) for item in entry.get("answer_session_ids") or [])
        ),
        "retrieved_session_ids": [],
        "retrieval": None,
        "answer": "",
        "judge": None,
        "context_chars": 0,
        "context_tokens_est": 0,
        "answer_chars": 0,
        "searches": 1,
        "latency_ms": {},
        "error": None,
    }


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


@contextlib.contextmanager
def open_index(
    workdir: Path,
    entry: dict[str, Any],
    *,
    ingest: str = "raw",
    extract_cmd: str = "",
    proposals: str = "",
    timeout: float = 300.0,
) -> Iterator[tuple[sqlite3.Connection, dict[str, Any], str, dict[str, Any] | None]]:
    """The per-question index for either ingest, as `(conn, haystack, preamble, curation)`.

    Raw ingest is `question_index` unchanged, with no preamble and no curation
    record. Curated ingest feeds the haystack through Ciaobot's memory pass
    first (see `curated_index`).
    """
    if ingest == "raw":
        with question_index(workdir, entry) as (conn, haystack):
            yield conn, haystack, "", None
        return
    if ingest != "curated":
        raise ValueError(f"unknown ingest {ingest!r}")
    with curated_index(
        workdir,
        entry,
        extract_cmd=extract_cmd,
        proposals=proposals,
        timeout=timeout,
    ) as (conn, haystack, preamble, curation):
        yield conn, haystack, preamble, curation


def _record_retrieval(
    row: dict[str, Any],
    hits: list[dict[str, Any]],
    curation: dict[str, Any] | None,
) -> None:
    """Fill the retrieval fields of a row from the top hits.

    Raw ingest: hits are sessions. Curated ingest: hits are notes, and a gold
    session counts as retrieved when a top note holds a line its pass wrote
    (see `score_curated_retrieval`).
    """
    top = hits[:SCORED_AT]
    if curation is None:
        row["retrieved_session_ids"] = [hit["session_id"] for hit in top]
        row["retrieval"] = score_retrieval(
            row["retrieved_session_ids"], row["gold_session_ids"]
        )
        return
    keys = [hit["session_id"] for hit in top]
    note_sessions = curation.pop("_note_sessions", {})
    row["retrieved_note_paths"] = keys
    row["retrieved_session_ids"] = list(
        dict.fromkeys(
            session for key in keys for session in note_sessions.get(key, [])
        )
    )
    row["retrieval"] = score_curated_retrieval(
        keys, note_sessions, row["gold_session_ids"]
    )
    gold = set(row["gold_session_ids"])
    if gold and row["stratum"] != ABSTENTION:
        in_context = set(row["retrieved_session_ids"]) | set(
            curation.get("_region_sessions", [])
        )
        curation["evidence_in_context"] = bool(gold & in_context)
    curation.pop("_region_sessions", None)


def _curated_row_fields(
    row: dict[str, Any], curation: dict[str, Any] | None, proposals: str
) -> None:
    if curation is None:
        return
    row["ingest"] = "curated"
    row["proposals"] = proposals
    row["curation"] = curation
    row["latency_ms"]["curate"] = curation.get("latency_ms")
    if curation.get("error"):
        row["error"] = f"curate: {curation['error']}"
        curation.pop("_note_sessions", None)
        curation.pop("_region_sessions", None)


def retrieve_for_question(
    entry: dict[str, Any],
    *,
    dataset_sha256: str = "",
    temp_root: Path | None = None,
    ingest: str = "raw",
    extract_cmd: str = "",
    proposals: str = "",
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Index one question's haystack and record the top-5 retrieval.

    No answer model. Curated ingest still runs the extracting command once per
    session.
    """
    row = _base_row(entry, dataset_sha256)
    row["mode"] = "retrieval"
    with _temporary_dir(temp_root) as raw:
        workdir = Path(raw)
        started = time.perf_counter()
        try:
            with open_index(
                workdir,
                entry,
                ingest=ingest,
                extract_cmd=extract_cmd,
                proposals=proposals,
                timeout=timeout,
            ) as (conn, haystack, _preamble, curation):
                row["latency_ms"]["index"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
                _curated_row_fields(row, curation, proposals)
                if not row["error"]:
                    search_started = time.perf_counter()
                    hits = search_hits(
                        conn,
                        row["question"],
                        haystack["prefix"],
                        haystack["path_to_session"],
                    )
                    row["latency_ms"]["search"] = round(
                        (time.perf_counter() - search_started) * 1000, 1
                    )
                    _record_retrieval(row, hits, curation)
        except Exception as exc:  # noqa: BLE001 - a bad question must not kill the run.
            row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def run_question(
    entry: dict[str, Any],
    *,
    mode: str,
    answer_cmd: str,
    judge_cmd: str,
    timeout: float,
    dataset_sha256: str = "",
    temp_root: Path | None = None,
    ingest: str = "raw",
    extract_cmd: str = "",
    proposals: str = "",
) -> dict[str, Any]:
    """Retrieve, answer with `mode`, and judge one question."""
    row = _base_row(entry, dataset_sha256)
    row["mode"] = mode
    with _temporary_dir(temp_root) as raw:
        workdir = Path(raw)
        try:
            started = time.perf_counter()
            with open_index(
                workdir,
                entry,
                ingest=ingest,
                extract_cmd=extract_cmd,
                proposals=proposals,
                timeout=timeout,
            ) as (conn, haystack, preamble, curation):
                row["latency_ms"]["index"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
                _curated_row_fields(row, curation, proposals)
                if row["error"]:
                    # The store is not what Ciaobot would hold, so the question
                    # is incomplete rather than answered over a partial vault.
                    row["judge"] = {
                        "verdict": None,
                        "reply": "",
                        "error": "skipped: curation failed",
                    }
                    return row
                search_started = time.perf_counter()
                hits = search_hits(
                    conn,
                    row["question"],
                    haystack["prefix"],
                    haystack["path_to_session"],
                )
                row["latency_ms"]["search"] = round(
                    (time.perf_counter() - search_started) * 1000, 1
                )
                top = hits[:SCORED_AT]
                _record_retrieval(row, hits, curation)

                if mode == "agentic":
                    answer, info = agentic_answer(
                        entry,
                        haystack,
                        conn,
                        top,
                        answer_cmd=answer_cmd,
                        workdir=workdir / "answer",
                        timeout=timeout,
                        preamble=preamble,
                    )
                    row["latency_ms"]["answer"] = info["latency_ms"]
                    row["context_chars"] = info["context_chars"]
                    row["searches"] = info["searches"]
                    if info["error"]:
                        row["error"] = info["error"]
                else:
                    context = build_context(haystack, top, mode, preamble)
                    prompt = answer_prompt(
                        context,
                        str(entry.get("question_date", "")),
                        row["question"],
                    )
                    answer, info = run_template(
                        answer_cmd, prompt, workdir / "answer", timeout
                    )
                    row["latency_ms"]["answer"] = info.get("latency_ms")
                    row["context_chars"] = len(context)
                    if info.get("error"):
                        row["error"] = f"answer: {info['error']}"

                row["answer"] = answer
                row["answer_chars"] = len(answer)
                row["context_tokens_est"] = round(row["context_chars"] / 4)

                if row["error"]:
                    row["judge"] = {
                        "verdict": None,
                        "reply": "",
                        "error": "skipped: answer failed",
                    }
                else:
                    row["judge"] = judge_answer(
                        entry, answer, judge_cmd, workdir / "judge", timeout
                    )
                    row["latency_ms"]["judge"] = row["judge"].get("latency_ms")
                    if row["judge"].get("error"):
                        row["error"] = f"judge: {row['judge']['error']}"
        except Exception as exc:  # noqa: BLE001 - counted as incomplete, not dropped.
            row["error"] = f"{type(exc).__name__}: {exc}"
    return row
def run_retrieval_only(
    entries: list[dict[str, Any]],
    out_path: Path,
    *,
    dataset_sha256: str = "",
    temp_root: Path | None = None,
) -> list[dict[str, Any]]:
    """The model-free retrieval path, written row by row as it goes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("", encoding="utf-8")
    rows = []
    for entry in entries:
        row = retrieve_for_question(
            entry, dataset_sha256=dataset_sha256, temp_root=temp_root
        )
        append_jsonl(out_path, row)
        rows.append(row)
    return rows


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(rows: list[dict[str, Any]], *, qa: bool) -> dict[str, Any]:
    """Aggregate a run without dropping a question.

    Per type, `fraction` is correct / n, so an incomplete question lowers the
    score and shows up in `judged < n` as well as in `incomplete`. Retrieval
    denominators cover answerable questions only; `answerable - n` is the count
    that errored before a retrieval could be scored.
    """
    by_type: dict[str, dict[str, int]] = {}
    judge_missing = 0
    for row in rows:
        name = str(row["stratum"])
        bucket = by_type.setdefault(name, {"n": 0, "correct": 0, "judged": 0})
        bucket["n"] += 1
        judge = row.get("judge") or {}
        verdict = judge.get("verdict")
        if qa:
            # A verdict read off a command that failed is not a judgement; the
            # case is incomplete, not wrong.
            if verdict is None or row.get("error"):
                judge_missing += 1
            else:
                bucket["judged"] += 1
                bucket["correct"] += 1 if verdict else 0

    accuracy = None
    if qa:
        judged = sum(bucket["judged"] for bucket in by_type.values())
        correct = sum(bucket["correct"] for bucket in by_type.values())
        accuracy = {
            "n": len(rows),
            "correct": correct,
            "judged": judged,
            "fraction": correct / len(rows) if rows else 0.0,
        }

    answerable = [row for row in rows if row.get("gold_session_ids")]
    scored = [row["retrieval"] for row in answerable if row.get("retrieval")]
    retrieval = {
        "n": len(scored),
        "answerable": len(answerable),
        "strict": _mean([float(item["strict"]) for item in scored]) or 0.0,
        "strict_count": sum(1 for item in scored if item["strict"] == 1.0),
        "fractional": _mean([float(item["fractional"]) for item in scored]) or 0.0,
        "any": _mean([float(item["any"]) for item in scored]) or 0.0,
        "any_count": sum(1 for item in scored if item["any"] == 1.0),
    }

    incomplete: list[dict[str, str]] = []
    for row in rows:
        if row.get("error"):
            reason = str(row["error"])
        elif qa and (row.get("judge") or {}).get("verdict") is None:
            reason = "judge verdict missing"
        else:
            continue
        incomplete.append(
            {"question_id": str(row["question_id"]), "reason": reason}
        )

    context_values = [
        float(row["context_tokens_est"])
        for row in rows
        if float(row.get("context_tokens_est") or 0) > 0
    ]
    answer_latency = [
        float(row["latency_ms"]["answer"])
        for row in rows
        if isinstance(row.get("latency_ms"), dict)
        and row["latency_ms"].get("answer") is not None
    ]
    judge_latency = [
        float(row["latency_ms"]["judge"])
        for row in rows
        if isinstance(row.get("latency_ms"), dict)
        and row["latency_ms"].get("judge") is not None
    ]

    summary = {
        "n": len(rows),
        "accuracy": accuracy,
        "by_type": {
            name: {
                **bucket,
                "fraction": bucket["correct"] / bucket["n"] if bucket["n"] else 0.0,
            }
            for name, bucket in by_type.items()
        },
        "retrieval": retrieval,
        "judge_missing": judge_missing,
        "incomplete": incomplete,
        "context_tokens_est": _mean(context_values),
        "answer_latency_ms": _mean(answer_latency),
        "judge_latency_ms": _mean(judge_latency),
    }
    curated = [row["curation"] for row in rows if isinstance(row.get("curation"), dict)]
    if curated:
        summary["curation"] = summarize_curation(rows, curated, summary)
    return summary


def summarize_curation(
    rows: list[dict[str, Any]],
    curated: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """What curated ingest kept, across the run. Only present for curated rows.

    Pass counts cover every curated question. Store, proposal and evidence
    figures cover only the questions whose curation completed: a failed one
    stopped at the failing session, so its store is not what Ciaobot would
    hold, and it is already counted as incomplete.
    """
    complete = [item for item in curated if not item.get("error")]

    def mean_of(path: tuple[str, ...]) -> float | None:
        values: list[float] = []
        for item in complete:
            value: Any = item
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if isinstance(value, (int, float)):
                values.append(float(value))
        return _mean(values)

    def store_chars(store: dict[str, Any] | None) -> float:
        store = store or {}
        return (
            float(store.get("note_chars") or 0)
            + float(store.get("workspace_chars") or 0)
            + float(sum((store.get("region_chars") or {}).values()))
        )

    stored: list[float] = []
    added: list[float] = []
    ratios: list[float] = []
    for item in complete:
        chars = store_chars(item.get("store"))
        # A fresh install already holds its General project doc; what the
        # passes added is what compares with the raw dump.
        grown = chars - store_chars(item.get("baseline_store"))
        stored.append(chars)
        added.append(grown)
        if item.get("raw_chars"):
            ratios.append(grown / float(item["raw_chars"]))

    retained = [
        item["evidence_retained"] for item in complete if item.get("evidence_retained") is not None
    ]
    in_context = [
        item["evidence_in_context"]
        for item in complete
        if item.get("evidence_in_context") is not None
    ]
    unaccepted: dict[str, int] = {}
    for item in complete:
        for kind, count in ((item.get("proposals") or {}).get("unaccepted") or {}).items():
            unaccepted[kind] = unaccepted.get(kind, 0) + int(count)

    def split(name: str) -> dict[str, Any] | None:
        bucket = summary["by_type"].get(name)
        if summary["accuracy"] is None or not bucket:
            return None
        return {key: bucket[key] for key in ("n", "correct", "judged", "fraction")}

    def total(key: str) -> int:
        return sum(int((item.get("proposals") or {}).get(key) or 0) for item in complete)

    return {
        "n": len(curated),
        "complete": len(complete),
        "proposals_mode": sorted({str(row.get("proposals") or "") for row in rows if row.get("curation")}),
        "sessions": sum(int(item.get("sessions") or 0) for item in curated),
        "passes_ok": sum(int(item.get("passes_ok") or 0) for item in curated),
        "passes_failed": sum(int(item.get("passes_failed") or 0) for item in curated),
        "passes_skipped": sum(int(item.get("passes_skipped") or 0) for item in curated),
        "extract_latency_ms": mean_of(("extract_latency_ms",)),
        "notes": mean_of(("store", "notes")),
        "note_chars": mean_of(("store", "note_chars")),
        "workspace_chars": mean_of(("store", "workspace_chars")),
        "region_chars_memory": mean_of(("store", "region_chars", "memory")),
        "region_chars_profile": mean_of(("store", "region_chars", "profile")),
        "stored_chars": _mean(stored),
        "added_chars": _mean(added),
        "raw_chars": mean_of(("raw_chars",)),
        "stored_to_raw": _mean(ratios),
        "proposals_filed": total("filed"),
        "proposals_accepted": total("accepted"),
        "proposals_dropped": total("dropped"),
        "proposals_failed": total("failed"),
        "proposals_unaccepted": unaccepted,
        "proposals_pending_at_end": mean_of(("proposals", "pending_at_end")),
        "evidence_retained": sum(1 for value in retained if value),
        "evidence_answerable": len(retained),
        "evidence_in_context": sum(1 for value in in_context if value),
        "evidence_in_context_n": len(in_context),
        "knowledge_update": split("knowledge-update"),
        "abstention": split(ABSTENTION),
        "contradictions": None,
        "engine_model_calls_blocked": sum(
            int(item.get("engine_model_calls_blocked") or 0) for item in curated
        ),
    }


def _format_curation(curation: dict[str, Any]) -> list[str]:
    def num(value: Any, spec: str = ".0f") -> str:
        return "n/a" if value is None else format(value, spec)

    def ratio(count: int, n: int) -> str:
        return f"{_pct(count / n)} ({count}/{n})" if n else "n/a (no answerable questions)"

    lines = [
        "Curated ingest (proposals: " + ", ".join(curation["proposals_mode"]) + ")",
        (
            f"  questions curated completely: {curation['complete']} of {curation['n']} "
            "(store, proposal and evidence figures cover these only)"
        ),
        (
            f"  memory passes: {curation['passes_ok']} ok, "
            f"{curation['passes_failed']} failed, {curation['passes_skipped']} "
            f"skipped, of {curation['sessions']} sessions"
        ),
        (
            f"  stored per question (mean): {num(curation['notes'], '.1f')} notes, "
            f"{num(curation['note_chars'])} note chars, "
            f"{num(curation['workspace_chars'])} Workspace/ chars, regions "
            f"{num(curation['region_chars_memory'])} + "
            f"{num(curation['region_chars_profile'])} chars"
        ),
        (
            f"  stored vs raw dump: {num(curation['stored_chars'])} chars stored, "
            f"{num(curation['added_chars'])} added by the passes, vs "
            f"{num(curation['raw_chars'])} raw (added/raw "
            f"{num(curation['stored_to_raw'], '.3f')})"
        ),
        (
            f"  proposals: {curation['proposals_filed']} filed, "
            f"{curation['proposals_accepted']} accepted, "
            f"{curation['proposals_dropped']} dropped, "
            f"{curation['proposals_failed']} failed to resolve, "
            f"{num(curation['proposals_pending_at_end'], '.1f')} pending at end (mean)"
        ),
    ]
    if curation["proposals_unaccepted"]:
        kinds = ", ".join(
            f"{kind} {count}" for kind, count in sorted(curation["proposals_unaccepted"].items())
        )
        lines.append(f"  left queued, not auto-acceptable: {kinds}")
    lines.append(
        "  retained evidence (an answer session's pass wrote a line still stored): "
        + ratio(curation["evidence_retained"], curation["evidence_answerable"])
    )
    lines.append(
        "  evidence in answer context (top notes or regions): "
        + ratio(curation["evidence_in_context"], curation["evidence_in_context_n"])
    )
    for label, key in (("knowledge-update", "knowledge_update"), ("abstention", "abstention")):
        bucket = curation.get(key)
        if bucket:
            lines.append(
                f"  {label} accuracy: {_pct(bucket['fraction'])} "
                f"({bucket['correct']}/{bucket['n']}, {bucket['judged']} judged)"
            )
    lines.append("  contradictory or superseded facts left behind: not measured")
    if curation["engine_model_calls_blocked"]:
        lines.append(
            f"  engine model calls blocked: {curation['engine_model_calls_blocked']}"
        )
    return lines


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _type_order(by_type: dict[str, Any]) -> list[str]:
    known = [name for name, _ in STRATA if name in by_type]
    extra = sorted(set(by_type) - set(known))
    return known + extra


def format_summary(summary: dict[str, Any], *, mode: str) -> str:
    lines: list[str] = []
    abstention = summary["by_type"].get(ABSTENTION, {}).get("n", 0)
    lines.append(f"Questions: {summary['n']} ({abstention} abstention)")
    lines.append(f"Mode: {mode}")
    lines.append("")

    accuracy = summary["accuracy"]
    if accuracy is not None:
        lines.append(
            f"{'type':<26}{'n':>4}{'correct':>9}{'judged':>8}{'acc':>9}"
        )
        for name in _type_order(summary["by_type"]):
            bucket = summary["by_type"][name]
            lines.append(
                f"{name:<26}{bucket['n']:>4}{bucket['correct']:>9}"
                f"{bucket['judged']:>8}{_pct(bucket['fraction']):>9}"
            )
        lines.append(
            f"{'TOTAL':<26}{accuracy['n']:>4}{accuracy['correct']:>9}"
            f"{accuracy['judged']:>8}{_pct(accuracy['fraction']):>9}"
        )
        lines.append(f"Judge missing or failed: {summary['judge_missing']}")
        lines.append("")

    retrieval = summary["retrieval"]
    if retrieval["answerable"]:
        lines.append(
            f"Retrieval @{SCORED_AT}, answerable questions: {retrieval['n']} of "
            f"{retrieval['answerable']} scored"
        )
        lines.append(
            f"  strict@{SCORED_AT}      {_pct(retrieval['strict'])} "
            f"({retrieval['strict_count']}/{retrieval['n']})"
        )
        lines.append(f"  fractional@{SCORED_AT}  {_pct(retrieval['fractional'])}")
        lines.append(
            f"  any-hit@{SCORED_AT}     {_pct(retrieval['any'])} "
            f"({retrieval['any_count']}/{retrieval['n']})"
        )
    else:
        lines.append("Retrieval: no answerable questions in this run")

    if summary["context_tokens_est"] is not None:
        lines.append(
            f"Context: mean {summary['context_tokens_est']:.0f} tokens estimated "
            "(characters / 4)"
        )
    if summary["answer_latency_ms"] is not None:
        lines.append(f"Answer latency: mean {summary['answer_latency_ms']:.0f} ms")
    if summary["judge_latency_ms"] is not None:
        lines.append(f"Judge latency: mean {summary['judge_latency_ms']:.0f} ms")
    lines.append("")

    if summary.get("curation"):
        lines.extend(_format_curation(summary["curation"]))
        lines.append("")

    if summary["incomplete"]:
        lines.append(f"Incomplete: {len(summary['incomplete'])}")
        for item in summary["incomplete"]:
            lines.append(f"  - {item['question_id']}: {item['reason']}")
    else:
        lines.append("Incomplete: 0")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the LongMemEval-S memory benchmark against Ciaobot's FTS index, "
            "the protocol in docs/evals/longmemeval-protocol.md."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="Official longmemeval_s_cleaned.json (never bundled or downloaded).",
    )
    parser.add_argument(
        "--questions",
        type=Path,
        help="JSON array of question ids to run; exactly what the run asks, in order.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        help="Stratified draw of N questions (60 reproduces the November sample).",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help="Sampling seed."
    )
    parser.add_argument(
        "--mode",
        choices=("snippets", "notes", "agentic"),
        default="notes",
        help="How the answer model reads the retrieved notes.",
    )
    parser.add_argument(
        "--answer-cmd",
        default=DEFAULT_ANSWER_CMD,
        help="Shell command with {prompt_file} and {out_file} placeholders.",
    )
    parser.add_argument(
        "--judge-cmd",
        default=DEFAULT_JUDGE_CMD,
        help="Shell command with {prompt_file} and {out_file} placeholders.",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Per-question JSONL output."
    )
    parser.add_argument(
        "--questions-out",
        type=Path,
        help="Write the selected question ids here, so a run can be replayed.",
    )
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="Index and search only; no answer model, no judge.",
    )
    parser.add_argument(
        "--allow-other-dataset",
        action="store_true",
        help="Accept a dataset whose sha256 is not the documented one.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Seconds allowed per answer or judge command.",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        help="Parent for per-question temporary directories (default: system temp).",
    )
    parser.add_argument(
        "--ingest",
        choices=("raw", "curated"),
        default="raw",
        help=(
            "raw: every session is a note (the November protocol). curated: each "
            "session is archived and run through Ciaobot's memory pass, and "
            "answering reads only what the pass kept."
        ),
    )
    parser.add_argument(
        "--extract-cmd",
        default="",
        help=(
            "Curated ingest only, required: the agent command that runs each memory "
            "pass. Placeholders {prompt_file}, {out_file}, {system_prompt_file}, "
            "{disallowed_tools}; it runs in the temp install's agent root with the "
            "pass's environment."
        ),
    )
    parser.add_argument(
        "--proposals",
        choices=PROPOSAL_MODES,
        default=None,
        help=(
            "Curated ingest only, required: what happens to proposals a pass queues "
            "for review. accept: promote every deterministic kind after each pass; "
            "drop: dismiss every proposal; leave: keep them queued."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sample is not None and args.questions is not None:
        raise SystemExit("pass --sample or --questions, not both")
    if args.sample is not None and args.sample < 1:
        raise SystemExit("--sample must be at least 1")
    if args.ingest == "curated":
        if not args.extract_cmd.strip():
            raise SystemExit("--ingest curated needs --extract-cmd")
        if args.proposals is None:
            raise SystemExit(
                "--ingest curated needs --proposals accept|drop|leave: a proposal "
                "waits for a person, and the run must say what that person does"
            )
        try:
            refuse_guide_above(Path(args.temp_dir or tempfile.gettempdir()))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.extract_cmd or args.proposals is not None:
        raise SystemExit("--extract-cmd and --proposals apply to --ingest curated only")

    dataset_sha256 = verify_dataset(
        args.dataset, allow_other=args.allow_other_dataset
    )
    entries = load_dataset(args.dataset)
    if args.questions is not None:
        selected = select_by_ids(entries, load_question_ids(args.questions))
    elif args.sample is not None:
        selected = select_questions(entries, args.sample, args.seed)
    else:
        selected = entries

    if args.questions_out is not None:
        args.questions_out.parent.mkdir(parents=True, exist_ok=True)
        args.questions_out.write_text(
            json.dumps(
                [str(entry["question_id"]) for entry in selected], indent=2
            )
            + "\n",
            encoding="utf-8",
        )

    out_path = args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("", encoding="utf-8")

    print(f"Dataset: {args.dataset} sha256 {dataset_sha256}")
    print(f"Questions: {len(selected)}")
    if args.retrieval_only:
        print("Mode: retrieval only (no answer or judge calls)")
    else:
        print(f"Mode: {args.mode}")
    curated: dict[str, Any] = {}
    if args.ingest == "curated":
        print(f"Ingest: curated (proposals: {args.proposals})")
        curated = {
            "ingest": "curated",
            "extract_cmd": args.extract_cmd,
            "proposals": args.proposals,
        }

    rows: list[dict[str, Any]] = []
    wall_started = time.perf_counter()
    for position, entry in enumerate(selected, start=1):
        if args.retrieval_only:
            row = retrieve_for_question(
                entry,
                dataset_sha256=dataset_sha256,
                temp_root=args.temp_dir,
                **({**curated, "timeout": args.timeout} if curated else {}),
            )
        else:
            row = run_question(
                entry,
                mode=args.mode,
                answer_cmd=args.answer_cmd,
                judge_cmd=args.judge_cmd,
                timeout=args.timeout,
                dataset_sha256=dataset_sha256,
                temp_root=args.temp_dir,
                **curated,
            )
        append_jsonl(out_path, row)
        rows.append(row)
        status = "error" if row["error"] else "ok"
        print(
            f"[{position}/{len(selected)}] {row['question_id']} {status}",
            file=sys.stderr,
            flush=True,
        )

    summary = summarize(rows, qa=not args.retrieval_only)
    print()
    print(
        format_summary(
            summary, mode="retrieval only" if args.retrieval_only else args.mode
        )
    )
    print()
    print(f"Rows: {out_path.resolve()}")
    print(f"Wall time: {time.perf_counter() - wall_started:.1f}s")
    return 1 if summary["incomplete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
