import { Alert, Button, Card, EmptyState, Grid, Stack, StatCard, StatusBadge, TextBlock, Toolbar, ToolbarGroup } from "@neko/plugin-ui"
import type { PanelController } from "../controller"
import { UsageSummary } from "../components/usage-summary"
import { statusTone, text } from "../format"

export function HomePage({ c }: { c: PanelController }) {
  const state = c.state
  const latest = c.latestRun
  const incident = state.last_incident || state.panel_error
  return <Stack>
    <Grid cols={4}>
      <StatCard label="游戏连接" value={state.connection ? "已认证" : "未连接"} />
      <StatCard label="游戏会话" value={state.session ? state.session.stale ? "已失效" : "有效" : "未加载"} />
      <StatCard label="模型配置" value={state.configuration?.agent_configured ? "已配置" : "未配置"} />
      <StatCard label="运行中的咨询" value={state.agent?.active_count ?? 0} />
    </Grid>
    {!state.configuration?.token_configured || !state.configuration?.agent_configured ? <Alert tone="warning">连接或模型尚未配置完整。请在设置页完成配置。</Alert> : null}
    <Card title="最近一次运行与用量"><UsageSummary run={latest} history={state.agent} /></Card>
    <Card title="最近结果">
      {latest ? <Stack>
        <TextBlock text={text(latest.question, "咨询", 300)} />
        <StatusBadge tone={statusTone(text(latest.result_status || latest.state))} label={text(latest.result_status || latest.state)} />
        <TextBlock text={text(latest.result_summary, "尚未返回最终交接文本。", 1000)} />
        <Button onClick={() => c.openRun(latest.run_id!)}>查看本次详情</Button>
      </Stack> : <EmptyState title="还没有运行记录" description="在调试页发起一次咨询，或从 NEKO 对话中调用插件。" />}
    </Card>
    {incident ? <Card title="最近异常"><Stack><TextBlock text={text(incident.message, "发生运行异常。", 500)} /><Button onClick={() => { c.setSessionMode("connection"); c.setPage("sessions") }}>查看连接事件</Button></Stack></Card> : null}
    <Toolbar><ToolbarGroup><Button tone="primary" onClick={() => { c.setPage("debug") }}>进入调试</Button><Button onClick={() => { c.setPage("settings") }}>打开设置</Button></ToolbarGroup></Toolbar>
  </Stack>
}
