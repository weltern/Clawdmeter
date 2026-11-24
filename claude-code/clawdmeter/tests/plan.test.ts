import { describe, expect, mock, test } from 'claude-code/testing'

const site = { maxRows: 6, bodyColumns: 96, scroll: { offset: 0, bodyRows: 6 }, view: {} }
const working = { component: 'AbovePrompt', props: { ...site, hasSurvey: false, isCollapsed: false, isWorking: true } } as const

type Auth = { handle: string; kind: 'bearer' | 'api-key' } | null

/** Start a session signed in with `auth`, then return the band's text. */
async function bandWith($: any, on: any, auth: Auth, rateLimits: unknown[] = []): Promise<string> {
  mock.clock(on)
  mock.store(on)
  on('session.authorize', () => ({ value: auth }))
  on('session.usage', () => ({ value: { rateLimits, context: { window: 200000, tokens: 84000, percent: 42 } } }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', (_$: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/' })
  const ui = await $.ui.mount({ plugin: 'clawdmeter', surface: 'desktop', ...working })
  const texts = (await ui.findAll({ type: 'Text' })).map((t: { text: string }) => t.text).join(' | ')
  await ui.unmount()
  return texts
}

describe('accounts without plan limits', () => {
  test('a Claude sign-in keeps the Session and Weekly meters, even before a reading', async ($, on) => {
    const texts = await bandWith($, on, { handle: 'h', kind: 'bearer' })
    expect(texts).toContain('SESSION 5h')
    expect(texts).toContain('WEEKLY 7d')
  })

  test('an API-key session hides them and keeps Context', async ($, on) => {
    const texts = await bandWith($, on, { handle: 'k', kind: 'api-key' })
    expect(texts).not.toContain('SESSION')
    expect(texts).not.toContain('WEEKLY')
    expect(texts).toContain('84K of 200K')
  })

  test('no Claude sign-in at all (Bedrock, Vertex, a gateway) hides them too', async ($, on) => {
    const texts = await bandWith($, on, null)
    expect(texts).not.toContain('SESSION')
    expect(texts).toContain('CONTEXT')
  })

  test('a limit that does arrive still shows', async ($, on) => {
    const texts = await bandWith($, on, null, [{ kind: 'five_hour', percentUsed: 30 }])
    expect(texts).toContain('SESSION 5h')
    expect(texts).toContain('30%')
    expect(texts).not.toContain('WEEKLY')
  })
})
