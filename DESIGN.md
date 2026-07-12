---
version: alpha
name: Ciao Console
description: A calm, terminal-inspired control surface for a personal AI assistant across the PWA and native tray.
colors:
  primary: "#ff4d6d"
  primary-strong: "#ff2e54"
  secondary: "#6a47b8"
  background: "#1a1a2e"
  surface: "#1f2240"
  surface-interactive: "#2a2e54"
  surface-elevated: "#23264a"
  text: "#e8e8f0"
  text-muted: "#b4b4c4"
  text-subtle: "#7a7a90"
  border: "#2e3258"
  border-strong: "#3a3f70"
  success: "#4caf50"
  warning: "#ff9800"
  error: "#f44336"
  light-primary: "#d81b60"
  light-primary-strong: "#b00d46"
  light-secondary: "#512da8"
  light-background: "#f4f4fa"
  light-surface: "#ffffff"
  light-surface-interactive: "#e6e8f4"
  light-text: "#1a1a2e"
  light-text-muted: "#5f607d"
  light-border: "#d2d4e3"
typography:
  title:
    fontFamily: "SF Mono, Fira Code, Cascadia Code, monospace"
    fontSize: 16px
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: -0.02em
  body:
    fontFamily: "SF Mono, Fira Code, Cascadia Code, monospace"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  body-mobile:
    fontFamily: "SF Mono, Fira Code, Cascadia Code, monospace"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "SF Mono, Fira Code, Cascadia Code, monospace"
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
    textColor: "#ffffff"
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

The PWA is information-dense but not cramped. Monospaced typography, compact labels, restrained animation, and a deep indigo foundation establish the console character. A warm pink accent supplies personality and orientation. The interface must remain understandable without color, animation, hover, or prior knowledge of its icons.

The macOS tray is a native companion, not a miniature copy of the PWA. Preserve platform menu conventions and use it for glanceable status, quick navigation, unread state, and server recovery.

## Colors

Dark mode is the primary visual expression. It uses layered indigo surfaces instead of neutral black, keeping long sessions comfortable while preserving clear hierarchy.

- **Primary pink (`#ff4d6d`):** The brand accent and primary-action color. Use it for the current location, focus, progress, and the single most important action in a region.
- **Violet (`#6a47b8`):** A secondary accent for selected filters, contextual information, and supporting distinctions. It must not compete with the primary action.
- **Background (`#1a1a2e`):** The deepest application canvas.
- **Surfaces (`#1f2240`, `#23264a`, `#2a2e54`):** Cards, elevated controls, hover, and pressed states. Prefer tonal separation and borders over large shadows.
- **Text (`#e8e8f0`):** Primary content. Muted text uses `#b4b4c4`; `#7a7a90` is reserved for genuinely nonessential metadata and disabled states.
- **Semantic colors:** Green communicates success, orange caution or recoverable risk, and red destructive actions or errors. Never use semantic colors decoratively.

Light mode keeps the same hierarchy with a soft lavender canvas, white surfaces, crisp crimson accent, and slate text. It is an adaptation of the same system, not a separate aesthetic.

Color is never the only state signal. Pair status colors with text, an icon, a shape, or an accessible label. Verify WCAG AA contrast in both themes whenever a token or component changes.

## Typography

The product uses the system monospace stack: **SF Mono**, **Fira Code**, **Cascadia Code**, then `monospace`. This is a functional choice: chat metadata, commands, schedules, identifiers, and system status align naturally and retain the product's console character.

- **Titles:** 15–16px, bold, with slightly tight tracking. Titles should remain visible when actions compete for space.
- **Body:** 13–14px on desktop with comfortable line height. Long-form assistant content may breathe more than controls and metadata.
- **Mobile form text:** At least 16px to prevent browser auto-zoom while keeping user zoom available.
- **Labels:** 11px, semibold, often uppercase with 0.5px tracking. Use for short section titles and field labels, not paragraphs.
- **Wordmark:** Bold monospace with a pink `›` prompt prefix. A blinking caret may appear only in startup or explicitly terminal-like moments.

Respect the user-controlled font scale. Truncate compact navigation labels only when the full value remains available through context, title text, or an expanded view.

## Layout

The PWA is mobile-first and safe-area aware. Desktop uses a persistent project sidebar beside the active workspace. Narrow screens use an overlay sidebar and full-width panels, with secondary actions moving into menus or sheets before titles are sacrificed.

Use the 4px-based spacing scale deliberately: 4px for internal micro-spacing, 8px between closely related controls, 12–16px for component rhythm, and 24–32px between major groups. Shared settings and schedule pages are capped near 600px so forms remain readable on wide screens.

All interactive targets are at least 44×44px on touch layouts. Compact visual glyphs may sit inside a larger hit area. Honor device safe areas, virtual keyboards, standalone PWA chrome, and browser zoom. Do not disable pinch zoom or text scaling.

Long or implementation-oriented content must not dominate a mobile page. Collapse long prompts and diagnostics behind Preview/Expand/Copy controls, preserve the page title, and move secondary or destructive actions into an overflow menu or bottom sheet.

## Elevation & Depth

Hierarchy comes primarily from tonal layers, one-pixel borders, and spacing. Cards sit on the page background; inputs may use the deeper canvas; modals and popovers use the elevated surface. Use stronger borders for focus and separation before adding shadows.

Shadows are reserved for content that genuinely floats above another interaction layer: mobile drawers, menus, modals, and toasts. Keep them soft and dark. A subtle low-opacity grain may texture the page background, but it must never reduce legibility or imply disabled content.

## Shapes

The shape language is compact and gently rounded. Standard controls and cards use a 10px radius, small nested elements use 6px, and modals use 14px. Fully rounded shapes are limited to badges, status dots, avatars, and true pill selectors.

Borders are structural, not decorative. Active navigation is marked by a slim pink edge plus a tonal background. Do not mix exaggerated rounding, glass effects, or unrelated shape styles into the same view.

## Components

- **Primary actions:** Pink filled buttons are scarce. Use one for the most important forward action in a panel. Routine, reversible, or secondary actions use neutral bordered controls.
- **Caution and danger:** Restart and similar recoverable operations use orange caution styling. Delete and irreversible actions use red and require clear wording or confirmation.
- **Navigation:** Use native buttons or links where possible. Every navigation row must support keyboard focus and Enter/Space activation, expose its selected/expanded state, and retain a visible text label.
- **Cards and panels:** Group related information with a tonal surface, border, 10px radius, and 16px padding. Avoid nesting multiple bordered cards without a clear hierarchy.
- **Inputs and composer:** Inputs use the deep background, visible border, pink focus ring, and plain-language labels. The chat composer remains the strongest persistent interaction affordance.
- **Badges and status:** Badges are compact supporting signals, never the sole explanation. Running, unread, failed, and disabled states need accessible text equivalents.
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
