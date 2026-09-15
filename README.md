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

The second one needs a cost model. Getting one took **655 controlled API calls
across 33 experiment cells**, changing one variable at a time. The raw trials and
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
| **Cache expiry hint** | Come back to a large session after 10+ idle minutes and the panel shows what resuming costs if the cache has expired |
| **Menu-bar fallback** | Works on Macs without a notch |
| **Usage history** | A second tab totals tokens from Codex, Claude Code and opencode by day, tool and model — from local logs only |
| **Bilingual** | English / 中文, follows system language |

### Task routing (experimental, terminal only)

`cli/codex_route.py` suggests a model for a task: local keyword rules first, and
Luna only when the rules have nothing to go on.

```bash
python3 cli/codex_route.py --task "fix the flaky test" --json
python3 cli/codex_route.py --task "..." --no-luna   # local rules only
```

It is not wired into the app. A backtest over real Codex history
([`research/backtest_routing.py`](research/backtest_routing.py)) found nothing to
save: over 90% of quota went to sessions longer than 100 requests, the rules were
confident about only about a third of that spend, and following them would have
cost slightly more. Cache misses turned out to be the bigger lever — see
[docs/ROADMAP.md](docs/ROADMAP.md).

### Usage history

The **History** tab adds up every token this Mac has a local record of — today,
7 days, 30 days or all time — split by tool and by model, with a daily bar chart.

<img src="docs/history-en.png" width="380" alt="The History tab: a week of tokens from Codex, Claude Code and opencode, split by tool and by model">

| Source | Where it reads | Deduplication |
|---|---|---|
| Codex (CLI and desktop) | `~/.codex/sessions`, `~/.codex/archived_sessions` | timestamp + token counts — forked and archived sessions copy events verbatim |
| Claude Code | `~/.claude/projects` | message id, keeping the largest output — one reply is written several times as it streams, and resumed sessions copy it into new files |
| opencode | `~/.local/share/opencode/opencode.db`, opened read-only | message id; opencode's own dollar cost is shown as-is |

Codex totals are also converted to 5-hour-window equivalents with the coefficients
above; Claude Code and opencode are shown in tokens only. Some early Codex sessions
never recorded a model name; their tokens are listed as “Unknown model”.

Cursor keeps a `tokenCount` field locally but it is always zero, and Gemini CLI and
the ChatGPT / Claude chat apps keep no token counts at all — so they cannot be
included, and the panel says so.

Parsing is cached per file in `~/Library/Application Support/codex-cost/usage-index.json`
(the first scan of several GB takes about 15 seconds, later refreshes about one).
The index keeps records from files that have since been deleted, so history survives
Claude Code's 30-day transcript cleanup. Nothing is uploaded.

## What the measurements showed

**Cached input is cheap, not free — roughly 12× cheaper than fresh.** One percent
of the five-hour window buys ~42K fresh input tokens or ~490K cached ones, and the
cached cost grows in straight proportion to context size (20 requests each at
20K, 60K, 120K and 200K of context all land on one line). That is why long
sessions still add up: one real session carrying ~150K of context across 148
turns spent about half of its quota on cached input alone. *Still don't clear
context to save quota* — rebuilding it costs full fresh price, about 12× worse.
But a very long session is not free either.

**Idle time is what expires the cache.** In this account's own sessions, a
request sent within a minute of the previous one found its cache gone less than 1%
of the time; after 10–30 idle minutes, more than a quarter of the time; after an
hour, 9 times in 10. A miss re-bills the whole context as new input — on
a 150K-token Sol session that is about 3.5% of the five-hour window instead of
0.3%. Over 30 days, cache misses took about 11% of this account's quota, so the
panel now warns you when you come back to a large session.

**Changing reasoning effort mid-session resets the cache too.** Of the misses
that came within five minutes of the previous request, nearly half followed an
effort change. Settle the effort at the start of a long session, not halfway through.

**The per-request floor is small — what you pay for is what each request
carries.** ~0.03% on Sol, so about 30 near-empty requests make 1% of the
five-hour window. An earlier version of this README said 0.08%: older
experiments sent thousands of fresh tokens with every request, so the two could
not be told apart. Two cells built to separate them — same cached total, 4×
different request counts — put the floor near zero. A tool loop is cheap if it
re-sends little; it is expensive when every turn drags a large context along.

**Effort is not charged at a premium — on Sol.** Higher effort costs more only
because it emits more reasoning tokens; across five levels the multiplier stayed
at 1.0 ± 0.1. In practice Sol at max effort still undercuts Astra at low effort,
so **turn the effort dial up before reaching for a bigger model.**

**Astra needs more than one number.** Relative to Sol its cached input costs
~2× and its output ~6×, while its per-request floor is an order of magnitude
higher, so any single "Astra is N×" figure shifts with how much the model talks.
A few substantial Astra requests are affordable; long reasoning chains and
chatty tool loops on it are not.

## Measured coefficients

Each model gets four coefficients instead of one multiplier:

| Model | New input per 1% | Cached input per 1% | Output + reasoning per 1% | Per request |
|---|---:|---:|---:|---:|
| `gpt-5.6-sol` | 42,500 tok | 492,537 tok | 13,572 tok | 0.0328% |
| `gpt-5.6-terra` | 47,223 tok | 547,263 tok | 15,080 tok | 0.0295% |
| `gpt-5.6-luna` | free | free | free | free |
| `gpt-6-astra` | 35,094 tok\* | 250,428 tok | 2,318 tok | 0.5632%\* |

