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
  subagents: boolean
  background_subagents: boolean
  subagent_messages: boolean
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
  runner?: RuntimeProvider
  default_model?: string
}

export interface WorkspaceInfo {
  name: WorkspaceName
  vault_root: string
  default_provider: WorkspaceProvider
  disallowed_tools?: string[] | null
  gws_profile: string
  // PWA accent preset: pink | cyan | amber | emerald | violet. Missing → pink.
  color?: string
  // Filesystem scope for the agent and its shell. Missing → workspace.
  agent_fs_scope?: 'workspace' | 'machine'
}

export interface WorkspacesResponse {
  workspaces: WorkspaceInfo[]
  active: WorkspaceName | null
  // The workspace that cannot be archived (the server refuses it).
  primary?: WorkspaceName | null
  provider_options?: WorkspaceProviderOption[]
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
  gws_profile: string
  // What a restore applies, after server-side validation. `null` means unset:
  // no MCP server is reachable, and the default tool deny-list applies.
  disallowed_tools?: string[] | null
  allowed_mcp_servers?: string[] | null
  // Valid automations, all restored paused, and how many were rejected.
  schedules: number
  schedules_dropped?: number
  restorable: boolean
  blocked_reason: string
}

export interface ArchivedWorkspacesResponse {
  archived: ArchivedWorkspace[]
}

export interface McpEnvKey {
  key: string
  configured: boolean
  source: string
}

export interface McpProjectServer {
  name: string
  url?: string
  command?: string
  args?: string[]
  transport?: 'http' | 'stdio' | string
  source: string
  config_path?: string
  env_path?: string
  env_keys?: McpEnvKey[]
  ready?: boolean
  tool_prefix?: string
  tools?: string[]
  tools_source?: 'observed' | 'probed' | 'none' | string
  tools_note?: string
}

export interface McpStatus {
  enabled: boolean
  bound: boolean
  url?: string
  tool_count: number
  tools?: string[]
  env_path?: string
  /** The workspace these servers belong to; '' is the install root. */
  workspace?: string
  project_servers?: McpProjectServer[]
  active_sessions?: number
  providers?: string[]
  last_error?: string
}

/** Status of the agent CLI surface (Settings → Agent CLI), from /api/agent/status. */
export interface AgentCliStatus {
  ready: boolean
  operations: string[]
  telemetry_path: string
  version: string
}

export interface McpToolUsage {
  tool: string
  calls: number
  errors: number
  avg_ms: number
  providers: string[]
  last_used: string
}

export interface McpUsage {
  total_calls: number
  total_errors: number
  tool_count: number
  tools: McpToolUsage[]
}

// ── Projects & Chats ────────────────────────────────────────────────────

