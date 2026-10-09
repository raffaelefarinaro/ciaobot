# Task and project lifecycle knowledge

## Resume block

- Status: implementing
- Current checkpoint: C1–C4 in code; C5 docs and verification in progress
- Next action: run the CI gates and inspect the completion sheets in the browser
- Decisions accepted: Q-01 always prompt, note optional only via Complete without note; Q-02 notes are editable and revision-checked, no delete; Q-03 first reusable occurrence is a learning, a second distinct completion id is a skill candidate, one occurrence is enough only to correct an owned skill
- Blocker: none
- Implementation repository: `/Users/raffaelefarinaro/repos/ciaobot`
- Generated plan output: `docs/TASK_LIFECYCLE_MEMORY_PLAN.md`
- Visual companions: none; the workflow is adequately reviewable in this document
- Verified on: 2026-10-09 against `ciao/task_board.py`, `ciao/task_resolution.py`, `ciao/task_log.py`, `ciao/control_plane.py`, `ciao/web/memory_pass.py`, `ciao/web/project_chats.py`, `ciao/web/routes_tasks.py`, `web/src/components/TaskBoardView.vue`, `web/src/components/ProjectView.vue`, `web/src/stores/projects.ts`, `web/src/lib/types.ts`, `DESIGN.md`, and `docs/ARCHITECTURE.md`

## Outcome and user value

Give work one understandable lifecycle:

1. While a task is active, the user or agent records short progress updates.
2. Completing a task or project asks what happened and why.
3. Ciaobot keeps those records with the task or project in plain Markdown.
4. One completion-time memory pass uses the description, updates, result, and relevant agent work to update project knowledge and identify reusable procedures.
5. Repeated procedures strengthen a learning and can produce a reviewable skill proposal; they never silently edit or create a skill.

The user sees one chronological **Activity** timeline. Storage may retain distinct typed sections so completion records, agent attempts, and progress notes keep their different semantics.

## Scope and non-goals

In scope:

- User and agent progress updates on tasks in progress or in review.
- A completion prompt on every user gesture that moves a task to Done.
- A completion prompt when completing a project.
- A chronological Activity view combining updates, delegation attempts, and completions.
- A completion-time evidence bundle for memory and reusable-procedure extraction.
- Reviewable skill proposals backed by concrete task evidence and recurrence.

Out of scope for the first release:

- Running a memory pass for every progress update.
- Automatically copying every agent turn or tool call into the task.
- A new top-level inbox or a second task/project notes database.
- Silent skill edits or automatic skill creation.
- Migrating the existing delegation and completion sections into one new on-disk format.

## Current-state evidence

Observed:

- `ciao/task_resolution.py` already stores completion time, user resolution, and linked attempt in a durable fenced Markdown section.
- `TaskBoardView.vue` already offers **Complete with resolution…** and sends the resolution with the completion POST, but the ordinary checkmark, drag-to-Done, status control, and **Approve Done** can complete without opening that prompt.
- `ciao/web/memory_pass.py` already supports two relevant paths:
  - a manual task resolution can trigger a memory pass without inventing a chat;
  - an approved delegated task archives its chat and adds a procedure-focused prompt for memory and skill proposals.
- `ciao/task_log.py` already persists agent-attempt summaries separately from the task description.
- No task-update or work-journal section exists.
- `ProjectView.vue` currently uses a confirmation dialog with no completion note.
- `ProjectChatManager.complete_project` moves the project folder to `projects/completed/`, rewrites project status, then archives/removes its chats. It stores no project closure record.
- The memory system already routes reusable lessons through `Workspace/Learnings.md`, owned-skill proposals, and review drafts for packaged or new skills.

Assumed:

- “Ticket” means a task-board task, while “project” means the containing Ciaobot project. This plan supports both rather than treating the terms as interchangeable.
- Progress updates are useful durable evidence but should not each incur a model call.

## Recommended direction

### 1. Add typed task updates, not free text in the description

