#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parallel-rsync-git —— 给 parallel-rsync 接上 git，让它看懂 .gitignore

为什么要这个：
  parallel-rsync 只认 rsync 的 --exclude，规则写死在配置文件里，
  仓库里的 .gitignore 它完全看不见。于是两头难受：
    1) models/、dataset/、outputs/ 这些大目录被一箱箱搬过去；
    2) 更要命的是配置里带着 --delete —— 远端各台机器自己训出来的
       checkpoint、自己攒的运行产物，会被当成“本地没有的多余文件”删掉。

它做什么：
  让 git 自己去回答“哪些路径被忽略”，再把答案翻译成 rsync 的排除规则，
  交给 parallel-rsync 去搬。git 认的规则全部生效：根目录和任意层级的
  .gitignore、.git/info/exclude、你的全局 excludes、子模块各自的
  .gitignore，以及 ! 取反规则。
  于是：代码照常同步（含本地还没提交的新代码），被忽略的东西不搬、
  远端已有的也不删 —— 各台机器的 model 保持各自独立。

用法：
  parallel-rsync-git                 # 用默认配置 ~/.config/parallel-rsync/config.yml
  parallel-rsync-git --report        # 只看计划：哪些会传、哪些被排除、为什么
  parallel-rsync-git --dry-run       # 预演（不改远端）
  parallel-rsync-git -c 别的.yml     # 指定配置

配置文件里新增的 gitignore 段（加在原有配置里即可，工具不用换）：
  gitignore:
    enabled: true            # 总开关，默认 true
    mode: exclude            # exclude（默认）/ tracked（只传 git 认的文件）
    include_git_dir: false   # 是否连 .git 目录一起同步（默认不同步，保住各机本地提交）
    submodules: true         # 子模块自己的 .gitignore 是否也生效
    extra_files: []          # 额外的忽略规则文件（相对源目录），gitignore 语法
    force_include: []        # 即使被 git 忽略也要同步的路径（相对源目录）
    delete_excluded: false   # 是否连远端被忽略的文件一起删（默认不删）
    required: false          # 源目录不在 git 仓库里时是否直接报错
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("缺少 PyYAML，先装一下：pip install pyyaml")


# ---------------------------------------------------------------------------
# 跨平台：Windows / macOS / Linux
# ---------------------------------------------------------------------------

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _detect_lang():
    """界面语言：中文环境用中文，其余一律英文。"""
    env = (
        os.environ.get("PARALLEL_RSYNC_GIT_LANG")
        or os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    ).lower()
    return "zh" if env.startswith("zh") else "en"


LANG = _detect_lang()


MESSAGES = {
    "en": {
        "missing_yaml": "PyYAML is missing. Install it with: pip install pyyaml",
        "not_a_repo": "source directory is not inside a git repository — gitignore rules skipped",
        "required_repo": "group '{name}': {msg} (required: true)",
        "no_config": "no config file found. Pass -c/--config, or create config.yml in one of:",
        "config_missing": "config file not found: {path}",
        "config_broken": "could not read config {path}: {exc}",
        "no_groups": "config {path} has no 'groups'",
        "no_rsync": "rsync was not found.",
        "hint_rsync_linux": "  Linux: sudo apt install rsync   (or sudo yum install rsync)",
        "hint_rsync_mac": (
            "  macOS: rsync ships with the system (older releases have 2.6.9).\n"
            "         Prefer 'brew install rsync' to get a 3.x release."
        ),
        "hint_rsync_win": (
            "  Windows: install Git for Windows (ships MSYS2 rsync), cwRsync or DeltaCopy,\n"
            "          then make sure 'rsync --version' works in a terminal.\n"
            "          Easiest path: run this script inside WSL (Ubuntu)."
        ),
        "platform": "Environment",
        "rsync_missing": "rsync not found",
        "plan_title": "Sync plan (parallel-rsync-git)",
        "plan_config": "Config",
        "plan_mode_exclude": "Mode: exclude — skip paths git ignores",
        "plan_mode_tracked": "Mode: tracked — only sync files git tracks",
        "plan_delete_on": (
            "Remote: .gitignore rules protect matching paths, --delete will not remove them"
        ),
        "plan_delete_off": (
            "Remote: only new and updated files are sent — nothing is deleted on the remote"
        ),
        "plan_delete_warn": (
            "WARNING: delete_excluded=true — ignored files on the remote WILL be deleted!"
        ),
        "group_src": "  source",
        "group_dest": "  target",
        "group_repo": "  repo",
        "group_repo_none": "  repo: none",
        "group_send": "  sending",
        "group_exclude": "  excluding",
        "group_exclude_hint": "paths git ignores (per git's own decision)",
        "group_protect": "  protecting",
        "group_protect_hint": (
            ".gitignore patterns — matching remote paths survive --delete"
        ),
        "group_protect_skipped": "  protecting: skipped (rsync older than 3.0 has no P/R rules)",
        "group_protect_idle": "  protecting: {n} rules ready (not needed while deleting is off)",
        "group_note": "  note",
        "files": "{n} files",
        "size": "{size}",
        "more_rules": "          … and {n} more",
        "builtin_workers": "running {workers} jobs in parallel, at most {per} per host",
        "builtin_ok": "  ✔ {name} ({host}) exit code {code}",
        "builtin_fail": "  ✖ {name} ({host}) exit code {code}",
        "builtin_done": "{n} job(s) completed.",
        "builtin_failed": "{n} job(s) failed.",
        "builtin_no_rsync": "error: rsync not found (for {host})",
        "interrupted": "\ninterrupted.",
        "usage_title": "parallel-rsync-git — teach parallel-rsync about .gitignore",
    },
    "zh": {
        "missing_yaml": "缺少 PyYAML，先装一下：pip install pyyaml",
        "not_a_repo": "源目录不在 git 仓库里，跳过 gitignore 规则",
        "required_repo": "group '{name}' {msg}（required: true）",
        "no_config": "错误：没有配置文件。用 -c 指定，或在其中一处建 config.yml：",
        "config_missing": "错误：找不到配置文件 {path}",
        "config_broken": "错误：配置读不出来 {path}: {exc}",
        "no_groups": "错误：配置 {path} 里没有 groups",
        "no_rsync": "错误：找不到 rsync。",
        "hint_rsync_linux": "  Linux：sudo apt install rsync   或   sudo yum install rsync",
        "hint_rsync_mac": (
            "  macOS：系统自带 rsync（较老的系统是 2.6.9）。\n"
            "         建议 brew install rsync 升级到 3.x。"
        ),
        "hint_rsync_win": (
            "  Windows：装 Git for Windows（自带 MSYS2 的 rsync）、cwRsync 或 DeltaCopy，\n"
            "          装完确认 rsync 在 PATH 里（命令行敲 rsync --version 能出版本号）。\n"
            "          想要最省事的环境，用 WSL（Ubuntu）跑这个脚本。"
        ),
        "platform": "环境",
        "rsync_missing": "rsync 没找到",
        "plan_title": "同步计划（parallel-rsync-git）",
        "plan_config": "配置",
        "plan_mode_exclude": "模式：exclude —— 跳过 git 忽略的路径",
        "plan_mode_tracked": "模式：tracked —— 只同步 git 认的文件",
        "plan_delete_on": "远端：.gitignore 里的规则会保护远端同名路径，--delete 不会删掉它们",
        "plan_delete_off": "远端：只接收新增和更新，不删除任何远端文件（delete: false）",
        "plan_delete_warn": "警告：delete_excluded=true —— 远端被忽略的文件会被删掉！",
        "group_src": "  源",
        "group_dest": "  目标",
        "group_repo": "  仓库",
        "group_repo_none": "  仓库：无",
        "group_send": "  传输",
        "group_exclude": "  排除",
        "group_exclude_hint": "git 判定被忽略的路径",
        "group_protect": "  保护",
        "group_protect_hint": ".gitignore 模式规则（远端同名路径不会被 --delete 删掉）",
        "group_protect_skipped": "  保护：跳过（rsync 3.0 以下不认识 P/R 规则）",
        "group_protect_idle": "  保护：{n} 条规则备用（当前不删除，用不上）",
        "group_note": "  提示",
        "files": "{n} 个文件",
        "size": "{size}",
        "more_rules": "          …… 还有 {n} 条",
        "builtin_workers": "并行 {workers} 个任务，同一台机器最多 {per} 个",
        "builtin_ok": "  ✔ {name} ({host}) 退出码 {code}",
        "builtin_fail": "  ✖ {name} ({host}) 退出码 {code}",
        "builtin_done": "{n} 个任务全部完成。",
        "builtin_failed": "{n} 个任务失败。",
        "builtin_no_rsync": "错误：找不到 rsync（{host}）",
        "interrupted": "\n已中断。",
        "usage_title": "parallel-rsync-git —— 给 parallel-rsync 接上 git，让它看懂 .gitignore",
    },
}


