---
version: alpha
name: Ciao Console
description: A calm, terminal-inspired control surface for a personal AI assistant, served as an installable PWA.
colors:
  primary: "#ff4d6d"
  primary-strong: "#ff2e54"
  on-primary: "#1a1a2e"
  secondary: "#6a47b8"
  background: "#1a1a2e"
  surface: "#1f2240"
  surface-interactive: "#2a2e54"
  surface-elevated: "#23264a"
  text: "#e8e8f0"
  text-muted: "#b4b4c4"
  text-subtle: "#8f90a8"
  border: "#2e3258"
  border-strong: "#3a3f70"
  success: "#4caf50"
  warning: "#ff9800"
  error: "#f44336"
  light-primary: "#d81b60"
  light-primary-strong: "#b00d46"
  light-on-primary: "#ffffff"
  light-secondary: "#512da8"
  light-background: "#f4f4fa"
  light-surface: "#ffffff"
  light-surface-interactive: "#e6e8f4"
  light-text: "#1a1a2e"
  light-text-muted: "#5f607d"
  light-text-subtle: "#66687f"
  light-border: "#d2d4e3"
typography:
  fontFamilySans: "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'SF Pro Display', 'Segoe UI', Roboto, sans-serif"
  fontFamilyMono: "SF Mono, Fira Code, Cascadia Code, monospace"
  title:
    fontFamily: "{typography.fontFamilySans}"
    fontSize: 16px
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: -0.02em
  body:
    fontFamily: "{typography.fontFamilySans}"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.6
  body-mobile:
    fontFamily: "{typography.fontFamilySans}"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "{typography.fontFamilyMono}"
    fontSize: 11px
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: 0.5px
rounded:
  sm: 6px
  md: 10px
  lg: 14px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
  touch: 44px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 10px
    height: 44px
  button-secondary:
    backgroundColor: "{colors.surface-interactive}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: 8px
    height: 44px
  button-icon:
    backgroundColor: transparent
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    size: 44px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: 16px
  input:
    backgroundColor: "{colors.background}"
    textColor: "{colors.text}"
    typography: "{typography.body-mobile}"
    rounded: "{rounded.md}"
    padding: 10px
    height: 44px
  modal:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    width: 520px
---

# Ciaobot Design System

## Overview

Ciaobot is a focused control surface for a personal AI assistant. Its visual identity combines the precision of a developer console with the warmth needed for daily conversation. It should feel capable, private, calm, and direct—not corporate, ornamental, or like a generic chatbot.

The PWA is information-dense but not cramped. A system sans carries prose and controls, monospace is reserved for technical metadata and terminal cues, restrained animation and a deep indigo foundation establish the console character, and a warm pink accent supplies personality and orientation. The interface must remain understandable without color, animation, hover, or prior knowledge of its icons.

