# Clawdmeter for Claude Code: changelog

The Claude Code plugin in this folder has its own version, separate from the
Clawdmeter app. Each version is tagged `claude-plugin-vX.Y.Z` on `main`.

## 0.3.0 (first release)

- **Clawd above your prompt.** A band above the Claude Code prompt where Clawd
  animates to match what Claude is doing (coding, reading, searching, planning,
  integrating, thinking, idle), with the tool and what it's working on.
- **Usage at a glance.** Session (5h) and weekly (7d) bars with reset times,
  how full the conversation's context is, and optionally its cost. Bars turn
  amber, then red, as a limit nears, show OVERAGE past 100%, and you get one
  notification per window the first time it passes 90%.
- **Fable meter.** Switch on Fable to add a FABLE 7d meter for your weekly Fable
  limit. It's off by default. While it's on, the plugin checks your usage page
  every 2 minutes using your existing Claude sign-in, which the plugin never
  sees; without a Claude sign-in it makes no request. Past 100% it shows OVER
  LIMIT.
- **Settings.** Click the gear at the bottom-right of the band, or type
  `/clawdmeter`, to choose the meters, the layout (full or slim), the activity
  line, Clawd's size and glow, where the bars turn amber, and whether you're
  notified. Changes show straight away and are kept for next time.
- Works in the Claude Code desktop app, with animated Clawd and graphic bars,
  and in the terminal, where the same activity and figures show as text.
