"""Review drafts for lessons whose target this workspace does not own (#728-D).

Every fixture is a throwaway install under ``tmp_path``: the feature exists to
file a *public* issue and to create a file every session of a workspace loads,
which is exactly why no test here may reach the developer's own vault, their
GitHub account, or their real skills.

Nothing here contacts GitHub. ``search`` and ``create`` are the two injectable
seams, and every test drives one of them with a recording double — including the
test that proves the approval path searches before it creates, which is the
whole reason the two are separate parameters rather than one call into
``subprocess``.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest

from ciao import upstream_drafts
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.proposal_kinds import parse_bullet
from ciao.upstream_drafts import (
    DRAFT_FILED,
    DRAFT_PENDING,
    DRAFT_REJECTED,
    NEW_SKILL,
    UPSTREAM_ISSUE,
    DraftRefused,
    SanitizeRefused,
    UnattendedRefused,
    approve_draft,
    create_new_skill,
    file_draft,
    find_draft,
    read_queue,
    read_records,
    reject_draft,
    route_for_skill,
    sanitize_lesson,
)
from ciao.workspace_reroot import mark_born_per_root


def _config(tmp_path: Path, *names: str) -> CiaoConfig:
    """A config over ``tmp_path`` registered with ``names``."""
    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={name: WorkspaceConfig(name=name, vault_root=name) for name in names},
    )
    mark_born_per_root(tmp_path, runtime, list(names))
    return config


def _stock_draft(**overrides: str) -> dict[str, str]:
    """A filing payload for a packaged skill, safe to publish by construction."""
    payload = {
        "target": UPSTREAM_ISSUE,
        "skill": "web-research",
        "title": "Document the offline fallback for a timed-out fetch",
        "change": (
            "State that a fetch that times out must be retried once with a longer "
            "budget before its result is reported as absent."
        ),
        "body": (
            "A search fetch that times out is reported as \"no results\" and the "
            "agent concludes the source has nothing. Retrying once with a larger "
            "timeout recovers the result. Document that a timeout is a transport "
            "failure rather than an empty answer, and that the retry happens "
            "before the answer is reported."
        ),
        "repository": "example/tools",
        "version": "1.4.0",
        "private_evidence": "chat-42 turn 7: the fetch reported no results",
    }
    payload.update(overrides)
    return payload


class _Github:
    """A recording double for the two `gh` seams.

    ``hits`` is what the search returns and ``raise_on_create`` is how a network
    failure is simulated. ``created`` records every call, so a test can assert
    that a retry did not open a second issue, and ``searched_in`` records the
    repository each search was aimed at, so a test can assert the dedupe looked
    where the filing goes.
    """

    def __init__(
        self,
        hits: list[str] | None = None,
        *,
        url: str = "https://github.com/example/tools/issues/7",
        raise_on_create: BaseException | None = None,
        raise_on_search: BaseException | None = None,
    ) -> None:
        self.hits = list(hits or [])
        self.url = url
        self.raise_on_create = raise_on_create
        self.raise_on_search = raise_on_search
        self.searches: list[str] = []
        self.searched_in: list[str] = []
        self.created: list[tuple[str, str, str]] = []

    def search(self, *, title: str, body: str, repository: str = "") -> list[str]:
        del body
        self.searches.append(title)
        self.searched_in.append(repository)
        if self.raise_on_search is not None:
            raise self.raise_on_search
        return list(self.hits)

    def create(self, *, title: str, body: str, repository: str) -> str:
        self.created.append((title, body, repository))
        if self.raise_on_create is not None:
            raise self.raise_on_create
        return self.url


# ── Filing ──────────────────────────────────────────────────────────────────


def test_one_draft_per_distinct_change_not_per_sighting(tmp_path: Path) -> None:
    """Two passes that reached the same conclusion are one row.

    The evidence differs — a different transcript, a different excerpt — and the
    change is the same. A person deciding "should this be an upstream issue"
    once is the whole point, so identity is the change and the sightings
    accumulate on the record.
    """
    config = _config(tmp_path, "work")
    first = file_draft(config, "work", **_stock_draft())
    second = file_draft(
        config,
        "work",
        **_stock_draft(private_evidence="chat-99 turn 3: same failure, other run"),
    )

    assert second.id == first.id
    assert len(read_records(config, "work")) == 1
    record = read_records(config, "work")[0]
    # Both sightings survive: a re-run usually sees a different excerpt, and
    # losing the first would lose the run that led here.
    assert "chat-42" in record.private_evidence
    assert "chat-99" in record.private_evidence


def test_a_distinct_change_is_a_distinct_row(tmp_path: Path) -> None:
    """Deduplication is on the change, so a second change is a second question."""
    config = _config(tmp_path, "work")
    first = file_draft(config, "work", **_stock_draft())
    second = file_draft(
        config,
        "work",
        **_stock_draft(
            change="State that a fetch that 404s is retried against the archive URL."
        ),
    )

    assert second.id != first.id
    assert sorted(draft.id for draft in read_queue(config, "work")) == sorted(
        [first.id, second.id]
    )


def test_re_worded_but_identical_change_is_the_same_row(tmp_path: Path) -> None:
    """Identity is normalized, so capitalization and punctuation are not a new row.

    Compared in `normalized_statement` form — the same comparison the learning
    model uses to fold two sightings of one statement — so a pass that quotes
    the finding with different spacing and capitalization lands on the record
    already queued.
    """
    config = _config(tmp_path, "work")
    first = file_draft(config, "work", **_stock_draft())
    second = file_draft(
        config,
        "work",
        **_stock_draft(
            change=(
                "state that a fetch that times out must be retried once, with a "
                "longer budget, before its result is reported as absent."
            )
        ),
    )

    assert second.id == first.id
    assert len(read_records(config, "work")) == 1


def test_filing_writes_one_review_bullet_and_does_not_stack_it(tmp_path: Path) -> None:
    """The queue gets a `[review]` row naming the record, exactly once.

    The bullet is the one line the review surface renders; the details live in
    the sidecar. Re-filing must not add a second bullet for one finding, which
    is the failure a dedupe that only guards the sidecar leaves behind.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    file_draft(config, "work", **_stock_draft())

    queue = upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8")
    bullets = [parse_bullet(line) for line in queue.splitlines()]
    rows = [b for b in bullets if b is not None and b.target == draft.id]
    assert len(rows) == 1
    assert rows[0].kind == "[review]".strip("[]")
    # The destination is in the line itself, because it is the decision the
    # person is being asked for and it has to survive in the queue.
    assert "upstream issue" in rows[0].text
    assert "web-research" in rows[0].text


