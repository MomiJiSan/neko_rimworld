import { NavigationTabs } from "./components/navigation-tabs"
import { Alert, Button, ErrorBoundary, InlineError, Page, Stack, useEffect, useLocalState } from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"
import type { DashboardState, PageId } from "./types"
import { usePanelServices } from "./services"
import { useDiagnostics } from "./diagnostics/useDiagnostics"
import { useSettings } from "./settings/useSettings"
import { useWorkView } from "./work/useWorkView"
import { StatusStrip } from "./components/status-strip"
import { WorkPage } from "./work/work-page"
import { DiagnosticsPage } from "./diagnostics/diagnostics-page"
import { SettingsPage } from "./settings/settings-page"
import { text } from "./format"

function PanelBody(props: PluginSurfaceProps<DashboardState>) {
  const services = usePanelServices(props)
  const [page, setPage] = useLocalState<PageId>("rimworld:v3:page", "work")
  const diagnostics = useDiagnostics(services, page, setPage)
  const settings = useSettings(services)
  const work = useWorkView(services, page)
  const active = diagnostics.running || (services.state.agent?.active_count ?? 0) > 0 || (services.state.consultations_in_progress ?? 0) > 0
  useEffect(() => {
    if (!active) return
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function tick() {
      await services.refresh()
      if (!stopped) timer = setTimeout(tick, typeof document !== "undefined" && document.hidden ? 4000 : 1000)
    }
    timer = setTimeout(tick, 1000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [active])
  return <Page title="N.E.K.O RimWorld" subtitle="持续调查、历史成果与原始证据">
    <Stack>
      <StatusStrip c={{ ...services, running: active }} />
      {services.refreshError ? <InlineError title="状态刷新失败" error={services.refreshError} /> : null}
      {(props.warnings || []).map((warning, index) => <Alert key={index} tone="warning">{text(warning.message, "界面状态警告", 500)}</Alert>)}
      <NavigationTabs id="rimworld-main-nav" activeId={page} onChange={id => setPage(id as PageId)} items={[
        { id: "work", label: "工作" },
        { id: "diagnostics", label: "诊断" },
        { id: "settings", label: settings.bridgeDirty || settings.agentDirty ? "设置 · 未保存" : "设置" },
      ]}>
        <ErrorBoundary key={page} title="页面内容加载失败">
          {page === "diagnostics" ? <DiagnosticsPage c={diagnostics} /> : page === "settings" ? <SettingsPage c={settings} /> : <WorkPage c={work} state={services.state} openAudit={diagnostics.openArchive} />}
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
