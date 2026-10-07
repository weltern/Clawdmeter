import { describe, expect, mock, test } from 'claude-code/testing'

import { DEFAULT_SETTINGS, heatFor, normalizeSettings } from '../hooks/clawd'

const site = { maxRows: 6, bodyColumns: 96, scroll: { offset: 0, bodyRows: 6 }, view: {} }
const working = { component: 'AbovePrompt', props: { ...site, hasSurvey: false, isWorking: true } } as const
const PANE = { component: 'Pane', requestId: 'clawdmeter-settings', props: {} as never } as const

describe('settings helpers', () => {
  test('anything stored comes back as valid settings', async () => {
    expect(normalizeSettings(undefined)).toEqual(DEFAULT_SETTINGS)
    expect(normalizeSettings('garbage')).toEqual(DEFAULT_SETTINGS)
    const odd = normalizeSettings({ layout: 'huge', warnAt: 0, glow: 'yes', meters: { session: false, weekly: false, context: false, cost: false } })
    expect(odd.layout).toBe('full')
    expect(odd.warnAt).toBe(75)
    expect(odd.glow).toBe(true)
    expect(odd.meters.session).toBe(true)
    expect(normalizeSettings({ warnAt: 60, clawd: 'large', notify: false })).toMatchObject({ warnAt: 60, clawd: 'large', notify: false })
  })

  test('the warn point moves both heat steps, as Clawdmeter does', async () => {
    expect([heatFor(59, 60), heatFor(60, 60), heatFor(80, 60), heatFor(79, 60)]).toEqual(['cool', 'warm', 'hot', 'warm'])
    expect([heatFor(89, 90), heatFor(90, 90), heatFor(95, 90)]).toEqual(['cool', 'warm', 'hot'])
  })
})

