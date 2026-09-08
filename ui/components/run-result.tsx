import { Alert, Button, CodeBlock, EmptyState, Field, KeyValue, Stack, StatusBadge, TextBlock } from "@neko/plugin-ui"
import { statusTone, text } from "../format"

export function RunResult({ result, draft, onOpenReport }: { result?: Record<string, any> | null; draft?: Record<string, any> | null; onOpenReport?: (id: string) => void }) {
  if (!result && draft?.full_text) return <Stack><Alert tone="warning">已保存提交草稿，尚未正式交付。</Alert><TextBlock text={text(draft.full_text, "", 12000)} /></Stack>
  if (!result) return <EmptyState title="尚无最终结果" description="运行可能尚未结束，或在提交结果前被取消。可查看事件和模型请求。" />
  const status = text(result.status, "unknown")
  return <Stack>
    <StatusBadge tone={statusTone(status)} label={status} />
    {status !== "ok" ? <Alert tone="warning">本次运行未正常交付。以下保留已返回内容，不代表调查成功。</Alert> : null}
    {result.report?.saved ? <Stack><Field label="报告 ID"><CodeBlock>{result.report.report_id}</CodeBlock></Field>{onOpenReport ? <Button onClick={() => onOpenReport(result.report.report_id)}>打开保存的完整报告</Button> : null}</Stack> : result.report?.saved === false ? <Alert tone="warning">报告持久保存尚未确认，请复制当前正文。重载可能清空运行记录。</Alert> : null}
    <Field label="完整回答"><TextBlock text={text(result.answer, "没有返回完整回答。", 12000)} /></Field>
    <Field label="交给 NEKO 的文本"><TextBlock text={text(result.summary, "没有可交付的文本。", 4000)} /></Field>
    {Array.isArray(result.caveats) && result.caveats.length ? <TextBlock text={result.caveats.map((x: any) => text(x)).join("\n")} /> : null}
    <KeyValue items={[
      { key: "mode", label: "提交方式", value: text(result.delivery?.submission_mode) },
      { key: "full", label: "完整回答文本 token", value: text(result.delivery?.full_answer_tokens) },
      { key: "handoff", label: "交接文本 token / 上限", value: `${text(result.delivery?.handoff_tokens)} / ${text(result.delivery?.token_budget)}` },
    ]} />
  </Stack>
}
