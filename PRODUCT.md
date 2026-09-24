# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Ciaobot's primary user is a solo operator or knowledge worker who already uses agent CLIs such as Claude Code and opencode for substantial personal, work, or client work. They use Ciaobot to delegate work through a coherent interface, preserve context across sessions, and grow a durable personal knowledge system without giving up ownership of their files or provider choices.

Host and client devices have equal standing in the intended workflow. A Mac or Linux host owns the engine, workspace, vault, and provider sessions; phones, tablets, and other computers connect through the PWA over a private network. Future work should preserve continuity across that boundary rather than treating remote access as a secondary mode.

## Product Purpose

Ciaobot is a second-brain-first, local-first control surface for working with personal AI agents. It brings chats, projects, files, schedules, automations, archives, and memory into one product while continuing to use the provider CLIs and credentials the user already owns.

The product succeeds when useful work leaves a legible, current, and retrievable trail: conversations can be resumed or branched, project context is available without repeated explanation, and durable knowledge remains useful in plain Markdown even when it is opened with another tool. Success is not tied to a particular model provider or to keeping the user inside Ciaobot.

## Positioning

Ciaobot combines a provider-neutral assistant interface with a file-owned memory system. It wraps existing agent CLIs rather than replacing them, and it turns archived work into scoped, reviewable Markdown knowledge instead of storing the user's long-term context only in an opaque application database.

The differentiating mechanism is the full continuity loop: agent work happens in persistent workspace and project context; selected conversations are archived; durable decisions, preferences, people, and learnings are extracted with source grounding; facts are routed to the correct workspace or project; uncertain changes become proposals; and the resulting notes remain portable, searchable, and editable outside Ciaobot.

## Operating Context

- The core hierarchy is **workspace → project → chat**. A workspace separates a life area such as personal, work, or a client; a project groups related work and supplies durable context; a chat is where delegated work happens.
- Users choose or create a workspace folder during setup. Ciaobot may adopt an existing notes folder, but it must preserve that folder's contents and keep the result usable from Obsidian, a text editor, Claude Code, opencode, or another file-based tool.
- One machine acts as the host and source of truth for the engine, workspace, vault, and provider sessions. Other devices are clients of that host and may use the same interface over a private network such as Tailscale.
- The supported desktop release path is Ciaobot.app on Apple Silicon macOS. The engine and PWA can also run on Linux, including Ubuntu 24.04, and be accessed from a browser or installed PWA.
- Provider setup and credentials remain owned by the provider CLIs. Ciaobot adds context, workflow, files, scheduling, archiving, and memory around those sessions rather than creating a second model account.
- The product is used in two directions: people ask agents to do work, and they review, edit, connect, and discuss the durable knowledge produced by that work.

## Capabilities and Constraints

Confirmed product capabilities include:

- persistent workspaces, projects, chats, conversation forks, model/provider selection, and visible background-agent or command activity;
- direct work with workspace and vault files, including previews, editing, pinned context, line and cell annotations, attachments, and document conversion;
- recurring, interval-based, and one-off automations that dispatch prompts into new or existing chats;
- archived-conversation insights, scoped durable-memory routing, proposal review, reversible managed changes, nightly curation, vault search, the Memory Map, and deterministic vault review;
- multi-model adversarial review through a configured critique panel;
- per-workspace extensions such as skills, subagents, commands, and permitted MCP servers;
- optional Google Workspace workflows through the `gws` CLI;
- a macOS menu-bar companion for status, notifications, navigation, and application utilities;
- host/client access from phones, tablets, and other computers without splitting the source of truth.

Durable constraints:

- Ciaobot is for a solo operator. Multi-user or team collaboration is not the confirmed product audience.
- Long-term user knowledge must remain ordinary, portable Markdown in the user's workspace, not a proprietary-only store.
- Provider credentials remain in the user's provider CLIs. Ciaobot must not require a parallel account or invent credentials it cannot own.
- The agent can act with the same filesystem and credential access as the host user; the UI must not imply stronger isolation than the runtime actually provides.
- Automatic memory work must be legible and reviewable. Uncertain new knowledge is proposed rather than silently promoted, and managed mutations must retain recovery or undo evidence.
- The product must not silently discard or rewrite an adopted notes folder during onboarding or workspace migration.
- Ciaobot supports Claude Code and opencode as the current runtime providers. Other model access is reached through opencode rather than by multiplying first-party integrations.
- Apple-only capabilities, including on-device voice and Foundation Models, must be presented as host-dependent enhancements rather than universal product requirements.

## Brand Commitments

- The user-facing product name is **Ciaobot**.
- The installed CLI is available as both `ciaobot` and the shorter compatibility name `ciao`; backend package and environment-variable compatibility names may continue to use `ciao`/`CIAO_*`.
- The project is open source under the Apache 2.0 License and is maintained by Raffaele Farinaro.
- Product communication should remain concrete, capable, and privacy-aware. It may describe verified functionality but must not imply that local-first storage means the model provider never receives data.
- Existing identity assets include the Ciaobot mascot and product imagery in `docs/hero.png` and the interface captures under `docs/screenshots/`.

## Evidence on Hand

- Product overview and supported workflows: `README.md`.
- Authoritative user-facing capability inventory: `ciao/stock/skills/ciao-capabilities/SKILL.md`.
- Product rationale and evaluation goals for memory: `docs/MEMORY_DESIGN.md`.
- System boundaries and operating constraints: `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/LINUX.md`, and `web/README.md`.
- Security posture: `SECURITY.md`.
- Product imagery and interface evidence: `docs/hero.png`, `docs/screenshots/pwa-overview-labeled.png`, `docs/screenshots/pwa-overview.png`, and `docs/screenshots/pwa-overview.svg`.
- No customer testimonials, quantified outcome claims, case studies, or press commitments were identified. Future work must not fabricate them.

## Product Principles

1. **Memory is the outcome.** Agent activity matters when it becomes useful knowledge and continuity, not merely when a response appears in a chat.
2. **The user owns the substrate.** Workspaces, notes, transcripts, and extension assets must remain inspectable, portable, and reusable outside the app.
3. **Provider freedom without fragmented identity.** Users may switch models and CLIs while keeping one coherent product context and their existing credentials.
4. **Trust must be inspectable.** Automatic behavior should explain itself, defer uncertain changes, preserve recovery paths, and never overstate isolation or certainty.
5. **One product across host and clients.** Remote devices are legitimate ways to do the same work; they should not become reduced or disconnected experiences.

## Accessibility & Inclusion

Ciaobot must preserve browser and platform accessibility rather than optimizing only for its default visual presentation. Product work must keep pinch zoom and browser zoom available, honor user font scaling, provide visible keyboard focus and complete keyboard operation for core workflows, maintain touch targets of at least 44×44 CSS pixels on touch layouts, respect reduced-motion preferences, and account for safe areas, virtual keyboards, standalone PWA chrome, and variable viewport sizes.
