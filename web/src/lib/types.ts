import type { AppearancePayload } from './appearance'
import type { SubagentCard } from './subagentCard'
import type { ShellCard } from './shellCard'

export type WorkspaceName = string

/**
 * A runtime provider id, enumerated by the backend registry
 * (`ciao/provider_registry.py`). The `(string & {})` arm keeps editor
 * autocomplete for the providers that ship today while still accepting any id
 * the backend reports, so adding a provider does not mean editing this union.
 */
export type RuntimeProvider = 'claude' | 'opencode' | (string & {})

/**
 * What a runtime provider supports, mirroring `ProviderCapabilities` in
 * `ciao/providers/base.py`. The PWA gates affordances on these rather than on
 * provider ids.
 */
export interface ProviderCapabilities {
  resume: boolean
  fork: boolean
  images: boolean
  stop: boolean
  permissions: boolean
  structured_questions: boolean
  dynamic_models: boolean
  thinking_levels: boolean
  usage: boolean
  quota: boolean
  background_subagents: boolean
  session_history: boolean
  schedule_unattended: boolean
}

/** One runtime provider as described by the backend registry. */
export interface ProviderDescriptor {
  id: RuntimeProvider
  label: string
  short_label: string
  capabilities: ProviderCapabilities
}

// Every selectable provider is a runtime provider now; the alias survives
// because workspace payloads and pickers name it throughout.
export type WorkspaceProvider = RuntimeProvider

export interface WorkspaceProviderOption {
  value: WorkspaceProvider
  label: string
  /** The CLI's short name ("Claude Code"), for chips and picker headings. */
  short_label?: string
  runner?: RuntimeProvider
  default_model?: string
}

export type ProviderOverrideField = 'model' | 'thinking' | 'permission_mode' | 'insights_model'
export type ProviderOverrides = Record<string, Partial<Record<ProviderOverrideField, string>>>

export type ClaudeTrimKind = 'tool' | 'skill' | 'setting'
// One entry of the engine's Claude context-trim catalog (GET /api/workspaces claude_trim_options; ciao/claude_trim.py).
export interface ClaudeTrimOption {
  key: string            // "<kind>:<name>", e.g. "tool:NotebookEdit"
  kind: ClaudeTrimKind
  name: string
  label: string
  reason: string
  group: string          // "scheduling" | "plan" | "notifications" | "design" | "review" | "skills" | "features" | "Connectors" (open set)
  default_trimmed: boolean
}

export interface WorkspaceInfo {
  name: WorkspaceName
  vault_root: string
  default_provider: WorkspaceProvider
  disallowed_tools?: string[] | null
  // PWA accent preset: amber | pink | cyan | emerald | violet | blue | teal | lime | fuchsia. Missing → amber (Ciao orange).
  color?: string
  // Operator-only; sparse: {provider: {field: value}}. Missing = engine default.
  provider_overrides?: ProviderOverrides
  // Sparse Claude context-trim overrides: option key -> trimmed. Missing = catalog defaults.
  claude_trim?: Record<string, boolean>
  // Operator-only; absent or empty = no shared brains.
  shared_brains?: SharedBrainLink[]
}

/**
 * A save row for one shared-brain folder. The name is optional and taken from the
 * repository when absent; it is set once, at link time.
 */
export interface SharedBrainRow {
  path: string
  name?: string
  /** Defaults to Git for registry rows written before folder links existed. */
  kind?: 'git' | 'folder'
  /** Omitted keeps a retained row's value; new rows start on. */
  enabled?: boolean
}

/** The payload of a workspace PATCH: a shared-brain save sends rows, not the read links. */
export type WorkspacePatch = Omit<Partial<WorkspaceInfo>, 'shared_brains'> & { shared_brains?: SharedBrainRow[] }

/** A Git checkout the engine pulls, or an ordinary folder read in place. */
export type SharedBrainKind = 'git' | 'folder'

/** One linked shared-brain folder, as the workspace registry lists it. */
export interface SharedBrainLink {
  name: string
  path: string
  identity: string
  kind?: 'git' | 'folder'
}

/** The last update attempt for a brain, as `ciao/shared_brain.py` records it. */
export interface SharedBrainUpdate {
  outcome: 'updated' | 'up_to_date' | 'skipped' | 'failed'
  reason: string
  detail: string
  at: string
}

/** Live state of one linked brain, from `GET /api/workspaces/{name}/shared-brains`. */
export interface SharedBrainState {
  name: string
  kind: SharedBrainKind
  /** Off keeps the brain registered but out of chats and the memory pass. */
  enabled: boolean
  identity: string
  path: string
  origin_changed: boolean
  missing: boolean
  // '' or why the folder cannot be used now: missing, moved, not_a_checkout,
  // outside_install, unsafe_config, origin_changed, inaccessible, or
  // registry_unavailable (a Git link the engine could not inspect).
  problem: string
  branch: string
  head: string
  head_date: string
  dirty: boolean
  upstream: boolean
  fetched_at: string | null
  last_update: SharedBrainUpdate | null
}

export interface SharedBrainsResponse {
  shared_brains: SharedBrainState[]
}


export interface WorkspacesResponse {
  workspaces: WorkspaceInfo[]
  active: WorkspaceName | null
  // The workspace that cannot be archived (the server refuses it).
  primary?: WorkspaceName | null
  provider_options?: WorkspaceProviderOption[]
  // Catalog the Settings "Claude context trimming" card renders, in engine order.
  claude_trim_options?: ClaudeTrimOption[]
}

/** One archived workspace, from `GET /api/workspaces/archived`. */
export interface ArchivedWorkspace {
  id: string
  name: WorkspaceName
  archived_at: string
  // Install-relative folder the archive lives in.
  path: string
  layout: string
  color: string
  default_provider: string
  // What a restore applies, after server-side validation. `null` means unset:
  // no MCP server is reachable, and the default tool deny-list applies.
  disallowed_tools?: string[] | null
  // Valid automations, all restored paused, and how many were rejected.
  schedules: number
  schedules_dropped?: number
  restorable: boolean
  blocked_reason: string
}

export interface ArchivedWorkspacesResponse {
  archived: ArchivedWorkspace[]
}

/** Status of the agent CLI surface (Settings → Agent CLI), from /api/agent/status. */
export interface AgentCliStatus {
  ready: boolean
  operations: string[]
  telemetry_path: string
  version: string
}

// ── Projects & Chats ────────────────────────────────────────────────────

/** A1: the project's linked repository main checkout, as the engine sees it now. */
export interface ProjectRepo {
  path: string
  branch: string      // '' when HEAD is detached
  dirty: boolean
  missing: boolean    // folder gone or no longer a git toplevel
  instructions?: { name: 'AGENTS.md' | 'CLAUDE.md'; chars: number } | null  // what code mode injects; null = none
}

/** A1: one entry of GET /api/repos/candidates `candidates` ({roots, candidates:[{path,name,root}]}). */
export interface RepoCandidate {
  path: string
  name: string
}

