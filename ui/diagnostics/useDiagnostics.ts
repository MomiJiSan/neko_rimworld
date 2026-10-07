import { useEffect, useLocalState, useRef, useState, useToast } from "@neko/plugin-ui"
import type { AgentRun, PageId, RunView } from "../types"
import type { PanelServices } from "../services"
import { actionPayload, normalizedError } from "../format"

export function useDiagnostics(services: PanelServices, page: PageId, setPage: (page: PageId) => void) {
  const { state, mounted, refresh, refreshing, refreshError, call } = services
  const toast = useToast()
  const [diagnosticMode, setDiagnosticMode] = useLocalState<string>("rimworld:diagnostic-mode", "records")
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
  const [archive, setArchive] = useState<{ workspace_id: string; turn_id: string } | null>(null)
  const [detailError, setDetailError] = useState<Error | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const detailSequence = useRef(0)
  const detailInFlight = useRef<Record<string, boolean>>({})
  const detailPending = useRef<Record<string, boolean>>({})
  const key = archive ? `${archive.workspace_id}:${archive.turn_id}` : selectedRunId
  const detailTarget = useRef({ runId: selectedRunId, eventOffset, archive, key })
  detailTarget.current = { runId: selectedRunId, eventOffset, archive, key }
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
  const runs = [...(state.agent?.active_runs || []), ...(state.agent?.recent_runs || [])]
  const latestRun = state.agent?.latest_run || null
  const selectedSummary = runs.find(run => run.run_id === selectedRunId)
  useEffect(() => {
    if (archive) { if (followLatest) setArchive(null); return }
    if (latestRun?.run_id && (!selectedRunId || followLatest) && latestRun.run_id !== selectedRunId) {
      setSelectedRunId(latestRun.run_id); setEventOffset(0); setSelectedEvent(null)
    }
  }, [latestRun?.run_id, followLatest, selectedRunId, archive])

  async function loadDetail() {
    const target = detailTarget.current
    if (!target.key) return
    if (detailInFlight.current[target.key]) { detailPending.current[target.key] = true; return }
    detailInFlight.current[target.key] = true
    const sequence = ++detailSequence.current
    setDetailLoading(true); setDetailError(null)
    try {
      const result = actionPayload(await (target.archive
        ? call("workspace_view", { action: "audit", ...target.archive, offset: target.eventOffset, limit: 50 }, 12_000)
        : call("get_agent_run_details", { run_id: target.runId, event_offset: target.eventOffset, event_limit: 50 }, 12_000)))
      if (!mounted.current || sequence !== detailSequence.current) return
      if (target.archive ? result?.workspace_id !== target.archive.workspace_id || result?.turn_id !== target.archive.turn_id : result?.run_id !== target.runId) throw new Error("返回的会话与当前选择不匹配。")
      setDetail(result)
    } catch (error) {
      if (mounted.current && sequence === detailSequence.current) setDetailError(normalizedError(error))
    } finally {
      delete detailInFlight.current[target.key]
      if (mounted.current && sequence === detailSequence.current) setDetailLoading(false)
      if (detailPending.current[target.key] && mounted.current && detailTarget.current.key === target.key) {
        delete detailPending.current[target.key]
        void loadDetail()
      }
    }
  }
  useEffect(() => {
    if (page === "diagnostics" && diagnosticMode === "records" && sessionMode === "runs" && (archive || selectedRunId && selectedSummary?.terminal)) void loadDetail()
    return () => { detailSequence.current++ }
  }, [page, diagnosticMode, sessionMode, selectedRunId, archive, eventOffset, selectedSummary?.terminal, selectedSummary?.revision, state.action_nonce])
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
    if (page === "diagnostics" && diagnosticMode === "records" && sessionMode === "reports") void loadReports()
    return () => { reportSequence.current++ }
  }, [page, diagnosticMode, sessionMode, selectedReportId, reportOffset, state.action_nonce])
  function openReport(id: string) {
    reportSequence.current++; setReport(null); setReportError(null)
    setSelectedReportId(id); setSessionMode("reports"); setDiagnosticMode("records"); setPage("diagnostics")
  }
  function askAboutReport(id: string) { setReferenceReportId(id); setQuestion(""); setDiagnosticMode("test"); setPage("diagnostics") }
  function selectRun(id: string) {
    setArchive(null)
    detailSequence.current++; setDetail(null); setDetailError(null)
    setSelectedRunId(id); setEventOffset(0); setSelectedEvent(null); setFollowLatest(false)
  }
  function openRun(id: string) {
    selectRun(id); setSessionMode("runs"); setDiagnosticMode("records"); setPage("diagnostics")
    detailTarget.current = { runId: id, eventOffset: 0, archive: null, key: id }
    setTimeout(() => { void loadDetail() }, 0)
  }
  function openArchive(workspace_id: string, turn_id: string) {
    detailSequence.current++; setDetail(null); setDetailError(null); setFollowLatest(false)
    setArchive({ workspace_id, turn_id }); setEventOffset(0); setSelectedEvent(null)
    setSessionMode("runs"); setDiagnosticMode("records"); setPage("diagnostics")
  }
  function changeEventPage(offset: number) { setEventOffset(Math.max(0, offset)); setSelectedEvent(null) }
  async function runTest() {
    if (runningGate.current) return
    setTestError(null); setTestResult(null)
    if (!question.trim()) { setTestError(new Error("请输入测试问题。")); return }
    runningGate.current = true; setRunning(true)
    try {
      const request = referenceReportId ? `${question.trim()}\n请分析保存的历史报告 ${referenceReportId}。` : question.trim()
      const result = actionPayload(await call("rimworld_consult", { question: request }, 115_000))
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
    state, page, setPage, latestRun, runs, selectedRunId, selectedSummary, selectRun, openRun,
    followLatest, setFollowLatest, sessionMode, setSessionMode, runView, setRunView,
    detail: archive ? (detail?.workspace_id === archive.workspace_id && detail.turn_id === archive.turn_id ? detail : null) : (detail?.run_id === selectedRunId ? detail : null), detailError, detailLoading, loadDetail,
    eventOffset, changeEventPage, eventFilter, setEventFilter, selectedEvent, setSelectedEvent,
    connectionLevel, setConnectionLevel, connectionCategory, setConnectionCategory,
    refreshing, refreshError, refresh, question, setQuestion, running, testResult, testError, runTest,
    probing, probeResult, probeError, probe,
    diagnosticMode, setDiagnosticMode, openArchive, archive,
  }
}
export type DiagnosticsController = ReturnType<typeof useDiagnostics>
