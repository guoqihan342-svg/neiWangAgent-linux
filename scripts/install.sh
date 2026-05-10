#!/usr/bin/env bash
# =============================================================================
# neiWangAgent-linux 安装脚本
# 用法: bash install.sh [--user] [--system]
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
INSTALL_MODE="${1:---user}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${CYAN}[→]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

# ── 检查依赖 ──
check_deps() {
    warn "检查系统依赖..."
    local missing=()
    for cmd in python3 git; do
        command -v $cmd &>/dev/null || missing+=($cmd)
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "缺少依赖: ${missing[*]}"
        echo "  Ubuntu/Debian: sudo apt install ${missing[*]}"
        echo "  CentOS/RHEL:   sudo yum install ${missing[*]}"
        exit 1
    fi
    log "依赖检查通过: python3 $(python3 --version 2>&1 | cut -d' ' -f2), git $(git --version 2>&1 | cut -d' ' -f3)"
}

# ── Python 版本检查 ──
check_python() {
    local ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if [[ "$(echo "$ver >= 3.10" | bc -l 2>/dev/null || echo 0)" != "1" ]]; then
        err "需要 Python >= 3.10，当前: $ver"
        exit 1
    fi
    log "Python $ver ✓"
}

# ── 安装 ──
install_pkg() {
    warn "安装 neiWangAgent-linux..."
    cd "$SCRIPT_DIR"
    if [[ "$INSTALL_MODE" == "--system" ]]; then
        sudo pip3 install -e . 2>&1 | tail -3
    else
        pip3 install --user -e . 2>&1 | tail -3
    fi
    log "Python 包安装完成"
}

# ── 验证 ──
verify() {
    warn "验证安装..."
    if command -v neiWangAgent &>/dev/null; then
        log "CLI 命令可用: $(which neiWangAgent)"
        neiWangAgent --version
    else
        err "CLI 命令不可用，检查 PATH 中是否包含 ~/.local/bin"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\" >> ~/.bashrc"
    fi
}

# ── Shell补全 ──
setup_completion() {
    warn "配置 bash 补全..."
    local comp_dir="${HOME}/.local/share/bash-completion/completions"
    mkdir -p "$comp_dir"
    neiWangAgent --show-completion 2>/dev/null > "$comp_dir/neiWangAgent" || true
    log "Shell 补全已配置 (source 后生效)"
}

# ── 环境变量提示 ──
env_hint() {
    echo ""
    echo "============================================"
    echo " 安装完成！配置 API Key 后即可使用:"
    echo ""
    echo "   export DEEPSEEK_API_KEY='sk-xxx'"
    echo "   cd /path/to/your/project"
    echo "   neiWangAgent init"
    echo "   neiWangAgent warmup"
    echo "   neiWangAgent run --task task.md"
    echo "============================================"
}

# ── Main ──
main() {
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║   neiWangAgent-linux v0.1 安装程序       ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    check_deps
    check_python
    install_pkg
    verify
    setup_completion
    env_hint
}

main
