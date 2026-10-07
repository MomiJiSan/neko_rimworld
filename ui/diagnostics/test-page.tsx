import { Alert, Button, Card, Field, Grid, InlineError, KeyValue, Stack, StatusBadge, Textarea, TextBlock } from "@neko/plugin-ui"
import type { DiagnosticsController } from "./useDiagnostics"
import { statusTone, text } from "../format"

export function IndependentTestPage({ c }: { c: DiagnosticsController }) {
  return <Grid cols={2}>
    <Card title="Agent 咨询测试"><Stack>
      {c.referenceReportId ? <Stack><Alert tone="info">本次明确引用历史报告：{c.referenceReportId}。新查询仍会单独记录采样时间。</Alert><Button disabled={c.running} onClick={() => { c.setReferenceReportId("") }}>取消报告引用</Button></Stack> : null}
      <Field label="问题" help="独立咨询测试。与 N.E.K.O 的持续工作分开；切换页面不会取消或重新提交。" required>
        <Textarea value={c.question} onChange={c.setQuestion} placeholder="现在殖民地整体情况怎么样？" />
      </Field>
      {c.testError ? <InlineError title="咨询执行失败" error={c.testError} /> : null}
      <Button tone="primary" disabled={c.running || !c.question.trim() || !c.state.configuration?.agent_configured} onClick={c.runTest}>{c.running ? "Agent 正在运行…" : "运行咨询"}</Button>
      {c.running ? <Alert tone="info">可以切换到主页查看状态，或在会话页跟随最新运行。</Alert> : null}
      {c.testResult ? <Stack><StatusBadge tone={statusTone(text(c.testResult.status))} label={text(c.testResult.status)} /><TextBlock text={text(c.testResult.summary, "本次未返回可交付文本。", 1500)} /></Stack> : null}
      {c.testResult?.report?.saved ? <Button onClick={() => c.openReport(c.testResult!.report.report_id)}>打开保存的完整报告</Button> : null}
      {c.testResult?.audit?.run_id ? <Button onClick={() => c.openRun(c.testResult!.audit.run_id)}>打开本次会话详情</Button> : c.testError && c.latestRun?.run_id ? <Button onClick={() => c.openRun(c.latestRun!.run_id!)}>查看最近会话</Button> : null}
    </Stack></Card>
    <Card title="游戏通路探测"><Stack>
      <TextBlock text="直接读取一次殖民地概况，用于检查 Mod 与插件连接，不调用模型。" />
      <Button disabled={c.probing} onClick={c.probe}>{c.probing ? "探测中…" : "探测游戏概况"}</Button>
      {c.probeError ? <InlineError title="探测失败" error={c.probeError} /> : null}
      {c.probeResult ? <KeyValue items={[
        { key: "status", label: "状态", value: text(c.probeResult.status) },
        { key: "summary", label: "说明", value: text(c.probeResult.summary) },
        { key: "tick", label: "游戏 tick", value: text(c.probeResult.response?.game_tick) },
      ]} /> : null}
      <Button onClick={() => { c.setSessionMode("connection"); c.setDiagnosticMode("records") }}>查看连接与协议详情</Button>
    </Stack></Card>
  </Grid>
}
