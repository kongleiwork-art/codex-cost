#!/usr/bin/env python3
"""codex-budget — 你这个会话花了多少额度，换个模型会花多少。

现有的用量工具都在回答"我用掉了多少"。这个回答两个它们不答的问题：
  · 这笔花费是怎么构成的（新输入 / 输出 / 每请求底价）
  · 同样这些活，换成别的模型要花多少 —— 反事实计算

成本模型来自受控实验（655 次测量、33 组，research/refit.py 联合拟合）：

    Δ5h% = fresh_input/F + cached_input/C + 输出侧/O + R × 请求数

每个模型一组 (F, C, O, R)；「输出侧」= output + reasoning。缓存输入不是
免费的，只是便宜：sol 上约为 fresh 的 1/12。

零依赖，标准库，Python 3.8+。只读本地日志，不上传任何数据。
"""
from __future__ import annotations
import argparse, glob, json, os, sys, time
from datetime import datetime, timezone

__version__ = "0.1.0"
HOME = os.path.expanduser("~")
# 与 Codex 自己一致：设了 CODEX_HOME 就用它，否则 ~/.codex
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"))
SESS_GLOB = os.path.join(CODEX_HOME, "sessions/**/rollout-*.jsonl")
WIN_5H, WIN_WEEK = 300, 10080

# ── 成本模型 ─────────────────────────────────────────────────────────────
# 为什么每个模型各一组系数、而不是「一个乘数乘以基准公式」：astra 各成分相对
# sol 的倍数差了一个数量级（缓存 ~2×、输出 ~6×、每请求高一个数量级），单一乘数下
# 等效倍数完全取决于调用的构成，怎么都对不上。

# (每 1% 的 fresh token 数, 每 1% 的缓存输入 token 数,
#  每 1% 的输出侧 token 数, 每次请求成本%)
#
# 与 Sources/Budget.swift 保持一致，来龙去脉见那边的注释。简言之：对全部 sol
# 测量做联合非负最小二乘（research/refit.py，读数滞后一次对齐），sol 140 个回归点
# RMS 0.84。req/* 与 ctx/* 把 fresh 和「每请求」拆开了：旧版 0.0819% 里大半其实是
# fresh 成本。terra 与 sol 分不出来，按 0.90× 缩放（5.5 是老模型，不参加对照）；astra 的缓存费率
# 由 astra/bigctx 定下，fresh 与每请求的拆分仍不稳。
# 数值只存在 research/coefficients.json（app 由它生成、refit 也读它）
COEF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                         "research", "coefficients.json")


def _load_coef():
    with open(COEF_FILE, encoding="utf-8") as f:
        data = json.load(f)
    coef = {m: (None, None, None, 0.0) if c.get("free")
            else (c["fresh"], c["cached"], c["output"], c["request"])
            for m, c in data["models"].items()}
    alts = [m for m, c in data["models"].items() if c.get("counterfactual", True)]
    return coef, coef[data["fallback"]], alts


COEF, DEFAULT_COEF, COUNTERFACTUAL = _load_coef()   # COUNTERFACTUAL：参加「全用一个模型」对照的模型
UNCERTAIN = {"gpt-6-astra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.6-terra"}

# 判定缓存失效：上一次请求的上下文至少这么大，这次却有一半以上按新增输入计
MISS_CONTEXT = 30_000


def cost_pct(fresh, outside, requests, model, cached=0):
    f, ca, o, r = COEF.get(model, DEFAULT_COEF)
    if f is None:
        return 0.0
    return fresh / f + cached / ca + outside / o + r * requests

def effective_mult(model):
    """相对 sol 的等效倍数只在给定构成下才有意义，这里给个粗略参考。"""
    f, _ca, o, r = COEF.get(model, DEFAULT_COEF)
    if f is None:
        return 0.0
    # 故意不含缓存项：这里只想给个「同样一次调用大概贵几倍」的粗略参考，
    # 而缓存占比完全取决于会话有多长，放进来反而会让这个数更没有意义。
    return (8000 / f + 500 / o + r) / (8000 / 41_398 + 500 / 15_450 + 0.0667)