def test_a_new_skill_draft_says_so_in_its_own_bullet(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(
        config,
        "work",
        target=NEW_SKILL,
        skill="invoice-recon",
        title="Add a skill for reconciling an invoice against its ledger",
        change="Create skills/invoice-recon with a trigger and one reconciliation step.",
    )
    queue = upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8")
    assert "new skill" in queue
    assert draft.target == NEW_SKILL


def test_the_record_keeps_the_private_provenance_the_body_does_not(tmp_path: Path) -> None:
    """The whole point of the split: the local record holds what the issue cannot.

    The bullet a stranger never sees names the skill; the JSON beside it holds
    the transcript excerpt, so refusing to publish is never losing the finding.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())

    record = json.loads(
        upstream_drafts.sidecar_path(config, "work", draft.id).read_text(encoding="utf-8")
    )
    assert record["private_evidence"] == "chat-42 turn 7: the fetch reported no results"
    assert "chat-42" not in draft.body


def test_filing_refuses_a_draft_with_nothing_to_approve(tmp_path: Path) -> None:
    """A draft with no change is a question with no answer, refused by name."""
    config = _config(tmp_path, "work")
    for field in ("skill", "title", "change"):
        payload = _stock_draft(**{field: "   "})
        with pytest.raises(DraftRefused):
            file_draft(config, "work", **payload)
    with pytest.raises(DraftRefused):
        file_draft(config, "work", **_stock_draft(target="whatever"))


def test_a_learning_link_without_an_id_is_refused(tmp_path: Path) -> None:
    """A link naming no learning is not a link, and dropping it is worse.

    `learning_settlement` folds links by id; an origin with an empty id is a
    finding that would silently never be counted, so the whole entry fails.
    """
    config = _config(tmp_path, "work")
    with pytest.raises(DraftRefused, match="learning_id"):
        file_draft(config, "work", **_stock_draft(origins=[{"finding": "x"}]))
    with pytest.raises(DraftRefused, match="unknown field"):
        file_draft(
            config,
            "work",
            **_stock_draft(origins=[{"learning_id": "l1", "state": "applied"}]),
        )


def test_a_learning_link_round_trips_through_the_sidecar(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(
        config,
        "work",
        **_stock_draft(
            origins=[
                {
                    "learning_id": "lrn-1",
                    "finding": "the timeout was reported as an empty answer",
                    "source_revision": "abc123",
                }
            ]
        ),
    )
    stored = read_records(config, "work")[0]
    assert stored.origins == draft.origins
    assert stored.origins[0]["learning_id"] == "lrn-1"


# ── Sanitization ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("a chat or archive path", "Seen in logs/Chats/abc/transcript.md last week"),
        ("a vault path", "Filed under memory-vault/personal/Workspace/"),
        ("an absolute home path", "It lives at /Users/someone/skills/web-research/SKILL.md"),
        ("a transcript excerpt marker", "The user said, turn 12, that it always times out"),
        ("a credential", "The config carries ghp_abcdefghijklmnopqrst in the env"),
        ("a credential", "The env holds github_pat_11ABCDEFG0abcdefghijklmnop in CI"),
        ("a credential", "The token is glpat-ABCDEFGHIJKLMNOPQRST in the job log"),
        ("a secret assigned to a field", "The README says password=hunter2isnotsafe"),
        ("a secret assigned to a field", "api_key: 'zzz-not-in-the-repo-either'"),
        ("an authorization header", "The dump showed `Authorization: Bearer abcdef0123456789z`"),
        ("a private key block", "Pasted -----BEGIN OPENSSH PRIVATE KEY----- into the log"),
        ("an email address", "Reported by the maintainer at maintainer@example.org"),
        ("a placeholder standing in for a person", "The <user> asked for a retry"),
    ],
)
def test_a_body_carrying_something_private_is_refused(label: str, body: str) -> None:
    """Refused, and refused by name — never redacted.

    A partially redacted issue is one whose author no longer knows what they
    published, so the whole body fails and the caller keeps the private text in
    `private_evidence` and files a rephrased one.

    The credential rows are deliberately several *shapes* rather than several
    vendor prefixes: naming four tokens somebody has met before made "a
    credential" mean those four, and every other secret walked straight through.
    """
    with pytest.raises(SanitizeRefused, match=re.escape(label)):
        sanitize_lesson(body)


def test_a_reproducible_body_passes(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    assert draft.body.startswith("A search fetch that times out")
    # A body is not required: a new-skill draft's change is not a public artifact.
    assert (
        file_draft(
            config,
            "work",
            target=NEW_SKILL,
            skill="invoice-recon",
            title="t",
            change="c",
        ).body
        == ""
    )


def test_a_draft_whose_body_would_leak_is_never_written(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    with pytest.raises(SanitizeRefused):
        file_draft(config, "work", **_stock_draft(body="it happened in turn 4 of chat-1"))
    assert read_records(config, "work") == []


def test_an_empty_body_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    with pytest.raises(SanitizeRefused, match="empty"):
        file_draft(config, "work", **_stock_draft(body="   "))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Fix the fetch timeout reported at /Users/someone/work"),
        ("title", "Reconcile the invoice maintainer@example.org complained about"),
        ("version", "1.4.0 (built from logs/Chats/abc)"),
        ("skill", "/Users/someone/skills/web-research"),
    ],
)
def test_a_field_that_reaches_gh_is_sanitized_too(
    tmp_path: Path, field: str, value: str
) -> None:
    """A title is as public as a body, and the gate used to skip it.

    `sanitize_lesson` ran on `body` only, so `title`, `skill` and `version` went
    straight into `gh issue create --title` with the private-pattern check never
    applied: a path, a name or a token in a title published exactly as loudly as
    one in a paragraph, and the refusal everyone relied on did not fire.
    """
    config = _config(tmp_path, "work")
    with pytest.raises(SanitizeRefused, match=field):
        file_draft(config, "work", **_stock_draft(**{field: value}))
    assert read_records(config, "work") == []


def test_a_private_title_is_never_written_at_all(tmp_path: Path) -> None:
    """The refusal is at the door, so nothing reaches the record either.

    A title is what the review queue shows a person deciding whether to approve
    a *public* issue, so a leaking one has to fail at filing rather than be
    caught later by whoever reads the queue.
    """
    config = _config(tmp_path, "work")
    with pytest.raises(SanitizeRefused, match="title"):
        file_draft(
            config,
            "work",
            **_stock_draft(
                title="Reproduce the failure from turn 12 of the support chat"
            ),
        )
    assert read_records(config, "work") == []
    assert not upstream_drafts.queue_path(config, "work").exists()


@pytest.mark.parametrize(
    "repository",
    [
        "example/tools --repo other/repo",
        "/srv/git/example/tools",
        "example",
        "https://github.com/example/tools",
        "../../etc",
    ],
)
def test_an_owning_repository_that_is_not_one_is_refused(
    tmp_path: Path, repository: str
) -> None:
    """`--repo` is where a public write goes, so its shape is checked at the door.

    An unidentifiable owner is an attended question the plan says to ask rather
    than answer here. A value that is not a repository at all is worse than
    none: `gh` would take the next argument, or fall back to the directory the
    command ran in, and both are a guess about somebody else's project.
    """
    config = _config(tmp_path, "work")
    with pytest.raises(DraftRefused, match="owning repository"):
        file_draft(config, "work", **_stock_draft(repository=repository))
    assert read_records(config, "work") == []


def test_an_unidentifiable_repository_is_stored_empty_not_guessed(tmp_path: Path) -> None:
    """Empty is a legitimate value: the plan says to say so rather than guess."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft(repository="  "))
    assert draft.repository == ""
    assert draft.lifecycle == DRAFT_PENDING


# ── Approval ────────────────────────────────────────────────────────────────


def test_an_unattended_run_cannot_file(tmp_path: Path) -> None:
    """The deferral, enforced where the request would be made.

    `UNATTENDED_DEFERRED_ACTIONS` already lists opening a public issue; this is
    the code that turns the list into a refusal, and it happens before anything
    is read or written so a caller cannot half-file and then discover it was
    unattended.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    github = _Github()

    with pytest.raises(UnattendedRefused, match="attended"):
        approve_draft(
            config,
            draft.id,
            unattended=True,
            search=github.search,
            create=github.create,
        )

    assert github.created == []
    assert github.searches == []
    assert read_records(config, "work")[0].lifecycle == DRAFT_PENDING
    # Still queued, so the deferred item is visible to the run that reports it.
    assert [d.id for d in read_queue(config, "work")] == [draft.id]


def _lease_the_vault(config: CiaoConfig, workspace: str) -> str:
    """Take the curation lease the unattended Workspace care run holds."""
    from ciao.curation_run import begin_run

    lease = begin_run(
        Path(config.workspace_vault_root(workspace)),
        holder="nightly:1",
        ttl_s=600,
    )
    return str(lease.holder)


def test_the_unattended_guard_is_read_off_the_run_not_a_caller_flag(tmp_path: Path) -> None:
    """The guard nobody can forget, because nobody supplies it.

    A caller-supplied `unattended=` only proves the caller read its own
    argument — and the CLI passed `False` on every path, so the parameter was a
    comment rather than a control. The curation lease is the run's own state: a
    live one means an automation is working on this very vault with nobody in the
    room, which is exactly what the deferral is about.
    """
    config = _config(tmp_path, "work")
    assert upstream_drafts.unattended_run(config, "work") == ""
    _lease_the_vault(config, "work")
    assert upstream_drafts.unattended_run(config, "work") == "nightly:1"


def test_an_unattended_run_cannot_file_create_or_settle(tmp_path: Path) -> None:
    """All three decisions, refused from the run's own state.

    An issue filing is public, a skill creation writes a file every session
    loads, and a settlement is the answer a person owes. One run reaches all
    three through the same `ciao skill-draft-approve` / `skill-draft-reject`, so
    a guard on only one of them is a guard the run routes around by picking the
    other verb.
    """
    config = _config(tmp_path, "work")
    stock = file_draft(config, "work", **_stock_draft())
    new = file_draft(config, "work", **_new_skill_payload(tmp_path))
    github = _Github()
    _lease_the_vault(config, "work")

    with pytest.raises(UnattendedRefused, match="unattended run"):
        approve_draft(config, stock.id, search=github.search, create=github.create)
    with pytest.raises(UnattendedRefused, match="unattended run"):
        create_new_skill(
            config, new.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )
    with pytest.raises(UnattendedRefused, match="unattended run"):
        reject_draft(config, new.id, reason="not worth it")

    assert github.created == [] and github.searches == []
    assert not (config.agent_root("work") / "skills" / "invoice-recon").exists()
    # Nothing settled: both rows are still queued, for the person to decide.
    assert [d.id for d in read_queue(config, "work")] == sorted([stock.id, new.id])


def test_a_released_lease_makes_the_decision_attended_again(tmp_path: Path) -> None:
    """A person is not locked out by a run that has finished.

    `curation-end` releases the lease, so the guard is scoped to the run rather
    than to the vault: a decision made after the nightly pass closed is an
    attended one, and refusing it would make the draft un-actionable until the
    next run happened to leave a window.
    """
    from ciao.curation_run import end_run

    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    _lease_the_vault(config, "work")
    end_run(Path(config.workspace_vault_root("work")), holder="nightly:1")

    github = _Github()
    stored = approve_draft(config, draft.id, search=github.search, create=github.create)

    assert stored is not None and stored.lifecycle == DRAFT_FILED


def test_approval_searches_before_it_creates(tmp_path: Path) -> None:
    """One matching open issue is linked, not duplicated."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    github = _Github(hits=["https://github.com/example/tools/issues/3"])

    stored = approve_draft(
        config, draft.id, search=github.search, create=github.create
    )

    assert stored is not None
    assert stored.lifecycle == DRAFT_FILED
    assert stored.issue_url == "https://github.com/example/tools/issues/3"
    assert github.searches == [draft.title]
    assert github.created == []


def test_the_dedupe_looks_in_the_repository_the_issue_would_go_to(
    tmp_path: Path,
) -> None:
    """The two `gh` calls name the same `--repo`, or the dedupe is not one.

    `search_existing_issues` used to pass no `--repo` at all, so `gh` searched the
    repository of the current directory while the create named the draft's. The
    consequence was not a slow search but a broken guarantee: a retry after a
    create that succeeded and failed to settle found nothing in the target repo
    and opened a second issue for one finding — which is the whole thing the
    search exists to prevent.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft(repository="other/tools"))
    github = _Github()

    approve_draft(config, draft.id, search=github.search, create=github.create)

    assert github.searched_in == ["other/tools"]
    assert [repository for _title, _body, repository in github.created] == [
        "other/tools"
    ]


def test_a_draft_with_no_owning_repository_files_nothing(tmp_path: Path) -> None:
    """Held, with the reason a person can act on.

    `gh`'s own default for a missing `--repo` is the repository of the current
    directory, so filing without one publishes into a project nobody identified.
    The plan says to state "the owning repository if identifiable" and not to
    guess, so the draft waits and says what is missing.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft(repository=""))
    github = _Github()

    held = approve_draft(config, draft.id, search=github.search, create=github.create)

    assert held is not None
    assert held.lifecycle == DRAFT_PENDING
    assert "owning repository not identified" in held.reason
    assert github.searches == [] and github.created == []
    assert [d.id for d in read_queue(config, "work")] == [draft.id]