export interface ProjectInfo {
  project_id: string
  name: string
  workspace: WorkspaceName
  context: string
  created_at: string
  order: number
  vault_folder: string
  vault_folder_only?: boolean
  vault_doc_path?: string
  is_system?: boolean
  is_auto?: boolean
  // 'memory' marks the app-owned per-workspace Memory project. The PWA hides
  // it from the sidebar; a user project may legitimately be named "Memory".
  kind?: string
  repo?: ProjectRepo | null
}

export type ArchivedChatSummary = Pick<ChatInfo, 'chat_id' | 'title' | 'created_at' | 'archive_path'>

// The report a project delete returns. `vault_entry_removed` says whether the
// project had a folder under projects/active/ that was removed; the counts
// and the edited backlink list describe that cleanup.
export interface ProjectDeleteReport {
  ok: boolean
  vault_entry_removed: boolean
  removed_notes: number
  removed_files: number
  edited_backlinks: string[]
}

export interface ChatArchivePage {
  items: ArchivedChatSummary[]
  total: number
  offset: number
  limit: number
}

export interface RepoLocation {
  path: string
  branch: string
  is_worktree: boolean
  exists: boolean
}

export interface ChatInfo {
  chat_id: string
  project_id: string
  title: string
  model: string
  // Runtime provider: which CLI runs the turn.
  provider: RuntimeProvider
  // Vestigial. Named which upstream a tier alias resolved to, back when
  // Ollama/OpenRouter ran through Claude Code by env injection. Still carried
  // on existing chats and accepted by the API, but nothing reads it.
  mode: string
  // Provider-native thinking/reasoning level ('' = provider default).
  // Allowed values per provider come from ModelsResponse.thinking_levels.
  thinking_level?: string
  session_id: string
  created_at: string
  archived: boolean
  last_activity_at?: string
  /**
   * Compact origin metadata for a memory pass: its source's title and archive path.
   * The source may be archived, so it is not in the active chat list; this is how
   * the pass reaches it.
   */
  memory_source?: ArchivedChatSummary & { last_activity_at: string }
  last_read_at?: string
  // Truncated text of the last assistant reply, persisted alongside
  // `last_activity_at`. Backs the sidebar unread tile's preview on initial
  // load, before any live `chat_result_ready` WS event has arrived.
  last_snippet?: string
  // One-line snippet of the error that ended the last turn. Empty or absent
  // when the last turn did not fail; cleared when a new turn starts.
  last_error?: string
  failure_id?: string
  recovery_eligible?: boolean
  user_turn_count?: number
  local?: boolean
  // Transient UI flag: 'pending' while the server is auto-titling a brand
  // new chat, 'ready' otherwise. Drives the shimmer placeholder in the
  // sidebar.
  title_status?: 'pending' | 'ready'
  // Relative workspace path to the archived markdown transcript.
  archive_path?: string
  // Raw AskUserQuestion JSON (`{"questions": [...]}`) when the chat is paused on
  // an unanswered question. Lets the PWA rebuild the picker after a reload.
  // Cleared by the server on the next user send.
  pending_question?: string
  // Raw PermissionRequestEvent JSON (`{request_id, tool_name, message,
  // tool_input}`) when the chat is blocked mid-turn on an unanswered
  // Approve/Deny prompt. Lets the PWA rebuild the card after a reload and
  // count the chat as needing attention even when it isn't the foreground
  // chat receiving the live WS stream. Cleared by the server on answer or
  // turn end.
  pending_permission?: string
  // Follow-ups parked when a turn ended without flushing them (Stop, error,
  // question pause, native question, retry, task-board stop). Each entry is
  // {id, text, images}. Parked entries are never re-seeded by a later send:
  // they leave only through Send now, edit, remove or reorder, so the list
  // is the chat's waiting chips.
  pending_queue?: Array<{ id: string; text: string; images?: string[] }>
  pending_takes?: Array<{ entry_id: string; text: string; images?: string[] }>
  retry?: ChatRetryInfo | null
  forked_from_chat_id?: string
  forked_from_turn_index?: number | null
  fork_root_chat_id?: string
  fork_index?: number
  fork_base_title?: string
  // Backlink to the schedule that created or drives this chat. Empty for
  // interactive chats. Drives the "triggered by schedule X" banner in
  // ChatPanel (mirrors the loop banner, but durable across schedule runs
  // because a schedule spawns a new chat each time).
  schedule_id?: string
  schedule_title?: string
  helper?: {
    // A memory pass: the background chat that distils an archived chat into
    // memory. `state === 'attention'` means it ended unclean and needs the owner.
    kind: 'memory_pass'
    source_chat_id: string
    archive_path: string
    doc_path: string
    source_title: string
    source_project: string
    state: 'queued' | 'running' | 'done' | 'attention'
    archive_policy: 'when_clean'
  } | {
    // A delegated board task's chat (ciao/task_attempts.py::task_delegation_helper):
    // the only record of which task and attempt the chat belongs to.
    kind: 'task_delegation'
    task_id: string
    task_revision: string
    attempt_id: string
  }
  // The memory pass spawned for this archived chat, as recorded on the source
  // chat (ciao/web/memory_pass.py). Present only on archived chats that queued
  // one.
  postprocess?: ChatPostprocess | null
  // Server-owned per-chat pinned file (canonical absolute POSIX path, '' when
  // nothing is pinned). The engine is the only writer of this state; the PWA
  // consumes it and PATCHes it via the `pin` body, never from localStorage.
  pinned_file_path?: string
  dismissed_pin_paths?: string[]
  pin_revision?: number
  // Needs-you actions the engine holds for this chat (an MCP sign-in, ...).
  actions?: ChatAction[]
  // Server sends `{}` for no location; test `repo_location.path`, not the object.
  repo_location?: Partial<RepoLocation>
}

/**
 * The server's authoritative per-chat pin state, as carried by `ChatInfo`,
 * the `chat_pin_changed` event, and the `/ws/events` snapshot's `chat_pins`
 * map. `path` is the canonical absolute POSIX path ('' = closed/dismissed),
 * `dismissed_paths` are the paths the user has explicitly closed, and
 * `revision` is the optimistic-concurrency counter every PATCH must present.
 */
export interface ChatPinState {
  path: string
  dismissed_paths: string[]
  revision: number
}

/** A needs-you kind the engine can raise on a chat. */
export type ChatActionKind = 'command' | 'permission_mode' | 'device_code' | 'system_setting' | 'restart' | 'limit'

/** Where a needs-you action is in its life. */
export type ChatActionState = 'open' | 'working' | 'waiting' | 'done' | 'failed'

/**
 * A transient needs-you action on a chat. The engine owns it and sends it on
 * the chat row, the snapshot and `chat_actions_changed`. `params` holds only
 * strings; the PWA composes all card copy from kind and params.
 */
export interface ChatAction {
  id: string
  kind: ChatActionKind
  origin: 'engine' | 'agent'
  state: ChatActionState
  params: Record<string, string>
  error: string
  created_at: number
}

