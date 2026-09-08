import { NavigationTabs } from "./components/navigation-tabs"
import { Alert, Button, ErrorBoundary, InlineError, Page, Stack } from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"
import type { DashboardState, PageId } from "./types"
import { usePanelController } from "./controller"
import { StatusStrip } from "./components/status-strip"
import { HomePage } from "./pages/home"
import { SessionsPage } from "./pages/sessions"
import { DebugPage } from "./pages/debug"
import { SettingsPage } from "./pages/settings"
import { text } from "./format"

function PanelBody(props: PluginSurfaceProps<DashboardState>) {
  const c = usePanelController(props)
  return <Page title="N.E.K.O RimWorld" subtitle="游戏观察与 Agent 咨询">
    <Stack>
      <StatusStrip c={c} />
      {c.refreshError ? <InlineError title="状态刷新失败" error={c.refreshError} /> : null}
      {(props.warnings || []).map((warning, index) => <Alert key={index} tone="warning">{text(warning.message, "界面状态警告", 500)}</Alert>)}
      <NavigationTabs id="rimworld-main-nav" activeId={c.page} onChange={id => c.setPage(id as PageId)} items={[
        { id: "home", label: "主页" },
        { id: "sessions", label: "会话与事件" },
        { id: "debug", label: c.running ? "调试 · 运行中" : "调试" },
        { id: "settings", label: c.bridgeDirty || c.agentDirty ? "设置 · 未保存" : "设置" },
      ]}>
        <ErrorBoundary key={c.page} title="页面内容加载失败">
          {c.page === "sessions" ? <SessionsPage c={c} /> : c.page === "debug" ? <DebugPage c={c} /> : c.page === "settings" ? <SettingsPage c={c} /> : <HomePage c={c} />}
        </ErrorBoundary>
      </NavigationTabs>
    </Stack>
  </Page>
}

export default function RimWorldBridgePanel(props: PluginSurfaceProps<DashboardState>) {
  return <ErrorBoundary title="RimWorld 面板加载失败" fallback={(error, reset) => <Page title="N.E.K.O RimWorld"><Stack><InlineError title="界面错误" error={error} /><Button onClick={reset}>重试显示</Button></Stack></Page>}>
    <PanelBody {...props} />
  </ErrorBoundary>
}