Sol's four are fitted jointly on every Sol measurement with non-negative
least squares ([`research/refit.py`](research/refit.py)). Terra is
indistinguishable from Sol at this resolution and is scaled from it; gpt-5.5, an
older model, is no longer listed. \*Astra's
cached rate comes from a dedicated 120K-context cell; how the rest splits between
fresh input and the per-request floor is still loose (fresh anywhere from 25K to
80K fits almost equally well).

<details>
<summary><b>How this was measured, and where it's shaky</b></summary>

<br>

These numbers are only worth something if you know their error bars.

**Reliability, by model**

- **Sol is the best-measured model, but not settled** — a joint fit over 140
  regression points, RMS 0.84 against a 0.29 rounding floor, leave-one-out 0.87.
  The fit error sits well above the floor, and one real 148-request session is
  over-predicted (88% vs. 82%). The table below shows where each version lands.
- **Terra is indistinguishable from Sol** at this resolution. Their error bars
  overlap; treat both as ≈1×.
- **Astra rests on four segments (~100 calls); Luna on 30.** Astra's cached and
  output rates hold up when any one segment is dropped; its fresh-vs-per-request
  split does not. Read Luna, and that part of Astra, as order-of-magnitude.

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

**Three things about the quota system itself**

- **Readings are event-driven.** The logs record a value only when Codex makes a
  request, so "current usage" is always as of the last request. After a window
  rolls over with no activity the last reading is stale — codex-cost detects this
  via `resets_at`. *Tools that skip that check will happily show you 99% on an
  empty window.*
- **The 5-hour limit is a rolling window, not a fixed one.** Usage going from 84%
  to 0% in 43 minutes appears in the data; a fixed window cannot do that, a
  rolling one can when a burst ages out together.
- **A reading lags one request.** The quota value returned with a request does
  not yet include that request's own cost; the next one does. Aligning the fit
  that way lowers its error for both Sol and Astra.

**How the coefficients got here — four versions**

1. **"Cached input is free."** Wrong, and wrong in the analysis rather than the
   data: resumed-session cells record *cumulative* token counts, and the analysis
   summed them across trials, inflating cached volume ~7×.
2. **677,444 tok/1%**, from a purpose-built cell — a 400KB seed, then 60
   one-word turns, Δ=24% — solved as a residual against the existing
   coefficients. Better, but those coefficients had been fitted with cached at
   zero, so the per-request term already absorbed part of the cache cost. Adding
   a cached term on top double-counted it: every cell came out over-estimated,
   by +0.85% on average.
3. **508,494 tok/1%**, from refitting all four coefficients jointly on all 481
   trials. Across 24 cells: MAE 0.87% → 0.66%, mean bias +0.85% → +0.32%. But
   its per-request floor (0.0819%) was inflated: in those trials fresh input and
   request count rose together, so the fit could trade one for the other.
4. **Current.** Two new cell families broke that tie. `req/many` and `req/few`
   hold the cached total equal while request counts differ 4×; the difference
   alone solves the floor at −0.012% ± 0.042%. `ctx/*` fix 20 requests and sweep
   context from 20K to 200K; all four sit within rounding of one straight line.
   Refitting everything, aligned to the one-request lag in quota readings, gives
   the table above.

|  | v3 | v4 (current) | observed |
|---|---:|---:|---:|
| `req/many` — 63 small requests | 12.7% | 10.6% | 9% |
| `req/few` — 15 large requests | 7.2% | 6.7% | 8% |
| `ctx/20k` | 2.5% | 1.7% | 1% |
| `ctx/200k` | 9.1% | 8.5% | 11% |
| `cache/bigctx` | 24.7% | 25.4% | 24% |
| a real 148-request session | 81.9% | 88.0% | 82% |

Neither version wins everywhere. v4 fixes the cells designed to separate the
floor from fresh input and halves the mean bias (+0.61 → +0.31 across 11
segments), but its segment MAE is slightly worse (0.99 → 1.13) and it misses the
real session by 6 points. Large contexts are under-predicted by both —
`ctx/200k` suggests cached input may cost more than the joint fit says, which the
older `cache/bigctx` cell does not show. That is the open question.

Version 3 came out of a second, independent pass over the data
([#1](https://github.com/kongleiwork-art/codex-cost/pull/1)). That pass ran on the
repo's copy of the trials, which was missing the 60 `cache/bigctx` rows — so it
concluded the cached rate was unidentifiable. With those rows restored, cached is
strongly identified: forcing it to zero raises the fit error from 0.49 to 3.11.

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
tests/       fixture logs + regression tests for the app and the CLI
research/    the experiment harness and every raw trial
docs/        screenshots, regenerated from fixtures by docs/render.sh
```

## Contributing

Planned work and open decisions live in [docs/ROADMAP.md](docs/ROADMAP.md) (Chinese).
Measurements from other plans and accounts are the most useful thing you could
contribute — the coefficients here come from a single Plus account. Run
`research/quota_probe.py` and open an issue with the output.

Changing the log parser or the cost model? Run the tests first. They feed the
same fixture logs to the app and the CLI (both honor `CODEX_HOME`) and check
the expected numbers and that the two implementations agree — including the
case where the weekly quota is exhausted and Codex moves to a separate pool:

```bash
./build.sh && python3 tests/test_quota.py
```

## License

[MIT](LICENSE)
