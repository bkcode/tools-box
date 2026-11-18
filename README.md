# tools-box

终端工具箱。每个工具有独立模块和参数，由 `tools-box <工具名>` 统一进入。
当前工具：`git-flow`，也可用快捷入口 `dev-flow`。

## 运行

需要 Python 3.11+、Git；交互推荐安装 `fzf`，没有时使用编号选择。
搜索结果按匹配度排序，完整项目名优先显示。
Git 功能仅使用 Python 标准库，可直接运行：

```bash
/var/www/tools-box/bin/tools-box list
/var/www/tools-box/bin/tools-box git-flow --help
/var/www/tools-box/bin/dev-flow
```

可把 `/var/www/tools-box/bin` 加入 PATH。启动器会定位工具箱自身，
以调用者的当前目录作为 Git 项目目录，不会进入工具箱仓库执行交付。
不要把启动器单独软链接到其他目录；需要全局命令时使用下方的包安装入口。

若要使用 MCP 部署或安装全局命令，建议使用虚拟环境：

```bash
cd /var/www/tools-box
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[deploy]'
# 然后把 /var/www/tools-box/.venv/bin 加入 PATH，或继续用 bin/ 启动器。
```

脚本不会自行安装依赖，不会绕过 Git hooks，不会自动启动任何发布任务。

## 项目选择入口

选择操作后，根据当前执行目录自动匹配配置，重叠时使用最具体的目录，跳过项目目录选择。
在项目根目录运行时显示子项目，在具体子仓库运行时只显示该仓库。
推送类操作使用“项目、原分支、目标分支”三列表格，本地 publish 使用普通项目多选。
只有未匹配时，才列出配置文件中的全部项目目录供手动选择。

```bash
# 默认：选择操作 → 自动匹配目录 → 项目与分支清单。
dev-flow

# 只读走完同一套选择流程。
dev-flow --dry-run

# 直接指定某项配置，跳过第一层目录选择。
dev-flow --name project

# 直接扫描指定目录，跳过第一层目录选择。
dev-flow --dir /path/to/projects

# 直接操作当前 Git 仓库，表格中预先勾选该仓库。
dev-flow --current-repo
```

所选目录优先扫描 `projects/`，否则扫描直接子 Git 仓库；若目录本身是单一仓库，
推送表格只显示该仓库并预先勾选。缺少目录的配置仍会显示，选中时提示目录不存在。
普通目录不会误识别为其祖先仓库，支持 worktree 与符号链接去重，不递归扫描磁盘。
无配置时仍可操作当前仓库，或通过 `--dir` 指定集合目录。

操作有三项：**推送到指定目标分支**、**推送到指定目标分支，并且部署**、**本地发布 API（publish）**。
先选择操作；推送类操作进入同一张三列表格：`项目 | 原分支 | 目标分支`。
原分支默认各仓库当前分支，目标默认 dev，可在选择项目时直接修改。

- ↑↓ / PgUp / PgDn 浏览；直接输入项目名筛选，Ctrl-U 清空筛选。
- Tab / 空格勾选；Ctrl-A 全选当前筛选结果，Ctrl-D 清空选择。
- ←→ 切换项目、原分支、目标分支列；在分支列按 Enter，或鼠标点击该列，在表格内选择分支。
- 在项目列按 Enter，或点击底部“完成选择”，结束选择并执行；Esc 取消。
- 默认不勾选整批项目。显式 --project / --current-repo 的范围会预先勾选。
- 修改分支不会自动勾选项目，只执行已勾选的行；每个项目可设置不同的原/目标分支。

默认值正确时，勾选项目后一次完成，不再按项目数量弹出原分支、目标分支选择页。
仅选择已有本地分支；不静默创建分支。完成清单时先检查全部已勾选仓库，
存在未提交内容会汇总退出；执行前再次检查。清单完成后不再询问 y。
本地 publish 仍使用普通项目多选，不显示分支配置；CLI --source / --target 和 --yes 用法保持不变。
表格使用 Python curses（macOS/Linux/WSL），不增加第三方依赖；参数化执行不需要 curses。

## 只读验证

```bash
# 在已配置工作区根目录预览指定子项目，不执行提交/合并/fetch/推送/MCP。
dev-flow deliver --dry-run --project backend

# 交互预览：可选择项目与分支，展示清单后自动结束。
dev-flow --dry-run
```

只读预览同样检查未提交内容，但不创建会话/仓库锁，也不调用 Git 执行器或 MCP。
Git 查询关闭可选的索引刷新写入。正常模式完成各项选择后直接开始执行；`--dry-run` 展示清单后直接结束。

## 参数执行