def _t(key, **kwargs):
    """取一条界面文案。"""
    table = MESSAGES.get(LANG, MESSAGES["en"])
    text = table.get(key) or MESSAGES["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text


def _pre_lang():
    """提前扫一遍 --lang，让 --help 也能用上正确的语言。"""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None


def _setup_console():
    """Windows 控制台默认不是 UTF-8，中文输出会炸 —— 统一改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 老版本或非标准流
            pass


_setup_console()


def _posix(path):
    """把路径转成斜杠分隔。git 的输出永远用斜杠，路径计算也得跟着走。"""
    text = str(path)
    if os.sep != "/":
        text = text.replace(os.sep, "/")
    if IS_WINDOWS:
        text = text.replace("\\", "/")
    return text


def _rel_posix(src, root):
    """两个路径的相对关系（斜杠分隔）。不在一个盘符下时返回空串。"""
    try:
        # 一定要先 realpath：macOS 上 /var 是 /private/var 的软链，
        # git 报出来的仓库根是解析后的路径，不统一的话相对关系会算歪
        rel = os.path.relpath(
            os.path.realpath(str(src)), os.path.realpath(str(root))
        )
    except ValueError:  # Windows 上跨盘符
        return ""
    return _posix(rel)


def _cmd_str(cmd):
    """把命令拼成一行给人看（Windows 用 Windows 的引号规则）。"""
    if IS_WINDOWS:
        return subprocess.list2cmdline(list(cmd))
    return " ".join(shlex.quote(str(c)) for c in cmd)


GITIGNORE_DEFAULTS: dict = {
    "enabled": True,
    "mode": "exclude",
    "include_git_dir": False,
    "submodules": True,
    "extra_files": [],
    "force_include": [],
    "delete_excluded": False,
    "required": False,
}

DELETE_OPTIONS = (
    "--delete",
    "--delete-after",
    "--delete-before",
    "--delete-during",
    "--delete-delay",
    "--delete-excluded",
    "--delete-missing-args",
)


def _is_delete_option(opt):
    name = opt.split("=", 1)[0]
    return name in DELETE_OPTIONS or name.startswith("--delete-")


def apply_delete_policy(cfg, delete_setting):
    """按配置的 delete 策略调整 rsync 选项。

    delete_setting 为 None：跟随配置里写的；
    True：强制开 --delete；False：强制关（远端只接收新增/更新，不做减法）。
    返回 (global_options, groups, 是否启用删除)。
    """
    global_options = list(cfg.get("global_options") or [])
    groups = [dict(g) for g in (cfg.get("groups") or [])]

    if delete_setting is True:
        if not any(_is_delete_option(o) for o in global_options):
            global_options.append("--delete")
    elif delete_setting is False:
        global_options = [o for o in global_options if not _is_delete_option(o)]
        for g in groups:
            g["options"] = [
                o for o in (g.get("options") or []) if not _is_delete_option(o)
            ]

    deleting = any(_is_delete_option(o) for o in global_options) or any(
        _is_delete_option(o) for g in groups for o in (g.get("options") or [])
    )
    return global_options, groups, deleting


def _merge_opts(*layers) -> dict:
    """按顺序叠加配置，后面的层覆盖前面的层。"""
    out = dict(GITIGNORE_DEFAULTS)
    for layer in layers:
        if isinstance(layer, dict):
            out.update({k: v for k, v in layer.items() if v is not None})
    return out


# ---------------------------------------------------------------------------
# git 小工具
# ---------------------------------------------------------------------------


def _git(args, cwd):
    """跑一条 git 命令，返回 (返回码, stdout 字节)。stderr 丢掉。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return 127, b""
    return proc.returncode, proc.stdout


def find_repo_root(path):
    """找出 path 所在 git 仓库的根目录；不在仓库里返回 None。"""
    if not path or not Path(path).exists():
        return None
    rc, out = _git(["rev-parse", "--show-toplevel"], Path(path))
    if rc != 0:
        return None
    text = out.decode("utf-8", "replace").strip()
    return Path(os.path.realpath(text)) if text else None


def repo_head_info(repo_root):
    """一句简短的话：分支 + commit。"""
    _, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    _, sha = _git(["rev-parse", "--short", "HEAD"], repo_root)
    branch = branch.decode("utf-8", "replace").strip() or "(detached)"
    sha = sha.decode("utf-8", "replace").strip()
    return f"{branch} @ {sha}" if sha else branch


def _split_nul(out):
    items = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        items.append(raw.decode("utf-8", "surrogateescape"))
    return items


def list_ignored(repo_root, scope=""):
    """列出 git 忽略的路径（相对仓库根），返回 [(路径, 是否目录)]。

    目录会折叠成 "dir/"，规则条数不会随文件数膨胀。
    """
    args = [
        "ls-files", "-z", "-o", "-i", "--exclude-standard",
        "--directory", "--no-empty-directory",
    ]
    if scope:
        args += ["--", scope]
    rc, out = _git(args, repo_root)
    if rc != 0:
        return []
    return [(p, p.endswith("/")) for p in _split_nul(out)]


def list_ignored_files(repo_root, scope):
    """列出 git 忽略的单个文件（不折叠目录），相对仓库根。"""
    args = ["ls-files", "-z", "-o", "-i", "--exclude-standard"]
    if scope:
        args += ["--", scope]
    rc, out = _git(args, repo_root)
    if rc != 0:
        return []
    return [p for p in _split_nul(out) if not p.endswith("/")]


def list_repo_files(repo_root, scope="", tracked_only=False):
    """git 会管的全部文件（已跟踪 + 未跟踪但没被忽略），相对仓库根。

    tracked_only=True 时只要已跟踪的（tracked 模式用）。
    """
    args = ["ls-files", "-z", "-c"]
    if not tracked_only:
        args += ["-o", "--exclude-standard"]
    if scope:
        args += ["--", scope]
    rc, out = _git(args, repo_root)
    if rc != 0:
        return []
    return [p for p in _split_nul(out) if not p.endswith("/")]


def list_submodules(repo_root):
    """列出子模块路径（相对仓库根）。"""
    rc, out = _git(["ls-files", "-z", "-s"], repo_root)
    if rc != 0:
        return []
    mods = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        meta, _, name = raw.partition(b"\t")
        fields = meta.split()
        if fields and fields[0] == b"160000":
            mods.append(name.decode("utf-8", "surrogateescape"))
    return mods


def _strip_scope(path, scope):
    """仓库根相对路径 -> 源目录相对路径；不在范围内返回 None。"""
    if not scope:
        return path
    prefix = scope.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    return path[len(prefix):]


# ---------------------------------------------------------------------------
# gitignore 语法匹配（给 extra_files / force_include 用）
# ---------------------------------------------------------------------------


def _translate(pattern):
    """把一条 gitignore 规则翻成正则。

    返回 (正则, 是否取反, 是否只匹配目录, 是否锚定在规则所在目录)。
    """
    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern[:-1]
    anchored = "/" in pattern
    if pattern.startswith("/"):
        pattern = pattern[1:]

    out = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
                continue
            body = pattern[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = end + 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$"), negated, dir_only, anchored


@dataclass
class _Rule:
    regex: object
    negated: bool
    dir_only: bool
    anchored: bool


class IgnoreRules:
    """一组 gitignore 风格的规则，按 git 的语义判断（最后命中者胜出）。"""

    def __init__(self, patterns):
        self.rules = []
        for raw in patterns or []:
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            regex, negated, dir_only, anchored = _translate(line.strip())
            self.rules.append(_Rule(regex, negated, dir_only, anchored))

    def __bool__(self):
        return bool(self.rules)

    def _match_one(self, rule, path, is_dir):
        if rule.dir_only and not is_dir:
            return False
        target = path if rule.anchored else path.rsplit("/", 1)[-1]
        return bool(rule.regex.match(target))

    def decide(self, path, is_dir=False):
        """True=命中规则，False=被 ! 取反，None=没规则命中。

        父目录命中时其下内容也算命中 —— 和 git 一致。
        """
        parts = [p for p in path.split("/") if p]
        verdict = None
        for idx in range(len(parts)):
            sub = "/".join(parts[:idx + 1])
            sub_is_dir = True if idx < len(parts) - 1 else is_dir
            for rule in self.rules:
                if self._match_one(rule, sub, sub_is_dir):
                    verdict = not rule.negated
        return verdict


def read_ignore_file(path):
    """读一个 gitignore 风格的文件，返回规则行。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


@dataclass
class GroupPlan:
    name: str
    src: Path
    dest: str
    repo_root: object = None
    head: str = ""
    excludes: list = field(default_factory=list)      # rsync 排除规则
    filter_rules: list = field(default_factory=list)   # .gitignore 模式规则（保护远端）
    tracked_files: object = None                     # mode=tracked 时的文件清单
    notes: list = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    extra_rules: list = field(default_factory=list)
    delete_excluded: bool = False


def _rsync_escape(path):
    """路径里的 * ? [ 是普通字符，rsync 当通配符，要转义。"""
    out = []
    for ch in path:
        if ch in "*?[":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _to_pattern(path, is_dir):
    """源目录相对路径 -> rsync 排除规则（锚定在传输根）。"""
    pat = "/" + _rsync_escape(path.strip("/"))
    if is_dir and not pat.endswith("/"):
        pat += "/"
    return pat


def _scope_of(src, repo_root):
    scope = _rel_posix(src, repo_root)
    return "" if scope == "." else scope


def _collect_ignored(src, repo_root, submodules, depth=0):
    """源目录下所有被 git 忽略的路径（相对 src），含子模块的。"""
    scope = _scope_of(src, repo_root)
    out = []
    for path, is_dir in list_ignored(repo_root, scope):
        rel = _strip_scope(path, scope)
        if rel:
            out.append((rel, is_dir))
    if not submodules or depth > 4:
        return out
    for mod in list_submodules(repo_root):
        mod_root = repo_root / mod
        if not mod_root.is_dir():
            continue  # 子模块没初始化
        if scope and not (mod == scope or mod.startswith(scope.rstrip("/") + "/")):
            continue
        for rel, is_dir in _collect_ignored(mod_root, mod_root, submodules, depth + 1):
            out.append((f"{mod}/{rel}", is_dir))
    return out


def _expand_dir(dir_path, repo_root, src):
    """把一个被整体排除的目录拆成逐文件规则（仅在需要强推其中文件时用）。"""
    scope = _scope_of(src, repo_root)
    full_scope = f"{scope}/{dir_path}" if scope else dir_path
    files = []
    for file_path in list_ignored_files(repo_root, full_scope):
        rel = _strip_scope(file_path, full_scope)
        if rel:
            files.append(f"{dir_path}/{rel}")
    return files


def _collapse_dirs(excluded, candidates):
    """整个目录都被排除时，折叠成一条目录级规则。"""
    total, bad = {}, {}
    for f in candidates:
        parts = f.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            total[d] = total.get(d, 0) + 1
            if f in excluded:
                bad[d] = bad.get(d, 0) + 1
    fully = {d for d, t in total.items() if bad.get(d, 0) == t}

    out = []
    for f in sorted(excluded):
        parts = f.split("/")
        if any("/".join(parts[:i]) in fully for i in range(1, len(parts))):
            continue
        out.append(f)
    for d in sorted(fully):
        parts = d.split("/")
        if any("/".join(parts[:i]) in fully for i in range(1, len(parts))):
            continue
        out.append(d + "/")
    return out


def _apply_force(ignored, force, repo_root, src):
    """被 git 忽略的路径里，挑出“即使被忽略也要同步”的。

    目录级规则如果内部有强推文件，就得拆成逐文件规则，
    否则整个目录还是会被排除掉。
    """
    if not force:
        return ignored, set()
    forced = set()
    out = []
    for path, is_dir in ignored:
        if not is_dir:
            if force.decide(path, False) is True:
                forced.add(path)
                continue
            out.append((path, is_dir))
            continue
        inner = _expand_dir(path, repo_root, src)
        hits = [f for f in inner if force.decide(f, False) is True]
        if not hits:
            out.append((path, is_dir))
            continue
        forced.update(hits)
        for f in inner:
            if f not in forced:
                out.append((f, False))
    return out, forced


# ---------------------------------------------------------------------------
# .gitignore 模式 -> rsync 规则
#
# 光有“本地有哪些文件被忽略”是不够的：远端可能有本地根本没有的模型
# （各台机器自己训出来的 checkpoint），--delete 会把它们当多余文件删掉。
# 所以要把 .gitignore 里的模式本身也翻译成 rsync 规则，让远端同名的
# 路径受到保护。
# ---------------------------------------------------------------------------


def _git_path(repo_root, rel):
    """git 目录下的某个路径（能处理 worktree / 子模块的情况）。"""
    rc, out = _git(["rev-parse", "--git-path", rel], repo_root)
    if rc != 0:
        return None
    text = out.decode("utf-8", "replace").strip()
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else repo_root / path


def collect_ignore_sources(repo_root, submodules=True, depth=0):
    """收集各处的忽略规则，按优先级从低到高排列。

    优先级（gitignore(5)：高 -> 低）：
      命令行 > 各层 .gitignore（越深越高）> $GIT_DIR/info/exclude > core.excludesFile

    base 是规则所在目录（相对仓库根）；子模块的规则会带上子模块的路径前缀，
    优先级排在主仓库之后（更深 = 优先级更高）。
    """
    sources = []
    rc, out = _git(["config", "--get", "core.excludesFile"], repo_root)
    if rc == 0:
        text = out.decode("utf-8", "replace").strip()
        if text:
            path = Path(os.path.expanduser(text))
            if path.is_file():
                sources.append(("", read_ignore_file(path)))
    exclude_file = _git_path(repo_root, "info/exclude")
    if exclude_file and exclude_file.is_file():
        sources.append(("", read_ignore_file(exclude_file)))
    rc, out = _git(
        ["ls-files", "-z", "-c", "-o", "--exclude-standard", "--", ":(glob)**/.gitignore"],
        repo_root,
    )
    nested = []
    for path in _split_nul(out):
        base = os.path.dirname(path)
        nested.append((base.count("/"), base, path))
    for _, base, path in sorted(nested):
        full = repo_root / path
        if full.is_file():
            sources.append((base, read_ignore_file(full)))
    if submodules and depth < 4:
        for mod in list_submodules(repo_root):
            mod_root = repo_root / mod
            if not mod_root.is_dir():
                continue  # 子模块没初始化
            for base, rules in collect_ignore_sources(mod_root, True, depth + 1):
                sources.append((f"{mod}/{base}" if base else mod, rules))
    return sources


def _pattern_prefix(pattern):
    """模式里通配符之前的字面部分。"""
    for i, ch in enumerate(pattern):
        if ch in "*?[":
            return pattern[:i]
    return pattern


def _rebase(full, scope):
    """仓库根相对的模式 -> 传输根相对。返回 None 表示与传输根无关。"""
    if not scope:
        return full
    prefix = _pattern_prefix(full)
    marker = scope.rstrip("/") + "/"
    if not prefix.startswith(marker):
        return None
    return full[len(marker):]


def translate_sources(sources, scope):
    """把各处的规则翻成 [(是否取反, rsync 模式, 用于比对的模式)]。

    顺序按优先级从低到高 —— git 是“最后命中者胜出”。
    """
    entries = []
    for base, rules in sources:
        # base 相对传输根的位置：空 = 就是传输根本身
        if not scope:
            rel_base = base
        elif base == scope:
            rel_base = ""
        elif base.startswith(scope.rstrip("/") + "/"):
            rel_base = base[len(scope.rstrip("/")) + 1:]
        elif scope.startswith(base.rstrip("/") + "/"):
            rel_base = ""  # 规则所在目录是传输根的上级
        else:
            continue  # 这份规则跟传输根没关系
        for raw in rules:
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            line = line.strip()
            negated = line.startswith("!")
            if negated:
                line = line[1:].strip()
            if not line:
                continue
            dir_only = line.endswith("/")
            body = line[:-1] if dir_only else line
            if not body:
                continue
            if "/" in body:
                # 含斜杠 = 锚定在规则所在目录
                full = f"{base}/{body}" if base else body
                rebased = _rebase(full, scope)
                if not rebased:
                    continue
                pat = "/" + rebased + ("/" if dir_only else "")
            else:
                # 不含斜杠 = 任意层级按名字匹配；子模块里的这种规则要限定在
                # 子模块目录内，别把主仓库里同名的目录也算进去
                if rel_base:
                    pat = f"{rel_base}/**/{body}" + ("/" if dir_only else "")
                else:
                    pat = body + ("/" if dir_only else "")
            # 第三项用于“后面还有没有规则也命中它”的比对，统一用去掉前导 / 的形式
            body_m = pat.lstrip("/")
            regex, _, _, anchored = _translate(body_m)
            entries.append((negated, pat, body_m, regex, anchored))
    return entries


def _rule_hits(rule, path):
    """一条规则是否命中这个路径（含父目录命中）。"""
    _, _, _, regex, anchored = rule
    parts = [p for p in path.split("/") if p]
    for idx in range(len(parts)):
        sub = "/".join(parts[: idx + 1])
        target = sub if anchored else sub.rsplit("/", 1)[-1]
        if regex.match(target):
            return True
    return False


def build_filter_rules(entries):
    """把翻译好的规则变成 rsync 过滤规则（只作用于接收端）。

    rsync 是“第一个命中者胜出”，git 是“最后一个命中者胜出”，所以要把
    取反规则提到前面；要是后面还有规则也命中它，那条取反本身就是废的
    （git 里会被后面的规则盖掉），直接丢掉。
    """
    negations = []
    exclusions = []
    for idx, entry in enumerate(entries):
        negated, pat, body = entry[0], entry[1], entry[2]
        if negated:
            if any(
                not e[0] and _rule_hits(e, body) for e in entries[idx + 1:]
            ):
                continue
            negations.append(pat)
        else:
            exclusions.append(pat)
    # P = 只保护（远端不许被 --delete 删掉），R = 只置险（远端可以删）。
    # 两者都只作用于接收端，不会影响“传什么”，所以本地该传的文件一定传。
    rules = [f"R {pat}" for pat in negations]
    rules += [f"P {pat}" for pat in exclusions]
    seen = set()
    out = []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            out.append(rule)
    return out


_CORE_CACHE: dict = {}


def _plan_core(src, opts):
    """算出源目录的排除清单和保护规则。

    同一份源目录被多个 group 指向时（同步到多台机器），算一次就够。
    """
    key = (
        str(src),
        repr(sorted((k, repr(v)) for k, v in opts.items())),
    )
    if key in _CORE_CACHE:
        return _CORE_CACHE[key]

    repo_root = find_repo_root(src)
    if repo_root is None:
        _CORE_CACHE[key] = None
        return None
    scope = _scope_of(src, repo_root)

    # 1) git 的权威判定：本地哪些路径被忽略 —— 决定“传什么”
    ignored = _collect_ignored(src, repo_root, bool(opts["submodules"]))
    force = IgnoreRules(opts["force_include"])
    ignored, forced = _apply_force(ignored, force, repo_root, src)
    concrete = [_to_pattern(path, is_dir) for path, is_dir in ignored]

    # 2) .gitignore 里的模式本身 —— 决定“远端哪些路径受保护”
    entries = translate_sources(collect_ignore_sources(repo_root), scope)
    filter_rules = build_filter_rules(entries)

    # 3) 本来会同步的文件清单
    tracked_mode = opts["mode"] == "tracked"
    candidates = []
    for path in list_repo_files(repo_root, scope, tracked_only=tracked_mode):
        rel = _strip_scope(path, scope)
        if rel:
            candidates.append(rel)
    candidates.extend(f for f in forced if f not in candidates)

    # 4) 额外规则文件
    extra_excludes = []
    extra_paths = list(opts["extra_files"] or [])
    if extra_paths:
        rules = IgnoreRules([])
        for extra in extra_paths:
            extra_path = Path(extra)
            if not extra_path.is_absolute():
                extra_path = src / extra_path
            rules.rules.extend(IgnoreRules(read_ignore_file(extra_path)).rules)
        if rules:
            dropped = {f for f in candidates if rules.decide(f, False) is True}
            dropped -= forced
            if dropped:
                extra_excludes = [
                    _to_pattern(p, p.endswith("/"))
                    for p in _collapse_dirs(dropped, candidates)
                ]

    # 5) .git 目录：默认不同步（保住各机自己的提交记录）
    git_rules = []
    if not opts["include_git_dir"]:
        own_git = src / ".git"
        if own_git.is_dir():
            git_rules.append("/.git/")
        elif own_git.exists():
            git_rules.append("/.git")
        for mod in list_submodules(repo_root):
            if scope and not mod.startswith(scope.rstrip("/") + "/"):
                continue
            rel = _strip_scope(f"{mod}/.git", scope)
            if rel and rel != ".git":
                git_rules.append(_to_pattern(rel, False))

    excludes = []
    seen = set()
    for pat in concrete + extra_excludes + git_rules:
        if pat not in seen:
            seen.add(pat)
            excludes.append(pat)

    core = {
        "repo_root": repo_root,
        "excludes": excludes,
        "filter_rules": filter_rules,
        "candidates": candidates,
        "forced": forced,
        "extra_rules": extra_paths,
    }
    _CORE_CACHE[key] = core
    return core


def plan_group(group, global_git):
    """为一个 group 算出排除清单。"""
    name = group.get("name", "<unnamed>")
    src_raw = group.get("src") or ""
    dest = group.get("dest") or ""
    opts = _merge_opts(global_git, group.get("gitignore"))
    plan = GroupPlan(
        name=name,
        src=Path(src_raw),
        dest=dest,
        delete_excluded=bool(opts["delete_excluded"]),
    )

    if not opts["enabled"]:
        plan.notes.append("已关闭（enabled: false）")
        return plan

    repo_root = find_repo_root(plan.src)
    if repo_root is None:
        msg = _t("not_a_repo")
        if opts["required"]:
            raise SystemExit(_t("required_repo", name=name, msg=msg))
        plan.notes.append(msg)
        return plan
    plan.repo_root = repo_root
    plan.head = repo_head_info(repo_root)
    core = _plan_core(plan.src, opts)
    plan.excludes = list(core["excludes"])
    plan.filter_rules = list(core["filter_rules"])
    plan.extra_rules = list(core["extra_rules"])
    candidates = list(core["candidates"])

    if opts["mode"] == "tracked":
        # 只列真实存在的文件：git 里已登记但工作区删掉的文件会让 rsync 报错
        plan.tracked_files = sorted(
            f for f in candidates if (plan.src / f).exists()
        )
    plan.file_count = len(candidates)
    plan.total_bytes = _measure(plan.src, candidates)
    return plan


def _measure(src, files):
    total = 0
    for rel in files:
        try:
            total += (Path(src) / rel).lstat().st_size
        except OSError:
            continue
    return total


def build_plans(cfg):
    global_git = cfg.get("gitignore") or {}
    return [plan_group(group, global_git) for group in cfg.get("groups") or []]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _human(size):
    size = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def print_report(plans, cfg, config_path):
    opts = _merge_opts(cfg.get("gitignore"))
    global_options, groups, deleting = apply_delete_policy(cfg, cfg.get("delete"))
    print("=" * 70)
    print(f"  {_t('plan_title')}")
    print("=" * 70)
    print(f"{_t('plan_config')} : {config_path}")
    print(f"{_t('platform')} : {_platform_info()}")
    if opts["mode"] == "tracked":
        print(_t("plan_mode_tracked"))
    else:
        print(_t("plan_mode_exclude"))
    if deleting:
        print(_t("plan_delete_on"))
    else:
        print(_t("plan_delete_off"))
    if opts["delete_excluded"]:
        print(_t("plan_delete_warn"))
    print()
    for plan in plans:
        print(f"▸ {plan.name}")
        print(f"{_t('group_src')}   : {plan.src}")
        print(f"{_t('group_dest')} : {plan.dest}")
        if plan.repo_root:
            print(f"{_t('group_repo')} : {plan.repo_root}  [{plan.head}]")
        else:
            print(_t("group_repo_none"))
        print(
            f"{_t('group_send')} : {_t('files', n=plan.file_count)} / {_human(plan.total_bytes)}"
        )
        if plan.excludes:
            print(
                f"{_t('group_exclude')} : {len(plan.excludes)} — "
                f"{_t('group_exclude_hint')}"
            )
            for pat in plan.excludes[:30]:
                print(f"          {pat}")
            if len(plan.excludes) > 30:
                print(_t("more_rules", n=len(plan.excludes) - 30))
        else:
            print(f"{_t('group_exclude')} : -")
        if plan.filter_rules and deleting:
            if not rsync_supports("prisk"):
                print(_t("group_protect_skipped"))
            else:
                print(
                    f"{_t('group_protect')} : {len(plan.filter_rules)} — "
                    f"{_t('group_protect_hint')}"
                )
                for pat in plan.filter_rules[:30]:
                    print(f"          {pat}")
                if len(plan.filter_rules) > 30:
                    print(_t("more_rules", n=len(plan.filter_rules) - 30))
        elif plan.filter_rules:
            print(_t("group_protect_idle", n=len(plan.filter_rules)))
        for note in plan.notes:
            print(f"{_t('group_note')} : {note}")
        print()


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def find_parallel_rsync():
    """找到 parallel-rsync 本体。"""
    found = shutil.which("parallel-rsync")
    if found:
        return found
    home = Path.home()
    candidates = []
    if IS_WINDOWS:
        exe = "parallel-rsync.exe"
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "uv" / "tools" / "parallel-rsync" / "Scripts" / exe)
        candidates.append(home / "AppData" / "Roaming" / "uv" / "tools" / "parallel-rsync" / "Scripts" / exe)
    else:
        candidates += [
            home / ".local/bin/parallel-rsync",
            home / ".local/share/uv/tools/parallel-rsync/bin/parallel-rsync",
            Path("/opt/homebrew/bin/parallel-rsync"),
            Path("/usr/local/bin/parallel-rsync"),
        ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def find_rsync():
    """找到 rsync 本体（各平台常见位置都找一遍）。"""
    found = shutil.which("rsync")
    if found:
        return found
    candidates = []
    if IS_WINDOWS:
        for var in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "Git" / "usr" / "bin" / "rsync.exe")
                candidates.append(Path(base) / "cwRsync" / "bin" / "rsync.exe")
        candidates.append(Path.home() / "AppData/Roaming/Git/usr/bin/rsync.exe")
    else:
        candidates += [
            Path("/opt/homebrew/bin/rsync"),   # macOS Homebrew (Apple Silicon)
            Path("/usr/local/bin/rsync"),       # macOS Homebrew (Intel) / Linux
            Path("/usr/bin/rsync"),
        ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


_RSYNC_VERSION: tuple = ()


def rsync_version():
    """rsync 的版本号，探测不到就返回空元组。"""
    global _RSYNC_VERSION
    if _RSYNC_VERSION:
        return _RSYNC_VERSION
    exe = find_rsync()
    if not exe:
        return ()
    try:
        proc = subprocess.run(
            [exe, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        return ()
    text = proc.stdout.decode("utf-8", "replace")
    m = re.search(r"rsync\s+version\s+(\d+)\.(\d+)\.(\d+)", text)
    _RSYNC_VERSION = tuple(int(x) for x in m.groups()) if m else ()
    return _RSYNC_VERSION


def rsync_supports(feature):
    """rsync 支不支持某个特性。

    "from0"   —— NUL 分隔的文件清单（3.2.4+）
    "prisk"   —— P / R 过滤规则（3.0+）
    """
    ver = rsync_version()
    if not ver:
        return False
    if feature == "from0":
        return ver >= (3, 2, 4)
    if feature == "prisk":
        return ver >= (3, 0, 0)
    return False


def _rsync_install_hint():
    """按平台给出装 rsync 的建议。"""
    if IS_WINDOWS:
        return _t("hint_rsync_win")
    if IS_MAC:
        return _t("hint_rsync_mac")
    return _t("hint_rsync_linux")


def _platform_info():
    """给报告用的一行环境说明。"""
    name = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
    ver = rsync_version()
    rs = f"rsync {'.'.join(str(x) for x in ver)}" if ver else _t("rsync_missing")
    return f"{name} / Python {sys.version.split()[0]} / {rs}"

def _has_other_from_options(options):
    """配置里是否还有别的 *-from 文件（会跟 --from0 抢格式）。"""
    for opt in options:
        name = opt.split("=", 1)[0]
        if name.endswith("-from") and name != "--files-from":
            return True
    return False


def _rel_to_cwd(path):
    """临时清单文件用相对路径。

    Windows 上 "C:/xxx" 会被 rsync 当成 "主机:路径"，相对路径就没这问题。
    rsync 运行时的工作目录就是本程序的工作目录（内置并行器会另外换目录，
    那时会把相对路径还原成绝对路径）。
    """
    try:
        return os.path.relpath(str(path), os.getcwd())
    except ValueError:  # Windows 跨盘符
        return str(path)


def _absolutize_list_args(arg, cwd, copied):
    """清单文件（--files-from / --filter=merge）挪到工作目录里，用相对路径引用。

    Windows 上 rsync 会把 "C:/xxx" 当成 "主机:路径" 而报错，
    所以这类文件一律给 rsync 一个不含盘符的相对路径。
    """
    for prefix in ("--files-from=", "--filter=merge "):
        if arg.startswith(prefix):
            value = arg[len(prefix):]
            if not value:
                return arg
            source = os.path.abspath(value)
            target = os.path.join(cwd, os.path.basename(value))
            try:
                shutil.copyfile(source, target)
                copied.append(target)
            except OSError:
                return arg
            return prefix + os.path.basename(target)
    return arg


def build_derived_config(cfg, plans, tmpdir):
    """生成喂给 parallel-rsync 的配置：把排除规则挂到各 group 上。"""
    global_options, groups, deleting = apply_delete_policy(cfg, cfg.get("delete"))
    derived = {k: v for k, v in cfg.items() if k != "gitignore"}
    derived_groups = []
    for group, plan in zip(groups, plans):
        g = dict(group)
        g.pop("gitignore", None)
        options = list(g.get("options") or [])
        for pat in plan.excludes:
            # 用 --exclude 而不是 --filter=-：前者连 rsync 2.6.9 和
            # macOS 自带的 openrsync 都认，兼容性更好
            options.append(f"--exclude={pat}")
        # .gitignore 模式规则可能上千条（子模块里几十个 .gitignore），
        # 走 merge 文件，别把命令行撑爆
        # 只在真的要删除时才需要保护规则：它们的作用只是拦住 --delete
        if plan.filter_rules and deleting and rsync_supports("prisk"):
            if len(plan.filter_rules) > 20:
                rules_file = tmpdir / f"rules-{abs(hash(plan.name))}.rules"
                rules_file.write_text(
                    "\n".join(plan.filter_rules) + "\n", encoding="utf-8"
                )
                options.append(f"--filter=merge {_rel_to_cwd(rules_file)}")
            else:
                options.extend(f"--filter={rule}" for rule in plan.filter_rules)
        if plan.tracked_files is not None:
            list_file = tmpdir / f"files-{abs(hash(plan.name))}.list"
            if _has_other_from_options(global_options + options) or not rsync_supports("from0"):
                # 没有 --from0（rsync < 3.2.4）就用换行分隔
                list_file.write_text("\n".join(plan.tracked_files), encoding="utf-8")
            else:
                list_file.write_bytes(
                    "\0".join(plan.tracked_files).encode("utf-8", "surrogateescape") + b"\0"
                )
                options.append("--from0")
            options.append(f"--files-from={_rel_to_cwd(list_file)}")
            options.append("-r")
        g["options"] = options
        derived_groups.append(g)
    derived["groups"] = derived_groups
    derived["global_options"] = global_options
    path = tmpdir / "derived.yml"
    path.write_text(
        yaml.safe_dump(derived, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def resolve_config_path(explicit):
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(_t("config_missing", path=explicit))
        return explicit
    for candidate in config_candidates():
        if candidate.is_file():
            return candidate
    raise SystemExit(
        _t("no_config")
        + "\n  "
        + "\n  ".join(str(c.parent / "config.yml") for c in config_candidates()[:4])
    )


def config_candidates():
    """按平台列出配置文件的可能位置（XDG 优先，跟 parallel-rsync 本体保持一致）。"""
    dirs = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        dirs.append(Path(xdg) / "parallel-rsync")
    home = Path.home()
    dirs.append(home / ".config" / "parallel-rsync")
    if IS_MAC:
        dirs.append(home / "Library" / "Application Support" / "parallel-rsync")
    if IS_WINDOWS:
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                dirs.append(Path(base) / "parallel-rsync")
    out = []
    for d in dirs:
        for name in ("config.yml", "config.yaml"):
            out.append(d / name)
    return out


def preview_command(cfg, plan):
    """按 parallel-rsync 的拼法预览一条 rsync 命令。"""
    global_options, groups, deleting = apply_delete_policy(cfg, cfg.get("delete"))
    group = dict(next((g for g in groups if g.get("name") == plan.name), {}))
    options = list(group.get("options") or [])
    for pat in plan.excludes:
        options.append(f"--exclude={pat}")
    if deleting and rsync_supports("prisk"):
        options.extend(f"--filter={rule}" for rule in plan.filter_rules)
    group["options"] = options
    cmd, cwd = build_rsync_cmd(group, global_options)
    if cwd:
        # 本地源目录用相对路径 + 工作目录，Windows 上才不会把盘符当主机名
        return f"(cd {shlex.quote(cwd)} && {' '.join(shlex.quote(c) for c in cmd)})"
    return " ".join(shlex.quote(c) for c in cmd)


def build_rsync_cmd(group, global_options):
    """按 parallel-rsync 的拼法组装一条 rsync 命令。

    返回 (命令, 工作目录)。本地源目录会换成"相对路径 + 指定工作目录"，
    这样 Windows 上 "C:/xxx" 不会被 rsync 当成 "主机:路径"（它会试图 ssh
    到一台叫 C 的机器上去）。
    """
    src = group.get("src")
    dest = group.get("dest")
    if not src or not dest:
        raise ValueError(f"Group '{group.get('name', '<unnamed>')}' 缺 src 或 dest。")
    excludes = group.get("exclude_options") or []
    kept = [
        o
        for o in (global_options or [])
        if not any(o == e or o.startswith(e + "=") for e in excludes)
    ]
    cwd = None
    if not _is_remote_path(src):
        abs_src = os.path.abspath(src)
        cwd = os.path.dirname(abs_src) or "."
        src = os.path.basename(abs_src) + "/"
        if not _is_remote_path(dest):
            dest = os.path.relpath(os.path.abspath(dest), cwd)
    elif not src.endswith("/"):
        src += "/"
    return ["rsync", *kept, *(group.get("options") or []), src, dest], cwd


def _is_remote_path(path):
    """rsync 会不会把这个路径当成"主机:路径"。"""
    if not path:
        return False
    if path.startswith("rsync://"):
        return True
    head = path.split("/", 1)[0]
    if ":" not in head:
        return False
    # Windows 盘符（C:）不算主机名
    if len(head) == 2 and head[1] == ":":
        return False
    return True


def extract_host(dest):
    """从 rsync 目标里取出主机名（本地目标返回 local）。"""
    if not dest:
        return "local"
    if dest.startswith("rsync://"):
        return dest.split("://", 1)[1].split("/", 1)[0]
    if "::" in dest:
        return dest.split("::", 1)[0].split("@")[-1]
    head = dest.split("/", 1)[0]
    if len(head) == 2 and head[1] == ":":
        return "local"          # Windows 盘符
    if ":" in head:
        return head.split(":", 1)[0].split("@")[-1]
    return "local"


def run_builtin(cfg, global_options, groups, dry_run=False):
    """内置并行执行器 —— 没装 parallel-rsync 也能用（自带并发与每主机限流）。"""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    try:
        workers = max(1, int(cfg.get("workers") or 4))
    except (TypeError, ValueError):
        workers = 4
    try:
        max_per_host = max(1, int(cfg.get("max_per_host") or 2))
    except (TypeError, ValueError):
        max_per_host = 2

    opts = list(global_options)
    if dry_run and "--dry-run" not in opts:
        opts.append("--dry-run")

    semaphores: dict = {}
    guard = threading.Lock()
    copied: list = []

    def run_one(group):
        cmd, cwd = build_rsync_cmd(group, opts)
        if cwd:
            # 工作目录换了，清单文件挪过去、用相对路径引用
            cmd = [_absolutize_list_args(c, cwd, copied) for c in cmd]
        host = extract_host(group.get("dest", ""))
        with guard:
            sem = semaphores.setdefault(host, threading.Semaphore(max_per_host))
        with sem:
            print(f"→ {host}: {_cmd_str(cmd)}", flush=True)
            try:
                proc = subprocess.run(cmd, cwd=cwd)
                code = proc.returncode
            except FileNotFoundError:
                print(
                    _t("builtin_no_rsync", host=host), file=sys.stderr, flush=True
                )
                code = 127
            except KeyboardInterrupt:
                code = 130
        return group.get("name", "<unnamed>"), host, code

    print(
        _t("builtin_workers", workers=workers, per=max_per_host), file=sys.stderr
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run_one, groups))

    for path in copied:
        try:
            os.remove(path)
        except OSError:
            pass

    failed = [r for r in results if r[2] != 0]
    print()
    for name, host, code in results:
        mark = "✔" if code == 0 else "✖"
        key = "builtin_ok" if code == 0 else "builtin_fail"
        print(_t(key, name=name, host=host, code=code))
    print()
    if failed:
        print(_t("builtin_failed", n=len(failed)), file=sys.stderr)
        return 1
    print(_t("builtin_done", n=len(results)))
    return 0


def main():
    global LANG
    lang = _pre_lang()
    if lang in ("en", "zh"):
        LANG = lang

    parser = argparse.ArgumentParser(
        prog="parallel-rsync-git",
        description=_t("usage_title"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
        "  parallel-rsync-git --report     show the plan (no network)\n"
        "  parallel-rsync-git --dry-run    rehearse the sync\n"
        "  parallel-rsync-git              do the sync\n",
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=None, help="path to the YAML config file"
    )
    parser.add_argument(
        "--report", action="store_true", help="print the sync plan and exit (no network)"
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="print the rsync commands that would run, then exit",
    )
    parser.add_argument(
        "--builtin",
        action="store_true",
        help="use the built-in parallel runner (no parallel-rsync needed)",
    )
    parser.add_argument(
        "--use-parallel-rsync",
        action="store_true",
        help="force the parallel-rsync binary even where the built-in runner is preferred",
    )
    parser.add_argument(
        "--lang",
        choices=["en", "zh"],
        default=None,
        help="UI language (default: en, or zh for a Chinese locale)",
    )
    args, passthrough = parser.parse_known_args()
    passthrough = [a for a in passthrough if a != "--"]

    config_path = resolve_config_path(args.config)
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise SystemExit(_t("config_broken", path=config_path, exc=exc))
    if not isinstance(cfg, dict) or not cfg.get("groups"):
        raise SystemExit(_t("no_groups", path=config_path))

    plans = build_plans(cfg)

    if args.report:
        print_report(plans, cfg, config_path)
        return

    if not find_rsync():
        raise SystemExit(_t("no_rsync") + "\n" + _rsync_install_hint())

    with tempfile.TemporaryDirectory(prefix="prsync-git-") as tmp:
        tmpdir = Path(tmp)
        derived_path = build_derived_config(cfg, plans, tmpdir)

        if args.print_command:
            for plan in plans:
                print(preview_command(cfg, plan))
            return

        # Windows 上 parallel-rsync 走绝对路径，rsync 会把 "C:" 当成主机名，
        # 所以 Windows 默认用自己的内置并行器（它用相对路径 + 工作目录）
        use_upstream = not args.builtin and not args.use_parallel_rsync
        if IS_WINDOWS and not args.use_parallel_rsync:
            use_upstream = False
        binary = find_parallel_rsync() if use_upstream else None
        if not binary:
            # 没装 parallel-rsync 也能跑：走内置并行器
            derived = yaml.safe_load(derived_path.read_text(encoding="utf-8"))
            raise SystemExit(
                run_builtin(
                    derived,
                    derived.get("global_options") or [],
                    derived.get("groups") or [],
                    dry_run="--dry-run" in passthrough,
                )
            )

        cmd = [binary, "-c", str(derived_path), *passthrough]
        print("→ " + _cmd_str(cmd), file=sys.stderr)
        try:
            result = subprocess.run(cmd)
        except KeyboardInterrupt:
            print(_t("interrupted"), file=sys.stderr)
            raise SystemExit(130)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
