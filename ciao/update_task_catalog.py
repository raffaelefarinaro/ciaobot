"""The packaged "After this update" task catalog: types, loading, validation.

What this is
------------
``ciao/operator_actions.py`` already detects conditions about an install — an
update waiting, a vault still in an old place, a retired vocabulary still in
use — and renders them as Home cards. What it has no notion of is the
follow-up work *this update* asks for: a versioned task with a stable id, the
engine version it needs, the detector that decides whether it applies, the
check that decides whether it is done, and the prompt that does it. This module
is the contract for that catalog, and nothing else.

It is deliberately inert
-----------------------
* Pure loading and validation. No filesystem write, no vault read, no network,
  no model call.
* Total. A missing file, malformed JSON, a row of the wrong shape and a field
  of the wrong type are ``Diagnostic`` values, never exceptions: a bad packaged
  catalog must not crash a Home render or a startup path, and it must never be
  reported as "the tasks are done".
* Names, not callables. ``DETECTORS`` and ``COMPLETION_CHECKS`` are the
  allowed vocabulary **and** the implementation registry: a name in one of them
  is a name ``ciao.update_tasks`` has code for, and a task row may only name one
  of those. A name with no code is an ``unknown_detector`` /
  ``unknown_completion_check`` error, and the row is dropped — which is the right
  answer now that the registries are not empty, because the provisional case they
  were shaped for (a catalog and its prompts shipping ahead of the code that acts
  on them) is over. The first task to ship here, ``learnings-cleanup`` (#728-E),
  is implemented in the same change: a task that cannot be applied is a row
  offering the operator follow-up work this engine has no way to do, and the
  runtime already answers that case honestly as ``unknown`` with a
  ``detector_not_implemented`` reason.

What it does not do
-------------------
Applicability detection, per-scope state and chat launch belong to the
follow-up children of #729. This module answers two questions and no more:
which task definitions does this engine understand, and which of them does the
installed version support.

Version rule
------------
``parse_version`` is deliberately the app's existing release ordering rather
than a new scheme: it applies the token rule ``package_version._version_key``
uses (digits and letter runs compared pairwise, numbers sorting after letter
runs, separators dropped, a longer key outranking a prefix) to a spelling
whose leading ``v`` is dropped first. Gating that disagreed with the update
check would let the two answer different questions about the same install, and
the tests pin the two orderings together.

Two consequences are worth stating rather than hiding. It is not PEP 440: a
prerelease such as ``1.2.0rc1`` sorts *after* ``1.2.0`` here, exactly as the
update check already orders it. And build metadata is an ordinary trailing
token, so ``1.2.0+build.5`` does not sort below ``1.2.0`` — the direction that
makes an installed build of a release still eligible for that release's tasks.
"""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

#: The package the catalog ships in. Resolved through ``importlib.resources``
#: so a prompt is read from installed package data, not from a checkout path.
STOCK_PACKAGE = "ciao.stock"

#: Directory under ``ciao/stock`` holding ``catalog.json`` and ``prompts/``.
UPDATE_TASKS_DIRNAME = "update-tasks"

#: The catalog file, relative to that directory. Its top level is a JSON list.
CATALOG_FILENAME = "catalog.json"

#: A task's scope: one logical workspace, or the whole install (one instance).
SCOPES: frozenset[str] = frozenset({"workspace", "install"})

#: The whole of a catalog row's schema. A key outside it is a defect rather than
#: something to ignore: a misspelled ``depends_on`` is a task that silently
#: loaded with no prerequisites, which is the one answer a catalog must not give.
TASK_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "revision",
        "since_version",
        "scope",
        "title",
        "why",
        "detector",
        "completion_check",
        "prompt_resource",
        "depends_on",
    }
)

#: The detector names this engine implements. A task row may only name one of
#: these, and every name here has a function behind it in
#: :data:`ciao.update_tasks.DETECTOR_FUNCTIONS` — see the module docstring for why
#: "registered but not yet written" is not a state this catalog has.
DETECTORS: frozenset[str] = frozenset(
    {
        "learnings-cleanup-review-needed",
        "unrehomed-people-review-needed",
        "vault-relocate-review-needed",
    }
)

