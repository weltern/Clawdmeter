import { describe, expect, mock, test } from 'claude-code/testing'

import { activityFor, clawdSvg, detailFor, heatFor, prettyTool, untilText } from '../hooks/clawd'

const site = { maxRows: 6, bodyColumns: 96, scroll: { offset: 0, bodyRows: 6 }, view: {} }
const working = { component: 'AbovePrompt', props: { ...site, hasSurvey: false, isWorking: true } } as const
const idle = { component: 'AbovePrompt', props: { ...site, hasSurvey: false, isWorking: false } } as const
const SURFACES = ['desktop', 'terminal'] as const

describe('activity mapping (Clawdmeter TOOL_MAP)', () => {
  test('tools land on Clawdmeter activities', async () => {
    expect(activityFor('Edit')).toBe('coding')
    expect(activityFor('Grep')).toBe('reading')
    expect(activityFor('WebSearch')).toBe('searching')
    expect(activityFor('Agent')).toBe('planning')
    expect(activityFor('mcp__unifi-network__list_clients')).toBe('integrating')
    expect(activityFor('SomethingNew')).toBe('coding')
    expect(prettyTool('mcp__unifi-network__list_clients')).toBe('unifi-network/list_clients')
    expect(detailFor('Edit', { file_path: 'C:\\repo\\CHANGELOG.md' })).toBe('Edit · CHANGELOG.md')
    expect(detailFor('WebFetch', { url: 'https://docs.example.com/a/b' })).toBe('WebFetch · docs.example.com')
  })

  test('heat steps and reset wording follow Clawdmeter', async () => {
    expect([heatFor(74), heatFor(75), heatFor(87), heatFor(100), heatFor(101)]).toEqual(['cool', 'warm', 'hot', 'hot', 'over'])
    const now = Date.parse('2026-10-07T12:00:00Z')
    expect(untilText('2026-10-07T12:41:00Z', now)).toBe('resets in 41m')
    expect(untilText('2026-10-07T14:20:00Z', now)).toBe('resets in 2h 20m')
    expect(untilText('2026-10-11T18:00:00Z', now)).toBe('resets in 4d 06h')
  })

  test('every activity sprite fits the Svg limit and animates', async () => {
    for (const act of ['coding', 'reading', 'searching', 'planning', 'integrating', 'thinking', 'idle'] as const) {
      const svg = clawdSvg(act, 44)
      expect(svg.length).toBeLessThan(131072)
      expect(svg).toContain('<animate')
      // without it the desktop's frame paints a white box behind Clawd
      expect(svg).toContain('color-scheme:light dark')
    }
  })
})