/** The memory pass's step on an archived chat's postprocess record. */
export interface ChatPostprocessStep {
  status: 'ok' | 'queued' | 'running' | 'attention'
  extra?: Record<string, unknown>
}

export interface ChatPostprocess {
  /** Keyed by step id; only `memory_pass` is written. */
  steps?: Record<string, ChatPostprocessStep>
  updated_at?: string
}

export interface ChatRetryInfo {
  status: '' | 'pending' | 'stopped'
  next_at: string
  last_error: string
  attempts: number
  interval_seconds: number
  kind?: string
  mode?: 'continue_recovery' | 'literal'
  schedule_id?: string
}

/** Who the engine wrote an injected user turn for (ChatInfo.user_turn_origin). */
export type TurnOrigin =
  | 'background_run' | 'cli_task_wake' | 'retry' | 'schedule' | 'loop' | 'task_delegation'
  | 'task_resume' | 'webhook' | 'compaction' | 'restart_resume' | 'handover' | 'chat_action'
  | 'loop_stop'

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  tool_name?: string
  is_error?: boolean
  effective_model?: string
  // Populated when tool_name === '_subagent': one subagent call's card, rendered in the
  // trace with its nested children. `steps` is bounded; `step_count` is the full count.
  subagent?: SubagentCard
  // Populated when tool_name === '_shell': one shell call's card, its command, state and output tail.
  shell?: ShellCard
  usage?: Record<string, string>
  quota?: Record<string, unknown>
  images?: string[]
  // True when this user turn was fired by a loop or schedule rather than
  // typed. Drives the ↻ marker on the bubble, so a self-driven turn is not
  // mistaken for something the reader sent. Only present on user messages.
  unattended?: boolean
  // Set when the engine wrote this user turn (wake, automation, webhook, ...); drawn as a notice card.
  origin?: TurnOrigin
  // Engine-recorded context pack a provider handover sent; on the handover marker only.
  sent_context?: string
  // Monotonic per-chat user-turn index. Server-assigned; used to dedup
  // user_echo events replayed on WS reconnect against already-rendered
  // history or an optimistic local push. Only present on user messages.
  turn_index?: number
  // The client send id of the client's OWN optimistic bubble for this turn.
  // The engine records the same id per user turn and echoes it on the
  // user_echo, so the client can tell its bubble from the server's copy of
  // the same turn and prove that a send was delivered. Only present on the
  // client-rendered copy of a user message.
  send_id?: string
  // Server-reported agent latency for the final assistant bubble of a turn,
  // in milliseconds. Drives the footer "· 7.3s" label.
  duration_ms?: number
  // Populated when tool_name === '_filecard'. Drives the inline preview card
  // rendered alongside the activity trace. `file_path` is whatever the agent
  // told us; absolute host paths are intentionally supported by the viewer.
  file_path?: string
  action?: string
  tool?: string
  // Provider-native assistant-message phase. Commentary stays in the reasoning
  // trace; only final_answer is eligible for the terminal response bubble.
  // Undefined keeps the legacy last-assistant-message inference.
  phase?: 'commentary' | 'final_answer'
  // Paginated-history annotations (envelope mode only). `i` is the row's
  // absolute index in the server's full assembled history; `lazy` marks a
  // pruned row whose full content must be fetched from the part endpoint.
  i?: number
  lazy?: boolean
  full_length?: number
}

// ── WebSocket events ────────────────────────────────────────────────────

export type WsEvent =
  // parent_tool_use_id is set when the event came from inside a Task
  // subagent. Its value is the parent's tool_use_id for the Task dispatch,
  // so the client can look up the subagent's description and label the
  // line in the trace ("[Explore] $ Bash …").
  | {
      type: 'text_delta';
      text: string;
      parent_tool_use_id?: string;
      phase?: 'commentary' | 'final_answer';
    }
  | {
      type: 'tool_use';
      tool_name: string;
      tool_input?: string;
      tool_use_id?: string;
      parent_tool_use_id?: string;
      request_id?: string;
      session_id?: string;
      // Set by the backend when the tool mutates a file on disk. The PWA
      // renders this as a standalone inline preview card instead of folding
      // it into the generic _activity row. Path may be workspace-relative
      // or absolute; the viewer enforces file-type and size allowlists.
      file_touch?: { file_path: string; action: string };
      // Populated when one shell command creates/overwrites multiple files.
      file_touches?: Array<{ file_path: string; action: string }>;
      // A shell call (ACP execute kind): its card is keyed by tool_use_id, `tool_input` is the command.
      shell?: boolean;
      shell_background?: boolean;
    }
  // A call's completion, as its own frame. OpenCode emits one after every
  // call; Claude emits none. It marks the call's existing trace line
  // instead of painting a second one (#1416).
  | {
      type: 'tool_result';
      tool_name?: string;
      tool_use_id?: string;
      is_error?: boolean;
      detail?: string;
      parent_tool_use_id?: string;
      session_id?: string;
      // A subagent's launch acknowledgement: the subagent keeps running.
      background?: boolean;
      // A subagent's terminal child status (completed, failed, interrupted).
      subagent_status?: string;
      // A shell call's facts; each is sent only when set.
      exit_code?: number;
      output_tail?: string;
      shell_id?: string;
      shell_pid?: number;
      output_path?: string;
    }
  | { type: 'thinking'; text: string; parent_tool_use_id?: string }
  | { type: 'status'; message: string }
  | { type: 'model_changed'; model: string }
  // Running token totals for the in-flight turn (cumulative, monotonic).
  // Emitted from partial stream events so the live trace can show a token
  // count as the model works; the authoritative totals still land on `result`.
  | { type: 'token_usage'; input_tokens: number; output_tokens: number }
  // `stopped` marks the synthetic result a user Stop publishes: a real turn
  // never carries it. The partial text still renders, but the turn is not an
  // answer, so it must not raise an unread badge on a backgrounded tab.
  | { type: 'result'; text: string; is_error: boolean; effective_model: string; usage: Record<string, string>; quota?: Record<string, unknown>; session_id: string; stopped?: boolean; fallback_final?: boolean; sent_at?: string; completed_at?: string; duration_ms?: number }
  | { type: 'permission_request'; tool_name: string; tool_input?: string; shell_command?: boolean; shell_cwd?: string; message: string; request_id: string; session_id?: string }
  | {
      type: 'permission_response_result';
      request_id: string;
      session_id?: string;
      ok: boolean;
      error?: string;
      retryable?: boolean;
      expired?: boolean;
      already_resolved?: boolean;
    }
  | { type: 'permission_resolved'; request_id: string; session_id?: string }
  // The selected model cannot see the attached images; the engine asks the
  // user to pick a vision-capable model before dispatching. Answered via a
  // `capability_response` client message (action switch | picker | cancel).
  | {
      type: 'model_capability_question';
      request_id: string;
      missing: string;
      current_model: string;
      candidates: Array<{
        id: string;
        label: string;
        supports_vision?: boolean;
        disabled?: boolean;
      }>;
      timeout_s: number;
    }
  | { type: 'chat_title'; chat_id: string; title: string }
  | {
      type: 'user_echo';
      text: string;
      images?: string[];
      turn_index?: number;
      sent_at?: string;
      unattended?: boolean;
      entry_id?: string;
      origin?: TurnOrigin;
      // The client send id this turn owns. It identifies the send exactly, so
      // the client can tell its own optimistic bubble from the server's copy.
      send_id?: string;
      // True when the engine already owns this send id: the frame was a
      // repeat of a turn that ran, and no second turn was started.
      duplicate?: boolean;
    }
  // A tool call was refused (user Deny, or the auto-deny on an unattended
  // run). The call never executed, so any file card already painted for this
  // tool_use_id has to be retracted.
  | { type: 'tool_denied'; tool_use_id: string }
  | {
      type: 'question_response_result';
      request_id: string;
      session_id?: string;
      ok: boolean;
      state: 'answered' | 'cancelled' | 'rejected';
      error?: string;
      retryable?: boolean;
      delivered_as_message?: boolean;
      already_resolved?: boolean;
    }
  | { type: 'question_resolved'; request_id: string; session_id?: string }
  | { type: 'queued'; id?: string; text: string; images?: string[]; waiting?: boolean; send_id?: string }
  | { type: 'queue_state'; queue: Array<{ id: string; text: string; images?: string[]; waiting?: boolean }> }
  | { type: 'queue_take_result'; request_id: string; entry_id: string; ok: boolean; text: string; images: string[] }
  // Stop cancelled a prompt before the provider picked it up: its user turn
  // never happened. The prompt comes back as a waiting queue entry.
  | { type: 'prompt_cancelled'; turn_index: number }
  | { type: 'error'; message: string }
  | { type: 'auth_required'; message?: string }
  // Server is draining for restart; client should show RestartNotice, not
  // treat this as a chat failure.
  | { type: 'server_restarting'; message: string }
  | { type: 'chat_retry'; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number; kind?: string; mode?: 'continue_recovery' | 'literal'; schedule_id?: string }
  // Idle heartbeat from the broker / events hub. Clients treat it as a
  // liveness signal only and must not mutate chat UI state.
  | { type: 'keepalive' }

