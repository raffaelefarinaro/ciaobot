# Visual output rules

Markdown is always the canonical approval artifact. HTML and Excalidraw are optional companions that answer a specific review question. This file defines the *decision* rules for when to reach for each, how to keep them aligned with the Markdown, and when to skip them. It does not re-inline authoring rules.

## Markdown is canonical

- Always create the Markdown plan first. It is the source of truth.
- HTML and Excalidraw are companions, never the only copy of the plan.
- Re-surface Markdown last before approval because it is the commentable canonical surface.

## HTML

Use HTML only when the reviewer needs to inspect interaction, layout, or state changes: an interactive UI flow, a side-by-side comparison, a timeline, or a prototype. It is for when the visual *is* the answer.

- Author interactive HTML companions by **loading and following the stock `html-artifact` skill**. Do not re-inline its sandbox rules; the stock skill already ships to every workspace and defines the CSP and panel constraints. If that skill is not installed, write the plan without the companion and say so.
- Keep the HTML self-contained: one file, everything inline, no network at runtime, no forms, no animation loops, under 2 MB.
- A narrow panel is a first-class review target. Design for roughly 420px wide first and let it grow.
- The HTML is never the commentable source. Keep the decisions in Markdown.

## Excalidraw

Use Excalidraw only when spatial relationships are easier to inspect visually: an architecture, a process flow, a data model, or a diagram that argues a relationship words alone cannot express.

- Excalidraw has no stock skill, so this file carries the minimum contract. Do not depend on any workspace-only `excalidraw-diagram` skill.
- The diagram is a companion, not a replacement for the plan's decisions.
- Keep it grounded in real product labels, current app chrome, actual file paths, and stated assumptions.
- Keep the Markdown plan and the diagram aligned on names, statuses, and state IDs.

## When to skip visual output

- Do not create visual output for its own sake. If the plan is mostly words with one small illustration, write Markdown.
- If visual generation is unavailable, finish with a complete Markdown plan and mark the visual companion as skipped, not as a blocker. Markdown is always sufficient.

## Surfacing

- Call `file_surface` explicitly for every artifact worth reviewing.
- Only one file can be the active pinned surface at a time. Surface the canonical Markdown plan first, surface a companion when needed, then re-surface Markdown before asking for approval.
- The output cards and links remain the durable way to move between files.
