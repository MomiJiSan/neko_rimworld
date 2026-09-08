import { EmptyState, JsonView, KeyValue, Stack } from "@neko/plugin-ui"
import type { AgentAuditEvent } from "../types"
import { text, timing } from "../format"

export function EventDetails({ event }: { event?: AgentAuditEvent }) {
  if (!event) return <EmptyState title="选择一条事件" description="查看输入、输出、观察或请求阶段诊断；隐藏推理和凭据不会显示。" />
  return <Stack>
    <KeyValue items={[
      { key: "event", label: "事件", value: text(event.event) },
      { key: "time", label: "经过时间", value: timing(event.elapsed_ms) },
      { key: "cycle", label: "轮次", value: text(event.cycle) },
    ]} />
    <JsonView data={event.data || {}} />
  </Stack>
}
