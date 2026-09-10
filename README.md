# codex-cost

A small macOS tool for answering a question I kept running into:

> **What is actually eating my Codex quota?**

Codex already tells you how much of your 5-hour and weekly limits you have used.  
codex-cost tries to explain **why** that number moved.

It reads your local Codex session data, breaks usage down into new input, output/reasoning, and per-request overhead, then estimates how the same workload would compare across models.

It lives beside the MacBook notch, or in the menu bar if your Mac does not have one.

- Native Swift
- No dependencies
- No account setup
- No telemetry
- Nothing leaves your Mac

[中文说明](README.zh-CN.md)

---

## Install

~~~bash
git clone https://github.com/kongleiwork-art/codex-cost
cd codex-cost
./build.sh
open CodexCost.app
~~~

You only need the Xcode Command Line Tools:

~~~bash
xcode-select --install
~~~

The app is currently unsigned. On first launch, right-click CodexCost.app, choose **Open**, then confirm **Open** again.

Command-line flags:

~~~bash
open CodexCost.app --args --menubar     # force menu-bar mode
open CodexCost.app --args --expanded    # start with the panel open
./codex-cost --render panel.png         # render the panel offscreen to a PNG
./codex-cost --lang en                  # override the system language
./codex-cost --dump                     # print the numbers to stdout
~~~

---

## What it shows

<img src="docs/panel-en.png" width="372" alt="The expanded panel: token breakdown, both quota windows, per-model spend, and the same workload priced on every other model">

The last section is the one that took the most work to earn. Those same tokens
would have cost 0% on Luna and about 8% on Sol — against the 35% they actually
cost on Astra.

The screenshots here are produced by `--render`, so they can be regenerated from
real data instead of hand-captured.

When collapsed, CodexCost stays out of the way beside the notch and shows the current model and quota usage.

Open it to see:

- 5-hour and weekly quota usage
- estimated burn rate
- token breakdown
- which models consumed the quota
- how the same workload would compare on other models

At 80% and 95% of the 5-hour window, it can notify you before you hit the limit. If a cheaper model would likely make a meaningful difference, the notification also suggests one.

The goal is not to tell you to always use the cheapest model. It is to make the trade-off visible.

---

## Why I built it

I kept seeing Codex quota disappear much faster on some sessions than others, but the usual usage number did not explain what changed.

Was it:

- a larger context?
- more reasoning?
- more output?
- too many small requests?
- the model itself?
- cached context being charged again?

So I started measuring it.

The current model comes from **421 controlled calls across 23 experiment cells**, with one variable changed at a time. The raw data and experiment harness are in [research/](research/).

This is still an experiment, not an official OpenAI billing model. The numbers below describe what I measured on one Plus account in September 2026.

---

## What I found

### 1. Cached input appears to be effectively free

In my measurements, cached input did not show up as a meaningful driver of quota burn.

That means a long-running session with 220K tokens of context can cost only slightly more per turn than a much smaller session, as long as most of that context stays cached.

**Clearing context just to save quota can backfire**, because rebuilding it means paying for fresh input again.

### 2. Small requests still have a floor

On gpt-5.6-sol, I measured a per-request floor of about **0.0667%** of the 5-hour window.

So even tiny requests are not free. Roughly 15 requests can consume 1% of the window even when very little text comes back.

That makes unnecessary tool loops and repeated tiny calls worth paying attention to.

### 3. Reasoning effort was not independently more expensive on Sol

For gpt-5.6-sol, higher reasoning effort cost more mainly because it produced more reasoning tokens. I did not observe a separate premium multiplier for the effort setting itself.

Across five effort levels, the measured multiplier stayed around **1.0 ± 0.1**.

One practical implication from this dataset: before jumping to a more expensive model, it can be worth trying a higher reasoning setting on Sol first.

### 4. Astra behaves very differently

A single "Astra is X times more expensive" number was not enough to describe what I measured.

Relative to Sol, Astra's input, output/reasoning, and request-floor components behaved differently. Long, verbose Astra runs were especially expensive, while short, focused calls were much easier on quota.

---

## Measured coefficients

Each model uses three coefficients rather than one overall multiplier:

| Model | New input per 1% | Output + reasoning per 1% | Per request |
|---|---:|---:|---:|
| gpt-5.6-sol | 41,398 tok | 15,450 tok | 0.0667% |
| gpt-5.5 | 48,137 tok | 17,965 tok | 0.0574% |
| gpt-5.6-terra | 46,000 tok | 17,167 tok | 0.0600% |
| gpt-5.6-luna | free | free | free |
| gpt-6-astra | 15,415 tok | 2,495 tok | 0.3514% |

For Astra, the three components were roughly **2.7× / 6× / 5.3×** Sol in this dataset. Depending on the shape of the request, the effective multiplier can therefore move a lot.

---

## How trustworthy are these numbers?

There are some important limits.

- **Token counts are estimated.** They are derived from local session/tool output, not official billed token records.
- **Quota readings are integers.** Small changes have large rounding error. Experiments that moved quota by only a few percentage points are much noisier.
- **Usage is event-driven.** Codex writes a new reading when it makes a request, so the latest value can be stale after a quiet period. CodexCost checks resets_at to avoid showing an old percentage after the window has rolled over.
- **The 5-hour limit behaves like a rolling window.** In the captured data, usage sometimes dropped sharply as an earlier burst aged out.
- **Sol has the strongest dataset.** Its per-call regression reached R² 0.987 across 22 cells.
- **5.5 and Terra are difficult to distinguish from Sol** at the current measurement resolution.
- **Astra and Luna have fewer samples**, so treat their numbers as directional rather than precise.
- **All measurements came from one Plus account in September 2026.** OpenAI can change metering behavior at any time.

If you want to re-check the model on your own account, the experiment harness in research/ includes a control cell.

And one important wording point: on a subscription, lower token usage does not literally save money. It gives you **more headroom before you hit the quota wall**.

---

## Privacy

CodexCost reads ~/.codex/sessions locally.

It does not send telemetry or upload your prompts, files, or session contents. The UI only shows aggregate usage data.

---

## Repo layout

~~~text
Sources/     macOS app, native Swift
cli/         terminal version of the same cost model
research/    experiment harness and all 421 raw trials
~~~

---

## License

MIT
