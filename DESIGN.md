---
version: alpha
name: Ciao Console
description: A calm, terminal-inspired control surface for a personal AI assistant across the PWA and native tray.
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

The home screen (**Home**) is the primary entry point and uses the Workbench composition. It has no headline, eyebrow or lede: the command surface is the page's subject. The surface holds the prompt, then one bar with the project chip (opens the shared project picker and only remembers the pick), a model chip (the workspace default unless the user picks a Claude or opencode model, which rides the chat-create call and resets on a workspace switch), and one icon send control whose accessible name stays **New** (it opens the picker first; ⌘/Ctrl+Enter sends, bare Enter is a newline, as in chat). Beside it on wide panes, *What changed* lists only the queues that need a look (memory proposals, notes to revisit, active automations) as plain hairline rows with no icons or action words, says *Nothing to review.* when all are clear, and ends with *Open Memory*. Below the composer, *Continue where you left off* lists the selected workspace's chats as flat rows — title, a project · status sub-line read from the row's tier, time on the right. Rows are grouped in this explicit order: **Needs you**, **Working**, **Unread**, and **Earlier**. The workspace name appears once, in the sidebar's workspace scope: the Home header says only *Home*, and the selected lane has no visible header (its status sentence remains for screen readers). Rescue lanes for stale or unknown workspaces keep a visible name and New action, because the name is what explains them. Switching workspaces swaps the home content instead of adding a column. Arrow-key navigation follows the visual order, using up/down between stacked lanes and left/right within a lane. Signals are shape-and-text-first (tier labels, weight, dots) so no tier relies on color alone. Counters are sparing: the sidebar's workspace scope shows only the name (counts and key hints live in its menu), New chat carries its shortcut in its tooltip, Home shows a subtle count when chats need you, and Memory shows no count because the review rail states it; every link's accessible name keeps the number. When the pane (not the window) is narrower than 940px the rail drops below the request column. The expanded sidebar stacks the wordmark row (ending in the back / forward pair, a hairline, then the collapse control; the pair drops below the toggle on the collapsed 40px rail, and on phones, where the sidebar is a drawer, the pane header shows a Back chevron after the menu button whenever there is in-app history; ⌘[ / ⌘] in the desktop app), the workspace scope, New chat, the destinations as a labelled list (38px rows, 44px on coarse pointers; the current one on an accent-tinted fill), then the `Projects` tree at 36px row density. In a plain browser tab on a secure origin, Home shows a short *Set up this device* section (install the app, turn on notifications, send a test). It is plain rows with one primary action, dismissible per browser, and it disappears once the app is installed and notifications are on.

Core product work follows one visible model: **request → run → output → durable knowledge**. The transcript remains the reasoning surface. A conditional Work details drawer adds Context, Activity, and Output without forcing a permanent multi-column IDE layout. The chat header holds only close, the title, and **Archive** as a filled primary button with its label (it is the header's one action). Work details is not toggled from the header: an info (ⓘ) button at the right end of the rail's heading hides the rail, and while it is hidden (or the pane is too narrow for a rail) the same ⓘ sits as a small bordered tab at the chat body's top right and brings it back, opening the drawer on narrow panes. Project is the context envelope; Memory's Suggested and To revisit sections are the durable-knowledge inbox; its Map (Graph/List) is the deeper exploration mode. Memory lists its sections in the sidebar exactly as Settings lists its tabs — grouped *To decide* (Suggested, To revisit), *Explore* (Map) and *Records* (Retired, History), each a route with a quiet count at the row's end (accent on the two queues) — so the page header only names the section ("Memory · To revisit") and carries no mode switch or tab row. The map's search and category filters sit under that list only while Map is showing. Suggested and To revisit share one row shape: a 16px heading and one muted sentence, violet-tinted filter chips (by change type; by reason) with counts and no zero chips, then hairline rows with a fixed-width action column of neutral bordered buttons and a text link — no pink primary on a row, since every row is the same routine choice. A suggestion names its change before it is opened: a bordered tag with a drawn icon (*New note*, *Add to a note*, *Merge into a note*, *Update a line*, *Move a note*, *Already saved*), the destination in mono, and a compact numbered diff; its button is the verb (*Create note*, *Add line*, *Merge*) and accepts against the previewed revision. A note to revisit shows *type · reason · backlinks*, and each reason is a disclosure for its evidence (the quoted line with the match marked, or the check date and where it came from).

`Ciaobot.app` owns the macOS Dock window, native menu-bar item, and
notifications. Desktop preferences live in the tray itself rather than a
separate settings window, so the shell has no bundled interactive UI. The tray
is not a miniature copy of the PWA: preserve platform menu conventions and use
it for glanceable status, navigation, unread/input/working state, and server
recovery. The one bundled page (`startup.html`) and the PWA's update overlay
mirror the boot screen's terminal aesthetic (indigo canvas, pink accent,
monospace progress bar and log rows), so the waiting/update states in the
native window, the PWA update overlay, and the PWA boot screen read as one
consistent surface; the remotely loaded PWA retains its own design system and
receives no native capabilities. Finder file drops pass through the main
webview to the PWA's standard composer drop target rather than becoming a
separate native attachment surface. When the engine is
unavailable, the main window uses the same boot-style surface for a concise
waiting/recovery state instead of showing a blank webview; it transitions to
the PWA automatically when the engine becomes reachable. In the PWA, an engine that stops answering shows a full-screen recovery curtain over the kept route. It uses the boot-screen language: a plain title, `ciao service start|status` on this computer, Retry, and automatic reconnect. It is modal (focus trapped, shortcuts suppressed), and it lifts without a reload when the engine returns.

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