# ── 日志解析 ─────────────────────────────────────────────────────────────
def parse(path):
    out = {"path": path, "model": None, "effort": None, "cwd": None,
           "fresh": 0, "cached": 0, "output": 0, "reasoning": 0,
           "requests": 0, "first": None, "last": None, "quota": {}, "quota_ts": "",
           "events": [], "readings": []}
    last_context = 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in fh:
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload") or {}
        t = p.get("type") or d.get("type")
        ts = d.get("timestamp") or ""
        if t == "session_meta":
            out["cwd"] = out["cwd"] or p.get("cwd")
        elif t == "turn_context":
            out["model"] = p.get("model") or out["model"]
            cm = (p.get("collaboration_mode") or {}).get("settings") or {}
            out["effort"] = cm.get("reasoning_effort") or out["effort"]
        elif t == "token_count":
            # 额度读数要先收，且与有没有用量无关 —— 只带 rate_limits 的事件
            # 往往正是最新的一条读数，丢了它会把额度读成陈旧值。
            rl = p.get("rate_limits") or {}
            # 只认 Codex 自己的额度读数；其它限额（base_model_inference / premium）读数
            # 忽略、用量照算 —— 原因见 Sources/Budget.swift
            if rl.get("limit_id") not in (None, "codex"):
                rl = {}
            for slot in ("primary", "secondary"):
                sl = rl.get(slot) or {}
                if sl.get("window_minutes") and sl.get("used_percent") is not None:
                    if ts >= out["quota_ts"]:
                        out["quota"][sl["window_minutes"]] = {
                            "used_percent": sl["used_percent"],
                            "resets_at": sl.get("resets_at")}
            if rl.get("primary") and ts > out["quota_ts"]:
                out["quota_ts"] = ts
            # 同时按事件保留完整读数：额度要按「桶」分，见 live_quota()
            wins = {}
            for slot in ("primary", "secondary"):
                sl = rl.get(slot) or {}
                if sl.get("window_minutes") and sl.get("used_percent") is not None:
                    wins[sl["window_minutes"]] = {"used_percent": sl["used_percent"],
                                                  "resets_at": sl.get("resets_at")}
            if wins and ts:
                out["readings"].append((ts, out["model"] or "?", wins))
            has5h = WIN_5H in wins
            weekly_reset = (wins.get(WIN_WEEK) or {}).get("resets_at")

            # info 为空的 token_count 不是一次真实请求（会话启动、额度刷新都会
            # 写这么一条）。照计的话每条白加一次"每请求固定成本" —— sol 上是
            # 0.0667%，一个 5h 窗口里混进十几条就是约 1% 的虚高。
            u = (p.get("info") or {}).get("last_token_usage") or {}
            if not u:
                continue
            inp = u.get("input_tokens", 0) or 0
            cch = u.get("cached_input_tokens", 0) or 0
            # 缓存失效后重读的老内容按缓存计价，规则见 Sources/Budget.swift
            fresh, cached = max(0, inp - cch), cch
            if last_context >= MISS_CONTEXT and inp > 0 and fresh >= inp / 2:
                cached, fresh = cached + fresh, 0
            last_context = inp
            out["events"].append({
                "ts": ts,
                "fresh": fresh,
                "cached": cached,
                "output": u.get("output_tokens", 0) or 0,
                "reasoning": u.get("reasoning_output_tokens", 0) or 0,
                "model": out["model"],
                "has5h": has5h,
                "weekly_reset": weekly_reset,
            })
            out["fresh"]     += max(0, inp - cch)
            out["cached"]    += cch
            out["output"]    += u.get("output_tokens", 0) or 0
            out["reasoning"] += u.get("reasoning_output_tokens", 0) or 0
            out["requests"]  += 1
            if ts:
                out["first"] = out["first"] or ts
                out["last"] = ts
    fh.close()
    # 只有额度读数、没有用量的文件也要留下 —— live_quota() 要用
    return out if (out["requests"] or out["quota"]) else None

