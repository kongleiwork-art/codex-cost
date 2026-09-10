#!/usr/bin/env python3
"""codex-budget — 你这个会话花了多少额度，换个模型会花多少。

现有的用量工具都在回答"我用掉了多少"。这个回答两个它们不答的问题：
  · 这笔花费是怎么构成的（新输入 / 输出 / 每请求底价）
  · 同样这些活，换成别的模型要花多少 —— 反事实计算

成本模型来自受控实验（22 组、约 300 次调用、R² 0.987）：

    Δ5h% = M(模型) × [ fresh_input/41,398 + 输出侧/15,450 + 0.0667 × 请求数 ]

其中「输出侧」= output + reasoning，缓存输入完全不计费。

零依赖，标准库，Python 3.8+。只读本地日志，不上传任何数据。
"""
from __future__ import annotations
import argparse, glob, json, os, sys, time
from datetime import datetime, timezone

__version__ = "0.1.0"
HOME = os.path.expanduser("~")
SESS_GLOB = os.path.join(HOME, ".codex/sessions/**/rollout-*.jsonl")
WIN_5H, WIN_WEEK = 300, 10080

# ── 成本模型 ─────────────────────────────────────────────────────────────
# 每个模型三个系数：新增输入、输出侧（output+reasoning）、每次请求的固定成本。
#
# 为什么不能用「一个乘数乘以基准公式」：astra 的三个成分相对 sol 分别是
# 2.7× / 34× / 5.3× —— 差一个数量级。用单一乘数时，等效倍数完全取决于
# 调用的构成（说话多不多），实测在 3.45× 到 11× 之间飘，怎么都对不上。
#
# 数据来源：
#   sol   —— 22 组受控实验，按次归一化回归 R² 0.987
#   astra —— fresh/请求 来自 41 次纯 astra、未打满窗口的累积回归；
#            输出侧来自 25 次纯 astra + 强制长输出、未打满（输出占成本 46%，
#            敏感性区间 2,100~3,072）。
#            注：早前用两个"已打满"窗口做残差得到 461 tok/1%（"贵 34 倍"），
#            与干净测量差 5 倍。那两个窗口跑的是 astra 各 effort 档位 ——
#            怀疑 effort 在 astra 上有独立乘数（sol 上没有），未验证。
#   5.5 / terra —— 只测到整体乘数，误差 ±0.11，按比例缩放 sol 的系数
#   luna  —— 30 次调用零消耗
FRESH_PER_PCT = 41_398      # sol 基准：1% 额度能买多少 fresh 输入 token
OUT_PER_PCT   = 15_450
PER_REQUEST   = 0.0667

# (每 1% 的 fresh token 数, 每 1% 的输出侧 token 数, 每次请求成本%)
COEF = {
    "gpt-5.6-sol":   (41_398, 15_450, 0.0667),
    "gpt-5.5":       (48_137, 17_965, 0.0574),   # 由 0.86× 缩放
    "gpt-5.6-terra": (46_000, 17_167, 0.0600),   # 由 0.90× 缩放
    "gpt-5.6-luna":  (None,   None,   0.0),      # 不计费
    "gpt-6-astra":   (15_415,  2_495, 0.3514),   # 输出侧见下
}
DEFAULT_COEF = COEF["gpt-5.6-sol"]
UNCERTAIN = {"gpt-6-astra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.6-terra"}

def cost_pct(fresh, outside, requests, model, cached=0):
    f, ca, o, r = COEF.get(model, DEFAULT_COEF)
    if f is None:
        return 0.0
    return fresh / f + cached / ca + outside / o + r * requests

def effective_mult(model):
    """相对 sol 的等效倍数只在给定构成下才有意义，这里给个粗略参考。"""
    f, o, r = COEF.get(model, DEFAULT_COEF)
    if f is None:
        return 0.0
    return (8000 / f + 500 / o + r) / (8000 / 41_398 + 500 / 15_450 + 0.0667)

# ── 日志解析 ─────────────────────────────────────────────────────────────
def parse(path):
    out = {"path": path, "model": None, "effort": None, "cwd": None,
           "fresh": 0, "cached": 0, "output": 0, "reasoning": 0,
           "requests": 0, "first": None, "last": None, "quota": {}, "quota_ts": "",
           "events": []}
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
            u = (p.get("info") or {}).get("last_token_usage") or {}
            inp = u.get("input_tokens", 0) or 0
            cch = u.get("cached_input_tokens", 0) or 0
            out["events"].append({
                "ts": ts,
                "fresh": max(0, inp - cch),
                "cached": cch,
                "output": u.get("output_tokens", 0) or 0,
                "reasoning": u.get("reasoning_output_tokens", 0) or 0,
                "model": out["model"],
            })
            out["fresh"]     += max(0, inp - cch)
            out["cached"]    += cch
            out["output"]    += u.get("output_tokens", 0) or 0
            out["reasoning"] += u.get("reasoning_output_tokens", 0) or 0
            out["requests"]  += 1
            if ts:
                out["first"] = out["first"] or ts
                out["last"] = ts
            rl = p.get("rate_limits") or {}
            for slot in ("primary", "secondary"):
                s = rl.get(slot) or {}
                if s.get("window_minutes") and s.get("used_percent") is not None:
                    if ts >= out["quota_ts"]:
                        out["quota"][s["window_minutes"]] = {
                            "used_percent": s["used_percent"],
                            "resets_at": s.get("resets_at")}
            if rl.get("primary") and ts > out["quota_ts"]:
                out["quota_ts"] = ts
    fh.close()
    return out if out["requests"] else None