```bash
# 指定项目集合，独立选择开发分支和目标分支。
dev-flow deliver --dir /path/to/projects \
  --project service-a --project frontend-b \
  --source service-a=codex/task-a --source frontend-b=feat/task-b \
  --target service-a=dev --target frontend-b=test \
  --yes

# 当前项目交付到所选目标分支，再由匹配的 MCP 发布 JVM 服务。
dev-flow deploy --source codex/task-a --target test \
  --yes
```

`--source` / `--target` 支持一个公共分支和项目专属覆盖，例如
`--source codex/task --source service-a=feat/special`。
工具不执行 git add/git commit，不提供提交范围或提交说明参数。
已暂存、未暂存、未跟踪文件都算未提交内容：一次列出所有不干净的项目、当前分支、路径和文件状态后退出。
检查在分支选择前以及执行前进行；任一所选项目不干净时，整批不开始交付。
请先自行处理并提交改动，再重新运行。Git 已忽略的构建目录不计入未提交内容。
独立 publish 仍使用当前工作区内容，不受推送类操作的干净工作区限制。

## 本地 API 发布

```bash
# 在当前仓库直接发布 API；没有 API 模块的项目显示跳过。
dev-flow publish --current-repo --yes

# 多选/指定项目中有 API 的执行发布，其余跳过。
dev-flow publish --project service-a --project frontend-b --yes

# 仅识别模块并展示计划，不调用 Gradle。
dev-flow publish --current-repo --dry-run
```

publish 独立于 Git 交付和 MCP，使用当前工作区内容，不提交、切换分支、推送或部署。
识别 Gradle settings 中注册的 `api` / `*-api` 模块，以及同名的独立 API 根项目，
支持 Kotlin/Groovy settings、多模块路径和静态 projectDir 映射。
发布前通过模块 `tasks --all` 查询是否存在 publish 任务，缺少时跳过；
有任务则执行对应 `:module:publish`，复用项目自身的发布仓库、版本和凭据配置。
普通无 API 项目直接跳过，不启动 Gradle。缺少 gradlew、任务识别失败、编译或上传失败会报错，
停止后续发布并保留已完成结果，不自动重试、回滚或清理消费者依赖缓存。
不单独执行测试/构建，Gradle publish 自身需要的编译和打包按任务依赖正常执行。
不要求 MCP 配置；Git 参数 `--source`、`--target` 不适用于 publish。
本地 publish 仅在终端输出结果，不保存会话或 JSON 状态文件，也不通过 status/resume 查询或重跑。

## Git 交付规则

- deliver（推送到指定目标分支）：把已提交的开发分支合并到目标分支并推送，随后返回开发分支。
- deploy（推送到指定目标分支，并且部署）：先完成同一套 Git 流程，再调用匹配 MCP 部署。
- 默认 remote=origin、base=main、target=dev，目标分支仍可动态选择。
- 目标为 dev 默认先同步 main/dev，然后 main → dev，再开发分支 → dev。
- 其他目标默认不合入 main；可显式指定 `--include-base` 或 `--no-include-base`。
- 同步使用 `--ff-only`，集成使用 `--no-ff`；缺少所需本地分支/远端时报错。
- 开发分支与目标分支不能相同；工具不会自动提交本地文件改动。
- 合并整个开发分支的待交付历史，包含该分支尚未交付的所有已提交代码。执行前展示本地待交付摘要。
- 冲突保留现场；不会 abort、stash、reset、rebase、force push 或禁用 hooks。
- 推送失败保留本地集成提交；无冲突时尽可能返回开发分支，报告恢复结果。
- Git 阶段不额外执行测试、构建、lint、类型检查或安装。已有 hooks 仍正常执行。
- 多仓库不是原子事务：已完成提交/推送不会因后续失败自动撤销。

## 项目配置与执行目录匹配

用户私有配置位置：`~/.config/dev-flow/config.toml`，支持 XDG_CONFIG_HOME 和 `--config`。
每个项目只需四项：名称、项目目录地址、MCP 地址、MCP Key。

```toml
[[projects]]
name = "project"
directory = "/path/to/project"
mcp_url = "http://publisher.example.invalid:9011/mcp"
mcp_key = "<MCP Key>"
```

```bash
dev-flow setup                      # 依次填写四项；不连接、不部署
dev-flow publisher check project     # 只读连接检查
```

执行时用实际 Git 仓库路径匹配 `directory`：路径等于配置目录，或位于其子目录内即匹配。
重叠配置采用最具体的目录；普通字符串前缀相同但不是父子目录的路径不会误匹配。
启动时先按当前目录自动匹配配置；匹配成功后直接继续，未匹配才展示全部项目目录。
选择目录和子项目后，各仓库按实际路径匹配对应 MCP，无需单独配置发布器/工作区绑定。
`directory` 可填写单个仓库目录，也可填写包含多个子仓库的项目集合目录。
未匹配到配置仍可执行 Git 操作；部署需要匹配的 MCP 配置。

