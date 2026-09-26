"""The vault's category registry: one editable list behind the type vocabulary.

The kinds of notes the agent keeps were hardcoded in six places that had to be
edited in step: ``vault_index``'s ``DIR_TYPE_MAP`` / ``CANONICAL_TYPES`` /
``TYPE_ALIASES``, ``config``'s ``_WORKSPACE_EVIDENCE_DIRS``,
``context.entity_tagger._CATEGORY_PARTS``, ``vault_lint``'s orphan-candidate
folders and ``memory_audit``'s ``STALE_NOTE_THRESHOLDS_DAYS``. Nothing showed
the user that list, and adding a category was an edit in five modules with no
schema to fill in.

This module is the single source of truth. ``ciao/stock/entity-types.yaml``
ships the defaults; ``<vault>/entity-types.yaml`` holds the user's edits; the
effective list is the two merged by ``id``. Nothing reads the registry yet —
this half of the work is additive, so behaviour with no vault file is
byte-identical to before, and ``tests/test_entity_types.py`` pins every derived
view against the live constant it will eventually replace.

**Which vault.** ``<vault>`` is the *agent* vault root
(``CiaoConfig.agent_vault_root(name)``) — the root that owns the generated
``INDEX.md`` and ``VOCABULARY.md``, not a workspace's notes root. The two differ
on a pre-re-rooting install (one shared vault, several workspaces), so
``load_entity_types`` takes the directory explicitly and leaves that choice to
the caller.

**Merge.** A vault entry is a *partial* override of the stock entry with the
same ``id``: only the keys it states change, so hand-writing

.. code-block:: yaml

    - id: person
      label: Human

relabels a category without dropping its folder, its aliases or its staleness.
Stating a key with its default value is a real instruction — ``kind: note`` on
a stock entity, or ``stale_after_days: 0`` on a project that should not age out
at all — so the merge tracks which keys the file named rather than inferring it
from the values. A vault ``id`` with no stock entry is appended as a custom one.
``builtin`` is never read from the file: a stock entry is builtin and a
vault-only entry is not, whatever the file claims.

**Failure is closed, not loud.** A vault file that is not a list, is not
parseable, or holds an entry that is not a well-formed category, is dropped
whole (one warning) and the stock list stands. Half a merge would produce a
vocabulary that is neither the user's nor the shipped one, which is worse than
ignoring the file; their notes still lint and index as before.

**``kind: entity`` vs ``kind: note``** is the one judgement call the file has to
make, and it is load-bearing rather than cosmetic: the entity categories are the
ones a roster resolves mentions against, and their folders are the ones the
orphan linter watches (:meth:`EntityTypeRegistry.entity_folders` and
:meth:`EntityTypeRegistry.orphan_dirs`). A stock entry is an entity when the note
is the record *of* the thing — a person, a project, a place, an idea, a
resource — and a note otherwise, including a product, a feature or an
automation, whose note describes the thing rather than being it.

**Three views are not fully derivable**, because the wire format they feed
predates this file and must not change. They are stock-only and documented at
their definitions: :data:`_LEGACY_DIR_TYPE_MAP`, :data:`_ORPHAN_EXTRA_DIRS` and
:data:`_CATEGORY_PART_FOLDERS`.

``config._WORKSPACE_EVIDENCE_DIRS`` is deliberately not derived here, even
though it is one of the six hardcoded lists: it is a *containment* test (a
directory that holds one of these is a workspace, which is how
``memory-vault/personal/People/`` makes ``personal`` a workspace) rather than a
type mapping, and it spans a workspace's own folder plus the lower-case
``projects``. Deriving it would be a fourth compatibility table with no consumer
to justify it yet, so it stays in ``config`` until one does.

Loads are cached per vault against the vault file's mtime, so a caller in a hot
path can ask on every read; ``clear_entity_types_cache()`` is the test hook.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from importlib import resources
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

#: The stock list, read from the package it ships in (see ``schedules.json``
#: for the same pattern).
STOCK_FILENAME = "entity-types.yaml"

#: The same name inside a vault: the override file is a peer of the generated
#: ``INDEX.md`` / ``VOCABULARY.md``, not a stock file with a per-vault copy.
VAULT_FILENAME = "entity-types.yaml"

KIND_ENTITY = "entity"
KIND_NOTE = "note"
_KINDS = frozenset({KIND_ENTITY, KIND_NOTE})

# ``active`` and ``completed`` are the two halves of the old per-project
# ``projects/{active,completed}/`` layout, and both infer ``project``. A category
# has ONE folder, so these two directory names cannot come from an entry; they
# stay here. They must keep mapping to ``project``: ``vault_index._workspace_of``
# tests membership in the keys of the same map to tell a folder type from a
# workspace name, so dropping a key silently changes workspace inference.
_LEGACY_DIR_TYPE_MAP: dict[str, str] = {"active": "project", "completed": "project"}

# The lower-case spellings the orphan linter also watches. ``projects`` is the
# historical name of the ``Projects`` folder, ``references`` is a real category's
# own folder; an entry expresses one folder per category, and neither name can be
# derived from the entries, so the two extras are spelled out here.
_ORPHAN_EXTRA_DIRS: frozenset[str] = frozenset({"projects", "references"})

# The folders an ``INDEX.md`` bullet may name, in both cases. This is the wire
# format of ``context.entity_tagger``: a bullet carries the folder as the indexer
# wrote it, and the tagger maps that part back to a category. Deriving it from
# every entry's folder would silently widen the tagger to the lower-case folders
# (``products``, ``references``, …) and change what an ``INDEX.md`` line resolves
# to, so the set is fixed here and a new category does not join it without a
# decision.
_CATEGORY_PART_FOLDERS: tuple[str, ...] = (
    "People",
    "Projects",
    "Places",
    "Ideas",
    "Resources",
    "Documents",
    "Workspace",
)


class EntityTypeFileError(ValueError):
    """A category file that cannot be trusted.

    Raised while parsing, never out of :func:`load_entity_types`: the loader
    catches it and falls back to the stock list.
    """


@dataclass(frozen=True, slots=True)
class EntityType:
    """One category: a frontmatter ``type:`` value and where its notes live.

    ``stale_after_days`` is an override, not a rule: ``0`` means "whatever the
    caller applies by default", which is how a category with no opinion about
    ageing says so.
    """

    id: str
    label: str
    kind: str = KIND_NOTE
    folder: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    stale_after_days: int = 0
    enabled: bool = True
    builtin: bool = True

    @property
    def is_entity(self) -> bool:
        """Whether this category is a record *of* a thing (see the module docstring)."""
        return self.kind == KIND_ENTITY


@dataclass(frozen=True, slots=True)
class _Stated:
    """A parsed entry beside the keys its file actually stated.

    The merge needs the difference. ``kind: note`` on a stock entity, or
    ``stale_after_days: 0`` on a project, asks for the default and is
    indistinguishable from staying silent if the merge only compares values.
    """

    entry: EntityType
    stated: frozenset[str]


class EntityTypeRegistry:
    """The effective category list, plus the views consumers will read.

    Built from the merged entries and immutable: every view is computed once, so
    two calls cannot disagree and a caller cannot edit the registry by mutating
    what it was handed. A disabled entry stays in :meth:`entries` (a row a
    Settings page renders) and is out of every derived view (the list a note is
    typed against).
    """

    __slots__ = (
        "_aliases",
        "_by_id",
        "_canonical_types",
        "_category_parts",
        "_dir_type_map",
        "_entries",
        "_entity_folders",
        "_orphan_dirs",
        "_stale_thresholds",
    )

    def __init__(self, entries: Sequence[EntityType]) -> None:
        self._entries: tuple[EntityType, ...] = tuple(entries)
        self._by_id: dict[str, EntityType] = {entry.id: entry for entry in self._entries}
        enabled = [entry for entry in self._entries if entry.enabled]

        self._canonical_types: frozenset[str] = frozenset(entry.id for entry in enabled)
        aliases: dict[str, str] = {}
        for entry in enabled:
            for alias in entry.aliases:
                aliases[alias] = entry.id
        self._aliases: dict[str, str] = aliases
        # Entries last: an entry that claims a legacy folder name overrides the
        # stock fallback, because an explicit configuration beats a shipped
        # default.
        self._dir_type_map: dict[str, str] = {
            **_LEGACY_DIR_TYPE_MAP,
            **{entry.folder: entry.id for entry in enabled if entry.folder},
        }
        self._entity_folders: tuple[str, ...] = tuple(
            sorted(entry.folder for entry in enabled if entry.is_entity and entry.folder)
        )
        self._stale_thresholds: dict[str, int] = {
            entry.id: entry.stale_after_days
            for entry in enabled
            if entry.stale_after_days > 0
        }
        self._orphan_dirs: frozenset[str] = frozenset(self._entity_folders) | _ORPHAN_EXTRA_DIRS
        self._category_parts: dict[str, str] = {
            case: folder
            for folder in _CATEGORY_PART_FOLDERS
            for case in (folder, folder.lower())
        }

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> tuple[EntityType, ...]:
        """Every entry, disabled ones included, in file order."""
        return self._entries

    def get(self, type_id: str) -> EntityType | None:
        """The entry for *type_id*, enabled or not, or ``None``."""
        return self._by_id.get(type_id)

    def canonical_types(self) -> frozenset[str]:
        """The closed vocabulary for frontmatter ``type:``.

        Every enabled entry's ``id``. With the stock list this is exactly
        ``vault_index.CANONICAL_TYPES``: the values of the folder map plus the
        three types no directory name implies (``log``, ``note``,
        ``skill-proposal``).
        """
        return self._canonical_types

    def aliases(self) -> dict[str, str]:
        """Alias -> owning entry ``id``, for the enabled entries."""
        return self._aliases

    def dir_type_map(self) -> dict[str, str]:
        """Folder -> type, for inferring a type from the path.

        With the stock list this is exactly ``vault_index.DIR_TYPE_MAP``,
        including the legacy ``active`` / ``completed`` keys: a category has one
        folder, so those two directory names come from
        :data:`_LEGACY_DIR_TYPE_MAP` instead.
        """
        return self._dir_type_map

    def entity_folders(self) -> tuple[str, ...]:
        """The folders of the enabled ``kind: entity`` categories, sorted."""
        return self._entity_folders

    def stale_thresholds(self) -> dict[str, int]:
        """Type -> days before its facts count as unverified, where stated.

        Only the categories that carry an opinion (a positive
        ``stale_after_days``) appear; a caller applies its own default to the
        rest and its own exempt set to the event surfaces.
        """
        return self._stale_thresholds

    def orphan_dirs(self) -> frozenset[str]:
        """Folders whose notes are candidates for the orphan check.

        The entity categories' own folders (a person's or a project's record is
        worth linking to) plus the lower-case names in
        :data:`_ORPHAN_EXTRA_DIRS` that real vaults still use. With the stock
        list this is exactly the set ``vault_lint`` lints against.
        """
        return self._orphan_dirs

    def category_parts(self) -> dict[str, str]:
        """``INDEX.md`` path part -> category label, in the case variants.

        With the stock list this is exactly
        ``context.entity_tagger._CATEGORY_PARTS``. Built from
        :data:`_CATEGORY_PART_FOLDERS` rather than from the entries: it is the
        tagger's wire format, and a new category must not silently change which
        ``INDEX.md`` lines resolve to a category.
        """
        return self._category_parts


def _read_stock() -> list[EntityType]:
    """The shipped categories, parsed once per process.

    A stock file that does not parse is our bug, not the user's, so it is logged
    loudly and dropped rather than taking the process down on import. The views
    are pinned against the live constants in ``tests/test_entity_types.py``, so a
    broken stock file fails there.
    """
    text = resources.files("ciao.stock").joinpath(STOCK_FILENAME).read_text(encoding="utf-8")
    try:
        payload = yaml.safe_load(text)
        return [stated.entry for stated in _parse(payload, builtin=True, partial=False)]
    except (yaml.YAMLError, EntityTypeFileError) as exc:
        logger.error("stock %s is malformed, ignoring it: %s", STOCK_FILENAME, exc)
        return []


def _parse(payload: object, *, builtin: bool, partial: bool) -> list[_Stated]:
    """Turn a parsed YAML document into entries, keeping the keys each stated.

    ``partial`` selects the two file roles. A stock file is complete: every entry
    must state ``label``, ``kind`` and ``enabled``, because a missing one there
    is a mistake in a file we ship. A vault file is a set of overrides, and only
    the keys it names are read — :func:`_merge` fills the rest from stock.

    Raises :class:`EntityTypeFileError` on anything a reader should not have to
    guess about: a document that is not a list, an entry that is not a mapping, a
    missing or non-string ``id``, an empty ``label``, an unknown ``kind``, an
    alias list that is not a list of names, a negative ``stale_after_days``, an
    ``enabled`` that is not a boolean. The caller decides whether that means
    "drop the file" (it always does) or "raise" (only a direct parse does).
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise EntityTypeFileError(f"expected a list of categories, got {type(payload).__name__}")
    stated_entries: list[_Stated] = []
    for index, raw in enumerate(payload):
        if not isinstance(raw, Mapping):
            raise EntityTypeFileError(f"entry {index} is not a mapping, got {type(raw).__name__}")
        try:
            stated_entries.append(_entry(raw, builtin=builtin, partial=partial))
        except EntityTypeFileError as exc:
            raise EntityTypeFileError(f"entry {index}: {exc}") from exc
    return stated_entries


