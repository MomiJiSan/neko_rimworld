import type { AgentRun, UsageTotals } from "../types"

export type WorkSummary = {
  workspace_id: string; owner: string; colony_id: string | null; work_state: string
  last_question?: string; last_status?: string; turn_count?: number; current_colony?: boolean
  active?: AgentRun | null
}
export type ExchangeSummary = { turn_id: string; sequence: number; question: string; status: string; preview?: string; created_at?: string }
export type WorkList = { workspaces: WorkSummary[]; total: number; next_offset: number | null }
export type WorkDetails = {
  workspace_id: string; workspace: WorkSummary; turns: ExchangeSummary[]; total: number; next_offset: number | null
  active?: AgentRun | null
  checkpoint?: { checkpoint_id: string; data: { work_note: string; open_questions: string[] }; verified_lesson: false } | null
}
export type ConsultationResult = {
  status: string; answer?: string; summary?: string; agent_text?: string; caveats?: string[]
  audit?: { run_id?: string }; report?: { report_id: string; saved: boolean }
  delivery?: { handoff_tokens?: number; transport_token_budget?: number; token_budget?: number; submission_mode?: string }
  observations?: unknown[]; usage?: UsageTotals
}
export type ExchangeDetails = {
  workspace_id: string; turn_id: string; question: string; status: string; history_saved: boolean
  result: ConsultationResult | null; active?: AgentRun | null; audit_available: boolean
  evidence: Array<{ observation_id: string; sha256: string }>
  handoff?: { summary: string; prepared_at: string; history_saved: boolean; report?: { saved?: boolean; export?: { status?: string } } } | null
  report?: { report_id: string; full_text: string; state: string }; report_unavailable?: boolean
}
export type EvidenceView = { workspace_id: string; turn_id: string; observation_id: string; sha256: string; observation: unknown }