#: The completion checks this engine implements, same contract as ``DETECTORS``.
#: Opening a chat is not completion: a check is a registered postcondition,
#: evaluated apart from any chat the operator starts, and the names here are the
#: postconditions that engine can actually evaluate.
COMPLETION_CHECKS: frozenset[str] = frozenset(
    {
        "learnings-cleanup-review-recorded",
        "unrehomed-people-rehome-recorded",
        "vault-relocate-recorded",
    }
)

#: The documented spelling of a version. A leading ``v`` is tolerated;
#: prerelease/dev suffixes and build metadata are tolerated, and a suffix may
#: repeat (``1.0.1.post1.dev0`` is an ordinary installed version); anything else
#: is not a version and gets a diagnostic rather than a silent ordering.
_VERSION_RE = re.compile(
    r"^v?\d+(?:[._-]\d+)*"
    r"(?:[._-]?(?:a|alpha|b|beta|rc|dev|post)[._-]?\d+)*"
    r"(?:\+[0-9A-Za-z._-]+)?$",
    re.IGNORECASE,
)

# The same tokenizer ``ciao/package_version.py::_version_key`` uses.
_VERSION_PART_RE = re.compile(r"\d+|[A-Za-z]+")

#: A task id is a stable kebab name, not a free-form string.
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Diagnostic severities.
_ERROR = "error"
_WARNING = "warning"

#: Diagnostics that make a row itself unusable, so ``load_catalog`` leaves that
#: row out of the catalog. A task on a cycle or with an unresolvable dependency
#: is one of them: a consumer resolving ``depends_on`` must not be handed a
#: graph it can loop on or a prerequisite that is not in the catalog. A duplicate
#: is deliberately not one: it is a cross-row condition, and which copy stands
#: is decided by file order rather than by the copy's own shape.
ROW_DEFECT_CODES: frozenset[str] = frozenset(
    {
        "invalid_row",
        "invalid_field",
        "invalid_id",
        "invalid_revision",
        "unknown_scope",
        "bad_since_version",
        "invalid_depends_on",
        "unknown_field",
        "unknown_detector",
        "unknown_completion_check",
        "prompt_not_confined",
        "prompt_not_markdown",
        "prompt_missing",
        "prompt_empty",
        "prompt_unreadable",
        "dependency_cycle",
        "unknown_dependency",
    }
)


class UpdateTaskError(ValueError):
    """A task definition could not be turned into a usable prompt read."""