Every pane uses one page grid (App.vue `--page-max` 1180px, `--page-gutter` 32px / 16px on phones, `--page-rail` 280px): a main column plus an optional right rail, centred, collapsing to one column when the pane (not the window) is under 940px. The pane header's padding follows the same grid, so the title starts where the content starts and the actions end where the rail ends; a view without its own title shows its page tag as that left title (no centred pill). Rails carry real, glanceable context for the page — Home: what changed; Chat: work details (a one-line note naming the automation the chat comes from, linked, above everything else; then *Agent context* — context-window use as a thin meter from the last reply, the workspace guide and the project brief as hairline rows with token sizes and when each is sent, the project name linking to its page — then *Notes matched in your last message*, one per line, then *Subagents running* when any are, skills and MCP used, files produced); Automations: status and recent or missed runs; Settings: on-this-page links; Memory: the always-loaded budget on its review sections (the Map section has no rail — the drawing takes the pane, with the vault's numbers on one toolbar line and a selected note in the same docked tile a pinned file uses); Project: current counts and where it lives — using the shared rail vocabulary (heading, hairline key/value rows, hairline link rows, muted note). Page bodies use plain sections (sentence-case 16px heading, optional muted line, hairline rows, text-link actions) instead of cards, with one primary action per region and destructive actions behind a menu. In chat, the model picker is the first chip in the composer bar, pending comments ride inside the composer, each agent turn collapses to one "Worked for …" line that opens into a step timeline, and a message's actions (Copy, Fork from here, turn details) appear only when it is selected by click or Enter, lifted above a blurred veil; Esc or clicking the veil puts it back. A pinned file opens as a docked tile, not a second flat column: an inset window 8px from the pane edges in the sidebar's surface (`--bg2`), 14px radius, a 1px border and a soft shadow, so the sidebar and the file read as one layer with the chat as the canvas between them. Its 52px title bar (type badge, filename, muted folder, actions, then Unpin last) lines its bottom rule up with the chat header's, the folder truncates before the filename, the document fills the tile's width with equal 32px margins left and right (no fixed text measure: the tile's width, which the user drags, is the measure), and the gap between chat and tile is the resize handle, showing a small grip on hover.

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
- **Inputs and composer:** Inputs use the deep background, visible border, pink focus ring, and plain-language labels. The chat composer remains the strongest persistent interaction affordance.
- **Badges and status:** Badges are compact supporting signals, never the sole explanation. The sidebar puts subtle numeric counts on the destinations, scoped to the selected workspace: chats needing you on Home, missed runs on Automations, suggestions and notes to revisit on Memory; Settings shows a short word (update, check). The workspace scope carries no counts, and a project row summarises its chats with one static dot only while collapsed. Running, unread, failed, and disabled states need accessible text equivalents.
- **Menus and sheets:** Overflow menus contain secondary and destructive actions when horizontal space is constrained. Mobile modals become edge-to-edge sheets and honor safe areas.
- **Onboarding:** Spotlight backdrops suppress competing content. Skip is visibly actionable but secondary; Back and Next meet the same touch-target requirements as the rest of the app.
- **Tray:** Follow native macOS menu typography, spacing, disabled-state, and confirmation conventions. Keep the menu concise: open Ciao, server status/recovery, unread chats, and essential utilities. Badge counts reflect the full unread total even when the quick list is capped.
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
- Don't copy custom PWA styling into the native tray when platform conventions are clearer.
- Don't introduce new colors, radii, shadows, or typefaces when an existing token serves the purpose.
