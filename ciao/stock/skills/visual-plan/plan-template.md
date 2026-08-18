# <Plan title>

> Copy this template into the user's project folder or `<vault>/Workspace/`, then tailor it to the task. Use the full template for ambiguous, UI-heavy, risky, or multi-file work. For ordinary work, combine the evidence, direction, and decision sections into a short document. Delete sections that do not apply.

## Resume block

- Status: proposed
- Current checkpoint: C0
- Next action: <the next concrete action>
- Blocker: <none, or the blocker>
- Implementation repository: <path>
- Generated plan output: <path to this file>
- Visual companions: <optional `.html` and `.excalidraw` paths beside this file>
- Verified on: <date> against <files read>

This block is the handoff contract. Any model that resumes the work should read it first, then read the current checkpoint and open questions before doing anything else.

## Outcome and user value

<What the plan delivers and why it matters.>

## Scope and non-goals

In scope:

- <item>

Out of scope for this release:

- <item>

## Current-state evidence

<Name the actual files, symbols, routes, components, or data shapes involved. Separate observed facts from assumptions, and say what was read to observe each. Record what will be reused before describing what will be added.>

## Recommended direction

<Choose one recommended approach. Record the alternatives considered and why they were not selected. Identify wire formats, ownership boundaries, public interfaces, data shapes, or UX decisions that would be costly to change later.>

## Alternatives and rejected options

### <Option>

<Why it was rejected.>

## Visual review

<If applicable: which companion (HTML, Excalidraw, or both) answers the review question, and why. Keep it grounded in real product labels, current app chrome, actual file paths, and stated assumptions.>

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | <decision> | <rationale> | Proposed |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | <question> | <default> | Open |

## Not yet specified (fog of war)

<Decisions that are clearly coming but cannot be phrased sharply yet. The test for fog versus an open question is whether the question can be stated precisely now, not whether it can be answered. Resolving a question clears the fog ahead of it and graduates whatever is now specifiable into fresh open questions. Fog only ever gathers toward the destination, so out-of-scope work is closed and never graduates.>

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | <section/heading/file> | <the user's actual request or concern> | <chosen response, or `open`> | open | <file path, test, comment, or user approval> |

## Implementation checkpoints

Each checkpoint has an exit condition so another model can tell whether the work is actually ready to move on.

### C0. Start or resume

- Read the Resume block and current checkpoint.
- Read the project canonical document and the live files named by the plan.
- Confirm that the plan's status still matches the repository.

Exit evidence: the plan records the next concrete action and any blocker.

### C1. Ground the plan

- Name the actual files, symbols, routes, components, or data shapes involved.
- Separate observed facts from assumptions.
- Record what will be reused before describing what will be added.

Exit evidence: a reader can verify the current-state claims without relying on chat history.

### C2. Set direction

- Choose one recommended approach.
- Record the alternatives considered and why they were not selected.
- Identify wire formats, ownership boundaries, public interfaces, data shapes, or UX decisions that would be costly to change later.
- Mark anything genuinely unresolved as an open question with a recommended default.
- Capture decisions that are coming but not yet phraseable in the "Not yet specified" fog-of-war section, separate from open questions.

Exit evidence: the plan is executable in one direction. It is not a menu of undecided options.

### C3. Build the review artifact

- Write the Markdown plan.
- Create an Excalidraw companion only when relationships need a spatial view.
- Create an HTML companion only when the reviewer needs to inspect interaction, layout, or state changes, and author it by loading the stock `html-artifact` skill. If that skill is not installed, write the plan without the companion and say so; do not re-derive its sandbox rules from memory.
- Keep the Markdown plan and companions aligned on names, statuses, and state IDs.

Exit evidence: `file_surface` has been called for the Markdown plan, and each companion passes its format-specific checks.

### C4. Review the artifact

- Inspect the Markdown in the pinned panel.
- Inspect the HTML at narrow and wide widths when it exists.
- Inspect the Excalidraw diagram at its default zoom when it exists.
- Remove filler, duplicate explanations, invented file paths, and visuals that do not answer a review question.

Exit evidence: the first screen or first page makes the proposed outcome understandable without the chat transcript.

### C5. Get approval

- Surface the plan and name the files or areas that implementation will touch.
- Capture user comments in the feedback log.
- Resolve each blocking question or leave it explicitly open with an owner and default.
- Mark the plan `approved` only after the user accepts the direction.

Exit evidence: the plan itself states what is approved and what remains deferred.

### C6. Implement

- Re-read the approved plan before editing.
- Work through the implementation tasks in order, updating the checkpoint ledger.
- Update the plan when scope changes instead of silently changing course in chat.

Exit evidence: every planned source change has a corresponding implementation or an explicit deferral.

### C7. Verify

- Run focused tests first, then the relevant broader suite.
- Verify the user-visible behavior in the running PWA only through the normal deploy or reload workflow.
- Record failures, skipped checks, and live verification limits.

Exit evidence: the plan distinguishes verified behavior from work that is only merged or assumed to be deployed.

### C8. Close or hand off

- Set the final status to `complete`, `deferred`, or `blocked`.
- Record the last verified commit or deployment state.
- Leave a next action only when work remains.

Exit evidence: another model can tell whether it should continue, verify, or stop.

## Verification and rollout

<How the plan will be verified and rolled out. Distinguish verified behavior from work that is only merged or assumed to be deployed.>
