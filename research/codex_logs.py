#!/usr/bin/env python3
"""读本机 Codex 会话日志，供离线分析用：真实会话验证集（validate_real.py）和
省额度回测（backtest_routing.py）。

只读 ~/.codex（设了 CODEX_HOME 就读它），不写任何东西。调用方只输出聚合数字，
不输出提示词原文和本机路径。零依赖，Python 3.8+。
"""
from __future__ import annotations
import glob, importlib.util, json, os
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_5H, WIN_WEEK = 300, 10080
# 受控实验和 Luna 判定开在这些目录里；它们已经在拟合数据里，不算真实使用
SANDBOX_MARKERS = ("quota-probe", "codex-cost-judge", "research/sandbox")
FREE = {"gpt-5.6-luna"}


def codex_home() -> str:
    v = os.environ.get("CODEX_HOME") or ""
    return os.path.expanduser(v) if v else os.path.expanduser("~/.codex")


def parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Request:
    __slots__ = ("ts", "model", "fresh", "cached", "output", "kind", "session", "reading")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    @property
    def total(self):
        return self.fresh + self.cached + self.output


class Session:
    def __init__(self, sid, kind):
        self.sid, self.kind = sid, kind
        self.first_prompt = None
        self.requests = []


def _user_text(p):
    c = p.get("content")
    if isinstance(c, list):
        return " ".join((x.get("text") or "") for x in c if isinstance(x, dict)).strip()
    return ""


def _reading(rl):
    """只认 Codex 自己的额度读数（limit_id 为 codex 或缺省），原因见 Sources/Budget.swift"""
    if not rl or rl.get("limit_id") not in (None, "codex"):
        return None
    out = {}
    for slot in ("primary", "secondary"):
        w = rl.get(slot) or {}
        if w.get("window_minutes") in (WIN_5H, WIN_WEEK) and w.get("used_percent") is not None:
            out[w["window_minutes"]] = (float(w["used_percent"]), w.get("resets_at"))
    return out or None


def load(home=None):
    """返回 (sessions, requests)。requests 全局按时间排序并去重：分叉、归档的会话会原样复制事件。"""
    home = home or codex_home()
    files = sorted(glob.glob(os.path.join(home, "sessions", "**", "rollout-*.jsonl"), recursive=True)) \
        + sorted(glob.glob(os.path.join(home, "archived_sessions", "**", "*.jsonl"), recursive=True))
    sessions, seen = [], set()
    for f in files:
        s, model, first_model, pending = None, None, None, []
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if ('"session_meta"' not in line and '"turn_context"' not in line
                        and '"token_count"' not in line and '"user"' not in line):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                t = p.get("type") or d.get("type")
                if t == "session_meta" and s is None:
                    cwd = p.get("cwd") or ""
                    kind = ("sandbox" if any(m in cwd for m in SANDBOX_MARKERS)
                            else "subagent" if (p.get("parent_thread_id") or p.get("agent_nickname"))
                            else "normal")
                    s = Session(p.get("id") or p.get("session_id") or os.path.basename(f), kind)
                elif t == "turn_context":
                    model = p.get("model") or model
                    first_model = first_model or model
                elif t == "message" and p.get("role") == "user":
                    if s is not None and s.first_prompt is None:
                        txt = _user_text(p)
                        if txt and not txt.startswith("<"):
                            s.first_prompt = txt
                elif t == "token_count":
                    u = (p.get("info") or {}).get("last_token_usage") or {}
                    ts = parse_ts(d.get("timestamp"))
                    if not u or ts is None:
                        continue
                    i, c = u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0
                    key = (d.get("timestamp"), i, c, u.get("output_tokens"), u.get("reasoning_output_tokens"))
                    if key in seen:
                        continue
                    seen.add(key)
                    pending.append(Request(
                        ts=ts, model=model, fresh=max(0, i - c), cached=c,
                        output=(u.get("output_tokens") or 0) + (u.get("reasoning_output_tokens") or 0),
                        kind=None, session=None, reading=_reading(p.get("rate_limits"))))
        s = s or Session(os.path.basename(f), "normal")
        for r in pending:
            r.model = r.model or first_model or "?"
            r.kind, r.session = s.kind, s.sid
        s.requests = pending
        sessions.append(s)
    requests = sorted((r for s in sessions for r in s.requests), key=lambda r: r.ts)
    return sessions, requests


