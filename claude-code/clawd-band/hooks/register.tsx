import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionRateLimit, SessionContextUsage } from 'claude-code'

import type { Activity, Doing, Limit, Usage } from '../types'
import {
  COLORS, HEAT_COLORS, LABELS, OVER_COLOR, activityFor, agoText, barSvg, clawdSvg, detailFor,
  heatFor, tokensText, untilText,
} from './clawd'

const doing = atom({ plugin: 'clawd-band', key: 'doing' } as const, { act: 'idle', detail: '', at: 0 } as Doing)
const usage = atom({ plugin: 'clawd-band', key: 'usage' } as const, null as Usage | null)
const now = atom({ plugin: 'clawd-band', key: 'now' } as const, 0)

const THINKING_DETAIL = 'Thinking it through'
const WINDOWS: { kind: string; title: string; unit: string }[] = [
  { kind: 'five_hour', title: 'SESSION', unit: '5h' },
  { kind: 'seven_day', title: 'WEEKLY', unit: '7d' },
]
const TOAST_AT = 90
const MASCOT_PX = 44
// Fixed so the meters never move when the activity text changes length.
const ACTIVITY_WIDTH = '24%'

// Built once per activity: the sprites never change while the module lives.
const mascots = new Map<Activity, string>()
function mascot(act: Activity): string {
  let svg = mascots.get(act)
  if (svg === undefined) { svg = clawdSvg(act, MASCOT_PX); mascots.set(act, svg) }
  return svg
}

function toUsage(limits: SessionRateLimit[], context: SessionContextUsage): Usage {
  return {
    limits: limits.map(l => ({ kind: l.kind, percentUsed: l.percentUsed, resetsAt: l.resetsAt })),
    contextPercent: context.percent,
    contextTokens: context.tokens,
    contextWindow: context.window,
  }
}

async function setDoing($: EngineInterface, act: Activity, detail: string): Promise<void> {
  const at = await $.clock.now()
  await update($, doing, () => ({ act, detail, at }))
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
    const name = w.kind === 'five_hour' ? 'Session' : 'Weekly'
    const when = l.resetsAt ? ` ${untilText(l.resetsAt, at).replace(/^resets/, 'Resets')}.` : ''
    $.ui.toast(`${name} limit at ${Math.round(l.percentUsed)}%.${when}`)
  }
}

export const register: Register = on => {
  // In-flight tool calls of the main conversation, newest last; a module value,
  // so a reload starts it empty (the next call or turn sets things right).
  const inflight = new Map<string, { act: Activity; detail: string }>()
  let seq = 0

  on('session.start', async ($, e, next) => {
    const first = await $.session.usage()
    await update($, usage, () => toUsage(first.rateLimits, first.context))
    const started = await $.clock.now()
    await update($, now, () => started)
    $.clock.every(30_000, () => { void $.clock.now().then(t => update($, now, () => t)) })
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    await update($, usage, () => toUsage(e.rateLimits, e.context))
    if (e.changed.includes('rateLimits')) await toastNearLimits($, e.rateLimits, await $.clock.now())
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
    const at = (await read($, now)) || (await $.clock.now())
    // The engine knows whether a turn runs; between turns Clawd is idle whatever was last seen.
    const act: Activity = e.props.isWorking ? (d.act === 'idle' ? 'thinking' : d.act) : 'idle'
    const detail = act === 'idle'
      ? (d.at > 0 ? `Waiting for you · ${agoText(d.at, at)}` : 'Waiting for you')
      : (d.act === 'idle' ? THINKING_DETAIL : d.detail)

    const meters = WINDOWS.map(w => {
      const l: Limit | undefined = u?.limits.find(x => x.kind === w.kind)
      return { ...w, pct: l?.percentUsed, sub: l ? untilText(l.resetsAt, at) : 'no reading yet' }
    })
    const ctxPct = u?.contextPercent
    const ctxSub = u && u.contextTokens !== undefined ? `${tokensText(u.contextTokens)} of ${tokensText(u.contextWindow)}` : 'no reading yet'

    if (e.surface === 'desktop') {
      const { Box, Text, Svg } = $.ui.resolve(e)
      const meter = (key: string, title: string, unit: string, pct: number | undefined, sub: string, fill: string | undefined) => {
        const over = pct !== undefined && pct > 100
        return (
          <Box key={key} flexDirection="column" width={0} flexGrow={1} flexShrink={1} minWidth={14}>
            <Box flexDirection="row" justifyContent="space-between">
              <Text dimColor wrap="truncate">
                {title} {unit}{over ? <Text color={OVER_COLOR} bold> OVERAGE</Text> : null}
              </Text>
              <Text bold>{pct === undefined ? '–' : `${Math.round(pct)}%`}</Text>
            </Box>
            <Svg
              source={barSvg(pct ?? 0, fill ?? HEAT_COLORS.cool)}
              alt={pct === undefined ? `${title} ${unit}: no reading yet` : `${title} ${unit}: ${Math.round(pct)}% used`}
              height={6}
            />
            <Text dimColor wrap="truncate">{sub}</Text>
          </Box>
        )
      }
      return (
        <Box flexDirection="row" alignItems="center" gap={2}>
          <Svg
            source={mascot(act)}
            alt={`Clawd, ${LABELS[act].toLowerCase()}`}
            width={MASCOT_PX}
            height={MASCOT_PX}
            isInteractive
          />
          <Box key="activity" flexDirection="column" width={ACTIVITY_WIDTH} flexGrow={0} flexShrink={0}>
            <Text color={COLORS[act]} bold>● {LABELS[act]}</Text>
            <Text dimColor wrap="truncate">{detail}</Text>
          </Box>
          {meters.map(m => meter(m.kind, m.title, m.unit, m.pct, m.sub, m.pct === undefined ? undefined : HEAT_COLORS[heatFor(m.pct)]))}
          {meter('context', 'CONTEXT', '', ctxPct, ctxSub, '#9C9A92')}
        </Box>
      )
    }

    if (e.surface === 'terminal') {
      // No pictures on the terminal: the activity in its colour, then text bars.
      const { Box, Text } = $.ui.resolve(e)
      const cells = 8
      const bar = (pct: number) => {
        const over = pct > 100
        const filled = Math.round((over ? Math.min(pct - 100, 100) : pct) / 100 * cells)
        return { on: '█'.repeat(filled), off: '░'.repeat(cells - filled), color: over ? OVER_COLOR : HEAT_COLORS[heatFor(pct)] }
      }
      return (
        <Box flexDirection="row" gap={2}>
          <Box key="activity" width={30} flexShrink={0} gap={1}>
            <Text color={COLORS[act]} bold wrap="truncate">● {LABELS[act]}</Text>
            <Text dimColor wrap="truncate">{detail}</Text>
          </Box>
          {meters.map(m => {
            if (m.pct === undefined) return <Text dimColor>{m.unit} –</Text>
            const b = bar(m.pct)
            return (
              <Text wrap="truncate">
                <Text dimColor>{m.unit} </Text><Text color={b.color}>{b.on}</Text><Text dimColor>{b.off}</Text>{`${Math.round(m.pct)}%`.padStart(5)}
              </Text>
            )
          })}
          <Text dimColor wrap="truncate">ctx {ctxPct === undefined ? '–' : `${ctxPct}%`}</Text>
        </Box>
      )
    }

    return next(e)
  })
}
