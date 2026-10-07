import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionCost, SessionRateLimit, SessionContextUsage } from 'claude-code'

import type { Activity, Doing, Limit, MeterKey, BandSettings, Usage } from '../types'
import {
  COLORS, HEAT_COLORS, LABELS, OVER_COLOR, activityFor, agoText, barSvg, clawdSvg, costText, detailFor,
  DEFAULT_SETTINGS, heatFor, normalizeSettings, tokensText, untilText,
} from './clawd'

const doing = atom({ plugin: 'clawdmeter', key: 'doing' } as const, { act: 'idle', detail: '', at: 0 } as Doing)
const usage = atom({ plugin: 'clawdmeter', key: 'usage' } as const, null as Usage | null)
const now = atom({ plugin: 'clawdmeter', key: 'now' } as const, 0)
const settings = atom({ plugin: 'clawdmeter', key: 'settings' } as const, DEFAULT_SETTINGS)

const SETTINGS_KEY = 'settings'
const PANE = 'clawdmeter-settings'
const THINKING_DETAIL = 'Thinking it through'
const WINDOWS: { kind: string; key: MeterKey; title: string; unit: string; name: string }[] = [
  { kind: 'five_hour', key: 'session', title: 'SESSION', unit: '5h', name: 'Session' },
  { kind: 'seven_day', key: 'weekly', title: 'WEEKLY', unit: '7d', name: 'Weekly' },
]
const TOAST_AT = 90
const CLAWD_PX: Record<BandSettings['clawd'], number> = { small: 32, medium: 44, large: 56, hidden: 0 }
const SLIM_CLAWD_PX = 24
const SLIM_BAR_PX = 48
// Fixed so the meters never move when the activity text changes length.
const ACTIVITY_WIDTH = '24%'
const CONTEXT_FILL = '#9C9A92'
// The settings panel: the desktop app's clay as the accent (checkboxes, Done), and a
// see-through grey (works on dark and light) for an unticked box and Reset.
const ACCENT = '#D97757'
const BOX_OFF = 'rgba(128,128,128,0.22)'
// The segmented control: a faint shared track, and the accent on each segment that is on.
const SEG_TRACK = 'rgba(128,128,128,0.14)'
const SEG_ON = ACCENT
const LABEL_CELLS = 16
// Glow presets: one segmented row, Off replacing the old checkbox. The stored
// glowAmount (0-100) reads back as its nearest preset.
const GLOW_PRESETS: [Exclude<GlowLevel, 'off'>, number][] = [['soft', 25], ['medium', 50], ['strong', 85]]
type GlowLevel = 'off' | 'soft' | 'medium' | 'strong'
function glowLevel(s: BandSettings): GlowLevel {
  if (!s.glow || s.glowAmount <= 0) return 'off'
  let best: GlowLevel = 'medium'
  let gap = Infinity
  for (const [level, amount] of GLOW_PRESETS) {
    if (Math.abs(amount - s.glowAmount) < gap) { gap = Math.abs(amount - s.glowAmount); best = level }
  }
  return best
}
// The Turn amber at stepper: 50-95% in 5% steps; a stored value off the grid (73) steps to the
// grid point on that side (75 up, 70 down).
const WARN_MIN = 50
const WARN_MAX = 95
const WARN_STEP = 5
function warnStep(value: number, dir: 1 | -1): number {
  const next = dir > 0 ? Math.floor(value / WARN_STEP) * WARN_STEP + WARN_STEP : Math.ceil(value / WARN_STEP) * WARN_STEP - WARN_STEP
  return Math.max(WARN_MIN, Math.min(WARN_MAX, next))
}

// Built once per activity, size and glow: the sprites never change while the module lives.
const mascots = new Map<string, string>()
function mascot(act: Activity, size: number, glow: number): string {
  const key = `${act}:${size}:${glow}`
  let svg = mascots.get(key)
  if (svg === undefined) { svg = clawdSvg(act, size, glow); mascots.set(key, svg) }
  return svg
}


/** One place that changes settings: the band redraws from the atom, the store keeps them for next time. */
async function changeSettings($: EngineInterface, fn: (s: BandSettings) => BandSettings): Promise<void> {
  await update($, settings, s => normalizeSettings(fn(s)))
  await $.store.set(SETTINGS_KEY, await read($, settings))
}

