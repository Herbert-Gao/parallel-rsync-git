#!/usr/bin/env bash
# parallel-rsync-git 自测
#
# 建一个临时 git 仓库，验证工具的核心行为：
#   · 本地被 git 忽略的东西不传
#   · 远端独有的、被 git 忽略的文件（各机自己的 checkpoint）不会被 --delete 删掉
#   · 不相关的远端文件照常清理
#   · 受版本管理的、! 取反的、未提交的新代码都照常同步
#   · force_include / extra_files / tracked 模式 / delete:false
#   · 跨平台入口：POSIX 启动器、python 入口、中英文界面、内置并行器
#
# 用法：bash test_parallel_rsync_git.sh
set -u

DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${TOOL:-}" ]; then
    TOOL="$TOOL"
elif command -v parallel-rsync-git >/dev/null 2>&1; then
    TOOL="$(command -v parallel-rsync-git)"
else
    TOOL="$DIR/parallel-rsync-git"
fi
PY="$DIR/parallel_rsync_git.py"
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
else
    PYTHON=python
fi
WORK="$(mktemp -d)"
# Git Bash 里 mktemp 给的是 /tmp/... 这种 MSYS 路径，原生 Windows 版 rsync
# 看不懂，得换成 C:/... 这种写法
if command -v cygpath >/dev/null 2>&1; then
    WORK="$(cygpath -m "$WORK")"
fi
pass=0
fail=0

HAVE_RSYNC=1
if ! command -v rsync >/dev/null 2>&1; then
    HAVE_RSYNC=0
    echo "  (no rsync on this machine — transfer checks will be skipped)"
fi
# rsync 3.0 以下（macOS 自带的 openrsync 就是）不认识 P/R 保护规则，
# "远端独有的 checkpoint 保住" 这条测不了，只能跳过
HAS_PRISK=1
if [ "$HAVE_RSYNC" = 1 ]; then
    rsync_ver="$(rsync --version 2>/dev/null)"
    if echo "$rsync_ver" | grep -qi openrsync; then
        HAS_PRISK=0
        echo "  (openrsync has no P/R rules — protection checks will be skipped)"
    else
        v="$(echo "$rsync_ver" | grep -oE 'version [0-9]+\.[0-9]+' | head -1 | grep -oE '[0-9]+\.[0-9]+')"
        major="$(echo "$v" | cut -d. -f1)"
        if [ -n "$major" ] && [ "$major" -lt 3 ]; then
            HAS_PRISK=0
            echo "  (rsync $v has no P/R rules — protection checks will be skipped)"
        fi
    fi
fi
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }

check() {
  if [ "$2" = "$3" ]; then
    printf '  \033[32mOK\033[0m %s\n' "$1"; pass=$((pass + 1))
  else
    printf '  \033[31mFAIL\033[0m %s (expected %s, got %s)\n' "$1" "$3" "$2"; fail=$((fail + 1))
  fi
}
exist() { [ -e "$1" ] && echo 1 || echo 0; }
ok() { if "$@" >/dev/null 2>&1; then echo 1; else echo 0; fi; }
has() { local pat="$1"; shift; if "$@" 2>/dev/null | grep -q "$pat"; then echo 1; else echo 0; fi; }

echo "tool: $TOOL"
echo "workdir: $WORK"
echo

# ---------------------------------------------------------------- 造场景
SRC="$WORK/src"
mkdir -p "$SRC/models" "$SRC/build" "$WORK/dest/models" "$WORK/dest/build"
cd "$SRC" || exit 1
git init -q .
printf 'models/*\n!models/README.md\nbuild/\n*.log\n!important.log\n' > .gitignore
echo "tracked code" > tracked.txt
echo "readme" > models/README.md
echo "local weights" > models/final.pt
echo "build output" > build/out.bin
echo "debug log" > debug.log
echo "keep this log" > important.log
echo "uncommitted new code" > newfile.py
printf 'newfile.py\n' > .syncignore
git add tracked.txt models/README.md .gitignore
git -c user.email=a@b -c user.name=c commit -qm init

# 远端预置：本地没有的文件
echo "remote checkpoint" > "$WORK/dest/models/remote_only.pt"
echo "remote build output" > "$WORK/dest/build/remote.bin"
echo "temp file" > "$WORK/dest/stale.txt"
echo "remote only code" > "$WORK/dest/remote_only_code.py"

# ---------------------------------------------------------------- 配置
cat > "$WORK/base.yml" << YML
global_options:
  - "-avz"
  - "--delete"
groups:
  - name: "t"
    src: "$SRC"
    dest: "$WORK/dest"
YML
cat > "$WORK/force.yml" << YML
global_options:
  - "-avz"
  - "--delete"
gitignore:
  force_include:
    - "models/final.pt"
  extra_files:
    - ".syncignore"