def load_coefficients():
    """系数取 refit.py 里的 SHIPPED，和 app 发布的一致"""
    spec = importlib.util.spec_from_file_location("refit", os.path.join(HERE, "refit.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return dict(m.SHIPPED)


def cost(r, coef, model=None):
    """一次请求折合多少 5 小时额度（%）；没有系数的模型返回 None"""
    model = model or r.model
    if model in FREE:
        return 0.0
    c = coef.get(model)
    if not c or any(x is None for x in c[:3]):
        return None
    f, ca, o, req = c
    return r.fresh / f + r.cached / ca + r.output / o + req


def _same(a, b, tol=300):
    return a is not None and b is not None and abs(a - b) <= tol


def clean_segments(requests, coef, max_gap=7200, min_requests=5, min_delta=8, cap=95):
    """真实使用里能拿来和模型核对的片段。

    条件：不混实验沙箱的请求；每个请求的模型都有系数；相邻读数间隔不超过 max_gap；
    5 小时读数不回落、重置时间不变（窗口没有滚动或重置）；读数跨度 ≥ min_delta
    （整数读数的量化误差才压得住）；最高读数 < cap（没有撞上限被截断）。
    片段首尾都必须带读数；中间偶尔缺读数的请求照算用量。
    """
    segs, cur = [], []

    def close():
        nonlocal cur
        while cur and not (cur[-1].reading and WIN_5H in cur[-1].reading):
            cur.pop()
        if len(cur) >= min_requests:
            segs.append(cur)
        cur = []

    for r in requests:
        if r.kind == "sandbox" or cost(r, coef) is None:
            close()
            continue
        rd = r.reading if (r.reading and WIN_5H in r.reading) else None
        if not cur:
            if rd:
                cur = [r]
            continue
        last = next(x for x in reversed(cur) if x.reading and WIN_5H in x.reading)
        if r.ts - cur[-1].ts > max_gap:
            close()
            if rd:
                cur = [r]
            continue
        if rd:
            p_prev, reset_prev = last.reading[WIN_5H]
            p_now, reset_now = rd[WIN_5H]
            if p_now < p_prev or not _same(reset_prev, reset_now):
                close()
                cur = [r]
                continue
        cur.append(r)
    close()
    return [s for s in segs
            if s[-1].reading[WIN_5H][0] - s[0].reading[WIN_5H][0] >= min_delta
            and max(x.reading[WIN_5H][0] for x in s if x.reading and WIN_5H in x.reading) < cap]


def weekly_ratio(requests, min_delta=5):
    """周额度 1% 相当于多少 5 小时额度：按（5 小时窗口，周窗口）分组，取读数跨度之比。

    包括实验沙箱 —— 这里只看两个读数怎么同涨，和谁花的无关。
    """
    groups = defaultdict(list)
    for r in requests:
        rd = r.reading
        if rd and WIN_5H in rd and WIN_WEEK in rd and rd[WIN_5H][1] and rd[WIN_WEEK][1]:
            groups[(round(rd[WIN_5H][1] / 600), round(rd[WIN_WEEK][1] / 600))].append(rd)
    d5 = dw = 0.0
    for rds in groups.values():
        a = max(x[WIN_5H][0] for x in rds) - min(x[WIN_5H][0] for x in rds)
        b = max(x[WIN_WEEK][0] for x in rds) - min(x[WIN_WEEK][0] for x in rds)
        if a >= min_delta:
            d5 += a
            dw += b
    return (d5 / dw) if dw > 0 else None
