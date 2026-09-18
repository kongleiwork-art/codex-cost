#!/usr/bin/env python3
"""Codex 钩子：在贵模型上按下回车之前，提示这一轮大概比便宜的模型多花多少额度。

装成 Codex 的 `userPromptSubmit` 钩子（协议见 docs/ROADMAP.md 的 3.3）：

    echo '{"model":"gpt-6-astra","transcript_path":"...","prompt":"..."}' \
        | python3 cli/codex_model_hint.py

**它不做什么**：不读提示词内容、不调任何模型、不改模型（钩子协议也不允许改）、
默认不拦你的消息。它只看当前模型和这段会话的上下文大小，算一句成本差。

为什么是提醒而不是自动路由：近 30 天的日志里，astra 的轮次推理 token 中位数 199、
sol 的是 1,445 —— 贵模型的钱不是花在难题上，是花在「切过去以后忘了切回来」。
这种粗活不需要模型判断。详见 docs/ROADMAP.md「3.3 路由重开」。

出错一律静默退出 0：钩子挡住你的消息，比少提示一句糟糕得多。
"""
from __future__ import annotations
import argparse, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from codex_budget import COEF, DEFAULT_COEF  # noqa: E402

# 参照模型：付费模型里最便宜的那个。提示说的「换成它能省多少」就是跟它比。
REFERENCE = "gpt-5.6-sol"
# 少于这个差值（每轮的 5 小时额度百分点）就不吭声 —— 提示太碎会被无视
MIN_GAP = float(os.environ.get("CODEX_COST_HINT_MIN") or 1.0)
# 从会话日志尾部读这么多字节就够看出当前上下文和最近几轮的规模
TAIL_BYTES = 512 * 1024
# 估算「这一轮要发几次请求」时看最近这么多轮
RECENT_TURNS = 8


def tail_lines(path, limit=TAIL_BYTES):
    """只读文件尾部。会话日志可以有几百 MB，钩子必须立刻返回。"""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()          # 丢掉半行
        return fh.read().decode("utf-8", "replace").splitlines()


def session_shape(path):
    """返回 (当前上下文 token 数, 最近几轮里每轮的请求数中位数)。读不出来就 (0, 1)。"""
    context, turns, cur = 0, [], 0
    for line in tail_lines(path):
        if '"token_count"' not in line and '"task_started"' not in line \
                and '"user_message"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") or {}
        t = p.get("type") or d.get("type")
        if t in ("task_started", "user_message"):
            if cur:
                turns.append(cur)
            cur = 0
        elif t == "token_count":
            u = (p.get("info") or {}).get("last_token_usage") or {}
            if not u:
                continue
            context = u.get("input_tokens") or context
            cur += 1
    if cur:
        turns.append(cur)
    recent = turns[-RECENT_TURNS:]
    return context, (int(statistics.median(recent)) if recent else 1)


def turn_cost(model, context, requests):
    """这一轮的额度估算：上下文按缓存价乘请求数，加上每请求底价。

    不算输出 —— 按回车之前谁也不知道模型会说多少。所以这是个低估；
    astra 的输出比 sol 贵 5.9 倍，真实差距只会比提示的更大。
    """
    f, c, o, r = COEF.get(model) or DEFAULT_COEF
    if not c:
        return None
    return requests * (context / c + r)


def short(model):
    return (model or "?").replace("gpt-", "")


def build_message(model, context, requests, ref=REFERENCE):
    now = turn_cost(model, context, requests)
    then = turn_cost(ref, context, requests)
    if now is None or then is None or now - then < MIN_GAP:
        return None
    return (f"{short(model)}｜上下文 {context / 10000:.1f} 万｜"
            f"这一轮约 {now:.1f}%，换 {short(ref)} 约 {then:.1f}%"
            f"（省 {now - then:.1f}%，按最近 {requests} 次请求一轮估，未计输出）")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--block", action="store_true",
                    help="不只是提示，直接拦下这条消息，让你先切模型（默认只提示）")
    ap.add_argument("--reference", default=REFERENCE, help="跟哪个模型比（默认 %(default)s）")
    args = ap.parse_args(argv)

    raw = sys.stdin.read()
    payload = json.loads(raw)
    model = payload.get("model")
    path = payload.get("transcript_path")
    if not model or not path or not os.path.exists(path):
        return 0
    context, requests = session_shape(path)
    if context <= 0:
        return 0
    msg = build_message(model, context, requests, args.reference)
    if not msg:
        return 0
    out = {"systemMessage": msg}
    if args.block:
        # decision 只接受 "block"；reason 会显示给你看
        out.update({"decision": "block", "reason": msg + " —— 切完模型再发一次"})
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # 钩子出任何问题都不许挡住消息
        sys.exit(0)