describe('the band', () => {
  test('shows the running tool while it runs, then thinking, on each surface', async ($, on) => {
    mock.clock(on)
    const seen: Record<string, string> = {}
    on('tool.call', async () => {
      for (const surface of SURFACES) {
        const ui = await $.ui.mount({ plugin: 'clawd-band', surface, ...working })
        seen[surface] = (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' | ')
        await ui.unmount()
      }
      return { result: {} as never }
    })
    await $.tool.call({ tool: 'Edit', file_path: 'C:\\repo\\CHANGELOG.md', old_string: 'a', new_string: 'b' })
    for (const surface of SURFACES) {
      expect(seen[surface]).toContain('CODING')
      expect(seen[surface]).toContain('Edit · CHANGELOG.md')
    }
    const after = await $.ui.mount({ plugin: 'clawd-band', surface: 'desktop', ...working })
    expect(await after.find({ type: 'Text', text: /THINKING/ })).toBeDefined()
    await after.unmount()
  })

  test('is idle whenever no turn runs, whatever was seen last', async ($, on) => {
    mock.clock(on)
    on('tool.call', async () => ({ result: {} as never }))
    await $.tool.call({ tool: 'Grep', pattern: 'x' })
    const ui = await $.ui.mount({ plugin: 'clawd-band', surface: 'desktop', ...idle })
    expect(await ui.find({ type: 'Text', text: /IDLE/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /READING|THINKING/ })).toBeUndefined()
    await ui.unmount()
  })

  test('draws the measured windows, overage and context', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    on('session.measure', (_$, e) => ({ changed: e.changed }))
    await $.session.measure({
      context: { window: 200000, tokens: 84000, percent: 42 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 106, resetsAt: new Date(Date.now() + 18 * 60000).toISOString() },
        { kind: 'seven_day', percentUsed: 63, resetsAt: new Date(Date.now() + 4 * 86400000).toISOString() },
      ],
      changed: ['context', 'rateLimits'],
    })
    const ui = await $.ui.mount({ plugin: 'clawd-band', surface: 'desktop', ...working })
    const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text).join(' | ')
    expect(texts).toContain('106%')
    expect(texts).toContain('OVERAGE')
    expect(texts).toContain('63%')
    expect(texts).toContain('84K of 200K')
    const svgs = await ui.findAll({ type: 'Svg' })
    expect(svgs.some(s => s.props.isInteractive === true && String(s.props.source).includes('<animate'))).toBe(true)
    // bars fill their meter so each ends under its percentage: no fixed width
    const bars = svgs.filter(s => String(s.props.alt).includes('used'))
    expect(bars).toHaveLength(3)
    for (const b of bars) {
      expect(b.props.width).toBeUndefined()
      expect(b.props.height).toBe(6)
      expect(String(b.props.source)).toContain('preserveAspectRatio="none"')
    }
    await ui.unmount()
  })

  test('toasts once per window per reset when it passes 90%', async ($, on) => {
    mock.clock(on)
    mock.store(on)
    const toasts: string[] = []
    on('ui.toast', (_$, e) => { toasts.push(e.text) })
    on('session.measure', (_$, e) => ({ changed: e.changed }))
    const resetsAt = new Date(Date.now() + 41 * 60000).toISOString()
    const measure = (pct: number, at = resetsAt) => $.session.measure({
      context: { window: 200000 },
      rateLimits: [{ kind: 'five_hour', percentUsed: pct, resetsAt: at }],
      changed: ['rateLimits'],
    })
    await measure(89)
    expect(toasts).toHaveLength(0)
    await measure(91)
    await measure(93)
    expect(toasts).toHaveLength(1)
    expect(toasts[0]).toContain('Session limit at 91%')
    await measure(92, new Date(Date.now() + 5 * 3600000).toISOString())
    expect(toasts).toHaveLength(2)
  })

  test('keeps the meters in place whatever the activity text', async ($, on) => {
    mock.clock(on)
    const layout: string[] = []
    on('tool.call', async () => {
      const ui = await $.ui.mount({ plugin: 'clawd-band', surface: 'desktop', ...working })
      const boxes = ['activity', 'five_hour', 'seven_day', 'context']
      const props = await Promise.all(boxes.map(async key => (await ui.find({ key }))?.props))
      for (const p of props) expect(p).toBeDefined()
      expect(props[0]).toMatchObject({ width: '24%', flexGrow: 0, flexShrink: 0 })
      for (const p of props.slice(1)) expect(p).toMatchObject({ width: 0, flexGrow: 1 })
      layout.push(JSON.stringify(props))
      await ui.unmount()
      return { result: {} as never }
    })
    await $.tool.call({ tool: 'Grep', pattern: 'x' })
    await $.tool.call({ tool: 'mcp__unifi-network__list_clients_with_a_much_longer_name' })
    expect(layout).toHaveLength(2)
    expect(layout[0]).toBe(layout[1])
  })

  test('yields the band to a survey', async ($, on) => {
    let passed = false
    on('ui.render', { component: 'AbovePrompt' }, (eng, e) => {
      passed = true
      const { Box } = eng.ui.resolve(e)
      return h(Box, {}) as never
    })
    const ui = await $.ui.mount({ plugin: 'clawd-band', surface: 'desktop', component: 'AbovePrompt', props: { ...working.props, hasSurvey: true } })
    expect(passed).toBe(true)
    expect(await ui.find({ type: 'Text', text: /CODING|THINKING|IDLE/ })).toBeUndefined()
    await ui.unmount()
  })
})