The home screen (**Home**) is the primary entry point and uses the Workbench composition. It has no headline, eyebrow or lede: the command surface is the page's subject. The surface holds the prompt, then one bar with the project chip (opens the shared project picker and only remembers the pick), a model chip (the workspace default unless the user picks a Claude or opencode model, which rides the chat-create call and resets on a workspace switch), and one icon send control whose accessible name stays **New** (it starts the chat in the chip's project without asking again, opening the picker only when the chip has no project to name; ⌘/Ctrl+Enter sends, bare Enter is a newline, as in chat). Beside it on wide panes, *What changed* lists only the queues that need a look (tasks the agent reported done and waiting for review — one opens its card, several open the board — then memory proposals, notes to revisit, active automations) as plain hairline rows with no icons or action words, says *Nothing to review.* when all are clear, and ends with *Open Memory*. Below the composer, *Continue where you left off* lists the selected workspace's chats as flat rows — title, a project · status sub-line read from the row's tier, time on the right. Rows are grouped in this explicit order: **Needs you**, **Working**, **Unread**, and **Earlier**. Below those tiers a separate **memory insights** section carries one entry per archived conversation whose memory work is in flight, unfinished, or asking for an answer. The entry is named for the *conversation*, never for the internal memory pass behind it, and its status is a state in words — *archiving…*, *queued for memory*, *updating memory…*, *needs you* (with the pending question on a second line), *needs attention*, *memory updated*. Clicking it opens the memory pass, which is a real chat: the live turn, the question and the reply all live there, and the archived transcript is the fallback only while no pass exists. A memory pass is never a chat row in the tiers and never appears in the sidebar. The workspace name appears once, in the sidebar's workspace scope: the Home header says only *Home*, and the selected lane has no visible header (its status sentence remains for screen readers). Rescue lanes for stale or unknown workspaces keep a visible name and New action, because the name is what explains them. Switching workspaces swaps the home content instead of adding a column. Arrow-key navigation follows the visual order, using up/down between stacked lanes and left/right within a lane. Signals are shape-and-text-first (tier labels, weight, dots) so no tier relies on color alone. Counters are sparing: the sidebar's workspace scope shows only the name (counts and key hints live in its menu), New chat carries its shortcut in its tooltip, Home shows a subtle count when chats need you, Tasks shows a quiet count of the tasks waiting for review, and Memory a quiet count of its queues; every link's accessible name keeps the number. When the pane (not the window) is narrower than 940px the rail drops below the request column. The expanded sidebar stacks the wordmark row (ending in the back / forward pair, a hairline, then the collapse control; the pair drops below the toggle on the collapsed 40px rail, and on phones, where the sidebar is a drawer, the pane header shows a Back chevron after the menu button whenever there is in-app history; ⌘[ / ⌘] in the browser), the workspace scope, New chat, the destinations as a labelled list (38px rows, 44px on coarse pointers; the current one on an accent-tinted fill), then the `Projects` tree at 36px row density. In a plain browser tab on a secure origin, Home shows a short *Set up this device* section (install the app, turn on notifications, send a test). It is plain rows with one primary action, dismissible per browser, and it disappears once the app is installed and notifications are on.

