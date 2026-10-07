// Pure helpers for the band: which activity a tool is, the line beside Clawd,
// Clawd's animated SVG, the bar SVGs and the time and token wording.
// Mappings and colours follow Clawdmeter (src/transcript.py, session_shelf.py).

import type { Activity, BandSettings, ScopedReading } from '../types'
import { SPRITES, type Frame } from './sprites'

export const LABELS: Record<Activity, string> = {
  coding: 'CODING',
  reading: 'READING',
  searching: 'SEARCHING',
  planning: 'PLANNING',
  integrating: 'INTEGRATING',
  thinking: 'THINKING',
  idle: 'IDLE',
}

/** Clawdmeter's ACTIVITY_COLORS: one hue per activity on every theme. */
export const COLORS: Record<Activity, string> = {
  coding: '#CE7D6B',
  reading: '#5FB3A1',
  searching: '#8B7DD8',
  planning: '#E0A458',
  integrating: '#C77DBB',
  thinking: '#5B8DEF',
  idle: '#8A8984',
}

/** Clawdmeter's ACTIVITY_ANIMS, played in turn; idle uses its idle group. */
const ANIMS: Record<Activity, string[]> = {
  coding: ['work_coding'],
  reading: ['work_think'],
  searching: ['idle_look_around'],
  planning: ['idle_blink', 'idle_look_around'],
  integrating: ['expression_surprise', 'idle_look_around'],
  thinking: ['work_think'],
  idle: ['idle_breathe'],
}

/** Clawdmeter's TOOL_MAP; MCP tools integrate and unknown tools count as coding. */
const TOOL_MAP: Record<string, Activity> = {
  bash: 'coding', powershell: 'coding', bashoutput: 'coding', edit: 'coding', write: 'coding',
  multiedit: 'coding', notebookedit: 'coding', killshell: 'coding', senduserfile: 'coding',
  read: 'reading', glob: 'reading', grep: 'reading', notebookread: 'reading', toolsearch: 'reading',
  webfetch: 'searching', websearch: 'searching',
  taskcreate: 'planning', taskupdate: 'planning', tasklist: 'planning', taskget: 'planning',
  taskoutput: 'planning', taskstop: 'planning', todowrite: 'planning', agent: 'planning', task: 'planning',
  sendmessage: 'planning', exitplanmode: 'planning', askuserquestion: 'planning', skill: 'planning',
  slashcommand: 'planning',
}

export function activityFor(tool: string): Activity {
  const lower = tool.toLowerCase()
  if (lower.startsWith('mcp__')) return 'integrating'
  return TOOL_MAP[lower] ?? 'coding'
}

/** `mcp__unifi-network__list_clients` -> `unifi-network/list_clients`, as Clawdmeter labels it. */
export function prettyTool(tool: string): string {
  if (!tool.toLowerCase().startsWith('mcp__')) return tool
  const parts = tool.split('__')
  if (parts.length >= 3) return `${parts[1]}/${parts.slice(2).join('__')}`
  return parts[1] || tool
}

function clip(text: string, max = 48): string {
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > max ? `${one.slice(0, max - 1)}…` : one
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/)
  return parts[parts.length - 1] || path
}

