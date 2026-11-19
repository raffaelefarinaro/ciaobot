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
    "variants": [
      "link",
      "row"
    ],
    "variantNotes": "Link (default) and row forms; both open the terminal store on click.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "browser-terminal-button--default"
        ],
        "note": "Link form (Open in Terminal): tab focuses it, a click reads the engine status once and the button stays."
      },
      {
        "name": "row",
        "coverage": "covered",
        "storyIds": [
          "browser-terminal-button--row"
        ],
        "note": "Row form (Open terminal) in the Chat details Open section; a click reads the engine status once."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No fetch on render. A click opens the terminal store, which reads status once; the story asserts that one declared request."
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
    "variants": [
      "hover-preview",
      "pinned",
      "actions",
      "draft-id",
      "narrow"
    ],
    "variantNotes": "The transcript read popover owns its state and is driven imperatively by ChatPanel through the exposed handlers it binds at event time; the host renders the highlights and logs the emitted edits/deletes. Hover preview (quote + note, unpinned), explicit pin (focus + backdrop), the Edit/Delete handoffs, the ignored draft id and the narrow viewport.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--default"],
        "note": "A closed popover renders nothing: no dialog, no backdrop and no recorded emissions until the transcript's handlers run."
      },
      {
        "name": "hover-preview",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--hover-preview"],
        "note": "onTargetOver previews the comment: the quoted selection and the note with Edit/Delete, never a pinned backdrop and never recorded as a handoff."
      },
      {
        "name": "pinned",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--pinned"],
        "note": "pinFromEvent pins the popover, records the id and gives the first action focus; Escape closes through the popover's own handler."
      },
      {
        "name": "actions",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--actions"],
        "note": "Edit and Delete each close the popover and hand the comment/id to the panel, the skipNextFocusRestore handoff the transcript relies on."
      },
      {
        "name": "draft-ignored",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--draft-ignored"],
        "note": "The in-flight draft highlight (data-comment-id = draftId) has no saved comment: hovering it opens nothing, while a genuine id beside it previews."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["chat-comment-popover--narrow"],
        "note": "The popover is a fixed overlay teleported to body, so the narrow 390px workshop viewport is the constraint: the box stays inside it with the 16px clamp."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The popover loads nothing: comments arrive as the transcript's props, and the only async surface is the viewport re-clamp."
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
    "variants": [
      "draft",
      "images",
      "inline",
      "keyboard",
      "narrow"
    ],
    "variantNotes": "The shared floating compose popover used by ChatPanel, FileViewerModal and PinnedFilePanel; anchor/modelValue/images/inline props carry every variant. Teleported to body unless inline. Empty-draft block, typed draft save, attached images, the dialog-inline variant, ⌘/Ctrl+Enter and Escape, and the narrow viewport.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--default"],
        "note": "An anchored empty draft: the textarea takes the caret on open, the placeholder names the note, Add comment is genuinely disabled and no image row renders."
      },
      {
        "name": "draft",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--draft"],
        "note": "Typing emits update:modelValue, unblocks Add comment, and clicking it emits save for the parent to commit — the popover stays anchored because closing is the parent's job."
      },
      {
        "name": "images",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--images"],
        "note": "Attached images render as thumbs and each remove control emits removeImage with the index to drop."
      },
      {
        "name": "inline",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--inline"],
        "note": "The inline prop keeps the compose inside the dialog's DOM instead of teleporting to body, where a Reka modal would make it visible but unusable."
      },
      {
        "name": "keyboard",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--keyboard"],
        "note": "⌘/Ctrl+Enter saves and Escape cancels through the popover's own keydown handler; the anchored box survives both because the parent decides the close."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["comment-compose-popover--narrow"],
        "note": "The teleported box obeys the ≤640px phone placement in the narrow workshop viewport: 8px gutters across the full width."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The popover loads nothing: draft text, images and the accent come from props and the seeded store."
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
    "variants": [
      "truncated"
    ],
    "variantNotes": "Read-only VueFinder over one GET /api/workspace-dir; initialPath/chatId are data props, not variants. Truncation, the held initial listing and the failure alert are states; navigation and file opening are real component+VueFinder flows.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["folder-browser--default"],
        "note": "A chat-less read lists the mounted VueFinder explorer (breadcrumb, entries) with the Loading line and the error alert away."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["folder-browser--loading"],
        "note": "The held initial read keeps the 'Loading…' status line and the explorer absent."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": ["folder-browser--load-error"],
        "note": "A failed listing names the folder in the .fbr-error alert ('Folder not found.'), with no half-mounted explorer."
      },
      {
        "name": "truncated",
        "coverage": "covered",
        "storyIds": ["folder-browser--truncated"],
        "note": "A truncated listing announces 'Showing the first 2 items.' as a status line over the entries it did answer."
      },
      {
        "name": "navigate",
        "coverage": "covered",
        "storyIds": ["folder-browser--navigate"],
        "note": "Entering a folder re-reads the sub listing through the driver (second declared read) and re-emits navigate with the absolute path; the old entries leave."
      },
      {
        "name": "open-file",
        "coverage": "covered",
        "storyIds": ["folder-browser--open-file"],
        "note": "Double-clicking a file emits open-file with its absolute path and no directory read."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is one GET /api/workspace-dir answered by a text 'Loading…' status line (not the mounted explorer geometry, so a loading-geometry follow-up applies) and failures become the alert line. Refreshes run through the driver: VueFinder's own menu item re-lists with its linear indicator, and the hosting tile's Refresh remounts the browser wholesale."
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
        "coverage": "covered",
        "storyIds": [
          "home-intake--default"
        ],
        "note": "<template> with no staged items and no model request: the composer, the workspace project chip and the New chat control."
      },
      {
        "name": "draft",
        "coverage": "covered",
        "storyIds": [
          "home-intake--draft"
        ],
        "note": "An unsent prompt, which the Send control names; a workspace switch keeps each workspace's draft."
      },
      {
        "name": "attachments",
        "coverage": "covered",
        "storyIds": [
          "home-intake--attachments"
        ],
        "note": "Staged files render in the Attachments list and each one has a remove control."
      },
      {
        "name": "action-error",
        "coverage": "covered",
        "storyIds": [
          "home-intake--action-error"
        ],
        "note": "The share-error alert, set from the ?share-error query on mount. A failed start reports through the app toast host, which this owner does not render, so it has no owner DOM here."
      },
      {
        "name": "incoming-share",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The shared-content banner needs a shared item in IndexedDB keyed by the ?shared query. The workshop does not seed that store, so the banner cannot render in isolation."
      },
      {
        "name": "model-error",
        "coverage": "covered",
        "storyIds": [
          "home-intake--model-error"
        ],
        "note": "The model list failed on its first request: the alert names the failure and offers Retry."
      },
      {
        "name": "models-loading",
        "coverage": "covered",
        "storyIds": [
          "home-intake--models-loading"
        ],
        "note": "The model list request is in flight: the status line reads Loading new chat models."
      },
      {
        "name": "models-loaded",
        "coverage": "covered",
        "storyIds": [
          "home-intake--models-loaded"
        ],
        "note": "The model list loaded: the status line is visually hidden and reads that availability may be incomplete."
      },
      {
        "name": "models-empty",
        "coverage": "covered",
        "storyIds": [
          "home-intake--models-empty"
        ],
        "note": "The model list loaded with no models: the status line reads that availability is unknown."
      },
      {
        "name": "model-stale",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "A refresh failure after a loaded list needs a second model fetch after the first one succeeded. This slice does not sequence that pair of responses, so the stale-with-failure line is not covered."
      },
      {
        "name": "starting",
        "coverage": "covered",
        "storyIds": [
          "home-intake--starting"
        ],
        "note": "A send is in flight: the start control reads Opening, is disabled, and the prompt is locked."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "home-intake--narrow"
        ],
        "note": "The composer inside a 390px wrapper, with the form no wider than the wrapper."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial model loading is models-loading, loaded is models-loaded, empty is models-empty and initial failure is model-error. Refresh failure with a kept list is model-stale and is uncovered. The model status line is visually hidden once loaded and visible while loading or failed, so its placeholder geometry differs from the loaded geometry. Recorded for the loading follow-up; not fixed here."
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
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--default"
        ],
        "note": "<template> with a memory proposal row to review; the other sources are current and drop out of the list."
      },
      {
        "name": "checking",
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--checking"
        ],
        "note": "Sources not yet loaded: the rows read Checking and carry the loading class."
      },
      {
        "name": "current",
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--current"
        ],
        "note": "Every source loaded with nothing to review: the owner renders only the Nothing to review line."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--stale"
        ],
        "note": "A refresh failed after a successful load: the last rows stay and carry the stale class with the last-load note."
      },
      {
        "name": "empty",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "Empty and current are one branch: with no visible rows the template renders only the Nothing to review line, which the current state covers. There is no separate empty copy."
      },
      {
        "name": "failed",
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--failed"
        ],
        "note": "The first load failed with no rows: the row shows the error with the attention class."
      },
      {
        "name": "automation-attention",
        "coverage": "covered",
        "storyIds": [
          "home-review-summary--automation-attention"
        ],
        "note": "A failed automation in the active workspace: the row carries the attention class and names the problem."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading is checking, initial failure is failed and a refresh failure that keeps rows is stale. Loading rows use the loaded row markup; placeholder geometry was not measured in this slice."
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
    "variantNotes": "No configurable variants: one toast shape whose action follows the toast's fields (an info action, an external link, an error fix, a fix route or a restore-draft action).",
    "states": [
      {
        "name": "info",
        "coverage": "covered",
        "storyIds": ["in-app-toast--info"],
        "note": "Info toast with title, body and Dismiss; a toast that offers nothing else has no action button."
      },
      {
        "name": "action",
        "coverage": "covered",
        "storyIds": ["in-app-toast--action"],
        "note": "Info toast with an in-app action button; clicking it runs the callback and dismisses the toast."
      },
      {
        "name": "link",
        "coverage": "covered",
        "storyIds": ["in-app-toast--link"],
        "note": "Info toast with an external link that opens in a new tab with noopener noreferrer."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": ["in-app-toast--error"],
        "note": "Error toast is an alert with the error border and the default Fix this error action."
      },
      {
        "name": "fix-in-settings",
        "coverage": "covered",
        "storyIds": ["in-app-toast--fix-in-settings"],
        "note": "An error whose remediation lives in Settings: the button uses fixLabel, dismisses, and routes to fixRoute."
      },
      {
        "name": "restore-draft",
        "coverage": "covered",
        "storyIds": ["in-app-toast--restore-draft"],
        "note": "An error carrying restoreDraft labels its action Restore draft; the story proves the label and Dismiss."
      },
      {
        "name": "restore-action",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Clicking Restore draft calls store.restoreDraft, which creates a chat over the API; the fail-closed fixture declares no chat-write routes, so the click is exercised by the component tests instead."
      },
      {
        "name": "dismiss",
        "coverage": "covered",
        "storyIds": ["in-app-toast--dismiss"],
        "note": "The close control removes the toast; removing the last toast removes the stack itself."
      },
      {
        "name": "swipe",
        "coverage": "covered",
        "storyIds": ["in-app-toast--swipe"],
        "note": "A horizontal drag past the dismiss threshold hides the toast; the toast follows the pointer while swiping."
      },
      {
        "name": "under-modal",
        "coverage": "covered",
        "storyIds": ["in-app-toast--under-modal"],
        "note": "While any aria-modal dialog is in the document the stack drops below it instead of painting over it."
      },
      {
        "name": "workspace-scope",
        "coverage": "covered",
        "storyIds": ["in-app-toast--workspace-scope"],
        "note": "Only toasts for the active workspace and account render; another workspace's chat or profile toast stays hidden."
      },
      {
        "name": "narrow",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The stack is position: fixed and already becomes full width under a 600px viewport; a wrapper cannot constrain a fixed element, so its own viewport rule is the narrow layout."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Renders only the toasts the project store already holds and never fetches, so it has no loading or empty treatment of its own."
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
    "kind": "component",
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
        "name": "closed",
        "coverage": "covered",
        "storyIds": ["item-peek-host--closed"],
        "note": "No peek target: the host mounts nothing, so no dialog or overlay is drawn. The story mounts the production host through mountConnected with an empty peek store."
      },
      {
        "name": "task-open",
        "coverage": "covered",
        "storyIds": ["item-peek-host--task-open"],
        "note": "Task target mounts TaskWorkspaceSurface in overlay mode: a modal dialog with the board editor drawn over the view. Its close ends the target and the host unmounts it; the play closes it and asserts no dialog remains."
      },
      {
        "name": "task-open-loading",
        "coverage": "covered",
        "storyIds": ["item-peek-host--task-open-loading"],
        "note": "Task target whose locate read is held: the overlay shows the compact Loading task… status sheet and no editor body."
      },
      {
        "name": "task-open-failed",
        "coverage": "covered",
        "storyIds": ["item-peek-host--task-open-failed"],
        "note": "Task target whose locate read fails: the overlay shows Could not load task with the server message and a Retry button; Retry draws the editor."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial: the Loading task… sheet (task-open-loading story). Failure: Could not load task with Retry (task-open-failed story). Both are drawn by TaskWorkspaceSurface in overlay mode. The host has no refresh or stale state of its own, so none is listed."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
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
    "variantNotes": "Initial loading/failure/stale/empty editor/validation/action error. Detailed variants are unassessed.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--default"
        ],
        "note": "The loaded registry: the not-loading, not-failed branch at MemoryCategoriesPanel.vue:362. Play blocks the core switch (disabled, aria-disabled), toggles the custom switch to an Unsaved draft and saves, asserting the aria-checked flip and the server-adopted Saved state."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--loading"
        ],
        "note": "Held first read: catLoading renders the SkeletonLoader rows variant (count 6) with its Loading categories status and no table."
      },
      {
        "name": "initial-error",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--initial-error"
        ],
        "note": "catFailed: the role=alert failure card with Retry; play retries and the fixture refuses again, so the alert persists over two reads."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--stale"
        ],
        "note": "The refresh branch: loadError over rows already on screen keeps the table with the role=status stale banner and Retry; play switches workspaces through the fixture and retries while the banner persists."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--empty"
        ],
        "note": "catEmpty: the This vault has no categories yet hint with Add category available and Save blocked at Saved."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--saving"
        ],
        "note": "A held PATCH: Save reads Saving… and is genuinely disabled, not styled; the draft keeps the user's toggles."
      },
      {
        "name": "save-error",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--save-error"
        ],
        "note": "A refused PATCH: the role=alert action error in MemoryCategoriesPanel.vue:382 leaves the draft exactly as built (Unsaved, Save changes enabled) for a corrected retry."
      },
      {
        "name": "drawer-edit",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--drawer-edit"
        ],
        "note": "The row-name button opens the drawer on a custom row: focus lands on Label, the id is readonly with its fixed-once note, Delete category is offered, Escape closes."
      },
      {
        "name": "drawer-add",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--drawer-add"
        ],
        "note": "Add category opens the empty drawer: focus on the writable id, Start from presets, a non-preset id shows the inline role=alert and blocks Save, a valid id plus label unblocks it."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "memory-categories-panel--narrow"
        ],
        "note": "Loaded in a maximum 390px container: the table keeps its columns and scrolls sideways in the Categories region with the switches reachable."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is the SkeletonLoader rows variant (count 6) whose bar-shaped boxes do not match the loaded actions row plus six-column table geometry (recorded for a loading follow-up; not fixed here). A workspace switch re-reads in place, a failed refresh keeps the rows with the stale banner, a failed first read shows the alert with Retry, and a held write blocks Save as Saving…. Covered: loading, initial-error, stale, saving."
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
    "variantNotes": "No configurable props: geometry comes from the guide content (over-cap flag, warn bars, expiry tags) and the workspace watcher.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--default"],
        "note": "The loaded read fills the v-else-if=\"guideStats\" region list: two region meters under cap, the token note and enabled Open/Discuss; the play opens the guide through the viewer store and Discuss fails closed in the toast stack."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--loading"],
        "note": "The held first read keeps guideStats null and shows the loading flag (the v-else-if=\"guideLoading\" span in the title line); both actions stay disabled until it answers."
      },
      {
        "name": "over-cap",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--over-cap"],
        "note": "A region over its advisory cap renders the 'over cap' flag (v-if=\"guideOverCap\"), the section's guide-budget--over class and the count/bar warn classes of that region's meter."
      },
      {
        "name": "high-usage",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--high-usage"],
        "note": "A region at 80% or more under cap renders the guide-region-bar--high tint without the over-cap flag or a warn count."
      },
      {
        "name": "expired-and-malformed",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--expired-and-malformed"],
        "note": "Entries with expiry tags fill the meta line's v-if=\"r.expiredCount\" span and the v-if=\"r.malformedCount\" span with its guide-region-meta--warn class."
      },
      {
        "name": "legacy-claude-guide",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--legacy-claude-guide"],
        "note": "The pre-migration lookup: a 404 on the workspace-qualified AGENTS.md resolves the legacy CLAUDE.md candidate, so the label and both actions name and act on that file."
      },
      {
        "name": "no-guide",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--no-guide"],
        "note": "All four candidate probes 404 (qualified AGENTS.md and CLAUDE.md, then the bare names): the silent 'No guide file found for this workspace.' rail note, the label placeholder and both actions blocked."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--error"],
        "note": "A failed qualified read renders the last error through v-if=\"guideError\" and blocks both actions; the owner stops before the bare fallback names once a qualified probe errored, so they stay undeclared routes the story must never see requested."
      },
      {
        "name": "refresh",
        "coverage": "covered",
        "storyIds": ["memory-guide-budget--refresh"],
        "note": "The workspace watcher re-reads: a held switched-workspace read keeps the previous regions on screen with the loading flag in the slot and both actions blocked."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "No skeleton: initial loading and refresh reuse the loaded rail card — title, label and both buttons — with the loading flag where the over-cap flag would sit; the region list is the only absence and a refresh keeps prior content. A failed read renders the error note instead of stale numbers, and an empty workspace is the silent no-guide state. Initial loading, refresh and the error render are covered; no loading geometry mismatch to fix."
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
    "variantNotes": "No configurable props: the only state is the per-browser disclosure choice; content is static.",
    "states": [
      {
        "name": "collapsed",
        "coverage": "covered",
        "storyIds": ["memory-how-it-works--collapsed"],
        "note": "The v-show=\"open\" body starts display:none and outside the tab order with aria-expanded false; the keyboard path Tab-Enter opens it and Tab reaches the Categories RouterLink."
      },
      {
        "name": "expanded",
        "coverage": "covered",
        "storyIds": ["memory-how-it-works--expanded"],
        "note": "The open body's content: the six-row tag table with its sr-only caption and the off-site noopener link; the toggle collapses and re-opens it and stores each choice per browser."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["memory-how-it-works--narrow"],
        "note": "A 390px viewport shows the compact table paddings from the owner's @media (max-width: 640px) branch, with unsplit tags and the keyboard path intact."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static explainer with no fetch and no asynchronous state; the open choice persists in the per-browser 'ciao.memory.howOpen' storage entry."
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
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--default"
        ],
        "note": "The store's default section (To decide): the review wrap's filter chips over the two queues, with zero chips dropped and All pressed, plus the never-claims static lines of the section. Queue bodies belong to their own component rows."
      },
      {
        "name": "graph",
        "coverage": "covered",
        "storyIds": [
          "memory-graph--graph"
        ],
        "note": "The map section's drawing: the graph story mounts the production owner, asserts the toolbar segment, the vault stats with the unchecked link and the zoom-controls group, and plays through the label-gate hint flip."
      },
      {
        "name": "list",
        "coverage": "covered",
        "storyIds": [
          "memory-list-selection--list"
        ],
        "note": "The list rendering of the same map section: sortable headers with aria-sort, the dash placeholder for an undated note and the stale row's Needs review flag; play sorts by Checked."
      },
      {
        "name": "selection",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--selection-detail",
          "memory-graph--selection",
          "memory-list-selection--selection"
        ],
        "note": "A selected note opens the docked tile (role=dialog naming the note, linked-note controls, the resize gutter is a viewport drag and has no story markup): covered opening from the graph path (production requestFocus), from the list's title buttons, and swapping to a neighbour inside the tile."
      },
      {
        "name": "filter",
        "coverage": "covered",
        "storyIds": [
          "memory-list-selection--filter"
        ],
        "note": "The map section's orphan filter: Orphans only narrows the list to the unlinked note and re-frames the drawing."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--loading"
        ],
        "note": "Held vault graph read: the toolbar and canvas unmount and the map body is the SkeletonLoader graph variant with its Loading vault graph status."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--error"
        ],
        "note": "A failed first graph read: the mm-empty line names the failure with neither skeleton nor drawing."
      },
      {
        "name": "detail-stale",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--detail-stale"
        ],
        "note": "The tile's stale callout: Unchecked for… with the type's horizon rule and the Review in To decide link to /memory/review?show=revisit."
      },
      {
        "name": "detail-pending",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--detail-pending"
        ],
        "note": "The tile's pending-check callout: Being checked. with the Open the proposal link to /memory/review?show=suggested; the stale callout is suppressed while the question is in flight."
      },
      {
        "name": "detail-conflicted",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--detail-conflicted"
        ],
        "note": "The tile's conflicted callout: A verification is out of date for this note. with the Review in To decide link; the dead verdict is never applied."
      },
      {
        "name": "detail-facts",
        "coverage": "covered",
        "storyIds": [
          "memory-map-view--detail-facts"
        ],
        "note": "The tile's entry-coverage block: Facts inside this note with the overdue bullet's Never checked reason and excerpt."
      },
      {
        "name": "categories-section",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The categories render is the page grid around MemoryCategoriesPanel alone; the component row components-memory-categories-panel carries its rendered-state coverage."
      },
      {
        "name": "import-section",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The import render is the page grid around ImportSources alone; the component row components-import-sources carries its rendered-state coverage."
      },
      {
        "name": "history-section",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The history render shows ProposalReviewPanel in its history section; that panel's component row owns the rendered states."
      },
      {
        "name": "retired-section",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The retired render shows VaultReviewPanel in its trash section; that panel's component row owns the rendered states."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The first vault read holds the map body behind the SkeletonLoader graph variant whose doodle geometry does not match the real canvas drawing (recorded for a loading follow-up; not fixed here). A workspace already in the store's snapshot cache paints without a skeleton and revalidates in the background, a background failure keeps the graph (no stale branch), and a foreground failure is the error line. Covered: loading, error."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    },
    "rationale": "Connected route composition mounted isolated through the fail-closed fixture boundary; its rendered states claim its own stories on this row (composition rows may claim story coverage), and its section routes are covered by their child component rows."
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
    "variantNotes": "No configurable variants: one row shape parametrised by tone, alert line, notice key and menu items.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["needs-you-row--default"],
        "note": "Title, line and primary control; a row with no extra items has no overflow trigger and stays a focus target rather than a tab stop."
      },
      {
        "name": "alert",
        "coverage": "covered",
        "storyIds": ["needs-you-row--alert"],
        "note": "An alert line is announced as an alert and clamps to two lines instead of truncating at one."
      },
      {
        "name": "error-tone",
        "coverage": "covered",
        "storyIds": ["needs-you-row--error-tone"],
        "note": "Error tone adds the marker and the screen-reader prefix Error:, never colour alone."
      },
      {
        "name": "blocking-tone",
        "coverage": "covered",
        "storyIds": ["needs-you-row--blocking-tone"],
        "note": "Blocking tone uses the same marker with the Blocking: prefix and no error styling."
      },
      {
        "name": "menu",
        "coverage": "covered",
        "storyIds": ["needs-you-row--menu"],
        "note": "Menu items add the ... trigger with aria-haspopup; the menu itself stays closed until asked for."
      },
      {
        "name": "menu-open",
        "coverage": "covered",
        "storyIds": ["needs-you-row--menu-open"],
        "note": "Opening links aria-controls to the menu, labels it, lists every item and moves focus to the first enabled one; an href item opens away from the app."
      },
      {
        "name": "menu-keyboard",
        "coverage": "covered",
        "storyIds": ["needs-you-row--menu-keyboard"],
        "note": "Enter opens the menu; arrow, Home and End move between enabled items; Escape closes it and returns focus to the trigger."
      },
      {
        "name": "menu-disabled-item",
        "coverage": "covered",
        "storyIds": ["needs-you-row--menu-disabled-item"],
        "note": "A disabled item is marked aria-disabled, does nothing when clicked and leaves the menu open."
      },
      {
        "name": "notice-target",
        "coverage": "covered",
        "storyIds": ["needs-you-row--notice-target"],
        "note": "A notice key makes the row the inbox's reveal target; it can take focus and is not hidden from assistive tech."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["needs-you-row--narrow"],
        "note": "Under the row's own container query the text takes the full width and the primary control stretches beneath it."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Renders entirely from props; it issues no request and has no loading or empty treatment of its own."
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
      "trigger"
    ],
    "variantNotes": "Popover or sheet, with or without its own trigger when another control opens it; the count badge and partial ring are states of that trigger.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["notification-bell--default"],
        "note": "With nothing to report the button opens a dialog, reports aria-expanded false and shows neither a count nor a ring."
      },
      {
        "name": "count",
        "coverage": "covered",
        "storyIds": ["notification-bell--count"],
        "note": "A count renders as a decorative badge while the accessible name carries the number and the item word."
      },
      {
        "name": "count-overflow",
        "coverage": "covered",
        "storyIds": ["notification-bell--count-overflow"],
        "note": "Past 99 the badge reads 99+ while the accessible name keeps the exact count."
      },
      {
        "name": "partial",
        "coverage": "covered",
        "storyIds": ["notification-bell--partial"],
        "note": "A stale or unavailable source adds the hollow ring and the words Some sources could not be fully checked without inventing a count."
      },
      {
        "name": "popover",
        "coverage": "covered",
        "storyIds": ["notification-bell--popover"],
        "note": "The popover is a labelled dialog teleported to the body; opening repeats the count and focuses its heading, and Close returns to the bell."
      },
      {
        "name": "escape",
        "coverage": "covered",
        "storyIds": ["notification-bell--escape"],
        "note": "Escape closes the open dialog and puts focus back on the bell that opened it."
      },
      {
        "name": "sheet",
        "coverage": "covered",
        "storyIds": ["notification-bell--sheet"],
        "note": "The sheet variant adds a backdrop that closes the dialog on click and still returns focus to the trigger."
      },
      {
        "name": "external-trigger",
        "coverage": "covered",
        "storyIds": ["notification-bell--external-trigger"],
        "note": "With trigger false no bell renders; another control calls the exposed open(anchor) and the sheet focuses its heading."
      },
      {
        "name": "navigate",
        "coverage": "covered",
        "storyIds": ["notification-bell--navigate"],
        "note": "Choosing a row closes the dialog and lets the row's link navigate to its notice."
      },
      {
        "name": "narrow",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The dialog is teleported to the body and position: fixed at min(380px, calc(100vw - 16px)); a 390px wrapper cannot constrain it, and that viewport rule is itself the narrow layout."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Sources still checking, or stale or unavailable, are named in the accessible name; only stale and unavailable add the hollow ring. The dialog's own list states are audited on NotificationInboxList."
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
      "popover",
      "sheet"
    ],
    "variantNotes": "Popover or sheet density; every other state is projected from the sources' health and the rows they produce.",
    "states": [
      {
        "name": "checking",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--checking"],
        "note": "Before any source answers the summary says it is checking, every source is listed as checking, and nothing claims the list is clear."
      },
      {
        "name": "ready",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--ready"],
        "note": "A healthy projection shows the summary, one section per populated group with its count, unread and attention markers, and no source list."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--stale"],
        "note": "A stale source keeps its last rows but the summary says they could not be fully checked, each row says May be out of date, and the source offers Retry."
      },
      {
        "name": "unavailable",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--unavailable"],
        "note": "A source that never answered is never reported as clear: the summary says nothing is confirmed and the source row offers Retry."
      },
      {
        "name": "all-clear",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--all-clear"],
        "note": "No rows and a ready overall status says You're all caught up with no source list and no empty sections."
      },
      {
        "name": "empty-stale",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--empty-stale"],
        "note": "No rows with a source out of date says only that nothing was found at the last check, not that everything is clear."
      },
      {
        "name": "retry-busy",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--retry-busy"],
        "note": "While a retry is in flight every Retry is disabled, so a second read cannot start from the same list."
      },
      {
        "name": "row-gone",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--row-gone"],
        "note": "Opening an exact row re-reads its source first; a row that vanished leaves the list, is announced as no longer available, and does not navigate."
      },
      {
        "name": "row-navigates",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--row-navigates"],
        "note": "A row without its own re-read navigates directly to its notice target."
      },
      {
        "name": "sheet",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--sheet"],
        "note": "Sheet density raises each row to the larger touch target."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["notification-inbox-list--narrow"],
        "note": "In a 390px column the summary and every row stay inside the width; the rows keep their title, reason, time and state stacked."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is checking (every source listed as checking, no skeleton or spinner); loaded is ready or all-clear; empty is all-clear, or empty-stale when a source is out of date; initial failure is unavailable; refresh is retry-busy, which disables every Retry; stale is stale or empty-stale. The list has no filter, so there is no filtered-empty state. Placeholder geometry is a text summary, not a skeleton, so it is not compared with the loaded layout."
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
      "folder",
      "image",
      "pdf",
      "pptx",
      "html",
      "csv",
      "markdown",
      "frontmatter",
      "editing",
      "return-folder",
      "comment-popover",
      "comment-draft",
      "close-label",
      "close-disabled",
      "narrow"
    ],
    "variantNotes": "Docked file owner over the shared annotation system. Kind branches: text (plain pre lines, markdown, frontmatter properties, CSV), image (plus its load-failure alert), PDF iframe (the LibreOffice notice covers .pptx), HTML artifact (preview/code) and the docked FolderBrowser; the Edit save flight (textarea/CSV); comment draft compose and read popover; return-folder Back; the memory-map note-tile props (inMemoryMap/close-label) and closeDisabled; the menu Refresh; narrow.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--default"],
        "note": "A loaded text file pinned as the chat tile: filename title with the folder hint, Edit, the three pre lines and the Unpin file close control."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--loading"],
        "note": "The file read held in flight renders the .pfp-skeleton (role=status, aria-busy) and actually disables Edit; nothing replaces the body."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--error"],
        "note": "A failed read (404 here) replaces the body with the .pfp-error line ('File not found.'), Edit stays blocked and the tile stays closable. Other statuses map to the same branch's sentences (Forbidden/too large/unsupported)."
      },
      {
        "name": "folder",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--folder"],
        "note": "A trailing-slash pin renders the docked FolderBrowser (pfp-body-folder): the browser mounts over one declared listing, Edit is withheld and the close control reads Unpin folder."
      },
      {
        "name": "return-folder",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--return-folder"],
        "note": "Docking a pinnable file out of the browser resolves the dock (declared file-path read) and the local pin write lands; after the parent's pin-prop propagation the tile switches to the file and the real pendingReturn bookkeeping offers 'Back to storybook-drafts', whose click rewrites the chat's pin to the folder. The parent-prop channel is the host's labelled story-repin button; everything else is production code."
      },
      {
        "name": "markdown",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--markdown"],
        "note": "A frontmatter-less .md renders the .pfp-md body; Edit stays available on text kinds."
      },
      {
        "name": "document-properties",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--document-properties"],
        "note": "YAML frontmatter renders the Document properties region (type/status/summary/tags) and drops the name that repeats the heading, with the markdown body under it."
      },
      {
        "name": "csv",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--csv"],
        "note": "A CSV path renders the read-only CsvViewer chunk (pfp-body-csv, meta bar, clickable cells for the comment system)."
      },
      {
        "name": "image",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Cannot render closed under the workshop fence: the img's error path probes the same URL the panel stamped with a Date.now() freshness tag at mount, so the exact declared route cannot be named ahead of the render and the probe would be recorded as a boundary violation."
      },
      {
        "name": "image-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Same blocker as image: the role=alert branch only appears after the <img> fails, and that failure always runs the fenced workspace-image probe whose timestamp tag the story cannot declare."
      },
      {
        "name": "pdf",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--pdf"],
        "note": "A .pdf renders the workspace-binary iframe with no Edit, and the LibreOffice notice stays away for non-pptx kinds."
      },
      {
        "name": "pptx-libreoffice",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--pptx-libreoffice"],
        "note": "A .pptx first reads /api/libreoffice-status; an unavailable soffice replaces the iframe with the warn notice and its Install in Chat action."
      },
      {
        "name": "html-preview",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--html-preview"],
        "note": "The artifact chunk mounts in Preview: the Preview/Code tab group (aria-pressed) over the frame, and the header withholds Edit while the source is unfetched."
      },
      {
        "name": "html-code",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--html-code"],
        "note": "Switching to Code fetches the declared source read and renders the code block; a loaded source is the one html branch where the header Edit appears."
      },
      {
        "name": "html-code-error",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--html-code-error"],
        "note": "A failed source read (HTTP 500) shows the artifact's error note and still withholds Edit (sourceLoaded stays false)."
      },
      {
        "name": "editing",
        "coverage": "covered",
        "storyIds": ["pinned-file-preview-editor--editing"],
        "note": "The Edit action swaps the body for .pfp-edit-shell's textarea: buffer starts from content, the caret lands in it, Cancel restores the preview without a write."
      },
      {
        "name": "editing-csv",
        "coverage": "covered",
        "storyIds": ["pinned-file-preview-editor--editing-csv"],
        "note": "Editing a CSV path renders CsvViewer in edit mode inside .pfp-edit-shell (labeled header/cell inputs, row add/delete); Cancel returns the read-only table."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": ["pinned-file-preview-editor--saving"],
        "note": "A save POST held by the boundary blocks both Cancel and Save ('Saving…'), the real disabled state not a styled one."
      },
      {
        "name": "save-error",
        "coverage": "covered",
        "storyIds": ["pinned-file-preview-editor--save-error"],
        "note": "A save answered HTTP 500 renders 'Save failed (HTTP 500).' in the editor shell and unblocks the controls without adopting the buffer."
      },
      {
        "name": "csv-save",
        "coverage": "covered",
        "storyIds": ["pinned-file-preview-editor--csv-save"],
        "note": "A successful save adopts the edited CSV: the tile returns to the read-only table with the added row, proving the POST round trip for the CSV editor."
      },
      {
        "name": "comment-popover",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--comment-popover"],
        "note": "A durable line comment highlights its .pre-line; clicking it pins the read popover (file · line quote, note, Edit/Delete) with the click-away backdrop, focus on Edit, and Escape closes through the popover's handler."
      },
      {
        "name": "comment-draft",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--comment-draft"],
        "note": "Selecting text floats the Comment trigger; clicking it opens the compose popover (focused, empty draft blocked) and saving stages the note durably on the file, re-highlighting the line."
      },
      {
        "name": "refresh",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--refresh"],
        "note": "The menu Refresh re-runs load(): the second declared read repaints the body and the header title stays on the pinned path."
      },
      {
        "name": "memory-map-tile",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--memory-map-tile"],
        "note": "The map's note tile passes inMemoryMap and close-label: the close control reads 'Close note' and is never desktop-only, and the file menu drops Open in memory map (it would reopen the page the tile is on)."
      },
      {
        "name": "close-disabled",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--close-disabled"],
        "note": "While a pin write is in flight ChatLayout passes closeDisabled and the close control is genuinely disabled with aria-disabled."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["pinned-file-panel--narrow"],
        "note": "The docked tile is in the layout flow, so the story wraps it in the 390px block: the tile fills the wrapper and the body does not overflow horizontally with short content."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is one GET /api/workspace-file (held in flight → the .pfp-skeleton role=status grid; failure → the .pfp-error line), so unlike the sibling modal the tile has its own real skeleton geometry. Refresh is the menu's re-read and replaces the loaded body with the skeleton again, and the stream-end/auto-reload watcher defers the same reload while the user is editing or commenting. Folder pins delegate loading to the browser's 'Loading…' line; image/PDF kinds have none. The refresh-replaces-content behavior is flagged for the loading follow-up (DESIGN's first-load rule keeps data on a refresh)."
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
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--default"
        ],
        "note": "ProposalHistoryList.stories.ts Default: one accepted and one dismissed decision with the production row layout."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--narrow"
        ],
        "note": "Viewport state: the decision list inside a 390px containing block."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--loading"
        ],
        "note": "SkeletonLoader (label \"Loading history\") while the first history read is held."
      },
      {
        "name": "initial-error",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--initial-error"
        ],
        "note": "historyError with no loaded page: role alert and no empty-state claim."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--stale"
        ],
        "note": "Seeded page with a failing refresh: rows stay on screen under the role alert."
      },
      {
        "name": "filtered-empty",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--filtered-empty"
        ],
        "note": "filtersHideEverything branch: the action filter hides every loaded decision."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--empty"
        ],
        "note": "Successful read with no decisions: \"No decisions yet.\" copy."
      },
      {
        "name": "load-more",
        "coverage": "covered",
        "storyIds": [
          "proposal-history-list--load-more"
        ],
        "note": "historyCanLoadMore branch: the server holds more decisions, so the \"show more\" control is offered (not clicked)."
      },
      {
        "name": "change-disclosure",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "toggleChange(row) receipt disclosure (Show change, receipt diff, undo); needs a receipt GET fixture, not rendered in this slice."
      },
      {
        "name": "verify-note-edit",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "row.note_edit verification details (before and after text, evidence); not rendered in this slice."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading shows the generic SkeletonLoader lines (label Loading history, 5 rows, ProposalHistoryList.vue:295); loaded decisions are multi-line rows with actor, time, text and a reason line, so the placeholder geometry does not match the loaded geometry. Recorded for the loading follow-up; the owner is not changed here. Initial failure (no page) and stale refresh (rows kept under the error) are both covered."
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
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--default"
        ],
        "note": "ProposalReviewPanel.stories.ts Default: the queue with two suggested memory rows, their previews and the row actions from the production template."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--loading"
        ],
        "note": "Queue skeleton (SkeletonLoader cards, label \"Loading proposals\") while the first GET /api/proposals is held; the queueLoading branch."
      },
      {
        "name": "initial-error",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--initial-error"
        ],
        "note": "queueFailed branch: no snapshot and a failed first read, role alert with Retry."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--stale"
        ],
        "note": "Seeded snapshot with a failing refresh: the pr-stale status keeps the rows under \"Could not refresh\" with Retry."
      },
      {
        "name": "filtered-empty",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--filtered-empty"
        ],
        "note": "filtersHideEverything branch: a search that matches none of the scoped rows, with Clear filters."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--empty"
        ],
        "note": "queueEmpty branch: a successful read with nothing in scope, \"All reviewed.\"."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--narrow"
        ],
        "note": "Viewport state: the default queue inside a 390px containing block."
      },
      {
        "name": "selection",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--selection"
        ],
        "note": "showChecks branch after Select: row checkboxes on, one ticked, batch bar counts \"1 selected\"."
      },
      {
        "name": "preview-open",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--preview-open"
        ],
        "note": "isPreviewOpen branch: the row decision card opened from its accept verb, repeating the preview text."
      },
      {
        "name": "preview-error",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--preview-error"
        ],
        "note": "store.previewErrors branch: each row's destination read fails, row says it could not read the destination with Retry."
      },
      {
        "name": "history",
        "coverage": "covered",
        "storyIds": [
          "proposal-review-panel--history"
        ],
        "note": "section=\"history\" branch: the panel renders its ProposalHistoryList child instead of the queue."
      },
      {
        "name": "dismiss-confirm",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "isDismissOpen(row) branch renders the dismissal confirmation; no story clicks Dismiss, so it is not rendered in this slice."
      },
      {
        "name": "skill-row",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "isSkill(row) branch (skill evidence, implement action, lifecycle); no skill proposal fixture in this slice."
      },
      {
        "name": "rehome-row",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "isRehome(row) branch (move destinations and re-home bullets); no re-home fixture in this slice."
      },
      {
        "name": "batch-summary",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "store.lastBatchSummary block after a bulk action (per-destination counts); not rendered in this slice."
      },
      {
        "name": "conflict",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "store.conflictIds card alert (destination changed underneath the row); not rendered in this slice."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading shows the generic SkeletonLoader cards variant (3 cards, ProposalReviewPanel.vue:1740); the loaded rows are multi-line cards with a change tag, a file line and action buttons, so the placeholder geometry does not match the loaded geometry. Recorded for the loading follow-up; the owner is not changed here. Initial failure (no snapshot) and stale refresh (rows kept, pr-stale status) are both covered."
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
    "variantNotes": "No configurable variants: one card with an optional message that its caller has already normalised and capped.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["restart-notice--default"],
        "note": "The fixed card with the default drain message, a polite status role, the spinner frame, the restart tag and the wait prompt."
      },
      {
        "name": "custom-message",
        "coverage": "covered",
        "storyIds": ["restart-notice--custom-message"],
        "note": "A supplied message replaces the default without changing the rest of the card."
      },
      {
        "name": "long-message",
        "coverage": "covered",
        "storyIds": ["restart-notice--long-message"],
        "note": "An over-long message is shown as the capped display string and clamps to three lines inside the card's width."
      },
      {
        "name": "moved",
        "coverage": "covered",
        "storyIds": ["restart-notice--moved"],
        "note": "A remembered position is restored from local storage as explicit left/top, drops the centring transform and reads as moved."
      },
      {
        "name": "keyboard-move",
        "coverage": "covered",
        "storyIds": ["restart-notice--keyboard-move"],
        "note": "The card is focusable; an arrow key moves it by one step from its rendered box, keeps focus on it and stores the new position."
      },
      {
        "name": "drag",
        "coverage": "covered",
        "storyIds": ["restart-notice--drag"],
        "note": "A pointer drag follows the grab offset, marks the card as dragging, and stores the dropped position on release."
      },
      {
        "name": "narrow",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The card is position: fixed at min(400px, calc(100vw - 24px)); a 390px wrapper cannot constrain a fixed element, and that viewport rule is the narrow layout."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "It is the restart message itself: a live drain notice with an animated spinner, not a request-backed view with loading or empty states."
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
    "kind": "component",
    "owner": {
      "path": "web/src/components/TaskBoardView.vue",
      "anchor": "<template>"
    },
    "sources": [],
    "consumers": [
      "web/src/components/ChatLayout.vue"
    ],
    "variants": [],
    "variantNotes": "No configurable variants: the wrapper renders the shared TaskWorkspaceSurface alone and forwards open-sidebar, pull and pull-end to the host; board, editor, loading and card variants are TaskWorkspaceSurface's.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-board-view--default"],
        "note": "The route wrapper mounts the production TaskWorkspaceSurface as the board: the New task action and both cards draw through the #3267 boundary with declared routes only. Its own template adds no branches."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "No loading of its own: renders TaskWorkspaceSurface's loading, which the surface enumerates as initial loading (firstLoad placeholder lanes), refresh (a re-read on mount, workspace switch and tasks_changed), failure (loadFailed with the server's sentence and Retry), stale refresh (loadStale over retained rows with Retry) and refused writes (board.error with Reload)."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
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
        "coverage": "covered",
        "storyIds": ["task-note-feed--default", "task-note-feed--narrow"],
        "note": "Default: an agent note and a user note, newest first, each with its author and time, and a live link. Narrow draws the same feed at 390px with an unbroken token that wraps inside its bubble."
      },
      {
        "name": "edited",
        "coverage": "covered",
        "storyIds": ["task-note-feed--edited"],
        "note": "v-if item.edited_at: the ' · edited' mark follows the time in the meta line."
      },
      {
        "name": "no-recorded-time",
        "coverage": "covered",
        "storyIds": ["task-note-feed--no-recorded-time"],
        "note": "formatCompletedAt has no time for the note, so the meta line reads No time recorded."
      },
      {
        "name": "actions",
        "coverage": "covered",
        "storyIds": ["task-note-feed--with-actions"],
        "note": "The actions scoped slot renders in each note's meta line (the board's Edit button). WithActions presses the second note's Edit and the owner's handler opens its editor."
      },
      {
        "name": "editing",
        "coverage": "covered",
        "storyIds": ["task-note-feed--editing"],
        "note": "v-if editingId equals the note id: the editor slot replaces that note's rendered text only. Editing shows the field with the note's text, and Done editing restores the rendered text."
      },
      {
        "name": "empty",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The owner has no empty branch: zero notes draws an empty ordered list. Any empty copy belongs to the caller."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No fetch: the notes arrive as the updates prop from the owner. The owner's own loading state is audited at the components-task-workspace-surface row."
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
        "name": "no-origin",
        "coverage": "covered",
        "storyIds": ["task-origin-note--no-origin"],
        "note": "NoOrigin: no delegation and no link (v-if taskId || linkedTasks.length is false), so the root is not rendered."
      },
      {
        "name": "task-not-loaded",
        "coverage": "covered",
        "storyIds": ["task-origin-note--task-not-loaded"],
        "note": "TaskNotLoaded: a delegated chat whose task the signals store does not hold yet. The row names A task on the board with the Delegated task detail, and no status or controls."
      },
      {
        "name": "running",
        "coverage": "covered",
        "storyIds": ["task-origin-note--running"],
        "note": "Running: the warn status word for a running attempt, and Mark done… offered (canMarkDone). Approve Done is not offered."
      },
      {
        "name": "review-ready",
        "coverage": "covered",
        "storyIds": ["task-origin-note--review-ready"],
        "note": "ReviewReady: the task awaits review (canApprove), so the Review ready status and the Approve Done button with its aria-label are drawn, and Mark done… is not."
      },
      {
        "name": "closure-form",
        "coverage": "covered",
        "storyIds": ["task-origin-note--closure-form"],
        "note": "ClosureForm: noteOpen on the approve path. The optional note field takes focus, the approve consequence line is drawn, and Cancel returns focus to the Approve Done button."
      },
      {
        "name": "mark-done-form",
        "coverage": "covered",
        "storyIds": ["task-origin-note--mark-done-form"],
        "note": "MarkDoneForm: noteOpen on the override path. The note field takes focus, the mark-done consequence line and the Mark done and archive submit are drawn, and Cancel returns focus to Mark done…."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": ["task-origin-note--saving"],
        "note": "Saving: approving is set while the completion write is held. The submit reads Saving… and the note field and Cancel are disabled."
      },
      {
        "name": "refused",
        "coverage": "covered",
        "storyIds": ["task-origin-note--refused"],
        "note": "Refused: approveError draws .task-origin-error with role alert and the server sentence. The form stays open, the note field regains focus, and Approve Done is enabled again."
      },
      {
        "name": "superseded",
        "coverage": "covered",
        "storyIds": ["task-origin-note--superseded"],
        "note": "Superseded: a later attempt holds the task, so the muted A newer attempt holds this task status is drawn and no controls are."
      },
      {
        "name": "done",
        "coverage": "covered",
        "storyIds": ["task-origin-note--done"],
        "note": "Done: the task is done, so the done status pill is drawn and no controls are."
      },
      {
        "name": "linked-task",
        "coverage": "covered",
        "storyIds": ["task-origin-note--linked-only"],
        "note": "LinkedOnly: a linked task without delegation is a read-only task link with the Linked task detail, and no state or controls."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial: until taskSignals holds the chat's task, the row is the delegation stamp's fallback (task-not-loaded), whose title is the placeholder A task on the board and which has no status pill, so its geometry differs from the loaded row (title, status word, controls); a later loading follow-up should note it, and this slice does not fix the owner. Loaded: the states above. Empty: no-origin. The owner draws no refresh, failure or stale state of its own: the signals store keeps the last rows on a failed read and is not rendered here."
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
    "kind": "component",
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
    "variantNotes": "Actual inline board/lanes/card, editor, completion, delegation and move/ordering owner. Audited branches: firstLoad/loadFailed/loadStale/filteredEmpty/boardEmpty load notices, missingTaskId, orderNotice, unreadable rows, refused-write action error, the collapsed Done tab and the overlay mode's status sheet. Page vs overlay is the mode prop; board vs editor is board chrome vs the detail page. The inline families are audited at their own rows (task-board-cards, task-editor, task-completion, task-delegation, task-ordering); the board load states (firstLoad, loadFailed, loadStale), filteredEmpty, boardEmpty and ordering are covered by the TaskWorkspaceSurface stories; the rest of these surface states stay uncovered, and the inline families are claimed at their own rows.",
    "states": [
      {
        "name": "default",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The board-mode <template> with the loaded workspace's four lanes and cards; no rendered-state story exists."
      },
      {
        "name": "overlay",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "mode=\"overlay\" (taskId prop): no board chrome, only the editor sheet, an overlay status sheet while it locates its task (loading, failed, missing sub-states) or the delegation preview over the host screen; ItemPeekHost is the first caller; no rendered-state story exists."
      },
      {
        "name": "firstLoad",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--first-load"],
        "note": "FirstLoad renders the .task-loading role=status placeholder: three lanes, two placeholder cards each, a count bar per head and the Done toggle label, at a 390px width with equal lane widths; no real card or title."
      },
      {
        "name": "loadFailed",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--load-failed"],
        "note": "LoadFailed renders .task-failed role=alert with the server sentence and Retry, and no lane or empty-board hint."
      },
      {
        "name": "loadStale",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--load-stale"],
        "note": "LoadStale: a refused order write re-reads and the re-read fails; .task-stale role=status with Retry sits over the retained cards."
      },
      {
        "name": "filteredEmpty",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--filtered-empty"],
        "note": "FilteredEmpty: a due filter hides both tasks; .task-filtered-empty names the count with Clear filters, and Clear filters brings the cards back."
      },
      {
        "name": "boardEmpty",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--board-empty"],
        "note": "BoardEmpty: an empty list read draws the .task-empty hint naming New task and ciao task, with no lanes and no filter hint."
      },
      {
        "name": "missingTaskId",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the missingTaskId branch: .task-stale role=status when the linked task is not on this board, with Dismiss; no rendered-state story exists."
      },
      {
        "name": "orderNotice",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "TaskWorkspaceSurface.vue implements the orderNotice branch: .task-order-notice role=status when the order file could not be read, so cards keep status moves but cannot be placed and the menu Move actions say why; no rendered-state story exists."
      },
      {
        "name": "unreadable",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "v-if=\"unreadable.length\": .task-unreadable keeps rows the engine could not read as tasks visible with path, code badge and message instead of dropping them; no rendered-state story exists."
      },
      {
        "name": "action-error",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "v-if=\"board.error\": .task-action-error role=alert keeps the refused write's rows and shows the server's sentence with Reload, a separate slot from the load notices so a later list read cannot clear it; no rendered-state story exists."
      },
      {
        "name": "ordering",
        "coverage": "covered",
        "storyIds": ["task-workspace-surface--ordering"],
        "note": "Ordering: Move down on a card in a shared lane writes one placement, and the answered order draws the moved card below its neighbour."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "v-if=\"detailOpen\" without overlay draws the full editor as a page over the board with the title rail, save line and Delete; the editor sheet itself is audited at the task-editor row; no rendered-state story exists."
      },
      {
        "name": "completion",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The resolution sheet (.task-resolution-sheet) and the editor's Resolution section open from the board; audited at the task-completion row; no rendered-state story exists."
      },
      {
        "name": "delegation",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The delegation preview sheet and the editor's Agent block open from the board; audited at the task-delegation row; no rendered-state story exists."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "TaskWorkspaceSurface.vue firstLoad/loadFailed/loadStale distinguish initial loading, failure and stale refresh; stale rows are retained. Initial loading is the firstLoad placeholder; refresh re-reads on mount, workspace switch and tasks_changed while keeping rows; a refused write keeps rows and shows board.error with Reload. The first-load placeholder mirrors the loaded lane geometry (#2961): column heads with a count bar and two loading bars per lane, Done reduced to its toggle label while collapsed."
    },
    "migration": {
      "status": "unassessed",
      "note": "Inventory admission does not approve a design migration."
    }
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
        "coverage": "covered",
        "storyIds": [
          "terminal-key-row--default"
        ],
        "note": "Enabled keys. A pointer tap reports the key with pointer true and leaves focus where it was."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": [
          "terminal-key-row--disabled"
        ],
        "note": "Disabled until the screen is live: each key is disabled and a press is not reported."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": [
          "terminal-key-row--keyboard-focus"
        ],
        "note": "Focus on a key and Enter: the press is reported as a keyboard activation and focus stays on the key."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Props only: no fetch, no store and no loading state."
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
    "variantNotes": "Tile, drawer, chooser, connecting, reconnecting, takeover, ended, creating, truncated, unavailable, capacity and error. Connected stories use the real store with declared status, sessions and socket fixtures (synthetic storybook-alpha and storybook-beta names).",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--default"
        ],
        "note": "Live shell in the drawer shape. The real open reads status and sessions, a declared socket replays fixed bytes, and the play asserts the screen and that Shift+Tab hands the keyboard back to Close."
      },
      {
        "name": "tile",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--tile"
        ],
        "note": "Live shell with inTile set; the play asserts the tile class. The shape follows window width (a phone-width window is forced to the drawer), so this state depends on the runner window staying at desktop width."
      },
      {
        "name": "drawer",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--drawer"
        ],
        "note": "Live shell in a 390px wrapper; the drawer class is used whenever the pane is not in the tile slot."
      },
      {
        "name": "chooser",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--chooser"
        ],
        "note": "A chat with a workspace and a repository folder and no shell asks which to open; the play picks the repository and the new tab is selected."
      },
      {
        "name": "connecting",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--connecting"
        ],
        "note": "The socket opens and never sends the handshake; the pane shows Connecting to the terminal."
      },
      {
        "name": "reconnecting",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--reconnecting"
        ],
        "note": "A dropped connection is retried after a held status read, so the pane stays reconnecting; the request count proves the retry read happened. The phase shares the connecting line."
      },
      {
        "name": "takeover",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--takeover"
        ],
        "note": "Another connection takes the session (close 4409); the play clicks Take over and the takeover prompt goes while the shell comes back live. Known: the takeover notice stays as a stale alert after the reconnect because the store keeps it in its error ref; recorded, not fixed in this slice."
      },
      {
        "name": "ended",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--ended"
        ],
        "note": "The shell prints and exits with code 0; the pane shows the ended line and the tab reads ended (0)."
      },
      {
        "name": "creating",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--creating"
        ],
        "note": "The first shell is being created and the create request is held; the pane says it is starting and the new-session control is disabled."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--failure"
        ],
        "note": "The status read fails; the pane shows the could-not-ask alert and no session list."
      },
      {
        "name": "unavailable",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--unavailable"
        ],
        "note": "The engine reports the terminal unavailable (https_required); the alert shows and the session tabs are hidden."
      },
      {
        "name": "capacity",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--capacity"
        ],
        "note": "A workspace with four ended sessions: the capacity alert shows, the new-session control is disabled, and each ended session still has its End control."
      },
      {
        "name": "truncated",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--truncated"
        ],
        "note": "The ready frame says the engine lost output; the truncation notice shows while the shell stays live."
      },
      {
        "name": "no-shell",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--no-shell"
        ],
        "note": "A chat with no shell and nothing asked for one: the pane says none is open, and the request count proves nothing was created."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--loading"
        ],
        "note": "The status read is held. The pane shows the same no-shell line as an empty chat, because it has no separate loading indicator."
      },
      {
        "name": "closed",
        "coverage": "covered",
        "storyIds": [
          "terminal-pane--closed"
        ],
        "note": "Mounted but not opened: the open class is absent and no request is made."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load: the status read is held and the pane shows the no-shell line, not a loading indicator, so the placeholder geometry differs from the loaded screen. Initial failure is the error state. Refresh shows the connecting line (reconnecting phase). Empty is no-shell. There is no filtered-empty state."
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
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--default"
        ],
        "note": "VaultReviewPanel.stories.ts Default: the To revisit section with two candidate notes and their signals."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--narrow"
        ],
        "note": "Viewport state: the To revisit list inside a 390px containing block."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--loading"
        ],
        "note": "showInitialLoading branch: the candidates SkeletonLoader (label \"Loading candidates\") while the read is held."
      },
      {
        "name": "initial-error",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--initial-error"
        ],
        "note": "showInitialError branch: no snapshot and a failed read, role alert with Retry."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--stale"
        ],
        "note": "showStaleError branch: a seeded snapshot with a failing refresh keeps the candidates under the stale status."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--empty"
        ],
        "note": "hasCurrentSnapshot with no candidates: \"Nothing to revisit.\" copy."
      },
      {
        "name": "trash",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--trash"
        ],
        "note": "section=\"trash\" branch: one retired note listed with its original path."
      },
      {
        "name": "trash-empty",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--trash-empty"
        ],
        "note": "section=\"trash\" with a successful empty read: \"Nothing retired yet.\" copy."
      },
      {
        "name": "trash-loading",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--trash-loading"
        ],
        "note": "section=\"trash\" showInitialLoading branch: the retired-notes SkeletonLoader while the read is held."
      },
      {
        "name": "trash-error",
        "coverage": "covered",
        "storyIds": [
          "vault-review-panel--trash-error"
        ],
        "note": "section=\"trash\" showInitialError branch: role alert with the retired-notes copy."
      },
      {
        "name": "cleared-list",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "visibleCleared <details> list of notes cleared with \"Still true\"; not rendered in this slice."
      },
      {
        "name": "entry-findings",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "entryCoverageOf / entryGroups stale-entry and unverified-entry findings on a candidate; needs an entry-coverage fixture, not rendered in this slice."
      },
      {
        "name": "complete-project",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "isCompletable(candidate) branch that offers Complete on a project candidate; not rendered in this slice."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading shows the candidates SkeletonLoader cards variant (3 cards, VaultReviewPanel.vue:660) and, for Trash, a generic 4-line SkeletonLoader (VaultReviewPanel.vue:1093). Loaded candidates are cards with signal evidence and actions, and loaded trash rows are title and path lines, so both placeholders do not match the loaded geometry. Recorded for the loading follow-up; the owner is not changed here. Initial failure, stale refresh and the trash states are covered."
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
    "variants": [
      "primary",
      "secondary",
      "link",
      "disabled"
    ],
    "variantNotes": "One variant prop over one native button: secondary (the default) is the outlined rest, primary the filled continue action, link the accent text action; disabled is a fall-through attribute the owners pass while a save is in flight.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["onboarding-button--default"],
        "note": "The secondary default render: one native `type=\"button\"` at the 44px touch height carrying `data-variant=\"secondary\"`."
      },
      {
        "name": "primary",
        "coverage": "covered",
        "storyIds": ["onboarding-button--primary"],
        "note": "`variant=\"primary\"` sets `data-variant=\"primary\"`; the Shell's Continue is the production use."
      },
      {
        "name": "link",
        "coverage": "covered",
        "storyIds": ["onboarding-button--link"],
        "note": "`variant=\"link\"` drops the border and reads as accent text; Leave setup, Change and Change name are the production uses."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": ["onboarding-button--disabled"],
        "note": "Attributes fall through to the native button, so the owners' busy flags disable it (Shell busy, RuntimeStage loadingStatus and saving); the story asserts the control is actually blocked."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["onboarding-button--keyboard-focus"],
        "note": "`:focus-visible` draws the 2px accent outline; the story tabs to the button, asserts focus-visible and that Enter fires the click."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "A presentational native button: it runs no fetch and has no async of its own, so there is no initial, refresh, failure or stale render. The disabled look arrives from its owner's busy state."
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
    "variants": [
      "checked",
      "disabled"
    ],
    "variantNotes": "A native radio row: checked follows the group's model; disabled comes from its own prop or the group's disabled. The description slot is optional.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--default"
        ],
        "note": "Unchecked row with a plain title: no description element, data-checked and data-disabled both false. Rendered inside the production OnboardingChoiceGroup host."
      },
      {
        "name": "checked",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--checked"
        ],
        "note": "The group model matches the value: the native radio is checked and data-checked is true. Clicking the checked radio fires no change, and the group still re-selects it, so it cannot be toggled off."
      },
      {
        "name": "description",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--description"
        ],
        "note": "The description slot renders inside the title block (.ob-choice-desc) with its own text."
      },
      {
        "name": "select",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--select"
        ],
        "note": "A click on the second radio moves the group model; the owner's pick() re-syncs the native group so exactly one radio stays checked."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--disabled"
        ],
        "note": "The choice's own disabled prop: data-disabled is true and the native radio is disabled. A click does not check it, while an enabled sibling still works."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice--keyboard-focus"
        ],
        "note": "Tab lands on the checked radio with :focus-visible; ArrowDown moves focus to the next choice and checks it."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Presentational radio row: it runs no fetch and has no async state. Initial, refresh, failure and stale renders do not apply; the group's model supplies every value."
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
    "variants": [
      "list",
      "cards",
      "segmented",
      "disabled",
      "after"
    ],
    "variantNotes": "layout is list (default), cards or segmented, set on the radiogroup as data-layout; disabled reaches every choice through the group context; the after slot renders under the choices.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--default"
        ],
        "note": "List layout (the default): a role=radiogroup labelled by the group label, data-layout list, three choices with one checked."
      },
      {
        "name": "cards",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--cards"
        ],
        "note": "layout cards: data-layout cards on the radiogroup; the choices are the same native radios."
      },
      {
        "name": "segmented",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--segmented"
        ],
        "note": "layout segmented: one wrapping row with data-layout segmented; the checked choice carries data-checked."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--disabled"
        ],
        "note": "Group-level disabled: every native radio is disabled and blocked, and a click leaves the checked value unchanged."
      },
      {
        "name": "after-slot",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--after-slot"
        ],
        "note": "The after slot renders below the choices container, as guidance under the group."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": [
          "onboarding-choice-group--keyboard-focus"
        ],
        "note": "One tab stop for the group; ArrowDown walks the radios and each move fires select."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Presentational grouping: it renders its label and choices from props and runs no fetch. No initial, refresh, failure or stale render."
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
    "variants": [
      "help",
      "help-slot",
      "error",
      "below"
    ],
    "variantNotes": "help is the prop text and the help slot overrides it with rich markup; error adds the role=alert line and the invalid flag; below holds chips or suggestions under the control.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--default"
        ],
        "note": "Label and slotted control only: the help element is present but empty, there is no error line, and aria-describedby names the help element."
      },
      {
        "name": "help",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--help"
        ],
        "note": "The help prop fills the help element, which the control's aria-describedby names."
      },
      {
        "name": "help-slot",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--help-slot"
        ],
        "note": "The help slot replaces the help prop with rich markup: two paragraphs and a link."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--error"
        ],
        "note": "error renders a role=alert paragraph with its own id; aria-describedby lists help then error, and the slotted control gets aria-invalid=true."
      },
      {
        "name": "below",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--below"
        ],
        "note": "The below slot sits between the control and the help element."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": [
          "onboarding-field--keyboard-focus"
        ],
        "note": "Tab reaches the slotted input, which shows :focus-visible."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "A labelled control wrapper with no async state: the value and any error come from the stage. No initial, refresh, failure or stale render."
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
      "step",
      "busy",
      "canContinue",
      "conflict",
      "error",
      "summary"
    ],
    "variantNotes": "step picks the heading and the current step; busy, canContinue, conflict and error are props; the target kind picks the context line; the summary slot adds an aside.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--default"
        ],
        "note": "Installation context on the first step: heading, steps nav with one aria-current, Back, Leave setup and an enabled Continue; no alert and no aside."
      },
      {
        "name": "workspace-target",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--workspace"
        ],
        "note": "A workspace target reads Workspace with the workspace name on the context line."
      },
      {
        "name": "finish",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--finish"
        ],
        "note": "The last step relabels the primary control Finish setup and marks Finish as current."
      },
      {
        "name": "busy",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--busy"
        ],
        "note": "busy disables Back, Leave setup and Continue; no alert is shown."
      },
      {
        "name": "blocked",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--blocked"
        ],
        "note": "canContinue false disables only Continue; Back and Leave stay enabled."
      },
      {
        "name": "conflict",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--conflict"
        ],
        "note": "conflict shows the role=alert paragraph and an enabled Reload, which the host records."
      },
      {
        "name": "error",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--error"
        ],
        "note": "error shows the message in the alert with no conflict paragraph."
      },
      {
        "name": "conflict-error",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--conflict-error"
        ],
        "note": "Both flags at once: two paragraphs over one Reload."
      },
      {
        "name": "summary-slot",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--summary-slot"
        ],
        "note": "The summary slot renders an aside labelled Setup summary."
      },
      {
        "name": "step-focus",
        "coverage": "covered",
        "storyIds": [
          "onboarding-shell--step-focus"
        ],
        "note": "Back moves one step and the step heading takes focus (activeElement). The h1 keeps its outline hidden by design, so only the focus move is asserted, not a visible ring."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The shell loads nothing itself: initial, refresh and stale loading belong to the stage slot. Its in-flight render is busy (story Busy), which disables Back, Leave setup and Continue with no spinner. A failed save renders as the error alert (story Error); there is no stale render."
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
    "variants": [
      "sm",
      "md",
      "xl",
      "2xl",
      "section",
      "form"
    ],
    "variantNotes": "gap sets data-gap (sm, md by default, xl, 2xl); as renders a section or form instead of the default div.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--default"
        ],
        "note": "Default gap md on a div: data-gap md and the rendered element is a DIV."
      },
      {
        "name": "small",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--small"
        ],
        "note": "gap sm: data-gap sm."
      },
      {
        "name": "large",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--large"
        ],
        "note": "gap xl: data-gap xl."
      },
      {
        "name": "double-large",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--double-large"
        ],
        "note": "gap 2xl around a nested default stack: the row gap scales with the prop, so the outer stack's gap is larger than the inner one."
      },
      {
        "name": "section",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--section"
        ],
        "note": "as section with an aria-label, as the stages and the summary render it."
      },
      {
        "name": "form",
        "coverage": "covered",
        "storyIds": [
          "onboarding-stack--form"
        ],
        "note": "as form: its submit is prevented in place and recorded on the element."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "A spacing wrapper with no data or async state. No initial, refresh, failure or stale render."
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
    "variantNotes": "No configurable props: the start and default slots fill the two rows, and the phone layout is a CSS media query.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "onboarding-step-footer--default"
        ],
        "note": "Two rows: the start slot (Back) and the default slot (Leave setup, Continue), with 44px buttons."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": [
          "onboarding-step-footer--narrow"
        ],
        "note": "At a 390px viewport the rows stop wrapping (nowrap) with no horizontal overflow and the same 44px targets."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "A presentational footer with no data or async state. No initial, refresh, failure or stale render."
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
    "variants": [
      "loading",
      "ready",
      "deferred",
      "attention",
      "unknown"
    ],
    "variantNotes": "loading swaps the list for one status line; each item state is ready, deferred, attention or unknown; an in-app href adds a Review link and any other href is dropped.",
    "states": [
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--loading"
        ],
        "note": "Initial load: loading=true renders one role=status line, Checking setup, and no list."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--empty"
        ],
        "note": "Loaded with no rows: the list element renders with no rows and no status line."
      },
      {
        "name": "ready",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--ready"
        ],
        "note": "A ready row shows its label, its value, the ready marker and an in-app Review link."
      },
      {
        "name": "deferred",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--deferred"
        ],
        "note": "A deferred row shows the deferred marker with its value."
      },
      {
        "name": "attention",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--attention"
        ],
        "note": "An attention row shows the attention marker with its value."
      },
      {
        "name": "unknown-item",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--unknown"
        ],
        "note": "An unreadable source renders the unknown marker and its explanation, never a guessed value."
      },
      {
        "name": "review-link",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--review-link"
        ],
        "note": "An in-app href gains a Review link that is at least 44px tall."
      },
      {
        "name": "external-href",
        "coverage": "covered",
        "storyIds": [
          "onboarding-summary--external-href"
        ],
        "note": "A non-internal href renders no link at all."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial: loading=true shows one role=status line (Checking setup) instead of the list (story Loading). The owner has no refresh, failure or stale render of its own: an unreadable source arrives as an unknown row. The placeholder is one line while the loaded state is a list of rows, so the two geometries differ."
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
    "variantNotes": "No configurable variants: one open prop plus its draft state; the audited render variants are the row states below.",
    "states": [
      {
        "name": "closed",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--closed"],
        "note": "Props open=false, the production closed state: the portalled dialog renders nothing."
      },
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--default"],
        "note": "Open with the shipped defaults: both theme sections show a readable ratio, Apply is blocked until something is edited and Reset is enabled."
      },
      {
        "name": "seeded-custom-colours",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--seeded-custom-colours"],
        "note": "Open with a saved pair per mode: the hex fields and swatches show the saved pair, not the theme's own colours (seedFor prefers the saved pair)."
      },
      {
        "name": "edited-readable",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--edited-readable"],
        "note": "A typed valid hex updates the draft, the ratio line and enables Apply."
      },
      {
        "name": "unreadable",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--unreadable"],
        "note": "A valid hex with too little contrast renders the refusal reason in hint--warn on the section status and keeps Apply blocked."
      },
      {
        "name": "invalid-hex",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--invalid-hex"],
        "note": "Typed text that is not a colour stays as typed, the swatch falls back to the seed and Apply stays blocked."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--saving"],
        "note": "Apply while the write is held: Apply and Reset are actually disabled and Escape cannot close the dialog while a save is in flight (onOpenChange refuses)."
      },
      {
        "name": "save-failure",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--save-failure"],
        "note": "A failing Apply (PATCH 500) renders the role=alert error, the dialog stays open and Apply recovers."
      },
      {
        "name": "apply",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--apply"],
        "note": "Apply normalizes the edited pair, PATCHes the production save path (declared) and closes the dialog through update:open."
      },
      {
        "name": "reset",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--reset"],
        "note": "Reset saves custom_colors {} for both modes through the declared PATCH and closes the dialog."
      },
      {
        "name": "cancel",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--cancel"],
        "note": "Escape closes the portalled dialog through reka's DialogRoot and the singleton settings are left untouched."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["custom-colors-dialog--narrow"],
        "note": "The card in a maximum 390px container: both theme section headings keep their layout at the card's mobile width."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No fetch or loading branch on open: the dialog reads the shared appearance singleton and only Apply and Reset write (declared PATCH routes)."
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
    "variants": [
      "connect",
      "reauth"
    ],
    "variantNotes": "The open prop and the connect/reauth mode change the title, display-name field, setup steps, checklist, sign-in controls and footer; every audited branch is a row state below.",
    "states": [
      {
        "name": "closed",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--closed"],
        "note": "Props open=false: the modal backdrop (v-if=\"open\") renders nothing."
      },
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--default"],
        "note": "Connect mode with gws not installed and no profile: focused editable display name, install step, upload client panel, a disabled Sign-in control with its reason and a disabled Done with its reason; Escape closes."
      },
      {
        "name": "install-problem",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--install-problem"],
        "note": "The install_problem branch (gwsIntegration.install_problem) renders a role=alert inside the install step."
      },
      {
        "name": "install-done",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--install-done"],
        "note": "installed=true replaces the install step with its checklist entry and keeps the OAuth client step pending."
      },
      {
        "name": "client-done-ready",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--client-done-ready"],
        "note": "Client secret present but the profile not configured: both checklist entries with the Replace file action, the ready Sign-in and Paste-a-code controls and the profile's storage hint."
      },
      {
        "name": "client-gcloud",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--client-gcloud"],
        "note": "The gcloud radio shows the derived setup command, and Check again (declared POST profiles/add then GET) registers the profile and swaps in its stored name."
      },
      {
        "name": "connected",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--connected"],
        "note": "A configured profile linked to this workspace: the connected line with the workspace link, Done enabled, no sign-in controls; Done closes the dialog."
      },
      {
        "name": "expired-reauth",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--expired-reauth"],
        "note": "Reauth mode over a profile that needs re-login: the title, the expired warning with the cached token error, sign-in controls offered and Done still blocked."
      },
      {
        "name": "relogin-pending",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--relogin-pending"],
        "note": "The loopback wait status with Stop sign-in; the declared relogin/cancel write ends the wait and restores the sign-in buttons. Google OAuth start actions (Sign in, Paste a code) deliberately fail closed: their POSTs are undeclared, so the plays assert the controls instead of running them."
      },
      {
        "name": "relogin-pending-blocked",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--relogin-pending-blocked"],
        "note": "The blocked-tab variant of the wait: the Open authorization page link points at the fixture URL."
      },
      {
        "name": "paste-code-flow",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--paste-code-flow"],
        "note": "The paste flow: Complete Sign-In is blocked while the field is empty and the declared exchange write replaces the box with the connected line."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["google-connect-dialog--narrow"],
        "note": "Reauth state in a maximum 390px container."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No read or loading branch at open: everything arrives through the injected Google accounts state. Reads happen only on explicit actions (Check again), which the covered states declare; OAuth writes stay undeclared and fail closed."
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
    "variants": ["Claude Code", "OpenCode", "Codex"],
    "variantNotes": "The sign-in command is the connection payload's command for each provider; the OpenCode variant also carries its own note.",
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
      "soft-ignored"
    ],
    "variantNotes": "No props: the render comes from the appearance settings (custom colours on both themes or one theme), from Soft with a painted pair and from load and save failures; all audited in the states below.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["settings-appearance--default"],
        "note": "Loaded defaults: Normal and System chosen, the Customise… control, the generic detail line, no Reset and no alert; the initial read is a declared fixture GET."
      },
      {
        "name": "custom-colours-both",
        "coverage": "covered",
        "storyIds": ["settings-appearance--custom-colours-both"],
        "note": "Custom colours on for both themes: the detail line names both and the Reset control renders (v-if=\"hasCustomColors\")."
      },
      {
        "name": "custom-colours-one-theme",
        "coverage": "covered",
        "storyIds": ["settings-appearance--custom-colours-one-theme"],
        "note": "Custom colours on for the dark theme only: the detail line names the one theme and Reset renders."
      },
      {
        "name": "soft-ignored",
        "coverage": "covered",
        "storyIds": ["settings-appearance--soft-ignored"],
        "note": "Soft chosen while the active theme has a painted pair (v-if=\"softIgnored\"): the hint explains why Soft has no effect."
      },
      {
        "name": "customise",
        "coverage": "covered",
        "storyIds": ["settings-appearance--customise"],
        "note": "The Customise… control opens the production CustomColorsDialog through v-model:open and Cancel closes it; the dialog makes no read of its own."
      },
      {
        "name": "custom-colours-reset",
        "coverage": "covered",
        "storyIds": ["settings-appearance--reset-custom-colours"],
        "note": "Reset saves custom_colors {} through the production save path (declared PATCH); the control disappears and the detail line reverts."
      },
      {
        "name": "text-contrast-soft",
        "coverage": "covered",
        "storyIds": ["settings-appearance--text-contrast-soft"],
        "note": "Choosing Soft saves through the production save path (declared PATCH), the radio moves selection and focus stays on the picked option."
      },
      {
        "name": "save-failure",
        "coverage": "covered",
        "storyIds": ["settings-appearance--save-failure"],
        "note": "A failing write (PATCH 500) renders the role=alert action-result and leaves the radios on the engine's value; the save loop stops with the error."
      },
      {
        "name": "load-failure",
        "coverage": "covered",
        "storyIds": ["settings-appearance--load-failure"],
        "note": "A failing initial read (GET 500) renders 'Could not load appearance settings.' as the role=alert action-result; the shipped defaults stay the model values."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["settings-appearance--narrow"],
        "note": "Custom colours, both radio groups and Reset inside a maximum 390px container."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load is a silent GET with no placeholder: until the first response the groups show the shipped defaults, so the pre-response render equals the default render and is not a separate story. A failed initial load is the covered load-failure alert. No refresh, empty, filtered-empty or stale state exists: the owner reads once on mount and writes only on a choice."
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
        "coverage": "covered",
        "storyIds": [
          "settings-devices--default"
        ],
        "note": "Default renders the loaded list: a Tailscale-served trusted row, a LAN row and a loopback row. The QR control is absent on the loopback row, the Tailscale tag shows and the plain-HTTP hint is hidden."
      },
      {
        "name": "no-tailscale",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--no-tailscale"
        ],
        "note": "NoTailscale renders only the LAN and loopback rows, so no Tailscale address is found and the Tailscale serve hint shows."
      },
      {
        "name": "qr-open",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--qr-open"
        ],
        "note": "QrOpen clicks the trusted row's QR control: aria-expanded becomes true, the label reads Hide QR and the generated SVG renders."
      },
      {
        "name": "copied",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--copied"
        ],
        "note": "Copied clicks Copy on the LAN row through a stubbed clipboard; the button reads Copied."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--loading"
        ],
        "note": "Loading holds the one /api/addresses read: the Looking up addresses line shows and no list renders."
      },
      {
        "name": "failed",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--failed"
        ],
        "note": "Failed answers the read with a 500: the role=alert line names the lookup failure and no list renders."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "settings-devices--empty"
        ],
        "note": "Empty answers an empty address list: no rows render and the Tailscale serve hint shows."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load: one GET /api/addresses on mount (loading state, held). Failure: the failed state. There is no refresh and no stale state; the lookup runs once per mount. The placeholder is a one-line hint and the loaded list is a multi-row block, so the two heights differ; not measured in this slice."
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
    "variantNotes": "Each observed branch of SettingsMemoryBackup.vue is a state below; the states are the audit of these variants.",
    "states": [
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--loading"
        ],
        "note": "Loading: the first status read is held; the checking line shows and no status or setup actions render."
      },
      {
        "name": "failed",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--failed"
        ],
        "note": "Failed: a first-load failure shows the role=alert read error with Check again; no setup actions or status render."
      },
      {
        "name": "not-configured",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--not-configured"
        ],
        "note": "NotConfigured: a not_configured status shows the saved-on-this-device lede with Set up in Ciaobot and Copy setup prompt enabled."
      },
      {
        "name": "setup-started",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--setup-started"
        ],
        "note": "SetupStarted: a started setup chat shows its confirmation and Open the setup chat; that button emits open-chat with the chat id."
      },
      {
        "name": "setup-failed",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--setup-failed"
        ],
        "note": "SetupFailed: a refused setup request shows the server message and no started-chat confirmation."
      },
      {
        "name": "setup-prompt-copied",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--setup-prompt-copied"
        ],
        "note": "SetupPromptCopied: Copy setup prompt fetches the prompt and writes it to a stubbed clipboard; the confirmation shows."
      },
      {
        "name": "ready",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--ready"
        ],
        "note": "Ready: the Backed up label with Pause and Back up now."
      },
      {
        "name": "paused",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--paused"
        ],
        "note": "Paused: the paused label with Resume; Resume patches the setting and shows the running-again result."
      },
      {
        "name": "running",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--running"
        ],
        "note": "Running: the Backing up now label with its running detail line."
      },
      {
        "name": "offline",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--offline"
        ],
        "note": "Offline: the Offline label, its detail line and the server reason inside Backup details."
      },
      {
        "name": "needs-attention",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--needs-attention"
        ],
        "note": "NeedsAttention: the Needs attention label; Review details opens the disclosure that holds the server reason."
      },
      {
        "name": "deferred",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--deferred"
        ],
        "note": "Deferred: the Backup deferred label and the update-in-progress detail."
      },
      {
        "name": "pending",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--pending"
        ],
        "note": "Pending: pending commits show the Waiting to upload label."
      },
      {
        "name": "scope-gap",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--scope-gap"
        ],
        "note": "ScopeGap: a non-zero coverage_gap shows the count warning under the Backed up state."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--stale"
        ],
        "note": "Stale: a failed refresh over a paused status keeps the status and shows Could not refresh with the last known status line."
      },
      {
        "name": "run-pending",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--run-pending"
        ],
        "note": "RunPending: a held backup run disables Back up now and reads Backing up."
      },
      {
        "name": "run-failed",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--run-failed"
        ],
        "note": "RunFailed: a refused run answers with the status object; the error says the local copy is safe and the state line moves to Offline."
      },
      {
        "name": "flag-pending",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-backup--flag-pending"
        ],
        "note": "FlagPending: a held Pause patch shows Working on the flag button, which is disabled."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load: one status read on mount (loading state). Refresh: Check again, and while not set up a bounded re-read every 10 seconds (at most 36 times); a failed refresh keeps the last status (stale state). The placeholder is a one-line checking hint and the loaded status is a multi-row block, so the heights differ; not measured in this slice."
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
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--loading"
        ],
        "note": "Loading holds the first handover read: the reading line shows and the radio group does not render."
      },
      {
        "name": "failed",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--failed"
        ],
        "note": "Failed answers the first read with an error: the role=alert message and a Try again control show; no radio group renders."
      },
      {
        "name": "no-decisions",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--no-decisions"
        ],
        "note": "NoDecisions renders the no-prediction line. The keyboard path (ArrowRight on Off) saves Dismiss and moves focus to it."
      },
      {
        "name": "evidence",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--evidence"
        ],
        "note": "Evidence renders the agreement sentence with the percent and the per-direction counts; Refresh re-reads the same answer."
      },
      {
        "name": "suggest-handover",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--suggest-handover"
        ],
        "note": "SuggestHandover shows the status line saying the operator could let dismiss suggestions be handed over."
      },
      {
        "name": "cannot-act",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--cannot-act"
        ],
        "note": "CannotAct: acting_available false shows the cannot-act note and disables the Dismiss and Dismiss-and-accept radios; Off stays enabled."
      },
      {
        "name": "stale",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--stale"
        ],
        "note": "Stale keeps the last known rate on screen when a refresh fails and shows the Could not refresh alert."
      },
      {
        "name": "saving",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--saving"
        ],
        "note": "Saving: a held save keeps the chosen radio checked and every option aria-disabled, while none is disabled."
      },
      {
        "name": "save-failed",
        "coverage": "covered",
        "storyIds": [
          "settings-memory-handover--save-failed"
        ],
        "note": "SaveFailed: a refused save shows the server message and hands the choice back to the saved value."
      },
      {
        "name": "confirm-all",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Not isolable: choosing Dismiss and accept opens the app-level askConfirm prompt, which is drawn by the global confirm host, not by this card. The host is not mounted in the workshop, so this state cannot render in isolation and is not played."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load: one read on mount (loading state). Failure: the failed state with Try again. Refresh: the Refresh control re-reads (evidence state) and a failed refresh keeps the last rate (stale state). A workspace switch re-reads and clears the previous rate. Not measured in this slice."
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
        "name": "network-default",
        "coverage": "covered",
        "storyIds": [
          "settings-server--network-default"
        ],
        "note": "NetworkDefault renders the Listen on select with the default host (empty value) and no restart hint. Choosing This computer only calls the save callback with pwa_host 127.0.0.1."
      },
      {
        "name": "network-restart-pending",
        "coverage": "covered",
        "storyIds": [
          "settings-server--network-restart-pending"
        ],
        "note": "NetworkRestartPending: the stored host differs from the running host, so the Saved, restart to listen hint shows and the select reads the stored host."
      },
      {
        "name": "network-custom-host",
        "coverage": "covered",
        "storyIds": [
          "settings-server--network-custom-host"
        ],
        "note": "NetworkCustomHost: a stored host that is neither the default nor loopback adds a Custom address option; the running host matches, so no hint shows."
      },
      {
        "name": "network-unloaded",
        "coverage": "covered",
        "storyIds": [
          "settings-server--network-unloaded"
        ],
        "note": "NetworkUnloaded: routines are not loaded yet (null), so the Listen on select is disabled."
      },
      {
        "name": "developer-closed",
        "coverage": "covered",
        "storyIds": [
          "settings-server--developer-closed"
        ],
        "note": "DeveloperClosed: the disclosure is collapsed (aria-expanded false) and its panel is not in the page; the toggle opens it."
      },
      {
        "name": "developer-open",
        "coverage": "covered",
        "storyIds": [
          "settings-server--developer-open"
        ],
        "note": "DeveloperOpen: the opened disclosure shows the native Developer mode switch (aria-checked false) and a disabled Save while the checkout path is unchanged. Clicking the switch calls the save callback with dev_mode true."
      },
      {
        "name": "developer-log-level-pending",
        "coverage": "covered",
        "storyIds": [
          "settings-server--developer-log-level-pending"
        ],
        "note": "DeveloperLogLevelPending: the stored log level differs from the running one, so the Saved, restart to log at debug hint shows."
      },
      {
        "name": "developer-saving",
        "coverage": "covered",
        "storyIds": [
          "settings-server--developer-saving"
        ],
        "note": "DeveloperSaving: while routinesSaving is true the Developer switch, the checkout Save and the log level select are disabled, and a click does not save."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No fetch: routines arrive as a prop from SettingsView. Routines null (not loaded yet) is the network-unloaded state. There is no refresh or stale state in this component."
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
        "name": "loading",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--loading"
        ],
        "note": "Loading: the first read is held; the checking line shows and no rows render."
      },
      {
        "name": "load-failed",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "OWNER BUG, not fixed here: on a first-load failure loaded stays false, so the Checking the update task history line stays on screen beside the role=alert read error. The panel says it is still checking while the check has failed. Recorded for a follow-up; no story."
      },
      {
        "name": "empty",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--empty"
        ],
        "note": "Empty: a loaded list with no finished, failed or hidden rows shows Nothing to show here yet."
      },
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--default"
        ],
        "note": "Default: the install and workspace groups, the hidden task's Reopen and the failed task's Recheck. Finished work stays collapsed behind Show N done."
      },
      {
        "name": "done-shown",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--done-shown"
        ],
        "note": "DoneShown: Show done expands the finished rows and the toggle reads Hide done."
      },
      {
        "name": "live-chat",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--live-chat"
        ],
        "note": "LiveChat: a row whose chat is still open shows Open its chat. Play asserts the control and does not click it, since following it would push a route into the workshop page."
      },
      {
        "name": "reopen-pending",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--reopen-pending"
        ],
        "note": "ReopenPending: a held reopen disables the row's button and reads Reopening."
      },
      {
        "name": "reopen-refused",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--reopen-refused"
        ],
        "note": "ReopenRefused: a 409 refusal shows the in-place Could not reopen it message and the row stays listed."
      },
      {
        "name": "refresh-failed",
        "coverage": "covered",
        "storyIds": [
          "settings-update-tasks--refresh-failed"
        ],
        "note": "RefreshFailed: a failed Check again shows the role=alert read error and keeps the last rows on screen."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial load: one read on mount (loading state, held). Refresh: Check again and the per-row Recheck re-read; a failed refresh keeps the last rows (refresh-failed state). A workspace switch clears the rows and re-reads. The first-load failure path shows the checking line beside the error (load-failed, uncovered). Not measured in this slice."
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
      "connected",
      "expired",
      "none",
      "actions menu",
      "advanced panel"
    ],
    "variantNotes": "Observed branches audited: loading skeleton, load error with retry, none, connected, expired, actions menu (stop-using confirm and busy menu), advanced panel and narrow wrap. The old 'connecting' variant does not exist in the template: gwsAccountState renders connected, expired or none.",
    "states": [
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--loading"],
        "note": "Initial: the generic SkeletonLoader placeholder (rows, one count, announced as Loading Google account) while the integration read has not settled."
      },
      {
        "name": "load-error",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--load-error", "workspace-google-account--refresh-failure"],
        "note": "Initial failure: the warn hint and Retry (v-else-if=\"gwsIntegrationError\"). The retry's declared read replaces the error with the row (load-error) or answers another failure the hint re-renders (refresh-failure)."
      },
      {
        "name": "none",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--none"],
        "note": "No linked profile (v-else): the hint, the Connect Google account control, and the Connect dialog with focus returned to the row's opener after it closes."
      },
      {
        "name": "connected",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--connected"],
        "note": "A linked configured profile: the Connected badge, label and email line, and the actions menu with Re-authenticate offered; Escape closes the menu. The profile's workspace list carries a second workspace so Stop using is offered."
      },
      {
        "name": "expired",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--expired"],
        "note": "A linked profile that needs re-login: the Sign-in expired badge, the cached token error hint, the Sign in again control and the reauth dialog with focus returned to it."
      },
      {
        "name": "advanced-panel",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--advanced-panel"],
        "note": "The Advanced / headless setup menu item opens the command panel (v-if=\"advancedOpen && profile\"); without a stored client the Create client command shows; Hide closes it."
      },
      {
        "name": "advanced-panel-secret",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--advanced-panel-secret"],
        "note": "The same panel with a stored client secret: the Create client block is absent and the Login, Headless and Stored commands remain."
      },
      {
        "name": "stop-using-confirm",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--stop-using-confirm"],
        "note": "The menu's Stop using in this workspace item opens the production confirm; cancelling writes nothing and leaves the link. Confirmed destructive actions are the Overview host's composition, not this row's isolated render."
      },
      {
        "name": "busy",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--busy"],
        "note": "Disconnect and remove while the removal write is held: the actions menu is actually disabled through the owner's busy flag, not just styled."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["workspace-google-account--narrow"],
        "note": "The connected row in a maximum 390px container: the actions wrap onto their own line under the name and email."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Initial loading is the generic SkeletonLoader (rows): its two-line geometry does not match the loaded row's set-row badge and menu geometry, which a later loading slice should replace. A failed read renders the hint and Retry; the declared reads cover both a recoving retry and a refresh failure. No stale or refresh-keep state exists: the owner renders only the injected fetch result and never fetches on mount."
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
        "coverage": "covered",
        "storyIds": ["global-buttons--default"],
        "note": "The unclassed native button baseline: app.css's global button rules (font, tap highlight, touch-action) plus the shared :focus-visible ring; the play clicks it and asserts focus landed."
      },
      {
        "name": "btn-primary",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-primary"],
        "note": "app.css .btn-primary (the anchor): accent fill, --on-accent label, no border, asserted against token probes."
      },
      {
        "name": "btn-small",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-small"],
        "note": "app.css .btn-small: 1px --border edge over the --bg3 fill; the play asserts fill, edge and style probes."
      },
      {
        "name": "primary-small",
        "coverage": "covered",
        "storyIds": ["global-buttons--primary-small"],
        "note": "app.css .btn-primary.btn-small stacking: the primary fill gains the 1px --accent outline the plain primary never has."
      },
      {
        "name": "btn-icon",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-icon"],
        "note": "app.css .btn-icon: a transparent, full --touch box for icon-only controls, named through aria-label because it has no visible text."
      },
      {
        "name": "btn-icon-pressed",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-icon-pressed"],
        "note": "app.css .btn-icon[aria-pressed=\"true\"]: a --bg3 fill over a --border edge; the play asserts the aria state and both token probes."
      },
      {
        "name": "btn-chip",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-chip"],
        "note": "app.css .btn-chip: the ghost control, transparent fill, 1px --border edge, --fg2 label."
      },
      {
        "name": "btn-chip-active",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-chip-active"],
        "note": "app.css .btn-chip.active: the label and the edge turn --accent; the play asserts the active class with both accent probes."
      },
      {
        "name": "btn-danger",
        "coverage": "covered",
        "storyIds": ["global-buttons--btn-danger"],
        "note": "app.css .btn-danger modifies a base (production renders btn-small btn-danger on SettingsView's delete actions) by forcing edge and label to --error; it owns no fill."
      },
      {
        "name": "disabled",
        "coverage": "covered",
        "storyIds": ["global-buttons--disabled"],
        "note": "app.css .btn-primary:disabled: --bg3 fill, --fg3 label, not-allowed cursor; the play asserts the disabled property, that a real click reaches no handler and no focus lands on it."
      },
      {
        "name": "touch-hit",
        "coverage": "covered",
        "storyIds": ["global-buttons--touch-hit"],
        "note": "app.css .touch-hit: content-box sizing with negative margin trade and the ::before at the --touch-hit-visual inset, so a compact icon keeps a full --touch hit box; the play asserts the content-box sizing, the 7px decorative inset and that the box stays within a half pixel of the --touch probe."
      },
      {
        "name": "hover",
        "coverage": "covered",
        "storyIds": ["global-buttons--hover"],
        "note": "The :hover fill rules (.btn-small, .btn-primary:not(:disabled), .btn-icon, .btn-chip) answer the browser's real pointer position, which the workshop's synthetic user event API cannot move; the play asserts the shipped .btn-primary:hover:not(:disabled) rule text instead, the same shipped-CSS pattern as the SkeletonLoader reduced-motion story."
      },
      {
        "name": "active-press",
        "coverage": "covered",
        "storyIds": ["global-buttons--active-press"],
        "note": "The :active press rules (scale and repaint on .btn-small, .btn-primary, .btn-icon, .btn-chip) apply only during a held pointer press the runner cannot perform; the play asserts the shipped .btn-primary:active rule text instead, the SkeletonLoader reduced-motion pattern."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["global-buttons--keyboard-focus"],
        "note": "Keyboard focus draws the :where(...):focus-visible 2px --accent outline with a 2px offset; the play tabs to the anchor and asserts the ring against a token probe."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "No loading, failure or stale treatment exists: every button class is a static presentation rule whose dynamic part is hover, press and focus feedback, none of which is a data state."
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
        "coverage": "covered",
        "storyIds": ["global-fields-forms--default"],
        "note": "The unclassed native input baseline: app.css's global input rules (fill, 1px --border edge, --radius corners), asserted against token probes."
      },
      {
        "name": "form-grid",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--form-grid"],
        "note": "app.css .form-grid (the anchor) at a wide canvas: grid with 12px gaps; the play asserts both groups share the first row."
      },
      {
        "name": "form-group",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--form-group"],
        "note": "app.css .form-group: a flex column with a 4px gap over an uppercase label."
      },
      {
        "name": "form-actions",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--form-actions"],
        "note": "app.css .form-actions: a wrapping flex row; the story carries a primary and a small secondary exactly as forms lay them out."
      },
      {
        "name": "hint",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--hint"],
        "note": "app.css .hint: --fg2 secondary text with no added margin, asserted against a token probe."
      },
      {
        "name": "hint--warn",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--hint-warn"],
        "note": "app.css .hint--warn: a block-level warning callout with an opaque tinted fill and a color-mix --warning edge; the play asserts the branch paints rather than restating the mix."
      },
      {
        "name": "checkbox-pill",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--checkbox-pill"],
        "note": "app.css .checkbox-pill rendered as production does (SchedulePanel): a label wrapping a hidden checkbox; the play asserts the 30px pill box and that clicking it toggles the checkbox."
      },
      {
        "name": "checkbox-pill-active",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--checkbox-pill-active"],
        "note": "app.css .checkbox-pill.active: the --accent2 fill under a --fg label."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--narrow"],
        "note": "The <=600px single-column collapse is a viewport media query; the story pins the workshop's narrow 390px viewport (the runner applies it to the page, as the ItemTitleRow phone-narrow story does) and asserts the groups stack."
      },
      {
        "name": "keyboard-focus",
        "coverage": "covered",
        "storyIds": ["global-fields-forms--keyboard-focus"],
        "note": "A real Tab focuses the field and the accent edge and focus ring settle over the token transitions; the play waits for the settled edge and asserts the ring paints."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Static presentation rules with no data states: grid, grouping, actions, hints and pills render as given and never show loading, refresh, failure or stale treatment."
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
        "coverage": "covered",
        "storyIds": ["global-badges--default"],
        "note": "The unclassed span baseline: proportional face and no fill, the before the badge classes paint over."
      },
      {
        "name": "badge",
        "coverage": "covered",
        "storyIds": ["global-badges--badge"],
        "note": "app.css .badge (the anchor): mono face, 10px pill radius, nowrap, 2px/8px padding."
      },
      {
        "name": "badge--accent",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-accent"],
        "note": "app.css .badge--accent: accent fill with an --on-accent label, asserted against token probes."
      },
      {
        "name": "badge--accent2",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-accent-2"],
        "note": "app.css .badge--accent2: --accent2 fill with an --fg label."
      },
      {
        "name": "badge--muted",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-muted"],
        "note": "app.css .badge--muted: --bg3 fill behind a --fg2 label and a --border edge."
      },
      {
        "name": "badge--builtin",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-builtin"],
        "note": "app.css .badge--builtin: the --accent2 tint over --bg3 and its tinted edge, both resolved through color-mix; the play asserts the branch paints rather than restating the mix."
      },
      {
        "name": "badge--builtin-light",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-builtin-light"],
        "note": "app.css .theme-light .badge--builtin: the light-scope swap; production mounts the class on <html>, the selector matches any ancestor and the rule's own fallbacks carry the light values, so a .theme-light wrapper reproduces the branch."
      },
      {
        "name": "badge--success",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-success"],
        "note": "app.css .badge--success: the translucent green fill with a --success label."
      },
      {
        "name": "badge--warn",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-warn"],
        "note": "app.css .badge--warn: the translucent amber fill with a --warning label."
      },
      {
        "name": "badge--error",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-error"],
        "note": "app.css .badge--error: the translucent red fill with an --error label."
      },
      {
        "name": "badge--dot",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-dot"],
        "note": "app.css .badge--dot: the resting dot marker, dimmed to 0.35, uppercase, no fill, --fg2 label."
      },
      {
        "name": "badge--dot-active",
        "coverage": "covered",
        "storyIds": ["global-badges--badge-dot-active"],
        "note": "app.css .badge--dot.active: full opacity over an --accent2 fill with an --fg label."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Status badges render their class in one paint; there is no loading, empty, failure or stale appearance."
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
        "coverage": "covered",
        "storyIds": ["global-surfaces--default"],
        "note": "The unclassed block baseline: no fill, no edge, plain flow, the before the card class paints over."
      },
      {
        "name": "card",
        "coverage": "covered",
        "storyIds": ["global-surfaces--card"],
        "note": "app.css .card (the anchor): a --bg2 panel with a 1px --border edge and --radius corners, a flex column with 12px row gap and 16px padding, asserted against token probes."
      }
    ],
    "loading": {
      "status": "none",
      "detail": ".card is a static container rule: content renders as given, with no loading, empty, failure or stale treatment."
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
        "coverage": "covered",
        "storyIds": ["global-typography--default"],
        "note": "The unclassed heading baseline: proportional face, no uppercase, no letter spacing, the treatment the shared helpers replace."
      },
      {
        "name": "sr-only",
        "coverage": "covered",
        "storyIds": ["global-typography--sr-only"],
        "note": "app.css .sr-only: absolute 1px clipping that screen readers still reach (the play reads the text) but that renders no visible box."
      },
      {
        "name": "section-title",
        "coverage": "covered",
        "storyIds": ["global-typography--section-title"],
        "note": "app.css .section-title (the anchor): mono extra-small face in --fg2 with 0.5px spacing at weight 600."
      },
      {
        "name": "label-eyebrow",
        "coverage": "covered",
        "storyIds": ["global-typography--label-eyebrow"],
        "note": "app.css .label-eyebrow: the mono face at uppercase with 0.5px spacing, for the all-caps meta headings."
      },
      {
        "name": "subsection-title",
        "coverage": "covered",
        "storyIds": ["global-typography--subsection-title"],
        "note": "app.css .subsection-title: a proportional --fg2 heading at weight 500, distinct from the mono anchors."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "Text helpers render instantly against the current tokens; no loading, failure or stale treatment exists."
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
        "coverage": "covered",
        "storyIds": ["global-focus--default"],
        "note": "The resting control: no focus, no outline; the family's before-state, asserted on an unfocused button."
      },
      {
        "name": "focus-visible",
        "coverage": "covered",
        "storyIds": ["global-focus--focus-visible"],
        "note": "app.css :where(button, a, input, textarea, select, [role=button], [role=link]):focus-visible (the anchor): a real Tab paints the 2px --accent outline with a 2px offset and focus lands on the intended control."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The outline is a focus-time paint, not a data state; no loading, failure or stale treatment exists."
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
        "coverage": "covered",
        "storyIds": ["global-modals--default"],
        "note": "An open modal, the family's composition: the fixed rgba(0, 0, 0, 0.55) backdrop at z-index 100 centring a sheet, asserted directly because the overlay is position: fixed."
      },
      {
        "name": "modal-backdrop",
        "coverage": "covered",
        "storyIds": ["global-modals--modal-backdrop"],
        "note": "app.css .modal-backdrop (the anchor) at a wide viewport: a fixed full-frame flex centre under a 16px padding inset."
      },
      {
        "name": "modal-sheet",
        "coverage": "covered",
        "storyIds": ["global-modals--modal-sheet"],
        "note": "app.css .modal-sheet at a wide viewport: --radius-lg corners, a 1px --border edge, up to 520px, scrollable, a flex column."
      },
      {
        "name": "narrow",
        "coverage": "covered",
        "storyIds": ["global-modals--narrow"],
        "note": "The <=600px phone branch: backdrop without inset, sheet stretched frame to frame with no corners or edge; the story pins the workshop's narrow 390px viewport (the runner applies it to the page, as the ItemTitleRow phone-narrow story does)."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The backdrop and sheet render on open; the shared rule has no loading, empty, failure or stale treatment of its own (owners draw any placeholder inside the sheet)."
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
        "coverage": "covered",
        "storyIds": [
          "home-intake-composer--default"
        ],
        "note": ".home-intake-form with an empty prompt: the Start a new chat form, the New chat control and no send chord."
      },
      {
        "name": "prompt",
        "coverage": "covered",
        "storyIds": [
          "home-intake-composer--prompt"
        ],
        "note": "A typed prompt: the control is named Send and its title carries the send chord."
      },
      {
        "name": "project",
        "coverage": "covered",
        "storyIds": [
          "home-intake-composer--project"
        ],
        "note": "A workspace with no project: the project chip and the start control are disabled, with the prompt still editable."
      },
      {
        "name": "model",
        "coverage": "covered",
        "storyIds": [
          "home-intake-composer--model"
        ],
        "note": "The model chip opens the picker; choosing a model renames the chip to that model."
      },
      {
        "name": "attachments",
        "coverage": "covered",
        "storyIds": [
          "home-intake-composer--attachments"
        ],
        "note": "Staged files appear in the form's Attachments list with a remove control."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The form has no fetch of its own. Model list loading is recorded on the HomeIntake row."
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
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers. Audited card-region branches and classes: lane arrangement and heads, card content variants, filter-drawn groups, the Done lane/tab, the first-load placeholder cards, drag-sort classes and the busy write state. Covered by the Task board cards stories, which mount the production surface. Dragging stays uncovered: it needs a live pointer drag the workshop runner does not drive, and the keyboard and menu moves are claimed under the ordering state of the surface row.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-board-cards--default"],
        "note": "Default: the loaded board draws each card with its check, title, open button, menu and project and due meta; asserts the check label and the lede count."
      },
      {
        "name": "lanes",
        "coverage": "covered",
        "storyIds": ["task-board-cards--lanes"],
        "note": "Lanes: four status lanes side by side with heads and counts, the empty lane says Nothing here, and Done sits behind its tab."
      },
      {
        "name": "cards",
        "coverage": "covered",
        "storyIds": ["task-board-cards--cards"],
        "note": "Cards: an overdue due date is red with a hidden Overdue prefix, the agent note and band show, and a needs-you band offers Open chat."
      },
      {
        "name": "filters",
        "coverage": "covered",
        "storyIds": ["task-board-cards--filters"],
        "note": "Filters: a status chip draws its one group at full width with aria-pressed set, and Clear filters is offered."
      },
      {
        "name": "Done",
        "coverage": "covered",
        "storyIds": ["task-board-cards--done"],
        "note": "Done: the drawn Done lane holds two .task-card--done cards with Hide; Hide returns it to a collapsed aria-expanded=false tab."
      },
      {
        "name": "loading",
        "coverage": "covered",
        "storyIds": ["task-board-cards--loading"],
        "note": "Loading: two placeholder cards per lane with two loading bars each and a count bar per head, at 390px; no real title."
      },
      {
        "name": "dragging",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Drag-sort visuals (task-lanes--dragging, drag-source, drag-placeholder, drag-settling) need a live pointer drag, which the workshop runner does not drive; stays uncovered by design."
      },
      {
        "name": "busy",
        "coverage": "covered",
        "storyIds": ["task-board-cards--busy"],
        "note": "Busy: a Resume on a settled card holds its write; the card check and the band action become disabled while it is in flight."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The card-region loading UI is the first-load placeholder owned at this anchor: two .task-loading-bar spans per placeholder card and a count bar in each lane head (fixed in #2961), reduced to the Done toggle label while Done is collapsed. After an answer, refreshes keep the cards and mark them stale in the surface's loadStale notice; refused writes surface through the surface's board.error alert."
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
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers. The sheets are fixed-position modals, so their narrow geometry is the workshop's narrow viewport preset rather than a wrapper; the page editor is covered inside a 390px block.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-editor--default"],
        "note": "The create sheet: the modal .task-sheet the board's New task opens, with focus on the title, the disabled File task until a title is typed, Filing… while its POST is in flight and the refusal line under the form when it is refused. Covered by task-editor--default, --create-refused and --create-busy."
      },
      {
        "name": "page",
        "coverage": "covered",
        "storyIds": ["task-editor--page"],
        "note": "The detail editor as a board page (task-sheet task-detail-pane): region role, the title's arrival focus, rendered description, the side column and the fields' autosave line, inside a 390px wrapper. Covered by task-editor--page."
      },
      {
        "name": "overlay",
        "coverage": "covered",
        "storyIds": ["task-editor--overlay"],
        "note": "The same editor as an overlay modal sheet (task-sheet task-detail-sheet): role=dialog, aria-modal, no board chrome, and the host's awaited-close seam. Covered by task-editor--overlay."
      },
      {
        "name": "title-required",
        "coverage": "covered",
        "storyIds": ["task-editor--title-required"],
        "note": "An overlay close over a title it cannot write is refused: 'Add a title before closing.' and the draft stays, with nothing presented. Covered by task-editor--title-required."
      },
      {
        "name": "write-refused",
        "coverage": "covered",
        "storyIds": ["task-editor--write-refused"],
        "note": "A refused save before a close keeps the editor open with the server's sentence, the draft and a re-read of the rows. Covered by task-editor--write-refused."
      },
      {
        "name": "editing",
        "coverage": "covered",
        "storyIds": ["task-editor--editing"],
        "note": "The description edit: a click on the rendered body is the textarea, focus follows, typing pauses write and the save line moves from Saving… to Saved. Covered by task-editor--editing."
      },
      {
        "name": "description-loading",
        "coverage": "covered",
        "storyIds": ["task-editor--description-loading"],
        "note": "The editor's per-open description read in flight: the field is absent, 'Loading description…' is role=status. Covered by task-editor--description-loading."
      },
      {
        "name": "description-failed",
        "coverage": "covered",
        "storyIds": ["task-editor--description-failed"],
        "note": "A refused description read leaves the field absent and offers Retry, which re-reads without closing. Covered by task-editor--description-failed."
      },
      {
        "name": "body-conflict",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The description conflict needs a GET-by-id answer that changes the prose under a dirty draft mid-write — a race inside one gesture window, not a state one story render can hold deterministically. Not claimed."
      },
      {
        "name": "overlay-loading",
        "coverage": "covered",
        "storyIds": ["task-editor--overlay-loading"],
        "note": "The overlay task resolution in flight: the compact task-overlay-state sheet, 'Loading task…'. Covered by task-editor--overlay-loading."
      },
      {
        "name": "overlay-failed",
        "coverage": "covered",
        "storyIds": ["task-editor--overlay-failed"],
        "note": "The overlay read failed: its own state, the server's sentence and a Retry, never an editor over stale rows. Covered by task-editor--overlay-failed."
      },
      {
        "name": "overlay-missing",
        "coverage": "covered",
        "storyIds": ["task-editor--overlay-missing"],
        "note": "The host's task is not on the board: Task not found, no editor. Covered by task-editor--overlay-missing."
      },
      {
        "name": "create-refused",
        "coverage": "covered",
        "storyIds": ["task-editor--create-refused"],
        "note": "A refused filing keeps the New task form, what was typed and the server's sentence under the buttons. Covered by task-editor--create-refused."
      },
      {
        "name": "create-busy",
        "coverage": "covered",
        "storyIds": ["task-editor--create-busy"],
        "note": "Filing… is the block itself: the submit and Cancel are disabled while the POST is in flight. Covered by task-editor--create-busy."
      },
      {
        "name": "record-missing",
        "coverage": "covered",
        "storyIds": ["task-editor--record-missing"],
        "note": "The record vanished under an open editor with a draft: the draft is kept and named with Copy draft and Discard draft. Covered by task-editor--record-missing."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "The editor's description read is an initial load per open (held GET shows Loading description… with no field); the overlay's task resolution is the same shape (Loading task…). A refresh after a refused write re-reads the list with the editor open (record-missing keeps its draft); failure is description-failed and overlay-failed; the sheet itself has no stale shape. The sheets draw no skeleton: the board's first-load placeholder geometry is owned by the components-task-workspace-surface row."
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
    "variantNotes": "The .task-resolution-sheet dialog owns completion and resolution editing. The .task-completion selector instead marks earlier-completion history items, not this sheet. Support sources are indirect composition hosts, not direct inline consumers. The sheet is a fixed-position modal, so its narrow geometry is the workshop's narrow viewport preset rather than a wrapper.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-completion--default"],
        "note": "TaskWorkspaceSurface.vue gates .task-resolution-sheet with resolutionOpen && resolutionTask; the sheet enters with focus on the note field. Covered by task-completion--default."
      },
      {
        "name": "complete",
        "coverage": "covered",
        "storyIds": ["task-completion--complete"],
        "note": "resolutionMode === 'complete' presents Complete with resolution and the Mark done action unless the task is review-ready; a blank draft completes with resolution ''. Covered by task-completion--complete."
      },
      {
        "name": "edit",
        "coverage": "covered",
        "storyIds": ["task-completion--edit"],
        "note": "resolutionMode.value === 'edit' presents Edit resolution and Save resolution. editResolution reads the stored text before opening; a failed read opens nothing. Covered by task-completion--edit."
      },
      {
        "name": "review-ready",
        "coverage": "covered",
        "storyIds": ["task-completion--review-ready"],
        "note": "In complete mode, isReviewReady(resolutionTask) selects Approve done and renders DecisionReason bound to resolutionReason. Covered by task-completion--review-ready."
      },
      {
        "name": "pending",
        "coverage": "covered",
        "storyIds": ["task-completion--pending"],
        "note": "resolutionBusy makes the textarea, DecisionReason, submit, refresh, close and Cancel controls disabled and changes the submit label to Saving…. Covered by task-completion--pending."
      },
      {
        "name": "action-error",
        "coverage": "covered",
        "storyIds": ["task-completion--action-error"],
        "note": "resolutionError renders a role=\"alert\" message with refreshResolutionTask. The refresh re-reads the board and task revision without changing the draft; refusal keeps the sheet and draft. Covered by task-completion--action-error."
      },
      {
        "name": "draft",
        "coverage": "covered",
        "storyIds": ["task-completion--draft"],
        "note": "The optional resolutionDraft textarea is initialized by openResolution. Completion sends an empty note for a blank or whitespace-only draft; edit mode preserves the exact typed text. Covered by task-completion--draft."
      },
      {
        "name": "submit",
        "coverage": "covered",
        "storyIds": ["task-completion--submit"],
        "note": "The form calls submitResolution, flushing editor changes and, for completion, releasing a held attempt before reading the current revision. Edit uses board.update; complete uses board.complete with the optional review reason. Covered by task-completion--submit (an approved result), --complete (a blank note) and --draft (a typed note)."
      },
      {
        "name": "cancel",
        "coverage": "covered",
        "storyIds": ["task-completion--cancel"],
        "note": "Cancel, close and backdrop invoke closeResolution without writing; Escape also closes through useModalFocus. Closing is refused while resolutionBusy; otherwise resetResolution clears the sheet and draft. Covered by task-completion--cancel."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Opening the sheet is local; the only read is edit mode's GET /api/tasks/{id} before it opens, and a failed read opens nothing (the board's own save line is the surface row's). The sheet has no initial-loading or empty render to show: the busy shape is the disabled controls with Saving…, the failure shape is the action-error row with Refresh task, and refresh is refreshResolutionTask. No stale state."
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
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers. The .task-delegate anchor matches the editor's Agent block and the .task-delegate-sheet preview it opens. Both are fixed-position surfaces, so their narrow geometry is the workshop's narrow viewport preset rather than a wrapper.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-delegation--default"],
        "note": "No attempt_state: the Agent block says in one line what delegating does (a new chat in the task's project, approval cards asked in that chat) and its control is Delegate to agent. Covered by task-delegation--default."
      },
      {
        "name": "delegation",
        "coverage": "covered",
        "storyIds": ["task-delegation--delegation"],
        "note": "A live attempt (running, needs_you): the badge is the record's own state; Stop and Detach are the run gestures; the lazy history reads and lists the attempts. Covered by task-delegation--delegation, --attempts-loading and --attempts-error."
      },
      {
        "name": "handover",
        "coverage": "covered",
        "storyIds": ["task-delegation--handover"],
        "note": "The delegate preview over the closed editor (.task-delegate-sheet): the facts the chat will run with (project, the model row, attendance read as attended), the read description, the per-attempt instructions field and the Delegate confirm. Covered by task-delegation--handover."
      },
      {
        "name": "review-ready",
        "coverage": "covered",
        "storyIds": ["task-delegation--review-ready"],
        "note": "A result waits for review: the review lead, the Approve Done and Reject… controls, and Open chat as the section's own control. Covered by task-delegation--review-ready."
      },
      {
        "name": "preview-resume",
        "coverage": "covered",
        "storyIds": ["task-delegation--preview-resume"],
        "note": "A settled attempt still linked to a live chat: the preview opens in resume mode, naming both ways out (Resume, and Start a new attempt beside Cancel). Covered by task-delegation--preview-resume."
      },
      {
        "name": "preview-retry",
        "coverage": "covered",
        "storyIds": ["task-delegation--preview-retry"],
        "note": "A settled attempt whose chat is archived: the preview opens in retry mode, with Start a new attempt as the confirm. Covered by task-delegation--preview-retry."
      },
      {
        "name": "preview-body-loading",
        "coverage": "covered",
        "storyIds": ["task-delegation--preview-body-loading"],
        "note": "The sheet's own description read in flight: Loading description…, and no preview to confirm against. Covered by task-delegation--preview-body-loading."
      },
      {
        "name": "preview-body-error",
        "coverage": "covered",
        "storyIds": ["task-delegation--preview-body-error"],
        "note": "A refused description read: the sheet explains what the chat still receives and offers Retry, which re-reads without closing. Covered by task-delegation--preview-body-error."
      },
      {
        "name": "handover-busy",
        "coverage": "covered",
        "storyIds": ["task-delegation--handover-busy"],
        "note": "A hand-over in flight: the confirm reads Working… and the instructions field and model chip stop taking input. Covered by task-delegation--handover-busy."
      },
      {
        "name": "attempts-loading",
        "coverage": "covered",
        "storyIds": ["task-delegation--attempts-loading"],
        "note": "The lazy history read in flight: Loading attempts… and no list. Covered by task-delegation--attempts-loading."
      },
      {
        "name": "attempts-error",
        "coverage": "covered",
        "storyIds": ["task-delegation--attempts-error"],
        "note": "A refused history read: the server's sentence with a Retry that re-reads without closing the editor. Covered by task-delegation--attempts-error."
      },
      {
        "name": "linked-chats",
        "coverage": "covered",
        "storyIds": ["task-delegation--linked-chats"],
        "note": "The task names chats that worked on it: the Linked chats section with unlink as an ordinary write the row adopts. Covered by task-delegation--linked-chats."
      },
      {
        "name": "reconcile",
        "coverage": "covered",
        "storyIds": ["task-delegation--reconcile"],
        "note": "A row that disagrees with itself (a settled attempt still holding the task): the block says what disagrees and which controls resolve it, never a rewrite. Covered by task-delegation--reconcile."
      },
      {
        "name": "open-chat-preview",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The preview's open_chat mode can only render through a programmatic openDelegate call: delegateFromDetail diverts a live attempt to its chat before any preview opens, and both card and editor Open chat gestures navigate there directly."
      }
    ],
    "loading": {
      "status": "known",
      "detail": "Two reads, both theirs: the Agent block's lazy history (initial, with its own failure shape and Retry) and the delegate preview's description read (the sheet's initial load, Loading description…, refusal keeps the sheet with a Retry). The block itself draws no skeleton; the badge and gestures render from the row."
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
    "variantNotes": "Markup and styles are owned here; inline families have no direct import consumers. These are indirect composition hosts, not direct inline consumers. The sheet is a fixed-position modal, so its narrow geometry is the workshop's narrow viewport preset rather than a wrapper.",
    "states": [
      {
        "name": "default",
        "coverage": "covered",
        "storyIds": ["task-ordering--default"],
        "note": "The .task-move-sheet the card menu's Move… opens for one task: its column preselected, the landing rows its lane offers, a Move confirm and Escape's write-free close. Covered by task-ordering--default."
      },
      {
        "name": "move",
        "coverage": "covered",
        "storyIds": ["task-ordering--move"],
        "note": "A cross-lane placement: another column lists another set of landing rows, and one write carries the status and the placement (PATCH with placement anchors); the card lands in its lane. Covered by task-ordering--move."
      },
      {
        "name": "done-append",
        "coverage": "covered",
        "storyIds": ["task-ordering--done-append"],
        "note": "When the Done lane is not drawn (no done rows, the collapsed tab's lane absent), the completion appends when confirmed: the sheet's note says so, the position list is the single appends row, and confirming opens the completion sheet writing nothing. Covered by task-ordering--done-append."
      },
      {
        "name": "done-position",
        "coverage": "covered",
        "storyIds": ["task-ordering--done-position"],
        "note": "When the Done lane is drawn, Done keeps the chosen position: Top and after each row, and the note says the position is saved with the confirmation. Covered by task-ordering--done-position (drawn with the workshop's per-browser Done-tab preference held on for the story)."
      },
      {
        "name": "order-error",
        "coverage": "covered",
        "storyIds": ["task-ordering--order-error"],
        "note": "The order file could not be read (the 'ordering' variant renamed to name its branch): the drawn menu's Move up, Move down and Move… are disabled and carry the reason as their title, beside the order notice the board draws. Covered by task-ordering--order-error."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The move sheet opens from rows already on screen and reads nothing: every position option is computed from the held order, so it has no initial, refresh, failure or stale shape."
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
        "note": "The .pfp-body preview states are proven by components-pinned-file-panel's covered states (default/markdown/document-properties/csv/pdf/pptx-libreoffice/html-preview/html-code/folder stories render exactly this body); the same markup owns no story file of its own."
      },
      {
        "name": "preview",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "Same body as default, named for the preview-vs-editor split this family owns; covered through components-pinned-file-panel (including html-code/html-code-error for the artifact panel)."
      },
      {
        "name": "editing",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The edit shell is exercised end to end by components-pinned-file-panel's editing, editing-csv, saving, save-error and csv-save states (the pinned-file-preview-editor--* stories, claimed by the panel component row)."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The family loads nothing of its own: the tile's initial read is the panel's skeleton story, and this family's one flight state is the save POST (blocked 'Saving…', proven by the panel row's saving state)."
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
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The wrap at rest is the baseline of the drawn-graph state: its rendered story memory-graph--graph is claimed on the owner row components-memory-map-view (story coverage lives on component rows only)."
      },
      {
        "name": "graph",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The drawn canvas with toolbar stats, zoom controls and hint: memory-graph--graph mounts the production owner and is claimed on components-memory-map-view; its play flips the label-gate hint by zooming in."
      },
      {
        "name": "zoomed-out",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The labels-hidden fit scale (more than 40 visible notes): the initial render of memory-graph--graph's dense synthetic vault, claimed on components-memory-map-view."
      },
      {
        "name": "selection",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "Selecting a note flies the camera in and opens its tile: memory-graph--selection drives the production requestFocus and is claimed on components-memory-map-view."
      },
      {
        "name": "hover-tip",
        "coverage": "uncovered",
        "storyIds": [],
        "note": "The v-if hoveredNode tooltip needs a pointer over a node's settled canvas position; the simulation's coordinates are not deterministic in the workshop, so no story asserts it."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The wrap renders only after the owner's graph load settles; the loading branch and its skeleton belong to the map-view row, which documents them."
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
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The wrap at rest is the baseline of the list state: its rendered story memory-list-selection--list is claimed on the owner row components-memory-map-view (story coverage lives on component rows only)."
      },
      {
        "name": "list",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The sortable table with aria-sort headers, the dash placeholder for an undated note and the stale row's Needs review flag: memory-list-selection--list is claimed on components-memory-map-view and its play sorts by Checked."
      },
      {
        "name": "selection",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "A row's title button selects the row (aria-pressed, the current class) and opens the tile: memory-list-selection--selection is claimed on components-memory-map-view."
      },
      {
        "name": "filter",
        "coverage": "not-applicable",
        "storyIds": [],
        "note": "The orphans filter narrows the table to unlinked notes: memory-list-selection--filter is claimed on components-memory-map-view."
      }
    ],
    "loading": {
      "status": "none",
      "detail": "The wrap renders only after the owner's graph load settles; there is no loading branch inside the list. The owner's loading states are on the map-view row."
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
