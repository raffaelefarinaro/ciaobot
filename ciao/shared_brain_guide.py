"""A shared brain's content guide and the destinations it may name.

A linked brain publishes one bounded Markdown file, ``CIAO-CONTENT-GUIDE.md``,
that says what the brain holds: its scope, its exclusions, and one typed
destination for each supported note type. The guide is a *proposal* written by
the brain's maintainers, not a permission. It cannot grant disclosure, cannot
name a destination the operator did not independently consent to, and cannot
choose an operation the operator did not allow.

The machine-readable part is a single fenced JSON block. Everything around it
is prose for people; prose is never interpreted as authority::

    # Platform team notes

    Decisions and vocabulary for the platform team. Personal notes stay local.

    ```ciao-guide
    {
      "version": 1,
      "scope": "Project decisions and vocabulary for the platform team.",
      "exclusions": ["personal notes", "customer data"],
      "types": {
        "decision": {
          "kind": "section",
          "path": "notes/decisions.md",
          "section": "Decisions",
          "operation": "append",
          "example": "Use UTC for every stored timestamp."
        },
        "glossary-term": {
          "kind": "collection",
          "path": "notes/glossary",
          "operation": "create",
          "example": "Monday huddle: the weekly planning meeting."
        }
      }
    }
    ```

The guide file is bounded before it is read (:data:`MAX_GUIDE_BYTES`), every
type entry is bounded and validated, and a single malformed entry fails the
whole guide closed: a guide is either wholly reviewable or unusable, never a
mix of trusted and dropped type entries. A malformed guide (no fence, two
fences, broken JSON, an unsupported version, no types, any invalid entry) is
returned as an unusable :class:`GuideLoad` so the caller defers publication for
fresh review. Paths, headings and free text must also be encodable as UTF-8:
an escaped lone surrogate (``"\\ud800"``) cannot become a filesystem path or be
rendered, so it is malformed rather than accepted or silently replaced.

A guide read is pinned to the brain's identity, the declared guide version and
the digest of the exact bytes. :func:`resolve_destination` refuses a resolution
whose stored pin no longer matches the current guide (``guide_changed``), so a
guide edit cannot silently widen an existing grant. Consent lives in a separate,
operator-owned :class:`ConsentMapping` bound to the same brain identity: the
guide and the consent must agree on the type, destination kind, exact relative
path, section and operation before a destination resolves. A change of kind,
path, section or operation is ``outside_consent``; a guide for one brain cannot
use another brain's consent (``consent_brain_mismatch``).

Integration gates this module does not own, kept explicit for the #3615
persistence and route layer:

* consent is persisted per workspace, brain and link lifetime; unlink/relink,
  archive restore and registry import convey no grant;
* :func:`resolve_for_publication` requires the stored pin, so a publication
  cannot skip guide revalidation; first-time review uses
  :func:`resolve_destination`, then persists the returned pin and the link
  lifetime;
* the writer re-reads the guide and validates the destination through a
  symlink-safe descriptor immediately before its final write; this module
  reads safely but performs no write;
* Settings owns the authenticated operator action, exact permission preview,
  revoke and the Review default.

This module is deliberately free of Git, the workspace registry, the web layer
and mutable module state. The caller supplies a root path, a brain identity and
a consent mapping; tests can exercise it entirely in ``tmp_path``.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, cast

from ciao.os_support.files import open_fd

#: The published guide's filename, at the brain's root.
GUIDE_NAME = "CIAO-CONTENT-GUIDE.md"

#: The only guide schema this module understands.
GUIDE_VERSION = 1

#: The whole file is bounded before any parsing, including its prose.
MAX_GUIDE_BYTES = 16_384

#: Bounds on the machine-readable guide parts.
MAX_TYPES = 40
MAX_EXCLUSIONS = 20
MAX_SCOPE_CHARS = 500
MAX_EXAMPLE_CHARS = 240
MAX_SECTION_CHARS = 80
MAX_TYPE_NAME_CHARS = 40
MAX_PATH_CHARS = 240
MAX_PATH_DEPTH = 8
MAX_COMPONENT_CHARS = 80
MAX_IDENTITY_CHARS = 300

DestinationKind = Literal["section", "collection"]
Operation = Literal["create", "replace", "append"]
GuideStatus = Literal[
    "ok", "missing", "unavailable", "unsafe", "too_large", "malformed", "unsupported"
]
ResolutionStatus = Literal[
    "ok",
    "guide_unavailable",
    "guide_changed",
    "unknown_type",
    "not_consented",
    "consent_brain_mismatch",
    "outside_consent",
    "path_mismatch",
    "unsafe_path",
    "operation_not_allowed",
]

_OPERATIONS: frozenset[str] = frozenset({"create", "replace", "append"})
_FENCE_RE = re.compile(
    r"^```ciao-guide[ \t]*\r?\n(.*?)^```[ \t]*\r?$",
    re.MULTILINE | re.DOTALL,
)
_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SAFE_COMPONENT = re.compile(r'^[^<>:"|?*#]+$')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
_GUIDE_KEYS = frozenset({"version", "scope", "exclusions", "types"})
_TYPE_KEYS = frozenset({"kind", "path", "section", "operation", "example"})


@dataclass(frozen=True, slots=True)
class GuidePin:
    """The identity, version and digest that bound one guide read."""

    brain_identity: str
    version: int
    digest: str


@dataclass(frozen=True, slots=True)
class Destination:
    """A typed place a note type may go.

    ``section`` is an exact Markdown note path plus the heading under which the
    note belongs. ``collection`` is a directory; the caller supplies one
    repository-relative Markdown filename inside it, never an arbitrary path.
    """

    kind: DestinationKind
    path: str
    operation: Operation
    section: str = ""


@dataclass(frozen=True, slots=True)
class NoteType:
    """One supported note type and the destination the guide proposes."""

    name: str
    destination: Destination
    example: str = ""


@dataclass(frozen=True, slots=True)
class ContentGuide:
    """A parsed, bounded guide whose complete type map has been reviewed."""

    pin: GuidePin
    text: str
    scope: str
    exclusions: tuple[str, ...]
    types: Mapping[str, NoteType]


@dataclass(frozen=True, slots=True)
class GuideLoad:
    """The outcome of reading and parsing one brain's guide."""

    status: GuideStatus
    guide: ContentGuide | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.guide is not None


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    """One operator-approved note type, destination and operation set.

    This is the independent half of a resolution. It is never derived from the
    guide: the operator names the type, kind, exact path, section and
    operations that may be used for that brain.
    """

    note_type: str
    kind: DestinationKind
    path: str
    operations: frozenset[str]
    section: str = ""

    def __post_init__(self) -> None:
        problems = _grant_problems(self)
        if problems:
            raise ValueError("; ".join(problems))


