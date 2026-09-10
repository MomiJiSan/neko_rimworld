import { Alert, Field, KeyValue, Stack, TextBlock } from "@neko/plugin-ui"
import { text } from "../format"

export function FailureNotice({ summary, observationCount, compact = false }: { summary?: Record<string, any> | null; observationCount?: number; compact?: boolean }) {
  if (!summary) return null
  const provider = summary.provider_error || {}
  const providerMessage = text(provider.message, "", 500)
  const marker = `${text(provider.code, "")} ${text(provider.type, "")} ${providerMessage}`.toLowerCase()
  const overloaded = marker.includes("overload") || marker.includes("upstream") || marker.includes("try again") || marker.includes("unavailable")
  const finalCode = text(summary.error_code, "unknown")
  const timedOut = finalCode.includes("timeout") || finalCode.includes("deadline")
  const limited = finalCode === "model_output_limit" || summary.finish_reason === "length"
  const title = limited ? "输出预算不足" : overloaded ? "模型服务过载" : timedOut ? "模型请求超时" : "模型服务请求失败"
  const retries = Number(summary.retries || 0)
  const explanation = limited ? "模型达到单次输出 token 上限，回答未完成。思考用量也可能占用该预算。" : overloaded && timedOut
    ? "模型供应商连续返回服务过载；自动重试后，最后一次尝试在剩余运行预算内超时。"
    : overloaded
      ? "模型供应商返回服务过载或上游暂时不可用。"
      : timedOut
        ? "模型供应商未能在本次运行的可用时间内返回完整响应。"
        : text(summary.message, "模型服务没有返回可用响应。", 500)
  const action = limited ? "建议：在设置页提高单次模型输出上限后重试；无需重启 RimWorld。" : overloaded
    ? "建议：稍后重试或更换模型服务；无需重启 RimWorld。"
    : timedOut
      ? "建议：检查模型服务响应速度，或调整运行预算后重试。"
      : "建议：根据错误类型检查模型名称、服务地址和供应商状态。"
  return <Stack>
    <Alert tone="danger">{title}：{explanation}</Alert>
    {retries ? <TextBlock text={`已自动重试 ${retries} 次，仍未取得最终回答。`} /> : <TextBlock text="该错误不满足安全重试条件，或剩余预算不足。" />}
    {typeof observationCount === "number" ? observationCount > 0 ? <TextBlock text={`已取得 ${observationCount} 份游戏观察，但模型未能生成最终回答；游戏查询结果没有丢失。`} /> : <TextBlock text="模型在取得可用游戏观察前失败。" /> : null}
    <TextBlock text={action} />
    {!compact && providerMessage ? <Field label="供应商返回"><TextBlock text={providerMessage} /></Field> : null}
    {!compact ? <KeyValue items={[
      { key: "error", label: "最终错误", value: finalCode },
      { key: "finish", label: "结束原因", value: text(summary.finish_reason) },
      { key: "limit", label: "本次请求输出上限", value: text(summary.max_output_tokens) },
      { key: "usage", label: "实际输出 / 其中推理 token", value: `${text(summary.usage?.completion_tokens)} / ${text(summary.usage?.reasoning_tokens)}` },
      { key: "provider", label: "供应商错误类型 / code", value: `${text(provider.type)} / ${text(provider.code)}` },
      { key: "phase", label: "失败阶段", value: text(summary.phase) },
      { key: "attempts", label: "本轮尝试 / 失败 / 自动重试", value: `${text(summary.current_cycle_attempts, text(summary.attempts))} / ${text(summary.failed_attempts)} / ${text(summary.retries)}` },
    ]} /> : null}
  </Stack>
}
