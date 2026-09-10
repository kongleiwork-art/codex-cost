<div align="center">

# codex-cost

**Codex quota in your Mac's notch — and what your tokens would have cost on another model.**

[![macOS 14+](https://img.shields.io/badge/macOS-14%2B-black?logo=apple&logoColor=white)](#install)
[![Swift](https://img.shields.io/badge/Swift-native%2C%20no%20deps-orange?logo=swift&logoColor=white)](Sources/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Local only](https://img.shields.io/badge/data-never%20leaves%20your%20Mac-green)](#privacy)

<img src="docs/panel-en.png" width="380" alt="The expanded panel: token breakdown, both quota windows, per-model spend, and the same workload priced on every model">

[中文说明](README.zh-CN.md)

</div>

---

Every Codex usage tracker answers *how much have I used*. This one answers the
two questions that actually change what you do next:

- **What is that spend made of** — new input, output, or the per-request floor?
- **What would the same work have cost on a different model?**

The second one needs a cost model. Getting one took **421 controlled API calls
across 23 experiment cells**, changing one variable at a time. The raw trials and
the harness are in [`research/`](research/).

## Install

```bash
git clone https://github.com/kongleiwork-art/codex-cost
cd codex-cost && ./build.sh && open CodexCost.app
```

Needs Xcode Command Line Tools (`xcode-select --install`). Nothing else — no
package manager, no account, no API key.

> The app is unsigned. On first launch: **right-click → Open → Open**.

## Features

|  |  |
|---|---|
| **Cost breakdown** | New input vs. output vs. per-request floor — see which one is actually draining you |
| **Counterfactual pricing** | The same tokens, priced on every model you could have used |
| **Both quota windows** | 5-hour and weekly, with reset countdowns |
| **Threshold alerts** | Notifies at 80% and 95% with burn rate, time left, and a cheaper model when one would help |
| **Menu-bar fallback** | Works on Macs without a notch |
| **Bilingual** | English / 中文, follows system language |

## What the measurements showed

**Cached input is free.** It does not appear in the cost model at all. A session
carrying 220K tokens of context costs barely more per turn than one carrying 40K.
*Don't clear context to save quota* — you pay full price to read it back.

**Every request has a floor.** ~0.0667% on Sol regardless of size, so roughly 15
requests consume 1% of the five-hour window even when almost nothing comes back.
Redundant tool loops are expensive even when they're tiny.

**Effort is not charged at a premium — on Sol.** Higher effort costs more only
because it emits more reasoning tokens; across five levels the multiplier stayed
at 1.0 ± 0.1. In practice Sol at max effort still undercuts Astra at low effort,
so **turn the effort dial up before reaching for a bigger model.**

**Astra needs three numbers, not one.** Its input, output and request-floor
components sit at different multiples of Sol's, so any single "Astra is N×"
figure drifts between 3× and 11× depending purely on how much the model talks.
Short, decisive Astra work is affordable; long reasoning chains on it are not.

## Measured coefficients

Each model gets three coefficients instead of one multiplier:

| Model | New input per 1% | Output + reasoning per 1% | Per request |
|---|---:|---:|---:|
| `gpt-5.6-sol` | 41,398 tok | 15,450 tok | 0.0667% |
| `gpt-5.5` | 48,137 tok | 17,965 tok | 0.0574% |
| `gpt-5.6-terra` | 46,000 tok | 17,167 tok | 0.0600% |
| `gpt-5.6-luna` | free | free | free |
| `gpt-6-astra` | 15,415 tok | 2,495 tok | 0.3514% |

<details>
<summary><b>How this was measured, and where it's shaky</b></summary>

<br>

These numbers are only worth something if you know their error bars.

**Reliability, by model**

- **Sol is solid** — 22 cells, R² 0.987 on a per-call regression.
- **5.5 and Terra are indistinguishable from Sol** at this resolution. Their
  error bars overlap; treat all three as ≈1×.
- **Astra and Luna rest on ~30–40 calls each.** Read them as order-of-magnitude.

**Known limits of the method**

- **Token counts are estimated** from the size of tool output, not billed
  figures. Use them for ratios, not accounting.
- **Quota readings are integers.** A cell measured over Δ=4% carries ±12%
  uncertainty from rounding alone; only large-Δ cells are trustworthy.
- **`max − min` systematically understates Δ** when a cell has few samples —
  and the expensive models are exactly the ones that run out of budget fastest.
  An early Astra estimate of 2.45× was wrong for this reason.
- **Windows run to 100% must be discarded.** The counter saturates while tokens
  keep flowing, so Δ is truncated.
- **Concurrency contaminates attribution.** Don't use Codex while measuring.

**Two things about the quota system itself**

- **Readings are event-driven.** The logs record a value only when Codex makes a
  request, so "current usage" is always as of the last request. After a window
  rolls over with no activity the last reading is stale — codex-cost detects this
  via `resets_at`. *Tools that skip that check will happily show you 99% on an
  empty window.*
- **The 5-hour limit is a rolling window, not a fixed one.** Usage going from 84%
  to 0% in 43 minutes appears in the data; a fixed window cannot do that, a
  rolling one can when a burst ages out together.

**Scope:** one account, Plus plan, September 2026. Metering can change — the
harness has a `control` cell for re-checking.

</details>

<details>
<summary><b>Command-line flags</b></summary>

<br>

```bash
open CodexCost.app --args --menubar    # force menu-bar mode
open CodexCost.app --args --expanded   # start with the panel open
./codex-cost --render panel.png        # render the panel offscreen to a PNG
./codex-cost --lang en                 # override the system language
./codex-cost --dump                    # print the numbers to stdout
```

The screenshots in this README are produced by `--render`, so they regenerate
from real data instead of being hand-captured.

</details>

## Privacy

Reads `~/.codex/sessions` locally. No network calls, no telemetry, nothing
uploaded. The panel shows aggregate numbers only — no prompts, no file contents.

## Repo layout

```
Sources/     the app — Swift, no dependencies
cli/         the same cost model as a terminal tool
research/    the experiment harness and all 421 raw trials
docs/        rendered screenshots
```

## Contributing

Measurements from other plans and accounts are the most useful thing you could
contribute — the coefficients here come from a single Plus account. Run
`research/quota_probe.py` and open an issue with the output.

## License

[MIT](LICENSE)
