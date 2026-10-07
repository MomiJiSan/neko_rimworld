import { Alert, Button, Card, Field, Grid, InlineError, Input, NumberInput, PasswordInput, Stack, TextBlock } from "@neko/plugin-ui"
import type { SettingsController } from "./useSettings"
import { BUDGET_FIELDS } from "../types"
import { budgetError } from "../format"

export function SettingsPage({ c }: { c: SettingsController }) {
  const busy = c.savingBridge || c.savingAgent
  const invalidBudget = budgetError(c.budgets, BUDGET_FIELDS)
  return <Stack>
    <Card title="游戏连接"><Stack>
      <Grid cols={2}>
        <Field label="TCP 端口" required><NumberInput value={c.port} min={1} max={65535} step={1} disabled={busy} onChange={value => c.changePort(value === "" ? "" : Number(value))} /></Field>
        <Field label="配对 token" help={c.state.configuration?.token_configured ? "已配置。留空保留原值；保存后清空输入框。" : "粘贴 RimWorld Mod 设置中生成的 token。"}>
          <PasswordInput value={c.pairingToken} disabled={busy} onChange={c.changeToken} />
        </Field>
      </Grid>
      {c.bridgeError ? <InlineError title="连接设置未保存" error={c.bridgeError} /> : null}
      {c.bridgeSaved ? <Alert tone="success">连接设置已保存。</Alert> : null}
      <Button tone="primary" disabled={busy || !c.bridgeDirty} onClick={c.saveBridge}>{c.savingBridge ? "保存中…" : "保存并重启桥接"}</Button>
    </Stack></Card>
    <Card title="模型服务"><Stack>
      <Grid cols={2}>
        <Field label="Base URL" required><Input value={c.baseUrl} disabled={busy} onChange={c.changeBaseUrl} placeholder="https://example.com/v1" /></Field>
        <Field label="模型名称" required><Input value={c.model} disabled={busy} onChange={c.changeModel} /></Field>
      </Grid>
      <Field label="API key" help={c.state.configuration?.agent_api_key_configured ? "已配置。留空保留原值；密钥不会从服务器回填。" : "本地模型可留空；远程服务通常需要凭据。"}>
        <PasswordInput value={c.apiKey} disabled={busy} onChange={c.changeApiKey} />
      </Field>
    </Stack></Card>
    <Card title="运行预算"><Stack>
      <TextBlock text="时间预算单位为秒；输出上限单位为 token，可能包含模型思考用量。默认 32768，需符合供应商支持范围。实际等待仍受请求和运行时间限制。" />
      <TextBlock text="瞬时模型故障仅在尚无有效输出时自动重试，最多尝试 3 次；每次尝试都计入运行时间与 token 统计。" />
      <Grid cols={2}>{BUDGET_FIELDS.map(field => <Field key={field.key} label={field.label} help={`${field.min}–${field.max} ${field.key === "max_output_tokens" ? "token（整数）" : "秒"}`} required>
        <NumberInput value={c.budgets[field.key]} min={field.min} max={field.max} step={1} disabled={busy} onChange={value => c.changeBudget(field.key, value === "" ? "" : Number(value))} />
      </Field>)}</Grid>
      {invalidBudget ? <Alert tone="warning">{invalidBudget}</Alert> : null}
      {c.agentError ? <InlineError title="模型设置未保存" error={c.agentError} /> : null}
      {c.agentSaved ? <Alert tone="success">模型与预算设置已保存，对下一次咨询生效。</Alert> : null}
      <Button tone="primary" disabled={busy || !c.agentDirty || !!invalidBudget || !c.baseUrl.trim() || !c.model.trim()} onClick={c.saveAgent}>{c.savingAgent ? "保存中…" : "保存模型与运行预算"}</Button>
    </Stack></Card>
    <TextBlock text="切页保留当前草稿；页面关闭后不会保留密钥输入。刷新服务状态不会覆盖未保存的编辑。" />
  </Stack>
}