本地与远程服务器均通过 HTTP MCP + Key 调用，差别只是 MCP 地址和 Key。
Key 隐藏输入，支持粘贴 Key、`Bearer <Key>` 或完整请求头；保存为 `mcp_key`，
调用时自动发送 `Authorization: Bearer <Key>`。用户配置权限为 `0600`，Key 不进入日志或会话报告。
向导不再询问连接类型、环境、模式、环境变量名或工作区绑定。
旧版 publishers/workspaces 和 token_env 格式仍可读取，新向导只写入上述四项。
完整模板和可选 Nacos 配置见 [examples/config.toml](examples/config.toml)。

- Git 的目标分支每次动态选择，默认 dev；客户端不再把部署目标限制为 dev。
- 部署环境、目标服务器及构建分支由 MCP 服务端控制；客户端不保存或传递 environment。
- 所有所选仓库 Git 交付成功后才发布，服务按选择顺序传入，不自动猜依赖。
- MCP 若声明构建分支，提交任务前会与各服务已推送的目标分支核对；不一致时停止部署。
- 当前 services-only 接口不传分支参数；若服务端不声明构建分支，记录未核验，发布后核对实际 commit。
- 仅部署有 JVM 构建及 Spring Boot 入口证据的服务，再核对 MCP 注册；前端/Go/库排除，未知类型阻塞。
- 构建、API/库制品发布等内部步骤由 MCP 部署任务定义；客户端不额外询问 API/库发布授权。
  选择“推送到指定目标分支，并且部署”即执行所选服务已登记的正常部署任务，不额外提交独立制品发布任务。
- `list_jobs` / `list_services` 核验启用状态、已登记服务及手动配置的可选身份约束；环境只展示服务端返回值。
- 请求仅包含 `services` 和唯一 request_id，不发送 host、environment、shell 等参数，不自动调用 validate-services。
- 作业成功后比较实际 commit 和已推送目标分支 HEAD，输出 build_tag 和部署目标。
- 当前接口不提供服务端 commit pinning，排队期间目标分支更新会报告差异。
- 不自动替换为 SSH 或其他 MCP，不自动重试或回滚；暂停发布时不要选择 deploy，并在服务端禁用任务。

## Nacos 与恢复

健康检查属于 CLI，不属于 MCP 发布脚本。配置 Naming v1 兼容 API 地址、认证环境变量、
namespace/group/服务名以及每个目标的实例 IP/端口，见模板。
`instances[].target` 必须与任务实际返回的部署 target 精确对应；缺少映射则报告未核验。
默认每5秒检查，最多120秒（单次网络请求另有10秒超时）。健康不代表新 JAR 版本已核验。
缺少配置/权限时，报告“部署完成，Nacos 健康未确认”，不会把脚本成功当作完全成功。

Git 交付与 MCP 部署的会话文件位于 `~/.local/state/tools-box/git-flow/sessions/`，支持 XDG_STATE_HOME；本地 publish 不写入此目录。
保存仓库结果、期望 commit、request_id、job_id、日志游标；不保存 Token 和原始发布日志。

```bash
dev-flow status <会话ID>   # 本地保存的结果
dev-flow resume <会话ID>   # 跟踪原任务；提交状态未知时复用原 request_id 和相同服务列表
```

已接受任务的跟踪连接发生网络中断时，最多重新连接2次，只查询同一 job_id，不创建新发布。
连接仍不可用时保存 tracking_interrupted 状态，使用 resume 继续查询原任务。
resume 成功后清理旧跟踪错误；不会把历史连接错误与当前成功结果混在一起。
resume 不执行 Git，不重试失败的发布、不生成新发布 ID，也不启动未提交的 prepared 任务。
每仓库有本工具进程锁；异常终止遗留锁时，核查进程和现场后才手动移除。
这不能阻止使用者在其他终端同时修改同一个仓库。

退出码：0 完成（也可能无 JVM 目标而跳过发布）；2 参数/前置条件错误；
3 Git 执行失败；4 Git 已交付但部署失败或阻塞；5 部署完成但 commit 不匹配或 Nacos 未健康确认；
6 本地 API 发布失败；130 用户中断。
始终以逐项目报告判断部分完成情况。

## 开发和扩展

```bash
python3 -m unittest discover -s tests -v

# 可选：使用已有 Gradle，验证真实 API JAR 只发布到临时 file:// Maven 仓库。
TOOLS_BOX_TEST_GRADLE=/path/to/gradle python3 -m unittest discover -s tests -p test_api_publish_gradle.py -v
```

测试使用临时本地仓库、bare remote 和模拟 MCP/Nacos，不操作真实业务 Git 或环境。
新增工具时创建 `tools_box/<tool>/` 并在 `tools_box/cli.py` 的 TOOLS 注册；
不把其他工具逻辑塞入 git_flow。
