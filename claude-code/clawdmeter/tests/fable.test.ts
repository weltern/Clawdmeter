import { describe, expect, mock, test } from 'claude-code/testing'

import { fableFromUsage, scopedTitle } from '../hooks/clawd'

const site = { maxRows: 6, bodyColumns: 96, scroll: { offset: 0, bodyRows: 6 }, view: {} }
const working = { component: 'AbovePrompt', props: { ...site, hasSurvey: false, isWorking: true } } as const
const PANE = { component: 'Pane', requestId: 'clawdmeter-settings', props: {} as never } as const

// The shape Clawdmeter reads from /api/oauth/usage (limits[]), verified live 2026-09-14.
const usageBody = (percent: number) => ({
  five_hour: { utilization: 20 },
  limits: [
    { group: 'session', percent: 20, resets_at: '2030-01-01T02:00:00Z', scope: null },
    { group: 'weekly', percent, resets_at: '2030-01-02T04:00:00Z', scope: { model: { display_name: 'Fable' } }, severity: 'normal', is_active: true },
  ],
})

describe('reading the Fable limit', () => {
  test('finds the Fable entry the way Clawdmeter does, and nothing else', async () => {
    const r = fableFromUsage(usageBody(17))
    expect(r).toEqual({ name: 'Fable', group: 'weekly', percent: 17, resetsAt: '2030-01-02T04:00:00Z' })
    expect(r && scopedTitle(r)).toEqual({ title: 'FABLE', unit: '7d' })
    expect(fableFromUsage({ limits: [{ group: 'weekly', percent: 40, scope: { model: { display_name: 'Opus' } } }] })).toBeNull()
    expect(fableFromUsage({ limits: [{ percent: 'x', scope: { model: { display_name: 'Fable' } } }] })).toBeNull()
    expect(fableFromUsage('nope')).toBeNull()
    expect(fableFromUsage({})).toBeNull()
  })
})

describe('the Fable meter', () => {
  test('asks nothing while off; once on, checks with the session sign-in and draws FABLE 7d', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    const calls: { url: string; init?: unknown }[] = []
    let percent = 17
    let status = 200
    on('session.authorize', () => ({ value: { handle: 'session-handle', kind: 'bearer' as const } }))
    on('http.fetch', (_$, e) => {
      calls.push({ url: e.url, init: e.init })
      return { value: { status, ok: status === 200, headers: {}, text: JSON.stringify(usageBody(percent)) } }
    })
    await clock.set(1_800_000_000_000)
    const band = async () => {
      const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
      const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' | ')
      await ui.unmount()
      return texts
    }

    expect(await band()).not.toContain('FABLE')
    expect(calls).toHaveLength(0)

    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    await pane.press({ key: 'fable' })
    expect(calls).toHaveLength(1)
    expect(calls[0]?.url).toBe('https://api.anthropic.com/api/oauth/usage')
    expect(calls[0]?.init).toMatchObject({ auth: 'session-handle', headers: { 'anthropic-beta': 'oauth-2025-04-20' } })
    let texts = await band()
    expect(texts).toContain('FABLE 7d')
    expect(texts).not.toContain('WEEKLY · FABLE')
    expect(texts).toContain('17%')

    // with every meter on, the band shows them in the panel's order, and the gear keeps its slot
    await pane.press({ key: 'cost' })
    const panelOrder = (await pane.findAll({ type: 'Box' })).map(b => b.key).filter(k => k?.startsWith('seg-')).map(k => k?.slice(4))
    expect(panelOrder.slice(0, 5)).toEqual(['session', 'weekly', 'fable', 'context', 'cost'])
    const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
    const bandOrder = (await ui.findAll({ type: 'Box' })).map(b => b.key).filter(k => ['five_hour', 'seven_day', 'fable', 'context', 'cost'].includes(k ?? ''))
    expect(bandOrder).toEqual(['five_hour', 'seven_day', 'fable', 'context', 'cost'])
    expect((await ui.find({ key: 'gear-box' }))?.props).toMatchObject({ flexShrink: 0, alignSelf: 'flex-end' })
    await ui.unmount()
    await pane.press({ key: 'cost' })

    // a failed check keeps the last reading and says what went wrong
    status = 500
    await clock.advance(61_000)
    await pane.press({ key: 'fable' })
    await pane.press({ key: 'fable' })
    texts = await band()
    expect(texts).toContain('17%')
    expect(texts).toContain('Usage check failed (HTTP 500)')

    // past 100% a model limit says OVER LIMIT, as Clawdmeter's scoped bars do
    status = 200
    percent = 104
    await clock.advance(61_000)
    await pane.press({ key: 'fable' })
    await pane.press({ key: 'fable' })
    texts = await band()
    expect(texts).toContain('OVER LIMIT')
    expect(texts).not.toContain('OVERAGE')

    // switched off, it asks nothing more, even once the wait has passed
    const before = calls.length
    await clock.advance(61_000)
    await pane.press({ key: 'fable' })
    expect(calls).toHaveLength(before)
    await pane.unmount()
  })

  test('no request without a Claude sign-in', async ($, on) => {
    const clock = mock.clock(on)
    mock.store(on)
    let fetched = 0
    on('session.authorize', () => ({ value: null }))
    on('http.fetch', () => { fetched++; return { value: { status: 200, ok: true, headers: {}, text: '{}' } } })
    await clock.set(1_800_000_000_000)
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    await pane.press({ key: 'fable' })
    expect(fetched).toBe(0)
    const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
    expect((await ui.findAll({ type: 'Text' })).map(t => t.text).join(' | ')).toContain('Needs a Claude sign-in')
    await ui.unmount()
    await pane.unmount()
  })
})
