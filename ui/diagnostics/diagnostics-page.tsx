import { Stack } from "@neko/plugin-ui"
import { NavigationTabs } from "../components/navigation-tabs"
import { RunHistoryView } from "./run-history"
import { ExecutionOverview } from "./overview"
import { IndependentTestPage } from "./test-page"
import type { DiagnosticsController } from "./useDiagnostics"

export function DiagnosticsPage({ c }: { c: DiagnosticsController }) {
  return <Stack>
    <NavigationTabs id="rimworld-diagnostics-nav" activeId={c.diagnosticMode} onChange={c.setDiagnosticMode} items={[{ id: "records", label: "执行记录" }, { id: "test", label: "独立测试" }]}>
      {c.diagnosticMode === "test" ? <IndependentTestPage c={c} /> : <Stack><ExecutionOverview c={c} /><RunHistoryView c={c} /></Stack>}
    </NavigationTabs>
  </Stack>
}
