import { Button, StatusBadge, Toolbar, ToolbarGroup } from "@neko/plugin-ui"
import { statusTone, text } from "../format"
import type { PanelServices } from "../services"

export function StatusStrip({ c }: { c: PanelServices & { running: boolean } }) {
  const status = text(c.state.status, "unavailable")
  return <Toolbar>
    <ToolbarGroup>
      <StatusBadge tone={statusTone(status)} label={`连接 · ${status}`} />
      <StatusBadge tone={c.state.session && !c.state.session.stale ? "success" : "warning"} label={c.state.session ? c.state.session.stale ? "游戏会话已失效" : "游戏会话有效" : "未加载游戏会话"} />
      <StatusBadge tone={(c.state.agent?.active_count ?? 0) > 0 || c.running ? "info" : "default"} label={(c.state.agent?.active_count ?? 0) > 0 || c.running ? "Agent 运行中" : "Agent 空闲"} />
    </ToolbarGroup>
    <ToolbarGroup><Button disabled={c.refreshing} onClick={c.refresh}>{c.refreshing ? "刷新中…" : "刷新状态"}</Button></ToolbarGroup>
  </Toolbar>
}
