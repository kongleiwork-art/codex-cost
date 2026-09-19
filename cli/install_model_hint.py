#!/usr/bin/env python3
"""把「贵模型提醒」钩子装进 Codex。只动一个文件：`~/.codex/config.toml`。

    python3 cli/install_model_hint.py --dry-run    # 只打印要写什么，不动文件
    python3 cli/install_model_hint.py              # 安装或更新
    python3 cli/install_model_hint.py --uninstall  # 卸载，整段删掉
    python3 cli/install_model_hint.py --block      # 装成「拦下消息」而不是只提示

**为什么是 config.toml 而不是 hooks.json**：Codex 也认 `hooks.json`，但只在两处 ——
从 Claude Code 迁移配置时，以及插件自带的清单（`"hooks": "./hooks.json"`）。
往 `~/.codex/hooks.json` 写，Codex 连读都不会读（实测写完之后 atime 一直没变）。
用户自己的钩子归 `config.toml` 的 `[hooks]` 管。

写入的内容夹在两行注释标记之间，卸载就整段删掉，文件里别的东西一律不动；
写之前先备份成 config.toml.bak-<时间戳>。设了 CODEX_HOME 就写它。

装完之后 Codex 要你确认信任这个钩子（终端版启动时弹「Hooks need review」，
桌面版在设置里）。这一步刻意留给你 —— 安装脚本不替你信任任何东西。
"""
from __future__ import annotations
import argparse, os, shlex, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "codex_model_hint.py")
START = "# >>> codex-cost 贵模型提醒钩子（由 cli/install_model_hint.py 管理，别手改）>>>"
END = "# <<< codex-cost 贵模型提醒钩子 <<<"


def codex_home() -> str:
    v = os.environ.get("CODEX_HOME") or ""
    return os.path.expanduser(v) if v else os.path.expanduser("~/.codex")


def hook_command(block: bool) -> str:
    """包一层 `|| true`：脚本被挪走、改名、删掉时 python3 会以退出码 2 结束，
    而 Codex 把 UserPromptSubmit 的退出码 2 当成「拦截这条消息」。真出那种事，
    宁可这个钩子静悄悄什么都不做，也不能让你发不出消息。"""
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(HOOK)}"
    if block:
        cmd += " --block"
    return f"/bin/sh -c {shlex.quote(cmd + ' || true')}"


def block_text(block: bool) -> str:
    cmd = hook_command(block).replace("\\", "\\\\").replace('"', '\\"')
    return (f"{START}\n"
            f"[[hooks.UserPromptSubmit]]\n"
            f"[[hooks.UserPromptSubmit.hooks]]\n"
            f'type = "command"\n'
            f'command = "{cmd}"\n'
            f"{END}\n")


def strip_block(text: str) -> str:
    """删掉标记之间那一段（含标记）。没装过就原样返回。"""
    while START in text and END in text:
        a, b = text.index(START), text.index(END) + len(END)
        text = text[:a].rstrip("\n") + "\n" + text[b:].lstrip("\n")
    return text


def update(text: str, install: bool, block: bool) -> str:
    text = strip_block(text)
    if not install:
        return text if text.endswith("\n") or not text else text + "\n"
    head = text.rstrip("\n")
    return (head + "\n\n" if head else "") + block_text(block)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--dry-run", action="store_true", help="只打印要写什么")
    ap.add_argument("--block", action="store_true", help="装成拦下消息（默认只提示，不拦）")
    args = ap.parse_args(argv)
    install = not args.uninstall

    path = os.path.join(codex_home(), "config.toml")
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    new = update(old, install, args.block)

    if args.dry_run:
        print(f"目标文件：{path}（{'已存在' if os.path.exists(path) else '还不存在，会新建'}）")
        print(f"原文 {len(old.splitlines())} 行，写入后 {len(new.splitlines())} 行")
        print("\n--- 会加在文件末尾 ---" if install else "\n--- 会删掉这一段 ---")
        print(block_text(args.block) if install else
              (START + " …… " + END if START in old else "（没装过，无事可做）"))
        print("（--dry-run：什么都没动）")
        return 0

    if new == old:
        print("没有变化：" + ("已经装好了" if install else "本来就没装"))
        return 0
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"已备份：{backup}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)
    print(("已安装" if install else "已卸载") + f"：{path}")
    if install:
        print("接着要你确认信任这个钩子：终端版启动时会弹 Hooks need review，桌面版在设置里。")
        print("想撤掉：python3 cli/install_model_hint.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