function toUsage(limits: SessionRateLimit[], context: SessionContextUsage, cost?: SessionCost): Usage {
  return {
    limits: limits.map(l => ({ kind: l.kind, percentUsed: l.percentUsed, resetsAt: l.resetsAt })),
    contextPercent: context.percent,
    contextTokens: context.tokens,
    contextWindow: context.window,
    costUsd: cost?.usd,
  }
}

async function setDoing($: EngineInterface, act: Activity, detail: string): Promise<void> {
  const at = await $.clock.now()
  await update($, doing, () => ({ act, detail, at }))
}

async function openSettings($: EngineInterface): Promise<void> {
  await $.ui.open({ id: PANE, title: 'Clawdmeter', focus: true })
}

/**
 * One toast per window per reset, the first time it passes 90%, remembered
 * across sessions. One store key per window, holding the reset it last toasted
 * for, so the store never grows past two keys.
 */
async function toastNearLimits($: EngineInterface, limits: SessionRateLimit[], at: number): Promise<void> {
  for (const w of WINDOWS) {
    const l = limits.find(x => x.kind === w.kind)
    if (!l || l.percentUsed < TOAST_AT || l.percentUsed > 100) continue
    const key = `toasted:${w.kind}`
    const reset = l.resetsAt ?? 'unknown'
    if ((await $.store.get(key)) === reset) continue
    await $.store.set(key, reset)
    const when = l.resetsAt ? ` ${untilText(l.resetsAt, at).replace(/^resets/, 'Resets')}.` : ''
    $.ui.toast(`${w.name} limit at ${Math.round(l.percentUsed)}%.${when}`)
  }
}

type MeterView = { key: string; title: string; unit: string; pct?: number; text?: string; sub: string; fill?: string; slimName: string }

