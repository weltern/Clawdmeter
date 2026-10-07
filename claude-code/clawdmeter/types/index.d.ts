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
  costUsd?: number
}

export type MeterKey = 'session' | 'weekly' | 'context' | 'cost' | 'fable'

/** The person's choices from the settings panel, kept in $.store across sessions. */
export type BandSettings = {
  meters: Record<MeterKey, boolean>
  layout: 'full' | 'slim'
  detail: 'full' | 'tool' | 'none'
  clawd: 'small' | 'medium' | 'large' | 'hidden'
  glow: boolean
  /** 0-100, how wide and strong the glow is when `glow` is on. */
  glowAmount: number
  warnAt: number
  notify: boolean
}

/** A model-scoped limit from the usage page (`limits[]`), e.g. Weekly · Fable. */
export type ScopedReading = { name: string; group: string; percent: number; resetsAt?: string }

/**
 * The Fable meter's last check: the last good reading is kept through a failed
 * check (Clawdmeter's ScopedWindowTracker does the same), with what went wrong.
 */
export type FableState = { reading?: ScopedReading; problem?: string; at: number }

declare module 'claude-code' {
  interface PluginState {
    'clawdmeter': { doing: Doing; usage: Usage | null; now: number; settings: BandSettings; fable: FableState | null }
  }
}
