#!/bin/zsh
# 上传到GitHub.sh —— macOS 版一键推送（替代原 Windows 的 .bat）
# 依赖：gh CLI 已登录（凭证存在 macOS 钥匙串，脚本内无任何令牌）
# 用法：终端执行 ./上传到GitHub.sh 或双击（右键-打开）

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BRANCH="master"

# ── 自动探测系统代理（走系统代理才能连 GitHub，沙箱默认代理不支持 git push）──
PROXY_ENABLE=$(scutil --proxy | awk '/HTTPSEnable/{print $3}')
PROXY_HOST=$(scutil --proxy  | awk '/HTTPSProxy/{print $3}')
PROXY_PORT=$(scutil --proxy  | awk '/HTTPSPort/{print $3}')
if [ "$PROXY_ENABLE" = "1" ] && [ -n "$PROXY_HOST" ]; then
    export HTTPS_PROXY="http://$PROXY_HOST:$PROXY_PORT"
    export HTTP_PROXY="http://$PROXY_HOST:$PROXY_PORT"
fi

cd "$REPO_DIR" || { echo "[错误] 无法进入 $REPO_DIR"; exit 1; }

echo "[1/3] 检查改动 ..."
git add -A
if git diff --cached --quiet; then
    echo "  无新改动，无需提交。"
else
    N=$(git diff --cached --numstat | wc -l | tr -d ' ')
    echo "[2/3] 提交 $N 个文件的改动 ..."
    git commit -m "auto: $(date '+%Y-%m-%d %H:%M') 更新（Mac 自动推送）" >/dev/null
fi

echo "[3/3] 推送到 GitHub ..."
if git push origin "$BRANCH" 2>&1 | tail -3; then
    echo "========================"
    echo "成功！https://github.com/cochain27/daa-stock-research-skill"
    echo "========================"
else
    echo "========================"
    echo "推送失败：请确认 1) 系统代理已开启  2) gh auth status 显示已登录"
    echo "========================"
    exit 1
fi
