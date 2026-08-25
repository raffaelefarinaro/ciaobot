# Visual output rules

Markdown is always the canonical approval artifact. HTML is an optional companion that answers a specific review question, including a diagram drawn as inline SVG. This file defines the *decision* rules for when to reach for it, how to keep it aligned with the Markdown, and when to skip it. It does not re-inline authoring rules.

## Markdown is canonical

- Always create the Markdown plan first. It is the source of truth.
- HTML is a companion, never the only copy of the plan.
- Re-surface Markdown last before approval because it is the commentable canonical surface.

## HTML

Use HTML when the reviewer needs to inspect interaction, layout, state changes, or a diagram that argues a relationship: an interactive UI flow, a side-by-side comparison, a timeline, a prototype, or an architecture/flow/sequence drawn as inline SVG. It is for when the visual *is* the answer.

- Author interactive HTML companions and inline-SVG diagrams by **loading and following the stock `html-artifact` skill**. Do not re-inline its sandbox rules; the stock skill already ships to every workspace and defines the CSP and panel constraints. If that skill is not installed, write the plan without the companion and say so.
- Keep the HTML self-contained: one file, everything inline, no network at runtime, no forms, no animation loops, under 2 MB.
- A narrow panel is a first-class review target. Design for roughly 420px wide first and let it grow.
- The HTML is never the commentable source. Keep the decisions in Markdown.

## When to skip visual output

- Do not create visual output for its own sake. If the plan is mostly words with one small illustration, write Markdown.
- If visual generation is unavailable, finish with a complete Markdown plan and mark the visual companion as skipped, not as a blocker. Markdown is always sufficient.

## Surfacing

- Call `file_surface` explicitly for every artifact worth reviewing.
- Only one file can be the active pinned surface at a time. Surface the canonical Markdown plan first, surface a companion when needed, then re-surface Markdown before asking for approval.
- The output cards and links remain the durable way to move between files.