export interface ProjectInfo {
  project_id: string
  name: string
  workspace: WorkspaceName
  context: string
  created_at: string
  order: number
  vault_folder: string
  vault_doc_path?: string
  is_system?: boolean
  is_auto?: boolean
  // 'memory' marks the app-owned per-workspace Memory project. The PWA hides
  // it from the sidebar; a user project may legitimately be named "Memory".
  kind?: string
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
  last_read_at?: string
  // Truncated text of the last assistant reply, persisted alongside
  // `last_activity_at`. Backs the sidebar unread tile's preview on initial
  // load, before any live `chat_result_ready` WS event has arrived.
  last_snippet?: string
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
  // Follow-ups parked when a turn ended without flushing them (error, question
  // pause, native question, retry, task-board stop). Each entry is
  // {id, text, images}. Empty once the next turn re-seeds the live queue, so
  // an empty list during a live turn is not "no chips".
  pending_queue?: Array<{ id: string; text: string; images?: string[] }>
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
    kind: 'proposal'
    intent: 'resolve' | 'review'
    proposal_ids: string[]
    archive_policy: 'when_resolved' | 'manual'
  } | {
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
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  tool_name?: string
  is_error?: boolean
  effective_model?: string
  usage?: Record<string, string>
  quota?: Record<string, unknown>
  images?: string[]
  // True when this user turn was fired by a loop or schedule rather than
  // typed. Drives the ↻ marker on the bubble, so a self-driven turn is not
  // mistaken for something the reader sent. Only present on user messages.
  unattended?: boolean
  // Monotonic per-chat user-turn index. Server-assigned; used to dedup
  // user_echo events replayed on WS reconnect against already-rendered
  // history or an optimistic local push. Only present on user messages.
  turn_index?: number
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

// Subagent transcripts from /api/chats/{id}/subagents. One entry per subagent
// spawned inside the chat's parent Claude session. Messages share the same
// shape as /messages (role, content, tool_name for _activity rollups).
// Dispatch metadata (tool_use_id, description, status, turn_index) is parsed
// from the parent session JSONL and may be absent for sessions the server
// can't inspect locally. `turn_index` matches the index stamped on user
// bubbles by /messages, anchoring the panel to the dispatching turn.
export interface SubagentTranscript {
  agent_id: string
  parent_agent_id?: string
  messages: ChatMessage[]
  tool_use_id?: string
  description?: string
  subagent_type?: string
  is_async?: boolean
  status?: 'running' | 'completed' | 'failed' | 'stopped' | ''
  turn_index?: number
}

// One live subagent from `/api/subagents/running`. Same dispatch metadata as
// SubagentTranscript, minus the transcript: the sidebar only needs to name the
// agent and say it is working, and polling full transcripts for every working
// chat would be far more than that costs.
export interface RunningSubagent {
  agent_id: string
  description?: string
  subagent_type?: string
  is_async?: boolean
  status?: 'running' | 'completed' | 'failed' | ''
  turn_index?: number | null
}

/**
 * A tracked `background_run_start` command, as /ws/events reports it. No pid,
 * cwd or log path: every connected client receives these.
 */
export interface BackgroundRunSummary {
  run_id: string
  label: string
  cmd: string[]
  started_at: string
  // "queued" | "running" while live; "ok" | "error" | "cancelled" once ended.
  status: string
  exit_code: number | null
}

/** `GET /api/chats/{id}/background-runs/{run}/log`. */
export interface BackgroundRunLog extends BackgroundRunSummary {
  last_lines: string[]
}

/** `GET /api/subagents/running`. Chats with nothing running are omitted. */
export interface RunningSubagentsResponse {
  chats?: Record<string, RunningSubagent[]>
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
  | { type: 'permission_request'; tool_name: string; tool_input?: string; message: string; request_id: string; session_id?: string }
  | {
      type: 'permission_response_result';
      request_id: string;
      session_id?: string;
      ok: boolean;
      error?: string;
      retryable?: boolean;
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
  | { type: 'user_echo'; text: string; images?: string[]; turn_index?: number; sent_at?: string; unattended?: boolean; entry_id?: string }
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
    }
  | { type: 'question_resolved'; request_id: string; session_id?: string }
  | { type: 'queued'; id?: string; text: string; images?: string[] }
  | { type: 'queue_state'; queue: Array<{ id: string; text: string; images?: string[] }> }
  | { type: 'error'; message: string }
  | { type: 'auth_required'; message?: string }
  // Server is draining for restart; client should show RestartNotice, not
  // treat this as a chat failure.
  | { type: 'server_restarting'; message: string }
  | { type: 'chat_retry'; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number }
  // Idle heartbeat from the broker / events hub. Clients treat it as a
  // liveness signal only and must not mutate chat UI state.
  | { type: 'keepalive' }

// Global awareness events from /ws/events
export type EventsWsMessage =
  | { type: 'keepalive' }
  | { type: 'snapshot'; active_streams: { chat_id: string; project_id: string }[]; background_agents?: Record<string, number>; background_runs?: Record<string, BackgroundRunSummary[]>; restarting?: boolean; chat_pins: Record<string, ChatPinState>; keyboard_shortcuts?: Record<string, string>; keyboard_send_mode?: 'modifier' | 'enter'; keyboard_revision?: string }
  | { type: 'keyboard_settings_changed'; keyboard_shortcuts: Record<string, string>; keyboard_send_mode: 'modifier' | 'enter'; revision: string }
  | { type: 'chat_pin_changed'; chat_id: string; path: string; dismissed_paths: string[]; revision: number }
  | { type: 'chat_created'; chat: ChatInfo }
  | { type: 'chat_streaming_started'; chat_id: string; project_id: string }
  | { type: 'chat_streaming_done'; chat_id: string; project_id: string; is_error: boolean }
  | { type: 'chat_result_ready'; chat_id: string; project_id: string; title: string; snippet: string }
  | { type: 'chat_subagents_ready'; chat_id: string; project_id: string; remaining: number; nudged?: boolean }
  | { type: 'chat_background_runs'; chat_id: string; project_id: string; runs: BackgroundRunSummary[]; finished?: BackgroundRunSummary }
  | { type: 'chat_read'; chat_id: string; last_read_at: string }
  | { type: 'chat_unread'; chat_id: string; last_read_at: string }
  | { type: 'chat_title'; chat_id: string; title: string; status?: 'pending' | 'ready' }
  | { type: 'chat_moved'; chat_id: string; project_id: string; old_project_id: string }
  | { type: 'chat_archived'; chat_id: string; project_id: string; archive_path?: string }
  | { type: 'chat_postprocess'; chat_id: string; project_id: string; postprocess: ChatPostprocess | null }
  | { type: 'chat_deleted'; chat_id: string; project_id: string; reason?: string }
  | { type: 'chat_retry'; chat_id: string; project_id: string; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number }
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
  | { type: 'open_chat'; chat_id: string }
  | { type: 'server_restarting'; message?: string }
  | { type: 'server_restart_cancelled' }
  | { type: 'gws_health'; profile: string; token_valid: boolean; token_error: string; title: string; body: string }
  | { type: 'auth_required'; message?: string }

export interface InAppToast {
  id: number
  // Chat this toast points at; '' for global error toasts not tied to a chat.
  chat_id: string
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
  message: string
  // Epoch ms when the request arrived — used by the UI to grey out very old
  // pending prompts that were likely cancelled server-side on a stream end.
  received_at: number
}

// ── Schedules ───────────────────────────────────────────────────────────

export type ScheduleArchivePolicy = 'manual' | 'auto'

export interface Schedule {
  schedule_id: string
  daily_time_utc: string
  prompt: string
  chat_id: number
  created_at: string
  timezone_name: string
  last_triggered_on: string
  last_dispatched_at?: string
  last_run_chat_id?: string
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
  // retry) — see project_chats._schedule_dispatch_status. 'unfinished' is the
  // case split out of it where background subagents never settled: nothing in
  // the chat is waiting on the user (schedules.RUN_STATUS_UNFINISHED).
  last_status?: '' | 'running' | 'ok' | 'error' | 'busy' | 'missing-chat' | 'skipped' | 'unfinished'
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
  scope?: string
  editable?: boolean
  removable?: boolean
}

// ── Webhook triggers ─────────────────────────────────────────────────────

/**
 * How a trigger's turn runs. Set at create and never editable afterwards.
 *
 * The three are the store's `WEBHOOK_MODES`; the receiver accepts nothing else
 * and answers 400 for a mode it does not know.
 */
export type WebhookMode = 'normal' | 'auto' | 'plan'

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
 * `mode` and `input_policy` are create-only: `PATCH` accepts `name`,
 * `instructions` and `enabled` and refuses everything else.
 */
export interface WebhookTrigger {
  trigger_id: string
  name: string
  workspace: WorkspaceName
  project_id: string | null
  instructions: string
  enabled: boolean
  mode: WebhookMode
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
}

// GET/PATCH /api/settings/routines — internal-routine model overrides and
// model overrides (Settings → Models tab).
export interface RoutineSettings {
  insights_enabled?: boolean

  // Override as stored; empty string = automatic default.
  critique_models: string
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

  critique_models_effective: string
  // Server settings that used to be workspace .env variables, as stored
  // ("" = default). The bind address and log level apply at the next start.
  pwa_host?: string
  log_level?: string
  dev_mode?: boolean
  app_repo?: string
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

export interface ProviderConnection {
  name: string
  // Display names from the backend registry, so the Settings card never has to
  // map provider ids to product names itself.
  label?: string
  short_label?: string
  ok: boolean
  auth: string
  command: string
  detail?: string
  version?: string
  account?: string
  protocol?: string
  mcps?: string[]
  skills?: string[]
  /** Skills that ship with the CLI itself; absent until the CLI has reported them. */
  bundled_skills?: string[]
  /** Docs page for installing the CLI, set when `auth === 'not_installed'`. */
  install_url?: string
  /** Shell line that puts the CLI on PATH (`not_installed`, `missing`, `cli_too_old`). */
  path_command?: string
  /** Desktop app found while the CLI is missing. */
  app_path?: string
  /** Absolute path of the CLI binary Ciaobot would run. */
  cli_path?: string
}

export interface ProviderConfigSettings {
  connections?: Record<string, ProviderConnection>
}

export interface GwsIntegrationProfile {
  name: string
  label: string
  purpose: string
  examples: string[]
  configured: boolean
  credentials_present: boolean
  client_secret_present: boolean
  config_dir: string
  workspaces: string[]
  setup_command: string
  headless_auth_command: string
  email: string
  // Cached token-health snapshot from the periodic monitor (issue #145).
  // `token_valid` is null when no health check has run yet for this profile.
  token_valid: boolean | null
  token_error: string
  needs_relogin: boolean
  // Whether the OAuth client accepts the random-port loopback redirect used by
  // the one-click sign-in flow (false for web-type clients, which need an
  // exact authorized redirect URI and therefore the manual paste flow).
  loopback_eligible: boolean
}

export interface GwsIntegrationSettings {
  installed: boolean
  binary_path: string
  default_profile: string
  cli_available: boolean
  profiles: GwsIntegrationProfile[]
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
  error_log_lines: number
  error_log_path: string
  failed_jobs: { job: string; label: string; ended_at: string; error: string }[]
  has_issues: boolean
  report_text: string
}

// ── CLI Stats ───────────────────────────────────────────────────────────

export interface DailyActivity {
  date: string
  messageCount: number
  sessionCount: number
  toolCallCount: number
}

export interface DailyModelTokens {
  date: string
  tokensByModel: Record<string, number>
}

export interface CliStats {
  version: number
  dailyActivity: DailyActivity[]
  dailyModelTokens: DailyModelTokens[]
  modelUsage: Record<string, {
    inputTokens: number
    outputTokens: number
    cacheReadInputTokens: number
    cacheCreationInputTokens: number
  }>
  totalSessions: number
  totalMessages: number
  firstSessionDate: string
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
  command?: string
  detail?: string
  /** Documentation page for installing the provider CLI (`auth: 'not_installed'`). */
  install_url?: string
  /** Shell line that puts the CLI on PATH (`not_installed`, `missing`, `cli_too_old`). */
  path_command?: string
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
  opened?: boolean
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

/**
 * One engine update run, as the coordinator persists it
 * (`ciao/engine_update.py:Operation`). `phase` is one of `engine_update.PHASES`
 * and `error` carries the reason when the run failed.
 */
export interface EngineUpdateOperation {
  id: string
  phase: string
  from_version: string
  to_version: string
  started_at: string
  updated_at: string
  error?: string
}

/** `GET /api/update/status` — the engine update coordinator's persisted job. */
export interface EngineUpdateStatus {
  install_mode: string
  can_update: boolean
  operation: EngineUpdateOperation | null
  /** A coordinator refusal that wrote no operation of its own. */
  error?: string
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
   * The task was edited after the current attempt was handed over.
   *
   * The result the agent is about to produce was reached against a description
   * the user has since changed. Nothing resolves this for the reviewer — the board
   * says it and lets them decide.
   */
  changed_since_delegated: boolean
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
  changed_since_delegated: boolean
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
 * What a **Send update** answers with.
 *
 * The attempt comes back rebound to the revision the update was made at, so
 * `task.changed_since_delegated` is `false` in this same answer — which is what
 * retires the control. `queued` says which of the two it was: the message was
 * accepted either way, but a queued one is still waiting behind a turn already
 * running in that chat.
 */
export interface TaskSendUpdateResponse {
  workspace: string
  attempt: TaskAttempt
  chat_id: string
  task: TaskDetail
  updated: boolean
  queued: boolean
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

export interface TaskListResponse {
  workspace: string
  tasks: TaskRow[]
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

// ── Update tasks: the "After this update" group ────────────────────────────

/** The lifecycles `ciao/update_tasks.py::LIFECYCLES` defines. The `| string`
 *  escape hatch is deliberate: a state file written by a newer engine can hold a
 *  lifecycle this build does not know, and rendering it as "unknown state" beats
 *  rendering it as a task nobody has started. */
export type UpdateTaskLifecycle =
  | 'offered'
  | 'in_progress'
  | 'waiting_review'
  | 'failed'
  | 'completed'
  | 'dismissed'
  | string

/** The three answers `update_tasks.APPLICABILITY_STATUSES` defines. `unknown`
 *  is the one that must never be drawn as "done": it means nobody could say. */
export type UpdateTaskApplicability =
  | 'applicable'
  | 'not_applicable'
  | 'unknown'
  | string

/** One row of `GET /api/update-tasks` — the Home group and the Settings
 *  history both render these, from `ciao/web/routes_api.py::_update_task_row`. */
export interface UpdateTaskRow {
  id: string
  revision: number
  /** `install` (one record for the whole engine) or `workspace` (the record
   *  lives in that workspace's own vault). The history groups by this. */
  scope: string
  title: string
  /** The one-line reason the task exists, shipped with the catalog. */
  why: string
  /** The engine version that introduced this task revision. */
  since_version: string
  status: UpdateTaskLifecycle
  applicability: UpdateTaskApplicability
  /** When this row's applicability was last *computed*. Distinct from
   *  `updated_at`, which is when the record was written: a dismissal is a
   *  decision, not a re-check, and a history that merged the two would claim
   *  somebody had looked again. Inside the server's freshness window this keeps
   *  the stamp of the call that computed the answer. */
  applicability_checked_at: string
  offered: boolean
  suppressed: boolean
  /** The chat an earlier start created, so "Resume" never needs the browser to
   *  have remembered it. Empty when no attempt exists; a nonempty id may still
   *  name an archived or deleted chat — see `chat_live`. */
  chat_id: string
  /** Whether that chat is still one the operator can open. `chat_id` names the
   *  chat a record remembers; an archived or deleted one is no longer open, and
   *  a card that said "its chat is open" against it would be describing
   *  something that is not there. A start over a dead chat mints a fresh one. */
  chat_live: boolean
  prompt_digest: string
  attempted_fingerprint: string
  /** When the *record* was last written — a decision or an attempt. */
  updated_at: string
}

/** Why `GET /api/update-tasks` is offering nothing. Present only then, because
 *  an absent key and an empty list are different statements and `[]` is the
 *  one a card would read as "you are done". */
export interface UpdateTaskCoverageGap {
  reason: string
  detail: string
}

export interface UpdateTasksResponse {
  tasks: UpdateTaskRow[]
  coverage_gap?: UpdateTaskCoverageGap
}

/** `POST /api/update-tasks/{id}/start|dismiss|reopen`. `tasks` is absent when
 *  the route's own decision landed but the follow-up detector pass could not be
 *  listed — a client must treat its absence as "unknown", never as "empty". */
export interface UpdateTaskActionResponse {
  ok: boolean
  task_id: string
  /** Start only: the chat the task is in. A 500 refusal still carries it, so a
   *  retry sends into that same chat instead of minting a second one. */
  chat_id?: string
  /** Start only: false means this call created the chat, true means nothing was
   *  created — the same press twice lands in the same chat. */
  resumed?: boolean
  error?: string
  tasks?: UpdateTaskRow[]
}

// ── Proposal review (agent roots) ────────────────────────────────────────

/** Live rehome signal for a `[rehome]` row, from `ciao/vault_rehome`. */
export interface RehomeSignal {
  /** The note this row is about (`personal/People/Mo.md`), for a clean label. */
  note: string
  /** The destination named in the bullet. A guess unless `justified`. */
  destination: string
  /** Every workspace the note's tags name. Empty when no tag backs it. */
  candidates: string[]
  /** True only when a single clean tag signal backs the destination. */
  justified: boolean
  /**
   * The bullet outlived its cause: the note was tagged, moved, or a later rule
   * settled it, and nothing re-detects it now. Distinct from "undecided", which
   * is a row still asking the operator something.
   */
  stale?: boolean
  reason: string
}

/**
 * One queued proposal row from `GET /api/proposals`. Region kinds
 * (`memory`/`profile`/`user`) carry `region` and `leak_warning`; `rehome`
 * rows carry `rehome`; destination kinds carry `target` (the person name for
 * `people`, the doc path for `project`); `skill` rows are files and carry
 * none of these.
 */
export interface ProposalRow {
  id: string
  kind: string
  text: string
  source: string
  workspace: string
  path: string
  line: number
  region?: string
  leak_warning?: boolean
  rehome?: RehomeSignal
  target?: string
  /** A `note_edit` row: a note verification the autonomy rule would not apply,
   * queued for a person to decide. `target` is the note it is about (the queue
   * bullet's own payload is a sidecar id), and this is what the accept needs
   * beyond it — the operation, and whether the accept can do what a button
   * saying so would claim. */
  note_edit?: {
    id: string
    /** `replace` | `restamp` | `retire`. */
    operation: string
    /** The verification outcome this was filed from. */
    outcome: string
    /** When it was decided, or empty while it is still queued. */
    settled: string
    /** The note receipt the accept's write handed back. */
    receipt_id: string
    can_accept: boolean
    reason: string
  }
  /** A `skill` row: the versioned record the queue owns, not a bullet.
   *
   * `chat_id` is the server's own record of which chat is implementing it and
   * the source of truth for "Open chat" — it used to live in the browser's
   * localStorage, which a reload and a second device could not see, so the same
   * proposal was implemented twice and nothing recorded that either had run.
   */
  skill?: string
  title?: string
  problem?: string
  change?: string
  rationale?: string
  canonical_path?: string
  reviewed_revision?: string
  chat_id?: string
  lifecycle?: SkillProposalLifecycle
  sources?: SkillEvidenceRow[]
  /** The learnings this finding was derived from, one entry per finding, and
   * the state of each. EMPTY on a proposal filed before origins existed, which
   * is not the same as settled: an empty list means the record links nothing,
   * so no learning behind it can be retired by settling the row. */
  origins?: SkillOriginRow[]
}

/** Where a skill proposal is in the server-owned accept lifecycle.
 *
 * `pending`/`implementing`/`interrupted` are OPEN: the work is unfinished, so
 * the row stays queued and stays re-openable. The rest are decisions.
 */
export type SkillProposalLifecycle =
  | 'pending'
  | 'implementing'
  | 'interrupted'
  | 'applied'
  | 'dismissed'
  | 'not_applicable'

/** One session behind a skill proposal, as the record holds it. */
export interface SkillEvidenceRow {
  chat_id: string
  archive: string
  turn: string
  excerpt: string
}

/** What became of one finding on one learning.
 *
 * `applied` is the only state that says the lesson is in the target AND
 * `verification` names the receipt or readback that proves it. `dismissed` is a
 * person rejecting that finding. `already_covered`, `not_applicable` and
 * `unclear` are answers that still need somebody to look at the skill, and
 * `failed` is the absence of one — none of them retire a learning.
 */
export type SkillOriginState =
  | 'pending'
  | 'implementing'
  | 'interrupted'
  | 'applied'
  | 'dismissed'
  | 'not_applicable'
  | 'already_covered'
  | 'unclear'
  | 'failed'

/** One finding's link back to the learning it came from. */
export interface SkillOriginRow {
  workspace: string
  /** EMPTY for an origin the record could not read: unattributable, and a
   * reason to leave every learning on that record alone. */
  learning_id: string
  /** The `content_revision` of `Workspace/Learnings.md` at filing, so a later
   * read can tell whether the learning moved under the finding. */
  source_revision: string
  finding: string
  summary: string
  state: SkillOriginState
  verification: string
}

/** `POST /api/proposals/{id}/implement` — accept a skill proposal into a chat.
 *
 * `created: false` is the idempotent answer: the same chat is already live, so
 * a double tap, a retry after a dropped response and a second device all get
 * the one implementation rather than one each. */
export interface ProposalSkillAcceptResponse {
  ok: boolean
  chat_id: string
  project_id: string
  created: boolean
  error?: string
}

export interface ProposalsResponse {
  rows: ProposalRow[]
}

/** One per-row outcome from `POST /api/proposals/batch` or `/{id}/{action}`. */
export interface ProposalActionResult {
  id: string
  action: string
  dismissed: boolean
  region?: string
  leak_warning?: boolean
  destination?: string
  /** notes a category accept skipped because they no longer exist */
  skipped?: string[]
  justified?: boolean
  promoted?: boolean
  duplicate?: boolean
  written?: string
  error?: string
  /** The destination moved since this row's preview; nothing was written and
   * the row is still queued. Distinct from a plain failure: reopening the
   * preview is the fix, not giving up on the row. */
  conflict?: boolean
}

/** One destination a batch touched, from `POST /api/proposals/batch`.
 *
 * A fifty-row accept reports fifty results, which is the same list the queue
 * already showed. This is the per-destination roll-up the review panel renders
 * instead; `failed_ids` keeps every per-row failure addressable. */
export interface ProposalBatchSummary {
  destination: string
  action: string
  total: number
  ok: number
  failed: number
  conflicts: number
  duplicates: number
  failed_ids: string[]
  errors: string[]
}

/** The 409 body a refused single-row accept returns.
 *
 * `deferred` marks the one refusal a fresh `?reconcile=1` retry can resolve on
 * its own: the fact may supersede something the region already holds and the
 * reconcile could not say what, so nothing was written and the row is still
 * queued. Every other refusal (over-cap region, event-shaped text) needs a
 * person to change something first, which is why the retry is offered on this
 * one alone. `reason` is why it could not decide and `competing` the region
 * entries it was weighed against, capped server-side at five.
 */
export interface ProposalAcceptRefusal {
  error?: string
  id?: string
  region?: string
  deferred?: boolean
  reason?: string
  competing?: string[]
}

export interface ProposalBatchResponse {
  ok: boolean
  action: 'accept' | 'dismiss'
  results: ProposalActionResult[]
  summary?: ProposalBatchSummary[]
}

/**
 * What accepting one proposal would write, from `GET /api/proposals/{id}/preview`.
 *
 * `before`/`after` are the destination body itself, computed by the same
 * functions the accept calls — so a stamped learned-at date, a duplicate that
 * writes nothing, and a learning whose recurrence count is bumped rather than
 * appended all show as what they are. `exact: false` marks a kind whose result
 * cannot be known without writing (a `[project]` fold is decided by a model at
 * accept time), and the card says so rather than showing a guess.
 *
 * `revision` is the destination digest this preview was computed against. It
 * goes back with the accept, which refuses (409) if the destination moved.
 */
export interface ProposalPreview {
  id: string
  workspace: string
  kind: string
  text: string
  source: string
  action?: string
  /** What the accept does to one destination. `add_category` is its own value
   * because it writes no file body: it appends a category to the registry and
   * retypes the notes the proposal was filed with, moving nothing.
   * `note_edit` rewrites a whole vault note from the verification's exact
   * replacement, and `retire_note` is the one operation that removes it — it
   * moves the note to the reversible review trash and writes no body at all. */
  operation: 'add' | 'update' | 'move' | 'add_category' | 'note_edit' | 'retire_note' | 'none' | ''
  /**
   * `note` or `entry` — the unit the accept writes.
   *
   * Separate from `operation` because all three entry operations report
   * `note_edit`: `replace_entry`, `restamp_entry` and `retire_entry` share the
   * note's operation word and differ in scope, and a card that read the
   * operation alone would promise a whole-file rewrite for an accept that
   * changes one line.
   *
   * Optional because a server older than this client does not send it, and
   * absent means `note` — which is exactly what such a server means, since it
   * has no entry operations to describe.
   */
  scope?: 'note' | 'entry'
  /** The entry operation, when `scope` is `entry`. */
  entry_operation?: string
  /** The entry's own text before the accept; the whole-note `before` is the
   * other half and is unchanged apart from this one span. */
  entry_before?: string
  /** The entry's own new text. Empty for an entry retirement, and for a record
   * whose splice could not be inverted. */
  entry_after?: string
  /** An entry retirement: this one line is removed and the note is kept. */
  entry_removed?: boolean
  destination: string
  destination_path: string
  revision: string
  before: string
  after: string
  exact: boolean
  truncated: boolean
  can_accept: boolean
  reason: string
  /** What joins the destination's units: `\n§\n` for a bounded region (whose
   * unit is an entry, not a line), `\n` for an ordinary file. */
  separator: string
  written?: string
  added?: string[]
  leak_warning?: boolean
  /** Only on a `[category]` preview: the entry the accept would add and the
   * notes it would retype, so the card can name both without a second call. */
  category?: {
    id: string
    label: string
    folder: string
    description: string
    notes: string[]
    missing?: string[]
  }
}

export interface ProposalPreviewResponse {
  ok: boolean
  preview: ProposalPreview
}

export interface ProposalDismissOlderResponse {
  ok: boolean
  removed: number
}

// ── Proposal history (decision ledger) ───────────────────────────────────

/** Who made the decision: through the app, the curation agent, or on its own. */
export type ProposalHistoryVia = 'pwa' | 'agent' | 'auto' | ''

/**
 * One decided proposal from `GET /api/proposals/history`. Read side of the
 * per-workspace decision sidecar `record_dismissal`/`record_promotion` write
 * in `ciao/memory_proposals.py`. `outcome` qualifies `action`: an "accepted"
 * row with `outcome: "duplicate"` or `"suppressed"` was recognized as already
 * known rather than freshly written, and `"swept"` marks an expiry dismissal.
 * Legacy sidecar rows predate `ts`/`via` and surface with both blank.
 */
export interface ProposalHistoryRow {
  id: string
  ts: string
  action: 'accepted' | 'dismissed'
  via: ProposalHistoryVia
  kind: string
  text: string
  source: string
  workspace: string
  destination: string
  outcome: string
  proposal_id: string
  /** The learning a derived finding was filed against, when this decision was
   * about one finding rather than a whole row. EMPTY for every decision about a
   * row, and for every row written before the field existed. */
  learning_id?: string
  /** Which finding within that learning, when the decision named one. */
  finding?: string
  /** The archive transcript this fact came from, when one is still on disk. */
  source_path?: string
  /** The receipt that performed this decision. ABSENT — not falsy — for every
   * decision the receipt protocol never recorded: History renders those as
   * "No change snapshot available" rather than offering an undo it cannot
   * honour. */
  change?: ProposalHistoryChange
  /**
   * The `note_edit` record behind this decision, when the server could still
   * read it. ABSENT for a `note_edit` whose sidecar is gone or unreadable —
   * same contract as `change`, and for the same reason: a record nobody can
   * read must not be rendered as one nobody can check.
   *
   * This is what makes a verified note edit in History judgeable rather than a
   * bare sentence: the exact before/after, the evidence the verdict rested on,
   * how much of the note the check covered, and whether the decision is still
   * open.
   */
  note_edit?: ProposalHistoryNoteEdit
  /**
   * `'restore'` for a decision that moved a note to the review trash. That
   * change has no memory receipt, so there is no Undo — but it is not
   * unrecoverable either, and saying "no change snapshot available" beside a
   * retirement that really happened reads as a false claim. Vault Review's
   * restore is the way back.
   */
  reversible_by?: 'restore'
}

/** One filed `note_edit` as the history ledger reports it. */
export interface ProposalHistoryNoteEdit {
  id: string
  /** The vault-relative note this edit is about. */
  relative_path: string
  /** `replace` | `restamp` | `retire`, or `replace_entry` | `restamp_entry` |
   * `retire_entry` for the three that change one list item and nothing else. */
  operation: string
  outcome: string
  /** `complete` | `partial`. */
  coverage: string
  /** The note's full text before the edit. */
  before: string
  /** The note's full text after it; empty for a retirement. */
  after: string
  reason: string
  evidence: { source_type: string; source_ref: string; quoted: string; supports: string }[]
  /** When the owner decided; empty while the proposal is still open. */
  settled: string
  accepted: boolean
  /** The note receipt the accept's write handed back. */
  receipt_id: string
  /** The decision has not been made yet. */
  pending: boolean
  /**
   * `note` or `entry` — the unit the accept actually writes.
   *
   * The whole point of the three `*_entry` operations: a `retire_entry` removes
   * one bullet and leaves every other fact in the file, so a history row that
   * said "this note was rewritten" would overstate what happened, and one that
   * showed only the whole-note before/after would differ by a single line with
   * no way to see which.
   *
   * Optional, absent meaning `note`: a server older than this client has no
   * entry operations to have recorded.
   */
  scope?: 'note' | 'entry'
  /** The entry's identity, for an `entry`-scope decision. */
  entry_identity?: string
  /** The entry's fingerprint as the verdict was reached about it. */
  entry_fingerprint?: string
  /** The `[start, end]` character span the entry occupied in `before`. */
  entry_span?: [number, number]
  /** The entry's own text, exactly as the note held it. */
  entry_before?: string
  /** The entry's own new text; empty for a retirement, and for a record whose
   * splice could not be inverted — `entry_removed` tells the two apart. */
  entry_after?: string
  /** An `entry`-scope retirement: this one line was removed and nothing else. */
  entry_removed?: boolean
  /** Set when the recorded splice could not be inverted, so the entry diff is
   * withheld rather than guessed at. */
  entry_recovery_error?: string
}

/** The receipt behind one history row, from `GET /api/proposals/history`. */
export interface ProposalHistoryChange {
  receipt_id: string
  kind: string
  status: string
  destination: string
  undoable: boolean
  changed: boolean
  ts: string
}

/** One line of a receipt's before/after. Context lines are not sent: what a
 * History row has to answer is what changed, and a region body reprinted in
 * full buries the one line that did. */
export interface MemoryReceiptDiffLine {
  op: 'added' | 'removed'
  text: string
}

/** One receipt with its images, from `GET /api/memory/receipts/{id}`.
 *
 * `has_snapshot: false` is the legacy/unsupported case and carries a `reason`;
 * the card shows that instead of an empty diff. */
export interface MemoryReceiptDetail {
  id: string
  workspace: string
  kind: string
  status: string
  ts: string
  actor: string
  source: string
  destination: string
  fact_text: string
  undoable: boolean
  has_snapshot: boolean
  changed: boolean
  error: string
  reason?: string
  before?: string
  after?: string
  diff?: MemoryReceiptDiffLine[]
  truncated?: boolean
  diff_truncated?: boolean
}

export interface ProposalHistoryResponse {
  rows: ProposalHistoryRow[]
  total: number
  truncated: boolean
  /** Page size the server actually served, after clamping to its cap. */
  limit?: number
  /** The request asked for more than the cap, so a wider limit returns the
   * same page. Paging must stop on this, not on `truncated`. */
  at_max?: boolean
}

// ── Vault review (stale-note retirement) ─────────────────────────────────
// Backed by `GET|POST /api/vault/review` (`ciao/vault_review.py`), not by the
// proposal queue: candidates are hash-identified vault notes with explainable
// detection signals, and dispositions land in the append-only
// `Workspace/Vault-Review.jsonl` ledger rather than the proposal sidecars.

/** Explainable detection evidence behind one retirement candidate. */
export interface VaultReviewEvidence {
  backlinks: string[]
  outbound_links: string[]
  bridge: boolean
  duplicate_group: string[]
  last_update: string
  type: string
  age_days: number | null
  /** The note's opening prose, already stripped of frontmatter and its H1.
   *
   * Optional because a server older than this client does not send it; the
   * panel falls back to its lazy per-row fetch when it is absent. */
  excerpt?: string
  /** Why an `unverified` candidate is due: how old its last check is, the
   * limit for its type, and where that date came from. Optional (and null
   * when the signal is absent) so an older server simply leaves it out. */
  unverified?: VaultReviewUnverified | null
  /** What the managed verification pass already concluded about this note's
   * CURRENT revision, from `Workspace/Note-Checks.json`. `null` when nobody has
   * checked the note, which is the ordinary case.
   *
   * `checked_at` is when the check RAN, which is not the note's own `updated:`:
   * a verdict that came back `unverified` writes nothing, so the two dates
   * diverge and the panel shows both rather than collapsing them. */
  verification?: VaultReviewCheck | null
  /** Where a `superseded_language` candidate says so: the 1-based line, the
   * line itself, the phrase that matched, and the nearest line either side. */
  superseded?: VaultReviewSuperseded | null
  /** What the entry-level detector found inside this note's own list items, and
   * the pending entry proposals that will change them.
   *
   * Absent (or null) when the note has no usable date to age entries from, or
   * when the server is older than this client. `unverified` is the file-level
   * question; this is the same question one bullet in, and it is the one a
   * re-stamped note cannot answer. A note can sit here with `stale: 0` because
   * its own `updated:` is current and still show three facts nobody has checked
   * — which is the whole reason it exists. */
  entry_verification?: VaultReviewEntryCoverage | null
}

/** One note's per-entry freshness, as the review queue reports it. */
export interface VaultReviewEntryCoverage {
  /** List items the parse found in the note. */
  entries: number
  /** How many were judged; the rest are exempt event records. */
  checked: number
  /** Deliberately not judged: event-shaped entries and event sections. */
  exempt: number
  /** Judged entries nobody ever stamped — no `[verified:]`, or an unusable one. */
  unverified: number
  /** Runs of note text that are not entries at all: a paragraph, a table, a
   * quote. Never verified, and never counted as clean. */
  uncovered: number
  /** Judged entries whose own date is past the horizon. */
  stale: number
  /** Share of the note read as entries, 0–1. */
  coverage_ratio: number
  /** Every in-scope assertion is covered AND current. */
  fully_verified: boolean
  /** The first few overdue entries, named in full so a row can show them. */
  stale_entries: VaultReviewEntryFinding[]
  /** How many further overdue entries the list left out. */
  more_stale_entries: number
  /** Pending `note_edit` proposals, one per entry that came back `needs_review`.
   *
   * These are the row's actionable links, and they are deliberately *links*:
   * each accept rewrites or removes exactly one bullet, so the row points at
   * the decision rather than offering a second whole-note button for a finding
   * that is about a single line. */
  proposals: VaultReviewEntryProposal[]
  /** How many further pending entry proposals the list left out. */
  more_proposals: number
}

/** One overdue entry, named in a review row. */
export interface VaultReviewEntryFinding {
  identity: string
  /** 0-based line index the entry opens on, for "line 42" in the disclosure. */
  line_number: number
  /** The nearest preceding heading's text — which list this bullet is in. */
  section: string
  /** The entry's own text, exactly as the note holds it. */
  excerpt: string
  /** The physical lines either side of it, so a row can show the context. */
  context: string[]
  /** `aged` | `no-stamp` | `unusable-stamp`. */
  reason: string
  /** The same thing in a sentence, with the numbers beside it. */
  detail: string
  age_days: number | null
  /** `YYYY-MM-DD`, or '' when there is no date at all. */
  last_verified: string
  /** True when this is the entry's own `[verified:]` day; false when the entry
   * has no usable stamp and inherited the note's date instead. */
  own_date: boolean
  supported: boolean
}

/** A pending decision about one entry, linked rather than duplicated. */
export interface VaultReviewEntryProposal {
  identity: string
  /** The queue row the decision is filed under — the thing to link to. */
  proposal_id: string
  /** `replace_entry` | `restamp_entry` | `retire_entry`, or '' when the
   * filed record could not be read. */
  operation: string
  outcome: string
  checked_at: string
  retry_after: string
  coverage: string
  reason: string
  citations: number
  receipt_id: string
  /** The accept will be refused: the note no longer holds the entry this was
   * filed against, so the entry is due again and the row must say so rather
   * than offering a button that can only fail. */
  conflicted: boolean
}

/** One note's last verification, as the review queue reports it. */
export interface VaultReviewCheck {
  /** `still_valid` | `update` | `retire` | `unverified`. */
  outcome: string
  /** `YYYY-MM-DD`: when the check ran. */
  checked_at: string
  /** `YYYY-MM-DD`: the end of the cooldown, before the note is asked again. */
  retry_after: string
  /** `complete` | `partial`: how much of the note the check actually covered. */
  coverage: string
  /** Why the verdict came out the way it did, in the pass's own words. */
  reason: string
  /** How many citations the verdict rested on. */
  citations: number
  /** The note receipt an applied verdict wrote, or '' for one that wrote none. */
  receipt_id: string
  /** The revision the check describes. */
  revision: string
  /** A `note_edit` proposal is waiting on a person for this exact revision. */
  pending: boolean
  /** A proposal is pinned to a revision the note is no longer in, so it can no
   * longer be applied. The note is due to be checked again. */
  conflicted: boolean
  /** The queue row id of the pending proposal, when `pending`. */
  proposal_id: string
}

/** The verification proposal a row links to instead of duplicating its decision. */
export interface VaultReviewPendingProposal {
  /** The queue row the review card is keyed by — the thing a client links to. */
  proposal_id: string
  /** The sidecar the accept resolves, for a direct read of the filed record. */
  note_edit_id: string
  outcome: string
  checked_at: string
  retry_after: string
  coverage: string
  reason: string
  citations: number
}

export interface VaultReviewUnverified {
  age_days: number
  threshold_days: number
  /** `YYYY-MM-DD`. */
  last_verified: string
  /** `frontmatter` when read off the note's `updated:` field, `mtime` when the
   * note has none and the file's modified date stood in. */
  source: 'frontmatter' | 'mtime'
}

export interface VaultReviewQuotedLine {
  line: number
  text: string
}

export interface VaultReviewSuperseded extends VaultReviewQuotedLine {
  match: string
  where: 'frontmatter' | 'lead'
  before: VaultReviewQuotedLine | null
  after: VaultReviewQuotedLine | null
}

/** One stale-note retirement candidate from `GET /api/vault/review`. */
export interface VaultReviewCandidate {
  candidate_id: string
  workspace: string
  path: string
  content_hash: string
  signals: string[]
  priority: number
  evidence: VaultReviewEvidence
  status: string
  disposition: string
  deferred_until: string
  /**
   * Whether the engine will accept `complete` for this row — it is a project
   * that still has somewhere to complete into. The backend decides it from the
   * same helpers the action gates on, so the panel offers Complete in place of
   * Retire exactly when the click will be honoured. Re-deriving it here from
   * `evidence.type` would put a second definition of project-ness in the
   * client, and a button the engine refuses is worse than no button.
   */
  completable: boolean
  /**
   * Whether that completion moves a whole project FOLDER rather than the one
   * note. An untyped note nested under `projects/active/<x>/` is completable on
   * its own — completing it closes the project, so the plan, the meeting notes
   * and the attachments beside it all move. A button that said only "Complete"
   * made that read as one file being moved, which is why the panel's confirm
   * asks about the folder when this is set. Read off the payload for the same
   * reason as `completable`: the layout decision is the engine's.
   */
  completion_moves_folder: boolean
  /**
   * The verification proposal this row links to, when one is waiting on a
   * person for the note's CURRENT revision, and `null` otherwise.
   *
   * The queue used to offer a second, independent *Still true* / *Retire* on
   * the same revision the pass had already filed a proposal about — the same
   * question asked twice, in two places, with the two answers able to disagree.
   * A row carrying this points at the proposal instead, and takes its own
   * retirement action away when nothing else justifies it.
   */
  pending_verification?: VaultReviewPendingProposal | null
  /**
   * Whether the row still offers its terminal action (Retire, or Complete on a
   * project). False only when a verification proposal is the note's *sole*
   * reason for being here: some other signal — unlinked, duplicate, superseded
   * wording — is an independent finding about the note, and the queue must not
   * lose it because the pass happened to reach the same note first.
   */
  retirement_offered?: boolean
}

/** One restorable note in `.vault-trash`, from `GET /api/vault/review?include=trashed`. */
export interface VaultTrashedNote {
  candidate_id: string
  workspace: string
  original_path: string
  content_hash: string
  trashed_at: string
}

export interface VaultReviewDecisionResult {
  candidate_id: string
  /** The id the caller asked about; `candidate_id` is recomputed post-stamp. */
  previous_candidate_id?: string
  /** True only when an `updated:` date was actually written to the note. */
  stamped?: boolean
  /**
   * Why a keep did or did not stamp the note. A union rather than `string`:
   * the sole consumer compares string literals, so a typo or a backend rename
   * would compile clean and silently disable the notice this exists to raise.
   */
  stamp_status?:
    | 'stamped'
    | 'already_current'
    | 'no_frontmatter'
    | 'not_utf8'
    | 'unreadable'
    | 'not_applicable'
}

/** One note cleared with `keep` and still in the vault, from `include=cleared`. */
export interface VaultClearedNote {
  candidate_id: string
  workspace: string
  path: string
  content_hash: string
  decided_at: string
}

export interface VaultReviewResponse {
  candidates: VaultReviewCandidate[]
  trashed?: VaultTrashedNote[]
  cleared?: VaultClearedNote[]
  result?: VaultReviewDecisionResult
}