Add a durable `TaskUpdate` record in a new fenced Markdown section:

- stable id
- recorded timestamp
- actor: `user` or `agent`
- Markdown text
- optional attempt/chat reference
- optional edited timestamp

Expose a revision-checked append operation through the task service, HTTP API, and `ciao task add-update`. User prose crosses the CLI through a file argument, following the existing body/resolution safety pattern.

The task description remains the requested work. Adding an update therefore does not falsely mark the description as “Changed since delegated.”

When a live delegated attempt exists, the UI offers two explicit actions:

- **Save update**
- **Save and send to agent**

Saving never injects a message into a running chat by surprise.

The working agent can append updates with `ciao task add-update`. Each one is labeled `agent`, optionally linked to the current attempt and chat, and shown in the same Activity timeline as the user's notes. The agent writes one when it reaches a milestone, hits a blocker, or finishes a distinct step. It does not copy every message or tool call.

An agent update is progress evidence. It is not the user's completion note, and it cannot move the task to Done. That completion, and the note about how or why it was finished, stay the user's.

#### What the working agent reads

The implementation plan itself is not injected into agents. It is a development
specification. The task record is the runtime source.

On initial delegation, the engine builds a bounded prompt from the task Markdown:

- title, description, acceptance criteria, due date, and project;
- existing progress updates, preserving their actor and timestamp;
- the task id and typed `ciao task get` / `ciao task add-update` operations.

The raw file is not injected blindly: engine metadata is rendered as quoted
evidence, not instructions. The existing completion and delegation sections keep
their current typed treatment.

After delegation:

- **Save and send to agent** stores the user update first, then sends that update
  to the linked task chat as an ordinary message.
- **Save update** stores it without interrupting the agent. Unseen updates are
  included when the task attempt is next resumed or retried, and the agent can
  fetch the current record with `ciao task get`.
- An update written by the working agent is already in that chat's context and
  is also persisted to the task record for later attempts and the memory pass.

Each attempt tracks the last task-update id it was given, so a resume sends only
unseen updates rather than repeatedly growing the prompt.

#### Where the record lives

There is one source of truth throughout the lifecycle:
`<vault>/Workspace/Tasks/<task-id>.md`.

The file exists from task creation, not only after completion. While work is
active it contains the description and updates. When the user completes it, the
same atomic write changes `status: done` and appends the completion record and
resolution. It remains a normal, readable Markdown file and continues to back
the Done column. Do not create a second “completed task” document or copy it to a
second folder: that would create two records that can disagree.

### 2. Present one Activity timeline

Compose, on read, one chronological timeline from:

- progress updates;
- durable delegation-attempt summaries;
- completion records.

Keep the existing typed sections on disk. They have different ownership and rewrite rules, and replacing them would create a risky migration for existing vaults. “All together” is a UI and evidence-bundle guarantee, not a claim that unlike events must share one parser.

### 3. Ask on every completion

Every task transition to Done opens the same completion sheet, including:

- checkmark;
- drag or keyboard move to Done;
- editor status selection;
- **Approve Done** for delegated work.

Use one Markdown field with the prompt:

> How was this completed, or why was it closed? What worked, and how did you verify it?

Recommended behavior: the prompt is always shown, but the note remains optional through an explicit **Complete without note** action. This captures knowledge without making quick or trivial work impossible to close.

The write remains atomic: completion status and note land together. A failed or conflicting write keeps the draft, as the current resolution sheet already does.

### 4. Add project closure as the same concept

Replace the project confirmation with a completion sheet containing:

- required choice: **Completed** or **Stopped / no longer needed**;
- optional closure note;
- a short summary of open tasks and active chats that will be archived.

Before moving the folder, append a durable project closure section to the canonical project document. Then archive project chats and enqueue their normal memory passes. Finally enqueue one project-closure pass that resolves the canonical project document at execution time from workspace + project folder, whether it is under `active/` or `completed/`.

