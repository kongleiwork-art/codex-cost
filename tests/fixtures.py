#!/usr/bin/env python3
"""生成可复现的 Codex 会话日志样本。

回归测试（tests/test_quota.py）和 README 截图（docs/）都用它，不用你自己的
真实日志：真实日志不可复现，截图还会把个人用量印进公开仓库。

时间戳相对「现在」生成，所以不需要假时钟 —— 写完立刻用
`CODEX_HOME=<目录>/<场景>` 跑 app 或 CLI 即可：

    python3 tests/fixtures.py /tmp/cc-fixtures
    CODEX_HOME=/tmp/cc-fixtures/reserve ./codex-cost --dump

每个场景函数返回它的期望值，测试直接拿来断言。
"""
from __future__ import annotations
import json, os, shutil, sys, time, uuid
from datetime import datetime, timezone

H = 3600
MARKER = ".codex-cost-fixture"   # 只清理自己生成过的目录


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def window(minutes, used, resets_at):
    return {"used_percent": float(used), "window_minutes": minutes, "resets_at": int(resets_at)}


def ramp(a, b, i, n):
    """额度读数是整数，从 a 线性爬到 b"""
    return round(a + (b - a) * i / max(1, n - 1))


def usage(i, fresh=2_400, cached=38_000, output=420):
    """一次请求的用量。数字确定，但带点起伏，不至于每条都一样。"""
    c = cached + i * 900
    return {"input_tokens": fresh + (i * 37) % 500 + c,
            "cached_input_tokens": c,
            "output_tokens": output + (i * 53) % 300,
            "reasoning_output_tokens": (i * 97) % 200,
            "total_tokens": 0}


class Session:
    """一个 rollout 文件，按真实日志的形状写：session_meta、turn_context、token_count。"""

    def __init__(self, root, start, model, effort="medium"):
        day = datetime.fromtimestamp(start, timezone.utc)
        d = os.path.join(root, "sessions", day.strftime("%Y/%m/%d"))
        os.makedirs(d, exist_ok=True)
        sid = uuid.uuid5(uuid.NAMESPACE_URL, f"{model}-{start}")
        self.path = os.path.join(d, f"rollout-{day.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl")
        self.lines, self.last = [], start
        self.add(start, {"type": "session_meta", "payload": {"id": str(sid), "cwd": "/tmp/fixture"}})
        self.add(start, {"type": "turn_context",
                         "payload": {"model": model,
                                     "collaboration_mode": {"settings": {"reasoning_effort": effort}}}})

    def add(self, t, obj):
        self.lines.append(json.dumps({"timestamp": iso(t), **obj}))
        self.last = max(self.last, t)

    def tokens(self, t, use, limits, limit_id="codex"):
        """use=None 就是只带额度读数的空事件（会话恢复、额度刷新时 Codex 会写）"""
        self.add(t, {"type": "event_msg",
                     "payload": {"type": "token_count",
                                 "info": {"last_token_usage": use} if use else None,
                                 "rate_limits": {"limit_id": limit_id, **limits}}})

    def close(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines) + "\n")
        os.utime(self.path, (self.last, self.last))   # 扫描按修改时间挑最近的文件


# ── 场景 ────────────────────────────────────────────────────────────────

def normal(root, now):
    """日常：两个模型、两种窗口都有，中间夹一条空 info 的额度刷新（不能算一次请求）。
    README 的截图就用这个，所以 5 小时读数与模型估算（约 12%）保持一致 ——
    否则面板会亮出「估算偏差」的提示。"""
    r5, rw = now + 2.2 * H, now + 3.4 * 24 * H
    s, n = Session(root, now - 3.5 * H, "gpt-5.6-sol"), 36
    for i in range(n):
        t = now - 3.4 * H + i * 240
        lim = {"primary": window(300, ramp(1, 10, i, n), r5),
               "secondary": window(10080, ramp(38, 41, i, n), rw)}
        s.tokens(t, usage(i), lim)
        if i == 10:
            s.tokens(t + 5, None, lim)
    s.close()
    s, n2 = Session(root, now - 1.2 * H, "gpt-5.6-terra"), 14
    for i in range(n2):
        s.tokens(now - 1.1 * H + i * 270, usage(i, fresh=1_800, cached=22_000, output=300),
                 {"primary": window(300, ramp(10, 12, i, n2), r5),
                  "secondary": window(10080, ramp(41, 42, i, n2), rw)})
    s.close()
    return {"requests": 50, "five_hour": 12, "weekly": 42, "pools": [], "binding": 42,
            "current_model": "gpt-5.6-terra", "models": {"gpt-5.6-sol": 36, "gpt-5.6-terra": 14},
            "cache_hint": None}


