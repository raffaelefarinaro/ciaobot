export type WorkspaceName = string

/**
 * A runtime provider id, enumerated by the backend registry
 * (`ciao/provider_registry.py`). The `(string & {})` arm keeps editor
 * autocomplete for the providers that ship today while still accepting any id
 * the backend reports, so adding a provider does not mean editing this union.
 */
export type RuntimeProvider = 'claude' | 'codex' | (string & {})

/**
 * What a runtime provider supports, mirroring `ProviderCapabilities` in
 * `ciao/providers/base.py`. The PWA gates affordances on these rather than on
 * provider ids — e.g. no "steer" control renders for a provider that reports
 * `steer: false`.
 */
export interface ProviderCapabilities {
  resume: boolean
  fork: boolean
  images: boolean
  stop: boolean
  steer: boolean
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
  default_model: string
  disallowed_tools?: string[] | null
  gws_profile: string
  // PWA accent preset: pink | cyan | amber | emerald | violet. Missing → pink.
  color?: string
}

export interface WorkspacesResponse {
  workspaces: WorkspaceInfo[]
  active: WorkspaceName | null
  // App-wide fallback model used when a workspace's default_model is empty.
  app_default_model?: string
  provider_options?: WorkspaceProviderOption[]
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
  project_servers?: McpProjectServer[]
  active_sessions?: number
  providers?: string[]
  last_error?: string
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
  // Ciaobot control surface. Engine-controlled now (MCP by default, with a
  // legacy fallback); no longer user-set from the PWA. Kept on ChatInfo
  // because the engine still returns/persists it, preserving round-trip typing.
  control_surface?: '' | 'legacy' | 'mcp'
  session_id: string
  created_at: string
  archived: boolean
  last_activity_at?: string
  last_read_at?: string
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
  // Set when this chat was spawned as a delegate by another chat's agent. The
  // sidebar nests delegates under their supervisor; the engine wakes the
  // supervisor with a fresh turn when a delegate finishes.
  spawned_from_chat_id?: string
  // Shared tag across delegates dispatched as one batch.
  delegation_id?: string
  // What the post-archive pipeline is doing, or did. Present only on archived
  // chats that ran it. Drives the greyed activity signal and the settled
  // "here is what was learned from this chat" line.
  postprocess?: ChatPostprocess | null
}

/** One step of the post-archive pipeline, as reported by ciao/job_runs.py. */
export interface ChatPostprocessStep {
  status: 'ok' | 'error' | 'skipped'
  extra?: Record<string, unknown>
}

export interface ChatPostprocess {
  /** 'running' while the pipeline task is alive; 'done' once it settles. */
  state: 'running' | 'done'
  /** Job id of the step that is running, or the last one that ran. */
  step?: string
  /** Steps that can run for this chat, in execution order. */
  expected?: string[]
  /** Outcome per step, keyed by job id. Only finished steps appear. */
  steps?: Record<string, ChatPostprocessStep>
  started_at?: string
  updated_at?: string
  /** Set when a server restart killed the pipeline mid-flight. */
  interrupted?: boolean
}

// One sidebar chat row. Delegates follow their supervisor and render indented,
// so the list stays a single flat v-for instead of a nested one.
export interface ChatRow {
  chat: ChatInfo
  isDelegate: boolean
}