Core product work follows one visible model: **request → run → output → durable knowledge**. The transcript remains the reasoning surface. A conditional Work details drawer adds Context, Activity, and Output without forcing a permanent multi-column IDE layout. The chat header holds only close, the title, and **Archive** as a filled primary button with its label (it is the header's one action). Work details is not toggled from the header: an info (ⓘ) button at the right end of the rail's heading hides the rail, and while it is hidden (or the pane is too narrow for a rail) the same ⓘ sits as a small bordered tab at the chat body's top right and brings it back, opening the drawer on narrow panes. Project is the context envelope; Memory's To decide section is the durable-knowledge inbox; its Map (Graph/List) is the deeper exploration mode. **Tasks** is a workspace destination of its own: a four-column board — To do, In progress, In review, Done — built from the existing card, badge and chip tokens with no column drag, a card's status being a native select, and the four columns reading as one status-filtered list on a narrow pane. Memory lists its sections in the sidebar exactly as Settings lists its tabs — *To decide* first on its own row, then *Explore* (Map, Categories) and *Records* (Retired, History), each a route with a quiet count at the row's end (accent on To decide) — so the page header only names the section ("Memory · To decide") and carries no mode switch or tab row. To decide holds both queues on one page: a row of filter chips (*All*, *Suggested*, *To revisit*, with counts and no zero chips) narrows it to one queue, and the filter rides in the address (`/memory/review?show=revisit`) so Home and the map link straight to the queue they name. Under *All* an empty queue steps aside for one that is not. The map's search and category filters sit under that list only while Map is showing. The two queues share one row shape: a 16px heading and one muted sentence, violet-tinted filter chips (by change type; by reason) with counts and no zero chips, then hairline rows with a fixed-width action column of neutral bordered buttons and a text link — no pink primary on a row, since every row is the same routine choice. A suggestion names its change before it is opened: a bordered tag with a drawn icon (*New note*, *Add to a note*, *Merge into a note*, *Update a line*, *Move a note*, *Already saved*), the destination in mono, and a compact numbered diff; its button is the verb (*Create note*, *Add line*, *Merge*) and accepts against the previewed revision. A note to revisit shows *type · reason · backlinks*, and each reason is a disclosure for its evidence (the quoted line with the match marked, or the check date and where it came from).

There is no native shell: the engine serves the PWA, and every one of these
states is a page in it. Before the engine reports startup phases, the connecting
screen states that it is checking the connection, with indeterminate motion rather
than an invented 0% boot progress. When the engine reports phases, it shows their
real status in plain-language rows. This screen shares the calm indigo canvas,
type hierarchy and restrained accent of the engine recovery curtain; neither
pretends the browser is booting the host. The update overlay remains the only
place a package update is reported, and it says a restart is pending until the
new engine answers. In the PWA, an
engine that stops answering shows a full-screen recovery curtain over the kept
route. It uses the boot-screen language: a plain title, `ciao service
start|status` on this computer, Retry, and automatic reconnect. It is modal
(focus trapped, shortcuts suppressed), and it lifts without a reload when the
engine returns.

On Home, the review rail is titled **At a glance**. Actionable operator notices, update tasks, and device setup each sit in a separate, in-flow window with the pinned file tile's tonal surface, bordered header and close control. For operator actions and update tasks, X hides the window until reload (or until **Show closed notices**) and persistent Hide remains a separate, explicit action. The device-setup reminder is different: its X is the only dismissal control and persists for this browser, so it does not nag again after a reload. Chat-row signals sit right-aligned above the relative time in both wide and narrow panes. In-flight memory insight rows use one quiet **extracting…** label; blocked and completed phases retain distinct wording. A normal browser tab cannot know whether the app is separately installed; installed PWA windows use display-mode detection, and the retired Ciaobot.app desktop wrapper does not report itself as a PWA.

## Colors

Dark mode is the primary visual expression. It uses layered indigo surfaces instead of neutral black, keeping long sessions comfortable while preserving clear hierarchy.

- **Primary pink (`#ff4d6d`):** The brand accent and primary-action color. Use it for the current location, focus, progress, and the single most important action in a region. Workspaces may override this with a saved accent preset (`pink`, `cyan`, `amber`, `emerald`, `violet`); canvas and surface tokens stay fixed.
- **On-primary (`--on-accent`, `#1a1a2e` dark / `#ffffff` light):** The label colour on any filled accent surface: primary buttons, active pills and toggles, accent badges. Never hard-code white on the accent. The dark accents are bright, so white on pink measures 3.21:1 (3.64:1 on hover) and on the cyan/amber/emerald presets about 2:1; the indigo canvas colour clears 4.5:1 on every dark accent and its hover shade (pink 5.31 / 4.69). Light accents are deep enough for white (pink 4.95 / 7.02).
- **Accent presets:** Each preset pairs `--accent` with a hover shade `--accent-strong`, and both must keep `--on-accent` at WCAG AA. Dark: pink `#ff4d6d`/`#ff2e54`, cyan `#38bdf8`/`#0ea5e9`, amber `#fb923c`/`#ea580c`, emerald `#34d399`/`#059669`, violet `#a78bfa`/`#9670f7`. Light: pink `#d81b60`/`#b00d46`, cyan `#0369a1`/`#075985`, amber `#c2410c`/`#9a3412`, emerald `#047857`/`#065f46`, violet `#7c3aed`/`#6d28d9`. The light shades also keep accent-coloured text at AA on white.
- **Violet (`#6a47b8`):** A secondary accent for selected filters, contextual information, and supporting distinctions. It must not compete with the primary action.
- **Background (`#1a1a2e`):** The deepest application canvas.
- **Surfaces (`#1f2240`, `#23264a`, `#2a2e54`):** Cards, elevated controls, hover, and pressed states. Prefer tonal separation and borders over large shadows.
- **Text (`#e8e8f0`):** Primary content. Muted dark text uses `#b4b4c4`; `#8f90a8` is the AA-compliant tertiary token for metadata and secondary controls. In light mode the tertiary token is `#66687f`; never reuse the old washed-out `#8e90a8` value for meaningful text.
- **Semantic colors:** Green communicates success, orange caution or recoverable risk, and red destructive actions or errors. Never use semantic colors decoratively.

Light mode keeps the same hierarchy with a soft lavender canvas, white surfaces, crisp crimson accent, and slate text. It is an adaptation of the same system, not a separate aesthetic.

Color is never the only state signal. Pair status colors with text, an icon, a shape, or an accessible label. Verify WCAG AA contrast in both themes whenever a token or component changes.

## Typography

The product uses a **hybrid typography system**: a clean system proportional sans-serif stack (`-apple-system`, `SF Pro`, `Segoe UI`, `Roboto`) for conversational chat prose and general UI, paired with a monospaced stack (**SF Mono**, **Fira Code**, **Cascadia Code**, `monospace`) for code blocks, badges, commands, schedules, timestamps, and terminal identifiers.

- **Titles:** 15–16px, bold, with slightly tight tracking. Titles should remain visible when actions compete for space.
- **Body:** 14px on desktop with comfortable 1.6 line height for natural reading.
- **Mobile form text:** At least 16px to prevent browser auto-zoom while keeping user zoom available.
- **Labels & Badges:** Monospaced 11px, semibold, often uppercase with 0.5px tracking for technical precision.
- **Wordmark:** Bold monospace `ciaobot`, led in the sidebar by the Ciaobot face as a one-colour pixel mark (`CiaoMark.vue`, traced from `face.png` in three tones of `currentColor`) drawn in the workspace accent. A blinking caret may appear only in startup or explicitly terminal-like moments.

Respect the user-controlled font scale. Truncate compact navigation labels only when the full value remains available through context, title text, or an expanded view.

## Layout

The PWA is mobile-first and safe-area aware. Desktop uses a persistent project sidebar beside the active workspace. Narrow screens use an overlay sidebar and full-width panels, with secondary actions moving into menus or sheets before titles are sacrificed.

Use the 4px-based spacing scale deliberately: 4px for internal micro-spacing, 8px between closely related controls, 12–16px for component rhythm, and 24–32px between major groups. Long-form surfaces stay readable on wide screens: settings cards are capped at `min(100%, 1040px)`; the home intake/review column is capped near 920px; project, Memory, and Automations use the available pane width but keep conversational rows within a readable measure.

Every pane uses one page grid (App.vue `--page-max` 1180px, `--page-gutter` 32px / 16px on phones, `--page-rail` 280px): a main column plus an optional right rail, centred, collapsing to one column when the pane (not the window) is under 940px. The pane header's padding follows the same grid, so the title starts where the content starts and the actions end where the rail ends; a view without its own title shows its page tag as that left title (no centred pill). Rails carry real, glanceable context for the page — Home: what changed; Chat: work details (a one-line note naming the automation the chat comes from, linked, above everything else — or, for a delegated chat, the task it works on, linked to its card, with the agent's report in words and a neutral *Approve Done* while the result waits for review; and, for the one app-owned chat, the conversation its memory pass is distilling; the same sentence sits above the transcript when the rail is hidden); then *Agent context* — context-window use as a thin meter from the last reply, the workspace guide and the project brief as hairline rows with token sizes and when each is sent, the project name linking to its page — then *Subagents running* when any are, skills and MCP used, files produced); Automations: status and recent or missed runs; Settings: on-this-page links; Memory: the always-loaded budget on its review sections (the Map section has no rail — the drawing takes the pane, with the vault's numbers on one toolbar line and a selected note in the same docked tile a pinned file uses); Project: current counts and where it lives — using the shared rail vocabulary (heading, hairline key/value rows, hairline link rows, muted note). Page bodies use plain sections (sentence-case 16px heading, optional muted line, hairline rows, text-link actions) instead of cards, with one primary action per region and destructive actions behind a menu. In chat, the model picker is the first chip in the composer bar, pending comments ride inside the composer, each agent turn collapses to one "Worked for …" line that opens into a step timeline, and a message's actions (Copy, Fork from here, turn details) appear only when it is selected by click or Enter, marked by a thin outline in the workspace accent and nothing else — no veil, no blur, no dimming, so the transcript around the selection stays readable; clicking the message again or pressing Esc puts it back. A composer that floats outside the themed root (teleported to `<body>`) carries the active workspace's accent with it, so it is tinted like the pane it was opened from. The actions are that message's own footer, inside its card: one hairline below the prose, bled to the card's edges and re-inset to the text column, so the message and what you can do to it read as one object and share one left edge. The turn details close the footer on the right as two units — when it finished, how long, and what ran it; then what it cost, as tokens in and out — dropping to their own line when the footer runs out of room, and never carrying a fifth separator. Context-window occupancy is not repeated there: the rail owns it, as a meter against the window's size, because a bare percentage means nothing without it. A pinned file opens as a docked tile, not a second flat column: an inset window 8px from the pane edges in the sidebar's surface (`--bg2`), 14px radius, a 1px border and a soft shadow, so the sidebar and the file read as one layer with the chat as the canvas between them. Its 52px title bar (type badge, filename, muted folder, actions, then Unpin last) lines its bottom rule up with the chat header's, the folder truncates before the filename, the document fills the tile's width with equal 32px margins left and right (no fixed text measure: the tile's width, which the user drags, is the measure), and the gap between chat and tile is the resize handle, showing a small grip on hover.

