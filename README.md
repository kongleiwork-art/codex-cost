# codex-cost

**Where your Codex quota actually goes — and what it would have cost on another model.**

Every other usage tracker answers *how much have I used*. This one answers two
questions they don't:

- **What is that spend made of** — new input, model output, or the per-request floor?
- **What would the same work have cost on a different model?**

That second question is the point. Model choice turns out to be the largest lever
by a wide margin, and nothing else shows you the counterfactual.

Lives in the notch. Falls back to the menu bar on Macs that don't have one.
Native Swift, no dependencies, nothing leaves your machine.

*[中文说明](README.zh-CN.md)*

---

## Install

```bash
git clone https://github.com/kongleiwork-art/codex-cost
cd codex-cost && ./build.sh && open CodexCost.app
```

Needs Xcode Command Line Tools (`xcode-select --install`). Nothing else.

The app is unsigned, so the first launch needs **right-click → Open → Open**.
After that it opens normally.

## What you see

Collapsed, it's a sliver beside the notch: current model, quota used, a ring.
Click it and it opens into the full panel — cost breakdown, both quota windows,
which models you burned it on, and what the same tokens would have cost on each
of the others.

At 80% and 95% of the 5-hour window it sends a notification with the burn rate,
how long you have left, and — when a cheaper model would have made a real
difference — which one to switch to.

```
--menubar    force menu-bar mode even on a Mac with a notch
```

---

## The cost model

This is the part no other tool has. The coefficients come from **421 controlled
API calls across 23 experiment cells**, changing one variable at a time. The raw
data and the harness are in [`research/`](research/).

Each model gets three coefficients rather than a single multiplier:

| | new input (1% buys) | output+reasoning (1% buys) | per request |
|---|---|---|---|
| `gpt-5.6-sol` | 41,398 tok | 15,450 tok | 0.0667% |
| `gpt-5.5` | 48,137 tok | 17,965 tok | 0.0574% |
| `gpt-5.6-terra` | 46,000 tok | 17,167 tok | 0.0600% |
| `gpt-5.6-luna` | free | free | free |
| `gpt-6-astra` | 15,415 tok | 2,495 tok | 0.3514% |

**A single multiplier does not describe `astra`.** Its three components sit at
2.7× / 6× / 5.3× relative to `sol`, so the "effective multiplier" swings between
3.4× and 11× purely with how much the model talks. Every contradictory estimate
we got early on was that one effect seen from different angles.

## Four findings you can act on

**Cached input is free.** Not discounted — absent from the cost model entirely. A
session carrying 220K tokens of context costs barely more per turn than one
carrying 40K, because everything but the newest few thousand tokens is cached.
*Do not clear context to save quota* — you pay full price to read it back.

**There is a per-request floor.** 0.0667% on `sol`, regardless of size. Fifteen
requests consume 1% of the five-hour window even if they return nothing, which
caps you at roughly 1,500 requests per window. Redundant tool calls are expensive
even when they're small.

**Reasoning effort has no independent multiplier** — on `sol`. Raising effort
costs more only because it produces more reasoning tokens; you are not charged a
premium rate. Measured across five effort levels (n=15–22 each), every multiplier
landed within 1.0 ± 0.1. In practice `sol` at max effort still costs about a
third less than `astra` at low effort, so **exhaust the effort dial before
reaching for a bigger model** — the opposite of the common instinct.

**Don't let `astra` think out loud.** Its output and reasoning tokens are ~6×
`sol`'s while its input is only 2.7×. Short, decisive `astra` work is affordable;
long chains of reasoning on it are the single most expensive thing you can do.

## How this is measured, and where it's shaky

Stated plainly, because these numbers are only worth anything if you know their
error bars:

- **Token counts are estimated** from the size of tool output. They are not
  billed figures. Use them for ratios, not accounting.
- **Quota readings are integers.** A cell measured over Δ=4% carries ±12%
  uncertainty from rounding alone. Only cells run to a large Δ are trustworthy.
- **Quota readings are event-driven.** The logs record a value only when Codex
  makes a request, so "current usage" is always as of the last request. After a
  window rolls over with no activity, the last reading is stale — CodexCost
  detects this via `resets_at`. Tools that skip that check will happily show you
  99% on an empty window.
- **The 5-hour limit is a rolling window, not a fixed one that resets.** Usage
  from 84% to 0% in 43 minutes appears in the data; a fixed window cannot do
  that, a rolling one can when a burst of usage ages out together.
- **`sol` is solid** (22 cells, R² 0.987 on a per-call regression). **`5.5` and
  `terra` are indistinguishable from `sol`** at the resolution we have — the
  error bars overlap, so treat all three as ≈1×. **`astra` and `luna` rest on
  ~30–40 calls each** and should be read as order-of-magnitude.
- **Measured on one account, `plus` plan, September 2026.** Metering can change.
  The harness in `research/` has a `control` cell for re-checking.

**Do not treat any of this as a savings estimate.** On a subscription the tokens
you save do not become money; they become headroom before you hit the wall.

## Privacy

Reads `~/.codex/sessions` locally. No network calls, no telemetry, nothing
uploaded. The panel shows only aggregate numbers — no prompts, no file contents.

## Repo layout

```
Sources/     the app — Swift, no dependencies
cli/         same cost model as a terminal tool
research/    the experiment harness and all 421 raw trials
```

## License

MIT