The project-closure pass runs after the chat passes in the workspace FIFO. It updates the final project summary from the closure note and already-extracted knowledge rather than asking one oversized prompt to reread every transcript.

### 5. Build one completion-time memory evidence bundle

For a task, the memory pass receives typed, clearly fenced evidence:

- task title and description;
- ordered task updates;
- user completion resolution;
- latest approved agent summary;
- archived chat transcript when one exists;
- project identity and canonical document.

Evidence roles remain explicit:

- completion note: the user’s verdict and intent;
- updates: chronological claims by their recorded actor;
- agent summary/transcript: work evidence, not automatically a user preference;
- task/project state: an event record, not itself a global memory.

The pass extracts only:

- current project state and decisions into the project document;
- durable facts into their normal memory destinations;
- reusable procedures into Learnings or a review proposal.

It does not remember “task X was completed” globally; that event already lives with the task.

### 6. Use recurrence as evidence for skills

Recommended policy:

- First reusable occurrence: create or strengthen a structured learning with source task/completion ids.
- Second independent occurrence: it becomes a skill candidate.
- A direct user correction or a demonstrated defect in an existing owned skill may create a proposal after one occurrence because the evidence is already specific.
- New-skill creation and every skill edit remain review actions.

The recurrence key should describe the procedure, not the task title. Distinct completion ids prevent a retry or edited note from inflating the count.

## Alternatives and rejected options

### Put updates directly in the task description

Rejected because description edits currently mean the delegated requirements changed. Progress history and requested work need different semantics.

### Run memory after every update

Rejected because it creates cost, noise, races between passes, and premature conclusions. Updates are accumulated and interpreted at completion.

### Automatically summarize every agent turn into an update

Rejected for the first release because it duplicates the transcript and produces low-signal history. Explicit milestones and the existing final attempt summary are better evidence.

### Replace all existing task history with one universal event format

Rejected because existing vaults already carry delegation and completion sections with distinct rewrite rules. The UI and memory pass can compose them without a destructive migration.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Updates are typed task events, separate from the description. | Preserves delegation revision semantics. | Proposed |
| D-02 | Memory runs at completion, not per update. | Better signal, lower cost, sequential vault writes. | Proposed |
| D-03 | One Activity timeline composes existing and new typed records. | Unified UX without breaking portable Markdown history. | Proposed |
| D-04 | Completion status and resolution are one atomic write. | Prevents orphan notes and keeps conflict behavior honest. | Observed and retained |
| D-05 | Skills remain proposal-driven. | A repeated workflow is evidence, not authorization to modify instructions. | Observed and retained |
| D-06 | Project closure is queued after its chat-memory work. | Makes the final pass a consolidation step rather than a competing writer. | Proposed |
| D-07 | The working agent may append progress updates, and cannot complete the task. | Progress belongs to whoever did the step; the user's verdict stays separate. | Accepted |
| D-08 | One Markdown task file survives the whole lifecycle. | Avoids duplicate active/completed records and keeps the vault portable. | Accepted |
| D-09 | Delegation injects a bounded typed task snapshot; resume/retry inject only unseen updates. | Gives agents current context without treating metadata as instructions or repeatedly growing prompts. | Accepted |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Must a completion note be non-empty? | No. Always show the prompt, with an explicit **Complete without note** action. | Open |
| Q-02 | Can an update be edited? | Yes, revision-checked, retaining `edited_at`; no delete control in the first release. | Open |
| Q-03 | When should recurrence propose a new skill? | Two independent completions; one is enough only for an explicit correction to an existing skill. | Open |

## Not yet specified (fog of war)