// Global awareness events from /ws/events
export type EventsWsMessage =
  | { type: 'keepalive' }
  | { type: 'snapshot'; active_streams: { chat_id: string; project_id: string; background?: boolean }[]; restarting?: boolean; chat_pins: Record<string, ChatPinState>; chat_actions?: Record<string, ChatAction[]>; running_shells?: Record<string, number>; keyboard_shortcuts?: Record<string, string>; keyboard_send_mode?: 'modifier' | 'enter'; keyboard_revision?: string; appearance?: AppearancePayload | null }
  | { type: 'keyboard_settings_changed'; keyboard_shortcuts: Record<string, string>; keyboard_send_mode: 'modifier' | 'enter'; revision: string }
  | ({ type: 'appearance_settings_changed' } & AppearancePayload)
  | { type: 'chat_pin_changed'; chat_id: string; path: string; dismissed_paths: string[]; revision: number }
  | { type: 'chat_actions_changed'; chat_id: string; actions: ChatAction[] }
  // How many background shells the chat's live agent session runs now; 0 clears the mark.
  | { type: 'chat_shells'; chat_id: string; project_id: string; running_shells: number }
  | { type: 'chat_created'; chat: ChatInfo }
  | { type: 'chat_streaming_started'; chat_id: string; project_id: string; background?: boolean }
  | { type: 'chat_streaming_done'; chat_id: string; project_id: string; is_error: boolean; last_error?: string; failure_id?: string; repo_location?: Partial<RepoLocation> }
  | { type: 'chat_repo_location'; chat_id: string; project_id: string; repo_location: Partial<RepoLocation> }
  | { type: 'chat_result_ready'; chat_id: string; project_id: string; title: string; snippet: string; memory_pass?: boolean }
  | { type: 'chat_read'; chat_id: string; last_read_at: string }
  | { type: 'chat_unread'; chat_id: string; last_read_at: string }
  | { type: 'chat_title'; chat_id: string; title: string; status?: 'pending' | 'ready' }
  | { type: 'chat_moved'; chat_id: string; project_id: string; old_project_id: string }
  | { type: 'chat_archived'; chat_id: string; project_id: string; archive_path?: string }
  | { type: 'chat_postprocess'; chat_id: string; project_id: string; postprocess: ChatPostprocess | null }
  | { type: 'chat_deleted'; chat_id: string; project_id: string; reason?: string }
  | { type: 'chat_retry'; chat_id: string; project_id: string; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number; kind?: string; mode?: 'continue_recovery' | 'literal'; schedule_id?: string }
  | { type: 'project_created'; project: ProjectInfo }
  | { type: 'project_updated'; project: ProjectInfo }
  | { type: 'project_deleted'; project_id: string }
  | { type: 'projects_reordered'; workspace: string; order: string[] }
  // An automation was created, edited, paused, resumed, or deleted. Carries no
  // payload: the client refetches /api/schedules, which is the only place the
  // computed running/next_run fields are assembled.
  | { type: 'schedules_changed' }
  // A board task or one of its attempts changed in `workspace` (the same name
  // `/api/tasks?workspace=` takes). No payload: the client re-reads the board.
  | { type: 'tasks_changed'; workspace: string }
  // A workspace was archived, restored or renamed (here or on another
  // device). Only a rename carries `from`/`to`; the client refetches
  // /api/workspaces either way.
  | { type: 'workspaces_changed'; from?: string; to?: string }
  | { type: 'server_restarting'; message?: string }
  | { type: 'server_restart_cancelled' }
  | { type: 'auth_required'; message?: string }

export interface InAppToast {
  id: number
  // Chat this toast points at; '' for global error toasts not tied to a chat.
  chat_id: string
  // Result events can arrive before the chat list has been reconciled.
  project_id?: string
  title: string
  body: string
  // 'error' toasts persist until dismissed and show a "Fix" action.
  variant?: 'info' | 'error'
  // Raw error log used to seed a fix chat when variant === 'error'.
  errorText?: string
  // When set on an error toast, the Fix action navigates to this route instead
  // of opening a fix chat — for errors whose remediation lives in Settings.
  fixRoute?: string
  // Button label for the Fix action when fixRoute is set.
  fixLabel?: string
  // An external link shown as a small action on an info toast (e.g. "What's
  // new" on the update-available toast). Opens in a new tab.
  linkUrl?: string
  linkLabel?: string
  // An in-app action button on an info toast, which is dismissed after it runs.
  actionLabel?: string
  onAction?: () => void
  // When set, "Fix" becomes "Restore draft": reopens `text` as a fresh chat
  // draft in `projectId` (falling back to General if that project is gone)
  // instead of the error/fixRoute flow. `originalChatId` is the dead chat's
  // id, cleared from storage once the text has a new home or is dismissed.
  // `workspace`, when known, is the draft's original workspace so the
  // General fallback opens there instead of whichever workspace happens to
  // be active when the user clicks Restore.
  restoreDraft?: { originalChatId: string; projectId: string; text: string; workspace?: string }
}

