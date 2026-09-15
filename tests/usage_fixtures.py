#!/usr/bin/env python3
"""「历史用量」页的演示数据：最近 7 天的 Codex、Claude Code、opencode 用量。

只给 README 截图用（docs/render.sh），不读你的真实日志：

    python3 tests/usage_fixtures.py /tmp/cc-usage
    CODEX_HOME=/tmp/cc-usage/codex CLAUDE_CONFIG_DIR=/tmp/cc-usage/claude \\
    XDG_DATA_HOME=/tmp/cc-usage/xdg CODEX_COST_DATA_DIR=/tmp/cc-usage/data \\
    ./codex-cost --render-history out.png week

时间戳相对「现在」生成。零依赖，标准库。
"""
from __future__ import annotations
import json, os, sqlite3, sys, time
from datetime import datetime, timezone

DAY = 86400
# 每天的相对强度，最后一个是今天 —— 柱状图看起来像真实的一周
SHAPE = [0.55, 0.9, 0.35, 1.0, 0.7, 0.25, 0.6]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def start_of(day_index, now, minutes_before):
    return now - (len(SHAPE) - 1 - day_index) * DAY - minutes_before * 60


def codex(root, now):
    for d, k in enumerate(SHAPE):
        t0 = start_of(d, now, 40)
        model = "gpt-6-astra" if d in (1, 3) else "gpt-5.6-sol"
        rows = [{"timestamp": iso(t0), "type": "session_meta", "payload": {"id": f"demo-{d}", "cwd": "/tmp/demo"}},
                {"timestamp": iso(t0), "type": "turn_context", "payload": {"model": model}}]
        for i in range(int(40 * k) + 5):
            cached = 60_000 + i * 1_500
            rows.append({"timestamp": iso(t0 + i * 20), "type": "event_msg",
                         "payload": {"type": "token_count", "rate_limits": {"limit_id": "codex"},
                                     "info": {"last_token_usage": {
                                         "input_tokens": cached + 3_000, "cached_input_tokens": cached,
                                         "output_tokens": 900, "reasoning_output_tokens": 300}}}})
        day = datetime.fromtimestamp(t0, timezone.utc)
        write_jsonl(os.path.join(root, "codex", "sessions", day.strftime("%Y/%m/%d"),
                                 f"rollout-{day.strftime('%Y-%m-%dT%H-%M-%S')}-demo{d}.jsonl"), rows)


def claude(root, now):
    rows = []
    for d, k in enumerate(SHAPE):
        t0 = start_of(d, now, 90)
        for i in range(int(30 * k) + 3):
            rows.append({"timestamp": iso(t0 + i * 30), "type": "assistant", "sessionId": "demo",
                         "message": {"id": f"msg-{d}-{i}", "model": "claude-opus-5", "role": "assistant",
                                     "usage": {"input_tokens": 12, "cache_creation_input_tokens": 2_500,
                                               "cache_read_input_tokens": 90_000 + i * 2_000,
                                               "output_tokens": 700}}})
    write_jsonl(os.path.join(root, "claude", "projects", "demo", "session.jsonl"), rows)


def opencode(root, now):
    db = os.path.join(root, "xdg", "opencode", "opencode.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table message (id text, session_id text, time_created integer, "
                "time_updated integer, data text)")
    for d, k in enumerate(SHAPE):
        t0 = start_of(d, now, 120)
        for i in range(int(20 * k) + 2):
            ms = int((t0 + i * 40) * 1000)
            data = {"role": "assistant", "modelID": "glm-5.3-flash" if i % 3 else "deepseek-flash",
                    "tokens": {"input": 8_000, "output": 400, "reasoning": 100,
                               "cache": {"read": 40_000 + i * 800, "write": 0}},
                    "cost": 0.0021, "time": {"created": ms}}
            con.execute("insert into message values (?, 'demo', ?, ?, ?)",
                        (f"oc-{d}-{i}", ms, ms, json.dumps(data)))
    con.commit()
    con.close()


def build(out_dir, now=None):
    now = now or time.time()
    if os.path.exists(out_dir) and os.listdir(out_dir):
        sys.exit(f"{out_dir} 不是空目录，拒绝写入")
    os.makedirs(out_dir, exist_ok=True)
    codex(out_dir, now)
    claude(out_dir, now)
    opencode(out_dir, now)
    return out_dir


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("用法：python3 tests/usage_fixtures.py <输出目录>")
    print(build(sys.argv[1]))