def test_create_issue_never_falls_back_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--repo` is always present, and an empty value is a refusal.

    The `gh` default is the repository of the directory the command happened to
    run in, which for a scheduled agent is its workspace — so the fallback was a
    public issue in a project nobody chose. `approve_draft` holds the draft
    before it gets here; this is the backstop for any other caller.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        upstream_drafts,
        "_run_gh",
        lambda args, timeout=30.0: calls.append(list(args)) or "https://example/1",
    )

    upstream_drafts.create_issue(title="t", body="b", repository="example/tools")
    assert calls[0][:3] == ["issue", "create", "--repo"]
    assert "example/tools" in calls[0]

    with pytest.raises(DraftRefused, match="owning repository"):
        upstream_drafts.create_issue(title="t", body="b", repository="")
    assert len(calls) == 1


def test_approval_creates_only_when_the_search_is_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    github = _Github()

    stored = approve_draft(
        config,
        draft.id,
        reason="checked, reproducible",
        search=github.search,
        create=github.create,
    )

    assert stored is not None and stored.lifecycle == DRAFT_FILED
    assert stored.issue_url == "https://github.com/example/tools/issues/7"
    assert stored.reason == "checked, reproducible"
    title, body, repository = github.created[0]
    assert title == draft.title
    assert body == draft.body
    # The repository the draft named is passed through; a caller that could not
    # identify one leaves it empty rather than having one guessed here.
    assert repository == "example/tools"


def test_an_ambiguous_search_holds_the_draft_rather_than_guessing(tmp_path: Path) -> None:
    """Two plausible matches means this code cannot say which is the right one.

    Attaching the record to the wrong thread is worse than leaving it pending,
    and picking the first would be a guess presented as a decision.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    github = _Github(hits=["https://example/1", "https://example/2"])

    stored = approve_draft(
        config, draft.id, search=github.search, create=github.create
    )

    assert stored is not None
    assert stored.lifecycle == DRAFT_PENDING
    assert "2 open issues" in stored.reason
    assert github.created == []
    assert [d.id for d in read_queue(config, "work")] == [draft.id]


def test_a_failed_github_call_leaves_the_draft_pending_and_retryable(tmp_path: Path) -> None:
    """A `gh` failure is not an answer, and the retry must not duplicate.

    The search runs on every attempt, so a retry after a partial success links
    the issue the first attempt created instead of opening another one.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    failing = _Github(raise_on_create=OSError("gh failed: connection reset"))

    held = approve_draft(
        config, draft.id, search=failing.search, create=failing.create
    )
    assert held is not None
    assert held.lifecycle == DRAFT_PENDING
    assert "could not reach GitHub" in held.reason
    assert "retry" in held.reason.lower()
    assert [d.id for d in read_queue(config, "work")] == [draft.id]

    # The retry reaches GitHub and finds what the first attempt created.
    retry = _Github(hits=["https://github.com/example/tools/issues/7"])
    settled = approve_draft(
        config, draft.id, search=retry.search, create=retry.create
    )
    assert settled is not None
    assert settled.lifecycle == DRAFT_FILED
    assert retry.created == []
    assert read_queue(config, "work") == []


def test_an_ambiguous_empty_reply_holds_the_draft(tmp_path: Path) -> None:
    """`gh` returning no URL is not a filing, and must not read as one."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())

    stored = approve_draft(
        config, draft.id, search=_Github().search, create=lambda **_: ""
    )

    assert stored is not None
    assert stored.lifecycle == DRAFT_PENDING
    assert "no URL" in stored.reason
    assert read_queue(config, "work") != []


def test_a_second_approval_of_a_filed_draft_is_a_no_op(tmp_path: Path) -> None:
    """Idempotent by id: a settled draft has nothing left to file."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    github = _Github()
    approve_draft(config, draft.id, search=github.search, create=github.create)

    assert approve_draft(
        config, draft.id, search=github.search, create=github.create
    ) is None
    assert len(github.created) == 1


def test_a_rejected_draft_is_never_re_filed(tmp_path: Path) -> None:
    """Rejection is the answer, and a filing must not reopen it."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())

    settled = reject_draft(config, draft.id, reason="already fixed upstream")
    assert settled is not None and settled.lifecycle == DRAFT_REJECTED
    assert read_queue(config, "work") == []

    github = _Github()
    with pytest.raises(DraftRefused, match="rejected"):
        approve_draft(
            config, draft.id, search=github.search, create=github.create
        )
    assert github.created == []

    # Re-filing the same change keeps the settled record settled.
    again = file_draft(config, "work", **_stock_draft())
    assert again.lifecycle == DRAFT_REJECTED
    assert read_queue(config, "work") == []


def test_a_settled_draft_keeps_accumulating_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    reject_draft(config, draft.id, reason="no")

    again = file_draft(
        config, "work", **_stock_draft(private_evidence="a third sighting")
    )
    assert again.lifecycle == DRAFT_REJECTED
    assert len(read_records(config, "work")) == 1
    assert "a third sighting" in again.private_evidence


def test_a_settled_draft_leaves_the_queue(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    queue = upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8")
    assert any(
        (bullet := parse_bullet(line)) is not None and bullet.target == draft.id
        for line in queue.splitlines()
    )

    reject_draft(config, draft.id, reason="no")
    queue = upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8")
    assert not [line for line in queue.splitlines() if draft.id in line]


def test_the_decision_reaches_the_decisions_sidecar(tmp_path: Path) -> None:
    """A record may be deleted later; the answer has to outlive the row.

    The sidecar is the same append-only log the memory queue keeps, and the row
    is keyed under a synthetic `skill-draft:` text so a routing decision can
    never read as a remembered fact with the same wording.
    """
    from ciao.memory_proposals import dismissed_log_path

    config = _config(tmp_path, "work")
    draft = file_draft(
        config,
        "work",
        **_stock_draft(
            origins=[{"learning_id": "lrn-1", "finding": "timeout read as empty"}]
        ),
    )
    approve_draft(
        config, draft.id, search=_Github().search, create=_Github().create
    )

    assert upstream_drafts.decided_with(config, "work", draft.id) is True
    sidecar = dismissed_log_path(
        upstream_drafts.queue_path(config, "work")
    ).read_text(encoding="utf-8")
    assert upstream_drafts.decision_text(draft.id) in sidecar
    # One row per learning link, so a learning's history says which finding was
    # answered rather than only that the draft was.
    assert "lrn-1" in sidecar
    assert "timeout read as empty" in sidecar


def test_an_unknown_draft_id_is_none_not_an_error(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    assert find_draft(config, "nope") is None
    assert approve_draft(
        config, "nope", search=_Github().search, create=_Github().create
    ) is None
    assert reject_draft(config, "nope") is None


# ── New-skill creation ──────────────────────────────────────────────────────


def _new_skill_payload(tmp_path: Path, **overrides: str) -> dict[str, str]:
    payload = {
        "target": NEW_SKILL,
        "skill": "invoice-recon",
        "title": "Add a skill for reconciling an invoice against its ledger",
        "change": (
            "Create skills/invoice-recon: it loads when reconciling an invoice "
            "against a ledger, and its first instruction is to match on the "
            "invoice number before the amount."
        ),
    }
    payload.update(overrides)
    return payload


def _skill_text(name: str) -> str:
    return (
        f"---\nname: {name}\ndescription: Reconcile an invoice against a ledger\n"
        "---\n\n# Invoice reconciliation\n\nMatch the invoice number first.\n"
    )


def test_a_new_skill_draft_creates_syncs_and_then_settles(tmp_path: Path) -> None:
    """The row settles only once the file exists — creation is verified, not assumed.

    The sync runs after the write because a skill that exists only in `skills/`
    is not a skill a provider can see, and its failure is reported rather than
    raised: undoing a created file because a mirror failed would be a worse
    outcome than a created file with an honest note.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    synced: list[str] = []

    stored = create_new_skill(
        config,
        draft.id,
        content=_skill_text("invoice-recon"),
        sync=lambda: synced.append("ran"),
    )

    assert synced == ["ran"]
    assert stored is not None
    assert stored.lifecycle == DRAFT_FILED
    assert stored.issue_url.endswith("skills/invoice-recon/SKILL.md")
    written = config.agent_root("work") / "skills" / "invoice-recon" / "SKILL.md"
    assert written.read_text(encoding="utf-8") == _skill_text("invoice-recon")
    assert read_queue(config, "work") == []


def test_a_failed_sync_leaves_the_draft_pending_for_a_resume(tmp_path: Path) -> None:
    """A skill no provider can load is not the thing this row proposed.

    The old contract settled the row `filed` with a note that the sync did not
    finish, which left a record that read as verified while the skill was
    invisible — and a person had nothing to retry, because the row was gone. So
    the file is still created (undoing it would be worse), the row stays pending
    with the reason on it, and the re-approve below is the answer it invites.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))

    def _boom() -> None:
        raise RuntimeError("sync exploded")

    held = create_new_skill(config, draft.id, content=_skill_text("invoice-recon"), sync=_boom)

    assert held is not None and held.lifecycle == DRAFT_PENDING
    assert "sync did not complete" in held.reason
    assert "re-approve" in held.reason
    # The creation is not undone — the next sync picks it up — and the row is
    # still open, so the retry is a decision rather than a re-derivation.
    assert (config.agent_root("work") / "skills" / "invoice-recon" / "SKILL.md").is_file()
    assert [d.id for d in read_queue(config, "work")] == [draft.id]


def test_a_re_approve_after_a_failed_sync_resumes_and_settles(tmp_path: Path) -> None:
    """The resume the hold invites has to be takeable, or the hold is a dead end.

    `create_owned_skill` refuses a name that exists, so a retry would have hit
    the collision refusal and stopped. It does not, because a file holding
    *exactly* the submitted content is the previous attempt's own creation: the
    creation is skipped and only the sync re-runs.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    calls: list[str] = []

    def _boom() -> None:
        raise RuntimeError("sync exploded")

    create_new_skill(config, draft.id, content=_skill_text("invoice-recon"), sync=_boom)

    settled = create_new_skill(
        config,
        draft.id,
        content=_skill_text("invoice-recon"),
        sync=lambda: calls.append("synced"),
    )

    assert calls == ["synced"]
    assert settled is not None and settled.lifecycle == DRAFT_FILED
    assert "creation was skipped" in settled.reason
    assert settled.issue_url.endswith("skills/invoice-recon/SKILL.md")
    assert read_queue(config, "work") == []


def test_a_resume_will_not_overwrite_a_different_file_at_that_name(tmp_path: Path) -> None:
    """Only the previous attempt's own bytes count as a resume.

    A file at that name that is not what was submitted is somebody's source, and
    the collision refusal is what says so by name — quietly treating it as ours
    would settle a row against a file nobody proposed.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))

    def _boom() -> None:
        raise RuntimeError("sync exploded")

    create_new_skill(config, draft.id, content=_skill_text("invoice-recon"), sync=_boom)
    existing = config.agent_root("work") / "skills" / "invoice-recon" / "SKILL.md"
    existing.write_text(
        "---\nname: invoice-recon\ndescription: d\n---\n\nsomething else\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="already has a skills/invoice-recon entry"):
        create_new_skill(
            config, draft.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )
    assert "something else" in existing.read_text(encoding="utf-8")


def test_a_resume_reports_the_budget_the_first_attempt_reported(tmp_path: Path) -> None:
    """A held attempt's note is not the only place the budget is remembered.

    The hold is overwritten by the retry's reason, so anything only the first
    pass could see is lost with it. The size is the submitted bytes either way,
    so both passes say the same thing about a skill nobody can load.
    """
    from ciao.skills_inventory import MAX_SKILL_BYTES

    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    body = "\n".join(f"line {n}" for n in range(MAX_SKILL_BYTES // 4))
    content = (
        "---\nname: invoice-recon\ndescription: Reconcile an invoice\n---\n\n" + body
    )

    def _boom() -> None:
        raise RuntimeError("sync exploded")

    create_new_skill(config, draft.id, content=content, sync=_boom)
    settled = create_new_skill(config, draft.id, content=content, sync=lambda: None)

    assert settled is not None and settled.lifecycle == DRAFT_FILED
    assert "creation was skipped" in settled.reason
    assert "over the" in settled.reason


def test_a_refused_creation_settles_nothing(tmp_path: Path) -> None:
    """A collision is the caller's problem to resolve, not something to work around."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    existing = config.agent_root("work") / "skills" / "invoice-recon" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("---\nname: invoice-recon\ndescription: d\n---\n\nbody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="already has a skills/invoice-recon entry"):
        create_new_skill(
            config, draft.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )

    assert read_queue(config, "work") != []
    # The existing file is untouched: a refusal never becomes an edit.
    assert existing.read_text(encoding="utf-8").endswith("body\n")


def test_a_frontmatter_name_mismatch_is_refused(tmp_path: Path) -> None:
    """A skill is loaded under its frontmatter name, so a mismatch is unusable."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))

    with pytest.raises(ValueError, match="frontmatter name"):
        create_new_skill(
            config,
            draft.id,
            content="---\nname: something-else\ndescription: d\n---\n\nbody\n",
            sync=lambda: None,
        )
    assert not (config.agent_root("work") / "skills" / "something-else").exists()


def test_approve_refuses_to_file_an_issue_for_a_new_skill_draft(tmp_path: Path) -> None:
    """The two routes are different operations and a caller cannot confuse them.

    An issue filed upstream is a thing that happened; a skill created locally is
    a file a session will load, so only `create_new_skill` may settle one.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    github = _Github()

    with pytest.raises(DraftRefused, match="create_new_skill"):
        approve_draft(
            config, draft.id, search=github.search, create=github.create
        )
    assert github.created == []


def test_a_rejected_new_skill_draft_is_never_created(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    reject_draft(config, draft.id, reason="covered by notes")

    with pytest.raises(DraftRefused, match="rejected"):
        create_new_skill(
            config, draft.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )
    assert not (config.agent_root("work") / "skills" / "invoice-recon").exists()


def test_an_over_budget_skill_is_created_and_reported(tmp_path: Path) -> None:
    """The budget warns everywhere else, so it warns here too."""
    from ciao.skills_inventory import MAX_SKILL_BYTES

    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    body = "\n".join(f"line {n}" for n in range(MAX_SKILL_BYTES // 4))
    content = (
        "---\nname: invoice-recon\ndescription: Reconcile an invoice\n---\n\n" + body
    )

    stored = create_new_skill(config, draft.id, content=content, sync=lambda: None)

    assert stored is not None
    assert "over the" in stored.reason
    assert (config.agent_root("work") / "skills" / "invoice-recon" / "SKILL.md").is_file()


def test_a_creation_refuses_a_name_an_installed_copy_already_takes(tmp_path: Path) -> None:
    """A local skill silently shadowing an installed one hides it, not replaces it.

    `resolve_owned_skill` treats a real local skill shadowing a packaged one as
    owned, so a *creation* is the case that has to refuse: the person writing
    the new file did not know the copy underneath, and the next sync would keep
    the two in tension.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    installed = config.agent_root("work") / ".claude" / "skills" / "invoice-recon"
    installed.mkdir(parents=True)
    (installed / ".ciao-stock-skill").touch()

    with pytest.raises(ValueError, match="already exists"):
        create_new_skill(
            config, draft.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )
    assert not (config.agent_root("work") / "skills" / "invoice-recon").exists()


def test_a_creation_refuses_a_symlinked_catalog(tmp_path: Path) -> None:
    """The shared-mirror shape: a link is refused at the link, not after it."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_new_skill_payload(tmp_path))
    root = config.agent_root("work")
    root.mkdir(parents=True, exist_ok=True)
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    (root / "skills").symlink_to(shared, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        create_new_skill(
            config, draft.id, content=_skill_text("invoice-recon"), sync=lambda: None
        )
    assert list(shared.iterdir()) == []


# ── The routing decision, as code ───────────────────────────────────────────


def test_route_for_skill_reads_the_filesystem_not_a_flag(tmp_path: Path) -> None:
    """The three answers, decided by where a name actually lives.

    A model told "if the skill is stock, file upstream" has to work out which of
    three directories a name is in, and a stock copy is installed under exactly
    the path a pass would otherwise edit — so the answer is a backend one.
    """
    config = _config(tmp_path, "work")
    root = config.agent_root("work")
    owned = root / "skills" / "notes"
    owned.mkdir(parents=True)
    (owned / "SKILL.md").write_text("---\nname: notes\ndescription: d\n---\n\nb\n", encoding="utf-8")
    installed = root / ".claude" / "skills" / "web-research"
    installed.mkdir(parents=True)
    (installed / ".ciao-stock-skill").write_text("stock\n", encoding="utf-8")

    assert route_for_skill(config, "work", "notes") == "owned"
    assert route_for_skill(config, "work", "web-research") == "upstream"
    assert route_for_skill(config, "work", "never-heard-of-it") == "new"
    # A name that could address a second directory is not a name at all.
    assert route_for_skill(config, "work", "../escape") == "new"


def test_learnings_present_is_the_gate_on_the_lesson_route(tmp_path: Path) -> None:
    config = _config(tmp_path, "work")
    assert upstream_drafts.learnings_present(config, "work") is False

    path = tmp_path / "memory-vault" / "work" / "Workspace" / "Learnings.md"
    path.parent.mkdir(parents=True)
    path.write_text("## Active\n", encoding="utf-8")
    assert upstream_drafts.learnings_present(config, "work") is True


def test_a_sidecar_naming_another_workspace_is_not_read(tmp_path: Path) -> None:
    """A learning id is workspace-scoped, so a foreign record is not this vault's.

    Reading it would make another workspace's id look verified here, which is
    the one claim this sidecar exists to make honestly.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    path = upstream_drafts.sidecar_path(config, "work", draft.id)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"work"', '"elsewhere"'),
        encoding="utf-8",
    )

    assert upstream_drafts.read_sidecar(path, "work") is None
    assert read_records(config, "work") == []


def test_an_unreadable_sidecar_reads_as_no_record_rather_than_raising(tmp_path: Path) -> None:
    """A queue you cannot show is worse than one row short."""
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    upstream_drafts.sidecar_path(config, "work", draft.id).write_text("{", encoding="utf-8")

    assert upstream_drafts.read_sidecar(
        upstream_drafts.sidecar_path(config, "work", draft.id), "work"
    ) is None
    assert read_queue(config, "work") == []


# ── The stock catalog is never written ──────────────────────────────────────


def test_the_shipped_skill_files_are_byte_identical_after_a_full_cycle(
    tmp_path: Path,
) -> None:
    """No code path here opens a packaged, mirrored or shared skill for writing.

    The routing contract's second rule is that a stock file stays byte-identical
    whatever the workspace concludes about it. Rather than trusting that by
    inspection, this runs the whole lifecycle — file, approve, reject, create —
    and re-hashes the shipped package afterwards.
    """
    import hashlib

    def _digests() -> dict[str, str]:
        root = resources.files("ciao.stock").joinpath("skills")
        out: dict[str, str] = {}
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            with resources.as_file(entry) as path:
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        out[str(child.relative_to(path))] = hashlib.sha256(
                            child.read_bytes()
                        ).hexdigest()
        return out

    before = _digests()
    config = _config(tmp_path, "work")
    packaged = sorted(
        entry.name
        for entry in resources.files("ciao.stock").joinpath("skills").iterdir()
        if entry.is_dir()
    )[0]

    stock = file_draft(
        config,
        "work",
        target=UPSTREAM_ISSUE,
        skill=packaged,
        title="Clarify the failure mode this skill does not name",
        change="Name the timeout case explicitly, with what to do about it.",
        body="A timeout is a transport failure rather than an empty answer.",
        repository="raffaelefarinaro/ciaobot",
        version="1.0.0",
    )
    approve_draft(
        config, stock.id, search=_Github().search, create=_Github().create
    )

    other = file_draft(config, "work", **_stock_draft())
    reject_draft(config, other.id, reason="covered upstream")

    new = file_draft(config, "work", **_new_skill_payload(tmp_path))
    create_new_skill(
        config, new.id, content=_skill_text("invoice-recon"), sync=lambda: None
    )

    assert _digests() == before


def test_a_deleted_record_does_not_reopen_a_decided_change(tmp_path: Path) -> None:
    """The sidecar is what outlives the row, so the sidecar is what is honoured.

    A settled draft whose record somebody deleted would otherwise be re-queued
    by the next pass — and a rejection a person gave is exactly the answer that
    must not come back. The decision is read through `read_decisions` rather
    than a helper that gives up on a workspace with no queue file yet, which is
    exactly the install where a draft is the only thing in it.
    """
    from ciao.memory_proposals import dismissed_log_path

    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    reject_draft(config, draft.id, reason="not worth filing")
    upstream_drafts.sidecar_path(config, "work", draft.id).unlink()

    again = file_draft(
        config, "work", **_stock_draft(private_evidence="a later sighting")
    )

    assert again.lifecycle == DRAFT_REJECTED
    assert "deleted after it was decided" in again.reason
    assert "a later sighting" in again.private_evidence
    assert read_queue(config, "work") == []
    assert dismissed_log_path(
        upstream_drafts.queue_path(config, "work")
    ).is_file()


def test_a_deleted_filed_record_comes_back_as_filed_with_its_url(
    tmp_path: Path,
) -> None:
    """A `filed` decision reconstructs as `filed`, not as a rejection.

    The reconstruction used to write *any* surviving decision back as
    `rejected`, so a record deleted after a successful filing came back as
    "a person turned this down" — a judgement nobody made, on a change already
    public, and one the record could no longer point at. The lifecycle is read
    from the decision row and the URL comes back with it, because the decision
    is what outlives the file.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    approve_draft(config, draft.id, search=_Github().search, create=_Github().create)
    upstream_drafts.sidecar_path(config, "work", draft.id).unlink()

    again = file_draft(
        config, "work", **_stock_draft(private_evidence="a later sighting")
    )

    assert again.lifecycle == DRAFT_FILED
    assert again.issue_url == "https://github.com/example/tools/issues/7"
    assert "a later sighting" in again.private_evidence
    assert read_queue(config, "work") == []


def test_a_deleted_pending_record_comes_back_as_pending(tmp_path: Path) -> None:
    """The guard is for a decision, not for the file's existence.

    A draft that was never decided and whose record was deleted has no answer to
    preserve, so the next pass re-files it — which is the recovery a half-finished
    run needs.
    """
    config = _config(tmp_path, "work")
    draft = file_draft(config, "work", **_stock_draft())
    upstream_drafts.sidecar_path(config, "work", draft.id).unlink()

    again = file_draft(config, "work", **_stock_draft())

    assert again.lifecycle == DRAFT_PENDING
    assert [d.id for d in read_queue(config, "work")] == [draft.id]
