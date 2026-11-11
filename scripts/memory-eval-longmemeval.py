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

It never bundles or downloads the dataset, never touches a live vault, and never
writes to the live FTS database: every question gets its own temporary vault and
its own temporary `CIAO_MEMORY_DIR`.

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
import contextlib
import hashlib
import json
import os
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ciao import fts_search  # noqa: E402

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
    haystack: dict[str, Any], hits: list[dict[str, Any]], mode: str
) -> str:
    """The retrieved context handed to the answer model, in one call.

    `snippets` sends the FTS-highlighted lines (Ciaobot's `vault search` view);
    `notes` sends the full Markdown note. Both use LongMemEval's
    "### Session / Session Date / Session Content" wrapper.
    """
    blocks = []
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
) -> tuple[str, dict[str, Any]]:
    """Up to four searches in all, answering from the full notes.

    Blocks are labelled with the neutral session id (`s03`) rather than a
    position number so the model can name them across turns. A repeated hit is
    marked as already shown instead of being pasted again.
    """
    shown: set[str] = set()
    pages = [_search_page(initial_hits, haystack, shown)]
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


def retrieve_for_question(
    entry: dict[str, Any],
    *,
    dataset_sha256: str = "",
    temp_root: Path | None = None,
) -> dict[str, Any]:
    """Index one question's haystack and record the top-5 retrieval. No model."""
    row = _base_row(entry, dataset_sha256)
    row["mode"] = "retrieval"
    with _temporary_dir(temp_root) as raw:
        workdir = Path(raw)
        started = time.perf_counter()
        try:
            with question_index(workdir, entry) as (conn, haystack):
                row["latency_ms"]["index"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
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
                row["retrieved_session_ids"] = [
                    hit["session_id"] for hit in hits[:SCORED_AT]
                ]
                row["retrieval"] = score_retrieval(
                    row["retrieved_session_ids"], row["gold_session_ids"]
                )
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
) -> dict[str, Any]:
    """Retrieve, answer with `mode`, and judge one question."""
    row = _base_row(entry, dataset_sha256)
    row["mode"] = mode
    with _temporary_dir(temp_root) as raw:
        workdir = Path(raw)
        try:
            started = time.perf_counter()
            with question_index(workdir, entry) as (conn, haystack):
                row["latency_ms"]["index"] = round(
                    (time.perf_counter() - started) * 1000, 1
                )
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
                row["retrieved_session_ids"] = [
                    hit["session_id"] for hit in top
                ]
                row["retrieval"] = score_retrieval(
                    row["retrieved_session_ids"], row["gold_session_ids"]
                )

                if mode == "agentic":
                    answer, info = agentic_answer(
                        entry,
                        haystack,
                        conn,
                        top,
                        answer_cmd=answer_cmd,
                        workdir=workdir / "answer",
                        timeout=timeout,
                    )
                    row["latency_ms"]["answer"] = info["latency_ms"]
                    row["context_chars"] = info["context_chars"]
                    row["searches"] = info["searches"]
                    if info["error"]:
                        row["error"] = info["error"]
                else:
                    context = build_context(haystack, top, mode)
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

    return {
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sample is not None and args.questions is not None:
        raise SystemExit("pass --sample or --questions, not both")
    if args.sample is not None and args.sample < 1:
        raise SystemExit("--sample must be at least 1")

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

    rows: list[dict[str, Any]] = []
    wall_started = time.perf_counter()
    for position, entry in enumerate(selected, start=1):
        if args.retrieval_only:
            row = retrieve_for_question(
                entry, dataset_sha256=dataset_sha256, temp_root=args.temp_dir
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
    print("")
    print(
        format_summary(
            summary, mode="retrieval only" if args.retrieval_only else args.mode
        )
    )
    print("")
    print(f"Rows: {out_path.resolve()}")
    print(f"Wall time: {time.perf_counter() - wall_started:.1f}s")
    return 1 if summary["incomplete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