@dataclass(frozen=True, slots=True)
class TaskRef:
    """An exact ``(id, revision)`` reference, the only way to name a dependency."""

    id: str
    revision: int


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One problem with the packaged catalog.

    ``code`` is the stable vocabulary a caller tests on; ``message`` is for a
    human reading a log. ``task_id``/``revision`` locate the row when the
    problem has one (a catalog-wide failure has neither), and ``severity``
    separates a broken row from a definition that merely has no implementation
    behind it yet.
    """

    code: str
    message: str
    task_id: str = ""
    revision: int = 0
    severity: str = _ERROR


@dataclass(frozen=True, slots=True)
class UpdateTask:
    """One versioned "After this update" task definition.

    Inert data. Naming a ``detector`` says how applicability *will* be decided,
    never that it has been decided, and naming a ``completion_check`` says how
    completion *will* be proven. ``prompt_resource`` is a path relative to the
    packaged ``update-tasks`` directory (the convention is
    ``prompts/<id>-<revision>.md``), never a remote URL and never a path into a
    user's workspace: the prompt ships with the engine so it is the same
    instructions offline, and a release-tag link may be documentation, not
    something executed at chat launch.
    """

    id: str
    revision: int
    since_version: str
    scope: str
    title: str
    why: str
    detector: str
    completion_check: str
    prompt_resource: str
    depends_on: tuple[TaskRef, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskCatalog:
    """The loaded definitions plus every diagnostic found while loading them."""

    tasks: tuple[UpdateTask, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def by_id(self) -> Mapping[str, UpdateTask]:
        """The definitions keyed by id. Ids are unique, so one entry each."""
        return {task.id: task for task in self.tasks}

    def eligible(self, installed_version: str) -> tuple[UpdateTask, ...]:
        """Every task this engine version supports, in catalog order.

        Eligibility is a version comparison, not "the newest release": a
        machine that skipped releases still gets the tasks of every release it
        passed, which is why a cumulative catalog is what ships. An installed
        version that does not parse supports nothing — gating cannot prove
        support, and offering a task the engine may not be able to run is the
        worse failure. Excluding a task this version cannot support does not
        remove it from the catalog, so its state survives a downgrade and the
        task returns when the engine is upgraded again.
        """
        installed = parse_version(installed_version)
        if installed is None:
            return ()
        return tuple(
            task
            for task in self.tasks
            if (parse_version(task.since_version) or _UNREADABLE_VERSION) <= installed
        )


def parse_version(value: str) -> tuple[tuple[int, object], ...] | None:
    """Return the sort key for a version spelling, or ``None`` if it is not one.

    See the module docstring: this is ``package_version._version_key`` applied
    to a leading-``v``-stripped spelling, so update-task gating and the update
    check order versions the same way.
    """
    text = (value or "").strip()
    if not _VERSION_RE.fullmatch(text):
        return None
    stripped = text[1:] if text[:1] in ("v", "V") else text
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.lower())
        for part in _VERSION_PART_RE.findall(stripped)
    )


#: The sort key of a version this rule cannot read. It sorts above every key
#: ``parse_version`` produces, because those start with ``(0, …)`` or ``(1, …)``,
#: so a task whose ``since_version`` is unreadable supports nothing — the same
#: answer an unreadable installed version gets, and never a ``TypeError`` out of
#: ``eligible`` on a hand-built catalog. ``load_catalog`` admits no such task.
_UNREADABLE_VERSION: tuple[tuple[int, object], ...] = ((2, ""),)


def packaged_root() -> Path:
    """The packaged ``ciao/stock/update-tasks`` directory.

    Resolved through ``importlib.resources`` rather than relative to this file,
    so the catalog and its prompts are the ones installed with the engine. The
    result is a filesystem path because a task row has to be walked and opened;
    a zipped install is not one Ciaobot ships.
    """
    return Path(str(resources.files(STOCK_PACKAGE).joinpath(UPDATE_TASKS_DIRNAME)))


def validate_catalog(
    tasks: Sequence[UpdateTask], *, root: Path | None = None
) -> list[Diagnostic]:
    """Return the diagnostics for already-parsed definitions.

    These are the cross-row rules: an id and a ``(id, revision)`` pair each
    appear once, a ``prompt_resource`` is confined to the packaged directory
    and present, every detector and completion check is a registered name, and
    every dependency names a known ``(id, revision)`` with no cycles. The
    per-row shape rules (types, scope, version syntax) belong to the row
    parser, because a row that fails them has no definition to validate.

    ``root`` is the packaged directory the tasks were read from; the default
    resolves the packaged one, so a hand-built list is still checked against
    the shipped prompts.
    """
    base = Path(root) if root is not None else packaged_root()
    out: list[Diagnostic] = []

    pairs: set[tuple[str, int]] = set()
    ids: set[str] = set()
    standing: list[UpdateTask] = []
    for task in tasks:
        pair = (task.id, task.revision)
        if pair in pairs:
            # Reported ahead of the id clash below: an exact repeat is a
            # copy-paste, and naming it as such is the more useful answer.
            out.append(
                _diag(
                    "duplicate_revision",
                    f"{_label(task)} is defined more than once at the same revision",
                    task_id=task.id,
                    revision=task.revision,
                )
            )
            continue
        if task.id in ids:
            out.append(
                _diag(
                    "duplicate_id",
                    f"{task.id!r} is defined more than once at different revisions",
                    task_id=task.id,
                    revision=task.revision,
                )
            )
            # Deliberately not added to `pairs`: the first definition of an id
            # is the one that stands, so this copy is not a dependency target.
            continue
        pairs.add(pair)
        ids.add(task.id)
        # The copy that stands, and the only one whose dependencies are read
        # below: a discarded copy's own cycle or missing reference is that
        # copy's defect, and it must not reach the row keyed the same way.
        standing.append(task)
        code = _prompt_defect(base, task.prompt_resource)
        if code:
            out.append(
                _diag(
                    code,
                    f"{_label(task)}: prompt_resource "
                    f"{task.prompt_resource!r} is {code.replace('_', ' ')}",
                    task_id=task.id,
                    revision=task.revision,
                )
            )
        for name, registry, unknown_code, kind in (
            (task.detector, DETECTORS, "unknown_detector", "detector"),
            (
                task.completion_check,
                COMPLETION_CHECKS,
                "unknown_completion_check",
                "completion check",
            ),
        ):
            if name not in registry:
                out.append(
                    _diag(
                        unknown_code,
                        f"{_label(task)}: {kind} {name!r} is not registered in this module",
                        task_id=task.id,
                        revision=task.revision,
                    )
                )

    known = pairs  # every (id, revision) this catalog defines
    for task in standing:
        for ref in task.depends_on:
            if (ref.id, ref.revision) not in known:
                out.append(
                    _diag(
                        "unknown_dependency",
                        f"{_label(task)} depends on {ref.id}@{ref.revision}, "
                        "which the catalog does not define",
                        task_id=task.id,
                        revision=task.revision,
                    )
                )
    out.extend(_dependency_cycles(standing))
    return out


def load_catalog(*, root: Path | None = None) -> TaskCatalog:
    """Load, validate and return the packaged catalog.

    Total by construction, so a malformed catalog degrades to a diagnostic and
    the rows that are still sound. ``root`` points the loader at another
    packaged root, which is how the tests drive it without a checkout-relative
    path; the default resolves the installed one through
    ``importlib.resources``.
    """
    base = Path(root) if root is not None else packaged_root()
    source = base / CATALOG_FILENAME
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return TaskCatalog(
            (), (_diag("catalog_unreadable", f"cannot read {source}: {exc}"),)
        )
    if not isinstance(data, list):
        return TaskCatalog(
            (),
            (
                _diag(
                    "catalog_not_a_list",
                    f"{CATALOG_FILENAME} must hold a list of task objects, got "
                    f"{type(data).__name__}",
                ),
            ),
        )

    parsed: list[UpdateTask] = []
    diagnostics: list[Diagnostic] = []
    for raw in data:
        task, row_diagnostics = _parse_row(raw)
        diagnostics.extend(row_diagnostics)
        if task is not None:
            parsed.append(task)
    diagnostics.extend(validate_catalog(parsed, root=base))

    defective = {
        (d.task_id, d.revision)
        for d in diagnostics
        if d.severity == _ERROR and d.code in ROW_DEFECT_CODES
    }
    kept: list[UpdateTask] = []
    seen_ids: set[str] = set()
    for task in parsed:
        if task.id in seen_ids:
            # A second definition of an id: `validate_catalog` reported it, and
            # the first entry in the file is the one that stands, which is the
            # same answer `by_id` gives.
            continue
        seen_ids.add(task.id)
        if (task.id, task.revision) in defective:
            continue
        kept.append(task)
    kept, dropped = _drop_unresolved_dependencies(kept)
    diagnostics.extend(dropped)
    return TaskCatalog(tuple(kept), tuple(diagnostics))


def _drop_unresolved_dependencies(
    tasks: list[UpdateTask],
) -> tuple[list[UpdateTask], list[Diagnostic]]:
    """Drop tasks whose dependencies are not all in the surviving catalog.

    Run to a fixed point, because dropping one task can strand the task that
    depended on it. Every stranded reference is reported against the task that
    carried it, so a caller sees why its prerequisite is missing rather than
    finding a hole.
    """
    kept = list(tasks)
    diagnostics: list[Diagnostic] = []
    while True:
        available = {(task.id, task.revision) for task in kept}
        survivors: list[UpdateTask] = []
        for task in kept:
            missing = [
                ref
                for ref in task.depends_on
                if (ref.id, ref.revision) not in available
            ]
            if not missing:
                survivors.append(task)
                continue
            for ref in missing:
                diagnostics.append(
                    _diag(
                        "unknown_dependency",
                        f"{_label(task)} depends on {ref.id}@{ref.revision}, "
                        "which was not loaded",
                        task_id=task.id,
                        revision=task.revision,
                    )
                )
        if len(survivors) == len(kept):
            return survivors, diagnostics
        kept = survivors


def read_prompt(task: UpdateTask, *, root: Path | None = None) -> str:
    """Return the packaged prompt text for one task.

    Read through ``importlib.resources`` by default, so the instructions are
    the ones installed with this engine — offline, and from a wheel rather than
    from whatever happens to sit in a checkout. The confinement rule is
    re-applied here rather than trusted from the load, because this is the one
    function that turns a row's string into file contents. ``root`` points the
    reader at another packaged root, which is how the tests read a prompt
    without a checkout-relative path.
    """
    base = Path(root) if root is not None else packaged_root()
    code = _prompt_defect(base, task.prompt_resource)
    if code:
        raise UpdateTaskError(
            f"{_label(task)}: prompt_resource {task.prompt_resource!r} is "
            f"{code.replace('_', ' ')}"
        )
    # Re-checked rather than trusted: a validated resource can still be gone by
    # the time a caller asks for it, and the caller gets one typed failure.
    try:
        return base.joinpath(*PurePosixPath(task.prompt_resource).parts).read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise UpdateTaskError(
            f"{_label(task)}: cannot read prompt_resource "
            f"{task.prompt_resource!r}: {exc}"
        ) from exc


def _diag(
    code: str,
    message: str,
    *,
    task_id: str = "",
    revision: int = 0,
    severity: str = _ERROR,
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        task_id=task_id,
        revision=revision,
        severity=severity,
    )


def _label(task: UpdateTask) -> str:
    return f"{task.id}@{task.revision}" if task.id else "<unnamed task>"


def _prompt_defect(root: Path, resource: str) -> str:
    """Return the diagnostic code for a prompt resource, or ``''`` when it is fine.

    The resource is a path relative to the packaged ``update-tasks`` directory
    and nothing else: not absolute, no ``..``, no component that is a symlink
    (a symlinked prompt could be swapped for anything once validated), a
    markdown file, present and non-empty. Every filesystem probe below is
    total: a name the OS cannot even stat is a diagnostic, never an exception
    out of ``load_catalog``.
    """
    text = (resource or "").strip()
    if not text:
        return "invalid_field"
    relative = PurePosixPath(text)
    if "\0" in text or relative.is_absolute() or text.startswith("~"):
        return "prompt_not_confined"
    if any(part == ".." for part in relative.parts):
        return "prompt_not_confined"
    if relative.suffix != ".md":
        return "prompt_not_markdown"

    target = root.joinpath(*relative.parts)
    try:
        walked = root
        for part in relative.parts:
            walked = walked / part
            if walked.is_symlink():
                return "prompt_not_confined"
        if not target.resolve().is_relative_to(root.resolve()):
            return "prompt_not_confined"
        # stat, not is_file: is_file() answers False for a name Windows cannot
        # even parse (WinError 123), which would read as "missing" there and
        # "unreadable" on POSIX. Only "no such file" is missing.
        try:
            mode = target.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return "prompt_missing"
        if not stat.S_ISREG(mode):
            return "prompt_missing"
        if not target.read_text(encoding="utf-8").strip():
            return "prompt_empty"
    except (OSError, ValueError):
        # ENAMETOOLONG, a symlink loop, an undecodable file, a permission
        # refusal: none of them is a reason to raise on a startup path.
        return "prompt_unreadable"
    return ""


_WHITE = 0
_GREY = 1
_BLACK = 2


def _dependency_cycles(tasks: Sequence[UpdateTask]) -> list[Diagnostic]:
    """A ``dependency_cycle`` diagnostic per task on each cycle.

    Every member is named, not only the one that closed the cycle, so
    ``load_catalog`` can drop the whole cycle rather than leave a consumer to
    walk into it.
    """
    by_pair = {(task.id, task.revision): task for task in tasks}
    colour: dict[tuple[str, int], int] = {}
    found: list[Diagnostic] = []

    def walk(pair: tuple[str, int], path: list[tuple[str, int]]) -> None:
        colour[pair] = _GREY
        task = by_pair[pair]
        for ref in task.depends_on:
            target = (ref.id, ref.revision)
            if target not in by_pair:
                continue  # an unknown reference is its own diagnostic
            state = colour.get(target, _WHITE)
            if state == _GREY:
                # `path` is the current stack and already ends at this task, so
                # the cycle is the tail from the referenced task round to it. The
                # trail keeps the closing hop; the diagnostic list is deduplicated
                # so a self-dependence names its one task once.
                start = path.index(target) if target in path else 0
                closed = [*path[start:], target]
                trail = " -> ".join(_label(by_pair[item]) for item in closed)
                for member in dict.fromkeys(closed):
                    found.append(
                        _diag(
                            "dependency_cycle",
                            f"{_label(by_pair[member])} is on a dependency cycle: {trail}",
                            task_id=by_pair[member].id,
                            revision=by_pair[member].revision,
                        )
                    )
                continue
            if state == _WHITE:
                walk(target, [*path, target])
        colour[pair] = _BLACK

    for task in tasks:
        pair = (task.id, task.revision)
        if colour.get(pair, _WHITE) == _WHITE:
            walk(pair, [pair])
    return found


def _parse_row(raw: Any) -> tuple[UpdateTask | None, list[Diagnostic]]:
    """Turn one catalog entry into a definition, or into diagnostics.

    Every field is type-checked, so one bad row reports all of its own problems
    and never reaches the definition dataclass half-built. A row with any error
    is dropped by the caller; a warning would not be.
    """
    if not isinstance(raw, dict):
        return None, [
            _diag("invalid_row", f"catalog entry {raw!r} is not a task object")
        ]
    out: list[Diagnostic] = []

    task_id, id_diagnostics = _text_field(raw, "id", "<unnamed task>")
    out.extend(id_diagnostics)
    if task_id and not _ID_RE.fullmatch(task_id):
        out.append(
            _diag(
                "invalid_id",
                f"id {task_id!r} is not a kebab-case task id",
                task_id=task_id,
            )
        )
        task_id = ""
    label = task_id or "<unnamed task>"

    raw_revision = raw.get("revision")
    if (
        isinstance(raw_revision, bool)
        or not isinstance(raw_revision, int)
        or raw_revision < 1
    ):
        out.append(
            _diag(
                "invalid_revision",
                f"{label}: revision must be a positive integer, got {raw_revision!r}",
                task_id=task_id,
            )
        )
        revision = 0
    else:
        revision = raw_revision
    label = f"{task_id}@{revision}" if task_id else "<unnamed task>"

    since_version, since_diagnostics = _text_field(raw, "since_version", label)
    out.extend(since_diagnostics)
    if since_version and parse_version(since_version) is None:
        out.append(
            _diag(
                "bad_since_version",
                f"{label}: since_version {since_version!r} is not a version",
                task_id=task_id,
                revision=revision,
            )
        )

    scope, scope_diagnostics = _text_field(raw, "scope", label)
    out.extend(scope_diagnostics)
    if scope and scope not in SCOPES:
        out.append(
            _diag(
                "unknown_scope",
                f"{label}: scope must be one of {sorted(SCOPES)}, got {scope!r}",
                task_id=task_id,
                revision=revision,
            )
        )

    title, title_diagnostics = _text_field(raw, "title", label)
    out.extend(title_diagnostics)
    why, why_diagnostics = _text_field(raw, "why", label)
    out.extend(why_diagnostics)
    detector, detector_diagnostics = _text_field(raw, "detector", label)
    out.extend(detector_diagnostics)
    completion_check, completion_diagnostics = _text_field(
        raw, "completion_check", label
    )
    out.extend(completion_diagnostics)
    prompt_resource, prompt_diagnostics = _text_field(raw, "prompt_resource", label)
    out.extend(prompt_diagnostics)

    depends_on, dependency_diagnostics = _parse_depends_on(
        raw.get("depends_on"), task_id, revision
    )
    out.extend(dependency_diagnostics)

    for key in sorted(key for key in raw if key not in TASK_FIELDS):
        out.append(
            _diag(
                "unknown_field",
                f"{label}: {key!r} is not a field of a task definition; a task "
                f"row carries exactly {', '.join(sorted(TASK_FIELDS))}",
                task_id=task_id,
                revision=revision,
            )
        )

    if any(d.severity == _ERROR for d in out):
        return None, out
    return (
        UpdateTask(
            id=task_id,
            revision=revision,
            since_version=since_version,
            scope=scope,
            title=title,
            why=why,
            detector=detector,
            completion_check=completion_check,
            prompt_resource=prompt_resource,
            depends_on=depends_on,
        ),
        out,
    )


def _text_field(
    raw: dict[str, Any], key: str, label: str
) -> tuple[str, list[Diagnostic]]:
    """Return a required, non-empty string field, or why there isn't one.

    The row label rides in the message rather than in ``task_id``: these run
    while the row is still being parsed, and the id may be the very field that
    is wrong.
    """
    value = raw.get(key)
    if value is None:
        return "", [_diag("invalid_field", f"{label}: {key} is required")]
    if not isinstance(value, str):
        return "", [
            _diag(
                "invalid_field",
                f"{label}: {key} must be a string, got {type(value).__name__}",
            )
        ]
    text = value.strip()
    if not text:
        return "", [_diag("invalid_field", f"{label}: {key} must not be empty")]
    return text, []


def _parse_depends_on(
    value: Any, task_id: str, revision: int
) -> tuple[tuple[TaskRef, ...], list[Diagnostic]]:
    """Parse ``depends_on`` as a list of ``{"id": ..., "revision": ...}`` objects.

    Held to the row's own rules: an id is a kebab string and a revision is a
    positive integer, because a coerced reference would only fail later as an
    unresolvable dependency and hide the row that was actually wrong.
    """
    if value is None:
        return (), []
    label = f"{task_id}@{revision}" if task_id else "<unnamed task>"
    if not isinstance(value, list):
        return (), [
            _diag(
                "invalid_depends_on",
                f"{label}: depends_on must be a list, got {type(value).__name__}",
                task_id=task_id,
                revision=revision,
            )
        ]
    refs: list[TaskRef] = []
    out: list[Diagnostic] = []
    for entry in value:
        if not isinstance(entry, dict):
            out.append(
                _diag(
                    "invalid_depends_on",
                    f"{label}: dependency {entry!r} must be an object with an id "
                    "and a revision",
                    task_id=task_id,
                    revision=revision,
                )
            )
            continue
        raw_id = entry.get("id")
        ref_revision = entry.get("revision")
        ref_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if (
            not ref_id
            or not _ID_RE.fullmatch(ref_id)
            or isinstance(ref_revision, bool)
            or not isinstance(ref_revision, int)
            or ref_revision < 1
        ):
            out.append(
                _diag(
                    "invalid_depends_on",
                    f"{label}: dependency {entry!r} needs a kebab-case id and a "
                    "positive integer revision",
                    task_id=task_id,
                    revision=revision,
                )
            )
            continue
        refs.append(TaskRef(id=ref_id, revision=ref_revision))
    return tuple(refs), out
