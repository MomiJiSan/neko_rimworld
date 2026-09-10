import { useEffect, useLocalState, useRef, useState, useToast } from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"
import { actionById, actionPayload, actionSummary, budgetError, normalizedError } from "./format"
import { BUDGET_FIELDS } from "./types"
import type { AgentRun, BudgetValues, DashboardState, PageId, RunView } from "./types"

export function usePanelController(props: PluginSurfaceProps<DashboardState>) {
  const state = props.state || {}
  const actions = Array.isArray(props.actions) ? props.actions : []
  const toast = useToast()
  const [page, setPage] = useLocalState<PageId>("rimworld:page", "home")
  const [runView, setRunView] = useLocalState<RunView>("rimworld:run-view", "result")
  const [sessionMode, setSessionMode] = useLocalState<string>("rimworld:session-mode", "runs")
  const [selectedRunId, setSelectedRunId] = useLocalState<string>("rimworld:selected-run", "")
  const [followLatest, setFollowLatest] = useLocalState<boolean>("rimworld:follow-latest", false)
  const [eventOffset, setEventOffset] = useState(0)
  const [eventFilter, setEventFilter] = useState("")
  const [selectedEvent, setSelectedEvent] = useState<number | null>(null)
  const [connectionLevel, setConnectionLevel] = useState("")
  const [connectionCategory, setConnectionCategory] = useState("")
  const [detail, setDetail] = useState<AgentRun | null>(null)
  const [detailError, setDetailError] = useState<Error | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const detailSequence = useRef(0)
  const detailInFlight = useRef<Record<string, boolean>>({})
  const detailPending = useRef<Record<string, boolean>>({})
  const detailTarget = useRef({ runId: selectedRunId, eventOffset })
  detailTarget.current = { runId: selectedRunId, eventOffset }
  const mounted = useRef(true)
  const latestProps = useRef(props)
  latestProps.current = props
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState<Error | null>(null)
  const refreshInFlight = useRef<Promise<void> | null>(null)
  const [reportPage, setReportPage] = useState<Record<string, any> | null>(null)
  const [reportOffset, setReportOffset] = useState(0)
  const [selectedReportId, setSelectedReportId] = useLocalState<string>("rimworld:selected-report", "")
  const [report, setReport] = useState<Record<string, any> | null>(null)
  const [reportError, setReportError] = useState<Error | null>(null)
  const [reportLoading, setReportLoading] = useState(false)
  const reportSequence = useRef(0)
  const [referenceReportId, setReferenceReportId] = useState("")
  const [question, setQuestion] = useState("现在殖民地整体情况怎么样？")
  const [running, setRunning] = useState(false)
  const runningGate = useRef(false)
  const [testResult, setTestResult] = useState<Record<string, any> | null>(null)
  const [testError, setTestError] = useState<Error | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeResult, setProbeResult] = useState<Record<string, any> | null>(null)
  const [probeError, setProbeError] = useState<Error | null>(null)
  const [port, setPort] = useState<number | "">(state.port ?? 48919)
  const [pairingToken, setPairingToken] = useState("")
  const [bridgeDirty, setBridgeDirty] = useState(false)
  const [baseUrl, setBaseUrl] = useState(state.configuration?.agent_base_url || "")
  const [model, setModel] = useState(state.configuration?.agent_model || "")
  const [apiKey, setApiKey] = useState("")
  const [budgets, setBudgets] = useState<BudgetValues>(initialBudgets(state))
  const [agentDirty, setAgentDirty] = useState(false)
  const [savingBridge, setSavingBridge] = useState(false)
  const [savingAgent, setSavingAgent] = useState(false)
  const saveGate = useRef(false)
  const [bridgeError, setBridgeError] = useState<Error | null>(null)
  const [agentError, setAgentError] = useState<Error | null>(null)
  const [bridgeSaved, setBridgeSaved] = useState(false)
  const [agentSaved, setAgentSaved] = useState(false)
  const savedAgentKey = JSON.stringify([state.configuration?.agent_base_url, state.configuration?.agent_model, state.configuration?.agent_budgets])
  useEffect(() => { if (!bridgeDirty) setPort(state.port ?? 48919) }, [state.port])
  useEffect(() => {
    if (!agentDirty) {
      setBaseUrl(state.configuration?.agent_base_url || "")
      setModel(state.configuration?.agent_model || "")
      setBudgets(initialBudgets(state))
    }
  }, [savedAgentKey])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; detailSequence.current++ } }, [])
  const runs = [...(state.agent?.active_runs || []), ...(state.agent?.recent_runs || [])]
  const latestRun = state.agent?.latest_run || null
  const selectedSummary = runs.find(run => run.run_id === selectedRunId)
  useEffect(() => {
    if (latestRun?.run_id && (!selectedRunId || followLatest) && latestRun.run_id !== selectedRunId) {
      setSelectedRunId(latestRun.run_id); setEventOffset(0); setSelectedEvent(null)
    }
  }, [latestRun?.run_id, followLatest, selectedRunId])

  function refresh(): Promise<void> {
    if (refreshInFlight.current) return refreshInFlight.current
    const request = (async () => {
      setRefreshing(true); setRefreshError(null)
      try { await latestProps.current.api.refresh() }
      catch (error) { if (mounted.current) setRefreshError(normalizedError(error)) }
      finally {
        refreshInFlight.current = null
        if (mounted.current) setRefreshing(false)
      }
    })()
    refreshInFlight.current = request
    return request
  }
  async function call(id: string, args: Record<string, any>, timeoutMs = 15_000) {
    const current = latestProps.current
    const action = actionById(current.actions || [], id)
    if (!action) throw new Error("操作暂不可用，请启动插件并刷新面板。")
    return current.api.call(action.entry_id || action.id, args, { timeoutMs, userInitiated: true })
  }
  async function loadDetail() {
    const target = detailTarget.current
    if (!target.runId) return
    if (detailInFlight.current[target.runId]) { detailPending.current[target.runId] = true; return }
    detailInFlight.current[target.runId] = true
    const sequence = ++detailSequence.current
    setDetailLoading(true); setDetailError(null)
    try {
      const result = actionPayload(await call("get_agent_run_details", { run_id: target.runId, event_offset: target.eventOffset, event_limit: 50 }, 12_000))
      if (!mounted.current || sequence !== detailSequence.current) return
      if (result?.run_id !== target.runId) throw new Error("返回的会话与当前选择不匹配。")
      setDetail(result)
    } catch (error) {
      if (mounted.current && sequence === detailSequence.current) setDetailError(normalizedError(error))
    } finally {
      delete detailInFlight.current[target.runId]
      if (mounted.current && sequence === detailSequence.current) setDetailLoading(false)
      if (detailPending.current[target.runId] && mounted.current && detailTarget.current.runId === target.runId) {
        delete detailPending.current[target.runId]
        void loadDetail()
      }
    }
  }
  useEffect(() => {
    if (page === "sessions" && sessionMode === "runs" && selectedRunId && selectedSummary?.terminal) void loadDetail()
    return () => { detailSequence.current++ }
  }, [page, sessionMode, selectedRunId, eventOffset, selectedSummary?.terminal, selectedSummary?.revision, state.action_nonce])
  async function loadReports() {
    const sequence = ++reportSequence.current
    setReportLoading(true); setReportError(null)
    try {
      const listing = actionPayload(await call("rimworld_report", { action: "list", offset: reportOffset, limit: 20 }, 10_000))
      if (!mounted.current || sequence !== reportSequence.current) return
      setReportPage(listing)
      if (selectedReportId) {
        const loaded = actionPayload(await call("rimworld_report", { action: "read", report_id: selectedReportId }, 10_000))
        if (!mounted.current || sequence !== reportSequence.current) return
        if (loaded?.report?.report_id !== selectedReportId) throw new Error("报告 ID 与当前选择不匹配。")
        setReport(loaded.report)
      }
    } catch (error) { if (mounted.current && sequence === reportSequence.current) { setReport(null); setReportError(normalizedError(error)) } }
    finally { if (mounted.current && sequence === reportSequence.current) setReportLoading(false) }
  }
  useEffect(() => {
    if (page === "sessions" && sessionMode === "reports") void loadReports()
    return () => { reportSequence.current++ }
  }, [page, sessionMode, selectedReportId, reportOffset, state.action_nonce])
  function openReport(id: string) {
    reportSequence.current++; setReport(null); setReportError(null)
    setSelectedReportId(id); setSessionMode("reports"); setPage("sessions")
  }
  function askAboutReport(id: string) { setReferenceReportId(id); setQuestion(""); setPage("debug") }
  const active = running || (state.agent?.active_count ?? 0) > 0
  useEffect(() => {
    if (!active) return
    let stopped = false
    let timer: any
    async function tick() {
      await refresh()
      if (!stopped) timer = setTimeout(tick, typeof document !== "undefined" && document.hidden ? 4000 : 1000)
    }
    timer = setTimeout(tick, typeof document !== "undefined" && document.hidden ? 4000 : 1000)
    return () => { stopped = true; clearTimeout(timer) }
  }, [active])

  function selectRun(id: string) {
    detailSequence.current++; setDetail(null); setDetailError(null)
    setSelectedRunId(id); setEventOffset(0); setSelectedEvent(null); setFollowLatest(false)
  }
  function openRun(id: string) {
    selectRun(id); setSessionMode("runs"); setPage("sessions")
    detailTarget.current = { runId: id, eventOffset: 0 }
    setTimeout(() => { void loadDetail() }, 0)
  }
  function changeEventPage(offset: number) { setEventOffset(Math.max(0, offset)); setSelectedEvent(null) }
  function changePort(value: number | "") { setPort(value); setBridgeDirty(true); setBridgeSaved(false) }
  function changeToken(value: string) { setPairingToken(value); setBridgeDirty(true); setBridgeSaved(false) }
  function changeBaseUrl(value: string) { setBaseUrl(value); setAgentDirty(true); setAgentSaved(false) }
  function changeModel(value: string) { setModel(value); setAgentDirty(true); setAgentSaved(false) }
  function changeApiKey(value: string) { setApiKey(value); setAgentDirty(true); setAgentSaved(false) }
  function changeBudget(key: string, value: number | "") { setBudgets(old => ({ ...old, [key]: value })); setAgentDirty(true); setAgentSaved(false) }
  async function saveBridge() {
    if (saveGate.current) return
    setBridgeError(null); setBridgeSaved(false)
    const token = pairingToken.trim()
    if (typeof port !== "number" || !Number.isInteger(port) || port < 1 || port > 65535 || (token && (token.length < 32 || token.length > 128)) || (!token && !state.configuration?.token_configured)) {
      setBridgeError(new Error("端口必须为 1–65535；首次配对需要 32–128 字符的 token。")); return
    }
    saveGate.current = true; setSavingBridge(true)
    try {
      actionSummary(await call("save_bridge_settings", { port, pairing_token: token, ui_nonce: latestProps.current.state?.action_nonce }))
      setPairingToken(""); setBridgeDirty(false); setBridgeSaved(true)
      await refresh()
    } catch (error) {
      setBridgeError(normalizedError(error, token))
      await refresh()
    }
    finally { saveGate.current = false; if (mounted.current) setSavingBridge(false) }
  }
  async function saveAgent() {
    if (saveGate.current) return
    setAgentError(null); setAgentSaved(false)
    const invalid = budgetError(budgets, BUDGET_FIELDS)
    if (!baseUrl.trim() || !model.trim() || invalid) { setAgentError(new Error(invalid || "请填写模型地址和名称。")); return }
    saveGate.current = true; setSavingAgent(true)
    const secret = apiKey.trim()
    try {
      actionSummary(await call("save_agent_settings", { base_url: baseUrl.trim(), model: model.trim(), api_key: secret, budgets, ui_nonce: latestProps.current.state?.action_nonce }))
      setApiKey(""); setAgentDirty(false); setAgentSaved(true)
      await refresh()
    } catch (error) { setAgentError(normalizedError(error, secret)) }
    finally { saveGate.current = false; if (mounted.current) setSavingAgent(false) }
  }
  async function runTest() {
    if (runningGate.current) return
    setTestError(null); setTestResult(null)
    if (!question.trim()) { setTestError(new Error("请输入测试问题。")); return }
    runningGate.current = true; setRunning(true)
    try {
      const result = actionPayload(await call("rimworld_consult", { question: question.trim(), ...(referenceReportId ? { report_id: referenceReportId } : {}) }, 115_000))
      if (mounted.current) {
        setTestResult(result)
        if (result?.status === "ok") toast.success("咨询完成。")
        else toast.warning("咨询未正常完成，可在会话详情中查看原因。")
      }
    } catch (error) { if (mounted.current) setTestError(normalizedError(error)) }
    finally { runningGate.current = false; if (mounted.current) { setRunning(false); await refresh() } }
  }
  async function probe() {
    if (probing) return
    setProbing(true); setProbeError(null); setProbeResult(null)
    try { setProbeResult(actionPayload(await call("probe_game_overview", {}, 8_000))) }
    catch (error) { setProbeError(normalizedError(error)) }
    finally { setProbing(false); await refresh() }
  }
  return {
    reportPage, reportOffset, setReportOffset, selectedReportId, report: report?.report_id === selectedReportId ? report : null, reportError, reportLoading, loadReports, openReport, askAboutReport, referenceReportId, setReferenceReportId,
    state, actions, page, setPage, latestRun, runs, selectedRunId, selectedSummary, selectRun, openRun,
    followLatest, setFollowLatest, sessionMode, setSessionMode, runView, setRunView,
    detail: detail?.run_id === selectedRunId ? detail : null, detailError, detailLoading, loadDetail,
    eventOffset, changeEventPage, eventFilter, setEventFilter, selectedEvent, setSelectedEvent,
    connectionLevel, setConnectionLevel, connectionCategory, setConnectionCategory,
    refreshing, refreshError, refresh, question, setQuestion, running, testResult, testError, runTest,
    probing, probeResult, probeError, probe, port, changePort, pairingToken, changeToken,
    baseUrl, changeBaseUrl, model, changeModel, apiKey, changeApiKey, budgets, changeBudget,
    bridgeDirty, agentDirty, savingBridge, savingAgent, bridgeError, agentError, bridgeSaved, agentSaved, saveBridge, saveAgent,
  }
}

function initialBudgets(state: DashboardState): BudgetValues {
  const values: BudgetValues = {}
  for (const field of BUDGET_FIELDS) values[field.key] = state.configuration?.agent_budgets?.[field.key] ?? field.initial
  return values
}

export type PanelController = ReturnType<typeof usePanelController>
