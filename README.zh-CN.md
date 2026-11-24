# parallel-rsync-git

**给 parallel-rsync 接上 git，让它看懂 .gitignore。**

把代码同步到多台机器，同时**各台机器的 model 保持各自独立**。
Linux / macOS / Windows 都能用。

[![test](https://github.com/Herbert-Gao/parallel-rsync-git/actions/workflows/test.yml/badge.svg)](https://github.com/Herbert-Gao/parallel-rsync-git/actions/workflows/test.yml)

[English](README.md) | 简体中文

---

## 为什么需要它

```
parallel-rsync 只认 rsync 的 --exclude，规则得写死在配置文件里，
仓库里的 .gitignore 它完全看不见。于是两头难受：

1. models/、dataset/、outputs/ 这些大目录被一箱箱搬过去；
2. 更要命的是配置里带着 --delete —— 远端各台机器自己训出来的
   checkpoint、自己攒的运行产物，会被当成"本地没有的多余文件"删掉。
```

`parallel-rsync-git` 让 git 自己去回答"哪些路径被忽略"，把答案翻译成
rsync 的规则交给 parallel-rsync 去搬：

- 代码照常同步（含本地改了还没提交的新代码）；
- 被忽略的东西不往远端搬；
- **远端已有的、命中你忽略规则的路径不受 --delete 影响** —— 各机 model 独立。

## 安装

```
git clone https://github.com/Herbert-Gao/parallel-rsync-git.git
cd parallel-rsync-git
```

**Linux / macOS**

```
# 放到 PATH 里
install -m 755 parallel-rsync-git parallel_rsync_git.py ~/.local/bin/
```

**Windows**

```
copy parallel-rsync-git.bat parallel_rsync_git.py C:\Tools\parallel-rsync-git\
:: 再把 C:\Tools\parallel-rsync-git 加进 PATH
```

也可以完全不用启动器，直接跑 Python 文件：

```
python3 parallel_rsync_git.py --report
```

### 依赖

| | |
|---|---|
| Python | 3.9+，带 PyYAML（`pip install pyyaml`） |
| git | 任意较新版本 |
| rsync | 建议 3.x（`brew install rsync` / `apt install rsync`） |

`parallel-rsync` 本体是**可选的** —— 工具自带内置并行器，没装时自动接管；
装了则优先用它（进度条更好看）。

`Windows 需要一个说人话的 rsync：Git for Windows（MSYS2 自带）、cwRsync
或 DeltaCopy。也可以直接在 WSL 里跑这个脚本，最省事。`

## 一键运行

```
parallel-rsync-git --report     # 看计划（不连远端）
parallel-rsync-git --dry-run    # 预演
parallel-rsync-git              # 真同步
```

不带参数时读 `~/.config/parallel-rsync/config.yml`（parallel-rsync 本体
也找这个位置），也可以用 `-c my.yml` 指定别的。

常用参数：

| 参数 | 作用 |
|---|---|
| `--report` | 只打印同步计划就退出，什么都不传 |
| `--dry-run` | 给 rsync 加上 `--dry-run` |
| `--print-command` | 打印实际会跑的 rsync 命令，不执行 |
| `--builtin` | 用内置并行器，不依赖 parallel-rsync |
| `--use-parallel-rsync` | 强制用 parallel-rsync 本体（Windows 上默认用内置并行器） |
| `--lang en\|zh` | 界面语言（默认英文，中文环境自动中文） |

其余参数（`--workers`、`--timeout`、`--no-progress` 等）原样转交
parallel-rsync。

## 配置

原来的 parallel-rsync 配置照用，加一段 `gitignore:` 就行：

```
delete: false          # 远端只接收新增和更新，什么都不删

gitignore:
  enabled: true            # 总开关
  mode: exclude            # exclude（默认）/ tracked（只同步 git 已跟踪的文件）
  include_git_dir: false   # 是否连 .git 目录一起同步（默认不同步，保住各机自己的提交）
  submodules: true         # 子模块自己的 .gitignore 是否也生效
  extra_files: []          # 额外的忽略规则文件（相对源目录），gitignore 语法
  force_include: []        # 即使被 git 忽略也要同步的路径（相对源目录）
  delete_excluded: false   # 是否连远端被忽略的文件一起删（默认不删）
  required: false          # 源目录不在 git 仓库里时是否直接报错
```

每个 group 也可以单独写一个 `gitignore:` 覆盖全局设置。

### 它认得哪些忽略规则

git 认的全认，优先级也跟 git 一致：

| 来源 | |
|---|---|
| 根目录 `.gitignore` | |
| 任意层级的 `.gitignore` | 子目录里的规则只管自己的子树 |
| `.git/info/exclude` | |
| `core.excludesFile` | 你的全局 excludes |
| 子模块各自的 `.gitignore` | 递归下去 |
| `!` 取反规则 | 例如 `models/*` + `!models/README.md` |
| 已被 git 跟踪的文件 | 永远同步（哪怕它长得像被忽略的） |

优先级：命令行 > 各层 `.gitignore`（越深越高）> `info/exclude` > 全局 excludes。

### 只做增量，不做减法

```
delete: false
```

工具会把 rsync 选项里的 `--delete`（以及 `--delete-after` 等一整套）去掉，
远端已有的文件一个都不会少 —— 各机自己训的 checkpoint、自己攒的运行产物、
远端独有的代码，全都原样留在远端。rsync 本来就是增量的，只传新增和变化的文件。

写 `delete: true` 开启删除，这时工具会自动把 `.gitignore` 里的模式变成
保护规则，被忽略的路径即使远端有也不会被删。

### 某个被忽略的文件也要同步

```
gitignore:
  force_include:
    - "models/train_module.py"
    - "models/cotrain/final.ckpt"
```

## 两条规则链，分别管两件事

| rsync 规则 | 作用范围 | 干什么 |
|---|---|---|
| `--filter=- <具体路径>` | 收发两端 | git 判定"本地这个路径被忽略" → 不传，远端同名文件也受保护 |
| `--filter=P <模式>` | 只接收端 | `.gitignore` 里的模式本身 → 远端匹配这个模式的文件，**不许被 --delete 删掉** |
| `--filter=R <模式>` | 只接收端 | 被 `!` 取反的路径 → 远端这类文件可以正常清理 |

第二条链才是关键。光知道"本地哪些文件被忽略"是不够的：远端可能有本地根本
没有的 `models/cotrain/final.pt`（那台机器自己训的），本地看不见，自然也
不会进忽略清单，`--delete` 就会把它当多余文件删掉。把 `.gitignore` 里的
**模式**也翻译成保护规则，才能挡住这一刀。

`P` / `R` 规则只在删除时才有意义，所以 `delete: false` 时会完全省略。

### 权重文件（.pt / .ckpt）会不会被挡住？

| | 会不会 |
|---|---|
| 远端已有的 .ckpt 能不能被新版本**更新** | **能** —— 保护规则只管"删不删"，不管"传不传" |
| 远端已有的 .ckpt 会不会被 **--delete 删掉** | 不会 |
| 本地的 .ckpt 会不会**同步过去** | 默认**不会** —— git 判定它被忽略，用 `force_include` 推 |

**一个小坑**（rsync 本身的行为，跟这个工具无关）：rsync 先比大小和修改时间，
两者都一样就认为文件没变、直接跳过。改了模型内容但**大小没变**、而远端那份
时间戳更新，rsync 就会以为不用传。要按内容比对，加 `-c`：

```
global_options:
  - "-avz"
  - "-c"
```

## 示例

`examples/sync-multi-host.yml` 是一份配置示例：一个仓库同步到多台机器。

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

## 各平台说明

| | |
|---|---|
| **Linux** | 开箱可用 |
| **macOS** | 系统自带的是 Apple 的 *openrsync*，它不认识 P/R 保护规则 —— 用它时远端独有的 checkpoint **挡不住** `--delete`。装 `brew install rsync` 换成真正的 rsync 3.x 就没这个问题了 |
| **Windows** | 需要 Git for Windows / cwRsync / DeltaCopy 的 rsync，或者直接在 WSL 里跑。两个 Windows 坑已经帮你绕开：rsync 会把盘符（`C:/...`）当成主机名，所以本地路径改用“相对路径 + 工作目录”；另外 Windows 默认走内置并行器（parallel-rsync 用的是绝对路径）。NUL 分隔的文件清单要 rsync 3.2.4+ |

工具本身是纯 Python，除了路径分隔符、配置查找、rsync/parallel-rsync 的定位，
没有别的平台相关分支。

## 已知限制

1. `mode: tracked` 下，`--delete` 不会清理根目录的多余文件
   （rsync 配 `--files-from` 的固有行为），不影响传输内容。
2. 子模块的 `.gitignore` 只在子模块内生效（和 git 一致）；
   子模块里被跟踪的文件照样同步。
3. `include_git_dir: false`（默认）下远端的 `.git` 既不更新也不删除，
   各机自己的提交记录得以保留；想同步就改成 `true`。
4. `!` 取反在 rsync 里是近似模拟的：git 是"最后命中者胜出"，rsync 是
   "第一个命中者胜出"。工具会把取反提到最前面，并丢掉被后面规则盖掉的取反。
   常见写法都能对上，极端嵌套可能有出入。
5. **SSH 偶发失败没有自动重试。** 跨公网同步时偶尔某台机器会报
   `code 255`（I/O 错误）断连，重跑一次通常就好。

## 自测

```
bash test_parallel_rsync_git.sh
```

会建一个临时 git 仓库，检查 25 条：被忽略的文件不传、远端独有的 checkpoint
保住、不相关的远端文件被清理、受版本管理的文件和 `!` 取反照常同步、
未提交的新代码也同步、`.git` 不传、`force_include` / `extra_files` /
`mode: tracked` / `delete: false` 都正常，以及各种入口都能用。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