def reserve(root, now):
    """主周额度打满，Codex 切到 gpt-reserve：同一个 limit_id，报的却是另一个周窗口，
    也没有 5 小时窗口。修复前它的读数会盖掉主池 —— 周额度显示成备用池的数，
    5 小时那行消失。"""
    r5, rw, rr = now + 3.5 * H, now + 3 * 24 * H, now + 39 * H
    s, n = Session(root, now - 4.2 * H, "gpt-5.6-sol"), 30
    for i in range(n):
        s.tokens(now - 4.1 * H + i * 300, usage(i),
                 {"primary": window(300, ramp(22, 41, i, n), r5),
                  "secondary": window(10080, ramp(97, 100, i, n), rw)})
    s.close()
    s, n2 = Session(root, now - 1.5 * H, "gpt-reserve"), 20
    for i in range(n2):
        s.tokens(now - 1.4 * H + i * 240, usage(i, fresh=1_500, cached=30_000, output=350),
                 {"primary": window(10080, ramp(40, 53, i, n2), rr)})
    s.close()
    return {"requests": 30, "five_hour": 41, "weekly": 100,
            "pools": [{"label": "gpt-reserve", "used": 53, "requests": 20}],
            "binding": 100, "current_model": "gpt-reserve", "models": {"gpt-5.6-sol": 30}}


def weekly_only(root, now):
    """整段时期只报周窗口、不报 5 小时窗口（7~8 月的 sol 就是这样）。
    这不是独立额度池：重置时间就是主池的，请求要照常计入。"""
    rw = now + 2 * 24 * H
    s, n = Session(root, now - 2 * H, "gpt-5.6-sol"), 15
    for i in range(n):
        s.tokens(now - 1.9 * H + i * 400, usage(i),
                 {"primary": window(10080, ramp(58, 62, i, n), rw)})
    s.close()
    return {"requests": 15, "five_hour": None, "weekly": 62, "pools": [], "binding": 62,
            "current_model": "gpt-5.6-sol", "models": {"gpt-5.6-sol": 15}}


def stale(root, now):
    """几小时没用：最后一条 5 小时读数的重置时间已经过了。日志只在发请求时更新，
    不修正的话会把早已滚动掉的窗口读成 80%。"""
    s, n = Session(root, now - 7.5 * H, "gpt-5.6-sol"), 10
    for i in range(n):
        s.tokens(now - 7.4 * H + i * 300, usage(i),
                 {"primary": window(300, ramp(60, 80, i, n), now - 2 * H),
                  "secondary": window(10080, 50, now + 4 * 24 * H)})
    s.close()
    return {"requests": 0, "five_hour": 0, "five_hour_stale": True, "weekly": 50,
            "pools": [], "binding": 50, "current_model": None, "models": {}}


def expired_pool(root, now):
    """备用池的周窗口已经重置过：它的旧读数不该再显示成一个池子。"""
    s = Session(root, now - 30 * H, "gpt-reserve")
    for i in range(5):
        s.tokens(now - 29 * H + i * 300, usage(i),
                 {"primary": window(10080, 70, now - 1 * H)})
    s.close()
    r5, rw = now + 4 * H, now + 5 * 24 * H
    s, n = Session(root, now - 1 * H, "gpt-5.6-sol"), 8
    for i in range(n):
        s.tokens(now - 0.9 * H + i * 300, usage(i),
                 {"primary": window(300, ramp(2, 6, i, n), r5),
                  "secondary": window(10080, 12, rw)})
    s.close()
    return {"requests": 8, "five_hour": 6, "weekly": 12, "pools": [], "binding": 12,
            "current_model": "gpt-5.6-sol", "models": {"gpt-5.6-sol": 8}}


