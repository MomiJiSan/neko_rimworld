export type ConnectionState = {
  connection_epoch?: string
  mod_version?: string
  game_version?: string
  capabilities?: string[]
  authenticated_at?: string
}

export type SessionState = {
  colony_id?: string
  load_epoch?: string
  game_tick?: number
  snapshot_sequence?: number
  stale?: boolean
}

export type ProtocolState = {
  version?: number
  rx_frames?: number
  tx_frames?: number
  auth_failures?: number
  invalid_frames?: number
  last_rx_at?: string
  last_tx_at?: string
}

export type QueryState = {
  status?: string
  latency_ms?: number
  error_code?: string | null
  completed_at?: string
}

export type IncidentState = {
  incident_id?: string
  category?: string
  message?: string
  timestamp?: string
}

export type DiagnosticEvent = {
  sequence?: number
  timestamp?: string
  level?: string
  category?: string
  message?: string
  incident_id?: string
}

export type AgentAuditEvent = {
  event_seq?: number
  timestamp?: string
  elapsed_ms?: number
  event?: string
  cycle?: number
  state_before?: string | null
  state_after?: string
  data?: Record<string, any>
}

export type UsageTotals = {
  prompt_tokens?: number | null
  completion_tokens?: number | null
  total_tokens?: number | null
  cached_tokens?: number | null
  reasoning_tokens?: number | null
  reported_requests?: number
  unreported_requests?: number
  complete?: boolean
  coverage?: Record<string, number>
}

export type AgentRun = {
  draft?: Record<string, any> | null
  run_id?: string
  question?: string
  result_status?: string | null
  result_summary?: unknown
  events_page?: { offset: number; limit: number; total: number; has_more: boolean }
  state?: string
  cycle?: number
  duration_ms?: number
  event_count?: number
  model_calls?: number
  model_responses?: number
  model_failures?: number
  model_cancellations?: number
  usage?: UsageTotals
  latest_request?: Record<string, any> | null
  tool_calls?: number
  tool_failures?: number
  events?: AgentAuditEvent[]
  result?: Record<string, any> | null
}

export type AgentRuntimeState = {
  totals?: AgentRun & { runs?: number }
  retained_run_limit?: number
  active_count?: number
  recent_count?: number
  active_runs?: AgentRun[]
  recent_runs?: AgentRun[]
  latest_run?: AgentRun | null
}

export type DashboardState = {
  action_nonce?: string
  status?: string
  endpoint?: string | null
  port?: number | null
  worker_alive?: boolean
  runtime?: { phase?: string; worker_alive?: boolean; started_at?: string }
  configuration?: {
    token_configured?: boolean
    agent_configured?: boolean
    agent_api_key_configured?: boolean
    agent_base_url?: string
    agent_model?: string
    agent_budgets?: Record<string, number>
  }
  agent?: AgentRuntimeState
  connection?: ConnectionState | null
  session?: SessionState | null
  metrics?: { pending_requests?: number; outbound_depth?: number }
  protocol?: ProtocolState
  last_query?: QueryState | null
  last_incident?: IncidentState | null
  panel_error?: IncidentState | null
  diagnostics?: DiagnosticEvent[]
  updated_at?: string
}

export type PageId = "home" | "sessions" | "debug" | "settings"
export type RunView = "result" | "events" | "observations" | "model"
export type BudgetValues = Record<string, number | "">
export const BUDGET_FIELDS = [
  { key: "connect_timeout_seconds", label: "连接等待", min: 1, max: 30, initial: 10 },
  { key: "first_response_timeout_seconds", label: "首个有效响应", min: 1, max: 60, initial: 45 },
  { key: "stream_idle_timeout_seconds", label: "流读取空闲", min: 1, max: 60, initial: 20 },
  { key: "request_timeout_seconds", label: "单次模型请求上限", min: 3, max: 60, initial: 60 },
  { key: "run_budget_seconds", label: "整个 Agent 运行上限", min: 10, max: 90, initial: 90 },
  { key: "finalization_reserve_seconds", label: "结束提交预留", min: 0, max: 30, initial: 15 },
]