def recent_files(limit=40):
    return sorted(glob.glob(SESS_GLOB, recursive=True),
                  key=lambda f: os.path.getmtime(f), reverse=True)[:limit]

def live_quota():
    """最新一次额度读数。日志只在 Codex 发请求时更新，所以要处理两件事：
    取事件时间戳最大的那条（不是文件 mtime 最新的），以及 resets_at
    已过期时判定窗口已滚动 —— 否则会把早已重置的窗口读成用满。"""
    best, best_ts = {}, ""
    for f in recent_files(15):
        s = parse(f)
        if s and s["quota"] and s["quota_ts"] > best_ts:
            best, best_ts = s["quota"], s["quota_ts"]
    now = time.time()
    for w, v in best.items():
        ra = v.get("resets_at")
        if ra and ra < now:
            v["used_percent"] = 0.0
            v["stale"] = True
    return best, best_ts

def window_slice(quota=None):
    """当前 5h 窗口的起点 = 现在往前 5 小时。

    这是滚动窗口，不是到点清零的固定窗口 —— 历史数据里出现过 43 分钟内
    从 84% 掉到 0%，固定窗口做不到，滚动窗口在一批集中用量整体过期时可以。
    所以「这个会话累计花了多少」是没意义的数字，必须按滚动窗口切片。
    """
    start = time.time() - WIN_5H * 60
    return datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def collect_window(since_iso, limit=60):
    """当前窗口内所有会话的事件，按模型分组。"""
    per_model = {}
    total = {"fresh": 0, "cached": 0, "outside": 0, "requests": 0}
    sessions = set()
    newest = ("", None)          # (时间戳, 模型) —— 用来判断"你现在用的是哪个"
    for f in recent_files(limit):
        s = parse(f)
        if not s:
            continue
        for e in s["events"]:
            if since_iso and e["ts"] and e["ts"] < since_iso:
                continue
            m = e["model"] or "?"
            d = per_model.setdefault(m, {"fresh": 0, "cached": 0, "outside": 0, "requests": 0})
            outside = e["output"] + e["reasoning"]
            for k, v in (("fresh", e["fresh"]), ("cached", e["cached"]),
                         ("outside", outside), ("requests", 1)):
                d[k] += v
                total[k] += v
            sessions.add(f)
            if e["ts"] > newest[0]:
                newest = (e["ts"], m)
    return per_model, total, len(sessions), newest[1]

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
def report(per_model, total, n_sessions, quota, quota_ts, since_iso, current_model=None):
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
            ("每次请求的底价", c_req,   f"{total['requests']} 次 × 0.067%"),
        ):
            print(f"    {name:<8} {c:>6.1f}%  {bar(c/spent*100, 16)}  \033[2m{detail}\033[0m")
    if total["cached"]:
        print(f"    \033[2m缓存按 fresh 的约 1/16 计价"
              f"（若按 fresh 全价要 {total['cached']/FRESH_PER_PCT:.0f}%）\033[0m")
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
    alt = {m: cost_pct(tf, to, tn, m, tc) for m in COEF}
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
    print()

def main():
    ap = argparse.ArgumentParser(prog="codex-budget",
        description="当前 5 小时窗口花了多少额度，钱花在哪，换个模型会花多少")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--files", type=int, default=60, help="扫描最近多少个会话文件")
    ap.add_argument("--version", action="version", version=f"codex-budget {__version__}")
    args = ap.parse_args()

    quota, quota_ts = live_quota()
    since = window_slice(quota)
    per_model, total, n_sessions, current_model = collect_window(since, args.files)
    if not per_model:
        if args.json:
            print(json.dumps({"window_start": since, "requests": 0, "spent_pct": 0.0,
                              "quota": {str(k): v for k, v in quota.items()}},
                             ensure_ascii=False, indent=2))
        else:
            print("\n  当前窗口还没有任何活动。\n")
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
                               for m in COEF},
            "quota": {str(k): v for k, v in quota.items()},
            "quota_read_at": quota_ts,
        }, ensure_ascii=False, indent=2))
        return 0

    report(per_model, total, n_sessions, quota, quota_ts, since, current_model)
    return 0

if __name__ == "__main__":
    sys.exit(main())