groups:
  - name: "t"
    src: "$SRC"
    dest: "$WORK/dest2"
YML
cat > "$WORK/tracked.yml" << YML
global_options:
  - "-avz"
  - "--delete"
gitignore:
  mode: tracked
groups:
  - name: "t"
    src: "$SRC"
    dest: "$WORK/dest3"
YML
cat > "$WORK/nodel.yml" << YML
global_options:
  - "-avz"
  - "--delete"
delete: false
gitignore:
  force_include:
    - "models/final.pt"
groups:
  - name: "t"
    src: "$SRC"
    dest: "$WORK/dest4"
YML

# ---------------------------------------------------------------- 测
echo "[default mode: exclude]"
if [ "$HAVE_RSYNC" = 1 ]; then
"$TOOL" -c "$WORK/base.yml" --no-progress > "$WORK/log1.txt" 2>&1
if [ "$HAS_PRISK" = 1 ]; then
check "remote-only checkpoint survives"      "$(exist "$WORK/dest/models/remote_only.pt")" 1
check "remote-only build output survives"    "$(exist "$WORK/dest/build/remote.bin")"    1
else
skip "remote-only checkpoint survives"
skip "remote-only build output survives"
fi
check "unrelated remote file is cleaned"     "$(exist "$WORK/dest/stale.txt")"           0
check "unrelated remote code is cleaned"     "$(exist "$WORK/dest/remote_only_code.py")" 0
check "git-ignored weights are not sent"     "$(exist "$WORK/dest/models/final.pt")"     0
check "git-ignored logs are not sent"        "$(exist "$WORK/dest/debug.log")"           0
check "git-ignored dirs are not sent"        "$(exist "$WORK/dest/build/out.bin")"       0
check "tracked file is sent"                 "$(exist "$WORK/dest/tracked.txt")"         1
check "negation !models/README.md works"     "$(exist "$WORK/dest/models/README.md")"    1
check "negation !important.log works"        "$(exist "$WORK/dest/important.log")"       1
check "uncommitted new code is sent"         "$(exist "$WORK/dest/newfile.py")"          1
check ".git is not sent"                     "$(exist "$WORK/dest/.git")"                0
fi

echo
echo "[force_include + extra_files]"
if [ "$HAVE_RSYNC" = 1 ]; then
"$TOOL" -c "$WORK/force.yml" --no-progress > "$WORK/log2.txt" 2>&1
check "force_include file is sent"           "$(exist "$WORK/dest2/models/final.pt")"    1
check "extra_files excluded file is not sent" "$(exist "$WORK/dest2/newfile.py")"        0
fi

echo
echo "[mode: tracked]"
if [ "$HAVE_RSYNC" = 1 ]; then
"$TOOL" -c "$WORK/tracked.yml" --no-progress > "$WORK/log3.txt" 2>&1
check "only tracked files are sent"          "$(exist "$WORK/dest3/tracked.txt")"        1
check "untracked files are not sent"         "$(exist "$WORK/dest3/newfile.py")"         0
fi

echo
echo "[delete: false — incremental only]"
mkdir -p "$WORK/dest4/models"
echo "remote checkpoint" > "$WORK/dest4/models/remote_only.pt"
echo "old remote file" > "$WORK/dest4/keep_me.txt"
if [ "$HAVE_RSYNC" = 1 ]; then
"$TOOL" -c "$WORK/nodel.yml" --no-progress > "$WORK/log4.txt" 2>&1
check "existing remote file is kept"         "$(exist "$WORK/dest4/keep_me.txt")"        1
check "remote checkpoint is kept"            "$(exist "$WORK/dest4/models/remote_only.pt")" 1
check "new files are still sent"             "$(exist "$WORK/dest4/tracked.txt")"         1
check "force_include .ckpt is sent"          "$(exist "$WORK/dest4/models/final.pt")"     1
fi

echo
echo "[cross-platform entry points]"
check "POSIX launcher works"   "$(ok bash "$DIR/parallel-rsync-git" --help)" 1
check "python entry works"     "$(ok "$PYTHON" "$PY" --help)" 1
check "English UI"             "$(has 'Sync plan' "$PYTHON" "$PY" --report --lang en -c "$WORK/base.yml")" 1
check "Chinese UI"             "$(has '同步计划' "$PYTHON" "$PY" --report --lang zh -c "$WORK/base.yml")" 1
check "built-in runner works"  "$(ok "$PYTHON" "$PY" -c "$WORK/base.yml" --builtin --no-progress)" 1

if [ "$fail" -gt 0 ]; then
    for lg in "$WORK"/log*.txt; do
        [ -f "$lg" ] || continue
        echo
        echo "---- $(basename "$lg") (last 30 lines) ----"
        tail -30 "$lg"
        echo "--------------------------------------"
    done
fi

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ] || exit 1
