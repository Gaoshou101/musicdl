export type BotDraft = {
  id: string
  username: string
  command_template: string
  priority: string
  timeout: string
}

export type ParsedBotSettings = { priority: number; timeout: number }

export const BLANK_BOT_DRAFT: BotDraft = {
  id: '',
  username: '',
  command_template: '',
  priority: '0',
  timeout: '10',
}

export type BotDraftResult =
  | { ok: true; value: ParsedBotSettings }
  | { ok: false; text: string }

/** Validate the two numeric values shared by Bot creation and editing. */
export function parseBotDraft(draft: BotDraft): BotDraftResult {
  if (!draft.priority.trim()) {
    return { ok: false, text: '优先级必须是 -1000 到 1000 之间的整数' }
  }
  if (!draft.timeout.trim()) {
    return { ok: false, text: '超时必须是 0.1 到 120 秒之间的数字' }
  }
  const priority = Number(draft.priority)
  const timeout = Number(draft.timeout)
  if (!Number.isInteger(priority) || priority < -1000 || priority > 1000) {
    return { ok: false, text: '优先级必须是 -1000 到 1000 之间的整数' }
  }
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 120) {
    return { ok: false, text: '超时必须是 0.1 到 120 秒之间的数字' }
  }
  return { ok: true, value: { priority, timeout } }
}

/** Empty templates deliberately select the backend's built-in `/search` command. */
export function botTemplateForCreate(value: string): string | undefined {
  return value.trim() || undefined
}

/** Empty templates explicitly restore the built-in command when editing. */
export function botTemplateForUpdate(value: string): string | null {
  return value.trim() || null
}