describe('the settings panel drives the band', () => {
  test('the gear sits bottom-right on the band', async ($, on) => {
    mock.clock(on)
    const band = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
    expect((await band.find({ key: 'gear-box' }))?.props.alignSelf).toBe('flex-end')
    expect(await band.find({ key: 'gear' })).toBeDefined()
    await band.unmount()
  })

  test('meters, layout, Clawd and the activity line follow the panel', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    on('session.measure', (_$, e) => ({ changed: e.changed }))
    await $.session.measure({
      context: { window: 1000000, tokens: 551000, percent: 55 },
      rateLimits: [{ kind: 'five_hour', percentUsed: 63 }, { kind: 'seven_day', percentUsed: 36 }],
      cost: { usd: 4.12 } as never,
      changed: ['context', 'rateLimits', 'cost'],
    })
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    const band = async () => {
      const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
      const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' | ')
      const svgs = await ui.findAll({ type: 'Svg' })
      const spacer = await ui.find({ key: 'spacer' })
      await ui.unmount()
      return { texts, svgs, isSlim: spacer !== undefined }
    }

    let b = await band()
    expect(b.texts).not.toContain('COST')
    expect(b.texts).toContain('CONTEXT')

    await pane.press({ key: 'cost' })
    await pane.press({ key: 'context' })
    b = await band()
    expect(b.texts).toContain('COST')
    expect(b.texts).toContain('$4.12')
    expect(b.texts).not.toContain('CONTEXT')

    await pane.press({ key: 'weekly' })
    b = await band()
    expect(b.texts).not.toContain('WEEKLY')
    expect(b.texts).toContain('SESSION')
    await pane.press({ key: 'weekly' })

    await pane.press({ key: 'glow-off' })
    b = await band()
    const clawd = b.svgs.find(s => s.props.isInteractive === true)
    expect(String(clawd?.props.source)).not.toContain('feDropShadow')

    await pane.press({ key: 'clawd-hidden' })
    b = await band()
    expect(b.svgs.some(s => s.props.isInteractive === true)).toBe(false)

    await pane.press({ key: 'layout-slim' })
    b = await band()
    expect(b.isSlim).toBe(true)

    await pane.press({ key: 'reset' })
    b = await band()
    expect(b.isSlim).toBe(false)
    expect(b.texts).toContain('CONTEXT')
    await pane.unmount()
  })

  test('the last meter cannot be switched off', async ($, on) => {
    mock.store(on)
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    await pane.press({ key: 'weekly' })
    await pane.press({ key: 'context' })
    await pane.press({ key: 'session' })
    expect((await pane.find({ key: 'seg-session' }))?.props.backgroundColor).toBe('#D97757')
    await pane.unmount()
  })

  test('What to show is a multi-select segmented control', async ($, on) => {
    mock.store(on)
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    const fill = async (k: string) => (await pane.find({ key: `seg-${k}` }))?.props.backgroundColor
    expect([await fill('session'), await fill('weekly'), await fill('context'), await fill('cost')])
      .toEqual(['#D97757', '#D97757', '#D97757', undefined])
    await pane.press({ key: 'cost' })
    expect(await fill('cost')).toBe('#D97757')
    expect(await fill('session')).toBe('#D97757')
    await pane.press({ key: 'weekly' })
    await pane.press({ key: 'context' })
    expect(await pane.find({ key: 'div-context' })).toBeDefined()
    expect(await pane.find({ key: 'div-weekly' })).toBeUndefined()
    // single-select rows light exactly the chosen segment
    expect([await fill('layout-full'), await fill('layout-slim')]).toEqual(['#D97757', undefined])
    await pane.press({ key: 'layout-slim' })
    expect([await fill('layout-full'), await fill('layout-slim')]).toEqual([undefined, '#D97757'])
    await pane.unmount()
  })

  test('glow is one Off | Soft | Medium | Strong row that sets the glow', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    const fill = async (k: string) => (await pane.find({ key: `seg-glow-${k}` }))?.props.backgroundColor
    expect([await fill('off'), await fill('soft'), await fill('medium'), await fill('strong')]).toEqual([undefined, undefined, '#D97757', undefined])
    const clawdSource = async () => {
      const band = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
      const clawd = (await band.findAll({ type: 'Svg' })).find(s => s.props.isInteractive === true)
      await band.unmount()
      return String(clawd?.props.source)
    }
    await pane.press({ key: 'glow-strong' })
    expect(await fill('strong')).toBe('#D97757')
    expect(await clawdSource()).toContain('stdDeviation="1.575"')
    await pane.press({ key: 'glow-off' })
    expect(await fill('off')).toBe('#D97757')
    expect(await clawdSource()).not.toContain('feDropShadow')
    await pane.unmount()
  })

  test('the warn stepper recolours the bars and stops at its ends', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    on('session.measure', (_$, e) => ({ changed: e.changed }))
    await $.session.measure({ context: { window: 200000 }, rateLimits: [{ kind: 'five_hour', percentUsed: 63 }], changed: ['rateLimits'] })
    const sessionBar = async () => {
      const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
      const svg = (await ui.findAll({ type: 'Svg' })).find(s => String(s.props.alt).startsWith('SESSION'))
      await ui.unmount()
      return String(svg?.props.source)
    }
    const shown = async (p: { find: (q: { key: string }) => Promise<{ text: string } | undefined> }) => (await p.find({ key: 'val-warn' }))?.text
    expect(await sessionBar()).toContain('#D97757')
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    expect(await shown(pane)).toBe('75%')
    await pane.press({ key: 'warn-down' })
    await pane.press({ key: 'warn-down' })
    await pane.press({ key: 'warn-down' })
    expect(await shown(pane)).toBe('60%')
    expect(await sessionBar()).toContain('#B85C42')
    for (let i = 0; i < 5; i++) await pane.press({ key: 'warn-down' })
    expect(await shown(pane)).toBe('50%')
    for (let i = 0; i < 12; i++) await pane.press({ key: 'warn-up' })
    expect(await shown(pane)).toBe('95%')
    await pane.unmount()
  })

  test('turning notifications off stops the 90% toast', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const toasts: string[] = []
    on('ui.toast', (_$, e) => { toasts.push(e.text); return { value: undefined } })
    on('session.measure', (_$, e) => ({ changed: e.changed }))
    const pane = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...PANE })
    await pane.press({ key: 'notify' })
    await $.session.measure({ context: { window: 200000 }, rateLimits: [{ kind: 'five_hour', percentUsed: 95, resetsAt: '2030-01-01T00:00:00Z' }], changed: ['rateLimits'] })
    expect(toasts).toHaveLength(0)
    await pane.press({ key: 'notify' })
    await $.session.measure({ context: { window: 200000 }, rateLimits: [{ kind: 'five_hour', percentUsed: 96, resetsAt: '2030-01-01T00:00:00Z' }], changed: ['rateLimits'] })
    expect(toasts).toHaveLength(1)
    await pane.unmount()
  })
})
