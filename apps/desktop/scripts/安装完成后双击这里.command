#!/bin/bash
set -u

APP="${1:-/Applications/LLANO Agent.app}"

show_dialog() {
    if [ -t 1 ]; then
        DIALOG_TITLE="$1" DIALOG_MESSAGE="$2" /usr/bin/osascript <<'APPLESCRIPT'
display dialog (system attribute "DIALOG_MESSAGE") with title (system attribute "DIALOG_TITLE") buttons {"好"} default button "好"
APPLESCRIPT
    fi
}

fail() {
    printf '处理失败：%s\n' "$1" >&2
    show_dialog "LLANO Agent" "处理失败：$1"
    exit 1
}

if [ ! -d "$APP/Contents" ]; then
    fail "请先把“LLANO Agent”拖入 Applications 文件夹，再双击本脚本。"
fi

if ! /usr/bin/codesign --verify --deep --strict --verbose=4 "$APP"; then
    fail "应用签名校验失败。请删除现有应用并从公司网关重新下载安装包。"
fi

if ! /usr/bin/xattr -dr com.apple.quarantine "$APP"; then
    fail "无法移除系统下载隔离标记。请确认应用已经复制到 Applications 文件夹。"
fi

if ! /usr/bin/codesign --verify --deep --strict --verbose=4 "$APP"; then
    fail "移除隔离标记后签名校验失败，应用没有启动。请重新下载安装包。"
fi

if ! /usr/bin/open "$APP"; then
    fail "系统未能启动应用，请重新打开 Applications 文件夹后再试。"
fi

printf '处理完成，正在启动“LLANO Agent”。\n'
show_dialog "LLANO Agent" "处理完成，正在启动“LLANO Agent”。"