/** The line under the activity label: the tool and what it is working on. */
export function detailFor(tool: string, input: Record<string, unknown>): string {
  const name = prettyTool(tool)
  if (tool.toLowerCase().startsWith('mcp__')) return name
  const text = (key: string) => (typeof input[key] === 'string' ? (input[key] as string) : undefined)
  const path = text('file_path') ?? text('notebook_path')
  if (path) return `${name} · ${baseName(path)}`
  const url = text('url')
  if (url) {
    const host = /^[a-z]+:\/\/([^/?#]+)/i.exec(url)?.[1]
    return `${name} · ${host ?? clip(url, 32)}`
  }
  const what = text('description') ?? text('query') ?? text('pattern') ?? text('skill') ?? text('command') ?? text('subject')
  return what ? `${name} · ${clip(what, 40)}` : name
}

/** `2h 20m`, `41m`, `4d 06h`, the way Clawdmeter words a reset. */
export function untilText(resetsAt: string | undefined, now: number): string {
  if (!resetsAt) return 'no reset time yet'
  const ms = Date.parse(resetsAt) - now
  if (!Number.isFinite(ms)) return 'no reset time yet'
  if (ms <= 0) return 'resetting now'
  const mins = Math.ceil(ms / 60000)
  if (mins < 60) return `resets in ${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `resets in ${hours}h ${String(mins % 60).padStart(2, '0')}m`
  return `resets in ${Math.floor(hours / 24)}d ${String(hours % 24).padStart(2, '0')}h`
}

/** `4m`, `2h`, `just now`: how long ago the last turn ended. */
export function agoText(at: number, now: number): string {
  const mins = Math.floor((now - at) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`
}

export function tokensText(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`
  if (n >= 1000) return `${Math.round(n / 1000)}K`
  return String(n)
}

/**
 * Clawdmeter's heat steps for one window (uiutil.heat): warm from the warn
 * point, hot halfway from there to 100 (75 -> 87), overage past 100%.
 */
export type Heat = 'cool' | 'warm' | 'hot' | 'over'
export function heatFor(pct: number, warnAt = 75): Heat {
  const warn = Math.max(0, Math.min(Math.round(warnAt), 100))
  const hotAt = warn + Math.max(1, Math.floor((100 - warn) / 2))
  if (pct > 100) return 'over'
  if (pct >= hotAt) return 'hot'
  if (pct >= warn) return 'warm'
  return 'cool'
}

/** `$4.12`, `$0.38`, `$128`: what this conversation has cost so far. */
export function costText(usd: number): string {
  return usd >= 100 ? `$${Math.round(usd)}` : `$${usd.toFixed(2)}`
}

/** The desktop app's clay, Clawdmeter's warm step, the app's red and a deep overage red. */
export const HEAT_COLORS: Record<Heat, string> = { cool: '#D97757', warm: '#B85C42', hot: '#E06666', over: '#C4222C' }
export const OVER_COLOR = HEAT_COLORS.over
const TRACK = 'rgba(140,138,131,0.28)'

/**
 * Wider than any band: drawn with no `width`, an Svg takes its markup's own
 * width up to its slot, so the bar fills the meter's column edge to edge and
 * ends under the percentage. `preserveAspectRatio="none"` lets it stretch.
 */
export const BAR_INTRINSIC_WIDTH = 4000

/** One usage bar. Past 100% it shows only the part over, as Clawdmeter's does. */
export function barSvg(pct: number, fill: string, height = 6, width = BAR_INTRINSIC_WIDTH): string {
  const shown = pct > 100 ? Math.min(pct - 100, 100) : Math.max(0, Math.min(pct, 100))
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 100 ${height}" preserveAspectRatio="none">` +
    `<rect width="100" height="${height}" fill="${TRACK}"/>` +
    `<rect width="${shown}" height="${height}" fill="${fill}"/></svg>`
}

type Box = { x: number; y: number; side: number }

/** The square around every lit cell of the frames, as Clawdmeter crops a sprite. */
function cropOf(frames: Frame[]): Box {
  let x0 = 20, y0 = 20, x1 = -1, y1 = -1
  for (const f of frames) {
    for (let i = 0; i < 400; i++) {
      if (f.px[i] === '.') continue
      const x = i % 20, y = Math.floor(i / 20)
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y)
    }
  }
  if (x1 < 0) return { x: 0, y: 0, side: 20 }
  const side = Math.max(x1 - x0, y1 - y0) + 1
  const clamp = (v: number) => Math.max(0, Math.min(20 - side, v))
  return { x: clamp(Math.round((x0 + x1 + 1 - side) / 2)), y: clamp(Math.round((y0 + y1 + 1 - side) / 2)), side }
}

/** One frame as one path per colour: each row's runs of a colour become a 1-high box. */
function framePaths(px: string, box: Box): string {
  const runs = new Map<string, string>()
  for (let y = 0; y < box.side; y++) {
    let x = 0
    while (x < box.side) {
      const c = px[(y + box.y) * 20 + x + box.x] ?? '.'
      let end = x + 1
      while (end < box.side && px[(y + box.y) * 20 + end + box.x] === c) end++
      if (c !== '.') runs.set(c, (runs.get(c) ?? '') + `M${x} ${y}h${end - x}v1h-${end - x}z`)
      x = end
    }
  }
  let out = ''
  for (const [c, d] of runs) out += `<path fill="${SPRITES.palette[c.charCodeAt(0) - 65]}" d="${d}"/>`
  return out
}

const fmt = (n: number) => String(Math.round(n * 10000) / 10000)

/**
 * Clawd for one activity as a self-contained animated SVG: each distinct frame
 * drawn once, and a timeline of `use`s switched on and off with SMIL at the
 * manifest's hold times. Needs `isInteractive` on the Svg to animate.
 */
export function clawdSvg(act: Activity, size: number, glowAmount = 50): string {
  const frames = ANIMS[act].flatMap(slug => SPRITES.anims[slug] ?? [])
  const box = cropOf(frames)
  const ids = new Map<string, number>()
  let defs = ''
  for (const f of frames) {
    if (ids.has(f.px)) continue
    ids.set(f.px, ids.size)
    defs += `<g id="f${ids.size - 1}">${framePaths(f.px, box)}</g>`
  }
  const total = frames.reduce((sum, f) => sum + f.ms, 0)
  let t = 0
  let uses = ''
  frames.forEach((f, i) => {
    const start = t / total, end = (t + f.ms) / total
    t += f.ms
    const id = ids.get(f.px)
    if (frames.length === 1) { uses += `<use href="#f${id}"/>`; return }
    const values = i === 0 ? 'visible;hidden' : i === frames.length - 1 ? 'hidden;visible' : 'hidden;visible;hidden'
    const times = i === 0 ? `0;${fmt(end)}` : i === frames.length - 1 ? `0;${fmt(start)}` : `0;${fmt(start)};${fmt(end)}`
    uses += `<use href="#f${id}" visibility="${i === 0 ? 'visible' : 'hidden'}"><animate attributeName="visibility" calcMode="discrete" values="${values}" keyTimes="${times}" dur="${total}ms" repeatCount="indefinite"/></use>`
  })
  const pad = 2
  // Glow 0-100: none at 0, a softer and fainter halo below 50, a wider and stronger one above.
  const blur = 0.3 + (Math.max(0, Math.min(100, glowAmount)) / 100) * 1.5
  const strength = 0.5 + Math.max(0, Math.min(100, glowAmount)) / 200
  const glow = act === 'idle' || glowAmount <= 0 ? '' :
    `<filter id="g" x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="0" stdDeviation="${fmt(blur)}" flood-color="${COLORS[act]}" flood-opacity="${fmt(strength)}"/></filter>`
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="${-pad} ${-pad} ${box.side + pad * 2} ${box.side + pad * 2}" shape-rendering="crispEdges">` +
    // The desktop draws an interactive Svg in its own frame; a frame whose colour
    // scheme differs from the app's gets an opaque white backdrop, so follow the app's.
    `<style>:root{color-scheme:light dark;background:transparent}</style>` +
    `<defs>${glow}${defs}</defs><g${glow ? ' filter="url(#g)"' : ''}>${uses}</g></svg>`
}

export const DEFAULT_SETTINGS: BandSettings = {
  meters: { session: true, weekly: true, context: true, cost: false, fable: false },
  layout: 'full',
  detail: 'full',
  clawd: 'medium',
  glow: true,
  glowAmount: 50,
  warnAt: 75,
  notify: true,
}

/** Whatever the store holds, as valid settings: unknown or bad fields fall back to the defaults. */
export function normalizeSettings(raw: unknown): BandSettings {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const m = (r.meters && typeof r.meters === 'object' ? r.meters : {}) as Record<string, unknown>
  const pick = <T extends string>(v: unknown, allowed: readonly T[], fallback: T): T =>
    typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : fallback
  const bool = (v: unknown, fallback: boolean) => (typeof v === 'boolean' ? v : fallback)
  const meters = {
    session: bool(m.session, DEFAULT_SETTINGS.meters.session),
    weekly: bool(m.weekly, DEFAULT_SETTINGS.meters.weekly),
    context: bool(m.context, DEFAULT_SETTINGS.meters.context),
    cost: bool(m.cost, DEFAULT_SETTINGS.meters.cost),
    fable: bool(m.fable, DEFAULT_SETTINGS.meters.fable),
  }
  if (!Object.values(meters).some(Boolean)) meters.session = true
  const warn = typeof r.warnAt === 'number' && Number.isInteger(r.warnAt) && r.warnAt >= 1 && r.warnAt <= 99 ? r.warnAt : DEFAULT_SETTINGS.warnAt
  return {
    meters,
    layout: pick(r.layout, ['full', 'slim'] as const, DEFAULT_SETTINGS.layout),
    detail: pick(r.detail, ['full', 'tool', 'none'] as const, DEFAULT_SETTINGS.detail),
    clawd: pick(r.clawd, ['small', 'medium', 'large', 'hidden'] as const, DEFAULT_SETTINGS.clawd),
    glow: bool(r.glow, DEFAULT_SETTINGS.glow),
    glowAmount: typeof r.glowAmount === 'number' && Number.isInteger(r.glowAmount) && r.glowAmount >= 0 && r.glowAmount <= 100 ? r.glowAmount : DEFAULT_SETTINGS.glowAmount,
    warnAt: warn,
    notify: bool(r.notify, DEFAULT_SETTINGS.notify),
  }
}

/**
 * The Fable limit out of the usage page's `limits[]`, read the way Clawdmeter's
 * scoped_windows.py reads it: the scope's model (or surface) display name, the
 * entry's `percent` (not clamped at 100), its `group` and `resets_at`. Null when
 * no scoped entry names Fable, which is what an account without that limit sees.
 */
export function fableFromUsage(body: unknown): ScopedReading | null {
  const limits = body && typeof body === 'object' ? (body as { limits?: unknown }).limits : undefined
  if (!Array.isArray(limits)) return null
  for (const entry of limits) {
    if (!entry || typeof entry !== 'object') continue
    const e = entry as Record<string, unknown>
    const scope = (e.scope && typeof e.scope === 'object' ? e.scope : {}) as Record<string, unknown>
    let name: string | undefined
    for (const part of ['model', 'surface']) {
      let v = scope[part]
      if (v && typeof v === 'object') v = (v as Record<string, unknown>).display_name
      if (typeof v === 'string' && v.trim()) { name = v.trim(); break }
    }
    const pct = e.percent
    if (!name || !/fable/i.test(name) || typeof pct !== 'number' || !Number.isFinite(pct)) continue
    return {
      name,
      group: typeof e.group === 'string' ? e.group.trim().toLowerCase() : '',
      percent: pct,
      resetsAt: typeof e.resets_at === 'string' ? e.resets_at : undefined,
    }
  }
  return null
}

/**
 * A scoped limit's title and unit, matching the band's SESSION 5h / WEEKLY 7d:
 * `FABLE` + `7d` for a weekly window (Nick's call over Clawdmeter's longer
 * `WEEKLY · FABLE`, which truncated with five meters on).
 */
export function scopedTitle(r: ScopedReading): { title: string; unit: string } {
  const unit = r.group === 'weekly' ? '7d' : r.group === 'session' ? '5h' : r.group
  return { title: r.name.toUpperCase(), unit }
}
