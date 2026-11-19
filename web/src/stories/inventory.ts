export type Coverage = 'unknown' | 'uncovered' | 'covered' | 'not-applicable'
export type Migration = 'unassessed' | 'retained' | 'planned' | 'migrated'
export interface InventoryEntry {
  id: string; name: string; family: string; kind: 'component' | 'composition' | 'inline' | 'css'
  owner: { path: string; anchor: string }; sources: string[]; consumers: string[]
  variants: string[]; variantNotes: string
  states: { name: string; coverage: Coverage; storyIds: string[]; note: string }[]
  loading: { status: 'known' | 'none' | 'unknown'; detail: string }
  migration: { status: Migration; note: string }; rationale?: string
}
export interface InventoryFilters { query: string; family: string; coverage: 'all' | Coverage; migration: 'all' | Migration }

// Ownership is deliberately explicit: adding a production SFC requires its own row.
export const inventory: InventoryEntry[] = [
  {
    id: 'components-settings-settings-switch',
    name: 'SettingsSwitch',
    family: 'Settings',
    kind: 'component',
    owner: { path: 'web/src/components/settings/SettingsSwitch.vue', anchor: 'class="settings-switch"' },
    sources: [],
    consumers: ['web/src/components/settings/SettingsNotifications.vue', 'web/src/components/settings/SettingsRestartResume.vue'],
    variants: ['checked', 'disabled', 'busy'],
    variantNotes: 'Controlled native switch; checked controls On/Off, disabled blocks activation, busy shows an ellipsis without disabling. Parents own saves.',
    states: [
      { name: 'default', coverage: 'covered', storyIds: ['settings-switch--on', 'settings-switch--off'], note: 'On renders checked; Off renders unchecked. Both use the production component.' },
      { name: 'disabled', coverage: 'covered', storyIds: ['settings-switch--disabled-on', 'settings-switch--disabled-off'], note: 'DisabledOn renders checked and disabled; DisabledOff renders unchecked and disabled.' },
      { name: 'busy', coverage: 'covered', storyIds: ['settings-switch--busy'], note: 'Busy renders checked, aria-busy and an ellipsis; the native button remains enabled.' },
      { name: 'narrow', coverage: 'covered', storyIds: ['settings-switch--narrow'], note: 'Narrow renders checked in a maximum 390px container.' },
      { name: 'keyboard-focus', coverage: 'covered', storyIds: ['settings-switch--keyboard-focus'], note: 'Tab lands on the switch with visible :focus-visible; Enter toggles aria-checked.' },
      { name: 'reduced-motion', coverage: 'covered', storyIds: ['settings-switch--reduced-motion'], note: 'Wrapper mirrors the production reduced-motion track rule; play asserts zero transition and a toggle.' },
    ],
    loading: { status: 'none', detail: 'No initial or refresh fetch. Busy is supplied by a parent, not owner-level loading coverage.' },
    migration: { status: 'migrated', note: 'Approved shared-switch slice 1: restart resume is the remaining Settings consumer; other native Settings switches are not migrated.' },
  },
  {
    "id": "app",
    "name": "App",
    "family": "Root, navigation and common presentation",
    "kind": "composition",
    "owner": {
      "path": "web/src/App.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/main.ts"
    ],
    "variants": [],
    "variantNotes": "Root composition; startup/recovery/restart/dialog hosts, not an isolated new app story. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in App.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in App.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-activity-glyph",
    "name": "ActivityGlyph",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ActivityGlyph.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatSignals.vue",
      "web/src/components/NeedsYouCard.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProposalReviewPanel.vue"
    ],
    "variants": [
      "working",
      "alert"
    ],
    "variantNotes": "One mark per kind: working is a 3x3 pixel grid with one lit pixel chasing the perimeter, alert is a filled still triangle. Shape and motion carry the meaning, never hue.",
    "states": [
      {
        "name": "working",
        "coverage": "covered",
        "storyIds": ["activity-glyph--working"],
        "note": "Working renders the nine-cell grid with one fixed dim centre; its reduced-motion frame is the checker."
      },
      {
        "name": "alert",
        "coverage": "covered",
        "storyIds": ["activity-glyph--alert"],
        "note": "Alert renders the filled triangle and the two knocked-out marks, still."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static SVG marks with no fetch, so there is no initial, refresh, failure or stale state. The working grid animates."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-agent-context-section",
    "name": "AgentContextSection",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/AgentContextSection.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Loaded/partial token counts, compaction, prompt disclosure and failed prompt load, guide errors/context/repo instructions. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in AgentContextSection.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in AgentContextSection.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-app-icon",
    "name": "AppIcon",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/AppIcon.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/AgentContextSection.vue",
      "web/src/components/BrowserTerminalButton.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRepoSection.vue",
      "web/src/components/ChatTurnActivity.vue",
      "web/src/components/InjectedPromptCard.vue",
      "web/src/components/NeedsYouCard.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/TaskOriginNote.vue",
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": [
      "name",
      "size",
      "label"
    ],
    "variantNotes": "Name selects one of 22 drawn shape branches; size sets the pixel box (default 16); label makes the icon an accessible image, otherwise it is decorative and hidden.",
    "states": [
      {
        "name": "image",
        "coverage": "covered",
        "storyIds": ["app-icon--image"],
        "note": "Image renders the framed picture, decorative at 16px."
      },
      {
        "name": "doc",
        "coverage": "covered",
        "storyIds": ["app-icon--doc"],
        "note": "Doc renders the ruled page, decorative at 16px."
      },
      {
        "name": "file",
        "coverage": "covered",
        "storyIds": ["app-icon--file"],
        "note": "File renders the clipped-corner page, decorative at 16px."
      },
      {
        "name": "model",
        "coverage": "covered",
        "storyIds": ["app-icon--model"],
        "note": "Model renders the chip, decorative at 16px."
      },
      {
        "name": "question",
        "coverage": "covered",
        "storyIds": ["app-icon--question"],
        "note": "Question renders the framed query, decorative at 16px."
      },
      {
        "name": "comment",
        "coverage": "covered",
        "storyIds": ["app-icon--comment"],
        "note": "Comment renders the log window with a tail, decorative at 16px."
      },
      {
        "name": "clock",
        "coverage": "covered",
        "storyIds": ["app-icon--clock"],
        "note": "Clock renders the marked clock face, decorative at 16px."
      },
      {
        "name": "activity",
        "coverage": "covered",
        "storyIds": ["app-icon--activity"],
        "note": "Activity renders the step signal, decorative at 16px."
      },
      {
        "name": "shield",
        "coverage": "covered",
        "storyIds": ["app-icon--shield"],
        "note": "Shield renders the permission shield, decorative at 16px."
      },
      {
        "name": "spark",
        "coverage": "covered",
        "storyIds": ["app-icon--spark"],
        "note": "Spark renders the four-point star, decorative at 16px."
      },
      {
        "name": "check",
        "coverage": "covered",
        "storyIds": ["app-icon--check"],
        "note": "Check renders the stroke tick, decorative at 16px."
      },
      {
        "name": "terminal",
        "coverage": "covered",
        "storyIds": ["app-icon--terminal"],
        "note": "Terminal renders the prompt window, decorative at 16px."
      },
      {
        "name": "arrow",
        "coverage": "covered",
        "storyIds": ["app-icon--arrow"],
        "note": "Arrow renders the navigation arrow, decorative at 16px."
      },
      {
        "name": "chevron",
        "coverage": "covered",
        "storyIds": ["app-icon--chevron"],
        "note": "Chevron renders the disclosure chevron, decorative at 16px."
      },
      {
        "name": "skill",
        "coverage": "covered",
        "storyIds": ["app-icon--skill"],
        "note": "Skill renders the five-point star, decorative at 16px."
      },
      {
        "name": "plug",
        "coverage": "covered",
        "storyIds": ["app-icon--plug"],
        "note": "Plug renders the connector, decorative at 16px."
      },
      {
        "name": "folder",
        "coverage": "covered",
        "storyIds": ["app-icon--folder"],
        "note": "Folder renders the folder outline, decorative at 16px."
      },
      {
        "name": "branch",
        "coverage": "covered",
        "storyIds": ["app-icon--branch"],
        "note": "Branch renders the git branch, decorative at 16px."
      },
      {
        "name": "code",
        "coverage": "covered",
        "storyIds": ["app-icon--code"],
        "note": "Code renders the angle brackets, decorative at 16px."
      },
      {
        "name": "copy",
        "coverage": "covered",
        "storyIds": ["app-icon--copy"],
        "note": "Copy renders the overlapping sheets, decorative at 16px."
      },
      {
        "name": "warning",
        "coverage": "covered",
        "storyIds": ["app-icon--warning"],
        "note": "Warning renders the triangle, decorative at 16px."
      },
      {
        "name": "task",
        "coverage": "covered",
        "storyIds": ["app-icon--task"],
        "note": "Task renders the checked box, decorative at 16px."
      },
      {
        "name": "labelled",
        "coverage": "covered",
        "storyIds": ["app-icon--labelled"],
        "note": "Labelled renders role img with the label as its accessible name and no aria-hidden."
      },
      {
        "name": "sized",
        "coverage": "covered",
        "storyIds": ["app-icon--sized"],
        "note": "Sized renders the warning artwork in a 24px box, still on the 24-unit viewBox."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static SVG glyphs with no fetch and no asynchronous state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-archive-task-dialog",
    "name": "ArchiveTaskDialog",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ArchiveTaskDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Production askArchiveTask requests with one mounted host per canvas; no inline Docs. Further configurable variants are unassessed.",
    "states": [
      { "name": "closed", "coverage": "covered", "storyIds": ["archive-task-dialog--closed"], "note": "No request: the production dialog stays closed." },
      { "name": "choose", "coverage": "covered", "storyIds": ["archive-task-dialog--choose"], "note": "Open task that is not done: Cancel, Keep task open, and Complete task…." },
      { "name": "awaiting-review", "coverage": "covered", "storyIds": ["archive-task-dialog--awaiting-review"], "note": "Review-ready task asks to approve and archive, or keep the task open." },
      { "name": "note", "coverage": "covered", "storyIds": ["archive-task-dialog--note"], "note": "Optional completion note, Back, and Mark done; focus moves to the note field." },
      { "name": "note-awaiting-review", "coverage": "covered", "storyIds": ["archive-task-dialog--note-awaiting-review"], "note": "Review-ready note step confirms with Approve Done." },
      { "name": "busy", "coverage": "covered", "storyIds": ["archive-task-dialog--busy"], "note": "Held completion POST shows Saving… and disables Back, the note, and the submit action." },
      { "name": "submit-error", "coverage": "covered", "storyIds": ["archive-task-dialog--submit-error"], "note": "Refused completion keeps the dialog open, shows the alert, and returns focus to the note." },
      { "name": "keep-task-open", "coverage": "covered", "storyIds": ["archive-task-dialog--keep-task-open"], "note": "Keep task open resolves keep and closes the dialog." },
      { "name": "escape-cancels", "coverage": "covered", "storyIds": ["archive-task-dialog--escape-cancels"], "note": "Escape resolves cancel and closes the dialog." },
      { "name": "back", "coverage": "covered", "storyIds": ["archive-task-dialog--back"], "note": "Back returns from the note step to the choose actions." },
      { "name": "initial-focus", "coverage": "covered", "storyIds": ["archive-task-dialog--initial-focus"], "note": "Open focus is on Cancel, the safe action." },
      { "name": "narrow", "coverage": "covered", "storyIds": ["archive-task-dialog--narrow"], "note": "Choose step in a maximum 390px containing block; also check a real narrow viewport." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ArchiveTaskDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-brand-mark",
    "name": "BrandMark",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/BrandMark.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ProjectSidebar.vue"
    ],
    "variants": [],
    "variantNotes": "No configurable variants: a fixed 30px box around the production CiaoMark; the mark's dots follow the accent.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["brand-mark--default"],
        "note": "Default renders the 30px brand box with the accessible name Ciaobot around the production CiaoMark."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "BrandMark.vue is static SVG presentation and performs no loading."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-wordmark",
    "name": "Wordmark",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/Wordmark.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/LoginView.vue",
      "web/src/components/RestartNotice.vue",
      "web/src/components/StartupView.vue",
      "web/src/components/UpdateProgressView.vue"
    ],
    "variants": [
      "text",
      "size"
    ],
    "variantNotes": "Text replaces the brand name (every `o`, in any case, becomes an accent dot; other characters stay letters); size is sm, md or lg.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["wordmark--default"],
        "note": "Default renders Ciaobot at md, with the two `o` characters as accent dots and the full text as the accessible name."
      },
      {
        "name": "size-small",
        "coverage": "covered",
        "storyIds": ["wordmark--small"],
        "note": "Small renders the brand text at the sm size."
      },
      {
        "name": "size-large",
        "coverage": "covered",
        "storyIds": ["wordmark--large"],
        "note": "Large renders the brand text at the lg size."
      },
      {
        "name": "no-dots",
        "coverage": "covered",
        "storyIds": ["wordmark--no-dots"],
        "note": "NoDots renders a synthetic text without an `o`, so every character is a letter and no accent dot appears."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["wordmark--narrow"],
        "note": "Narrow renders the brand text in a maximum 390px column, still on one line."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Wordmark.vue is static presentation and performs no loading."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-browser-terminal-button",
    "name": "BrowserTerminalButton",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/BrowserTerminalButton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ProjectView.vue"
    ],
    "variants": [],
    "variantNotes": "Row/other button form. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in BrowserTerminalButton.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in BrowserTerminalButton.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-comment-popover",
    "name": "ChatCommentPopover",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatCommentPopover.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Hover/pinned/selection read comment. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatCommentPopover.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatCommentPopover.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-header-more-menu",
    "name": "ChatHeaderMoreMenu",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatHeaderMoreMenu.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatHeaderMoreMenu.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatHeaderMoreMenu.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-layout",
    "name": "ChatLayout",
    "family": "Root, navigation and common presentation",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/ChatLayout.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/router.ts"
    ],
    "variants": [],
    "variantNotes": "Connected route/split/sidebar/Home composition; known pending chat and bootstrap branches. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatLayout.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatLayout.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-chat-pane-skeleton",
    "name": "ChatPaneSkeleton",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatPaneSkeleton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Prop-less: one loading render and no configurable variants.",
    "states": [
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["chat-pane-skeleton--loading"],
        "note": "ChatPaneSkeleton.vue's one render: a role=status named Loading conversation with aria-busy, the pane header rule, two assistant placeholders, one user placeholder and a collapsed trace row, all aria-hidden. The story asserts no focusable element exists."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["chat-pane-skeleton--narrow"],
        "note": "The loading render in a 390px containing block: the body keeps its reading measure inside the block. The phone header grid is a max-width 768px viewport media query the 390px wrapper does not trigger."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading only: the shell covers the lazy ChatPanel chunk and a chat route whose catalog entry has not arrived. It has no loaded, empty, filtered empty, failure, refresh or stale state of its own, and no interactive surface. Geometry is copied from ChatPanel.vue's .history-skeleton-stack so the handover fills in rather than jumps; the placeholder shapes are not the loaded transcript's content."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-panel",
    "name": "ChatPanel",
    "family": "Chat, messages, composer, activity and context",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/ChatPanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue",
      "web/src/lib/chatComponents.ts"
    ],
    "variants": [
      "messages",
      "composer",
      "question",
      "capability"
    ],
    "variantNotes": "Real owner of inline message bubbles, streaming transcript, composer, question/capability cards and turn footer; connected composition, not stories importing the live store.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatPanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "streaming",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ChatPanel.vue implements the streaming branch; no rendered-state story exists."
      },
      {
        "name": "question",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ChatPanel.vue implements the question branch; no rendered-state story exists."
      },
      {
        "name": "retry",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ChatPanel.vue implements the retry branch; no rendered-state story exists."
      },
      {
        "name": "history-loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ChatPanel.vue implements the history-loading branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "ChatPanel.vue .history-skeleton-stack owns initial transcript loading; route loading is ChatPaneSkeleton.vue. Geometry parity is not audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-chat-repo-section",
    "name": "ChatRepoSection",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatRepoSection.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Missing repo/worktree/dirty/clean variants. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatRepoSection.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatRepoSection.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-retry-state",
    "name": "ChatRetryState",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatRetryState.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Countdown/attempt/error/provider-auth recovery. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatRetryState.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatRetryState.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-signals",
    "name": "ChatSignals",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatSignals.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProjectView.vue"
    ],
    "variants": [
      "card",
      "sidebar"
    ],
    "variantNotes": "Card/sidebar density, needs/memory/working/retry/unread/schedule signals.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatSignals.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatSignals.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-chat-turn-activity",
    "name": "ChatTurnActivity",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ChatTurnActivity.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Collapsed/expanded, activity/filecard/thinking/lazy step/output branches; chatTrace.css. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ChatTurnActivity.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatTurnActivity.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-ciao-mark",
    "name": "CiaoMark",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/CiaoMark.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/BrandMark.vue",
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "No configurable variants: one inline SVG whose C follows currentColor and whose two dots use the accent.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["ciao-mark--default"],
        "note": "Default renders the decorative inline mark in a sized box; the C is a currentColor stroke and both dots use --accent."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "CiaoMark.vue is static SVG presentation and performs no loading."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-comment-compose-popover",
    "name": "CommentComposePopover",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/CommentComposePopover.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/PinnedFilePanel.vue"
    ],
    "variants": [],
    "variantNotes": "Anchored composing with/without images; distinct focus/placement owner. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in CommentComposePopover.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in CommentComposePopover.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-composer-upload-progress",
    "name": "ComposerUploadProgress",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ComposerUploadProgress.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Hidden when empty, real upload-progress rows. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ComposerUploadProgress.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ComposerUploadProgress.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-confirm-dialog",
    "name": "ConfirmDialog",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ConfirmDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Production askConfirm/askConfirmReason requests with one mounted host per canvas; no inline Docs. Enter confirms even with Cancel focused, except inside the reason field or chips. Further configurable variants are unassessed.",
    "states": [
      { "name": "default", "coverage": "covered", "storyIds": ["confirm-dialog--normal"], "note": "Normal opens a non-destructive request through askConfirm." },
      { "name": "destructive", "coverage": "covered", "storyIds": ["confirm-dialog--destructive"], "note": "Destructive uses the actual danger action." },
      { "name": "reason", "coverage": "covered", "storyIds": ["confirm-dialog--reason"], "note": "Reason uses askConfirmReason and the production DecisionReason." },
      { "name": "long-content", "coverage": "covered", "storyIds": ["confirm-dialog--long-content"], "note": "Long title, multiline message and longer action label." },
      { "name": "narrow", "coverage": "covered", "storyIds": ["confirm-dialog--narrow"], "note": "Reason dialog in a maximum 390px, 640px-high containing block; also check a real narrow viewport." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ConfirmDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-csv-viewer",
    "name": "CsvViewer",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/CsvViewer.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/FileViewerModal.vue",
      "web/src/components/PinnedFilePanel.vue",
      "web/src/composables/useFileComments.ts"
    ],
    "variants": [
      "read-only",
      "editable",
      "commentable",
      "header",
      "empty",
      "parse-error"
    ],
    "variantNotes": "Read-only/editable/commentable/header/empty/parse-error variants: the row-number column and meta bar belong to the commentable preview, the toolbar and inputs to edit mode; hasHeader is content-derived.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["csv-viewer--default"],
        "note": "Default props: a read-only non-commentable table with no rows header-like, so headers synthesize (Column 1..N), no row-number column renders and cells are spans. The meta bar still reads 'Click a cell to comment' at these props although clicks do nothing until the commentable prop is set."
      },
      {
        "name": "header",
        "coverage": "covered",
        "storyIds": ["csv-viewer--header"],
        "note": "A header-like first row renders as th text through parseCsv's hasHeader; in edit mode those headers become labeled inputs (covered by the editable state)."
      },
      {
        "name": "commentable",
        "coverage": "covered",
        "storyIds": ["csv-viewer--commentable"],
        "note": "Read-only commentable preview (how FileViewerModal renders CSVs): the row-number column, the meta bar's click hint, durable cell highlights via the cellComments prop, and the is-selected mark a click leaves."
      },
      {
        "name": "editable",
        "coverage": "covered",
        "storyIds": ["csv-viewer--editable"],
        "note": "Edit mode (readOnly false): the toolbar with + Row / + Column and the rows·cols count, labeled header/cell inputs, and one delete control per row. hasHeader false in edit mode synthesizes headers on add; not separately covered."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": ["csv-viewer--empty"],
        "note": "Content whose parse yields no rows renders 'Empty CSV.' and no table. A content row that parses with quoted cells is not a distinct branch."
      },
      {
        "name": "parse-error",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The v-if=\"parseError\" branch is defensive only: parseCsv (papaparse, header:false) never throws for the string content props supply, so parseError cannot be set from props; no story can render it without editing the owner."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["csv-viewer--narrow"],
        "note": "A 390px pane: the meta bar and the table's own .csv-scroll scroll container still render inside the narrow wrapper. The 640px/767px media queries are viewport-driven and belong to the visual check, not a wrapper state."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "CsvViewer parses its content prop synchronously and performs no fetches: there is no initial, refresh, failure or stale loading state of its own. Its parseError branch is a dead defensive path (see the parse-error state)."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-decision-reason",
    "name": "DecisionReason",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/DecisionReason.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ConfirmDialog.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": [],
    "variantNotes": "Production controlled field with default quick reasons, custom text and disabled controls; no persistence. Further configurable variants are unassessed.",
    "states": [
      { "name": "empty", "coverage": "covered", "storyIds": ["decision-reason--empty"], "note": "Empty controlled textarea and unselected quick reasons." },
      { "name": "quick-reason", "coverage": "covered", "storyIds": ["decision-reason--quick-reason"], "note": "Already done is selected in the actual chip group." },
      { "name": "custom", "coverage": "covered", "storyIds": ["decision-reason--custom"], "note": "Custom text with no selected quick reason." },
      { "name": "disabled", "coverage": "covered", "storyIds": ["decision-reason--disabled"], "note": "All chips and the textarea disabled with a selected reason." },
      { "name": "long-content", "coverage": "covered", "storyIds": ["decision-reason--long-content"], "note": "Long text at the 1000-character field limit." },
      { "name": "narrow", "coverage": "covered", "storyIds": ["decision-reason--narrow"], "note": "Custom text and wrapping chips in a maximum 390px container." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in DecisionReason.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-engine-offline-view",
    "name": "EngineOfflineView",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/EngineOfflineView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in EngineOfflineView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in EngineOfflineView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-file-viewer-modal",
    "name": "FileViewerModal",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/FileViewerModal.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/lib/chatComponents.ts"
    ],
    "variants": [
      "preview",
      "history",
      "diff",
      "loading",
      "error",
      "editing"
    ],
    "variantNotes": "Lazy dialog owner. Preview branches: text/Markdown/document properties/empty, CSV, image (+ its load-failure alert), PDF (+ the LibreOffice notice), HTML artifact (preview/code/code-loading/code-error) and folder. Loading/error and the streaming-end refresh; editing (textarea/CSV/save-flight/save-failure); comment draft and read popovers; pinned and pin-pending; the More actions menu; the archived-transcript continue action; narrow.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--default"],
        "note": "A chat-scoped text file loaded: title/subtitle, Edit, Discuss, unpinned pin control and the pre body; open-auto-focus lands the keyboard on the modal frame and Escape closes through requestClose."
      },
      {
        "name": "markdown",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--markdown"],
        "note": "A .md file renders the .fv-md branch; the chat-less open keeps Edit hidden (canEdit needs a chat id)."
      },
      {
        "name": "document-properties",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--document-properties"],
        "note": "YAML frontmatter renders the .fv-meta properties header (kind, status, name, summary, tags) above the markdown body."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--empty"],
        "note": "A loaded file with empty content renders the .fv-pre branch with no text."
      },
      {
        "name": "csv",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--csv"],
        "note": "A CSV path renders CsvViewer read-only and commentable inside the modal, with a durable cell highlight and the cell-select mark. Its Comment trigger is not asserted: a click fires the document selectionchange handler, which clears the CSV cell anchor (useFileComments onSelectionChange). The comment compose and read flows are their own states below."
      },
      {
        "name": "image",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--image"],
        "note": "A served attachment (imageUrl) renders the .fv-img branch; a served image has no workspace path, so pin, Discuss and Edit are absent."
      },
      {
        "name": "image-error",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--image-error"],
        "note": "A workspace-path image that fails to load: the <img> native load 404s, onImageError probes the same URL (declared route) and the role=alert error branch replaces the image. The raw 404 the img makes is a plain resource load outside the fence."
      },
      {
        "name": "folder",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--folder"],
        "note": "kind folder mounts the real FolderBrowser (VueFinder) over one declared /api/workspace-dir listing; the browser's own first fetch is the only request."
      },
      {
        "name": "pdf",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--pdf"],
        "note": "kind pdf renders the workspace-binary preview iframe; the iframe's navigation is a plain resource load outside the fence, so the story asserts the iframe attributes rather than its content."
      },
      {
        "name": "libreoffice-notice",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--libreoffice-notice"],
        "note": "A .pptx with pptxNeedsLibreoffice renders the install notice; the Install in Chat click stops at the store's fix-chat flow (no General project seeded → error toast, zero requests)."
      },
      {
        "name": "html",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--html"],
        "note": "kind html in preview view mounts HtmlArtifactViewer: the toolbar and the sandbox='allow-scripts' frame the artifact contract depends on."
      },
      {
        "name": "html-code",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--html-code"],
        "note": "Switching to Code view fetches the artifact source over /api/workspace-file (declared route) and renders the code; with sourceLoaded, Edit becomes available."
      },
      {
        "name": "html-code-loading",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--html-code-loading"],
        "note": "Code view while the source fetch is in flight renders the 'Loading source…' note and keeps Edit refused."
      },
      {
        "name": "html-code-error",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--html-code-error"],
        "note": "A failed source fetch renders the source-error note; Edit stays refused without the source."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--loading"],
        "note": "Initial open held at the declared /api/workspace-file route: the .fv-loading 'Loading…' branch, driven through the store's real open() flow."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--error"],
        "note": "An open answered HTTP 404 sets store.error and renders the .fv-error branch (fileLoadErrorMessage wording)."
      },
      {
        "name": "refresh",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--refresh"],
        "note": "The streaming-end watcher re-opens the same file: chat path resolution then a fresh read (both routes declared) repaints the body with the new content."
      },
      {
        "name": "refresh-failure",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--refresh-failure"],
        "note": "A refresh whose read fails (HTTP 500) replaces the body with the .fv-error branch."
      },
      {
        "name": "editing",
        "coverage": "covered",
        "storyIds": ["file-modal-preview-editor--editing"],
        "note": "The Edit action swaps the body for .fv-edit-shell's textarea: buffer starts from content, typing marks isDirty, Cancel restores the preview without a write."
      },
      {
        "name": "editing-csv",
        "coverage": "covered",
        "storyIds": ["file-modal-preview-editor--editing-csv"],
        "note": "Editing a CSV path renders CsvViewer in edit mode inside .fv-edit-shell (toolbar, labeled header/cell inputs, row add/delete)."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": ["file-modal-preview-editor--saving"],
        "note": "A save POST held by the boundary blocks both Cancel and Save ('Saving…'), the real disabled state not a styled one."
      },
      {
        "name": "save-error",
        "coverage": "covered",
        "storyIds": ["file-modal-preview-editor--save-error"],
        "note": "A save answered HTTP 500 renders 'Save failed (HTTP 500).' in the editor and unblocks the controls without writing."
      },
      {
        "name": "pinned",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--pinned"],
        "note": "The dock target's pinned file shows the pin control active ('Unpin from sidebar'); unpinning keeps the modal open (only a successful pin closes it)."
      },
      {
        "name": "pin-pending",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--pin-pending"],
        "note": "A chat-scoped pin write held at PATCH /api/chats/{id} leaves the pin control actually disabled."
      },
      {
        "name": "actions-menu",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--actions-menu"],
        "note": "The More menu lists Copy path / Show in folder / Download / Open in default app (loopback page); Download is enabled on a loaded file and Open in memory map is absent for non-markdown paths. The copy/share/open-elsewhere confirmation labels are transient timers of this same menu."
      },
      {
        "name": "archived-transcript",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--archived-transcript"],
        "note": "A memory-vault Logs/Chats transcript path adds 'Continue this chat'; its click runs continueArchivedChat, and a failed declare (HTTP 500) surfaces the store's error toast then restores the button."
      },
      {
        "name": "comment-draft",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--comment-draft"],
        "note": "Selecting text in the pre body floats the Comment trigger; clicking it opens the compose popover, and saving stages the comment durably for the file and re-highlights the line."
      },
      {
        "name": "comment-popover",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--comment-popover"],
        "note": "A durable line comment highlights its .pre-line; clicking it opens the read popover (location label, note, Edit/Delete), and Delete removes the highlight."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["file-viewer-modal--narrow"],
        "note": "The dialog is a fixed overlay teleported to body, so a 390px wrapper cannot constrain it: the story sets the narrow viewport (parameters.viewport) and asserts the modal fits it. The ≤640px media queries (Discuss into the menu, zero padding) are viewport-driven and belong to the visual check."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is one GET /api/workspace-file (held in flight → the .fv-loading text line; failure → the .fv-error line): no skeleton geometry, just a centered text line that does not match the loaded table/prose geometry, so a loading-geometry follow-up applies. Loaded, empty, image-error and initial failure are rendered states; the streaming-end watcher performs the only refresh (refresh / refresh-failure; a superseded response is discarded by the store's fetch generation with no visual stale state)."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-folder-browser",
    "name": "FolderBrowser",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/FolderBrowser.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/FileViewerModal.vue",
      "web/src/components/PinnedFilePanel.vue"
    ],
    "variants": [],
    "variantNotes": "VueFinder boundary/initial listing/error/truncation; no runtime import into catalogue. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in FolderBrowser.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in FolderBrowser.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-intake",
    "name": "HomeIntake",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeIntake.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [
      "prompt",
      "project",
      "model",
      "attachments"
    ],
    "variantNotes": "Real Home composer owner: prompt/start, project/model/slash picker, incoming share/staged attachment/error states.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in HomeIntake.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "draft",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeIntake.vue implements the draft branch; no rendered-state story exists."
      },
      {
        "name": "attachments",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeIntake.vue implements the attachments branch; no rendered-state story exists."
      },
      {
        "name": "action-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeIntake.vue implements the action-error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HomeIntake.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-notice-reopen",
    "name": "HomeNoticeReopen",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeNoticeReopen.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Closed count/hidden state. Detailed variants are unassessed.",
    "states": [
      {
        "name": "hidden",
        "coverage": "covered",
        "storyIds": ["home-notice-reopen--hidden"],
        "note": "A closed count of zero renders no control. The story asserts the absent button and a quiet fixture ledger."
      },
      {
        "name": "closed-count-singular",
        "coverage": "covered",
        "storyIds": ["home-notice-reopen--singular"],
        "note": "One closed notice renders the singular label; the play reopens it and asserts the count control disappears."
      },
      {
        "name": "closed-count-plural",
        "coverage": "covered",
        "storyIds": ["home-notice-reopen--plural"],
        "note": "Two closed notices render the plural label; the play reopens all and asserts both rows return."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["home-notice-reopen--keyboard-focus"],
        "note": "The reopen control receives visible keyboard focus and Enter reopens the notices."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "HomeNoticeReopen reads an in-memory closed-count synchronously; it performs no request and has no loading or refresh state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-notice-window",
    "name": "HomeNoticeWindow",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeNoticeWindow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeSetupCard.vue",
      "web/src/components/HomeWelcomeCard.vue"
    ],
    "variants": [],
    "variantNotes": "Shared window composition/slots. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["home-notice-window--default"],
        "note": "Real owner with a synthetic title, key and slot; the play closes the window and asserts focus moves to the recovery control."
      },
      {
        "name": "blocking",
        "coverage": "covered",
        "storyIds": ["home-notice-window--blocking"],
        "note": "The blocking modifier is asserted on the real article and close/focus still works."
      },
      {
        "name": "custom-close-label",
        "coverage": "covered",
        "storyIds": ["home-notice-window--custom-close-label"],
        "note": "A caller-supplied close label is used for both the accessible name and tooltip, and the close path still returns focus."
      },
      {
        "name": "close-keyboard-focus",
        "coverage": "covered",
        "storyIds": ["home-notice-window--close-keyboard-focus"],
        "note": "The close button is keyboard focused with a visible ring and Enter closes the window and moves focus."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["home-notice-window--narrow"],
        "note": "The owner renders inside a 390px wrapper with its closing and focus behaviour intact."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "HomeNoticeWindow is a pure presentational shell over a slot; it fetches nothing and has no loading, busy or refresh branch."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-onboarding-resume",
    "name": "HomeOnboardingResume",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeOnboardingResume.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "hidden",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--hidden"],
        "note": "A completed receipt renders nothing; the story asserts the absent card after the receipt read."
      },
      {
        "name": "initial-load-failure",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--initial-failure"],
        "note": "A synthetic 503 leaves the card hidden, matching the owner's fail-silent fetch."
      },
      {
        "name": "installation-pending",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--installation-pending"],
        "note": "An in-progress installation receipt renders the resume card and its inert /onboarding link."
      },
      {
        "name": "workspace-pending",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--workspace-pending"],
        "note": "A pending workspace receipt names the synthetic workspace in the label and link."
      },
      {
        "name": "refresh-focus",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--focus-refresh"],
        "note": "A window focus event re-reads the receipt; the play dispatches it and asserts the newly pending workspace label appears."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["home-onboarding-resume--narrow"],
        "note": "The pending card and its link stay usable inside a 390px wrapper."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "HomeOnboardingResume renders nothing until its first /api/onboarding read answers, and also renders nothing when that read fails: there is no skeleton, spinner or error text to distinguish those moments. Refresh keeps the previous label while the new read is in flight."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-recent-chats",
    "name": "HomeRecentChats",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeRecentChats.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/tiers/empty/pending-title/task and memory-insight rows; ListRow, ChatSignals. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in HomeRecentChats.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HomeRecentChats.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-review-summary",
    "name": "HomeReviewSummary",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeReviewSummary.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Known checking/current/stale/empty/failed contract, counts rendered from its actual derived items. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in HomeReviewSummary.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "checking",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeReviewSummary.vue implements the checking branch; no rendered-state story exists."
      },
      {
        "name": "current",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeReviewSummary.vue implements the current branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeReviewSummary.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeReviewSummary.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "failed",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "HomeReviewSummary.vue implements the failed branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HomeReviewSummary.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-setup-card",
    "name": "HomeSetupCard",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeSetupCard.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Installation/push/phone/error, persistent browser dismissal. Detailed variants are unassessed.",
    "states": [
      {
        "name": "complete-hidden",
        "coverage": "covered",
        "storyIds": ["home-setup-card--complete-hidden"],
        "note": "With install and push already done the card renders no notice."
      },
      {
        "name": "dismissed-hidden",
        "coverage": "covered",
        "storyIds": ["home-setup-card--dismissed-hidden"],
        "note": "The device-local dismissal key hides the card; the story stubs only that localStorage key."
      },
      {
        "name": "install-prompt",
        "coverage": "covered",
        "storyIds": ["home-setup-card--install-prompt"],
        "note": "A synthetic beforeinstallprompt leads to a real Install button; the play presses it and asserts the control is withdrawn while the prompt is awaited and the installed hint appears after acceptance."
      },
      {
        "name": "install-instructions",
        "coverage": "covered",
        "storyIds": ["home-setup-card--install-instructions"],
        "note": "Without a browser prompt the owner shows the How to install link to Settings."
      },
      {
        "name": "installed-on-device",
        "coverage": "covered",
        "storyIds": ["home-setup-card--installed-on-device"],
        "note": "A related-app probe result marks the install step done."
      },
      {
        "name": "push-enabled",
        "coverage": "covered",
        "storyIds": ["home-setup-card--push-enabled"],
        "note": "An existing subscription marks notifications on for this device."
      },
      {
        "name": "push-denied",
        "coverage": "covered",
        "storyIds": ["home-setup-card--push-denied"],
        "note": "A denied permission shows the system-settings guidance and blocks the enable button."
      },
      {
        "name": "needs-install-first",
        "coverage": "covered",
        "storyIds": ["home-setup-card--needs-install-first"],
        "note": "On a phone browser the notification step says to install the app first."
      },
      {
        "name": "push-unavailable",
        "coverage": "covered",
        "storyIds": ["home-setup-card--push-unavailable"],
        "note": "Without service worker or push support the notification step is absent."
      },
      {
        "name": "push-enable-busy",
        "coverage": "covered",
        "storyIds": ["home-setup-card--push-enable-busy"],
        "note": "A held public-key read keeps the real Enable button disabled; the owner keeps its label rather than showing a spinner."
      },
      {
        "name": "push-enable-error",
        "coverage": "covered",
        "storyIds": ["home-setup-card--push-enable-error"],
        "note": "A synthetic refused subscribe response surfaces the owner's error region without claiming notifications are on."
      },
      {
        "name": "phone-step",
        "coverage": "covered",
        "storyIds": ["home-setup-card--phone-step"],
        "note": "A touch, non-standalone browser shows the two ordered device steps."
      },
      {
        "name": "dismiss-action",
        "coverage": "covered",
        "storyIds": ["home-setup-card--dismiss-action"],
        "note": "The dismiss control writes only the story-local storage stub and hides the card."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["home-setup-card--narrow"],
        "note": "The card and its step controls stay usable inside a 390px wrapper. The workshop runner has a fine pointer, so the production 44px minimum under `(pointer: coarse)` is not asserted here."
      },
      {
        "name": "in-app-desktop-branch",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The app hides this card (#3197); desktop-client detection cannot be exercised in the browser workshop without faking the native bridge."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "HomeSetupCard starts from browser capability probes, then awaits installed-app probing, push support and subscription reads before showing the push step; a pending probe leaves that step unasserted rather than showing a spinner. The in-app branch is not reachable here."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-today-stats",
    "name": "HomeTodayStats",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeTodayStats.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeReviewSummary.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["connected-home-today-stats--default"],
        "note": "Real owner, project store, memory RouterLink and stats normalization; synthetic GET fixture, no bootstrap."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["connected-home-today-stats--loading"],
        "note": "Held initial stats read: six dashes, no averages. The owner has no loading announcement or skeleton."
      },
      {
        "name": "failed",
        "coverage": "covered",
        "storyIds": ["connected-home-today-stats--failed"],
        "note": "Synthetic HTTP 503: six dashes, no averages. The owner catches the error and renders no distinct error message."
      },
      {
        "name": "partial",
        "coverage": "covered",
        "storyIds": ["connected-home-today-stats--partial"],
        "note": "Real normalization of valid zeroes, missing and nonnumeric fields; only valid averages render."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "HomeTodayStats clears values before each read: initial and workspace refresh show dashes. The held initial read is covered; loading and failure are visually identical. Other refresh/race states are not claimed."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-home-welcome-card",
    "name": "HomeWelcomeCard",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HomeWelcomeCard.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Visible/on-host/revealed/error; no real passwords in fixtures/metadata. Detailed variants are unassessed.",
    "states": [
      {
        "name": "hidden",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--hidden"],
        "note": "A not-pending welcome status renders no card; the play asserts the empty canvas after the fixture read."
      },
      {
        "name": "on-host",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--on-host"],
        "note": "The loopback page renders the host-only card with a synthetic password from the fixture."
      },
      {
        "name": "remote",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--remote"],
        "note": "A non-host welcome status has no host-only affordance; the synthetic fixture sets on_host false and the play asserts the different copy."
      },
      {
        "name": "copied",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--copied"],
        "note": "The copy command writes the synthetic password to the clipboard stub and the play asserts the copied label."
      },
      {
        "name": "password-busy",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--password-busy"],
        "note": "A held /api/welcome/password read keeps the Show button actually disabled; the play asserts the block and the absent error."
      },
      {
        "name": "password-error",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--password-error"],
        "note": "A refused /api/welcome/password read shows the owner's fixed host-down wording and no password."
      },
      {
        "name": "folder-busy",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--folder-busy"],
        "note": "A held /api/welcome/open-folder call keeps the button disabled; the play asserts the actual block."
      },
      {
        "name": "folder-error",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--folder-error"],
        "note": "A failing folder request shows an error and re-enables the button."
      },
      {
        "name": "dismissed",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--dismissed"],
        "note": "The dismiss action posts once and removes the card without touching other storage."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["home-welcome-card--narrow"],
        "note": "The card's password row and controls stay inside a 390px wrapper. Production raises the buttons to 44px only under `(pointer: coarse)`, which the browser runner does not emulate."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "HomeWelcomeCard shows no card while the initial /api/welcome read is pending and none when it fails or reports not pending. The password and open-folder calls each have their own pending/error branch: both buttons are actually disabled while held; a failed password read replaces the transport wording with fixed host-down guidance."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-host-terminal-button",
    "name": "HostTerminalButton",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HostTerminalButton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HousekeepingStrip.vue",
      "web/src/components/LoginView.vue",
      "web/src/components/ProviderSignInHint.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue",
      "web/src/components/settings/GoogleConnectDialog.vue"
    ],
    "variants": [],
    "variantNotes": "Host-only/done/error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in HostTerminalButton.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HostTerminalButton.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-housekeeping-strip",
    "name": "HousekeepingStrip",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HousekeepingStrip.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Notices/unavailable, row/menu primary action kinds and update-task rows, not a generic alert clone. Detailed variants are unassessed.",
    "states": [
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--empty"],
        "note": "Three successful empty reads render no 'Needs you' section and no all-clear row."
      },
      {
        "name": "initial-loading",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--initial-loading"],
        "note": "One held source keeps the three-source load gate pending; the play asserts no rows are drawn during it."
      },
      {
        "name": "health-error",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--health-error"],
        "note": "A synthetic error check renders a health row with an error tone and its detail."
      },
      {
        "name": "health-fix-busy",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--health-fix-busy"],
        "note": "A held fix request keeps the Fix button disabled and relabelled 'Fixing…'."
      },
      {
        "name": "action-run-busy",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--action-run-busy"],
        "note": "A held run request keeps the lead button disabled and relabelled 'Running…'."
      },
      {
        "name": "action-dismiss",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--action-dismiss"],
        "note": "The dismiss action posts once and its fresh empty list removes the row."
      },
      {
        "name": "action-terminal",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--action-terminal"],
        "note": "A runnable terminal action leads the row; the play launches it and asserts the 'Opened in Terminal' status."
      },
      {
        "name": "task-offered",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--task-offered"],
        "note": "An applicable offered task renders its reason and a Start in chat button."
      },
      {
        "name": "task-preview",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The store fetches /api/update-tasks/{id}/preview, but HousekeepingStrip.vue's template never renders taskPreviews, so the fetched summary has no DOM to assert in isolation."
      },
      {
        "name": "task-start-busy",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--task-start-busy"],
        "note": "A held start request keeps the lead button disabled and relabelled 'Working…'."
      },
      {
        "name": "task-in-progress",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--task-in-progress"],
        "note": "An in-progress task with a live chat leads with Resume chat and says the chat is open."
      },
      {
        "name": "task-waiting-review",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--task-needs-review"],
        "note": "A waiting-review task leads with Review proposals and names the archived-chat case."
      },
      {
        "name": "task-unknown-applicability",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--task-unknown-applicability"],
        "note": "An unknown detector answer says it is checking and offers Check again, never a start."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["housekeeping-strip--narrow"],
        "note": "A row with a lead button stays usable inside a 390px wrapper."
      },
      {
        "name": "global-router-navigation",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "View/chat/review handlers import the app's global router instead of the connected one, so those destinations cannot be exercised inside the fixture workshop without production edits."
      },
      {
        "name": "notice-reveal",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The ?notice= reveal path depends on the app's notification inbox and query-string orchestration, which this story host deliberately does not bootstrap."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "HousekeepingStrip draws nothing until the first reads of actions, update tasks and workspace health have each settled or the 3000ms gate fires; an empty list is silent rather than an all-clear. A failed source leaves the strip silent too. Per-row busy states disable the pressed control (Fix, Run, terminal launch, task start/dismiss) and preview loads independently."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-html-artifact-viewer",
    "name": "HtmlArtifactViewer",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/HtmlArtifactViewer.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/FileViewerModal.vue",
      "web/src/components/PinnedFilePanel.vue"
    ],
    "variants": [
      "default",
      "code",
      "code-loading",
      "code-error",
      "narrow"
    ],
    "variantNotes": "Preview frame (sandbox allow-scripts frame that navigates to /api/workspace-html by plain iframe navigation) with the Preview/Code toggle; code view renders the parent-loaded source one line per element, or the parent's Loading…/error notes. No live iframe sources in the catalogue.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["html-artifact-viewer--default"],
        "note": "Preview view: the toolbar (aria-pressed on both tabs) and the sandboxed frame — sandbox=\"allow-scripts\", referrerpolicy=\"no-referrer\", lazy load and the file-path title. The iframe's own navigation is not fenced."
      },
      {
        "name": "code",
        "coverage": "covered",
        "storyIds": ["html-artifact-viewer--code"],
        "note": "Code view: the source renders one .hav-code-line per line inside the pre; toggling back to Preview unmounts the code and remounts the frame (the controlled view prop flips through the parent wrapper)."
      },
      {
        "name": "code-loading",
        "coverage": "covered",
        "storyIds": ["html-artifact-viewer--code-loading"],
        "note": "Code view while the parent's source fetch is in flight renders the 'Loading source…' note and neither frame nor code."
      },
      {
        "name": "code-error",
        "coverage": "covered",
        "storyIds": ["html-artifact-viewer--code-error"],
        "note": "A parent-owned source failure renders the .hav-note-error note in place of the code, never in place of the frame."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["html-artifact-viewer--narrow"],
        "note": "A 390px pane: the toolbar and the sandboxed frame stay inside the narrow wrapper."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "HtmlArtifactViewer performs no transport itself: the preview frame navigates natively to /api/workspace-html and the source fetch belongs to the parent, which passes sourceLoading/sourceError in as props. There is no own initial/refresh/failure/stale state; the two note branches render the parent's loading and failure."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-import-runs",
    "name": "ImportRuns",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ImportRuns.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ImportSources.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/failure/stale/queued/running/empty/filtered/progress/error/history. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ImportRuns.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportRuns.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportRuns.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportRuns.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportRuns.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "ImportRuns.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-import-sources",
    "name": "ImportSources",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ImportSources.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Initial/stale/empty/candidates/excluded/unsupported/selection/preview/file errors. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ImportSources.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportSources.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportSources.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportSources.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ImportSources.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "ImportSources.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-in-app-toast",
    "name": "InAppToast",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/InAppToast.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Actual toast stack, error/action/link/hidden-under-modal states; no live notifications in catalogue. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in InAppToast.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in InAppToast.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-injected-prompt-card",
    "name": "InjectedPromptCard",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/InjectedPromptCard.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Compaction/background_run/cli_task_wake/schedule branches, closed/open summary and run errors/log/tail. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in InjectedPromptCard.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in InjectedPromptCard.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-item-header-menu",
    "name": "ItemHeaderMenu",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ItemHeaderMenu.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SchedulePanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ItemHeaderMenu.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ItemHeaderMenu.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-item-peek-host",
    "name": "ItemPeekHost",
    "family": "Tasks, projects and automations",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/ItemPeekHost.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Explicit overlay host composition over TaskWorkspaceSurface, not a second task editor. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ItemPeekHost.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ItemPeekHost.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-item-title-row",
    "name": "ItemTitleRow",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ItemTitleRow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": [
      "rail",
      "close-title",
      "phone-narrow"
    ],
    "variantNotes": "`rail: true` takes the header's width for the rail, `closeTitle` overrides the tooltip while `aria-label` keeps `closeLabel`, and under a 767px viewport the close button hides so the title keeps the room.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["item-title-row--default"],
        "note": "The close button in its shared .btn-icon box plus the slotted plain title; the close tooltip falls back to closeLabel (title attribute, aria-hidden glyph) and a click emits close."
      },
      {
        "name": "close-title",
        "coverage": "covered",
        "storyIds": ["item-title-row--close-title"],
        "note": "closeTitle overrides the root's tooltip while aria-label keeps the accessible name (SchedulePanel: name Close automation, tooltip Close)."
      },
      {
        "name": "rail",
        "coverage": "covered",
        "storyIds": ["item-title-row--rail"],
        "note": "rail: true adds item-title-row--rail so the row takes the header's room; ChatPanel, ProjectView and SchedulePanel pass :rail when the title rail is on."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["item-title-row--keyboard-focus"],
        "note": "Tab reaches the close button, :focus-visible draws the 2px accent outline, and Enter emits close."
      },
      {
        "name": "phone-narrow",
        "coverage": "covered",
        "storyIds": ["item-title-row--phone-narrow"],
        "note": "The @media (max-width: 767px) rule hides .close-btn so the title keeps the room on a phone; the story pins the workshop's narrow 390px viewport (the media query follows the viewport, not the story wrapper)."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The title row has no loading states: close and slot content render as given. While the engine names a chat the pending shimmer is the host's own branch outside this row (ChatPanel renders it when no rail is shown) and each host draws its own first-load skeleton; the row has no failure, empty or stale state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-list-row",
    "name": "ListRow",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ListRow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/SchedulePanel.vue"
    ],
    "variants": [
      "link",
      "heading-slot",
      "sub-slot",
      "detail-slot",
      "end-slot",
      "disabled"
    ],
    "variantNotes": "A `to` route renders the link branch; without it the row is a button the host wires up. The heading slot replaces the plain title (host spans carry list-row-title), sub is the muted sub-line, detail holds any clamped lines under it and end the right column (list-row-when for the muted mono); all of the host's classes, handlers and attributes land on the root.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["list-row--default"],
        "note": "The v-else button branch with the plain text title over the shared hairline row; the host's handlers land on the root (HomeRecentChats passes @click, title and disabled), so the story proves a click reaches it."
      },
      {
        "name": "link-row",
        "coverage": "covered",
        "storyIds": ["list-row--link-row"],
        "note": "The v-if `to` branch in a fresh memory router: an anchor with the row's href whose click opens the route and lands router-link-exact-active (SchedulePanel's overview renders its link rows this way)."
      },
      {
        "name": "heading-slot",
        "coverage": "covered",
        "storyIds": ["list-row--heading-slot"],
        "note": "The heading slot replaces the plain title; a host title span must carry list-row-title to share the title style (:slotted rule, SchedulePanel's ov-title)."
      },
      {
        "name": "sub-slot",
        "coverage": "covered",
        "storyIds": ["list-row--sub-slot"],
        "note": "The v-if $slots.sub branch: one muted sub-line under the title."
      },
      {
        "name": "detail-slot",
        "coverage": "covered",
        "storyIds": ["list-row--detail-slot"],
        "note": "The detail slot under the sub-line (a pending question, a reply snippet); the slot renders raw, with no wrapper of the row's own, so hosts clamp it themselves."
      },
      {
        "name": "end-slot",
        "coverage": "covered",
        "storyIds": ["list-row--end-slot"],
        "note": "The v-if $slots.end branch: the right-hand end column, whose time or state span carries list-row-when for the muted mono (:slotted rule)."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": ["list-row--disabled"],
        "note": "The host's :disabled lands on the root button (HomeRecentChats for chats that are not in this browser) and keeps it actually disabled, while a host title stays the root's tooltip beside the visible text label."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["list-row--keyboard-focus"],
        "note": "Tab reaches the row and :focus-visible draws the global 2px accent outline; roaming the list with arrows is the host's roamFocus (lib/listRoaming.ts), not the row's, and is not claimed here."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "ListRow has no loading states of its own: it renders loaded rows only. List loading belongs to the hosts — Home's lanes draw the shared SkeletonLoader while their first load is in flight (a generic row pair, not ListRow's title-over-sub-line geometry, and a candidate for the loading follow-up), while the Automations overview keeps its loaded rows; the row itself has no failure, empty or stale state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-login-view",
    "name": "LoginView",
    "family": "Root, navigation and common presentation",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/LoginView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/router.ts"
    ],
    "variants": [],
    "variantNotes": "Login/bootstrap/native-bootstrap/restarting/provider diagnostics; connected composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in LoginView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in LoginView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-memory-categories-panel",
    "name": "MemoryCategoriesPanel",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/MemoryCategoriesPanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Initial loading/failure/stale/empty/editor/validation/action error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in MemoryCategoriesPanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "MemoryCategoriesPanel.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "MemoryCategoriesPanel.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "MemoryCategoriesPanel.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "MemoryCategoriesPanel.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "MemoryCategoriesPanel.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-memory-guide-budget",
    "name": "MemoryGuideBudget",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/MemoryGuideBudget.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/error/over-cap/stats. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in MemoryGuideBudget.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in MemoryGuideBudget.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-memory-how-it-works",
    "name": "MemoryHowItWorks",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/MemoryHowItWorks.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Native disclosure/static tag table (not dynamic network content). Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in MemoryHowItWorks.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in MemoryHowItWorks.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-memory-map-view",
    "name": "MemoryMapView",
    "family": "Memory and usage",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/MemoryMapView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Connected sections/graph/list/selection/review/import/categories/load/error/stale composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in MemoryMapView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in MemoryMapView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-model-selector",
    "name": "ModelSelector",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ModelSelector.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/HomeIntake.vue",
      "web/src/components/NewScheduleForm.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue"
    ],
    "variants": [
      "search",
      "provider",
      "selected",
      "default",
      "badge",
      "empty",
      "triggerless sheet"
    ],
    "variantNotes": "Real searchable provider/model popover and triggerless sheet, selected/default/badge/empty/header/footer variants; not a generic Select clone.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ModelSelector.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "search",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a search branch; no rendered-state story exists."
      },
      {
        "name": "provider",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a provider branch; no rendered-state story exists."
      },
      {
        "name": "selected",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a selected branch; no rendered-state story exists."
      },
      {
        "name": "badge",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a badge branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a empty branch; no rendered-state story exists."
      },
      {
        "name": "triggerless sheet",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ModelSelector.vue has a triggerless sheet branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ModelSelector.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-card",
    "name": "NeedsYouCard",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NeedsYouCard.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/NeedsYouStack.vue"
    ],
    "variants": [],
    "variantNotes": "Per-kind registered body, busy/failed/done/dismiss/error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NeedsYouCard.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NeedsYouCard.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-row",
    "name": "NeedsYouRow",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NeedsYouRow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HousekeepingStrip.vue"
    ],
    "variants": [],
    "variantNotes": "Error/blocking tone, open menu, href vs action. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NeedsYouRow.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NeedsYouRow.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-stack",
    "name": "NeedsYouStack",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NeedsYouStack.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Actions/folded/error composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NeedsYouStack.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NeedsYouStack.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-new-chat-picker",
    "name": "NewChatPicker",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NewChatPicker.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Real shared project picker dialog, selected and badge states. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NewChatPicker.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NewChatPicker.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-new-schedule-form",
    "name": "NewScheduleForm",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NewScheduleForm.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SchedulePanel.vue"
    ],
    "variants": [
      "interval",
      "once",
      "weekly",
      "monthly",
      "time",
      "model",
      "archive"
    ],
    "variantNotes": "Interval/once/weekly/monthly/time/model/archive options.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NewScheduleForm.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "interval",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a interval branch; no rendered-state story exists."
      },
      {
        "name": "once",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a once branch; no rendered-state story exists."
      },
      {
        "name": "weekly",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a weekly branch; no rendered-state story exists."
      },
      {
        "name": "monthly",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a monthly branch; no rendered-state story exists."
      },
      {
        "name": "time",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a time branch; no rendered-state story exists."
      },
      {
        "name": "model",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a model branch; no rendered-state story exists."
      },
      {
        "name": "archive",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NewScheduleForm.vue has a archive branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NewScheduleForm.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-notification-bell",
    "name": "NotificationBell",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NotificationBell.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatHeaderMoreMenu.vue",
      "web/src/components/ItemHeaderMenu.vue",
      "web/src/components/PaneHeader.vue",
      "web/src/components/ProjectSidebar.vue"
    ],
    "variants": [
      "popover",
      "sheet",
      "count",
      "partial"
    ],
    "variantNotes": "Popover/sheet/trigger/count/partial.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NotificationBell.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "popover",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationBell.vue has a popover branch; no rendered-state story exists."
      },
      {
        "name": "sheet",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationBell.vue has a sheet branch; no rendered-state story exists."
      },
      {
        "name": "count",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationBell.vue has a count branch; no rendered-state story exists."
      },
      {
        "name": "partial",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationBell.vue has a partial branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NotificationBell.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-notification-inbox-list",
    "name": "NotificationInboxList",
    "family": "Home and notices",
    "kind": "component",
    "owner": {
      "path": "web/src/components/NotificationInboxList.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/NotificationBell.vue"
    ],
    "variants": [
      "checking",
      "ready",
      "stale",
      "unavailable",
      "all-clear"
    ],
    "variantNotes": "Checking/ready/stale/unavailable/all-clear; freshness per source, unread and timestamps.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in NotificationInboxList.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "checking",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationInboxList.vue implements the checking branch; no rendered-state story exists."
      },
      {
        "name": "ready",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationInboxList.vue implements the ready branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationInboxList.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "unavailable",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationInboxList.vue implements the unavailable branch; no rendered-state story exists."
      },
      {
        "name": "all-clear",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "NotificationInboxList.vue implements the all-clear branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in NotificationInboxList.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-pane-header",
    "name": "PaneHeader",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/PaneHeader.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/PinnedFilePanel.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/UsageView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in PaneHeader.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in PaneHeader.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-pinned-file-panel",
    "name": "PinnedFilePanel",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/PinnedFilePanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/lib/chatComponents.ts"
    ],
    "variants": [
      "loading",
      "error",
      "editing",
      "return-folder"
    ],
    "variantNotes": "Docked file owner; kind/loading/error/editing/return-folder/lead-after-slot branches.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in PinnedFilePanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "PinnedFilePanel.vue has a loading branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "PinnedFilePanel.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "PinnedFilePanel.vue has a editing branch; no rendered-state story exists."
      },
      {
        "name": "return-folder",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "PinnedFilePanel.vue has a return-folder branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in PinnedFilePanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-project-sidebar",
    "name": "ProjectSidebar",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ProjectSidebar.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Expanded/collapsed, workspace/chat/automation/memory navigation; loading delegates SidebarListSkeleton. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ProjectSidebar.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ProjectSidebar.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-project-view",
    "name": "ProjectView",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ProjectView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Project/header/context/repository/close-sheet owner; loading uses SkeletonLoader. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ProjectView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ProjectView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-prompt-dialog",
    "name": "PromptDialog",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/PromptDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Production askPrompt request after host mount. Empty/whitespace disable submission; no whole-dialog disabled API exists. One host per canvas; no inline Docs. Further configurable variants are unassessed.",
    "states": [
      { "name": "empty", "coverage": "covered", "storyIds": ["prompt-dialog--empty"], "note": "Empty input, placeholder and disabled Create action." },
      { "name": "whitespace", "coverage": "covered", "storyIds": ["prompt-dialog--whitespace"], "note": "Whitespace-only request value cannot submit." },
      { "name": "prefilled", "coverage": "covered", "storyIds": ["prompt-dialog--prefilled"], "note": "Rename request seeds and selects an existing name." },
      { "name": "populated", "coverage": "covered", "storyIds": ["prompt-dialog--populated"], "note": "Nonblank text enables submission; returned text is trimmed." },
      { "name": "disabled", "coverage": "covered", "storyIds": ["prompt-dialog--disabled"], "note": "Actual validation-disabled submit action with whitespace; input and Cancel remain enabled." },
      { "name": "narrow", "coverage": "covered", "storyIds": ["prompt-dialog--narrow"], "note": "Prefilled request in a maximum 390px, 640px-high containing block; also check a real narrow viewport." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in PromptDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-proposal-history-list",
    "name": "ProposalHistoryList",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ProposalHistoryList.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ProposalReviewPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/failure/stale/filter-empty/empty/load-more/reversible history. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ProposalHistoryList.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalHistoryList.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalHistoryList.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalHistoryList.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "filtered-empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalHistoryList.vue implements the filtered-empty branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalHistoryList.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "ProposalHistoryList.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-proposal-review-panel",
    "name": "ProposalReviewPanel",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ProposalReviewPanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/initial-failure/stale/filter-empty/true-empty/selection/batch/error/history. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ProposalReviewPanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalReviewPanel.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalReviewPanel.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalReviewPanel.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "filtered-empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalReviewPanel.vue implements the filtered-empty branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "ProposalReviewPanel.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "ProposalReviewPanel.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-provider-sign-in-hint",
    "name": "ProviderSignInHint",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ProviderSignInHint.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRetryState.vue"
    ],
    "variants": [],
    "variantNotes": "Loading/load-error/connection/terminal action. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in ProviderSignInHint.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ProviderSignInHint.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-repo-picker-dialog",
    "name": "RepoPickerDialog",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/RepoPickerDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ProjectView.vue"
    ],
    "variants": [],
    "variantNotes": "Listing/loading/error/save error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in RepoPickerDialog.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in RepoPickerDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-restart-notice",
    "name": "RestartNotice",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/RestartNotice.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in RestartNotice.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in RestartNotice.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-schedule-glyph",
    "name": "ScheduleGlyph",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/ScheduleGlyph.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatSignals.vue"
    ],
    "variants": [],
    "variantNotes": "No configurable variants: one decorative 12px SVG scaled by --font-scale; the caller owns the accessible name.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["schedule-glyph--default"],
        "note": "Default renders the clock-face mark beside its row text; it is decorative and never focusable."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static SVG mark with no fetch and no asynchronous state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-schedule-panel",
    "name": "SchedulePanel",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/SchedulePanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Schedule/list/new/detail/system/user/edit/initial-failure/stale branches. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SchedulePanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SchedulePanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-view",
    "name": "SettingsView",
    "family": "Settings",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/SettingsView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Connected tab/card/provider/update composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-sidebar-list-skeleton",
    "name": "SidebarListSkeleton",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/SidebarListSkeleton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ProjectSidebar.vue"
    ],
    "variants": [
      "groups",
      "rows",
      "nested"
    ],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-list-skeleton--loading"],
        "note": "Actual SidebarListSkeleton: Loading automations, one group of three placeholder rows and one status announcement."
      },
      {
        "name": "nested",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-list-skeleton--nested"],
        "note": "Actual SidebarListSkeleton: Loading chats, two groups of two nested placeholder rows. Does not separately cover SidebarRow placeholder mode."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "SidebarListSkeleton.vue renders groups, rows and nested SidebarRow placeholders."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-sidebar-row",
    "name": "SidebarRow",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/SidebarRow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/SidebarListSkeleton.vue"
    ],
    "variants": [
      "link",
      "placeholder",
      "pending",
      "meta"
    ],
    "variantNotes": "A `to` route renders a RouterLink with active-class; without it the row is a plain element. pending swaps the title for the naming shimmer, meta is the muted mono badge, and placeholder is the loading stand-in bar.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--default"],
        "note": "Actual plain-div SidebarRow with Weekly review text and 09:00 meta; no link, host controls or click/focus behavior claimed."
      },
      {
        "name": "active",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--active"],
        "note": "Plain-div row with active: true."
      },
      {
        "name": "unread",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--unread"],
        "note": "Plain-div row with unread: true; no host-added unread dot."
      },
      {
        "name": "muted",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--muted"],
        "note": "Plain-div row with muted: true; combinations with unread are not covered."
      },
      {
        "name": "pending",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--pending"],
        "note": "Plain-div row with pending: true; production naming shimmer and Naming conversation screen-reader text."
      },
      {
        "name": "long-content",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--long-content"],
        "note": "Long title and 09:00 meta in a 240px wrapper; production ellipsis, not a navigation or keyboard story."
      },
      {
        "name": "link",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--link", "foundation-sidebar-row--link-active"],
        "note": "The v-if `to` branch renders a RouterLink with active-class and the same title, shimmer and meta marks. The link stories assert href and the meta mark, and the row's active classes on the linked route (every production consumer passes `to`; the sidebar's task and settings rows add `active` and aria-current on top)."
      },
      {
        "name": "placeholder",
        "coverage": "covered",
        "storyIds": ["sidebar-row-placeholder--default", "sidebar-row-placeholder--narrow"],
        "note": "The loading branch: a plain sidebar-row--placeholder box, aria-hidden, not focusable, with one shimmer bar at the given width and no title or meta. SidebarListSkeleton draws lists of these on a first load; the narrow story keeps it inside a 390px block."
      },
      {
        "name": "host-control",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--host-control"],
        "note": "The v-else branch the host wires up: a plain element carrying the host's role, tabindex, aria-pressed and click handler on the root. No current production consumer uses this branch (ProjectSidebar and SidebarListSkeleton all pass `to` or `placeholder`), so the story claims the branch's own contract, not a host screen."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["foundation-sidebar-row--keyboard-focus"],
        "note": ":focus-visible draws the row's 2px accent outline. The keyboard story tabs to a link row (naturally in the tab order), asserts focus-visible plus the outline and that Enter opens the row's route; the plain branch is focusable only when the host adds tabindex, which the host-control story shows."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The placeholder branch is the row's own first-load shape, drawn in groups by SidebarListSkeleton while a sidebar list is loading; pending (Naming conversation) is a naming state, not loading. Initial load is the placeholder bar, loaded rows replace it, refresh over rows already on screen keeps them (each list's own load-state rule), and the row itself has no failure, empty or stale state. The placeholder bar is a 10px line, not the loaded title and meta geometry."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-skeleton-loader",
    "name": "SkeletonLoader",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/SkeletonLoader.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/ProposalHistoryList.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/UsageView.vue",
      "web/src/components/VaultReviewPanel.vue",
      "web/src/components/WebhookTriggers.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "variants": [
      "rows",
      "cards",
      "board",
      "graph"
    ],
    "variantNotes": "variant selects the shape (rows is the default), count sets rows and cards (per column for board) and label is the one status name. Board draws four placeholder columns of max(1, count - column % 3) cards; graph draws the Memory Map's node and link sketch.",
    "states": [
      {
        "name": "rows",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--rows"],
        "note": "Default variant with count: bordered row shapes, two lines each, aria-hidden behind one role=status named by label. The story asserts the row count, the two lines per row and no card, column or graph shape."
      },
      {
        "name": "cards",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--cards"],
        "note": "Cards variant: count stacked card shapes with a heading, a full line and a short line each, aria-hidden; no rows render."
      },
      {
        "name": "board",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--board"],
        "note": "Board variant: four named columns, a heading bar and max(1, count - column % 3) cards per column, four equal grid tracks above the 940px container query."
      },
      {
        "name": "board-narrow",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--board-narrow"],
        "note": "The same board inside a 390px chat-pane container: the @container (max-width: 940px) rule collapses it to one track and hides columns two to four."
      },
      {
        "name": "graph",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--graph"],
        "note": "Graph variant: the 200x140 node and link drawing for the Memory Map, eleven nodes and twelve edges, aria-hidden inside the status; no rows or cards render."
      },
      {
        "name": "reduced-motion",
        "coverage": "covered",
        "storyIds": ["skeleton-loader--reduced-motion"],
        "note": "Reduced motion is a CSS media query, not an arg: the same rows render and the play asserts the owner's shipped prefers-reduced-motion rule, which replaces the sweep with the slow skeleton-pulse. The runner does not emulate the media query."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "First-load placeholder only: initial loading is this component's one state, named by label; loaded content replaces it, and empty, filtered empty, initial failure, refresh and refresh failure or stale are each consuming page's own load-state rule, not drawn here (a refresh over data already on screen keeps the data). Rows, cards and graph are generic placeholder shapes rather than the consumed rows, cards or nodes, so the placeholder geometry does not match the loaded geometry; board draws four placeholder columns and collapses to one in the named chat-pane container at 940px, and parity with the 310px board lanes is not established."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-slash-command-picker",
    "name": "SlashCommandPicker",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/SlashCommandPicker.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/HomeIntake.vue"
    ],
    "variants": [],
    "variantNotes": "Skill/command, active row, argument hint/description; slashCommands.css. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SlashCommandPicker.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SlashCommandPicker.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-startup-view",
    "name": "StartupView",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/StartupView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in StartupView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in StartupView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-tab-bar",
    "name": "TabBar",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TabBar.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
    ],
    "variants": [
      "selected",
      "count"
    ],
    "variantNotes": "Four genuine covered stories as above; no disabled/loading API.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "foundation-tab-bar--default"
        ],
        "note": "The foundation story renders this state; interaction behavior is not automatically verified."
      },
      {
        "name": "counts",
        "coverage": "covered",
        "storyIds": [
          "foundation-tab-bar--counts"
        ],
        "note": "The foundation story renders this state; interaction behavior is not automatically verified."
      },
      {
        "name": "long-content",
        "coverage": "covered",
        "storyIds": [
          "foundation-tab-bar--long-labels"
        ],
        "note": "The foundation story renders this state; interaction behavior is not automatically verified."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "foundation-tab-bar--narrow-pane"
        ],
        "note": "The foundation story renders this state; interaction behavior is not automatically verified."
      },
      {
        "name": "disabled",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "TabBar.vue defines no disabled prop."
      },
      {
        "name": "loading",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "TabBar.vue defines no loading prop."
      },
      {
        "name": "keyboard-focus",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Keyboard behavior has no separate rendered-state story."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "TabBar.vue has no loading prop or loading branch."
    },
    "migration": {
      "status": "retained",
      "note": "The foundation workshop uses the shared production TabBar."
    }
  },
  {
    "id": "components-task-board-view",
    "name": "TaskBoardView",
    "family": "Tasks, projects and automations",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/TaskBoardView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "Explicit wrapper composition over TaskWorkspaceSurface; does not own cards/editor. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TaskBoardView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskBoardView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-task-glyph",
    "name": "TaskGlyph",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TaskGlyph.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/ProjectSidebar.vue"
    ],
    "variants": [],
    "variantNotes": "No configurable variants: one decorative 12px SVG scaled by --font-scale; the caller owns the accessible name.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-glyph--default"],
        "note": "Default renders the ticked-box mark beside its row text; it is decorative and never focusable."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static SVG mark with no fetch and no asynchronous state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-task-handover-card",
    "name": "TaskHandoverCard",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TaskHandoverCard.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Delegation title/description read surface. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TaskHandoverCard.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskHandoverCard.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-task-note-feed",
    "name": "TaskNoteFeed",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TaskNoteFeed.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": [],
    "variantNotes": "Author/edited/editing/newest-first feed. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TaskNoteFeed.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskNoteFeed.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-task-origin-note",
    "name": "TaskOriginNote",
    "family": "Chat, messages, composer, activity and context",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TaskOriginNote.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue"
    ],
    "variants": [],
    "variantNotes": "Delegated vs manually linked task rows; note/approve/mark-done/action-error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TaskOriginNote.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskOriginNote.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-task-workspace-surface",
    "name": "TaskWorkspaceSurface",
    "family": "Tasks, projects and automations",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ItemPeekHost.vue",
      "web/src/components/TaskBoardView.vue"
    ],
    "variants": [
      "page",
      "overlay",
      "board",
      "editor"
    ],
    "variantNotes": "Actual inline board/lanes/card, editor, completion, delegation and move/ordering owner. Known firstLoad/loadFailed/loadStale/filteredEmpty/boardEmpty/missingTaskId/orderNotice, page vs overlay, done lane and editor status branches. These are uncovered, not represented by InventoryBrowser cards.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TaskWorkspaceSurface.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "firstLoad",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the firstLoad branch; no rendered-state story exists."
      },
      {
        "name": "loadFailed",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the loadFailed branch; no rendered-state story exists."
      },
      {
        "name": "loadStale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the loadStale branch; no rendered-state story exists."
      },
      {
        "name": "filteredEmpty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the filteredEmpty branch; no rendered-state story exists."
      },
      {
        "name": "boardEmpty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the boardEmpty branch; no rendered-state story exists."
      },
      {
        "name": "missingTaskId",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the missingTaskId branch; no rendered-state story exists."
      },
      {
        "name": "orderNotice",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the orderNotice branch; no rendered-state story exists."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the editing branch; no rendered-state story exists."
      },
      {
        "name": "completion",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the completion branch; no rendered-state story exists."
      },
      {
        "name": "delegation",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the delegation branch; no rendered-state story exists."
      },
      {
        "name": "ordering",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the ordering branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "TaskWorkspaceSurface.vue firstLoad/loadFailed/loadStale distinguish initial loading, failure and stale refresh; stale rows are retained."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-terminal-key-row",
    "name": "TerminalKeyRow",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TerminalKeyRow.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/TerminalPane.vue"
    ],
    "variants": [],
    "variantNotes": "Real coarse-pointer key controls and disabled-until-live state. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TerminalKeyRow.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TerminalKeyRow.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-terminal-pane",
    "name": "TerminalPane",
    "family": "Files, terminal and dialogs",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TerminalPane.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [
      "tile",
      "drawer",
      "chooser",
      "connecting",
      "reconnecting",
      "takeover",
      "ended",
      "creating",
      "error"
    ],
    "variantNotes": "Tile/drawer/chooser/connecting/reconnecting/takeover/ended/creating/truncated/error; no live socket fixtures here.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in TerminalPane.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "tile",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a tile branch; no rendered-state story exists."
      },
      {
        "name": "drawer",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a drawer branch; no rendered-state story exists."
      },
      {
        "name": "chooser",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a chooser branch; no rendered-state story exists."
      },
      {
        "name": "connecting",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a connecting branch; no rendered-state story exists."
      },
      {
        "name": "reconnecting",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a reconnecting branch; no rendered-state story exists."
      },
      {
        "name": "takeover",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a takeover branch; no rendered-state story exists."
      },
      {
        "name": "ended",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a ended branch; no rendered-state story exists."
      },
      {
        "name": "creating",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a creating branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TerminalPane.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TerminalPane.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-title-rail",
    "name": "TitleRail",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/TitleRail.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": [
      "pending-title",
      "segment-marks",
      "busy"
    ],
    "variantNotes": "A titlePending item shows the naming shimmer under its accessible name; each item's mark (needs, working, unread) draws its track segment in miniature; busy refuses the swipe, wheel and arrow-key steps.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["title-rail--default"],
        "note": "The settled rail: role=group named `<group label>, 2 of 3`, the current item with aria-current=page and tabindex 0 over neighbours at -1, title over project, and one highlighted segment on the hidden track. Swipe, wheel and pull gestures are not claimed by stories."
      },
      {
        "name": "pending-title",
        "coverage": "covered",
        "storyIds": ["title-rail--pending-title"],
        "note": "The titlePending branch: the naming shimmer stands in for the title on the pending item (label Naming conversation, <project> passes from the host), while neighbours keep their title text."
      },
      {
        "name": "segment-marks",
        "coverage": "covered",
        "storyIds": ["title-rail--segment-marks"],
        "note": "The track's per-item marks in miniature: needs the alert triangle, working the chase (asserted through its animation), unread the dot."
      },
      {
        "name": "busy",
        "coverage": "covered",
        "storyIds": ["title-rail--busy"],
        "note": "With busy the arrow-key step (and the swipe and wheel) is refused: the asserted window passes with no select landing and aria-current stays put."
      },
      {
        "name": "keyboard-step",
        "coverage": "covered",
        "storyIds": ["title-rail--keyboard-step"],
        "note": "ArrowRight on the focused current item slides to the next item and selects it; the story's host handler moves the current id, so aria-current follows to the neighbour."
      },
      {
        "name": "rename-gesture",
        "coverage": "covered",
        "storyIds": ["title-rail--rename-gesture"],
        "note": "A double click on the current item emits rename (the hosts open the rename field); the story's host handler counts the rename."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["title-rail--keyboard-focus"],
        "note": "Tab reaches the current item only (its neighbours are tabindex -1) and :focus-visible draws the rail's 2px outline on it."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The rail has no loading states: a pending title is the naming-conversation shimmer, not a load, and hosts render the rail only once items exist — each host's own first-load placeholder (the chat's ChatPaneSkeleton and friends) covers that load. No failure, empty or stale geometry exists."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-update-progress-view",
    "name": "UpdateProgressView",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/UpdateProgressView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in UpdateProgressView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in UpdateProgressView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-usage-breakdown",
    "name": "UsageBreakdown",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/UsageBreakdown.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/UsageView.vue"
    ],
    "variants": [],
    "variantNotes": "Grid/project/model variants and empty data. Detailed variants are unassessed.",
    "states": [
      {
        "name": "loaded",
        "coverage": "covered",
        "storyIds": [
          "usage-breakdown--loaded"
        ],
        "note": "Heatmap, projects, pipeline, hours and model bars with synthetic stats props; no fetch."
      },
      {
        "name": "empty-data",
        "coverage": "covered",
        "storyIds": [
          "usage-breakdown--empty-data"
        ],
        "note": "Null stats: activity, projects and models empty captions; pipeline and hours show dashes."
      },
      {
        "name": "heatmap-focus",
        "coverage": "covered",
        "storyIds": [
          "usage-breakdown--heatmap-focus"
        ],
        "note": "The activity scroll region is keyboard-focusable for narrow viewports."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Presentational child: UsageView owns fetch and loading; this owner has no loading UI."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-usage-view",
    "name": "UsageView",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/UsageView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "First-load/error/KPI page composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "initial-loading",
        "coverage": "covered",
        "storyIds": [
          "usage-view--initial-loading"
        ],
        "note": "First load before answered: SkeletonLoader cards only; range picker stays usable."
      },
      {
        "name": "loaded",
        "coverage": "covered",
        "storyIds": [
          "usage-view--loaded"
        ],
        "note": "KPI grid, deltas and UsageBreakdown after a synthetic GET fixture resolves."
      },
      {
        "name": "load-failed",
        "coverage": "covered",
        "storyIds": [
          "usage-view--load-failed"
        ],
        "note": "role=alert error with retry; KPI values show dashes until a successful read."
      },
      {
        "name": "refresh-loading",
        "coverage": "covered",
        "storyIds": [
          "usage-view--refresh-loading"
        ],
        "note": "After the first answer, a new range keeps the grid and shows dashes, not the skeleton."
      },
      {
        "name": "range-focus",
        "coverage": "covered",
        "storyIds": [
          "usage-view--range-focus"
        ],
        "note": "Range toggles are buttons with aria-pressed; focus stays on the selected control."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load uses SkeletonLoader variant cards (count 3) which does not match the loaded eight-tile KPI grid geometry; refresh and workspace changes keep the grid with dashes. Failure shows a distinct alert with retry. Covered: initial-loading, refresh-loading, load-failed, loaded."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-vault-review-panel",
    "name": "VaultReviewPanel",
    "family": "Memory and usage",
    "kind": "component",
    "owner": {
      "path": "web/src/components/VaultReviewPanel.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue"
    ],
    "variants": [],
    "variantNotes": "Retired/review, loading/failure/stale/empty/candidate signal/complete-vs-retire. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in VaultReviewPanel.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "VaultReviewPanel.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "VaultReviewPanel.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "VaultReviewPanel.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "VaultReviewPanel.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "VaultReviewPanel.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-webhook-triggers",
    "name": "WebhookTriggers",
    "family": "Tasks, projects and automations",
    "kind": "component",
    "owner": {
      "path": "web/src/components/WebhookTriggers.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SchedulePanel.vue"
    ],
    "variants": [],
    "variantNotes": "Initial loading/failure/stale/empty/form/write error/history/revealed-secret states; do not include real secrets. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in WebhookTriggers.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WebhookTriggers.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "initial-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WebhookTriggers.vue implements the initial-error branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WebhookTriggers.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "empty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WebhookTriggers.vue implements the empty branch; no rendered-state story exists."
      },
      {
        "name": "action-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WebhookTriggers.vue implements the action-error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "WebhookTriggers.vue distinguishes first loading, initial failure and stale refresh retaining rows."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-workspace-move-dialog",
    "name": "WorkspaceMoveDialog",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/WorkspaceMoveDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Listing/plan/refusals/warnings/error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in WorkspaceMoveDialog.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in WorkspaceMoveDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-workspace-tile",
    "name": "WorkspaceTile",
    "family": "Root, navigation and common presentation",
    "kind": "component",
    "owner": {
      "path": "web/src/components/WorkspaceTile.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": ["name"],
    "variantNotes": "One required workspace name prop; identity reads projectStore.workspaceOptions for sidebar 1-9 keys and workspaceAccent for tint.",
    "states": [
      {
        "name": "shortcut-key",
        "coverage": "covered",
        "storyIds": ["workspace-tile--shortcut-key"],
        "note": "Positions 1-9 render the numeric sidebar shortcut and the workspace preset accent."
      },
      {
        "name": "initial",
        "coverage": "covered",
        "storyIds": ["workspace-tile--initial"],
        "note": "The tenth registry slot falls back to the label initial instead of a numeric key."
      },
      {
        "name": "accent",
        "coverage": "covered",
        "storyIds": ["workspace-tile--accent"],
        "note": "A non-active workspace keeps its own data-workspace-color tint."
      },
      {
        "name": "neutral-accent",
        "coverage": "covered",
        "storyIds": ["workspace-tile--neutral-accent"],
        "note": "An empty registry yields the neutral accent and the name initial before options load."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Synchronous read from a seeded Pinia project store; consumers own registry fetches, not the tile."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-callback-paste-field",
    "name": "CallbackPasteField",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/CallbackPasteField.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/GwsAuthBody.vue",
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in CallbackPasteField.vue implements this render; no rendered-state story exists."
      },
      { "name": "pending", "coverage": "uncovered", "storyIds": [], "note": "CallbackPasteField.vue busy makes the input readonly and disables the submit button; no rendered-state story exists." },
      { "name": "action-error", "coverage": "uncovered", "storyIds": [], "note": "CallbackPasteField.vue v-if=\"error\" renders a role=alert callback error; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in CallbackPasteField.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-command-body",
    "name": "CommandBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/CommandBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["open", "working", "done", "copiedOnce"],
    "variantNotes": "CommandBody.vue renders a command, terminal typing, manual completion and copy fallback; remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in CommandBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "open", "coverage": "uncovered", "storyIds": [], "note": "CommandBody.vue action.state === 'open' renders the terminal instruction; no rendered-state story exists." },
      { "name": "working", "coverage": "uncovered", "storyIds": [], "note": "CommandBody.vue action.state === 'working' switches to I ran it; no rendered-state story exists." },
      { "name": "done", "coverage": "uncovered", "storyIds": [], "note": "CommandBody.vue action.state === 'done' hides command actions; no rendered-state story exists." },
      { "name": "action-error", "coverage": "uncovered", "storyIds": [], "note": "CommandBody.vue v-if=\"error\" renders the error alert; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in CommandBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-device-code-body",
    "name": "DeviceCodeBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/DeviceCodeBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["open", "failed", "waiting", "working", "code", "url"],
    "variantNotes": "DeviceCodeBody.vue branches on action.state, code and URL; terminal support is conditional. Remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in DeviceCodeBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "open / failed", "coverage": "uncovered", "storyIds": [], "note": "DeviceCodeBody.vue open/failed action states render the start button; starting disables it. No rendered-state story exists." },
      { "name": "waiting", "coverage": "uncovered", "storyIds": [], "note": "DeviceCodeBody.vue waiting renders code/copy and a URL when available; working/waiting can show HelperTerminalButton. No rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in DeviceCodeBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-gws-auth-body",
    "name": "GwsAuthBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/GwsAuthBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["open", "failed", "waiting", "onHost"],
    "variantNotes": "GwsAuthBody.vue branches on action.state and onHost for callback paste; remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in GwsAuthBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "open / failed", "coverage": "uncovered", "storyIds": [], "note": "GwsAuthBody.vue open/failed renders start; a failed web-app error links to manual connect. No rendered-state story exists." },
      { "name": "waiting", "coverage": "uncovered", "storyIds": [], "note": "GwsAuthBody.vue waiting renders the URL and, when !onHost, CallbackPasteField; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in GwsAuthBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-helper-terminal-button",
    "name": "HelperTerminalButton",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/HelperTerminalButton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/DeviceCodeBody.vue",
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in HelperTerminalButton.vue implements this render; no rendered-state story exists."
      },
      { "name": "hidden", "coverage": "uncovered", "storyIds": [], "note": "HelperTerminalButton.vue v-if=\"sessionId\" hides the terminal action without a session; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HelperTerminalButton.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-permission-mode-body",
    "name": "PermissionModeBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/PermissionModeBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["done", "isBypass", "note"],
    "variantNotes": "PermissionModeBody.vue shows current/requested mode, optional reason and bypass warning; remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in PermissionModeBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "bypass", "coverage": "uncovered", "storyIds": [], "note": "PermissionModeBody.vue isBypass renders the command/file-edit warning; no rendered-state story exists." },
      { "name": "done", "coverage": "uncovered", "storyIds": [], "note": "PermissionModeBody.vue action.state !== 'done' gates the requested/current modes and controls; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in PermissionModeBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-restart-body",
    "name": "RestartBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/RestartBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["note"],
    "variantNotes": "RestartBody.vue owns the optional agent reason and Settings action; remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in RestartBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "reason", "coverage": "uncovered", "storyIds": [], "note": "RestartBody.vue v-if=\"note\" renders the agent reason alongside the Settings action; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in RestartBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-needs-you-system-setting-body",
    "name": "SystemSettingBody",
    "family": "Needs-you bodies",
    "kind": "component",
    "owner": {
      "path": "web/src/components/needs-you/SystemSettingBody.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/needs-you/registry.ts"
    ],
    "variants": ["showOpen", "showHint", "showRestart"],
    "variantNotes": "SystemSettingBody.vue distinguishes host opening, remote guidance and restart instructions; remaining variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SystemSettingBody.vue implements this render; no rendered-state story exists."
      },
      { "name": "host / remote", "coverage": "uncovered", "storyIds": [], "note": "SystemSettingBody.vue showOpen/showHint choose host action or Mac guidance, and showRestart offers Settings; no rendered-state story exists." },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SystemSettingBody.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-access-stage",
    "name": "OnboardingAccessStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingAccessStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "provider"
    ],
    "variantNotes": "Observed branches in OnboardingAccessStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingAccessStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "provider",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingAccessStage.vue has a provider branch; no rendered-state story exists."
      },
      {
        "name": "bypass-note",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "approval group renders BYPASS_NOTE and the provider docs link"
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingAccessStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-button",
    "name": "OnboardingButton",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingButton.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingFinishStage.vue",
      "web/src/components/onboarding/OnboardingLocationStage.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue",
      "web/src/components/onboarding/OnboardingShell.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingButton.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingButton.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-care-stage",
    "name": "OnboardingCareStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingCareStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "routine",
      "error"
    ],
    "variantNotes": "Observed branches in OnboardingCareStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingCareStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "routine",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingCareStage.vue has a routine branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingCareStage.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingCareStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-choice",
    "name": "OnboardingChoice",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingChoice.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingAccessStage.vue",
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingMemoryStage.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingChoice.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingChoice.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-choice-group",
    "name": "OnboardingChoiceGroup",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingChoiceGroup.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingAccessStage.vue",
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingMemoryStage.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingChoiceGroup.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingChoiceGroup.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-field",
    "name": "OnboardingField",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingField.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingLocationStage.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingField.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingField.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-finish-stage",
    "name": "OnboardingFinishStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingFinishStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "loading",
      "attention",
      "send error",
      "sent"
    ],
    "variantNotes": "Observed branches in OnboardingFinishStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingFinishStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingFinishStage.vue has a loading branch; no rendered-state story exists."
      },
      {
        "name": "attention",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingFinishStage.vue has a attention branch; no rendered-state story exists."
      },
      {
        "name": "send error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingFinishStage.vue has a send error branch; no rendered-state story exists."
      },
      {
        "name": "sent",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingFinishStage.vue has a sent branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingFinishStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-location-stage",
    "name": "OnboardingLocationStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingLocationStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "folder",
      "host",
      "provisioned"
    ],
    "variantNotes": "Observed branches in OnboardingLocationStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingLocationStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "folder",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingLocationStage.vue has a folder branch; no rendered-state story exists."
      },
      {
        "name": "host",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingLocationStage.vue has a host branch; no rendered-state story exists."
      },
      {
        "name": "provisioned",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingLocationStage.vue has a provisioned branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingLocationStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-memory-stage",
    "name": "OnboardingMemoryStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingMemoryStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "pending",
      "error"
    ],
    "variantNotes": "Observed branches in OnboardingMemoryStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingMemoryStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "pending",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingMemoryStage.vue has a pending branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingMemoryStage.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingMemoryStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-runtime-stage",
    "name": "OnboardingRuntimeStage",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingRuntimeStage.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "provider",
      "model",
      "sign-in",
      "loading"
    ],
    "variantNotes": "Observed branches in OnboardingRuntimeStage.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingRuntimeStage.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "provider",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingRuntimeStage.vue has a provider branch; no rendered-state story exists."
      },
      {
        "name": "model",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingRuntimeStage.vue has a model branch; no rendered-state story exists."
      },
      {
        "name": "sign-in",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingRuntimeStage.vue has a sign-in branch; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingRuntimeStage.vue has a loading branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingRuntimeStage.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-shell",
    "name": "OnboardingShell",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingShell.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [
      "conflict",
      "error"
    ],
    "variantNotes": "Observed branches in OnboardingShell.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingShell.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "conflict",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingShell.vue has a conflict branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OnboardingShell.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingShell.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-stack",
    "name": "OnboardingStack",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingStack.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingAccessStage.vue",
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingFinishStage.vue",
      "web/src/components/onboarding/OnboardingLocationStage.vue",
      "web/src/components/onboarding/OnboardingMemoryStage.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue",
      "web/src/components/onboarding/OnboardingSummary.vue",
      "web/src/components/onboarding/OnboardingView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingStack.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingStack.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-step-footer",
    "name": "OnboardingStepFooter",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingStepFooter.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingShell.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingStepFooter.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingStepFooter.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-summary",
    "name": "OnboardingSummary",
    "family": "Onboarding",
    "kind": "component",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingSummary.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/onboarding/OnboardingFinishStage.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingSummary.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingSummary.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-onboarding-onboarding-view",
    "name": "OnboardingView",
    "family": "Onboarding",
    "kind": "composition",
    "owner": {
      "path": "web/src/components/onboarding/OnboardingView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/router.ts"
    ],
    "variants": [],
    "variantNotes": "Connected lifecycle/context/step/error composition. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in OnboardingView.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in OnboardingView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route or host composition; isolate its seam before rendering it in the workshop."
  },
  {
    "id": "components-settings-custom-colors-dialog",
    "name": "CustomColorsDialog",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/CustomColorsDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsAppearance.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in CustomColorsDialog.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in CustomColorsDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-google-connect-dialog",
    "name": "GoogleConnectDialog",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/GoogleConnectDialog.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in GoogleConnectDialog.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in GoogleConnectDialog.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-provider-inventory",
    "name": "ProviderInventory",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/ProviderInventory.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": ["complete", "partial", "unknown"],
    "variantNotes": "Inventory status controls the displayed provider availability guidance.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Provider inventory and sign-in disclosures have no rendered-state story."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Inventory data is supplied by the parent; this component does not load it."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-provider-sign-in-guidance",
    "name": "ProviderSignInGuidance",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/ProviderSignInGuidance.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue",
      "web/src/components/settings/ProviderInventory.vue"
    ],
    "variants": ["Claude", "OpenCode"],
    "variantNotes": "Sign-in command and guidance depend on the CLI.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Sign-in guidance has no rendered-state story."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further action and status branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The guidance has no initial or refresh loading state."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-all-workspaces",
    "name": "SettingsAllWorkspaces",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsAllWorkspaces.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [
      "first-load",
      "archived",
      "error",
      "reorder"
    ],
    "variantNotes": "Observed branches in SettingsAllWorkspaces.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsAllWorkspaces.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "first-load",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAllWorkspaces.vue has a first-load branch; no rendered-state story exists."
      },
      {
        "name": "archived",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAllWorkspaces.vue has a archived branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAllWorkspaces.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "reorder",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAllWorkspaces.vue has a reorder branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsAllWorkspaces.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-app-install",
    "name": "SettingsAppInstall",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsAppInstall.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsAppInstall.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsAppInstall.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-appearance",
    "name": "SettingsAppearance",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsAppearance.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [
      "custom colours",
      "error"
    ],
    "variantNotes": "Observed branches in SettingsAppearance.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsAppearance.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "custom colours",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAppearance.vue has a custom colours branch; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsAppearance.vue has a error branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsAppearance.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-claude-trim",
    "name": "SettingsClaudeTrim",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsClaudeTrim.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsClaudeTrim.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsClaudeTrim.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-devices",
    "name": "SettingsDevices",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsDevices.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsDevices.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsDevices.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-engine-login",
    "name": "SettingsEngineLogin",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsEngineLogin.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsEngineLogin.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsEngineLogin.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-keyboard-shortcuts",
    "name": "SettingsKeyboardShortcuts",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsKeyboardShortcuts.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsKeyboardShortcuts.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    id: 'components-provider-mcp-button',
    name: 'ProviderMcpButton',
    family: 'Settings',
    kind: 'component',
    owner: { path: 'web/src/components/ProviderMcpButton.vue', anchor: '<template>' },
    sources: ['web/src/composables/useRunInTerminal.ts'],
    consumers: ['web/src/components/ChatPanel.vue', 'web/src/components/SettingsView.vue'],
    variants: [],
    variantNotes: 'Open, copy and terminal outcomes have not yet been audited.',
    states: [
      { name: 'default', coverage: 'uncovered', storyIds: [], note: 'The button, command and reload note render when mcp_command is set; no story exists.' },
      { name: 'typed', coverage: 'uncovered', storyIds: [], note: 'A typed terminal run has no story.' },
      { name: 'copied', coverage: 'uncovered', storyIds: [], note: 'A copied terminal fallback has no story.' },
      { name: 'failed', coverage: 'uncovered', storyIds: [], note: 'A failed terminal run has no story.' },
    ],
    loading: { status: 'none', detail: 'No fetch. The command arrives on the provider row.' },
    migration: { status: 'unassessed', note: 'Inventory admission does not approve a design migration.' },
  },
  {
    "id": "components-settings-settings-memory-backup",
    "name": "SettingsMemoryBackup",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsMemoryBackup.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [
      "loading",
      "failed",
      "stale",
      "scope-gap",
      "ready",
      "paused"
    ],
    "variantNotes": "Observed branches in SettingsMemoryBackup.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsMemoryBackup.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "loading",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the loading branch; no rendered-state story exists."
      },
      {
        "name": "failed",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the failed branch; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the stale branch; no rendered-state story exists."
      },
      {
        "name": "scope-gap",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the scope-gap branch; no rendered-state story exists."
      },
      {
        "name": "ready",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the ready branch; no rendered-state story exists."
      },
      {
        "name": "paused",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsMemoryBackup.vue implements the paused branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsMemoryBackup.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-memory-handover",
    "name": "SettingsMemoryHandover",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsMemoryHandover.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsMemoryHandover.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsMemoryHandover.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-notifications",
    "name": "SettingsNotifications",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsNotifications.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsNotifications.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsNotifications.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-radio-group",
    "name": "SettingsRadioGroup",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsRadioGroup.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue",
      "web/src/components/settings/SettingsAppearance.vue"
    ],
    "variants": [
      "selected",
      "fontFamily"
    ],
    "variantNotes": "Controlled label/options/modelValue radio group; Theme, Text contrast and Body font use production consumer options, including per-option fontFamily.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--default"],
        "note": "Theme with Dark selected in the dark theme; local state accepts production update:modelValue events."
      },
      {
        "name": "selected",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--selected"],
        "note": "Theme with Light selected in the light theme."
      },
      {
        "name": "text-contrast",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--text-contrast"],
        "note": "Normal/Soft consumer options with Soft selected and the production soft text contrast applied."
      },
      {
        "name": "body-font",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--body-font"],
        "note": "System/Serif/Mono consumer options in their actual font families, with Serif selected."
      },
      {
        "name": "unselected",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--unselected"],
        "note": "Null modelValue leaves all options unchecked and the first option tabbable."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--narrow"],
        "note": "Body font options with Serif selected in a maximum 390px container."
      },
      {
        "name": "disabled",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "SettingsRadioGroup.vue defines no disabled prop or disabled branch."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["settings-radio-group--keyboard-focus"],
        "note": "Tab reaches the checked option with :focus-visible; ArrowRight moves focus and selects the next option."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Synchronous controlled component with no fetch, loading or busy branch; parents own persistence."
    },
    "migration": {
      "status": "retained",
      "note": "Existing production component and CSS retained; stories do not migrate connected Settings consumers."
    }
  },
  {
    "id": "components-settings-settings-restart-resume",
    "name": "SettingsRestartResume",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsRestartResume.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsRestartResume.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsRestartResume.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-server",
    "name": "SettingsServer",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsServer.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsServer.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsServer.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-update-tasks",
    "name": "SettingsUpdateTasks",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsUpdateTasks.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsUpdateTasks.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsUpdateTasks.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-workspace-models",
    "name": "SettingsWorkspaceModels",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsWorkspaceModels.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [
      "provider defaults",
      "dirty",
      "save"
    ],
    "variantNotes": "Observed branches in SettingsWorkspaceModels.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsWorkspaceModels.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "provider defaults",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsWorkspaceModels.vue has a provider defaults branch; no rendered-state story exists."
      },
      {
        "name": "dirty",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsWorkspaceModels.vue has a dirty branch; no rendered-state story exists."
      },
      {
        "name": "save",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "SettingsWorkspaceModels.vue has a save branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsWorkspaceModels.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-settings-workspace-overview",
    "name": "SettingsWorkspaceOverview",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue"
    ],
    "variants": [],
    "variantNotes": "Detailed configurable variants have not yet been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in SettingsWorkspaceOverview.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in SettingsWorkspaceOverview.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-workspace-color-picker",
    "name": "WorkspaceColorPicker",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/WorkspaceColorPicker.vue",
      "anchor": "<template>"
    },
    "sources": ["web/src/lib/workspaceColors.ts"],
    "consumers": [
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [
      "selected",
      "expanded",
      "disabled"
    ],
    "variantNotes": "Controlled modelValue, disabled native buttons and local disclosure; five base presets and four extras. Selected extra remains visible after collapse.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--default"],
        "note": "Amber selected with five base presets and a closed disclosure."
      },
      {
        "name": "selected",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--selected"],
        "note": "Pink selected; story parent feeds emitted choices back into modelValue."
      },
      {
        "name": "expanded",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--expanded"],
        "note": "Teal selected, all nine presets visible on mount."
      },
      {
        "name": "selected-extra-collapsed",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--selected-extra-collapsed"],
        "note": "Story closes the real disclosure; selected teal remains beside the five base presets."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--disabled"],
        "note": "Amber selected, collapsed; every swatch and disclosure disabled."
      },
      {
        "name": "disabled-expanded",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--disabled-expanded"],
        "note": "Teal selected, all nine presets and disclosure disabled."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["settings-workspace-color-picker--narrow"],
        "note": "Teal selected, expanded inside a maximum 390px container."
      },
      {
        "name": "keyboard-focus",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Native buttons support Tab and activation with focus-visible styling; no dedicated focus story. Radio arrow-key navigation is not implemented."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No fetch or loading branch; the parent supplies modelValue and disabled."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "components-settings-workspace-google-account",
    "name": "WorkspaceGoogleAccount",
    "family": "Settings",
    "kind": "component",
    "owner": {
      "path": "web/src/components/settings/WorkspaceGoogleAccount.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "variants": [
      "none",
      "connected",
      "expired",
      "connecting"
    ],
    "variantNotes": "Observed branches in WorkspaceGoogleAccount.vue; remaining variants have not been audited.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "<template> in WorkspaceGoogleAccount.vue implements this render; no rendered-state story exists."
      },
      {
        "name": "none",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WorkspaceGoogleAccount.vue has a none branch; no rendered-state story exists."
      },
      {
        "name": "connected",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WorkspaceGoogleAccount.vue has a connected branch; no rendered-state story exists."
      },
      {
        "name": "expired",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WorkspaceGoogleAccount.vue has a expired branch; no rendered-state story exists."
      },
      {
        "name": "connecting",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "WorkspaceGoogleAccount.vue has a connecting branch; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches and their coverage have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in WorkspaceGoogleAccount.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-chat-details",
    "name": "chatDetails",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/chatDetails.css",
      "anchor": ".cd-section"
    },
    "sources": [],
    "consumers": [
      "web/src/components/AgentContextSection.vue",
      "web/src/components/BrowserTerminalButton.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRepoSection.vue",
      "web/src/components/TaskOriginNote.vue"
    ],
    "variants": ["cd-section", "cd-heading", "cd-row", "cd-action"],
    "variantNotes": "Shared .cd-* sections, headings, counts, rows, links and actions; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".cd-section renders in chatDetails.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in chatDetails.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-chat-trace",
    "name": "chatTrace",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/chatTrace.css",
      "anchor": ".trace-block"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatTurnActivity.vue"
    ],
    "variants": ["trace-block", "live-preview", "reduced-motion"],
    "variantNotes": "Shared trace disclosure, streaming preview and reduced-motion rules; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".trace-block renders in chatTrace.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in chatTrace.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-header-fold",
    "name": "headerFold",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/headerFold.css",
      "anchor": ".fold-phone"
    },
    "sources": [
      "web/src/components/ItemTitleRow.vue",
      "web/src/components/ItemHeaderMenu.vue"
    ],
    "consumers": [
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": ["fold-phone", "fold-wide"],
    "variantNotes": "Phone/wide header folding. Support sources use these classes without importing the stylesheet; consumers are include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".fold-phone renders in headerFold.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in headerFold.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-import-sources",
    "name": "importSources",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/importSources.css",
      "anchor": ".import-head"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ImportRuns.vue",
      "web/src/components/ImportSources.vue"
    ],
    "variants": ["import-head", "import-list", "import-error", "import-warn"],
    "variantNotes": "Shared import list/status/actions/error/warning family; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".import-head renders in importSources.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in importSources.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-markdown-dead-link",
    "name": "markdownDeadLink",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/markdownDeadLink.css",
      "anchor": ":deep(button.markdown-dead-link--openable)"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/PinnedFilePanel.vue"
    ],
    "variants": ["hover", "focus-visible"],
    "variantNotes": "Openable Markdown dead-link buttons have hover/focus rules; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ":deep(button.markdown-dead-link--openable) renders in markdownDeadLink.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in markdownDeadLink.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-memory-review",
    "name": "memoryReview",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/memoryReview.css",
      "anchor": ".mr-head"
    },
    "sources": [],
    "consumers": [
      "web/src/components/MemoryMapView.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/VaultReviewPanel.vue"
    ],
    "variants": ["mr-head", "mr-chips", "mr-chip"],
    "variantNotes": "Shared review headings and filter chips; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mr-head renders in memoryReview.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in memoryReview.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-overview-sections",
    "name": "overviewSections",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/overviewSections.css",
      "anchor": ".ov-section"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SchedulePanel.vue",
      "web/src/components/WebhookTriggers.vue"
    ],
    "variants": ["ov-section"],
    "variantNotes": "Shared overview sections, lists, headings and actions; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".ov-section renders in overviewSections.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in overviewSections.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-rail-motion",
    "name": "railMotion",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/railMotion.css",
      "anchor": ".rail-leaning"
    },
    "sources": [
      "web/src/lib/titleRail.ts"
    ],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/TaskWorkspaceSurface.vue"
    ],
    "variants": ["rail-leaning", "data-rail-lean", "reduced-motion"],
    "variantNotes": "Shared lean/arrival motion. Support helper titleRail.ts produces classes without importing the stylesheet; consumers are include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".rail-leaning renders in railMotion.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in railMotion.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-settings-settings-panels",
    "name": "settingsPanels",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/settings/settingsPanels.css",
      "anchor": ".card"
    },
    "sources": [],
    "consumers": [
      "web/src/components/SettingsView.vue",
      "web/src/components/settings/GoogleConnectDialog.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppInstall.vue",
      "web/src/components/settings/SettingsAppearance.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRadioGroup.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "variants": ["settings-card"],
    "variantNotes": "Shared scoped settings panel family, not the global card implementation; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".card renders in settingsPanels.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in settingsPanels.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-slash-commands",
    "name": "slashCommands",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/slashCommands.css",
      "anchor": ".commands-picker"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/SlashCommandPicker.vue"
    ],
    "variants": ["commands-picker", "active", "hint"],
    "variantNotes": "Shared skill/command picker, active row and argument hints; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".commands-picker renders in slashCommands.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in slashCommands.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-title-shimmer",
    "name": "titleShimmer",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/components/titleShimmer.css",
      "anchor": ".title-shimmer"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/SidebarRow.vue",
      "web/src/components/TitleRail.vue"
    ],
    "variants": ["title-shimmer", "chat-title-shimmer", "reduced-motion"],
    "variantNotes": "Shared pending-title shimmer and reduced-motion rules; consumers are stylesheet include sites.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".title-shimmer renders in titleShimmer.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in titleShimmer.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "css-app",
    "name": "app",
    "family": "Shared CSS",
    "kind": "css",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ":root"
    },
    "sources": [],
    "consumers": [
      "web/src/App.vue"
    ],
    "variants": ["tokens", "controls", "surfaces", "layout", "focus"],
    "variantNotes": "Canonical global tokens and primitives; App.vue is the production stylesheet include site. Individual inline families document class users as support sources.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ":root renders in app.css; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further state branches have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-buttons",
    "name": "Global buttons",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".btn-primary"
    },
    "sources": [
      "web/src/components/AgentContextSection.vue",
      "web/src/components/ArchiveTaskDialog.vue",
      "web/src/components/BrandMark.vue",
      "web/src/components/BrowserTerminalButton.vue",
      "web/src/components/ChatCommentPopover.vue",
      "web/src/components/ChatHeaderMoreMenu.vue",
      "web/src/components/ChatLayout.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRepoSection.vue",
      "web/src/components/ChatRetryState.vue",
      "web/src/components/ChatTurnActivity.vue",
      "web/src/components/CommentComposePopover.vue",
      "web/src/components/ConfirmDialog.vue",
      "web/src/components/CsvViewer.vue",
      "web/src/components/DecisionReason.vue",
      "web/src/components/EngineOfflineView.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/HomeIntake.vue",
      "web/src/components/HomeNoticeReopen.vue",
      "web/src/components/HomeNoticeWindow.vue",
      "web/src/components/HomeOnboardingResume.vue",
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/HomeReviewSummary.vue",
      "web/src/components/HomeSetupCard.vue",
      "web/src/components/HomeWelcomeCard.vue",
      "web/src/components/HostTerminalButton.vue",
      "web/src/components/HousekeepingStrip.vue",
      "web/src/components/HtmlArtifactViewer.vue",
      "web/src/components/ImportRuns.vue",
      "web/src/components/ImportSources.vue",
      "web/src/components/InAppToast.vue",
      "web/src/components/InjectedPromptCard.vue",
      "web/src/components/ItemHeaderMenu.vue",
      "web/src/components/ItemTitleRow.vue",
      "web/src/components/ListRow.vue",
      "web/src/components/LoginView.vue",
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/MemoryGuideBudget.vue",
      "web/src/components/MemoryHowItWorks.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/ModelSelector.vue",
      "web/src/components/NeedsYouCard.vue",
      "web/src/components/NeedsYouRow.vue",
      "web/src/components/NeedsYouStack.vue",
      "web/src/components/NewChatPicker.vue",
      "web/src/components/NewScheduleForm.vue",
      "web/src/components/NotificationBell.vue",
      "web/src/components/NotificationInboxList.vue",
      "web/src/components/PaneHeader.vue",
      "web/src/components/PinnedFilePanel.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/PromptDialog.vue",
      "web/src/components/ProposalHistoryList.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/ProviderSignInHint.vue",
      "web/src/components/RepoPickerDialog.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/StartupView.vue",
      "web/src/components/TabBar.vue",
      "web/src/components/TaskOriginNote.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/TerminalKeyRow.vue",
      "web/src/components/TerminalPane.vue",
      "web/src/components/TitleRail.vue",
      "web/src/components/UsageView.vue",
      "web/src/components/VaultReviewPanel.vue",
      "web/src/components/WebhookTriggers.vue",
      "web/src/components/WorkspaceMoveDialog.vue",
      "web/src/components/needs-you/CallbackPasteField.vue",
      "web/src/components/needs-you/CommandBody.vue",
      "web/src/components/needs-you/DeviceCodeBody.vue",
      "web/src/components/needs-you/GwsAuthBody.vue",
      "web/src/components/needs-you/HelperTerminalButton.vue",
      "web/src/components/needs-you/PermissionModeBody.vue",
      "web/src/components/needs-you/RestartBody.vue",
      "web/src/components/needs-you/SystemSettingBody.vue",
      "web/src/components/onboarding/OnboardingButton.vue",
      "web/src/components/onboarding/OnboardingView.vue",
      "web/src/components/settings/CustomColorsDialog.vue",
      "web/src/components/settings/GoogleConnectDialog.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppearance.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRadioGroup.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "web/src/components/settings/WorkspaceColorPicker.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "consumers": [],
    "variants": [
      "btn-primary",
      "btn-small",
      "btn-icon",
      "btn-chip",
      "btn-danger",
      "touch-hit",
      "native button",
      "primary + small",
      "hover",
      "active",
      "disabled",
      "aria-pressed"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "btn-primary",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes btn-primary; no rendered-state story exists."
      },
      {
        "name": "btn-small",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes btn-small; no rendered-state story exists."
      },
      {
        "name": "btn-icon",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes btn-icon; no rendered-state story exists."
      },
      {
        "name": "btn-chip",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes btn-chip; no rendered-state story exists."
      },
      {
        "name": "btn-danger",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes btn-danger; no rendered-state story exists."
      },
      {
        "name": "touch-hit",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".btn-primary in app.css includes touch-hit; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-fields-forms",
    "name": "Global fields and forms",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".form-grid"
    },
    "sources": [
      "web/src/components/ArchiveTaskDialog.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/CommentComposePopover.vue",
      "web/src/components/CsvViewer.vue",
      "web/src/components/DecisionReason.vue",
      "web/src/components/EngineOfflineView.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/HomeIntake.vue",
      "web/src/components/HomeSetupCard.vue",
      "web/src/components/HomeWelcomeCard.vue",
      "web/src/components/ImportRuns.vue",
      "web/src/components/ImportSources.vue",
      "web/src/components/LoginView.vue",
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/ModelSelector.vue",
      "web/src/components/NewScheduleForm.vue",
      "web/src/components/PinnedFilePanel.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/PromptDialog.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/RepoPickerDialog.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/TaskOriginNote.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/WebhookTriggers.vue",
      "web/src/components/WorkspaceMoveDialog.vue",
      "web/src/components/needs-you/CallbackPasteField.vue",
      "web/src/components/onboarding/OnboardingAccessStage.vue",
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingChoice.vue",
      "web/src/components/onboarding/OnboardingFinishStage.vue",
      "web/src/components/onboarding/OnboardingLocationStage.vue",
      "web/src/components/onboarding/OnboardingMemoryStage.vue",
      "web/src/components/settings/CustomColorsDialog.vue",
      "web/src/components/settings/GoogleConnectDialog.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppInstall.vue",
      "web/src/components/settings/SettingsAppearance.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "consumers": [],
    "variants": [
      "form-grid",
      "form-group",
      "form-actions",
      "hint",
      "checkbox-pill",
      "input",
      "textarea",
      "select",
      "focus",
      "coarse-pointer"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "form-grid",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css includes form-grid; no rendered-state story exists."
      },
      {
        "name": "form-group",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css includes form-group; no rendered-state story exists."
      },
      {
        "name": "form-actions",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css includes form-actions; no rendered-state story exists."
      },
      {
        "name": "hint",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css includes hint; no rendered-state story exists."
      },
      {
        "name": "checkbox-pill",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".form-grid in app.css includes checkbox-pill; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-badges",
    "name": "Global badges",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".badge"
    },
    "sources": [
      "web/src/components/LoginView.vue",
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/ModelSelector.vue",
      "web/src/components/ProposalHistoryList.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/WebhookTriggers.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "consumers": [],
    "variants": [
      "badge",
      "badge--accent",
      "badge--accent2",
      "badge--muted",
      "badge--builtin",
      "badge--success",
      "badge--warn",
      "badge--error",
      "badge--dot"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "badge",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge; no rendered-state story exists."
      },
      {
        "name": "badge--accent",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--accent; no rendered-state story exists."
      },
      {
        "name": "badge--accent2",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--accent2; no rendered-state story exists."
      },
      {
        "name": "badge--muted",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--muted; no rendered-state story exists."
      },
      {
        "name": "badge--builtin",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--builtin; no rendered-state story exists."
      },
      {
        "name": "badge--success",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--success; no rendered-state story exists."
      },
      {
        "name": "badge--warn",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--warn; no rendered-state story exists."
      },
      {
        "name": "badge--error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--error; no rendered-state story exists."
      },
      {
        "name": "badge--dot",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".badge in app.css includes badge--dot; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-surfaces",
    "name": "Global surfaces",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".card"
    },
    "sources": [
      "web/src/components/ImportSources.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppInstall.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "consumers": [],
    "variants": [
      "card"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".card in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "card",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".card in app.css includes card; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-page-layout",
    "name": "Global page layout",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".page"
    },
    "sources": [
      "web/src/components/MemoryHowItWorks.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/NewScheduleForm.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/UsageView.vue"
    ],
    "consumers": [],
    "variants": [
      "page",
      "page-grid",
      "page-main",
      "page-rail"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".page in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "page",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".page in app.css includes page; no rendered-state story exists."
      },
      {
        "name": "page-grid",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".page in app.css includes page-grid; no rendered-state story exists."
      },
      {
        "name": "page-main",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".page in app.css includes page-main; no rendered-state story exists."
      },
      {
        "name": "page-rail",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".page in app.css includes page-rail; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-typography",
    "name": "Global typography",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".section-title"
    },
    "sources": [
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRepoSection.vue",
      "web/src/components/ChatTurnActivity.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/HomeIntake.vue",
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/HomeWelcomeCard.vue",
      "web/src/components/MemoryHowItWorks.vue",
      "web/src/components/NeedsYouCard.vue",
      "web/src/components/NeedsYouRow.vue",
      "web/src/components/NotificationInboxList.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/SidebarListSkeleton.vue",
      "web/src/components/SidebarRow.vue",
      "web/src/components/SkeletonLoader.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/UsageView.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppInstall.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue"
    ],
    "consumers": [],
    "variants": [
      "sr-only",
      "section-title",
      "label-eyebrow",
      "subsection-title"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".sr-only in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "sr-only",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".sr-only in app.css includes sr-only; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-focus",
    "name": "Global focus",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ":focus-visible"
    },
    "sources": [
      "web/src/components/AgentContextSection.vue",
      "web/src/components/ArchiveTaskDialog.vue",
      "web/src/components/BrandMark.vue",
      "web/src/components/BrowserTerminalButton.vue",
      "web/src/components/ChatCommentPopover.vue",
      "web/src/components/ChatHeaderMoreMenu.vue",
      "web/src/components/ChatLayout.vue",
      "web/src/components/ChatPanel.vue",
      "web/src/components/ChatRepoSection.vue",
      "web/src/components/ChatRetryState.vue",
      "web/src/components/ChatTurnActivity.vue",
      "web/src/components/CommentComposePopover.vue",
      "web/src/components/ConfirmDialog.vue",
      "web/src/components/CsvViewer.vue",
      "web/src/components/DecisionReason.vue",
      "web/src/components/EngineOfflineView.vue",
      "web/src/components/FileViewerModal.vue",
      "web/src/components/HomeIntake.vue",
      "web/src/components/HomeNoticeReopen.vue",
      "web/src/components/HomeNoticeWindow.vue",
      "web/src/components/HomeOnboardingResume.vue",
      "web/src/components/HomeRecentChats.vue",
      "web/src/components/HomeReviewSummary.vue",
      "web/src/components/HomeSetupCard.vue",
      "web/src/components/HomeWelcomeCard.vue",
      "web/src/components/HostTerminalButton.vue",
      "web/src/components/HousekeepingStrip.vue",
      "web/src/components/HtmlArtifactViewer.vue",
      "web/src/components/ImportRuns.vue",
      "web/src/components/ImportSources.vue",
      "web/src/components/InAppToast.vue",
      "web/src/components/InjectedPromptCard.vue",
      "web/src/components/ItemHeaderMenu.vue",
      "web/src/components/ItemTitleRow.vue",
      "web/src/components/ListRow.vue",
      "web/src/components/LoginView.vue",
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/MemoryGuideBudget.vue",
      "web/src/components/MemoryHowItWorks.vue",
      "web/src/components/MemoryMapView.vue",
      "web/src/components/ModelSelector.vue",
      "web/src/components/NeedsYouCard.vue",
      "web/src/components/NeedsYouRow.vue",
      "web/src/components/NeedsYouStack.vue",
      "web/src/components/NewChatPicker.vue",
      "web/src/components/NewScheduleForm.vue",
      "web/src/components/NotificationBell.vue",
      "web/src/components/NotificationInboxList.vue",
      "web/src/components/PaneHeader.vue",
      "web/src/components/PinnedFilePanel.vue",
      "web/src/components/ProjectSidebar.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/PromptDialog.vue",
      "web/src/components/ProposalHistoryList.vue",
      "web/src/components/ProposalReviewPanel.vue",
      "web/src/components/ProviderSignInHint.vue",
      "web/src/components/RepoPickerDialog.vue",
      "web/src/components/SchedulePanel.vue",
      "web/src/components/SettingsView.vue",
      "web/src/components/StartupView.vue",
      "web/src/components/TabBar.vue",
      "web/src/components/TaskOriginNote.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/TerminalKeyRow.vue",
      "web/src/components/TerminalPane.vue",
      "web/src/components/TitleRail.vue",
      "web/src/components/UsageView.vue",
      "web/src/components/VaultReviewPanel.vue",
      "web/src/components/WebhookTriggers.vue",
      "web/src/components/WorkspaceMoveDialog.vue",
      "web/src/components/needs-you/CallbackPasteField.vue",
      "web/src/components/needs-you/CommandBody.vue",
      "web/src/components/needs-you/DeviceCodeBody.vue",
      "web/src/components/needs-you/GwsAuthBody.vue",
      "web/src/components/needs-you/HelperTerminalButton.vue",
      "web/src/components/needs-you/PermissionModeBody.vue",
      "web/src/components/needs-you/RestartBody.vue",
      "web/src/components/needs-you/SystemSettingBody.vue",
      "web/src/components/onboarding/OnboardingButton.vue",
      "web/src/components/onboarding/OnboardingCareStage.vue",
      "web/src/components/onboarding/OnboardingChoice.vue",
      "web/src/components/onboarding/OnboardingFinishStage.vue",
      "web/src/components/onboarding/OnboardingLocationStage.vue",
      "web/src/components/onboarding/OnboardingRuntimeStage.vue",
      "web/src/components/onboarding/OnboardingSummary.vue",
      "web/src/components/onboarding/OnboardingView.vue",
      "web/src/components/settings/CustomColorsDialog.vue",
      "web/src/components/settings/GoogleConnectDialog.vue",
      "web/src/components/settings/SettingsAllWorkspaces.vue",
      "web/src/components/settings/SettingsAppInstall.vue",
      "web/src/components/settings/SettingsAppearance.vue",
      "web/src/components/settings/SettingsClaudeTrim.vue",
      "web/src/components/settings/SettingsDevices.vue",
      "web/src/components/settings/SettingsEngineLogin.vue",
      "web/src/components/settings/SettingsKeyboardShortcuts.vue",
      "web/src/components/settings/SettingsMemoryBackup.vue",
      "web/src/components/settings/SettingsMemoryHandover.vue",
      "web/src/components/settings/SettingsNotifications.vue",
      "web/src/components/settings/SettingsRadioGroup.vue",
      "web/src/components/settings/SettingsRestartResume.vue",
      "web/src/components/settings/SettingsServer.vue",
      "web/src/components/settings/SettingsUpdateTasks.vue",
      "web/src/components/settings/SettingsWorkspaceModels.vue",
      "web/src/components/settings/SettingsWorkspaceOverview.vue",
      "web/src/components/settings/WorkspaceColorPicker.vue",
      "web/src/components/settings/WorkspaceGoogleAccount.vue"
    ],
    "consumers": [],
    "variants": [
      "focus-visible"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ":focus-visible in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "focus-visible",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ":focus-visible in app.css includes focus-visible; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-modals",
    "name": "Global modals",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".modal-backdrop"
    },
    "sources": [
      "web/src/components/MemoryCategoriesPanel.vue",
      "web/src/components/ProjectView.vue",
      "web/src/components/TaskWorkspaceSurface.vue",
      "web/src/components/settings/GoogleConnectDialog.vue"
    ],
    "consumers": [],
    "variants": [
      "modal-backdrop",
      "modal-sheet"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".modal-backdrop in app.css implements this family; no rendered-state story exists."
      },
      {
        "name": "modal-backdrop",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".modal-backdrop in app.css includes modal-backdrop; no rendered-state story exists."
      },
      {
        "name": "modal-sheet",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".modal-backdrop in app.css includes modal-sheet; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "global-terminal-branding",
    "name": "Global terminal branding",
    "family": "Root, navigation and common presentation",
    "kind": "inline",
    "owner": {
      "path": "web/src/styles/app.css",
      "anchor": ".wordmark"
    },
    "sources": [
      "web/src/components/LoginView.vue",
      "web/src/components/RestartNotice.vue",
      "web/src/components/StartupView.vue",
      "web/src/components/UpdateProgressView.vue"
    ],
    "consumers": [],
    "variants": [
      "wordmark",
      "caret"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Support paths use these classes in markup; they do not directly import this family.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".wordmark in app.css styles Wordmark.vue and terminal status labels; no rendered-state story exists."
      },
      {
        "name": "wordmark",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".wordmark in app.css includes the shared monospace wordmark; no rendered-state story exists."
      },
      {
        "name": "caret",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".wordmark in app.css includes caret; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in app.css has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "chat-panel-messages",
    "name": "Chat messages",
    "family": "Chat, messages, composer, activity and context",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/ChatPanel.vue",
      "anchor": ".messages"
    },
    "sources": [
      "web/src/lib/safeMarkdown.ts"
    ],
    "consumers": [],
    "variants": [
      "user",
      "assistant",
      "system",
      "streaming",
      "thinking",
      "retry"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. Markdown helper supports the transcript.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "user",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes user; no rendered-state story exists."
      },
      {
        "name": "assistant",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes assistant; no rendered-state story exists."
      },
      {
        "name": "system",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes system; no rendered-state story exists."
      },
      {
        "name": "streaming",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes streaming; no rendered-state story exists."
      },
      {
        "name": "thinking",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes thinking; no rendered-state story exists."
      },
      {
        "name": "retry",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".messages in ChatPanel.vue includes retry; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatPanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "chat-panel-composer",
    "name": "Chat composer",
    "family": "Chat, messages, composer, activity and context",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/ChatPanel.vue",
      "anchor": ".input-bar"
    },
    "sources": [
      "web/src/composables/useChatComposer.ts"
    ],
    "consumers": [],
    "variants": [
      "textarea",
      "send",
      "stop",
      "attachments",
      "model",
      "thinking",
      "drag-over"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. The composable supports behavior, not visual ownership.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "textarea",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes textarea; no rendered-state story exists."
      },
      {
        "name": "send",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes send; no rendered-state story exists."
      },
      {
        "name": "stop",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes stop; no rendered-state story exists."
      },
      {
        "name": "attachments",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes attachments; no rendered-state story exists."
      },
      {
        "name": "model",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes model; no rendered-state story exists."
      },
      {
        "name": "thinking",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes thinking; no rendered-state story exists."
      },
      {
        "name": "drag-over",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".input-bar in ChatPanel.vue includes drag-over; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatPanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "chat-panel-question-capability",
    "name": "Question and capability cards",
    "family": "Chat, messages, composer, activity and context",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/ChatPanel.vue",
      "anchor": ".question-card"
    },
    "sources": [],
    "consumers": [],
    "variants": [
      "question-option",
      "question-other",
      "multiSelect",
      "stale",
      "validation",
      "submitting",
      "error",
      "capability-card"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "question-option",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes question-option; no rendered-state story exists."
      },
      {
        "name": "question-other",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes question-other; no rendered-state story exists."
      },
      {
        "name": "multiSelect",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes multiSelect; no rendered-state story exists."
      },
      {
        "name": "stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes stale; no rendered-state story exists."
      },
      {
        "name": "validation",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes validation; no rendered-state story exists."
      },
      {
        "name": "submitting",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes submitting; no rendered-state story exists."
      },
      {
        "name": "error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes error; no rendered-state story exists."
      },
      {
        "name": "capability-card",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".question-card in ChatPanel.vue includes capability-card; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in ChatPanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "home-intake-composer",
    "name": "Home intake composer",
    "family": "Home and notices",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/HomeIntake.vue",
      "anchor": ".home-intake-form"
    },
    "sources": [],
    "consumers": [],
    "variants": [
      "prompt",
      "project",
      "model",
      "attachments"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".home-intake-form in HomeIntake.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "prompt",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".home-intake-form in HomeIntake.vue includes prompt; no rendered-state story exists."
      },
      {
        "name": "project",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".home-intake-form in HomeIntake.vue includes project; no rendered-state story exists."
      },
      {
        "name": "model",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".home-intake-form in HomeIntake.vue includes model; no rendered-state story exists."
      },
      {
        "name": "attachments",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".home-intake-form in HomeIntake.vue includes attachments; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in HomeIntake.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "task-board-cards",
    "name": "Task board cards",
    "family": "Tasks, projects and automations",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": ".task-cards"
    },
    "sources": [
      "web/src/components/TaskBoardView.vue",
      "web/src/components/ItemPeekHost.vue"
    ],
    "consumers": [],
    "variants": [
      "lanes",
      "cards",
      "filters",
      "Done"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-cards in TaskWorkspaceSurface.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "lanes",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-cards in TaskWorkspaceSurface.vue includes lanes; no rendered-state story exists."
      },
      {
        "name": "cards",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-cards in TaskWorkspaceSurface.vue includes cards; no rendered-state story exists."
      },
      {
        "name": "filters",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-cards in TaskWorkspaceSurface.vue includes filters; no rendered-state story exists."
      },
      {
        "name": "Done",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-cards in TaskWorkspaceSurface.vue includes Done; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskWorkspaceSurface.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "task-editor",
    "name": "Task editor",
    "family": "Tasks, projects and automations",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": ".task-sheet"
    },
    "sources": [
      "web/src/components/TaskBoardView.vue",
      "web/src/components/ItemPeekHost.vue"
    ],
    "consumers": [],
    "variants": [
      "page",
      "overlay",
      "editing"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-sheet in TaskWorkspaceSurface.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "page",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-sheet in TaskWorkspaceSurface.vue includes page; no rendered-state story exists."
      },
      {
        "name": "overlay",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-sheet in TaskWorkspaceSurface.vue includes overlay; no rendered-state story exists."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-sheet in TaskWorkspaceSurface.vue includes editing; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskWorkspaceSurface.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "task-completion",
    "name": "Task completion",
    "family": "Tasks, projects and automations",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": ".task-resolution-sheet"
    },
    "sources": [
      "web/src/components/TaskBoardView.vue",
      "web/src/components/ItemPeekHost.vue"
    ],
    "consumers": [],
    "variants": [
      "complete",
      "edit",
      "review-ready"
    ],
    "variantNotes": "The .task-resolution-sheet dialog owns completion and resolution editing. The .task-completion selector instead marks earlier-completion history items, not this sheet. Support sources are indirect composition hosts, not direct inline consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue gates .task-resolution-sheet with resolutionOpen && resolutionTask; no rendered-state story exists."
      },
      {
        "name": "complete",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "resolutionMode === 'complete' presents Complete with resolution and the Mark done action unless the task is review-ready; no rendered-state story exists."
      },
      {
        "name": "edit",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "resolutionMode.value === 'edit' presents Edit resolution and Save resolution. editResolution reads the stored text before opening; a failed read opens nothing. No rendered-state story exists."
      },
      {
        "name": "review-ready",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "In complete mode, isReviewReady(resolutionTask) selects Approve done and renders DecisionReason bound to resolutionReason; no rendered-state story exists."
      },
      {
        "name": "pending",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "resolutionBusy makes the textarea, DecisionReason, submit, refresh, close and Cancel controls disabled and changes the submit label to Saving…; no rendered-state story exists."
      },
      {
        "name": "action-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "resolutionError renders a role=\"alert\" message with refreshResolutionTask. The refresh re-reads the board and task revision without changing the draft; refusal keeps the sheet and draft. No rendered-state story exists."
      },
      {
        "name": "draft",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The optional resolutionDraft textarea is initialized by openResolution. Completion sends an empty note for a blank or whitespace-only draft; edit mode preserves the exact typed text. No rendered-state story exists."
      },
      {
        "name": "submit",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The form calls submitResolution, flushing editor changes and, for completion, releasing a held attempt before reading the current revision. Edit uses board.update; complete uses board.complete with the optional review reason. Success resets the sheet; refusal preserves the draft. No rendered-state story exists."
      },
      {
        "name": "cancel",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Cancel, close and backdrop invoke closeResolution without writing; Escape also closes through useModalFocus. Closing is refused while resolutionBusy; otherwise resetResolution clears the sheet and draft. No rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskWorkspaceSurface.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "task-delegation",
    "name": "Task delegation",
    "family": "Tasks, projects and automations",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": ".task-delegate"
    },
    "sources": [
      "web/src/components/TaskBoardView.vue",
      "web/src/components/ItemPeekHost.vue"
    ],
    "consumers": [],
    "variants": [
      "delegation",
      "handover"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-delegate in TaskWorkspaceSurface.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "delegation",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-delegate in TaskWorkspaceSurface.vue includes delegation; no rendered-state story exists."
      },
      {
        "name": "handover",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-delegate in TaskWorkspaceSurface.vue includes handover; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskWorkspaceSurface.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "task-ordering",
    "name": "Task ordering",
    "family": "Tasks, projects and automations",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/TaskWorkspaceSurface.vue",
      "anchor": ".task-move-sheet"
    },
    "sources": [
      "web/src/components/TaskBoardView.vue",
      "web/src/components/ItemPeekHost.vue"
    ],
    "consumers": [],
    "variants": [
      "move",
      "ordering"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-move-sheet in TaskWorkspaceSurface.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "move",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-move-sheet in TaskWorkspaceSurface.vue includes move; no rendered-state story exists."
      },
      {
        "name": "ordering",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".task-move-sheet in TaskWorkspaceSurface.vue includes ordering; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in TaskWorkspaceSurface.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "file-modal-preview-editor",
    "name": "File modal preview and editor",
    "family": "Files, terminal and dialogs",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/FileViewerModal.vue",
      "anchor": ".fv-body"
    },
    "sources": [
      "web/src/components/CsvViewer.vue",
      "web/src/components/HtmlArtifactViewer.vue",
      "web/src/components/FolderBrowser.vue"
    ],
    "consumers": [],
    "variants": [
      "preview",
      "history",
      "diff",
      "editing"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are renderer support components. History and diff branches no longer exist in the template: the scoped style still carries .fv-history-*/.fv-diff-* rules nothing renders.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The .fv-body preview states are proven by components-file-viewer-modal's covered states (default/markdown/document-properties/empty/csv/image/image-error/folder/pdf/libreoffice-notice/html stories render exactly this body); inline rows cannot claim story coverage."
      },
      {
        "name": "preview",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Same body as default, named for the preview-vs-editor split this family owns; covered through components-file-viewer-modal (including html-code/code-loading/code-error for the artifact panel). Inline rows cannot claim story coverage."
      },
      {
        "name": "history",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "No history branch exists in .fv-body's template: the history UI was removed (only its .fv-history-* CSS remains in the scoped style) and no web surface consumes file snapshots."
      },
      {
        "name": "diff",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "No diff branch exists in .fv-body's template: the diff UI was removed (only its .fv-diff-* CSS remains in the scoped style)."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The edit shell is exercised end to end by components-file-viewer-modal's editing, editing-csv, saving and save-error states (the file-modal-preview-editor--* stories, claimed by the modal component row); inline rows cannot claim story coverage."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The .fv-body shows the modal's 'Loading…'/error text lines for the initial load and the streaming-end refresh failure — no separate skeleton geometry (initial loading and failure are the modal row's covered loading/error stories). Loaded content replaces the whole body, so the loading line does not match the loaded table/prose geometry and a loading-geometry follow-up applies."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "pinned-file-preview-editor",
    "name": "Pinned file preview and editor",
    "family": "Files, terminal and dialogs",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/PinnedFilePanel.vue",
      "anchor": ".pfp-body"
    },
    "sources": [
      "web/src/components/CsvViewer.vue",
      "web/src/components/HtmlArtifactViewer.vue",
      "web/src/components/FolderBrowser.vue"
    ],
    "consumers": [],
    "variants": [
      "preview",
      "editing"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are renderer support components.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".pfp-body in PinnedFilePanel.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "preview",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".pfp-body in PinnedFilePanel.vue includes preview; no rendered-state story exists."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".pfp-body in PinnedFilePanel.vue includes editing; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in PinnedFilePanel.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "memory-graph",
    "name": "Memory graph",
    "family": "Memory and usage",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/MemoryMapView.vue",
      "anchor": ".mm-canvas-wrap"
    },
    "sources": [],
    "consumers": [],
    "variants": [
      "graph",
      "selection"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-canvas-wrap in MemoryMapView.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "graph",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-canvas-wrap in MemoryMapView.vue includes graph; no rendered-state story exists."
      },
      {
        "name": "selection",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-canvas-wrap in MemoryMapView.vue includes selection; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in MemoryMapView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  },
  {
    "id": "memory-list-selection",
    "name": "Memory list and selection",
    "family": "Memory and usage",
    "kind": "inline",
    "owner": {
      "path": "web/src/components/MemoryMapView.vue",
      "anchor": ".mm-list-wrap"
    },
    "sources": [],
    "consumers": [],
    "variants": [
      "list",
      "selection",
      "filter"
    ],
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-list-wrap in MemoryMapView.vue implements this family; no rendered-state story exists."
      },
      {
        "name": "list",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-list-wrap in MemoryMapView.vue includes list; no rendered-state story exists."
      },
      {
        "name": "selection",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-list-wrap in MemoryMapView.vue includes selection; no rendered-state story exists."
      },
      {
        "name": "filter",
        "coverage": "uncovered",
        "storyIds": [],
        "note": ".mm-list-wrap in MemoryMapView.vue includes filter; no rendered-state story exists."
      },
      {
        "name": "other states",
        "coverage": "unknown",
        "storyIds": [],
        "note": "Further states have not yet been audited."
      }
    ],
    "loading": {
      "status": "unknown",
      "detail": "Initial and refresh loading fidelity in MemoryMapView.vue has not yet been audited."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
  }
]

export function filterInventory(entries: readonly InventoryEntry[], filters: InventoryFilters): InventoryEntry[] {
  const terms = filters.query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  return entries.filter((entry) => {
    if (filters.family !== 'all' && entry.family !== filters.family) return false
    if (filters.coverage !== 'all' && !entry.states.some((state) => state.coverage === filters.coverage)) return false
    if (filters.migration !== 'all' && entry.migration.status !== filters.migration) return false
    const text = [entry.id, entry.name, entry.family, entry.kind, entry.owner.path, entry.owner.anchor,
      ...entry.sources, ...entry.consumers, ...entry.variants, entry.variantNotes,
      ...entry.states.flatMap((state) => [state.name, state.note]), entry.loading.status, entry.loading.detail,
      entry.migration.status, entry.migration.note, entry.rationale ?? ''].join(' ').toLowerCase()
    return terms.every((term) => text.includes(term))
  })
}