def _entry(raw: Mapping[str, object], *, builtin: bool, partial: bool) -> _Stated:
    known = {declared.name for declared in fields(EntityType)}
    unknown = sorted(str(key) for key in raw if str(key) not in known)
    if unknown and not partial:
        # A typo in a file we ship is worth naming; a typo in a user's override
        # file is not worth failing the whole file over, since the key is
        # ignored either way.
        raise EntityTypeFileError(f"unknown key(s) {', '.join(unknown)}")

    entry_id = raw.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise EntityTypeFileError(f"id must be a non-empty string, got {entry_id!r}")
    if not partial:
        for required in ("label", "kind", "enabled"):
            if required not in raw:
                raise EntityTypeFileError(f"missing {required}")

    values: dict[str, object] = dict(_DEFAULTS)
    stated = {name for name in known if name in raw and name not in {"id", "builtin"}}
    for name in stated:
        values[name] = raw[name]

    label = _as_str(values["label"], "label")
    if "label" in raw and not label:
        raise EntityTypeFileError("label must not be empty")
    kind = _as_str(values["kind"], "kind")
    if kind not in _KINDS:
        raise EntityTypeFileError(f"kind must be one of {sorted(_KINDS)}, got {kind!r}")
    entry = EntityType(
        id=entry_id,
        label=label,
        kind=kind,
        folder=_as_str(values["folder"], "folder"),
        description=_as_str(values["description"], "description"),
        aliases=_as_names(values["aliases"], "aliases"),
        stale_after_days=_as_days(values["stale_after_days"]),
        enabled=_as_bool(values["enabled"], "enabled"),
        builtin=builtin,
    )
    return _Stated(entry, frozenset(stated))


