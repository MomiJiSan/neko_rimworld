import { ReportsView } from "../components/reports-view"
import { NavigationTabs } from "../components/navigation-tabs"
import { Alert, Button, Card, DataTable, EmptyState, Field, Grid, InlineError, Input, JsonView, KeyValue, Stack, StatusBadge, TextBlock, Toolbar, ToolbarGroup } from "@neko/plugin-ui"
import type { DiagnosticsController } from "./useDiagnostics"
import { EventDetails } from "../components/event-details"
import { RunResult } from "../components/run-result"
import { UsageSummary } from "../components/usage-summary"
import { levelTone, progressText, shortId, statusTone, text, timing } from "../format"

export function RunHistoryView({ c }: { c: DiagnosticsController }) {
  const detail = c.detail
  const events = Array.isArray(detail?.events) ? detail.events : []
  const visibleEvents = events.filter(event => (!c.eventFilter || text(event.event).toLowerCase().includes(c.eventFilter.toLowerCase())) && (c.runView !== "model" || text(event.event).startsWith("model_")))
  const event = events.find(item => item.event_seq === c.selectedEvent)
  const connectionEvents = (c.state.diagnostics || []).slice().reverse().filter(item => (!c.connectionLevel || text(item.level).toLowerCase().includes(c.connectionLevel.toLowerCase())) && (!c.connectionCategory || text(item.category).toLowerCase().includes(c.connectionCategory.toLowerCase())))
  const pageInfo = detail?.events_page
  return <Stack>
    <NavigationTabs id="rimworld-session-mode" activeId={c.sessionMode} onChange={c.setSessionMode} items={[{ id: "runs", label: "Agent 会话" }, { id: "reports", label: "完整报告" }, { id: "connection", label: "游戏会话与连接事件" }]}>
      {c.sessionMode === "reports" ? <ReportsView c={c} /> : c.sessionMode === "connection" ? <Stack>
        <Grid cols={2}>
          <Card title="当前游戏会话">{c.state.session ? <KeyValue items={[
            { key: "colony", label: "殖民地 ID", value: text(c.state.session.colony_id) },
            { key: "load", label: "加载实例", value: text(c.state.session.load_epoch) },
            { key: "tick", label: "游戏 tick", value: text(c.state.session.game_tick) },
            { key: "sample", label: "采样序号", value: text(c.state.session.snapshot_sequence) },
            { key: "stale", label: "会话失效", value: c.state.session.stale ? "是" : "否" },
          ]} /> : <EmptyState title="未加载游戏会话" />}</Card>
          <Card title="连接与协议"><KeyValue items={[
            { key: "endpoint", label: "监听地址", value: text(c.state.endpoint) },
            { key: "mod", label: "Mod / 游戏版本", value: `${text(c.state.connection?.mod_version)} / ${text(c.state.connection?.game_version)}` },
            { key: "epoch", label: "连接实例", value: text(c.state.connection?.connection_epoch) },
            { key: "frames", label: "收 / 发帧", value: `${c.state.protocol?.rx_frames ?? 0} / ${c.state.protocol?.tx_frames ?? 0}` },
            { key: "failures", label: "认证失败 / 无效帧", value: `${c.state.protocol?.auth_failures ?? 0} / ${c.state.protocol?.invalid_frames ?? 0}` },
            { key: "pending", label: "等待响应 / 发送队列", value: `${c.state.metrics?.pending_requests ?? 0} / ${c.state.metrics?.outbound_depth ?? 0}` },
            { key: "last", label: "最后查询", value: `${text(c.state.last_query?.status)} · ${timing(c.state.last_query?.latency_ms)}` },
          ]} /></Card>
        </Grid>
        <Card title="连接诊断事件"><Stack>
          <Grid cols={2}><Field label="级别筛选"><Input value={c.connectionLevel} onChange={c.setConnectionLevel} placeholder="例如 error" /></Field><Field label="类别筛选"><Input value={c.connectionCategory} onChange={c.setConnectionCategory} /></Field></Grid>
          <DataTable data={connectionEvents} rowKey="sequence" maxRows={200} emptyText="没有匹配的连接事件。" columns={[
            { key: "timestamp", label: "时间", render: row => text(row.timestamp) },
            { key: "level", label: "级别", render: row => <StatusBadge tone={levelTone(text(row.level))} label={text(row.level)} /> },
            { key: "category", label: "类别", render: row => text(row.category) },
            { key: "message", label: "说明", render: row => text(row.message, "—", 500) },
          ]} />
        </Stack></Card>
      </Stack> : <Grid cols={2}>
        <Card title="运行记录"><Stack>
          <Button tone={c.followLatest ? "primary" : "default"} onClick={() => { c.setFollowLatest(!c.followLatest) }}>{c.followLatest ? "已跟随最新运行" : "跟随最新运行"}</Button>
          <TextBlock text={`保留最近 ${c.state.agent?.retained_run_limit ?? 20} 次已结束运行及正在运行的会话；插件重载后清空。`} />
          <DataTable data={c.runs} rowKey="run_id" selectedKey={c.selectedRunId} onSelect={row => c.selectRun(row.run_id!)} maxRows={30} emptyText="没有可查看的会话。" columns={[
            { key: "question", label: "问题", render: row => text(row.question, shortId(row.run_id), 130) },
            { key: "state", label: "状态", render: row => <StatusBadge tone={statusTone(text(row.result_status || row.state))} label={text(row.result_status || row.state)} /> },
            { key: "duration", label: "耗时", render: row => timing(row.duration_ms) },
          ]} />
        </Stack></Card>
        <Card title="会话详情"><Stack>
          <Toolbar><ToolbarGroup><TextBlock text={c.archive ? "工作中保存的执行审计" : shortId(c.selectedRunId)} /></ToolbarGroup><ToolbarGroup><Button disabled={(!c.selectedRunId && !c.archive) || c.detailLoading} onClick={c.loadDetail}>{c.detailLoading ? "读取中…" : "刷新详情"}</Button></ToolbarGroup></Toolbar>
          {c.detailError ? <InlineError title="详情刷新暂时失败" error={c.detailError} details="已保留上次成功读取的内容；运行跟踪不会停止，也不会自动切换到其他会话。" /> : null}
          {detail ? <Stack>
            <TextBlock text={text(detail.question, "咨询", 300)} />
            <UsageSummary run={detail} />
            <NavigationTabs id="rimworld-run-view" activeId={c.runView} onChange={id => c.setRunView(id as any)} items={[{ id: "result", label: "结果" }, { id: "events", label: "事件" }, { id: "observations", label: "观察" }, { id: "model", label: "模型请求" }]}>
              {c.runView === "result" ? <RunResult result={detail.result} draft={detail.draft} failureSummary={detail.failure_summary} onOpenReport={c.openReport} /> : c.runView === "observations" ? (
                detail.result?.observations?.length ? <Stack><TextBlock text="每份观察保留各自的目标、采样、完整性与返回数据，不把不同时间的数据伪装为同一快照。" /><JsonView data={detail.result.observations} /></Stack> : <EmptyState title="尚无最终观察集合" description="运行中可在事件页查看已完成的工具结果。" />
              ) : <Stack>
                <Field label="当前事件页内筛选"><Input value={c.eventFilter} onChange={c.setEventFilter} placeholder="例如 failed / model_request" /></Field>
                <DataTable data={visibleEvents} rowKey="event_seq" selectedKey={c.selectedEvent} onSelect={row => c.setSelectedEvent(row.event_seq!)} maxRows={50} emptyText="当前页没有匹配事件。" columns={[
                  { key: "event_seq", label: "#" },
                  { key: "event", label: "事件" },
                  { key: "elapsed_ms", label: "经过时间", render: row => timing(row.elapsed_ms) },
                ]} />
                <Toolbar><ToolbarGroup><Button disabled={c.eventOffset === 0 || c.detailLoading} onClick={() => c.changeEventPage(c.eventOffset - 50)}>上一页</Button><Button disabled={!pageInfo?.has_more || c.detailLoading} onClick={() => c.changeEventPage(c.eventOffset + 50)}>下一页</Button></ToolbarGroup></Toolbar>
                <TextBlock text={`事件 ${pageInfo ? Math.min(pageInfo.offset + 1, pageInfo.total) : 0}–${pageInfo ? Math.min(pageInfo.offset + pageInfo.limit, pageInfo.total) : 0} / ${pageInfo?.total ?? 0}，顺序按审计序号。`} />
                <EventDetails event={event} />
              </Stack>}
            </NavigationTabs>
          </Stack> : c.selectedSummary ? <Stack>
            <TextBlock text={text(c.selectedSummary.question, "咨询", 300)} />
            <UsageSummary run={c.selectedSummary} />
            <StatusBadge tone={statusTone(text(c.selectedSummary.result_status || c.selectedSummary.state))} label={text(c.selectedSummary.result_status || c.selectedSummary.state)} />
            <KeyValue items={[
              { key: "progress", label: "当前进度", value: progressText(c.selectedSummary) },
              { key: "cycle", label: "Agent 轮次", value: text(c.selectedSummary.cycle) },
              { key: "revision", label: "进度版本", value: text(c.selectedSummary.revision, text(c.selectedSummary.event_count)) },
              { key: "event", label: "最新事件", value: text(c.selectedSummary.latest_event?.event) },
            ]} />
            {!c.selectedSummary.terminal ? <TextBlock text="运行中仅刷新轻量进度；结束后会自动读取一次完整详情。也可以手动读取当前详情。" /> : null}
          </Stack> : !c.detailError && !c.detailLoading ? <EmptyState title="选择一条运行记录" /> : null}
        </Stack></Card>
      </Grid>}
    </NavigationTabs>
  </Stack>
}