def recent_files(limit=40):
    return sorted(glob.glob(SESS_GLOB, recursive=True),
                  key=lambda f: os.path.getmtime(f), reverse=True)[:limit]

def _same_bucket(a, b):
    """两个周窗口是不是同一个桶：按重置时间认，相差一小时以内算同一个。"""
    return a is not None and b is not None and abs(a - b) < 3600

def live_quota():
    """最新额度读数，按「桶」分。

    不能只取时间上最新的一条：主周额度打满后 Codex 会切到 gpt-reserve 这类
    模型，它的 rate_limits 同样是 limit_id=codex，报的却是另一个周窗口（重置
    时间不同、没有 5 小时窗口）。只取最新一条，备用池会盖掉主池。

    返回 (主池 {窗口: 读数}, 读数时间, {桶: 独立池}, 主周窗口的重置时间)。
    """
    readings = []
    for f in recent_files(15):
        s = parse(f)
        if s:
            readings.extend(s["readings"])
    readings.sort(key=lambda r: r[0])
    now = time.time()
    main5 = next((r for r in reversed(readings) if WIN_5H in r[2]), None)
    main_reset = (main5[2].get(WIN_WEEK) or {}).get("resets_at") if main5 else None
    if main_reset is None:          # 整段时期不报 5 小时窗口时，按最新周读数认主池
        wk = next((r for r in reversed(readings) if WIN_WEEK in r[2]), None)
        main_reset = wk[2][WIN_WEEK].get("resets_at") if wk else None
    quota = {}
    if main5:
        quota[WIN_5H] = dict(main5[2][WIN_5H])
    mw = next((r for r in reversed(readings)
               if _same_bucket((r[2].get(WIN_WEEK) or {}).get("resets_at"), main_reset)), None)
    if mw:
        quota[WIN_WEEK] = dict(mw[2][WIN_WEEK])
    pools = {}
    for ts, m, wins in readings:
        wk = wins.get(WIN_WEEK)
        ra = (wk or {}).get("resets_at")
        if not wk or WIN_5H in wins or not ra or ra <= now or _same_bucket(ra, main_reset):
            continue
        pool = pools.setdefault(int(ra // 3600), {"models": set(), "window": None})
        pool["models"].add(m)
        pool["window"] = dict(wk)
    for v in quota.values():
        ra = v.get("resets_at")
        if ra and ra < now:
            v["used_percent"] = 0.0
            v["stale"] = True
    quota_ts = readings[-1][0] if readings else ""
    return quota, quota_ts, pools, main_reset

def window_slice(quota=None):
    """当前 5h 窗口的起点 = 现在往前 5 小时。

    这是滚动窗口，不是到点清零的固定窗口 —— 历史数据里出现过 43 分钟内
    从 84% 掉到 0%，固定窗口做不到，滚动窗口在一批集中用量整体过期时可以。
    所以「这个会话累计花了多少」是没意义的数字，必须按滚动窗口切片。
    """
    start = time.time() - WIN_5H * 60
    return datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

HINT_MIN_CONTEXT = 30_000


def last_request_info(latest):
    """最近一次请求的缓存失效提示，规则与 Sources/Budget.swift 的 LastRequest 一致。

    空闲越久越容易失效：实测 10–30 分钟约四分之一，超过 1 小时约九成。上下文小、重读
    不贵，或者空闲超过半天，都不提示。
    """
    if not latest or not latest.get("ts"):
        return None
    try:
        ts = datetime.fromisoformat(latest["ts"].replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    idle = (time.time() - ts) / 60
    miss = cost_pct(latest["context"], 0, 1, latest["model"], 0)
    hit = cost_pct(0, 0, 1, latest["model"], latest["context"])
    hint = None
    if latest["context"] >= HINT_MIN_CONTEXT and miss >= 0.5:
        if 60 <= idle <= 12 * 60:
            hint = "likely"
        elif 10 <= idle < 60:
            hint = "maybe"
    return {"model": latest["model"], "context": latest["context"], "idle_minutes": idle,
            "resume_miss_pct": miss, "resume_hit_pct": hit, "cache_hint": hint}


def print_cache_hint(info):
    if not info or not info["cache_hint"]:
        return
    m = int(info["idle_minutes"])
    idle = f"{m // 60} 小时 {m % 60} 分钟" if m >= 60 else f"{m} 分钟"
    head = "缓存大概率已失效" if info["cache_hint"] == "likely" else "缓存可能已失效"
    print(f"\n  \033[33m{head}\033[0m  \033[2m上次请求在 {idle}前 · 上下文 {info['context']:,} tok\033[0m")
    print(f"    接着这段会话约花 {info['resume_miss_pct']:.1f}%"
          f"\033[2m（缓存还在只要 {info['resume_hit_pct']:.1f}%）\033[0m\n")


def collect_window(since_iso, limit=60, main_reset=None, pools=None):
    """当前窗口内所有会话的事件，按模型分组。"""
    per_model = {}
    total = {"fresh": 0, "cached": 0, "outside": 0, "requests": 0}
    sessions = set()
    newest = ("", None)          # (时间戳, 模型) —— 用来判断"你现在用的是哪个"
    pool_req = {}                # 走独立额度池的请求数，不算进主池估算
    latest = None                # 最近一次请求，不受窗口限制：用来提示缓存可能已失效
    for f in recent_files(limit):
        s = parse(f)
        if not s:
            continue
        for e in s["events"]:
            if e["ts"] and (latest is None or e["ts"] > latest["ts"]):
                latest = {"ts": e["ts"], "model": e["model"] or "?",
                          "context": e["fresh"] + e["cached"]}
            if since_iso and e["ts"] and e["ts"] < since_iso:
                continue
            m = e["model"] or "?"
            ra = e.get("weekly_reset")
            if (pools and not e.get("has5h") and ra and not _same_bucket(ra, main_reset)
                    and int(ra // 3600) in pools):
                pool_req[int(ra // 3600)] = pool_req.get(int(ra // 3600), 0) + 1
                if e["ts"] > newest[0]:
                    newest = (e["ts"], m)
                continue
            d = per_model.setdefault(m, {"fresh": 0, "cached": 0, "outside": 0, "requests": 0})
            outside = e["output"] + e["reasoning"]
            for k, v in (("fresh", e["fresh"]), ("cached", e["cached"]),
                         ("outside", outside), ("requests", 1)):
                d[k] += v
                total[k] += v
            sessions.add(f)
            if e["ts"] > newest[0]:
                newest = (e["ts"], m)
    return per_model, total, len(sessions), newest[1], pool_req, latest

def bar(pct, width=22):
    fill = int(round(min(100.0, max(0.0, pct)) / 100 * width))
    return "█" * fill + "─" * (width - fill)

def human_mins(m):
    if m <= 0:
        return "已到期"
    m = int(m)
    if m >= 2880:                    # 超过两天，按天显示更好读
        d, h = divmod(m // 60, 24)
        return f"{d}天{h}小时" if h else f"{d}天"
    h, mm = divmod(m, 60)
    return f"{h}h{mm:02d}m" if h else f"{mm}m"

def fmt_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except Exception:
        return "?"

# ── 报告 ─────────────────────────────────────────────────────────────────
def report(per_model, total, n_sessions, quota, quota_ts, since_iso, current_model=None,
           pools=None, pool_req=None):
    cost = {m: cost_pct(d["fresh"], d["outside"], d["requests"], m, d["cached"])
            for m, d in per_model.items()}
    spent = sum(cost.values())
    def comp(idx):
        t = 0.0
        for m, d in per_model.items():
            f, ca, o, r = COEF.get(m, DEFAULT_COEF)
            if f is None:
                continue
            t += (d["fresh"] / f, d["cached"] / ca, d["outside"] / o,
                  r * d["requests"])[idx]
        return t
    c_fresh, c_cached, c_out, c_req = comp(0), comp(1), comp(2), comp(3)

    start = fmt_ts(since_iso) if since_iso else "?"
    cm = f"  \033[1m{current_model}\033[0m" if current_model else ""
    print(f"\n\033[1m当前 5 小时窗口\033[0m{cm}  \033[2m自 {start} 起 · {n_sessions} 个会话\033[0m")
    print(f"\n  这个窗口里你花掉 \033[1m{spent:.1f}%\033[0m 额度（{total['requests']} 次请求）")
    if spent > 0:
        for name, c, detail in (
            ("新读进来的内容", c_fresh, f"{total['fresh']:,} tok"),
            ("缓存输入",     c_cached, f"{total['cached']:,} tok"),
            ("模型写出来的",   c_out,   f"{total['outside']:,} tok"),
            ("每次请求的底价", c_req,   f"{total['requests']} 次 × {c_req/max(1, total['requests']):.3f}%"),
        ):
            print(f"    {name:<8} {c:>6.1f}%  {bar(c/spent*100, 16)}  \033[2m{detail}\033[0m")
    if total["cached"]:
        print(f"    \033[2m缓存按 fresh 的约 1/{round(COEF['gpt-5.6-sol'][1]/COEF['gpt-5.6-sol'][0])} 计价"
              f"（若按 fresh 全价要 {total['cached']/COEF['gpt-5.6-sol'][0]:.0f}%）\033[0m")
    if spent and c_req / spent > 0.35 and total["requests"] > 10:
        print(f"    \033[33m⚠ 底价占了 {c_req/spent*100:.0f}%：请求太碎，合并成更少轮次能直接省\033[0m")

    q5 = (quota.get(WIN_5H) or {}).get("used_percent")
    if q5 is not None:
        gap = q5 - spent
        if abs(gap) > max(4.0, q5 * 0.2):
            print(f"\n  \033[33m估算偏差 {gap:+.0f}%\033[0m  "
                  f"\033[2m服务端 {q5:.0f}% vs 估算 {spent:.1f}% —— "
                  f"以服务端为准\033[0m")
            print(f"    \033[2m偏差主要来自模型乘数不准（astra 尤其），不是有别的东西在偷额度\033[0m")

    if len(per_model) > 1:
        print(f"\n\033[1m  按模型\033[0m")
        for m, c in sorted(cost.items(), key=lambda kv: -kv[1]):
            d = per_model[m]
            print(f"    {m:<16} {c:>6.1f}%  \033[2m{d['requests']} 次请求\033[0m")

    tf = sum(d["fresh"] for d in per_model.values())
    to = sum(d["outside"] for d in per_model.values())
    tn = sum(d["requests"] for d in per_model.values())
    tc = sum(d["cached"] for d in per_model.values())
    alt = {m: cost_pct(tf, to, tn, m, tc) for m in COUNTERFACTUAL}
    print(f"\n\033[1m  同样这些活，全用一个模型的话\033[0m")
    for m, v in sorted(alt.items(), key=lambda kv: kv[1]):
        cur = "  \033[1m← 你现在用的\033[0m" if m == current_model else ""
        print(f"    {m:<16} {v:>6.1f}%{cur}")

    print(f"\n\033[1m  额度\033[0m  \033[2m读数于 {fmt_ts(quota_ts)}\033[0m")
    for w, name in ((WIN_5H, "5小时"), (WIN_WEEK, "每周")):
        q = quota.get(w)
        if not q:
            continue
        used = q["used_percent"]
        tag = " \033[2m(已重置)\033[0m" if q.get("stale") else ""
        left = ""
        if q.get("resets_at"):
            mins = (q["resets_at"] - time.time()) / 60
            if mins > 0:
                left = f"  \033[2m{human_mins(mins)}后重置\033[0m"
        print(f"    {name:<6} {bar(used)} {used:>3.0f}%{left}{tag}")
    for k, pool in sorted((pools or {}).items()):
        w = pool["window"]; label = "/".join(sorted(pool["models"]))
        mins = (w["resets_at"] - time.time()) / 60 if w.get("resets_at") else 0
        n = (pool_req or {}).get(k, 0)
        print(f"    {label + ' 周额度':<12} {bar(w['used_percent'])} {w['used_percent']:>3.0f}%"
              f"  \033[2m{human_mins(mins)}后重置 · 独立额度池\033[0m")
        if n:
            print(f"    \033[2m另有 {n} 次请求走 {label} 的独立额度，没算进上面的 5 小时估算\033[0m")
    print()

def main():
    ap = argparse.ArgumentParser(prog="codex-budget",
        description="当前 5 小时窗口花了多少额度，钱花在哪，换个模型会花多少")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--files", type=int, default=60, help="扫描最近多少个会话文件")
    ap.add_argument("--version", action="version", version=f"codex-budget {__version__}")
    args = ap.parse_args()

    quota, quota_ts, pools, main_reset = live_quota()
    since = window_slice(quota)
    per_model, total, n_sessions, current_model, pool_req, latest = collect_window(
        since, args.files, main_reset, pools)
    if not per_model:
        if args.json:
            print(json.dumps({"window_start": since, "current_model": None,
                              "requests": 0, "spent_pct": 0.0,
                              "quota": {str(k): v for k, v in quota.items()},
                              "pools": [{"label": "/".join(sorted(v["models"])),
                                         "window": v["window"], "requests": 0}
                                        for k, v in sorted(pools.items())],
                              "last_request": last_request_info(latest)},
                             ensure_ascii=False, indent=2))
        else:
            print("\n  当前窗口还没有任何活动。\n")
            print_cache_hint(last_request_info(latest))
        return 0

    if args.json:
        print(json.dumps({
            "window_start": since,
            "current_model": current_model,
            "sessions": n_sessions,
            "fresh": total["fresh"], "cached": total["cached"],
            "output_side": total["outside"], "requests": total["requests"],
            "spent_pct": sum(cost_pct(d["fresh"], d["outside"], d["requests"], m,
                                      d["cached"])
                             for m, d in per_model.items()),
            "by_component": {
                "fresh": sum(d["fresh"] / (COEF.get(m, DEFAULT_COEF)[0] or 1e18)
                             for m, d in per_model.items()),
                "cached": sum(d["cached"] / (COEF.get(m, DEFAULT_COEF)[1] or 1e18)
                              for m, d in per_model.items()),
                "output": sum(d["outside"] / (COEF.get(m, DEFAULT_COEF)[2] or 1e18)
                              for m, d in per_model.items()),
                "requests": sum(COEF.get(m, DEFAULT_COEF)[3] * d["requests"]
                                for m, d in per_model.items()),
            },
            "by_model": {m: {"requests": d["requests"],
                             "pct": cost_pct(d["fresh"], d["outside"], d["requests"], m,
                                             d["cached"])}
                         for m, d in per_model.items()},
            "counterfactual": {m: cost_pct(sum(d["fresh"] for d in per_model.values()),
                                           sum(d["outside"] for d in per_model.values()),
                                           sum(d["requests"] for d in per_model.values()), m,
                                           sum(d["cached"] for d in per_model.values()))
                               for m in COUNTERFACTUAL},
            "quota": {str(k): v for k, v in quota.items()},
            "quota_read_at": quota_ts,
            "last_request": last_request_info(latest),
            "pools": [{"label": "/".join(sorted(v["models"])), "window": v["window"],
                       "requests": pool_req.get(k, 0)} for k, v in sorted(pools.items())],
        }, ensure_ascii=False, indent=2))
        return 0

    report(per_model, total, n_sessions, quota, quota_ts, since, current_model,
           pools, pool_req)
    print_cache_hint(last_request_info(latest))
    return 0

if __name__ == "__main__":
    sys.exit(main())