# A field's declared default, read off a throwaway instance: on a slotted
# dataclass the class attribute is the slot descriptor, not the default.
_DEFAULTS = {
    declared.name: getattr(EntityType("", ""), declared.name) for declared in fields(EntityType)
}


def _as_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise EntityTypeFileError(f"{name} must be a string, got {value!r}")
    return value


def _as_names(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise EntityTypeFileError(f"{name} must be a list of names, got {value!r}")
    return tuple(value)


def _as_days(value: object) -> int:
    # bool is an int subclass, and `enabled: true` in the wrong field is a typo
    # worth reporting rather than reading as a one-day threshold.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EntityTypeFileError(
            f"stale_after_days must be a non-negative integer, got {value!r}"
        )
    return value


def _as_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise EntityTypeFileError(f"{name} must be a boolean, got {value!r}")
    return value


def _merge(stock: Sequence[EntityType], overrides: Sequence[_Stated]) -> list[EntityType]:
    """The stock entries with the vault's overrides applied, then the new ids.

    Order is stock order, then the vault's own order for ids stock does not have,
    so the list a user reads back has the categories they added at the end rather
    than shuffled into the middle.
    """
    pending = {stated.entry.id: stated for stated in overrides}
    merged: list[EntityType] = []
    for entry in stock:
        stated = pending.pop(entry.id, None)
        merged.append(entry if stated is None else _apply(entry, stated))
    return merged + [stated.entry for stated in pending.values()]