All interactive targets are at least 44×44px on touch layouts. Compact visual glyphs may sit inside a larger hit area. Honor device safe areas, virtual keyboards, standalone PWA chrome, and browser zoom. Do not disable pinch zoom or text scaling.

Long or implementation-oriented content must not dominate a mobile page. Collapse long prompts and diagnostics behind Preview/Expand/Copy controls, preserve the page title, and move secondary or destructive actions into an overflow menu or bottom sheet.

## Elevation & Depth

Hierarchy comes primarily from tonal layers, one-pixel borders, and spacing. Cards sit on the page background; inputs may use the deeper canvas; modals and popovers use the elevated surface. Use stronger borders for focus and separation before adding shadows.

Shadows are reserved for content that genuinely floats above another interaction layer: mobile drawers, menus, modals, and toasts. Keep them soft and dark. The normal application surface stays clean; CRT grain is reserved for startup, update, and native terminal surfaces where it carries product meaning. Visible thin scrollbars remain available in bounded data regions so scrollability never becomes an invisible assumption.

## Shapes

The shape language is compact and gently rounded. Standard controls and cards use a 10px radius, small nested elements use 6px, and modals use 14px. A tighter 4px radius (`--radius-xs`) is reserved for small squared tags that must not be mistaken for count badges: the needs-you state chip and the workspace key badges. Fully rounded shapes are limited to badges, status dots, avatars, and true pill selectors.