// A pending approval surfaced to the user by Auto mode's classifier. One
// of these sticks to the chat bubble until the user clicks Approve or Deny,
// at which point the client sends a `permission_response` on the chat WS.
export interface PendingPermission {
  request_id: string
  session_id?: string
  tool_name: string
  tool_input: string
  shell_command?: boolean
  shell_cwd?: string
  message: string
  // Epoch ms when the request arrived — used by the UI to grey out very old
  // pending prompts that were likely cancelled server-side on a stream end.
  received_at: number
}

// ── Schedules ───────────────────────────────────────────────────────────

export type ScheduleArchivePolicy = 'manual' | 'auto'

export interface Schedule {
  schedule_id: string
  // Opaque token for the editable content; sent back on update/delete so the
  // engine refuses an edit made from a row that has changed since.
  revision?: string
  daily_time_utc: string
  prompt: string
  chat_id: number
  created_at: string
  timezone_name: string
  last_triggered_on: string
  last_dispatched_at?: string
  last_run_chat_id?: string
  last_dispatch_id?: string
  days_of_week: string[] | null
  thread_id: number | null
  context_label: string
  // Whether the schedule's target project/chat still resolves. Explicit
  // because context_label is always set, so its truthiness says nothing
  // about whether the target is still there.
  context_available?: boolean
  // 'interval' is the sub-day cadence that replaced loops: it fires
  // interval_minutes after its last run rather than at a time of day.
  frequency: 'daily' | 'weekly' | 'monthly' | 'manual' | 'once' | 'interval'
  interval_minutes: number
  // Outcome of the most recent interval run. Interval entries have no expected
  // wall-clock slot, so `missed` is always false for them and this is what
  // reports their health instead. A wall-clock entry carries it too when its
  // last dispatch failed ('error'): the missed-run check alone reports that
  // far too late to be actionable — see project_chats.dispatch_schedule.
  // 'skipped' is what the dispatcher records when a run reached the provider
  // but stopped short of a result (approval card, AskUserQuestion, deferred
  // retry) — see project_chats._schedule_dispatch_status.
  // 'retrying' means the agent could not reach an external service and armed a
  // network retry in the run chat; the retried turn writes 'ok' or 'error'
  // (schedules.RUN_STATUS_RETRYING).
  last_status?: '' | 'running' | 'ok' | 'error' | 'busy' | 'missing-chat' | 'skipped' | 'retrying'
  day_of_month: number | null
  run_at_date: string | null
  web_chat_id: string | null
  web_project_id: string | null
  workspace: WorkspaceName
  model: string
  provider?: RuntimeProvider
  effective_model?: string
  effective_provider?: RuntimeProvider
  next_run: string | null
  last_expected_run: string | null
  missed: boolean
  enabled: boolean
  archive_policy: ScheduleArchivePolicy
  title?: string
  description?: string
  // Optional shell command run before the prompt; empty when there is none.
  command?: string
  scope?: string
  editable?: boolean
  removable?: boolean
  // A chat-owned loop (see lib/loops.ts). Set only on loops: the chat that
  // owns it, its end (ISO UTC), its turn cap and the turns it has started.
  owner_chat_id?: string
  expires_at?: string
  max_runs?: number
  run_count?: number
}

// ── Webhook triggers ─────────────────────────────────────────────────────

/**
 * The body a sender may post. `event_text` is the only policy the store has, so
 * this is a named type rather than a bare string: the body shape a recipe shows
 * is derived from the policy, and there is one policy.
 */
export type WebhookInputPolicy = 'event_text'

/**
 * One configured webhook trigger, as the public record the store returns.
 *
 * **There is no field here that holds a credential.** The engine keeps a
 * verifier beside the record and never serialises it, so a trigger read from
 * `GET /api/webhooks` cannot carry a secret — and neither may a type the PWA
 * writes. The one-time secret arrives top-level in the create and rotate
 * responses instead, and never again.
 *
 * `project_id: null` is the workspace's General project, not "unset".
 * `project_id` and `input_policy` are create-only: `PATCH` accepts `name`,
 * `instructions` and `enabled` and refuses everything else.
 */
export interface WebhookTrigger {
  trigger_id: string
  name: string
  workspace: WorkspaceName
  project_id: string | null
  instructions: string
  enabled: boolean
  input_policy: WebhookInputPolicy
  created_at: string
  updated_at: string
  /** Optimistic-concurrency counter from 1; every write presents the one it read. */
  revision: number
}

/** `GET /api/webhooks?workspace=` — public records only, never a secret. */
export interface WebhookTriggerResponse {
  triggers: WebhookTrigger[]
}

/**
 * `POST /api/webhooks` and `POST /api/webhooks/{id}/rotate`.
 *
 * `secret` is top-level, beside the record rather than inside it, and it is the
 * only time either response carries one. Nothing re-reads a trigger to find it
 * again, so the PWA keeps it in one dialog and drops it when the dialog closes.
 */
export interface WebhookCreateResponse {
  trigger: WebhookTrigger
  secret: string
}

/**
 * How far one recorded event got.
 *
 * The two open states are not failures — `accepted` is recorded and nothing has
 * been launched yet, `launching` is the durable allocation and the turn has not
 * reported back. `launched` is the **success** outcome (the turn ran; its
 * progress lives in the chat, not here). `interrupted` is the one a person has
 * to look at: a process died inside the launch window, so nobody can tell
 * whether the turn ran.
 */
export type WebhookReceiptStatus = 'accepted' | 'launching' | 'launched' | 'failed' | 'interrupted'

/**
 * One recorded webhook event, as the history read returns it.
 *
 * **A projection, not the journal row.** There is no verifier here and no
 * idempotency key or body digest either: those are the receiver's machinery for
 * collapsing a retry, and "what arrived?" does not ask for them. `chat_id` is the
 * chat a `launched` event became and `null` for every other state — the chat is an
 * ordinary one whose title says nothing about which event made it, so this is
 * the only thing that can point at it.
 *
 * `detail` is the engine's own sentence about the outcome: the chat on a
 * `launched` row, why the launch did not complete on a `failed` one, and why
 * nobody can tell on an `interrupted` one.
 */
export interface WebhookReceipt {
  receipt_id: string
  trigger_id: string
  trigger_name: string
  status: WebhookReceiptStatus
  chat_id: string | null
  event_text: string
  created_at: string
  updated_at: string
  detail: string
}

/**
 * `GET /api/webhooks/{id}/receipts?workspace=` and
 * `GET /api/webhooks/receipts?workspace=`.
 *
 * `limit` is the cap the read was bounded by, so the UI can say what it is
 * showing instead of implying the list is everything the trigger ever received.
 * The per-trigger read adds the trigger's own `trigger_id`/`trigger_name`.
 */
export interface WebhookReceiptsResponse {
  limit: number
  receipts: WebhookReceipt[]
}

