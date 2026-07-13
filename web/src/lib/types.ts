export type WorkspaceName = string
export type WorkspaceProvider = 'claude' | 'codex' | 'ollama' | 'openrouter'

export interface WorkspaceProviderOption {
  value: WorkspaceProvider
  label: string
}

export interface WorkspaceInfo {
  name: WorkspaceName
  vault_root: string
  default_provider: WorkspaceProvider
  default_model: string
  disallowed_tools?: string[] | null
  // claude.ai connector MCP toggle. null = per-workspace default
  // (personal off, else on). When off, the connector set is added to the
  // effective denylist; disallowed_tools covers the extra tools.
  claude_ai_mcps?: boolean | null
  gws_profile: string
  model_bucket: string
}

export interface WorkspacesResponse {
  workspaces: WorkspaceInfo[]
  active: WorkspaceName | null
  // App-wide fallback model used when a workspace's default_model is empty.
  app_default_model?: string
  provider_options?: WorkspaceProviderOption[]
  // claude.ai connector MCP names the per-workspace toggle controls.
  claude_ai_connectors?: string[]
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
  // Runtime provider. Claude also covers Ollama/OpenRouter env-injection;
  // Codex uses the authenticated OpenAI CLI app-server session.
  provider: 'claude' | 'codex'
  // Claude routing bucket. Legacy values: 'work'/'anthropic' pin Anthropic,
  // 'personal'/'ollama' pin Ollama routing. '' = auto from project workspace.
  // Only meaningful when provider is 'claude'.
  model_bucket?: string
  mode: string
  // Provider-native thinking/reasoning level ('' = provider default).
  // Allowed values per provider come from ModelsResponse.thinking_levels.
  thinking_level?: string
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
    }
  | { type: 'thinking'; text: string; parent_tool_use_id?: string }
  | { type: 'status'; message: string }
  // Running token totals for the in-flight turn (cumulative, monotonic).
  // Emitted from partial stream events so the live trace can show a token
  // count as the model works; the authoritative totals still land on `result`.
  | { type: 'token_usage'; input_tokens: number; output_tokens: number }
  | { type: 'result'; text: string; is_error: boolean; effective_model: string; usage: Record<string, string>; quota?: Record<string, unknown>; session_id: string; sent_at?: string; completed_at?: string; duration_ms?: number }
  | { type: 'permission_request'; tool_name: string; tool_input?: string; message: string; request_id: string }
  | { type: 'chat_title'; chat_id: string; title: string }
  | { type: 'user_echo'; text: string; images?: string[]; turn_index?: number; sent_at?: string }
  | { type: 'queued'; text: string; images?: string[] }
  | { type: 'steered'; text: string; images?: string[] }
  | { type: 'error'; message: string }
  | { type: 'chat_retry'; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number }

// Global awareness events from /ws/events
export type EventsWsMessage =
  | { type: 'snapshot'; active_streams: { chat_id: string; project_id: string }[]; background_agents?: Record<string, number> }
  | { type: 'chat_streaming_started'; chat_id: string; project_id: string }
  | { type: 'chat_streaming_done'; chat_id: string; project_id: string; is_error: boolean }
  | { type: 'chat_result_ready'; chat_id: string; project_id: string; title: string; snippet: string }
  | { type: 'chat_subagents_ready'; chat_id: string; project_id: string; remaining: number }
  | { type: 'chat_read'; chat_id: string; last_read_at: string }
  | { type: 'chat_title'; chat_id: string; title: string; status?: 'pending' | 'ready' }
  | { type: 'chat_moved'; chat_id: string; project_id: string; old_project_id: string }
  | { type: 'chat_deleted'; chat_id: string; project_id: string; reason?: string }
  | { type: 'chat_retry'; chat_id: string; project_id: string; status: 'pending' | 'stopped' | ''; next_at?: string; last_error?: string; attempts?: number; interval_seconds?: number }
  | { type: 'project_created'; project: ProjectInfo }
  | { type: 'project_updated'; project: ProjectInfo }
  | { type: 'project_deleted'; project_id: string }
  | { type: 'open_chat'; chat_id: string }

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
  days_of_week: string[] | null
  thread_id: number | null
  context_label: string
  frequency: 'daily' | 'weekly' | 'monthly' | 'manual' | 'once'
  day_of_month: number | null
  run_at_date: string | null
  web_chat_id: string | null
  web_project_id: string | null
  model: string
  provider?: 'claude' | 'codex'
  next_run: string | null
  last_expected_run: string | null
  missed: boolean
  enabled: boolean
  archive_policy: ScheduleArchivePolicy
  title?: string
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
  // Keyed by picker bucket (claude_work, claude_personal, openrouter).
  provider_models: Record<string, string[]>
  provider_defaults: Record<string, string>
  // Names listed in CIAO_OLLAMA_MODELS, repeated here so the UI can
  // derive a chat's active bucket from (provider, model) without an
  // extra round-trip.
  ollama_models?: string[]
  // Subset of ollama_models served by the local Ollama daemon (free,
  // on-device); the picker can badge these as "local".
  ollama_local_models?: string[]
  // OpenRouter owner/model ids available as a backend.
  openrouter_models?: string[]
  // Account-visible Codex models and their app-server metadata.
  codex_models?: string[]
  codex_model_metadata?: Record<string, {
    display_name: string
    description: string
    default_reasoning_effort: string
    input_modalities: string[]
  }>
  model_reasoning_levels?: Record<string, string[]>
  // Per-backend haiku/sonnet/opus/fable tier models and which
  // backends are configured/available.
  alias_tiers?: Record<string, Record<string, string>>
  backends?: Record<string, boolean>
  // Keyed by runtime provider; Claude buckets share the SDK effort levels,
  // while Codex is additionally narrowed by model_reasoning_levels.
  thinking_levels?: Record<string, string[]>
}

// GET/PATCH /api/settings/routines — internal-routine model overrides and
// voice transcription engine (Settings → Models tab).
export interface RoutineSettings {
  // Overrides as stored; empty string = automatic default.
  title_model: string
  insights_model: string

  critique_models: string
  ollama_haiku_model: string
  ollama_sonnet_model: string
  ollama_opus_model: string
  ollama_fable_model: string
  openrouter_haiku_model: string
  openrouter_sonnet_model: string
  openrouter_opus_model: string
  openrouter_fable_model: string
  // What actually runs right now, after defaults.
  title_model_effective: string
  insights_model_effective: string

  critique_models_effective: string
  // Env-backed models used when a tier override is cleared.
  tier_defaults?: Record<string, Record<string, string>>
  alias_tiers?: Record<string, Record<string, string>>
  transcription: {
    engine: 'cloud' | 'local'
    local_model: string
    local_available: boolean
    cloud_available: boolean
  }
  speech: {
    engine: 'cloud' | 'local'
    cloud_voice: string
    local_voice: string
    local_available: boolean
    cloud_available: boolean
  }
  model_options: {
    anthropic: string[]
    ollama_cloud: string[]
    ollama_local: string[]
    openrouter?: string[]
  }
  backends?: Record<string, boolean>
  workspace_context?: {
    workspace_root: string
    vault_root: string
  }
}

export interface ProviderConnection {
  name: string
  ok: boolean
  auth: string
  command: string
  detail?: string
  version?: string
  account?: string
  protocol?: string
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
  source: 'project' | 'user'
  path: string
}

export interface CommandsResponse {
  commands: SlashCommand[]
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
  last_run: JobRun | null
  recent: JobRun[]
  stats: AutomationStats
}
