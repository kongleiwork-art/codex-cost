#!/bin/bash
# 构建 CodexCost.app。只需要 Xcode 命令行工具，无第三方依赖。
set -e
cd "$(dirname "$0")"
APP=CodexCost.app
BIN=codex-cost

echo "编译…"
swiftc -O -parse-as-library Sources/*.swift -o "$BIN"

echo "打包…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/"
cp "$BIN" "$APP/Contents/MacOS/"

echo "完成：$APP"
echo
echo "运行：open $APP"
echo "首次打开若被 Gatekeeper 拦住：右键点 $APP → 打开 → 再点「打开」"