// ── Status & Models ─────────────────────────────────────────────────────

export interface StatusResponse {
  active_model: string
  mode: string
  cost: number
}

export interface ModelsResponse {
  models: string[]
  default: string
  // Keyed by provider id: claude, opencode.
  provider_models: Record<string, string[]>
  provider_defaults: Record<string, string>
  // Models reachable through opencode's connected backends, already
  // namespaced as `providerID/modelID`. Empty when nothing is authenticated.
  opencode_models?: string[]
  // Registry-driven provider descriptors, so the PWA never has to hard-code
  // the set of runtime providers. See `ciao/provider_registry.py`.
  providers?: ProviderDescriptor[]
  model_reasoning_levels?: Record<string, string[]>
  backends?: Record<string, boolean>
  // Keyed by runtime provider; Claude and opencode levels may be narrowed by
  // model_reasoning_levels.
  thinking_levels?: Record<string, string[]>
  provider_catalog_evidence?: Record<string, ProviderCatalogEvidence>
}

export interface ProviderCatalogEvidence {
  status: 'observed' | 'unknown'
  source: 'claude_initialize' | 'opencode_catalog' | null
  // `description` is the agent's own note on an entry; for Claude Code's
  // `default` it names the concrete model that entry runs ("Opus 4.5").
  models: Array<{ id: string; label: string; description?: string }>
  reason: 'not_observed' | 'expired' | 'invalid_response' | 'no_models_reported' | null
}

// GET/PATCH /api/settings/routines — internal-routine model overrides and
// model overrides (Settings → Providers tab).
export interface RoutineSettings {
  // Per-provider default model for new chats; a missing entry = the provider's
  // own catalog default.
  provider_default_models?: Record<string, string>
  // Per-provider default execution (permission) mode for new chats:
  // manual | auto | bypass. Missing = the app default (auto).
  provider_default_modes?: Record<string, string>
  // Per-provider default thinking level for new chats; missing = provider default.
  provider_default_thinking?: Record<string, string>
  // Per-provider Session insights models; missing = the source chat's model.
  provider_insights_models?: Record<string, string>

  // Server settings that used to be workspace .env variables, as stored
  // ("" = default). The bind address and log level apply at the next start.
  pwa_host?: string
  log_level?: string
  dev_mode?: boolean
  app_repo?: string
  // Settings → General: a chat whose turn a restart or crash cut off gets one
  // "Continue after restart" turn at the next start. Missing = on.
  resume_after_restart?: boolean
  server_defaults?: {
    pwa_host: string
    log_level: string
    log_levels: string[]
  }
  // What the running engine bound and logs at.
  server_running?: {
    pwa_host: string
    log_level: string
  }
  model_options: {
    anthropic: string[]
  }
  backends?: Record<string, boolean>
  workspace_context?: {
    workspace_root: string
    vault_root: string
  }
}

/** Action ids the engine can run in a terminal on its own machine (ciao/host_terminal.py::ACTION_IDS). */
export type HostTerminalAction =
  | 'claude-install'
  | 'claude-login'
  | 'opencode-install'
  | 'opencode-login'
  | 'codex-install'
  | 'codex-login'
  | 'skills-update'
  | 'opencode-update'
  | 'claude-update'

/** `GET /api/host/terminal`. */
export interface HostTerminalCapabilities {
  available: boolean
  platform: string
  reason: string
  terminal: string
  actions: HostTerminalAction[]
}

/** `POST /api/host/terminal` on success. */
export interface HostTerminalLaunchResult {
  ok: boolean
  platform: string
  launched: string
}

/** What `ciao/provider_versions.py::release_check` says about an installed CLI. */
export interface ProviderReleaseCheck {
  state: 'untested' | 'unsupported' | 'unknown' | 'below_tested' | 'current'
  tested?: string
  installed?: string
  command?: string
  terminal_action?: HostTerminalAction
}

/**
 * A provider row's state, from `ciao/setup_status.py`: `not_installed` is a CLI
 * the user never installed (neutral, not an error); `adapter_missing` is a CLI
 * present without its ACP adapter; `ready` and `failed` are the rest.
 */
export type ProviderState = 'not_installed' | 'adapter_missing' | 'ready' | 'failed'

export interface ProviderConnection {
  name: string
  // Display names from the backend registry, so the Settings card never has to
  // map provider ids to product names itself.
  label?: string
  short_label?: string
  ok: boolean
  auth: string
  /** Explicit install and readiness state; the Settings card and the status rail read this. */
  state: ProviderState
  command: string
  detail?: string
  version?: string
  account?: string
  credential_inventory?: {
    status: 'complete' | 'partial' | 'unknown'
    providers: {
      id: string
      name: string
      auth_types: ('oauth' | 'api_key' | 'environment' | 'unknown')[]
      configured: true
      verified: null
    }[]
  }
  protocol?: string
  skills?: string[]
  /** Skills that ship with the CLI itself; absent until the CLI has reported them. */
  bundled_skills?: string[]
  /** Docs page for installing the CLI, set when `auth === 'not_installed'`. */
  install_url?: string
  /** Shell line that puts the CLI on PATH (`not_installed`, `missing`, `cli_too_old`). */
  path_command?: string
  /** Terminal action that runs the command shown for this row, when the engine offers one. */
  terminal_action?: HostTerminalAction
  /** The agent CLI's launch command (`claude`, `opencode`, `codex`), typed into the in-app terminal. Absent when the CLI is not installed. */
  mcp_command?: string
  /** The input typed after the CLI has started, once it has drawn its screen: the slash command that opens its MCP view (`/mcp`, or `/mcps` for OpenCode). */
  mcp_slash?: string
  /** How the installed CLI compares with the version this release was tested with. */
  release_check?: ProviderReleaseCheck
  /** Desktop app found while the CLI is missing. */
  app_path?: string
  /** Absolute path of the CLI binary Ciaobot would run. */
  cli_path?: string
}

export interface ProviderConfigSettings {
  connections?: Record<string, ProviderConnection>
}

export interface AdminStatus {
  cost: number
  branch: string
  models: string[]
  default_model: string
  default_mode: string
}

export interface LocalStatus {
  git_repo: boolean
  branch: string | null
  dirty: boolean
  dev_mode?: boolean
  restart_only?: boolean
}

export interface DeployResult {
  ok: boolean
  steps: { step: string; ok: boolean; output?: string }[]
}

export interface DebugIssueReport {
  error_log: string
  error_log_records: number
  error_log_path: string
  failed_jobs: { job: string; label: string; ended_at: string; error: string }[]
  has_issues: boolean
  report_text: string
}

// ── Settings skill inventory ───────────────────────────────────────────

export interface SkillInventoryItem {
  name: string
  label: 'custom' | 'stock'
  source: string
  source_type: string
  description: string
  path: string
  content?: string
  installed_targets: string[]
}

export interface SkillInventory {
  counts: {
    custom: number
    stock: number
  }
  skills: SkillInventoryItem[]
}

// ── Settings command inventory ───────────────────────────────────────────