- Whether a later release should add project-level progress updates in addition to task updates.
- Whether task Activity needs filters once real workspaces show its typical size.
- Whether completed-task retention should gain an archive policy separate from the current Done filtering.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Completion UX | Ask for how or why the work was completed. | Use one completion sheet for every Done gesture and for project completion. | open | User request, 2026-10-09 |
| F-02 | In-progress work | Record user or agent progress notes. | Add typed task updates and a composed Activity timeline. | open | User request, 2026-10-09 |
| F-03 | Memory and skills | Use lifecycle information when concluding a ticket and detect recurring procedures. | Build one typed evidence bundle at completion and route recurrence through Learnings and reviewable skill proposals. | open | User request, 2026-10-09 |
| F-04 | Agent updates | The agent should also be able to add task updates. | `ciao task add-update` records agent milestones and blockers in Activity. Completion remains the user's. | accepted | User confirmation, 2026-10-09 |
| F-05 | Agent context and storage | Clarify whether agents read the document, whether it is injected, and where completed work is saved. | Inject a bounded task snapshot and unseen updates; keep one `Workspace/Tasks/<id>.md` file before and after completion. | accepted | User clarification, 2026-10-09 |

## Implementation checkpoints

### C0. Approval

- Resolve Q-01 through Q-03.
- Mark this plan approved.

Exit evidence: the decision log records the accepted defaults.

### C1. Durable task updates

- Add a source-preserving task-update parser/renderer.
- Add revision-checked append/edit service operations.
- Add PWA and agent API routes plus `ciao task add-update`.
- Keep updates out of delegated description and changed-since-delegated calculation.

Likely areas: `ciao/task_updates.py`, `ciao/task_board.py`, `ciao/control_plane.py`, `ciao/agent_cli.py`, `ciao/web/routes_tasks.py`, route wiring, API docs, and focused backend tests.

Exit evidence: user and agent updates round-trip in task Markdown and stale writes are refused.

### C2. Task Activity and completion UX

- Extend task transport types with updates and composed activity records.
- Add update entry and Activity rendering to the task detail.
- Route every Done gesture through the existing resolution sheet.
- Add **Save and send to agent** only when a live linked chat exists.

Likely areas: `web/src/lib/types.ts`, `web/src/stores/taskBoard.ts`, `web/src/components/TaskBoardView.vue`, and component/store tests.

Exit evidence: mouse, keyboard, touch, drag, status selection, and review approval all show the same completion decision.

### C3. Completion evidence and recurrence

- Extend manual and delegated task memory helpers with the typed evidence bundle.
- Deduplicate recurrence by source completion id.
- Preserve existing proposal review and skill ownership boundaries.

Likely areas: `ciao/web/memory_pass.py`, `ciao/web/chat_service.py`, learning/proposal helpers, and memory-pass tests.

Exit evidence: one completion queues at most one pass for the same evidence revision, updates are attributed, and repeated procedures strengthen one learning rather than duplicate it.

### C4. Project closure

- Add completion kind and note to the project-complete contract.
- Persist closure in the canonical project document.
- Resolve active/completed canonical paths at memory-pass execution.
- Queue the project closure after project chat passes.
- Replace the confirmation dialog with an accessible completion sheet.

Likely areas: `ciao/web/project_chats.py`, `ciao/web/routes_api.py`, `ciao/web/memory_pass.py`, `web/src/stores/projects.ts`, `web/src/components/ProjectView.vue`, and project/memory tests.

Exit evidence: completing and restoring a project preserves its closure record, chat memories run, and the final project pass targets the moved document.

### C5. Documentation and verification

- Update `PWA_API.md`, `docs/ARCHITECTURE.md`, `DESIGN.md`, and the capability catalog.
- Run focused backend and frontend tests.
- Run `mypy ciao`, full backend tests, full frontend tests, and the frontend build.
- Visually inspect desktop, narrow touch layout, keyboard focus, browser zoom, light theme, and conflict recovery.

Exit evidence: all blocking gates pass and the lifecycle is verified in the running PWA.

## Verification and rollout

Ship as two reviewable changes:

1. Task updates, unified Activity, completion prompt, and task-memory evidence.
2. Project closure note and project-level final memory pass.

No data migration is required for existing tasks. A task without updates simply has no update section; existing delegation and completion histories continue to parse as their current typed records. New writes use only the new canonical format—no second write path or compatibility fallback is introduced.