// One sidebar stack. The supervisor remains the visible anchor and its
// delegate chats can be expanded beneath it as a single group.
export interface ChatGroup {
  chat: ChatInfo
  delegates: ChatInfo[]
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
  // Codex-native assistant-message phase. Commentary stays in the reasoning
  // trace; only final_answer is eligible for the terminal response bubble.
  // Undefined keeps the legacy last-assistant-message inference.
  phase?: 'commentary' | 'final_answer'
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
  status?: 'running' | 'completed' | 'failed' | ''
  turn_index?: number
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
  | { type: 'result'; text: string; is_error: boolean; effective_model: string; usage: Record<string, string>; quota?: Record<string, unknown>; session_id: string; fallback_final?: boolean; sent_at?: string; completed_at?: string; duration_ms?: number }
  | { type: 'permission_request'; tool_name: string; tool_input?: string; message: string; request_id: string }
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
  | { type: 'queued'; id?: string; text: string; images?: string[] }
  | { type: 'queue_state'; queue: Array<{ id: string; text: string; images?: string[] }> }
  | { type: 'steered'; text: string; images?: string[] }
  | { type: 'error'; message: string }
  // The local client proxy could not open the remote host socket. This is a
  // connection state, not a chat/model failure, so the PWA renders one
  // reconnecting card instead of appending an error to conversation history.
  | { type: 'host_unreachable' }
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
  | { type: 'snapshot'; active_streams: { chat_id: string; project_id: string }[]; background_agents?: Record<string, number>; postprocessing?: string[]; restarting?: boolean }
  | { type: 'chat_created'; chat: ChatInfo }
  | { type: 'chat_streaming_started'; chat_id: string; project_id: string }
  | { type: 'chat_streaming_done'; chat_id: string; project_id: string; is_error: boolean }
  | { type: 'chat_result_ready'; chat_id: string; project_id: string; title: string; snippet: string }
  | { type: 'chat_subagents_ready'; chat_id: string; project_id: string; remaining: number; nudged?: boolean }
  | { type: 'chat_read'; chat_id: string; last_read_at: string }
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
  // A loop was created, edited, started, stopped, or deleted. Carries no
  // payload: the client refetches /api/loops, which is the only place the
  // computed running/next_run fields are assembled.
  | { type: 'loops_changed' }
  | { type: 'open_chat'; chat_id: string }
  | { type: 'server_restarting'; message?: string }
  | { type: 'gws_health'; profile: string; token_valid: boolean; token_error: string; title: string; body: string }

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
}

// A pending approval surfaced to the user by Auto mode's classifier. One
// of these sticks to the chat bubble until the user clicks Approve or Deny,
// at which point the client sends a `permission_response` on the chat WS.
export interface PendingPermission {
  request_id: string
  tool_name: string
  tool_input: string
  message: string
  // Epoch ms when the request arrived — used by the UI to grey out very old
  // pending prompts that were likely cancelled server-side on a stream end.
  received_at: number
}

// ── Voice ───────────────────────────────────────────────────────────────

export interface VoiceResult {
  text: string
  cost: number
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
  frequency: 'daily' | 'weekly' | 'monthly' | 'manual' | 'once'
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

// In-chat loop: re-dispatches its prompt into one fixed chat every N minutes.
export interface Loop {
  loop_id: string
  prompt: string
  web_chat_id: string
  created_at: string
  interval_minutes: number
  title: string
  autostart: boolean
  last_run_at: string
  last_status: '' | 'running' | 'ok' | 'error' | 'busy' | 'missing-chat'
  scope?: 'user' | 'system'
  // Computed server-side
  running: boolean
  context_label: string
  next_run: string | null
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
  // Keyed by provider id: claude, codex, opencode.
  provider_models: Record<string, string[]>
  provider_defaults: Record<string, string>
  // Account-visible Codex models and their app-server metadata.
  codex_models?: string[]
  // Models reachable through opencode's connected backends, already
  // namespaced as `providerID/modelID`. Empty when nothing is authenticated.
  opencode_models?: string[]
  // Registry-driven provider descriptors, so the PWA never has to hard-code
  // the set of runtime providers. See `ciao/provider_registry.py`.
  providers?: ProviderDescriptor[]
  codex_model_metadata?: Record<string, {
    display_name: string
    description: string
    default_reasoning_effort: string
    input_modalities: string[]
  }>
  model_reasoning_levels?: Record<string, string[]>
  backends?: Record<string, boolean>
  // Keyed by runtime provider; Claude buckets share the SDK effort levels,
  // while Codex is additionally narrowed by model_reasoning_levels.
  thinking_levels?: Record<string, string[]>
}

// GET/PATCH /api/settings/routines — internal-routine model overrides and
// voice transcription engine (Settings → Models tab).
export interface RoutineSettings {
  // Overrides as stored; empty string = automatic default.
  insights_model: string

