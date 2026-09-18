#!/usr/bin/env python3
"""把「贵模型提醒」钩子装进 Codex。只动一个文件：`~/.codex/hooks.json`。

    python3 cli/install_model_hint.py --dry-run    # 只打印要写什么，不动文件
    python3 cli/install_model_hint.py              # 安装或更新
    python3 cli/install_model_hint.py --uninstall  # 卸载，还原成没装过的样子
    python3 cli/install_model_hint.py --block      # 装成「拦下消息」而不是只提示

设了 CODEX_HOME 就写它，否则 ~/.codex。已有的其它钩子原样保留；重复安装不会
叠加（先删掉自己那条再写）；写之前先备份成 hooks.json.bak-<时间戳>。
hooks.json 本身格式坏了就直接报错退出，绝不覆盖。

Codex 对新加或改动过的钩子要人工确认：下次打开会提示「Hooks need review」，
你选信任之后它才会运行。这一步刻意留给你 —— 安装脚本不替你信任任何东西。
"""
from __future__ import annotations
import argparse, json, os, shlex, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "codex_model_hint.py")
EVENT = "UserPromptSubmit"


def codex_home() -> str:
    v = os.environ.get("CODEX_HOME") or ""
    return os.path.expanduser(v) if v else os.path.expanduser("~/.codex")


def hook_command(block: bool) -> str:
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(HOOK)}"
    return cmd + " --block" if block else cmd


def is_ours(handler: dict) -> bool:
    return "codex_model_hint.py" in (handler.get("command") or "")


def update(data: dict, install: bool, block: bool) -> dict:
    """删掉自己那条，需要的话再加回去。别人的钩子一律不动。"""
    hooks = data.setdefault("hooks", {})
    groups = []
    for g in hooks.get(EVENT, []):
        kept = [h for h in g.get("hooks", []) if not is_ours(h)]
        if kept:
            groups.append(dict(g, hooks=kept))
    if install:
        groups.append({"hooks": [{"type": "command", "command": hook_command(block)}]})
    if groups:
        hooks[EVENT] = groups
    else:
        hooks.pop(EVENT, None)
    if not hooks:
        data.pop("hooks", None)
    return data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--dry-run", action="store_true", help="只打印要写什么")
    ap.add_argument("--block", action="store_true",
                    help="装成拦下消息（默认只提示，不拦）")
    args = ap.parse_args(argv)
    install = not args.uninstall

    path = os.path.join(codex_home(), "hooks.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    before = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    content = json.dumps(update(data, install, args.block), ensure_ascii=False, indent=2) + "\n"

    if args.dry_run:
        print(f"目标文件：{path}（{'已存在' if os.path.exists(path) else '还不存在，会新建'}）")
        print(f"\n--- 现在是 ---\n{before if before != '{}' else '（空）'}")
        print(f"\n--- 会写成 ---\n{content}", end="")
        print("\n（--dry-run：什么都没动）")
        return 0

    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"已备份：{backup}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)
    print(("已安装" if install else "已卸载") + f"：{path}")
    if install:
        print("下次打开 Codex 会提示 Hooks need review，你确认信任之后它才会运行。")
        print("想撤掉：python3 cli/install_model_hint.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
