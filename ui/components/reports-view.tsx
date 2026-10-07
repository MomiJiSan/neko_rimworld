import { Alert, Button, Card, CodeBlock, DataTable, EmptyState, Field, Grid, InlineError, JsonView, Markdown, useClipboard, Stack, StatusBadge, TextBlock, Toolbar, ToolbarGroup } from "@neko/plugin-ui"
import type { DiagnosticsController } from "../diagnostics/useDiagnostics"
import { text } from "../format"

export function ReportsView({ c }: { c: DiagnosticsController }) {
  const clipboard = useClipboard()
  const report = c.report
  return <Grid cols={2}>
    <Card title="已保存的报告"><Stack>
      <TextBlock text="完整报告与未完成草稿独立保存，插件重载后可取回；最多保留最近 100 份。同一工作的历史可按需检索；明确指定报告 ID 也可以追问。" />
      <Button disabled={c.reportLoading} onClick={c.loadReports}>{c.reportLoading ? "读取中…" : "刷新报告"}</Button>
      {c.reportError ? <InlineError title="报告读取失败" error={c.reportError} /> : null}
      <DataTable data={c.reportPage?.reports || []} rowKey="report_id" selectedKey={c.selectedReportId} onSelect={row => c.openReport(row.report_id)} maxRows={20} emptyText="尚无保存的报告。" columns={[
        { key: "question", label: "问题", render: row => text(row.question, "咨询", 130) },
        { key: "state", label: "状态", render: row => <StatusBadge tone={row.state === "complete" ? "success" : "warning"} label={row.state === "complete" ? "已完成" : "草稿"} /> },
        { key: "created_at", label: "时间", render: row => text(row.created_at) },
      ]} />
      <Toolbar><ToolbarGroup><Button disabled={c.reportOffset === 0 || c.reportLoading} onClick={() => { c.setReportOffset(Math.max(0, c.reportOffset - 20)) }}>上一页报告</Button><Button disabled={c.reportPage?.next_offset == null || c.reportLoading} onClick={() => { c.setReportOffset(c.reportPage!.next_offset) }}>下一页报告</Button></ToolbarGroup></Toolbar>
      <TextBlock text={`共 ${c.reportPage?.total ?? 0} 份`} />
    </Stack></Card>
    <Card title="报告全文"><Stack>
      {report ? <Stack>
        <Alert tone={report.state === "complete" ? "info" : "warning"}>{report.state === "complete" ? "这是历史报告，不代表当前游戏状态。" : "这是已保存的草稿：提交未完成，不能当作正式交付。"}</Alert>
        <Field label="报告 ID"><CodeBlock>{report.report_id}</CodeBlock></Field>
        <TextBlock text={`${text(report.created_at)} · ${text(report.question, "咨询", 2000)}`} />
        <Field label="完整正文"><Markdown source={text(report.full_text, "", 12000)} /></Field>
        <Toolbar><ToolbarGroup><Button onClick={async () => { await clipboard.write(text(report.full_text, "", 12000)) }}>复制完整正文</Button><Button onClick={async () => { await clipboard.write(report.report_id) }}>复制报告 ID</Button></ToolbarGroup></Toolbar>
        {clipboard.copied ? <TextBlock text="已复制" /> : null}
        {clipboard.error ? <InlineError title="复制失败" error={clipboard.error} /> : null}
        <Button tone="primary" disabled={c.running} onClick={() => c.askAboutReport(report.report_id)}>基于这份报告追问</Button>
        {report.reference_report_id ? <Button onClick={() => c.openReport(report.reference_report_id)}>查看引用的原报告</Button> : null}
        {report.evidence_status === "omitted_size_limit" ? <Alert tone="warning">观察数据超出保存上限，正文与采样标识已保留，原始观察未保留。</Alert> : null}
        <TextBlock text={`原调查包含 ${report.observation_count ?? 0} 份观察；不同采样不合并为同一快照。`} />
        {report.state === "draft" ? <JsonView data={report.draft_errors || {}} /> : null}
      </Stack> : <EmptyState title={c.reportLoading ? "正在读取报告" : "选择一份报告"} />}
    </Stack></Card>
  </Grid>
}