@dataclass(frozen=True, slots=True)
class ConsentMapping:
    """The grants one operator issued for one brain, keyed by note type."""

    brain_identity: str
    grants: tuple[ConsentGrant, ...] = ()

    def __post_init__(self) -> None:
        problem = _identity_problem(self.brain_identity)
        if problem:
            raise ValueError(f"consent mapping: {problem}")
        seen: set[str] = set()
        for grant in self.grants:
            if not isinstance(grant, ConsentGrant):
                raise ValueError("consent mapping: every grant must be a ConsentGrant")
            if grant.note_type in seen:
                raise ValueError(
                    f"consent mapping: more than one grant for note type {grant.note_type!r}"
                )
            seen.add(grant.note_type)

    def for_type(self, note_type: str) -> ConsentGrant | None:
        """The grant for ``note_type``, or None when the operator did not allow it."""
        return next((grant for grant in self.grants if grant.note_type == note_type), None)


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    """A destination that the guide proposed and the consent mapping allowed."""

    note_type: str
    destination: Destination
    guide: GuidePin


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving a note type against a guide and its consent."""

    status: ResolutionStatus
    reason: str = ""
    destination: ResolvedDestination | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.destination is not None


class _DuplicateKey(ValueError):
    """A JSON object repeated a key; Python would otherwise keep the last one."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _reject_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant: {value}")


def _label(value: str) -> str:
    """A short, printable rendering of an untrusted type key for a report."""
    shown = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in value)
    return shown[:MAX_TYPE_NAME_CHARS]


def _control_free(value: str, *, allow_layout: bool) -> bool:
    for ch in value:
        if ch in "\n\t" and allow_layout:
            continue
        if ord(ch) < 32 or ord(ch) == 127:
            return False
    return True