  critique_models: string
  // Per-provider default model for new chats; a missing entry = the provider's
  // own catalog default.
  provider_default_models?: Record<string, string>
  // Per-provider default thinking level for new chats; missing = provider default.
  provider_default_thinking?: Record<string, string>
  // Per-provider session-insights models; missing = provider default.
  provider_insights_models?: Record<string, string>
  // Per-provider default execution mode for new chats (Settings → Providers).
  // Missing entry = the app-wide default mode.
  provider_default_modes?: Record<string, string>
  // Resolved effective default mode per provider, after built-in defaults.
  provider_default_modes_effective?: Record<string, string>
  // What actually runs right now, after defaults.
  insights_model_effective: string
  // On Automatic this resolves from the chat's workspace, so *_effective above
  // is only the primary workspace's answer. Empty when an override is set.
  insights_model_by_workspace?: Record<string, string>

  critique_models_effective: string
  // The "apple" insights option is hardware-gated: needs macOS 26+, the
  // desktop app, and Apple Intelligence on. Nothing installable, so Settings
  // shows the reason when the machine lacks it.
  apple_model_available?: boolean
  apple_model_unavailable_reason?: string
  // Voice is on-device only: Apple dictation (macOS 26+) and
  // AVSpeechSynthesizer, both via the bundled sidecar. There is no engine to
  // choose any more, so the payload reports availability and a reason rather
  // than a selection.
  transcription: {
    // BCP-47 language for the on-device engines.
    locale: string
    available: boolean
    unavailable_reason: string
  }
  speech: {
    // Empty = let macOS pick the best installed voice for the language.
    local_voice: string
    available: boolean
    local_voices?: { id: string; name: string; locale: string; quality: string }[]
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
  /** Docs page for installing the CLI, set when `auth === 'not_installed'`. */
  install_url?: string
  /** Desktop app found while the CLI is missing. */
  app_path?: string
  /** Absolute path of the CLI binary Ciaobot would run. */
  cli_path?: string
}

export interface ProviderConfigSettings {
  keys: Record<string, {
    label: string
    description: string
    configured: boolean
    auth_method?: string
  }>
  service_keys?: Record<string, {
    label: string
    description: string
    configured: boolean
    auth_method?: string
  }>
  connections?: Record<string, ProviderConnection>
  auto_update_github_skills?: boolean
  requires_restart: boolean
  env_path: string
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
  wrapper_available: boolean
  helper_available: boolean
  email: string
  // Cached token-health snapshot from the periodic monitor (issue #145).
  // `token_valid` is null when no health check has run yet for this profile.
  token_valid: boolean | null
  token_error: string
  needs_relogin: boolean
}

export interface GwsIntegrationSettings {
  installed: boolean
  binary_path: string
  default_profile: string
  wrapper_path: string
  headless_helper_path: string
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
  label: 'custom' | 'github'
  source: string
  source_type: string
  description: string
  content?: string
  installed_targets: string[]
}

export interface SkillInventory {
  counts: {
    custom: number
    github: number
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

export interface PromptAsset {
  id: string
  title: string
  description: string
  source: string
  path: string
  editable: boolean
  content: string
  scope?: string
  parent_id?: string
  level?: number
  status?: 'ok' | 'missing' | 'blocked' | string
  imports?: string[]
  provider?: 'claude' | 'codex' | 'shared' | string
  workspace?: string
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

export interface AgentAssetsResponse {
  context: PromptAsset[]
  subagents: SubagentAsset[]
  commands: CommandAsset[]
  health?: WorkspaceHealthResponse
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

// ── Automation status (Settings → Automation) ──────────────────────────────

export interface JobRun {
  job: string
  label: string
  category: 'content' | 'system'
  started_at: string
  ended_at: string
  duration_ms: number
  status: 'ok' | 'error' | 'skipped'
  model: string
  provider: string
  error: string | null
  extra: Record<string, unknown>
}

export interface AutomationStats {
  total_runs: number
  success_rate: number | null
  avg_duration_ms: number
  last_error: { error: string; ts: string } | null
}

export interface AutomationProcess {
  job: string
  label: string
  category: 'content' | 'system'
  description: string
  // Optional for compatibility with servers upgraded before capability
  // metadata was added to GET /api/automation.
  uses_model?: boolean
  produces_outcome?: boolean
  // Plain-language "when does this run?", the system schedule that fires it,
  // and whether it is a one-shot migration. Optional: older servers omit them.
  trigger?: string
  schedule_id?: string
  one_time?: boolean
  // Bulk/manual variants of this job (Session insights carries the backfill),
  // reported nested so the page keeps one row per automation.
  sub_jobs?: AutomationProcess[]
  // Steps that run inside this job's task, on this job's trigger, in execution
  // order. A step is not an automation — it has no trigger of its own — so it is
  // reported here rather than as a peer row. Session insights owns the four-step
  // archive pipeline; everything else has none.
  steps?: AutomationProcess[]
  // Name of the whole pipeline, set only on the job that owns one
  // ("When you archive a chat"). The job keeps `label` for its own step.
  pipeline_label?: string
  // Set on a step: when it is skipped, in the user's terms. A step answers this
  // instead of "when does this run?", which its pipeline already answers.
  step_condition?: string
  // True while this job is inside a tracked run right now.
  running?: boolean
  last_run: JobRun | null
  recent: JobRun[]
  stats: AutomationStats
}

// ── Multi-device (host / client) ───────────────────────────────────────────
export interface NodePeer {
  node_id: string
  url: string
  last_seen: string
  is_active: boolean
}

export interface NodeStatus {
  node_id: string
  role: 'host' | 'client' | 'active' | 'standby'
  mode?: 'host' | 'client'
  active_since: string | null
  last_handover: string | null
  host_url?: string | null
  active_peer_url?: string | null
  host_reachable?: boolean | null
  active_peer_reachable?: boolean | null
  // Name and version of the machine a client is mirroring. Only present when
  // the host answered the reachability ping.
  host_node_id?: string
  host_version?: string
  has_host_session?: boolean
  peers: NodePeer[]
  // From LocalSessionManager.status() on the host (ciao/local_session.py).
  git?: LocalGitStatus
}

/** Workspace git state, as `/api/node/status` reports it under `git`. */
export interface LocalGitStatus {
  git_repo?: boolean
  branch?: string
  dirty?: boolean
  dev_mode?: boolean
}

/**
 * What the small action endpoints answer with: `/api/device/update`,
 * `/api/node/connect`, `/api/node/handover`. Callers only branch on `ok` and
 * show `error`; the rest of the body is diagnostic, hence the index signature.
 */
export interface ActionResult {
  ok?: boolean
  error?: string
  [key: string]: unknown
}

/** Per-subchat row in the archive cascade response. */
export interface ArchivedSubchat {
  chat_id: string
  archived: boolean
  stopped_mid_turn: boolean
  error: string
}

/**
 * `POST /api/chats/{id}/archive`.
 *
 * Archiving a supervisor cascades to its delegate subchats, and the cascade can
 * partly fail. The id lists are the contract that matters: mark archived only
 * what `archived_chat_ids` names, because a subchat missing from it is still
 * streaming, and hiding it would leave it burning tokens out of sight.
 * The fields are optional so a client talking to an older host (or through the
 * node proxy) degrades to "the chat I asked for" instead of breaking.
 */
export interface ArchiveChatResponse {
  ok?: boolean
  archived_to?: string | null
  postprocess?: ChatPostprocess | null
  archived_chat_ids?: string[]
  stopped_chat_ids?: string[]
  failed_chat_ids?: string[]
  subchats?: ArchivedSubchat[]
}

/** One commit row in the update modal, from ciao/package_version.py. */
export interface ChangelogCommit {
  sha: string
  subject: string
}

/** `/api/package/changelog` and `/api/device/changelog`. */
export interface PackageChangelog {
  commits: ChangelogCommit[]
  compare_url: string
  repo?: string
  error: string
  current_version?: string
  latest_version?: string
  update_available?: boolean
}

/** `/api/package/update` and `/api/device/update`. */
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
}