export const register: Register = on => {
  // In-flight tool calls of the main conversation, newest last; a module value,
  // so a reload starts it empty (the next call or turn sets things right).
  const inflight = new Map<string, { act: Activity; detail: string }>()
  let seq = 0

  on('session.start', async ($, e, next) => {
    const saved = await $.store.get(SETTINGS_KEY)
    await update($, settings, () => normalizeSettings(saved))
    const first = await $.session.usage()
    await update($, usage, () => toUsage(first.rateLimits, first.context, first.cost))
    const started = await $.clock.now()
    await update($, now, () => started)
    $.clock.every(30_000, () => { void $.clock.now().then(t => update($, now, () => t)) })
    await $.command.register({ name: 'clawdmeter', description: 'Open the Clawdmeter settings' })
    return next(e)
  })

  on('command.run', { command: 'clawdmeter' }, async $ => {
    await openSettings($)
    return { text: 'Opened the Clawdmeter settings.' }
  })

  on('session.measure', async ($, e, next) => {
    await update($, usage, () => toUsage(e.rateLimits, e.context, e.cost))
    if (e.changed.includes('rateLimits') && (await read($, settings)).notify) {
      await toastNearLimits($, e.rateLimits, await $.clock.now())
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    inflight.clear()
    await setDoing($, 'thinking', THINKING_DETAIL)
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    const tool = String(e.tool)
    const id = e.tool_use_id ?? `call-${++seq}`
    const entry = { act: activityFor(tool), detail: detailFor(tool, e as unknown as Record<string, unknown>) }
    inflight.set(id, entry)
    await setDoing($, entry.act, entry.detail)
    try {
      return await next(e)
    } finally {
      inflight.delete(id)
      const still = [...inflight.values()].pop()
      if (still) await setDoing($, still.act, still.detail)
      else await setDoing($, 'thinking', THINKING_DETAIL)
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) {
      inflight.clear()
      await setDoing($, 'idle', '')
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const d = await read($, doing)
    const u = await read($, usage)
    const s = await read($, settings)
    const at = (await read($, now)) || (await $.clock.now())
    // The engine knows whether a turn runs; between turns Clawd is idle whatever was last seen.
    const act: Activity = e.props.isWorking ? (d.act === 'idle' ? 'thinking' : d.act) : 'idle'
    const fullDetail = act === 'idle'
      ? (d.at > 0 ? `Waiting for you · ${agoText(d.at, at)}` : 'Waiting for you')
      : (d.act === 'idle' ? THINKING_DETAIL : d.detail)
    const detail = s.detail === 'none' ? '' : s.detail === 'tool' && act !== 'idle' ? (fullDetail.split(' · ')[0] ?? '') : fullDetail

    const meters: MeterView[] = []
    for (const w of WINDOWS) {
      if (!s.meters[w.key]) continue
      const l: Limit | undefined = u?.limits.find(x => x.kind === w.kind)
      meters.push({
        key: w.kind, title: w.title, unit: w.unit, slimName: w.unit, pct: l?.percentUsed,
        sub: l ? untilText(l.resetsAt, at) : 'no reading yet',
        fill: l === undefined ? undefined : HEAT_COLORS[heatFor(l.percentUsed, s.warnAt)],
      })
    }
    if (s.meters.context) {
      meters.push({
        key: 'context', title: 'CONTEXT', unit: '', slimName: 'ctx', pct: u?.contextPercent,
        sub: u && u.contextTokens !== undefined ? `${tokensText(u.contextTokens)} of ${tokensText(u.contextWindow)}` : 'no reading yet',
        fill: CONTEXT_FILL,
      })
    }
    if (s.meters.cost) {
      meters.push({
        key: 'cost', title: 'COST', unit: '', slimName: 'cost',
        text: u?.costUsd === undefined ? '–' : costText(u.costUsd), sub: 'this conversation',
      })
    }

    if (e.surface === 'desktop') {
      const { Box, Text, Svg, Button } = $.ui.resolve(e)
      const gear = (
        <Box key="gear-box" alignSelf="flex-end">
          <Button key="gear" label="⚙" plain dimColor onPress={() => { void openSettings($) }} />
        </Box>
      )

      if (s.layout === 'slim') {
        return (
          <Box flexDirection="row" alignItems="center" gap={2}>
            {s.clawd === 'hidden' ? null : (
              <Svg source={mascot(act, SLIM_CLAWD_PX, s.glow ? s.glowAmount : 0)} alt={`Clawd, ${LABELS[act].toLowerCase()}`} width={SLIM_CLAWD_PX} height={SLIM_CLAWD_PX} isInteractive />
            )}
            <Box key="activity" flexDirection="row" gap={1} width={ACTIVITY_WIDTH} flexGrow={0} flexShrink={0}>
              <Text color={COLORS[act]} bold>● {LABELS[act]}</Text>
              {detail ? <Text dimColor wrap="truncate">{detail}</Text> : null}
            </Box>
            {meters.map(m => (
              <Box key={m.key} flexDirection="row" alignItems="center" gap={1}>
                <Text dimColor>{m.slimName}</Text>
                {m.text !== undefined ? null : (
                  <Svg source={barSvg(m.pct ?? 0, m.pct !== undefined && m.pct > 100 ? OVER_COLOR : (m.fill ?? HEAT_COLORS.cool), 5, SLIM_BAR_PX)}
                    alt={m.pct === undefined ? `${m.title}: no reading yet` : `${m.title}: ${Math.round(m.pct)}% used`} width={SLIM_BAR_PX} height={5} />
                )}
                <Text bold>{m.text ?? (m.pct === undefined ? '–' : `${Math.round(m.pct)}%`)}</Text>
              </Box>
            ))}
            <Box key="spacer" flexGrow={1} />
            {gear}
          </Box>
        )
      }

      const meter = (m: MeterView) => {
        const over = m.pct !== undefined && m.pct > 100
        return (
          <Box key={m.key} flexDirection="column" width={0} flexGrow={1} flexShrink={1} minWidth={14}>
            <Box flexDirection="row" justifyContent="space-between">
              <Text dimColor wrap="truncate">
                {m.title}{m.unit ? ` ${m.unit}` : ''}{over ? <Text color={OVER_COLOR} bold> OVERAGE</Text> : null}
              </Text>
              <Text bold>{m.text ?? (m.pct === undefined ? '–' : `${Math.round(m.pct)}%`)}</Text>
            </Box>
            {m.text !== undefined ? null : (
              <Svg
                source={barSvg(m.pct ?? 0, over ? OVER_COLOR : (m.fill ?? HEAT_COLORS.cool))}
                alt={m.pct === undefined ? `${m.title} ${m.unit}: no reading yet` : `${m.title} ${m.unit}: ${Math.round(m.pct)}% used`}
                height={6}
              />
            )}
            <Text dimColor wrap="truncate">{m.sub}</Text>
          </Box>
        )
      }
      const size = CLAWD_PX[s.clawd]
      return (
        <Box flexDirection="row" alignItems="center" gap={2}>
          {size === 0 ? null : (
            <Svg source={mascot(act, size, s.glow ? s.glowAmount : 0)} alt={`Clawd, ${LABELS[act].toLowerCase()}`} width={size} height={size} isInteractive />
          )}
          <Box key="activity" flexDirection="column" width={ACTIVITY_WIDTH} flexGrow={0} flexShrink={0}>
            <Text color={COLORS[act]} bold>● {LABELS[act]}</Text>
            {detail ? <Text dimColor wrap="truncate">{detail}</Text> : null}
          </Box>
          {meters.map(meter)}
          {gear}
        </Box>
      )
    }

    if (e.surface === 'terminal') {
      // No pictures on the terminal: the activity in its colour, then text bars.
      const { Box, Text, Button } = $.ui.resolve(e)
      const cells = 8
      const bar = (pct: number, fill: string) => {
        const over = pct > 100
        const filled = Math.round((over ? Math.min(pct - 100, 100) : Math.max(0, Math.min(pct, 100))) / 100 * cells)
        return { on: '█'.repeat(filled), off: '░'.repeat(cells - filled), color: over ? OVER_COLOR : fill }
      }
      return (
        <Box flexDirection="row" gap={2}>
          <Box key="activity" width={30} flexShrink={0} gap={1}>
            <Text color={COLORS[act]} bold wrap="truncate">● {LABELS[act]}</Text>
            {detail ? <Text dimColor wrap="truncate">{detail}</Text> : null}
          </Box>
          {meters.map(m => {
            if (m.text !== undefined) return <Text dimColor>{m.slimName} <Text bold>{m.text}</Text></Text>
            if (m.pct === undefined) return <Text dimColor>{m.slimName} –</Text>
            const b = bar(m.pct, m.fill ?? HEAT_COLORS.cool)
            return (
              <Text wrap="truncate">
                <Text dimColor>{m.slimName} </Text><Text color={b.color}>{b.on}</Text><Text dimColor>{b.off}</Text>{`${Math.round(m.pct)}%`.padStart(5)}
              </Text>
            )
          })}
          <Button key="gear" label="⚙" plain dimColor onPress={() => { void openSettings($) }} />
        </Box>
      )
    }

    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text, Button } = elements
    const s = await read($, settings)
    // The band already shows the change; only keeping it for next time can fail, and that is said out loud.
    const change = (fn: (x: BandSettings) => BandSettings) => {
      changeSettings($, fn).catch(() => $.ui.toast("Couldn't save the Clawdmeter settings; they last until Claude Code restarts."))
    }
    // Laid out like Clawdmeter's own settings page: small uppercase section
    // headers with a muted line of help, sub-options indented under the option
    // they belong to, one column.
    const header = (key: string, title: string, help: string, first = false) => (
      <Box key={`head-${key}`} flexDirection="column" marginTop={first ? 0 : 1}>
        <Text dimColor bold>{title}</Text>
        <Text dimColor>{help}</Text>
      </Box>
    )
    // A checkbox: a filled square (accent with a tick when on, a dim square when
    // off) beside a plain Button carrying the label, which is what you click.
    const checkbox = (key: string, label: string, isOn: boolean, press: () => void, indent = 0) => (
      <Box key={`row-${key}`} flexDirection="row" alignItems="center" gap={1} paddingLeft={indent}>
        <Box key={`box-${key}`} width={2} backgroundColor={isOn ? ACCENT : BOX_OFF} justifyContent="center">
          <Text bold>{isOn ? '✓' : ' '}</Text>
        </Box>
        <Button key={key} label={label} plain onPress={press} />
      </Box>
    )
    const labelled = (key: string, label: string, control: unknown, indent = 0) => (
      <Box key={`field-${key}`} flexDirection="row" alignItems="center" gap={2} paddingLeft={indent}>
        <Box width={LABEL_CELLS}><Text>{label}</Text></Box>
        {control as never}
      </Box>
    )
    // A segmented control: one connected row on a shared track that hugs its
    // segments. An on segment is filled with the accent at full-strength text; a
    // thin divider separates two neighbouring off segments, as in a native one.
    // Multi-select (What to show) and single-select (the rest) share the drawing.
    const segmented = (key: string, items: { key: string; label: string; isOn: boolean; press: () => void }[]) => (
      <Box key={`segs-${key}`} flexDirection="row" alignItems="center" alignSelf="flex-start" backgroundColor={SEG_TRACK}>
        {items.map((it, i) => {
          const prev = items[i - 1]
          const divider = prev !== undefined && !prev.isOn && !it.isOn
          return [
            divider ? <Box key={`div-${it.key}`}><Text dimColor>│</Text></Box> : null,
            <Box key={`seg-${it.key}`} backgroundColor={it.isOn ? SEG_ON : undefined} paddingX={1}>
              <Button key={it.key} label={it.label} plain dimColor={!it.isOn} onPress={it.press} />
            </Box>,
          ]
        })}
      </Box>
    )
    const single = <K extends string>(key: string, value: K, options: [K, string][], pick: (v: K) => void) =>
      segmented(key, options.map(([v, label]) => ({ key: `${key}-${v}`, label, isOn: v === value, press: () => pick(v) })))
    // A stepper: [ − ] value [ + ], native-looking chips either side; at an end the
    // button on that side dims and does nothing.
    const stepper = (key: string, value: string, canDown: boolean, canUp: boolean, down: () => void, up: () => void) => (
      <Box key={`step-${key}`} flexDirection="row" alignItems="center" gap={1}>
        <Box key={`chip-${key}-down`} backgroundColor={BOX_OFF} paddingX={1}>
          <Button key={`${key}-down`} label="−" plain dimColor={!canDown} onPress={() => { if (canDown) down() }} />
        </Box>
        <Box key={`val-${key}`} width={5} justifyContent="center"><Text bold>{value}</Text></Box>
        <Box key={`chip-${key}-up`} backgroundColor={BOX_OFF} paddingX={1}>
          <Button key={`${key}-up`} label="+" plain dimColor={!canUp} onPress={() => { if (canUp) up() }} />
        </Box>
      </Box>
    )
    const meterNames: [MeterKey, string][] = [['session', 'Session 5h'], ['weekly', 'Weekly 7d'], ['context', 'Context'], ['cost', 'Cost']]

    return (
      <Box flexDirection="column" gap={1} paddingX={2} paddingY={1} width={76}>
        {header('show', 'WHAT TO SHOW', 'Pick the meters the band shows. Any mix, at least one.', true)}
        {/* normalizeSettings keeps at least one meter on, so the last one cannot go */}
        {segmented('meters', meterNames.map(([k, name]) => ({
          key: k, label: name, isOn: s.meters[k],
          press: () => change(x => ({ ...x, meters: { ...x.meters, [k]: !x.meters[k] } })),
        })))}

        {header('look', 'HOW IT LOOKS', 'Changes show on the band straight away.')}
        {labelled('layout', 'Layout', single('layout', s.layout, [['full', 'Full'], ['slim', 'Slim']],
          v => change(x => ({ ...x, layout: v }))))}
        {labelled('detail', 'Activity line', single('detail', s.detail, [['full', 'Tool and target'], ['tool', 'Tool only'], ['none', 'Hidden']],
          v => change(x => ({ ...x, detail: v }))))}
        {labelled('clawd', 'Clawd', single('clawd', s.clawd, [['small', 'Small'], ['medium', 'Medium'], ['large', 'Large'], ['hidden', 'Hidden']],
          v => change(x => ({ ...x, clawd: v }))))}
        {labelled('glow', 'Glow', single('glow', glowLevel(s), [['off', 'Off'], ['soft', 'Soft'], ['medium', 'Medium'], ['strong', 'Strong']],
          v => change(x => (v === 'off' ? { ...x, glow: false } : { ...x, glow: true, glowAmount: GLOW_PRESETS.find(([l]) => l === v)?.[1] ?? 50 }))))}

        {header('alerts', 'ALERTS', 'When the bars change colour, and when you are told.')}
        {labelled('warn', 'Turn amber at', stepper('warn', `${s.warnAt}%`, s.warnAt > WARN_MIN, s.warnAt < WARN_MAX,
          () => change(x => ({ ...x, warnAt: warnStep(x.warnAt, -1) })), () => change(x => ({ ...x, warnAt: warnStep(x.warnAt, 1) }))))}
        {checkbox('notify', 'Notify when a limit passes 90%', s.notify, () => change(x => ({ ...x, notify: !x.notify })), LABEL_CELLS + 2)}

        <Box flexDirection="row" gap={1} justifyContent="flex-end" marginTop={1}>
          <Box key="chip-reset" backgroundColor={BOX_OFF} paddingX={1}>
            <Button key="reset" label="Reset to defaults" plain onPress={() => change(() => DEFAULT_SETTINGS)} />
          </Box>
          <Box key="chip-done" backgroundColor={ACCENT} paddingX={1}>
            <Button key="done" label="Done" plain onPress={() => { void $.ui.close({ id: PANE }) }} />
          </Box>
        </Box>
      </Box>
    )
  })
}
