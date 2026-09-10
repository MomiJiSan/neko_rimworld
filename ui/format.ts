import type { HostedAction, Tone } from "@neko/plugin-ui"
import type { UsageTotals } from "./types"

export function text(value: unknown, fallback = "-", maxLength = 300): string {
  if (value && typeof value === "object" && Array.isArray((value as any).text_chunks)) value = (value as any).text_chunks.join("")
  if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return fallback
  const result = String(value).trim()
  if (!result) return fallback
  return result.length > maxLength ? `${result.slice(0, maxLength - 1)}…` : result
}

export function shortId(value: unknown): string {
  const result = text(value, "", 160)
  if (!result) return "-"
  return result.length <= 16 ? result : `${result.slice(0, 8)}…${result.slice(-4)}`
}

export function statusTone(status: string): Tone {
  if (["ready", "listening", "ok", "active", "connected"].includes(status)) return "success"
  if (["starting", "authenticating", "stale", "partial", "busy", "degraded", "cancelled"].includes(status)) return "warning"
  if (["error", "failed", "stop_failed"].includes(status)) return "danger"
  return "default"
}

export function levelTone(level: string): Tone {
  if (["error", "fatal", "critical"].includes(level)) return "danger"
  if (["warning", "warn"].includes(level)) return "warning"
  if (["debug", "trace"].includes(level)) return "default"
  return "info"
}

export function actionById(actions: HostedAction[], id: string): HostedAction | undefined {
  return actions.find((action) => action.id === id || action.entry_id === id)
}

export function actionPayload(envelope: any): any {
  return envelope?.result && typeof envelope.result === "object" ? envelope.result : envelope
}

export function actionSummary(envelope: any, fallback = "Action completed."): string {
  const result = actionPayload(envelope)
  const status = text(result?.status, "unknown", 80).toLowerCase()
  const summary = text(result?.summary, `${fallback} Status: '${status}'.`, 400)
  if (["error", "failed", "rejected"].includes(status)) throw new Error(summary)
  return summary
}

export function normalizedError(error: unknown, secret = ""): Error {
  const raw = error instanceof Error ? error.message : typeof error === "string" ? error : error && typeof error === "object" && typeof (error as any).message === "string" ? (error as any).message : "The request failed."
  const safe = secret ? raw.split(secret).join("[redacted]") : raw
  return new Error(text(safe, "The request failed.", 500))
}

export function tokenValue(usage: UsageTotals | undefined, key: string, requests: number): string {
  const value = usage?.[key as keyof UsageTotals]
  if (typeof value !== "number") return "—"
  const partial = (usage?.coverage?.[key] ?? 0) < requests
  return `${value.toLocaleString()}${partial ? " + ?" : ""}`
}

export function timing(value: unknown): string {
  return typeof value === "number" ? `${(value / 1000).toFixed(1)} 秒` : "—"
}

export function progressText(run: Record<string, any> | null | undefined): string {
  const event = text(run?.latest_event?.event, "")
  const data = run?.latest_event || {}
  if (event === "model_request_started") return `正在请求模型（第 ${text(data.cycle, text(run?.cycle))} 轮）`
  if (event === "model_retry_scheduled") return "正在重试模型"
  if (event === "model_response_received") return "模型响应已返回，正在处理"
  if (event === "tool_batch_started" || event === "tool_started") return "正在读取游戏信息"
  if (event === "tool_completed" || event === "tool_batch_completed") return "游戏信息读取完成，正在继续分析"
  if (event === "model_request_attempt_failed") return "模型请求暂时失败，正在判断是否重试"
  if (event === "model_request_failed") return "模型请求失败"
  if (event === "final_received" || event === "degraded_final_created") return "正在整理最终结果"
  if (run?.terminal || event === "run_completed") return "运行已结束"
  return text(run?.latest_event_summary, text(run?.stage, "正在运行"), 300)
}

export function budgetError(values: Record<string, number | "">, fields: Array<{ key: string; label: string; min: number; max: number }>): string | null {
  for (const field of fields) {
    const value = values[field.key]
    if (field.key === "max_output_tokens" && !Number.isInteger(value)) return "输出 token 上限必须为整数。"
    if (typeof value !== "number" || !Number.isFinite(value) || value < field.min || value > field.max) return `${field.label}需要在 ${field.min}–${field.max} 秒之间。`
  }
  return null
}