def _apply(stock_entry: EntityType, stated: _Stated) -> EntityType:
    """The stock entry with the keys the vault file stated, and nothing else.

    ``id`` is the merge key and ``builtin`` is decided by where an entry came
    from, so neither is ever taken from a file.
    """
    return replace(
        stock_entry,
        **{name: getattr(stated.entry, name) for name in stated.stated},
    )


# One registry per vault, keyed by the vault directory, holding the mtime the
# file had when the registry was built. ``None`` is the mtime of a vault with no
# file.
_CACHE: dict[Path, tuple[int | None, EntityTypeRegistry]] = {}
_STOCK: list[EntityType] | None = None


def load_entity_types(vault: Path) -> EntityTypeRegistry:
    """The effective categories for *vault*: stock merged with the vault file.

    *vault* is a vault directory, not the file — the loader appends
    ``entity-types.yaml`` itself, and the stock list stands when that file is
    absent. The result is cached against the file's mtime, so a second call with
    an unchanged file returns the same object while a rewritten (or deleted) one
    rebuilds.

    Never raises for the contents of the vault file: a malformed one is logged
    and dropped whole, leaving the stock registry.

    Safe to call from any thread and unguarded. The worst a race does is build
    the same registry twice and keep one, which a read-mostly cache that every
    request already hits is not worth a lock for.
    """
    global _STOCK
    if _STOCK is None:
        _STOCK = _read_stock()
    root = Path(vault)
    path = root / VAULT_FILENAME
    mtime = _mtime_ns(path)
    cached = _CACHE.get(root)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    registry = EntityTypeRegistry(_merge(_STOCK, _read_overrides(path)))
    _CACHE[root] = (mtime, registry)
    return registry


def _mtime_ns(path: Path) -> int | None:
    """The file's mtime in nanoseconds, or ``None`` when it cannot be read.

    Nanoseconds rather than the float ``st_mtime``: two writes inside one
    filesystem tick must not look like one, or a just-saved override is served
    from the previous cache entry.
    """
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_overrides(path: Path) -> list[_Stated]:
    """The vault's own entries, or none when the file is missing or unusable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # A missing file is the normal case and not worth a warning; anything
        # else (unreadable, not UTF-8) is, and both mean "no overrides".
        if not isinstance(exc, FileNotFoundError):
            logger.warning("ignoring unreadable %s: %s", path, exc)
        return []
    try:
        return _parse(yaml.safe_load(text), builtin=False, partial=True)
    except (yaml.YAMLError, EntityTypeFileError) as exc:
        logger.warning("ignoring malformed %s, using the stock categories: %s", path, exc)
        return []


def clear_entity_types_cache() -> None:
    """Drop every cached registry and the parsed stock list.

    The test hook, and the way a long-lived process picks up a stock file
    replaced under it (an upgrade, an editable checkout).
    """
    global _STOCK
    _STOCK = None
    _CACHE.clear()
