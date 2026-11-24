# parallel-rsync-git

**Teach [parallel-rsync](https://pypi.org/project/parallel-rsync/) about .gitignore.**

Sync your code to several machines at once — while every machine keeps **its own
models**. Works on **Linux, macOS and Windows**.

[![test](https://github.com/Herbert-Gao/parallel-rsync-git/actions/workflows/test.yml/badge.svg)](https://github.com/Herbert-Gao/parallel-rsync-git/actions/workflows/test.yml)

---

## Why

```
parallel-rsync only understands rsync's --exclude, and those rules have to be
hard-coded in the config file. It knows nothing about your .gitignore. So:

  1. models/, dataset/, outputs/ — big directories — get shipped to every host;
  2. worse, if the config carries --delete, the checkpoints and run artifacts
     that each machine produced locally are treated as "files the source no
     longer has" and get wiped.
```

`parallel-rsync-git` asks **git** which paths are ignored, translates the answer
into rsync rules, and hands the result to parallel-rsync. The result:

- code syncs as usual (including changes you have not committed yet);
- ignored things are not shipped;
- **files that already exist on the remote and match your ignore rules are
  protected from --delete** — every machine keeps its own checkpoints.

## Install

```
git clone https://github.com/Herbert-Gao/parallel-rsync-git.git
cd parallel-rsync-git
```

**Linux / macOS**

```
# put it on your PATH
install -m 755 parallel-rsync-git parallel_rsync_git.py ~/.local/bin/
```

**Windows**

```
copy parallel-rsync-git.bat parallel_rsync_git.py C:\Tools\parallel-rsync-git\
:: then add C:\Tools\parallel-rsync-git to your PATH
```

You can also skip the launchers entirely and run the module directly:

```
python3 parallel_rsync_git.py --report
```

### Requirements

| | |
|---|---|
| Python | 3.9+ with PyYAML (`pip install pyyaml`) |
| git | any recent version |
| rsync | 3.x recommended (`brew install rsync` / `apt install rsync`) |

`parallel-rsync` itself is **optional** — the tool ships a built-in parallel
runner and falls back to it automatically. If you do have it installed, it is
used instead (nicer progress bars).

`Windows needs an rsync that speaks the usual protocol: Git for Windows (MSYS2
rsync), cwRsync or DeltaCopy. Running the tool inside WSL is the smoothest
option.`

## Quick start

```
parallel-rsync-git --report     # show the plan, touch no network
parallel-rsync-git --dry-run    # rehearse
parallel-rsync-git              # do it
```

With no arguments it reads `~/.config/parallel-rsync/config.yml` (the same
place parallel-rsync itself looks), or you can point it somewhere with
`-c my.yml`.

Useful flags:

| flag | what it does |
|---|---|
| `--report` | print the plan and exit — nothing is sent |
| `--dry-run` | pass `--dry-run` to rsync |
| `--print-command` | print the exact rsync commands, run nothing |
| `--builtin` | use the built-in runner instead of parallel-rsync |
| `--use-parallel-rsync` | force the parallel-rsync binary (Windows prefers the built-in runner) |
| `--lang en\|zh` | UI language (default: English, or Chinese for a Chinese locale) |

Any other flag (`--workers`, `--timeout`, `--no-progress`, …) is passed
straight through to parallel-rsync.

## Configuration

Your existing parallel-rsync config keeps working — just add a `gitignore:`
block:

```
delete: false          # remote only receives new/updated files, nothing is deleted

gitignore:
  enabled: true            # master switch
  mode: exclude            # exclude (default) | tracked (only files git tracks)
  include_git_dir: false   # also sync .git? (default: no — keeps per-host commits)
  submodules: true         # honour each submodule's own .gitignore
  extra_files: []          # extra ignore files (relative to src), gitignore syntax
  force_include: []        # sync these even though git ignores them
  delete_excluded: false   # delete ignored files on the remote too (default: no)
  required: false          # fail if src is not inside a git repository
```

A group may carry its own `gitignore:` block to override the global one.

### Which ignore rules are honoured

Everything git honours, with git's own precedence:

| source | |
|---|---|
| root `.gitignore` | |
| `.gitignore` in any subdirectory | rules apply to their own subtree |
| `.git/info/exclude` | |
| `core.excludesFile` | your global excludes |
| each submodule's `.gitignore` | recursed into |
| `!` negation rules | e.g. `models/*` + `!models/README.md` |
| already-tracked files | always synced, even if they look ignored |

Precedence is git's: command line > per-directory `.gitignore` (deeper wins)
> `info/exclude` > global excludes.

### Incremental only, no deletions

```
delete: false
```

The tool strips `--delete` (and the rest of that family) from the rsync
options. Nothing is ever removed from the remote — each machine's checkpoints,
run artifacts and machine-only code stay exactly where they are. rsync is
incremental anyway, so only new and changed files are transferred.

Write `delete: true` to turn deletion on. When it is on, the tool
automatically turns your ignore rules into protection rules, so ignored paths
survive even on the remote.

### Syncing a file git ignores

```
gitignore:
  force_include:
    - "models/train_module.py"
    - "models/cotrain/final.ckpt"
```

## How it works: two rule chains

| rsync rule | scope | effect |
|---|---|---|
| `--filter=- <path>` | both sides | git says "this path is ignored" → not sent, and the remote's copy of that exact path is protected |
| `--filter=P <pattern>` | receiver only | the ignore **patterns** → remote paths matching them are **not deleted** |
| `--filter=R <pattern>` | receiver only | paths re-included with `!` → those may be cleaned up normally |

The second chain is the important one. Knowing "which local files are ignored"
is not enough: the remote may hold `models/cotrain/final.pt` that the local
machine has never seen — it is invisible locally, so it never shows up in the
ignore list, and `--delete` would happily remove it. Translating the
patterns themselves into protection rules is what stops that.

`P` and `R` rules only matter when deleting, so they are omitted entirely
when `delete: false` is set.

### About weights (.pt / .ckpt)

| | |
|---|---|
| can an existing remote `.ckpt` be **updated**? | **yes** — protection rules only decide "delete or not", never "send or not" |
| can it be **deleted** by `--delete`? | no |
| will a local `.ckpt` be **sent**? | not by default — git ignores it. Use `force_include` to push one. |

**One trap** (plain rsync behaviour, nothing to do with this tool): rsync first
compares size and modification time, and skips the file when both match. If you
changed a model's content but not its size, and the remote copy is newer, rsync
will assume there is nothing to do. Add `-c` to compare by checksum:

```
global_options:
  - "-avz"
  - "-c"
```

## Examples

See `examples/sync-multi-host.yml` for a configuration that syncs one
repository to several machines.

```
workers: 4
max_per_host: 2

delete: false

global_options:
  - "-avz"
  - "--partial"
  - "--timeout=1800"

gitignore:
  enabled: true
  mode: exclude
  include_git_dir: false
  submodules: true

groups:
  - name: "host-a"
    src: "/home/me/projects/myrepo"
    dest: "host-a:/data/myrepo"
  - name: "host-b"
    src: "/home/me/projects/myrepo"
    dest: "host-b:/data/myrepo"
```

## Platform notes

| | |
|---|---|
| **Linux** | works out of the box |
| **macOS** | the system rsync is Apple's *openrsync*, which does not know the `P`/`R` protection rules — so with it, a remote-only checkpoint is **not** protected from `--delete`. Run `brew install rsync` for a real rsync 3.x and everything works. |
| **Windows** | needs an rsync from Git for Windows / cwRsync / DeltaCopy, or run inside WSL. Two Windows quirks are handled for you: rsync mistakes a drive letter (`C:/...`) for a host name, so local paths are passed relative to a working directory; and the built-in runner is used by default (parallel-rsync passes absolute paths). NUL-separated file lists need rsync 3.2.4+. |

The tool is pure Python and has no OS-specific code paths beyond path
separators, config discovery and the rsync/parallel-rsync lookup.

## Known limitations

1. With `mode: tracked`, `--delete` does not remove extraneous files in the
   top-level directory (a plain rsync quirk with `--files-from`). Transfer
   contents are unaffected.
2. A submodule's `.gitignore` only applies inside that submodule — exactly
   like git. Files the submodule tracks are still synced.
3. `include_git_dir: false` (the default) leaves the remote's `.git` alone;
   it is neither updated nor deleted, so per-host commits survive. Set it to
   `true` to keep the remote's git directory in sync.
4. `!` negations are emulated approximately: git resolves "last match wins",
   rsync "first match wins". The tool hoists negations to the front and drops
   the ones a later rule overrides. Common patterns behave correctly, exotic
   nesting may differ.
5. There is no automatic retry. Over public links a host occasionally drops
   with an I/O error (exit code 255) — just run it again.

## Tests

```
bash test_parallel_rsync_git.sh
```

Builds a throwaway git repository and checks 25 behaviours: ignored files are
not sent, a remote-only checkpoint survives, unrelated remote files are
cleaned up, tracked files and `!` negations are sent, uncommitted code is
sent, `.git` is not sent, `force_include` / `extra_files` /
`mode: tracked` / `delete: false` all behave, and every entry point works.

## Licence

MIT — see [LICENSE](LICENSE).