def other_limit(root, now):
    """同一个会话里夹着 limit_id 不是 codex 的读数（09-14 实测：base_model_inference，
    limit_name 为 gpt-reserve，周窗口 0%、重置时间总在事件 7 天后；premium 连窗口都没有）。
    它们不是独立额度池 —— 主池 5 小时读数照样跟着这些请求涨 —— 读数忽略，用量照算。
    修复前每条都被当成一个新池子，请求被排除在 5 小时估算之外。"""
    r5, rw = now + 3 * H, now + 5 * 24 * H
    s, n = Session(root, now - 2 * H, "gpt-5.6-sol"), 20
    for i in range(n):
        t = now - 1.9 * H + i * 300
        if i % 5 == 2:
            s.tokens(t, usage(i), {"primary": window(10080, 0, t + 7 * 24 * H)},
                     limit_id="base_model_inference")
        elif i == 9:
            s.tokens(t, usage(i), {}, limit_id="premium")
        else:
            s.tokens(t, usage(i), {"primary": window(300, ramp(3, 14, i, n), r5),
                                   "secondary": window(10080, ramp(20, 22, i, n), rw)})
    s.close()
    return {"requests": 20, "five_hour": 14, "weekly": 22, "pools": [], "binding": 22,
            "current_model": "gpt-5.6-sol", "models": {"gpt-5.6-sol": 20}}


def idle_big_context(root, now):
    """大上下文会话空闲了一个多小时：缓存大概率已失效，要提示接着用得按新增输入重读。
    最近一次请求不受 5 小时窗口限制；app 和 CLI 的提示档位、重读代价必须一致。"""
    r5, rw = now + 2.0 * H, now + 3 * 24 * H
    s, n = Session(root, now - 2.5 * H, "gpt-5.6-sol"), 20
    for i in range(n):
        s.tokens(now - 2.4 * H + i * 240, usage(i, fresh=2_400, cached=140_000),
                 {"primary": window(300, ramp(5, 20, i, n), r5),
                  "secondary": window(10080, ramp(30, 33, i, n), rw)})
    s.close()
    last = usage(n - 1, fresh=2_400, cached=140_000)     # 最后一次在约 68 分钟前
    return {"requests": 20, "five_hour": 20, "weekly": 33, "pools": [], "binding": 33,
            "current_model": "gpt-5.6-sol", "models": {"gpt-5.6-sol": 20},
            "cache_hint": "likely", "context": last["input_tokens"]}


def cache_miss(root, now):
    """会话中途缓存失效：上一次 12 万上下文，这次整段按新增输入重读（缓存计数归零）。

    v5 起这部分按缓存价计 —— 服务端仍认这批内容，日志只是把缓存计数清零了。
    照新增输入计的话，这一次就要多算十倍，长会话会被严重高估。
    """
    r5, rw = now + 4 * H, now + 6 * 24 * H
    s, n = Session(root, now - 90 * 60, "gpt-5.6-sol"), 10
    for i in range(n):
        t = now - 80 * 60 + i * 300
        use = (usage(i, fresh=120_000, cached=0) if i == 5
               else usage(i, fresh=2_000, cached=118_000))
        s.tokens(t, use, {"primary": window(300, ramp(2, 9, i, n), r5),
                          "secondary": window(10080, ramp(10, 11, i, n), rw)})
    s.close()
    return {"requests": 10, "five_hour": 9, "weekly": 11, "pools": [], "binding": 11,
            "current_model": "gpt-5.6-sol", "models": {"gpt-5.6-sol": 10}}


SCENARIOS = {f.__name__: f for f in (normal, reserve, weekly_only, stale, expired_pool, cache_miss,
                                     other_limit, idle_big_context)}


def build(out_dir, now=None):
    """把所有场景写到 out_dir/<场景名>/sessions/…，返回 {场景名: 期望值}"""
    now = now or time.time()
    expect = {}
    for name, fn in SCENARIOS.items():
        root = os.path.join(out_dir, name)
        if os.path.exists(root):
            if not os.path.exists(os.path.join(root, MARKER)):
                sys.exit(f"{root} 已存在且不是本脚本生成的，拒绝覆盖")
            shutil.rmtree(root)
        os.makedirs(root)
        open(os.path.join(root, MARKER), "w").close()
        expect[name] = fn(root, now)
    return expect


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法：python3 tests/fixtures.py <输出目录>")
    for name in build(sys.argv[1]):
        print(os.path.join(sys.argv[1], name))
