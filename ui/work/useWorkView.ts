import { useEffect, useLocalState, useRef, useState } from "@neko/plugin-ui"
import type { PanelServices } from "../services"
import type { PageId } from "../types"
import type { EvidenceView, ExchangeDetails, WorkDetails, WorkList } from "./types"
import { actionPayload, normalizedError } from "../format"

export function useWorkView(services: PanelServices, page: PageId) {
  const { state, call, mounted } = services
  const [selectedWork, setSelectedWork] = useLocalState<string>("rimworld:selected-work", "")
  const [selectedTurn, setSelectedTurn] = useState("")
  const [workOffset, setWorkOffset] = useState(0)
  const [turnOffset, setTurnOffset] = useState(0)
  const [list, setList] = useState<WorkList | null>(null)
  const [work, setWork] = useState<WorkDetails | null>(null)
  const [exchange, setExchange] = useState<ExchangeDetails | null>(null)
  const [evidence, setEvidence] = useState<EvidenceView | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(false)
  const generation = useRef(0)
  const evidenceGeneration = useRef(0)
  const target = useRef({ selectedWork, selectedTurn })
  target.current = { selectedWork, selectedTurn }

  async function load() {
    const seq = ++generation.current
    setLoading(true); setError(null)
    try {
      const rows = actionPayload(await call("workspace_view", { action: "list", offset: workOffset, limit: 20 })) as WorkList
      if (!mounted.current || seq !== generation.current) return
      setList(rows)
      const id = selectedWork || rows.workspaces?.[0]?.workspace_id
      if (!id) { setWork(null); setExchange(null); return }
      if (!selectedWork) { setSelectedWork(id); return }
      const history = actionPayload(await call("workspace_view", { action: "work", workspace_id: id, offset: turnOffset, limit: 20 })) as WorkDetails
      if (!mounted.current || seq !== generation.current) return
      if (history.workspace_id !== id) throw new Error("返回的工作与当前选择不匹配。")
      setWork(history)
      const turn = selectedTurn || history.turns[0]?.turn_id
      if (!turn) { setExchange(null); return }
      if (!selectedTurn) { setSelectedTurn(turn); return }
      const result = actionPayload(await call("workspace_view", { action: "turn", workspace_id: id, turn_id: turn })) as ExchangeDetails
      if (!mounted.current || seq !== generation.current) return
      if (result.workspace_id !== id || result.turn_id !== turn) throw new Error("返回的交流记录与当前选择不匹配。")
      setExchange(result)
    } catch (failure) { if (mounted.current && seq === generation.current) setError(normalizedError(failure)) }
    finally { if (mounted.current && seq === generation.current) setLoading(false) }
  }
  useEffect(() => {
    if (page === "work") void load()
    return () => { generation.current++; evidenceGeneration.current++ }
  }, [page, selectedWork, selectedTurn, workOffset, turnOffset, state.action_nonce, state.workspaces?.revision])

  function selectWork(id: string) {
    generation.current++; evidenceGeneration.current++
    setSelectedWork(id); setSelectedTurn(""); setTurnOffset(0)
    setWork(null); setExchange(null); setEvidence(null); setError(null)
  }
  function selectTurn(id: string) {
    generation.current++; evidenceGeneration.current++
    setSelectedTurn(id); setExchange(null); setEvidence(null); setError(null)
  }
  async function readEvidence(id: string) {
    const seq = ++evidenceGeneration.current
    const current = target.current
    setEvidence(null); setError(null)
    try {
      const result = actionPayload(await call("workspace_view", {
        action: "observation", workspace_id: current.selectedWork, turn_id: current.selectedTurn, observation_id: id,
      })) as EvidenceView
      if (!mounted.current || seq !== evidenceGeneration.current) return
      if (result.workspace_id !== current.selectedWork || result.turn_id !== current.selectedTurn || result.observation_id !== id) throw new Error("证据与当前选择不匹配。")
      setEvidence(result)
    } catch (failure) { if (mounted.current && seq === evidenceGeneration.current) setError(normalizedError(failure)) }
  }
  return { list, work: work?.workspace_id === selectedWork ? work : null,
    exchange: exchange?.workspace_id === selectedWork && exchange.turn_id === selectedTurn ? exchange : null,
    evidence, selectedWork, selectedTurn, selectWork, selectTurn, workOffset, setWorkOffset,
    turnOffset, setTurnOffset, loading, error, load, readEvidence }
}
export type WorkController = ReturnType<typeof useWorkView>