Borders are structural, not decorative. Active navigation is marked by a slim pink edge plus a tonal background. Do not mix exaggerated rounding, glass effects, or unrelated shape styles into the same view.

## Components

- **Primary actions:** Pink filled buttons are scarce. Use one for the most important forward action in a panel. Routine, reversible, or secondary actions use neutral bordered controls.
- **Caution and danger:** Restart and similar recoverable operations use orange caution styling. Delete and irreversible actions use red and require clear wording or confirmation.
- **Navigation:** Use native buttons or links where possible. Every navigation row must support keyboard focus and Enter/Space activation, expose its selected/expanded state, and retain a visible text label.
- **Cards and panels:** Group related information with a tonal surface, border, 10px radius, and 16px padding. Avoid nesting multiple bordered cards without a clear hierarchy.
- **Data tables:** Compact tables should fit their content instead of stretching across a message. At narrow widths, preserve readable row labels and contain horizontal overflow in a visibly focused, keyboard-scrollable region.
- **Canvas surfaces:** A canvas may own direct-manipulation gestures (drag-to-pan, drag-to-move) only where it captures the pointer and scopes `touch-action` to itself; page scroll, browser zoom, and text selection stay available everywhere else. Every canvas action needs an equivalent native control — a named button or list row — because a canvas is not keyboard accessible. Never rely on a modifier click or hover alone to reach an action.
- **Task board:** A card carries no control for its own status: its column says where it is. On the four columns a card drags to another lane, or moves one lane with Shift+←/→ while its title has focus (Option+Arrow stays the section switch). The editor's status radios are the path for a screen reader or a phone, where the board is one list and each card names its status in a badge. Done is a round checkbox beside the title. Only a delegated card has a foot, and it holds the gestures for that attempt. Its badge uses the agent's own report (*Agent says done*, *Blocked*, *Needs input*, *Unfinished*) with the agent's summary clamped to three lines under it. A URL in a title is a real link that opens in a new tab without opening the card; the card's keyboard control stays a visually hidden button whose focus is drawn on the card. The editor has no Assignee field: its *Agent* section is where delegation, its state and every attempt's chat live. Handing a task over starts in the editor's *Agent* block, which says in one line what delegating does. A task's description opens rendered, with Edit to show the source, never a raw textarea with a collapsed second copy under it. The columns are *To do*, *In progress*, *In review*, *Done*; a delegated card the agent reported done sits in *In review* until approved. A card shows project, assignee and due date only when they differ from the defaults. A task with no project and a task in the workspace's auto-managed General project are one place, always named General — never "No project". A delegated chat says so in text, never colour: a small mono `task` tag (`--radius-xs`) after its sidebar title, a Home sub-line of *project · task · status* (*needs input*, *blocked*, *waiting on you*, *ready for review*, *working*) in its usual tier — a chat whose agent waits on the user files under Needs you — and a first message drawn as a *Task handed over* card (title and rendered description, the raw prompt behind *Show what the agent received*). `/tasks?task=<id>` opens that task's card. The task editor has no Save button: selects and the date write when they change, the title and description write when typing pauses and on blur or close, and a line beside Delete says whether that has happened.
- **Inputs and composer:** Inputs use the deep background, visible border, pink focus ring, and plain-language labels. The chat composer remains the strongest persistent interaction affordance.
- **OS sharing:** Content shared into the installed PWA waits on Home in a neutral review strip until explicitly added to a draft or discarded; no share starts a chat or sends an agent turn. Native file sharing is an optional secondary action beside Download, never a replacement for it.
- **Badges and status:** Badges are compact supporting signals, never the sole explanation. The sidebar puts subtle numeric counts on the destinations, scoped to the selected workspace: chats needing you on Home (including a delegated chat whose agent asked for input or reported blocked), missed runs on Automations, tasks the agent reported done and waiting for review on Tasks, suggestions and notes to revisit on Memory; Settings shows a short word (update, check). The workspace scope carries no counts, and a project row summarises its chats with one static dot only while collapsed. Running, unread, failed, and disabled states need accessible text equivalents.
- **Menus and sheets:** Overflow menus contain secondary and destructive actions when horizontal space is constrained. Mobile modals become edge-to-edge sheets and honor safe areas.
- **Onboarding:** Spotlight backdrops suppress competing content. Skip is visibly actionable but secondary; Back and Next meet the same touch-target requirements as the rest of the app. Onboarding orients; it never becomes a gate or a wizard the user must finish. The seeded interview asks, in short turns the user can decline, what the workspace is for and what should stay out of it, how they want to be worked with, and the operating context around them; the scope answer goes to the workspace/project context rather than a bounded region, so the profile and memory regions keep holding personal facts. Before it asks, it reports what the workspace already holds from Ciaobot's own state, and afterwards it seeds starting knowledge from confirmed facts only, leaving anything uncertain in `Workspace/Memory-Proposals.md` and creating an entity folder only when a confirmed fact needs it. When it explains memory, it names the two layers — the bounded profile and preferences loaded into every conversation, and durable notes filed by category — and names the workspace's **effective** categories (enabled, not hidden, in registry order) from the registry that backs the Categories page, never a copy of the shipped list. That page stays the single editor, so the explanation links to it instead of adding a second way to change categories. A seeded welcome is a snapshot and makes no promise beyond what the feature does: it scans no history, calls no model, writes no category, and cannot claim that past conversations are imported. It may *name* the place where a non-automatic capability lives — past conversations are not imported on their own, and Memory → Import is where you choose which ones Ciaobot reads and see what would be sent before anything runs — and it stops there, because naming a page is not running it. It is skippable, ignorable, and readable by someone who never opens it — ordinary chatting and setup must stay usable when it is skipped.
- **Motion:** Use short 120ms interaction transitions and the shared easing curve. Longer fades are acceptable for startup and major overlays. Respect `prefers-reduced-motion`; never make meaning depend on animation.
- **Focus:** Interactive elements use a visible 2px pink focus outline with separation from the component edge. Do not remove focus styling unless an equally visible replacement exists.

## Do's and Don'ts

- Do reserve pink emphasis for the current location, focus, progress, and primary action.
- Do keep titles and textual state visible when layouts become narrow.
- Do use plain-language recovery guidance instead of exposing raw IDs or implementation details.
- Do make every touch target at least 44×44px and every core workflow keyboard accessible.
- Do preserve browser zoom, font scaling, safe-area behavior, and reduced-motion preferences.
- Do test dark and light themes at desktop and mobile widths.
- Don't turn a group of routine actions into competing pink bars.
- Don't rely on unexplained icons, color alone, hover, or animation to communicate state.
- Don't expose long prompts or diagnostics in full by default on mobile.
- Don't copy custom styling into a host-native surface when platform conventions are clearer.
- Don't introduce new colors, radii, shadows, or typefaces when an existing token serves the purpose.
