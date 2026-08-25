---
name: visual-plan
description: Turn important work into a local, reviewable Markdown plan with an optional self-contained HTML companion. Use when the user asks for a plan, design direction, architecture review, UI flow, implementation proposal, migration plan, or approval artifact and the work spans more than one file or subsystem, has meaningful product or UX ambiguity, benefits from seeing a UI state or workflow, involves a data/API/security/ownership decision that is expensive to undo, or must be reviewed by another person or model before implementation. Skip it for a typo, a one-line fix, a single obvious function, or a task explainable in one sentence. Also skip it for a routine working doc (notes, an analysis, a draft with no approval gate and no cross-session handoff) — that is the stock workspace-authoring skill's territory.
---

# Visual plan

Produce a disciplined, local, reviewable plan before implementation begins. Markdown is the canonical plan because it supports comments, editing, history, backlinks, and durable storage. A self-contained HTML companion is optional and answers a specific review question, including a diagram drawn as inline SVG. The plan is the approval gate: research and write it, then stop before source edits until the user approves.

This skill is provider-neutral. It works the same on Claude and OpenCode. It does not depend on any hosted plan application, MDX, or a second review service.

## When to use this skill

Use it when the user asks for a plan, design direction, architecture review, UI flow, implementation proposal, migration plan, or approval artifact **and** the work has at least one of these properties:

- more than one file or subsystem;
- meaningful product or UX ambiguity;
- a UI state or workflow that benefits from seeing it;
- a data, API, security, or ownership decision that is expensive to undo;
- a plan that another person or model must review before implementation.

Skip it for a typo, a one-line fix, a single obvious function, or a task whose complete change can be explained in one sentence.

Also skip it for a routine working doc: notes, an analysis, a draft with no approval gate and no cross-session handoff. That is the stock `workspace-authoring` skill's territory. The dividing line is whether the document exists to get a decision approved and survive a provider switch. If not, it is a working doc.

## Required workflow

1. **Proceed to write the plan file.** Execution mode is fixed at auto: file writes run silently, and a destructive or system-level step asks for approval. Do not gate the plan on a mode that no longer exists — just create the file.
2. Read the project canonical document, relevant files, current implementation patterns, and recent changes.
3. State the outcome, scope, non-goals, and the files or areas that may change.
4. Record hard-to-reverse decisions and their recommended defaults.
5. Choose one output mode:
   - document only: Markdown;
   - document plus diagram: Markdown with a diagram as inline SVG inside a companion HTML file;
   - document plus interactive review: Markdown and HTML;
   - document plus both when the task needs architecture and UI review.
6. Write the Markdown plan with the resume block, checkpoint ledger, decisions, open questions, feedback log, implementation tasks, and verification gates. Copy `plan-template.md` into the user's project folder or `<vault>/Workspace/`, then tailor it to the task.
7. Create only the visual companions that help answer the review question. Keep them grounded in real product labels, current app chrome, actual file paths, and stated assumptions. Follow `visual-output.md` for the decision rules, and delegate all HTML authoring (interactive surfaces and inline-SVG diagrams alike) to the stock `html-artifact` skill.
8. Surface the Markdown plan with `file_surface`. Surface a companion separately when the user needs to inspect it.
9. Ask for approval in the same handoff. Name the files and areas that implementation will touch.
10. Stop before source edits until the user approves the plan.
11. On resume, read the plan again, reconcile its status with the live repository, and continue from the first incomplete checkpoint.

Before any file write, tool batch, or end-of-turn handoff, update the Resume block with the current checkpoint, the next action, and any blocker. If the user edited the plan between turns, read the live file and preserve those edits before writing. Never replace a plan from an old in-memory copy.

## Plan shape

Every generated plan should contain these sections, in this order:

1. Resume block
2. Outcome and user value
3. Scope and non-goals
4. Current-state evidence
5. Recommended direction
6. Alternatives and rejected options
7. Visual review, if applicable
8. Decisions and hard-to-reverse bets
9. Open questions with recommended defaults
10. Not yet specified (fog of war)
11. Feedback and decision log
12. Implementation checkpoints
13. Verification and rollout

The plan should be standalone. It must not refer to "the previous plan", "what we discussed above", or information that exists only in the chat.

Mark every claim in the plan as observed or assumed, and say what was read to observe it. A belief written as a fact becomes a false premise for the next agent, who treats the document as a contract and will not re-check it.

Use a compact shape for ordinary work. A minimal plan may combine the evidence, direction, and decision sections into a short document. Use the full template for ambiguous, UI-heavy, risky, or multi-file work. Add the skeptical review only when the risk justifies its cost. Do not turn every two-file change into a long ceremony.

The "Not yet specified" section is the fog of war. It holds decisions the model can tell are coming but cannot yet phrase sharply. The test for fog versus an open question is whether the question can be stated precisely now, not whether it can be answered. Resolving a question clears the fog ahead of it and graduates whatever is now specifiable into fresh open questions. Fog only ever gathers toward the destination, so out-of-scope work is closed and never graduates.

## Handoff rules

Two rules keep the resume block honest:

- **Reference by path, never copy.** The Resume block links specs, plans, ADRs, commits, and diffs by path or URL rather than copying them in. That keeps the block small and stops two copies drifting apart.
- **Downgrade unverified claims.** Read what the resume block claims before handing off, and mark anything only assumed as an assumption, not a verified fact.

## Feedback protocol

Use stable IDs for decisions and questions, such as `D-01`, `Q-01`, and `F-01`. Each feedback entry records an ID, location, the user's actual request, the chosen response (or `open`), a status (`open`, `accepted`, `rejected`, `deferred`, `implemented`, or `superseded`), and evidence. Do not delete unresolved feedback because the plan was rewritten. If text disappears, mark the entry `superseded` and explain where the decision now lives. If a question changes the architecture, scope, UX, data shape, or rollout, stop and update the plan before implementation continues.

## Visual companions

Read `visual-output.md` for the decision rules on when to reach for HTML, how to keep it aligned with the Markdown, and when to skip it. Author interactive HTML companions and inline-SVG diagrams by loading and following the stock `html-artifact` skill; do not re-inline its sandbox rules.

Only one file can be the active pinned surface at a time. Surface the canonical Markdown plan first, surface a companion when needed, then re-surface Markdown before asking for approval. The output cards and links remain the durable way to move between them.

## More

- `plan-template.md` is the reference template to copy and tailor.
- `visual-output.md` defines the visual companion decision rules.
