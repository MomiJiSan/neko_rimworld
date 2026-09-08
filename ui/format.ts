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

export function budgetError(values: Record<string, number | "">, fields: Array<{ key: string; label: string; min: number; max: number }>): string | null {
  for (const field of fields) {
    const value = values[field.key]
    if (typeof value !== "number" || !Number.isFinite(value) || value < field.min || value > field.max) return `${field.label}需要在 ${field.min}–${field.max} 秒之间。`
  }
  return null
}
