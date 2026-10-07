import { Alert, Button, Card, DataTable, EmptyState, Grid, InlineError, JsonView, Markdown, Stack, StatusBadge, TextBlock, Toolbar, ToolbarGroup } from "@neko/plugin-ui"
import type { WorkController } from "./useWorkView"
import type { DashboardState } from "../types"
import { shortId, text, progressText } from "../format"

function exchangeLabel(status: string, active: boolean) {
  if (status === "ok") return "调查已完成"
  if (status === "degraded") return "调查未完整完成"
  if (status === "failed") return "执行失败"
  if (status === "cancelled") return "已取消"
  return active ? "正在处理" : "记录尚未结清"
}

export function WorkPage({ c, state, openAudit }: { c: WorkController; state: DashboardState; openAudit: (workspace: string, turn: string) => void }) {
  const work = c.work, exchange = c.exchange
  const result = exchange?.result
  const body = result?.answer || exchange?.report?.full_text
  const status = result?.status || exchange?.status || "unknown"
  const active = exchange?.active?.turn_id === exchange?.turn_id
  return <Stack>
    <Alert tone="info">{state.session && !state.session.stale ? "游戏已连接，可在 N.E.K.O 对话中继续调查。" : "游戏当前离线，已保存的工作和证据仍可查看。"} 此面板只查看持续工作；独立咨询测试位于诊断页。</Alert>
    {c.error ? <InlineError title="工作读取失败" error={c.error} details="已保留同一工作上次读取的内容；查看不会重跑调查。" /> : null}
    <Grid cols={2}>
      <Card title="查看的工作"><Stack>
        <Button disabled={c.loading} onClick={c.load}>{c.loading ? "读取中…" : "刷新工作"}</Button>
        <DataTable data={c.list?.workspaces || []} rowKey="workspace_id" selectedKey={c.selectedWork} onSelect={row => c.selectWork(row.workspace_id)} maxRows={20} emptyText="尚无持续工作。通过 N.E.K.O 对话咨询后，记录会保存在这里。" columns={[
          { key: "owner", label: "角色", render: row => <StatusBadge tone="default" label={<span style={{ whiteSpace: "nowrap" }}>{row.owner}</span>} /> },
          { key: "colony", label: "殖民地", render: row => `${shortId(row.colony_id)}${row.current_colony ? " · 当前游戏" : ""}` },
          { key: "last_question", label: "最近讨论", render: row => text(row.last_question, "尚无往来", 90) },
          { key: "turn_count", label: "往来" },
        ]} />
        <Toolbar><ToolbarGroup><Button disabled={c.workOffset === 0 || c.loading} onClick={() => c.setWorkOffset(Math.max(0, c.workOffset - 20))}>上一页工作</Button><Button disabled={c.list?.next_offset == null || c.loading} onClick={() => c.setWorkOffset(c.list!.next_offset!)}>下一页工作</Button></ToolbarGroup></Toolbar>
        <TextBlock text={`共 ${c.list?.total ?? 0} 个工作；历史保存在数据库中，插件重启后仍可读取。`} />
      </Stack></Card>
      <Card title="调查往来"><Stack>
        {work ? <Stack>
          <TextBlock text={`${work.workspace.owner} · ${shortId(work.workspace.colony_id)}`} />
          {work.active ? <Alert tone="info">{work.active.run_id ? progressText(work.active) : "正在准备或保存这次交流。"}</Alert> : null}
          <DataTable data={work.turns} rowKey="turn_id" selectedKey={c.selectedTurn} onSelect={row => c.selectTurn(row.turn_id)} maxRows={20} emptyText="此工作尚无交流记录。" columns={[
            { key: "question", label: "插件收到的问题", render: row => text(row.question, "", 160) },
            { key: "status", label: "状态", render: row => <StatusBadge tone={row.status === "ok" ? "success" : "warning"} label={<span style={{ whiteSpace: "nowrap" }}>{exchangeLabel(row.status, work.active?.turn_id === row.turn_id)}</span>} /> },
            { key: "created_at", label: "时间", render: row => text(row.created_at, "未记录") },
          ]} />
          <Toolbar><ToolbarGroup><Button disabled={c.turnOffset === 0 || c.loading} onClick={() => c.setTurnOffset(Math.max(0, c.turnOffset - 20))}>上一页往来</Button><Button disabled={work.next_offset == null || c.loading} onClick={() => c.setTurnOffset(work.next_offset!)}>下一页往来</Button></ToolbarGroup></Toolbar>
        </Stack> : <EmptyState title="选择一个工作" />}
      </Stack></Card>
    </Grid>
    {work?.checkpoint ? <Card title="工作笔记"><Stack>
      <Alert tone="info">这是未经验证的工作交接笔记，属于历史资料。</Alert>
      <TextBlock text={work.checkpoint.data.work_note} />
      {work.checkpoint.data.open_questions?.length ? <TextBlock text={"尚待解决：\n" + work.checkpoint.data.open_questions.join("\n")} /> : null}
    </Stack></Card> : work ? <TextBlock text="尚无工作笔记，可从调查往来了解此前进展。" /> : null}
    {exchange ? <Card title="这次调查的结果"><Stack>
      <TextBlock text={exchange.question} />
      <StatusBadge tone={status === "ok" ? "success" : "warning"} label={exchangeLabel(status, !!active)} />
      {!exchange.history_saved ? <Alert tone="warning">{active ? "本轮仍在处理，结果尚未写入历史。" : "历史保存尚未确认。已取得的内容如可用仍会展示，后续调用会尝试恢复保存。"}</Alert> : <TextBlock text="历史已保存。以下结果与证据属于这次调查，不代表当前游戏状态。" />}
      {exchange.report ? <TextBlock text={exchange.report.state === "complete" ? "完整报告已保存。" : "已保存草稿，不能当作正式交付。"} /> : exchange.report_unavailable ? <Alert tone="warning">独立报告暂时无法读取，工作中的原结果仍可读取。</Alert> : null}
      {body ? <Markdown source={body} /> : <EmptyState title={active ? "调查正在进行" : "没有保存的回答"} />}
      {result?.caveats?.length ? <TextBlock text={result.caveats.join("\n")} /> : null}
      {exchange.handoff ? <Card title="准备返回宿主的摘要"><Stack><TextBlock text={exchange.handoff.summary} /><TextBlock text="这是插件准备返回的文本，不是 N.E.K.O 已消费该结果的回执。" /></Stack></Card> : result?.agent_text ? <Card title="执行内核生成的摘要"><TextBlock text={result.agent_text} /><TextBlock text="这份历史记录未保存入口最终返回的摘要。" /></Card> : null}
      <Toolbar><ToolbarGroup>{exchange.audit_available ? <Button onClick={() => openAudit(exchange.workspace_id, exchange.turn_id)}>查看保存的执行诊断</Button> : null}{exchange.active?.run_id ? <TextBlock text="当前执行详情可在诊断页查看。" /> : null}</ToolbarGroup></Toolbar>
      <TextBlock text={`原始证据 ${exchange.evidence.length} 份；按需读取，不重新查询游戏。`} />
      {exchange.evidence.map(source => <Button key={source.observation_id} onClick={() => c.readEvidence(source.observation_id)}>{`读取证据 ${source.observation_id}`}</Button>)}
      {c.evidence ? <Stack><TextBlock text={`历史原始观察 · SHA256 ${c.evidence.sha256}`} /><JsonView data={c.evidence.observation} /></Stack> : null}
    </Stack></Card> : null}
  </Stack>
}
