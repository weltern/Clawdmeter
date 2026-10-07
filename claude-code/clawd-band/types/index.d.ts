export type Activity = 'coding' | 'reading' | 'searching' | 'planning' | 'integrating' | 'thinking' | 'idle'

/** What Claude is doing in the main conversation right now. */
export type Doing = { act: Activity; detail: string; at: number }

export type Limit = { kind: string; percentUsed: number; resetsAt?: string }

/** The figures the band draws, as Claude Code last measured them. */
export type Usage = {
  limits: Limit[]
  contextPercent?: number
  contextTokens?: number
  contextWindow: number
}

declare module 'claude-code' {
  interface PluginState {
    'clawd-band': { doing: Doing; usage: Usage | null; now: number }
  }
}
