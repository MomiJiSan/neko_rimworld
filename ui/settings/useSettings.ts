import { useEffect, useRef, useState } from "@neko/plugin-ui"
import type { PanelServices } from "../services"
import { actionSummary, budgetError, normalizedError } from "../format"
import { BUDGET_FIELDS } from "../types"
import type { BudgetValues, DashboardState } from "../types"

export function useSettings(services: PanelServices) {
  const { state, mounted, call, refresh } = services
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
      actionSummary(await call("save_bridge_settings", { port, pairing_token: token, ui_nonce: state.action_nonce }))
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
      actionSummary(await call("save_agent_settings", { base_url: baseUrl.trim(), model: model.trim(), api_key: secret, budgets, ui_nonce: state.action_nonce }))
      setApiKey(""); setAgentDirty(false); setAgentSaved(true)
      await refresh()
    } catch (error) { setAgentError(normalizedError(error, secret)) }
    finally { saveGate.current = false; if (mounted.current) setSavingAgent(false) }
  }
  return { state, port, changePort, pairingToken, changeToken, baseUrl, changeBaseUrl,
    model, changeModel, apiKey, changeApiKey, budgets, changeBudget, bridgeDirty, agentDirty,
    savingBridge, savingAgent, bridgeError, agentError, bridgeSaved, agentSaved, saveBridge, saveAgent }
}
function initialBudgets(state: DashboardState): BudgetValues {
  const values: BudgetValues = {}
  for (const field of BUDGET_FIELDS) values[field.key] = state.configuration?.agent_budgets?.[field.key] ?? field.initial
  return values
}

export type SettingsController = ReturnType<typeof useSettings>