export interface SlashCommand {
  name: string
  description: string
  argument_hint: string
  source: 'project' | 'user' | 'builtin' | 'skill'
  path: string
}

export interface CommandsResponse {
  commands: SlashCommand[]
  skills?: SlashCommand[]
}

// ── Settings agent assets ────────────────────────────────────────────────

export interface AgentAssetsResponse {
  subagents: SubagentAsset[]
  commands: CommandAsset[]
  health?: WorkspaceHealthResponse
}

export interface SubagentAsset {
  name: string
  description: string
  source: string
  scope: string
  path: string
  editable: boolean
  vault_path: string
  content: string
}

export interface CommandAsset {
  name: string
  description: string
  argument_hint: string
  source: string
  scope: string
  path: string
  editable: boolean
  vault_path: string
  content: string
}

export interface CreatedAgentAssetResponse<T> {
  ok: boolean
  asset: T
  path: string
  vault_path: string
}

export interface WorkspaceHealthCheck {
  id: string
  title: string
  status: 'ok' | 'warn' | 'error' | string
  detail: string
  path: string
  action: string
}

export interface WorkspaceHealthResponse {
  status: 'ok' | 'warn' | 'error' | string
  checks: WorkspaceHealthCheck[]
}

/**
 * What the small action endpoints answer with: `/api/package/update`.
 * Callers only branch on `ok` and show `error`; the rest of the body is
 * diagnostic, hence the index signature.
 */
export interface ActionResult {
  ok?: boolean
  error?: string
  [key: string]: unknown
}

/**
 * `POST /api/chats/{id}/archive`.
 *
 * The fields are optional so a client talking to an older engine degrades to
 * "the chat I asked for" instead of breaking.
 */
export interface ArchiveChatResponse {
  ok?: boolean
  archived_to?: string | null
  postprocess?: ChatPostprocess | null
}

/** One commit row in the update modal, from ciao/package_version.py. */
export interface ChangelogCommit {
  sha: string
  subject: string
}

/** `/api/package/changelog`. */
export interface PackageChangelog {
  commits: ChangelogCommit[]
  compare_url: string
  repo?: string
  error: string
  current_version?: string
  latest_version?: string
  update_available?: boolean
}

/** `/api/package/update`. */
export interface PackageUpdateResult {
  ok?: boolean
  mode?: string
  output?: string
  command?: string
  error?: string
}

/** One provider row inside {@link SetupStatus}. */
export interface SetupProviderStatus {
  ok: boolean
  auth?: string
  /** Explicit install and readiness state the engine computed; read this, never the detail text. */
  state: ProviderState
  command?: string
  detail?: string
  /** Documentation page for installing the provider CLI (`auth: 'not_installed'`). */
  install_url?: string
  /** Shell line that puts the CLI on PATH (`not_installed`, `missing`, `cli_too_old`). */
  path_command?: string
  /** Terminal action that runs the command shown for this row, when the engine offers one. */
  terminal_action?: HostTerminalAction
  /** Desktop app found while the CLI is missing, so setup can say which step is left. */
  app_path?: string
  /** Absolute path of the CLI binary Ciaobot would run. */
  cli_path?: string
}

/** One requirement row inside {@link SetupStatus}. */
export interface SetupCheck {
  ok: boolean
  required?: boolean
  detail?: string
  command?: string
  [key: string]: unknown
}

/** `/api/setup-status`, from ciao/setup_status.py::setup_status. */
export interface SetupStatus {
  configured: boolean
  bootstrap: boolean
  mode: string
  workspace_root: string
  vault_root: string
  checks: SetupCheck[]
  providers: Record<string, SetupProviderStatus>
  provider_ready: boolean
}

/** `/api/settings/providers/{provider}/{connect|logout|check}`. */
export interface ProviderActionResult {
  ok?: boolean
  launched?: string
  command?: string
  auth?: string
  detail?: string
}

/** `/api/local/handback`. */
export interface LocalHandbackResult {
  ok?: boolean
  step?: string
  error?: string
  merged?: boolean
  conflict?: boolean
}

export interface PackageStatus {
  current_version?: string
  latest_version?: string
  update_available?: boolean
  mode?: string
  error?: string
  /** Public URL that redirects to the latest release page. */
  source?: string
}

/** One home-screen operator action (see `ciao/operator_actions.py`). */
export interface OperatorAction {
  id: string
  kind: string
  severity: number
  title: string
  detail: string
  glyph: string
  workspace: string
  run_label: string
  chat_label: string
  chat_prompt: string
  /** A purpose-built surface for this action, when one already exists. */
  view_label: string
  view_route: string
  /** An external page this action links to (release notes, the repository).
   *  Rendered like a run button but opens a new tab. */
  link_label?: string
  link_url?: string
  /** A "not now" button for ask-style actions; records a suppression receipt. */
  dismiss_label?: string
  /** Which button leads the tile: "view" makes the view button the filled
   *  primary and demotes the link to a chip. Empty keeps the default order. */
  primary?: string
  /** A precondition the install cannot get past on its own: unmissable and not
   *  dismissible. Deliberately not an app-wide lock. */
  blocking: boolean
  /** Host-terminal action the strip may offer on the engine's computer. */
  terminal_action?: HostTerminalAction
  /** The same command as copyable text. */
  command?: string
}

export interface HousekeepingResponse {
  actions: OperatorAction[]
}

export interface HousekeepingRunResponse {
  ok: boolean
  action_id: string
  error?: string
  summary: string
  result?: Record<string, unknown>
  actions: OperatorAction[]
}

export interface HousekeepingDismissResponse {
  ok: boolean
  action_id: string
  error?: string
  summary: string
  result?: Record<string, unknown>
  actions: OperatorAction[]
}

// ── Task board: /api/tasks* ───────────────────────────────────────────────
//
// The shape is `ciao/control_plane.py::_task_payload`, copied rather than
// invented. `revision` is a **string** (the SHA-256 of the file's exact bytes),
// not a number: every write has to present it back, which is what makes a stale
// edit a 409 instead of a lost update, so a client that rounded it into a number
// would be sending back a revision the server never issued.

/** The four board columns, in board order. */
export type TaskStatus = 'backlog' | 'in_progress' | 'in_review' | 'done'

/** Who the task is for. Delegation is B5; the field exists so the card can say. */
export type TaskAssignee = 'user' | 'agent'

