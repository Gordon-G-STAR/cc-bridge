#!/usr/bin/env bash
#
# build_dmg.sh — 把 GUI 安装器打包成 macOS 的 .app，再生成可分发的 .dmg。
#
# !!! 必须在 macOS 上运行 !!!
#   - PyInstaller 的 --windowed 在 macOS 上产出 .app 包；
#   - dmgbuild 只能在 macOS 上生成磁盘映像（依赖 hdiutil 等系统工具）。
#   在 Windows / Linux 上运行本脚本不会得到可用产物。
#
# 用法（在 macOS 上、项目根目录下）：
#   # 1) 以可编辑方式安装本包并带上 macOS 打包依赖（pyinstaller + dmgbuild）
#   pip install -e '.[build-macos]'
#
#   # 2) 运行本脚本，产物落在 <项目根>/dist/cc-bridge-installer.dmg
#   bash src/cc_bridge/installer/build_dmg.sh
#
set -euo pipefail

# ---- 基本参数 --------------------------------------------------------------
APP_NAME="cc-bridge-installer"      # 产物名（.app / .dmg 的主干）
VOLUME_NAME="cc-bridge installer"   # 挂载时显示的卷名
ENTRY_MODULE_PATH="cc_bridge/installer/main.py"  # GUI 安装器入口（相对 src）

# ---- 路径推断 --------------------------------------------------------------
# 本脚本位于 <root>/src/cc_bridge/installer/build_dmg.sh，回退三级得到项目根。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

SRC_DIR="${PROJECT_ROOT}/src"
ENTRY_SCRIPT="${SRC_DIR}/${ENTRY_MODULE_PATH}"

DIST_DIR="${PROJECT_ROOT}/dist"
BUILD_DIR="${DIST_DIR}/build"
SPEC_DIR="${DIST_DIR}/spec"

APP_BUNDLE="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${APP_NAME}.dmg"

# ---- 平台与依赖检查 --------------------------------------------------------
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: 本脚本必须在 macOS（Darwin）上运行。当前系统：$(uname -s)" >&2
    exit 1
fi

# 用当前 python 解释器调用模块，避免 PATH 里没有脚本入口。
PY="${PYTHON:-python3}"

if ! "${PY}" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "error: 未检测到 PyInstaller。请先安装打包依赖：" >&2
    echo "    pip install -e '.[build-macos]'" >&2
    exit 1
fi

if ! "${PY}" -c "import dmgbuild" >/dev/null 2>&1; then
    echo "error: 未检测到 dmgbuild。请先安装 macOS 打包依赖：" >&2
    echo "    pip install -e '.[build-macos]'" >&2
    exit 1
fi

if [[ ! -f "${ENTRY_SCRIPT}" ]]; then
    echo "error: 找不到 GUI 安装器入口脚本：${ENTRY_SCRIPT}" >&2
    exit 1
fi

# ---- 1) PyInstaller 打出 .app ---------------------------------------------
mkdir -p "${DIST_DIR}" "${BUILD_DIR}" "${SPEC_DIR}"

# 说明：
# - macOS 上用 --windowed 生成标准 .app；与 Windows 不同，.app 内部可执行文件
#   （Contents/MacOS/<name>）被当作子进程、用 stdio 管道拉起时 stdin/stdout 仍可用，
#   因此可以充当 MCP stdio server。
# - frozen 自拉起时，MCP server 以 .app 内部可执行文件的【绝对路径】就地注册
#   （见 config._ensure_frozen_launcher：.app/onedir 不可单独拷出，否则丢失 Frameworks 依赖）。
#   ⇒ 安装后请把 .app 留在固定位置（如 /Applications），不要移动或删除，否则桥接会失效。
echo "==> running PyInstaller (生成 ${APP_NAME}.app) ..." >&2
"${PY}" -m PyInstaller \
    --windowed \
    --name "${APP_NAME}" \
    `# 只收集 mcp.server 子树 + 数据；--collect-all mcp 会 import 依赖 typer/rich 的 mcp.cli 而失败` \
    --collect-submodules mcp.server \
    --collect-data mcp \
    --exclude-module mcp.cli \
    --collect-submodules cc_bridge \
    --noconfirm \
    --distpath "${DIST_DIR}" \
    --workpath "${BUILD_DIR}" \
    --specpath "${SPEC_DIR}" \
    "${ENTRY_SCRIPT}"

if [[ ! -d "${APP_BUNDLE}" ]]; then
    echo "error: 未生成预期的 .app：${APP_BUNDLE}" >&2
    exit 1
fi

# ---- 2) dmgbuild 生成 .dmg -------------------------------------------------
# 用临时 here-doc 写一份 dmgbuild 的 settings.py；脚本退出时清理。
SETTINGS_PY="$(mktemp -t cc_bridge_dmg_settings.XXXXXX.py)"
trap 'rm -f "${SETTINGS_PY}"' EXIT

cat > "${SETTINGS_PY}" <<'PYEOF'
# dmgbuild 配置：由 build_dmg.sh 在运行时通过环境变量注入实际路径。
import os

app_path = os.environ["CC_BRIDGE_APP_BUNDLE"]
app_name = os.path.basename(app_path)

# 放进 DMG 的内容：.app 本体，以及一个 /Applications 软链方便用户拖拽安装。
files = [app_path]
symlinks = {"Applications": "/Applications"}

# Finder 窗口与图标布局。
icon_locations = {
    app_name: (140, 160),
    "Applications": (380, 160),
}
window_rect = ((200, 200), (520, 360))
default_view = "icon-view"
icon_size = 96

# 压缩格式（只读、压缩）。
format = "UDZO"
PYEOF

echo "==> running dmgbuild (生成 ${APP_NAME}.dmg) ..." >&2
# 删除旧的 dmg，避免 dmgbuild 因为目标已存在而报错。
rm -f "${DMG_PATH}"

CC_BRIDGE_APP_BUNDLE="${APP_BUNDLE}" \
    "${PY}" -m dmgbuild \
    -s "${SETTINGS_PY}" \
    "${VOLUME_NAME}" \
    "${DMG_PATH}"

if [[ ! -f "${DMG_PATH}" ]]; then
    echo "error: 未生成预期的 .dmg：${DMG_PATH}" >&2
    exit 1
fi

echo "done: 产物位于 ${DMG_PATH}" >&2