def _encodable(value: str) -> bool:
    """Whether ``value`` can become UTF-8 bytes without replacement or error.

    JSON permits an escaped lone surrogate such as ``"\\ud800"``, which Python
    decodes into a ``str`` that cannot be encoded or written to a filesystem
    path. The guide is shared data, so it is refused rather than repaired.
    """
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    return True


def _identity_problem(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "the brain identity is empty"
    if value != value.strip():
        return "the brain identity has leading or trailing whitespace"
    if len(value) > MAX_IDENTITY_CHARS:
        return f"the brain identity exceeds {MAX_IDENTITY_CHARS} characters"
    if not _control_free(value, allow_layout=False):
        return "the brain identity contains a control character"
    if not _encodable(value):
        return "the brain identity contains a character that cannot be encoded as UTF-8"
    return ""


def _relative_parts(value: object, *, suffix: str) -> tuple[tuple[str, ...], str]:
    """Split and validate a repository-relative path; ((), reason) on refusal."""
    if not isinstance(value, str) or not value:
        return (), "a repository-relative path is required"
    if len(value) > MAX_PATH_CHARS:
        return (), f"the path exceeds {MAX_PATH_CHARS} characters"
    if value.startswith(("/", "\\")) or "\\" in value or (len(value) >= 2 and value[1] == ":"):
        return (), "the path must be repository-relative, not absolute"
    if not _control_free(value, allow_layout=False):
        return (), "the path contains a control character"
    if not _encodable(value):
        return (), "the path contains a character that cannot be encoded as UTF-8"
    parts = tuple(value.split("/"))
    if len(parts) > MAX_PATH_DEPTH:
        return (), "the path is nested too deeply"
    for part in parts:
        if not part or part in (".", ".."):
            return (), "the path contains an empty, '.' or '..' component"
        if len(part) > MAX_COMPONENT_CHARS:
            return (), f"a path component exceeds {MAX_COMPONENT_CHARS} characters"
        if part.startswith("."):
            return (), "the path contains a hidden component"
        if part.endswith((".", " ")):
            return (), "a path component ends with a dot or space"
        if not _SAFE_COMPONENT.fullmatch(part):
            return (), "the path contains an unsafe character"
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            return (), "the path contains a reserved filename"
    if suffix and not parts[-1].lower().endswith(suffix):
        return (), f"the path must end in {suffix}"
    return parts, ""


def _heading_problem(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "a section heading is required"
    if value != value.strip():
        return "the section heading has leading or trailing whitespace"
    if len(value) > MAX_SECTION_CHARS:
        return f"the section heading exceeds {MAX_SECTION_CHARS} characters"
    if value.startswith("#") or not _control_free(value, allow_layout=False):
        return "the section heading starts with '#' or contains a control character"
    if not _encodable(value):
        return "the section heading contains a character that cannot be encoded as UTF-8"
    return ""


def _prose_problem(value: object, *, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return f"{label} must be non-empty text"
    if len(value) > limit:
        return f"{label} exceeds {limit} characters"
    if not _control_free(value, allow_layout=True):
        return f"{label} contains a control character"
    if not _encodable(value):
        return f"{label} contains a character that cannot be encoded as UTF-8"
    return ""


def _type_name_ok(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TYPE_NAME_CHARS
        and _TYPE_RE.fullmatch(value) is not None
    )


def _note_type(name: str, entry: object) -> tuple[NoteType | None, str]:
    """One type entry, or the reason it makes the whole guide malformed."""
    if not _type_name_ok(name):
        return None, "the type name must use lowercase letters, digits and single dashes"
    if not isinstance(entry, dict):
        return None, "the type entry must be an object"
    row = cast(dict[str, object], entry)
    unknown = set(row) - _TYPE_KEYS
    if unknown:
        return None, "the type entry has an unknown field"
    kind_raw = row.get("kind")
    if kind_raw not in ("section", "collection"):
        return None, "kind must be 'section' or 'collection'"
    kind = cast(DestinationKind, kind_raw)
    operation_raw = row.get("operation")
    if not isinstance(operation_raw, str) or operation_raw not in _OPERATIONS:
        return None, "operation must be 'create', 'replace' or 'append'"
    operation = cast(Operation, operation_raw)
    if kind == "collection" and operation == "append":
        return None, "a collection destination cannot append to one note"
    suffix = ".md" if kind == "section" else ""
    parts, why = _relative_parts(row.get("path"), suffix=suffix)
    if why:
        return None, why
    section = ""
    if kind == "section":
        why = _heading_problem(row.get("section"))
        if why:
            return None, why
        section = cast(str, row["section"])
    elif "section" in row:
        return None, "a collection destination has no section heading"
    example_raw = row.get("example", "")
    if not isinstance(example_raw, str):
        return None, "example must be text"
    if len(example_raw) > MAX_EXAMPLE_CHARS:
        return None, f"example exceeds {MAX_EXAMPLE_CHARS} characters"
    if not _control_free(example_raw, allow_layout=True):
        return None, "example contains a control character"
    if not _encodable(example_raw):
        return None, "example contains a character that cannot be encoded as UTF-8"
    destination = Destination(
        kind=kind,
        path="/".join(parts),
        operation=operation,
        section=section,
    )
    return NoteType(name=name, destination=destination, example=example_raw.strip()), ""


def _parse_document(
    payload: object, *, brain_identity: str, raw: bytes, text: str
) -> GuideLoad:
    if not isinstance(payload, dict):
        return GuideLoad("malformed", reason="the guide block must be a JSON object")
    doc = cast(dict[str, object], payload)
    version = doc.get("version")
    if type(version) is not int:
        return GuideLoad("malformed", reason="the guide version must be an integer")
    if version != GUIDE_VERSION:
        return GuideLoad(
            "unsupported",
            reason=f"guide version {version} is not supported (this engine reads version {GUIDE_VERSION})",
        )
    if set(doc) != _GUIDE_KEYS:
        return GuideLoad(
            "malformed",
            reason="the guide must contain exactly version, scope, exclusions and types",
        )
    scope_problem = _prose_problem(doc.get("scope"), limit=MAX_SCOPE_CHARS, label="scope")
    if scope_problem:
        return GuideLoad("malformed", reason=scope_problem)
    scope = cast(str, doc["scope"]).strip()

    exclusions_raw = doc.get("exclusions")
    if not isinstance(exclusions_raw, list):
        return GuideLoad("malformed", reason="exclusions must be a list of text")
    if len(exclusions_raw) > MAX_EXCLUSIONS:
        return GuideLoad(
            "malformed", reason=f"the guide lists more than {MAX_EXCLUSIONS} exclusions"
        )
    exclusions: list[str] = []
    for item in exclusions_raw:
        problem = _prose_problem(item, limit=MAX_EXAMPLE_CHARS, label="an exclusion")
        if problem:
            return GuideLoad("malformed", reason=problem)
        exclusion = cast(str, item).strip()
        if exclusion not in exclusions:
            exclusions.append(exclusion)

    types_raw = doc.get("types")
    if not isinstance(types_raw, dict):
        return GuideLoad("malformed", reason="types must be an object")
    if len(types_raw) > MAX_TYPES:
        return GuideLoad("malformed", reason=f"the guide lists more than {MAX_TYPES} note types")
    if not types_raw:
        return GuideLoad("malformed", reason="the guide lists no note types")
    valid: dict[str, NoteType] = {}
    for name, entry in cast(dict[str, object], types_raw).items():
        note_type, reason = _note_type(name, entry)
        if note_type is None:
            return GuideLoad(
                "malformed",
                reason=f"the guide's {_label(name)!r} note type is malformed: {reason}",
            )
        valid[name] = note_type
    pin = GuidePin(
        brain_identity=brain_identity,
        version=GUIDE_VERSION,
        digest=hashlib.sha256(raw).hexdigest(),
    )
    return GuideLoad(
        "ok",
        guide=ContentGuide(
            pin=pin,
            text=text,
            scope=scope,
            exclusions=tuple(exclusions),
            types=MappingProxyType(valid),
        ),
    )


def parse_guide(raw: bytes, *, brain_identity: str) -> GuideLoad:
    """Parse bounded guide bytes and pin them to ``brain_identity``.

    Any malformed type entry fails the whole guide closed; a partially trusted
    map is never returned. The caller must have bounded ``raw`` already; this
    function re-checks it so a direct caller cannot bypass the limit.
    """
    identity_problem = _identity_problem(brain_identity)
    if identity_problem:
        raise ValueError(identity_problem)
    if not isinstance(raw, bytes):
        raise TypeError("guide bytes must be bytes")
    if len(raw) > MAX_GUIDE_BYTES:
        return GuideLoad(
            "too_large", reason=f"the guide exceeds {MAX_GUIDE_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return GuideLoad("malformed", reason="the guide is not valid UTF-8")
    blocks = _FENCE_RE.findall(text)
    if len(blocks) != 1:
        return GuideLoad(
            "malformed",
            reason="the guide must contain exactly one ```ciao-guide fenced block",
        )
    try:
        payload: object = json.loads(
            blocks[0],
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey as exc:
        return GuideLoad("malformed", reason=f"the guide repeats the key {_label(str(exc))!r}")
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return GuideLoad("malformed", reason="the guide block is not valid JSON")
    return _parse_document(
        payload, brain_identity=brain_identity, raw=raw, text=text
    )


def read_guide(root: str | os.PathLike[str], *, brain_identity: str) -> GuideLoad:
    """Read one brain's ``CIAO-CONTENT-GUIDE.md`` from ``root``, bounded.

    The final component is opened with ``O_NOFOLLOW`` and must be a regular
    file, so a symlink or a directory published under the guide's name is
    refused instead of being read through. Reading goes through a descriptor,
    so the returned digest describes the exact snapshot even if the file is
    replaced while the caller inspects it.
    """
    identity_problem = _identity_problem(brain_identity)
    if identity_problem:
        raise ValueError(identity_problem)
    path = Path(root) / GUIDE_NAME
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        # A FIFO published under the guide's name must not block the engine
        # before fstat can reject it as a non-regular file.
        flags |= os.O_NONBLOCK
    try:
        fd = open_fd(path, flags, follow_symlinks=False)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return GuideLoad("missing", reason=f"the brain has no {GUIDE_NAME}")
        if exc.errno == errno.ELOOP:
            return GuideLoad("unsafe", reason="the guide must be a regular file, not a symlink")
        return GuideLoad("unavailable", reason=exc.strerror or "the guide could not be read")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return GuideLoad("unsafe", reason="the guide must be a regular file")
        chunks: list[bytes] = []
        remaining = MAX_GUIDE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        return GuideLoad("unavailable", reason=exc.strerror or "the guide could not be read")
    finally:
        os.close(fd)
    if len(raw) > MAX_GUIDE_BYTES:
        return GuideLoad("too_large", reason=f"the guide exceeds {MAX_GUIDE_BYTES} bytes")
    return parse_guide(raw, brain_identity=brain_identity)


def _grant_problems(grant: ConsentGrant) -> list[str]:
    problems: list[str] = []
    if not _type_name_ok(grant.note_type):
        problems.append("the note type must use lowercase letters, digits and single dashes")
    if grant.kind not in ("section", "collection"):
        problems.append("kind must be 'section' or 'collection'")
    suffix = ".md" if grant.kind == "section" else ""
    _parts, path_problem = _relative_parts(grant.path, suffix=suffix)
    if path_problem:
        problems.append(path_problem)
    if grant.kind == "section":
        heading_problem = _heading_problem(grant.section)
        if heading_problem:
            problems.append(heading_problem)
    elif grant.section:
        problems.append("a collection grant has no section heading")
    if not isinstance(grant.operations, frozenset):
        problems.append("operations must be a frozenset")
    elif not grant.operations:
        problems.append("at least one operation must be allowed")
    else:
        unknown = set(grant.operations) - _OPERATIONS
        if unknown:
            problems.append("operations must be create, replace or append")
        if grant.kind == "collection" and "append" in grant.operations:
            problems.append("a collection grant cannot allow append")
    return problems


def _refuse(status: ResolutionStatus, reason: str) -> Resolution:
    return Resolution(status, reason=reason)


def resolve_destination(
    load: GuideLoad,
    consent: ConsentMapping,
    *,
    note_type: str,
    relative_path: str = "",
    pinned: GuidePin | None = None,
    operation: str | None = None,
) -> Resolution:
    """Resolve one note type against the brain's guide and the operator's consent.

    ``pinned`` is the guide pin stored in the private link policy. When given,
    any identity, version or byte change refuses with ``guide_changed``. When
    omitted, this is a first review of a new guide: the returned
    :class:`ResolvedDestination` carries the pin to persist. Anything about to
    publish must call :func:`resolve_for_publication` instead, which makes the
    pin mandatory.

    For a ``section`` destination the guide fixes the note path; a caller may
    repeat it but not choose another. For a ``collection`` destination the
    caller supplies one repository-relative Markdown path inside the guide's
    directory. Consent must independently allow the same type, kind, exact
    path, section and operation.
    """
    if not load.ok or load.guide is None:
        detail = f": {load.reason}" if load.reason else ""
        return _refuse("guide_unavailable", f"the guide is {load.status}{detail}")
    guide = load.guide
    if pinned is not None and pinned != guide.pin:
        return _refuse(
            "guide_changed",
            "the content guide changed; review the new guide before publishing",
        )
    if consent.brain_identity != guide.pin.brain_identity:
        return _refuse(
            "consent_brain_mismatch",
            "the consent mapping belongs to another shared brain",
        )
    if not _type_name_ok(note_type):
        return _refuse("unknown_type", "the note type is not a supported slug")
    note = guide.types.get(note_type)
    if note is None:
        return _refuse("unknown_type", "the guide does not list this note type")
    grant = consent.for_type(note_type)
    if grant is None:
        return _refuse("not_consented", "the operator did not consent to this note type")
    destination = note.destination
    if destination.kind != grant.kind:
        return _refuse("outside_consent", "the guide's destination kind is not consented")
    if destination.path != grant.path:
        return _refuse("outside_consent", "the guide's destination path is not consented")
    if destination.kind == "section" and destination.section != grant.section:
        return _refuse("outside_consent", "the guide's section heading is not consented")
    if destination.kind == "section":
        if relative_path:
            parts, why = _relative_parts(relative_path, suffix=".md")
            if why:
                return _refuse("unsafe_path", why)
            if "/".join(parts) != destination.path:
                return _refuse(
                    "path_mismatch", "a section destination cannot choose another note path"
                )
        resolved_path = destination.path
    else:
        if not relative_path:
            return _refuse(
                "path_mismatch",
                "a collection destination needs a repository-relative Markdown path",
            )
        parts, why = _relative_parts(relative_path, suffix=".md")
        if why:
            return _refuse("unsafe_path", why)
        base, base_why = _relative_parts(destination.path, suffix="")
        if base_why:
            return _refuse("outside_consent", "the guide's collection path is not usable")
        if len(parts) <= len(base) or parts[: len(base)] != base:
            return _refuse(
                "path_mismatch", "the note path is outside the guide's collection"
            )
        resolved_path = "/".join(parts)
    chosen = destination.operation if operation is None else operation
    if not isinstance(chosen, str) or chosen not in _OPERATIONS:
        return _refuse(
            "operation_not_allowed", "the operation must be create, replace or append"
        )
    if chosen != destination.operation:
        return _refuse(
            "operation_not_allowed",
            f"the guide binds this type to {destination.operation}, not {chosen}",
        )
    if chosen not in grant.operations:
        return _refuse(
            "operation_not_allowed",
            f"the operator did not consent to {chosen} for this note type",
        )
    resolved = ResolvedDestination(
        note_type=note_type,
        destination=Destination(
            kind=destination.kind,
            path=resolved_path,
            operation=cast(Operation, chosen),
            section=destination.section,
        ),
        guide=guide.pin,
    )
    return Resolution("ok", destination=resolved)


def resolve_for_publication(
    load: GuideLoad,
    consent: ConsentMapping,
    *,
    note_type: str,
    pinned: GuidePin | None,
    relative_path: str = "",
    operation: str | None = None,
) -> Resolution:
    """Resolve a destination for a write, refusing when no guide pin was stored.

    Publication and any other effect must use this entry point. A caller that
    skipped the first review cannot publish by omitting ``pinned``; the refusal
    is ``guide_changed`` because the guide needs review and a stored pin before
    the effect can proceed.
    """
    if pinned is None:
        return _refuse(
            "guide_changed",
            "a stored guide pin is required before publication",
        )
    return resolve_destination(
        load,
        consent,
        note_type=note_type,
        relative_path=relative_path,
        pinned=pinned,
        operation=operation,
    )