/** One readable task file, as `GET /api/tasks` serves it (no `body`). */
export interface Task {
  id: string
  title: string
  status: TaskStatus
  /** Empty when the task belongs to no project; the board reads that as General. */
  project_id: string
  /** `YYYY-MM-DD`, or empty for no date. */
  due: string
  assignee: TaskAssignee
  /** The chat an attempt is working in, empty when nothing is delegated. */
  chat_id: string
  /** The attempt holding the task, empty when nothing is delegated. */
  attempt_id: string
  /** Chats that worked on the task outside delegation; no lifecycle meaning. */
  linked_chats: string[]
  created_at: string
  updated_at: string
  /** SHA-256 of the file's exact bytes, hex. Every write presents the one it read. */
  revision: string
  relative_path: string
  /**
   * The current attempt's state (`ciao/task_attempts.py::ATTEMPT_STATES`), or `''`
   * when the task has never been delegated.
   *
   * The badge's field, and deliberately not "the live attempt's state": `failed`,
   * `interrupted` and `stopped` are settled states a user has to see, so a board
   * reading only live attempts would go blank the moment a turn ended.
   */
  attempt_state: TaskAttemptState | ''
  /** The agent's own report on the current attempt (#1064), or `''`. */
  attempt_outcome: TaskAttemptOutcome | ''
  /** What the agent said it did and what is left, in its words. */
  attempt_summary: string
  /** The engine's note on how the current attempt ended, when it says one. */
  attempt_detail: string
  /**
   * When the latest completion was recorded (`completed_at`), or `null` for a task
   * never completed. Done-today reads this, never `updated_at`: an edit to a done
   * task moves `updated_at` without finishing anything.
   */
  completed_at: string | null
  /** Whether the latest completion carries resolution text. */
  has_resolution: boolean
  /**
   * Non-empty only while an attempt actually holds the task.
   *
   * That is what tells the board whether Stop and Detach are available: a
   * `ready_for_review` attempt is live while the task stays linked, a `stopped`
   * one is not, and a released one — approved or detached — is not either even
   * though its `attempt_state` still reads `ready_for_review`. The badge alone
   * cannot say which, which is why the server sends this beside it.
   */
  live_attempt_id: string
  /**
   * The task's chat is archived. Present only when `chat_id` is set: archived
   * chats are not in the browser's active chat list, so the board reads this
   * instead of looking the chat up.
   */
  chat_archived?: boolean
  /** `'agent'` for a task the agent filed, else `null`. Nothing renders it. */
  created_by?: 'agent' | null
}

/**
 * One delegation attempt's state, as `ciao/task_attempts.py::ATTEMPT_STATES`.
 *
 * `running` and `needs_you` mean a turn is in flight; `ready_for_review` means the
 * provider turn ended and the result waits for the user; `failed`, `interrupted`
 * and `stopped` are settled. None of them is a board column — `ready_for_review` is
 * a badge in *In progress*, and only the user moves a card to *Done*.
 */
export type TaskAttemptState =
  | 'running'
  | 'needs_you'
  | 'failed'
  | 'interrupted'
  | 'ready_for_review'
  | 'stopped'

/**
 * One attempt, as `POST /delegate` and the gesture routes answer with it.
 *
 * `live` is carried rather than derived, so a client cannot disagree with the
 * server about which attempts hold a task.
 */
/** What a delegated agent may report about its own attempt (#1064). */
export type TaskAttemptOutcome = 'done' | 'blocked' | 'needs_input'

export interface TaskAttempt {
  attempt_id: string
  task_id: string
  /** The task revision this attempt was handed, rebound after the linkage write. */
  task_revision: string
  chat_id: string
  state: TaskAttemptState
  created_at: string
  updated_at: string
  /** Empty while the attempt is live; stamped when it settles. */
  ended_at: string
  /** A bounded sentence about the engine's own outcome, never user prose. */
  detail: string
  /** The agent's own report (#1064): `done`, `blocked`, `needs_input`, or `''`. */
  outcome: TaskAttemptOutcome | ''
  /** The agent's summary with that report. */
  summary: string
  /**
   * The user approved this result or detached the card, so the task is free again.
   *
   * A marker and not a state: `state` stays what the turn ended as, which is the
   * record of what the agent did. What it changes is `live`, and a released
   * `ready_for_review` attempt is not live — which is what lets the card be
   * delegated again.
   */
  released: boolean
  live: boolean
  /** The attempt's chat is archived; present only when `chat_id` is set. See `Task.chat_archived`. */
  chat_archived?: boolean
}

/** One task's whole attempt history: the live attempt first, live and settled alike. */
export interface TaskAttemptsResponse {
  workspace: string
  task: TaskDetail
  attempts: TaskAttempt[]
}

/**
 * What `POST /api/tasks/{id}/resolution-review` answers with.
 *
 * `queued: true` names the completion the review will read. `queued: false`
 * carries the server's reason (for example, a task with no resolution to review).
 */
export interface TaskResolutionReviewResponse {
  queued: boolean
  completion_id?: string
  reason?: string
}

/** What `POST /delegate` answers with. `created: false` means nothing was started. */
export interface TaskDelegateResponse {
  workspace: string
  created: boolean
  attempt: TaskAttempt
  chat_id: string
  /** Where the chat was hosted, and whether a project, the task or General chose it. */
  project_id: string
  project_origin: 'requested' | 'task' | 'general' | ''
  task: TaskDetail
}

/** What an attempt gesture answers with. `resume`/`retry` add their own flags. */
export interface TaskAttemptActionResponse {
  workspace: string
  attempt: TaskAttempt
  chat_id: string
  task?: TaskDetail
  resumed?: boolean
  retried?: boolean
}

/**
 * A file the server could not read as a task.
 *
 * `GET /api/tasks` appends one of these per unreadable file rather than dropping
 * it, so a malformed task can never read as an empty or healthy board.
 */
export interface TaskInvalidRow {
  id: string
  path: string
  code: string
  message: string
}

/** What one entry of `GET /api/tasks`'s `tasks` array may be. */
export type TaskRow = Task | TaskInvalidRow

/**
 * The board's persisted card order: one id sequence per status, plus the
 * version the server stamps on every change to it.
 */
export interface TaskOrder {
  version: number
  sequences: Record<TaskStatus, string[]>
}

export interface TaskListResponse {
  workspace: string
  tasks: TaskRow[]
  order: TaskOrder
}

/**
 * The same record with its Markdown description.
 *
 * Every write answers with one (`create`, `update`, `complete`), and there is no
 * read-by-id route to get it for an arbitrary task — which is why the board only
 * ever shows a description it actually holds.
 */
export interface TaskDetail extends Task {
  /** The description alone: the engine's completion and delegation sections are not in it. */
  body: string
  /** The latest completion's resolution text, or `''`. */
  resolution: string
  /** Every completion, newest first. */
  completions: TaskCompletion[]
  /** The engine's delegation log as text, or `''`. */
  delegation_log: string
  /** Progress notes, newest first. Empty when this payload did not carry them. */
  updates: TaskUpdate[]
}

/** One progress note (`ciao/task_updates.py`), as the get route serves it. */
export interface TaskUpdate {
  id: string
  recorded_at: string
  actor: 'user' | 'agent' | string
  text: string
  /** When the note was last reworded, or `null` when it never was. */
  edited_at: string | null
  attempt_id: string
  chat_id: string
}

/** One recorded completion of a task (`ciao/task_resolution.py`), as the get route serves it. */
export interface TaskCompletion {
  id: string
  completed_at: string
  resolution: string
  /** When the resolution was last reworded, or `null` when it never was. */
  edited_at: string | null
  attempt_id: string
}

