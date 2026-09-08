import { Alert, Grid, KeyValue, Stack, StatCard, TextBlock } from "@neko/plugin-ui"
import { timing, tokenValue } from "../format"
import type { AgentRun, AgentRuntimeState } from "../types"

export function UsageSummary({ run, history }: { run?: AgentRun | null; history?: AgentRuntimeState }) {
  const requests = run?.model_calls ?? 0
  return <Stack>
    <Grid cols={4}>
      <StatCard label="模型请求" value={run ? requests : "—"} />
      <StatCard label="工具调用" value={run?.tool_calls ?? "—"} />
      <StatCard label="已知总 token" value={tokenValue(run?.usage, "total_tokens", requests)} />
      <StatCard label="运行耗时" value={timing(run?.duration_ms)} />
    </Grid>
    <KeyValue items={[
      { key: "input", label: "输入 token", value: tokenValue(run?.usage, "prompt_tokens", requests) },
      { key: "output", label: "输出 token", value: tokenValue(run?.usage, "completion_tokens", requests) },
      { key: "reported", label: "返回用量的请求", value: run ? `${run.usage?.reported_requests ?? 0} / ${requests}` : "—" },
      { key: "failures", label: "请求失败 / 取消", value: `${run?.model_failures ?? 0} / ${run?.model_cancellations ?? 0}` },
    ]} />
    {requests > 0 && !run?.usage?.complete ? <Alert tone="warning">用量不完整。“+ ?”表示已知小计；超时或取消的请求也可能消耗 token。</Alert> : null}
    {history?.totals ? <TextBlock text={`最近保留 ${history.totals.runs ?? 0} 次运行：模型请求 ${history.totals.model_calls ?? 0} 次，已知 token ${tokenValue(history.totals.usage, "total_tokens", history.totals.model_calls ?? 0)}。最多保留 ${history.retained_run_limit ?? 20} 次已结束运行，插件重载后清空。`} /> : null}
  </Stack>
}
