import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid
from .common import FlowError, redact, safe_error, utc_now, write_json
from .config import Config, setup
from .git import (Selection, RepoLocks, branches, current_branch, discover, git,
                  git_root, ensure_clean_worktrees, status)
from .publish import connect, discover as discover_publisher, prepare_groups, release_group
from .ui import choose, interactive
from .project_table import BranchRow, select_project_branches
from .api_publish import ApiPublishError, api_tasks, publish_api_task


def parser():
    p = argparse.ArgumentParser(prog="dev-flow", description="项目多选、Git 推送、MCP 部署及本地 API 发布。",
        epilog="其他命令：setup；publisher check NAME；status SESSION；resume SESSION。Git 阶段不额外执行测试或构建。")
    p.add_argument("action", nargs="?", choices=("deliver", "deploy", "publish"), help="省略则交互选择")
    location = p.add_mutually_exclusive_group()
    location.add_argument("--dir", type=Path, help="跳过项目配置选择，直接扫描指定目录的子仓库")
    location.add_argument("--name", help="直接使用配置文件中指定名称的项目目录")
    location.add_argument("--current-repo", action="store_true", help="强制使用当前 Git 仓库，不进入工作区子项目列表")
    p.add_argument("--dry-run", action="store_true", help="只读预览：展示清单后结束，不执行 Git 变更、fetch、推送或 MCP 调用")
    p.add_argument("--project", action="append", default=[], help="项目集合目录中的仓库名，可重复；省略则多选")
    p.add_argument("--source", action="append", default=[], help="开发分支，或 项目名=分支，可重复")
    p.add_argument("--target", action="append", default=[], help="目标分支，或 项目名=分支，可重复；默认 dev")
    p.add_argument("--base", default="main", help="基准分支，默认 main")
    p.add_argument("--remote", default="origin", help="交付远端，默认 origin")
    p.add_argument("--include-base", action=argparse.BooleanOptionalAction, default=None,
                   help="先同步/合入基准分支；默认仅目标为 dev 时启用")
    p.add_argument("--yes", action="store_true", help="使用明确参数非交互执行")
    p.add_argument("--config", type=Path, help="用户 TOML 配置路径")
    return p


def overrides(values, selected):
    global_value = None
    mapped = {}
    names = {p.name for p in selected}
    for value in values:
        if "=" in value:
            name, branch = value.split("=", 1)
            if name not in names or not branch:
                raise FlowError(f"分支映射无效：{value}")
            if name in mapped:
                raise FlowError(f"项目 {name} 的分支被重复指定。")
            mapped[name] = branch
        elif global_value is None:
            global_value = value
        else:
            raise FlowError("只能指定一个公共分支；项目差异请使用 项目名=分支。")
    return {p: mapped.get(p.name, global_value) for p in selected}


def select_repos(args, config=None, *, list_only=False):
    cwd = Path.cwd().resolve()
    collection = args.dir
    candidates = None
    selected_scope = None
    if collection:
        candidates = discover(collection)
    elif not args.current_repo and config is not None:
        entries = config.project_directories()
        if args.name:
            matches = [p for p in entries if p["name"] == args.name]
            if len(matches) != 1:
                raise FlowError(f"项目配置名称不存在或对应多个目录：{args.name}，请交互选择。")
            selected_scope = matches[0]["directory"]
        elif entries:
            try:
                selected_scope, binding = config.workspace(cwd)
            except FlowError:
                selected_scope = None
            if selected_scope is not None:
                print(f"自动匹配项目：{binding['publisher']}（{selected_scope}）")
                current_root = git_root(cwd)
                if (not args.project and current_root is not None and current_root != selected_scope
                        and selected_scope in current_root.parents):
                    return [current_root]
            elif not args.project and not args.yes and interactive():
                labels = [f"{entry['name']}    {entry['directory']}" for entry in entries]
                index = choose(f"选择项目目录（共 {len(entries)} 项）", labels)[0]
                selected_scope = entries[index]["directory"]
        if selected_scope is not None:
            if not selected_scope.is_dir():
                raise FlowError(f"所选项目目录不存在：{selected_scope}")
            nested = selected_scope / "projects"
            if nested.is_dir():
                candidates = discover(nested)
                if candidates:
                    collection = nested
            if not candidates:
                candidates = discover(selected_scope)
                if candidates:
                    collection = selected_scope
            if not candidates:
                root = git_root(selected_scope)
                if root == selected_scope:
                    if args.project and set(args.project) != {root.name}:
                        raise FlowError(f"所选配置只有仓库 {root.name}，没有指定的子项目。")
                    return [root]
                raise FlowError(f"所选目录没有独立 Git 仓库：{selected_scope}")
        else:
            # Explicit repository filters/non-interactive calls retain directory matching.
            try:
                scope, _ = config.workspace(cwd)
            except FlowError:
                scope = None
            if scope is not None and cwd == scope:
                nested = scope / "projects"
                if nested.is_dir():
                    candidates = discover(nested)
                    if candidates:
                        collection = nested
                if not candidates:
                    candidates = discover(scope)
                    if candidates:
                        collection = scope
            elif scope is not None and cwd == scope / "projects":
                candidates = discover(cwd)
                if candidates:
                    collection = cwd
    if args.name and config is None:
        raise FlowError("--name 需要项目配置。")
    if collection is not None:
        candidates = candidates if candidates is not None else discover(collection)
        if not candidates:
            raise FlowError("指定目录下没有直接子 Git 仓库。")
        print(f"项目目录：{Path(collection).resolve()}（{len(candidates)} 个仓库）")
        if args.project:
            lookup = {p.name: p for p in candidates}
            missing = set(args.project) - lookup.keys()
            if missing:
                raise FlowError("找不到项目：" + ", ".join(sorted(missing)))
            return [lookup[name].resolve() for name in dict.fromkeys(args.project)]
        if list_only:
            return [p.resolve() for p in candidates]
        labels = [f"{p.name}    当前分支：{current_branch(p) or '(detached HEAD)'}" for p in candidates]
        indices = choose("选择子项目", labels, multi=True)
        return [candidates[i].resolve() for i in indices]
    if args.project:
        raise FlowError("--project 需要项目集合目录；请指定 --name 或 --dir，或在已配置的项目根目录执行。")
    root = git_root(cwd)
    if root is None:
        raise FlowError("当前目录不属于 Git 仓库；请使用 --dir <项目集合目录>。")
    return [root]


def select_branches(repos, values, *, source):
    """Resolve explicit arguments/defaults; interactive editing lives in the table."""
    mapped = overrides(values, repos)
    return {p: mapped[p] or (current_branch(p) if source else "dev") for p in repos}


def print_plan(selections, action):
    label = {"deliver": "推送到指定目标分支", "deploy": "推送到指定目标分支，并且部署"}[action]
    print(f"\n操作：{label}；Git 额外验证：跳过")
    for selection in selections:
        target = selection.target
        base = f"；先合入 {selection.base}" if selection.include_base else ""
        print(f"  {selection.repo.name}: {selection.source} → {target}{base}")
        print(f"    仓库：{selection.repo}")
        short = git(selection.repo, "status", "--short").stdout.rstrip()
        if short:
            print(short)
        if action in ("deliver", "deploy"):
            commits = git(selection.repo, "log", "--oneline", "-12", f"{selection.target}..{selection.source}").stdout.rstrip()
            count = git(selection.repo, "rev-list", "--count", f"{selection.target}..{selection.source}").stdout.strip()
            print(f"    当前本地引用中待交付 {count} 个提交（远端同步后可能变化）")
            if commits:
                print(commits)
    print("多项目按上述顺序串行执行；任一 Git 失败后停止，已完成的推送不会自动回滚。")
    if action == "deploy":
        print("全部 Git 成功后，按所选顺序向各工作区发布器提交 JVM 服务；不自动推断依赖顺序。")


def print_report(state, *, include_session=True):
    if include_session:
        print(f"\n会话：{state['id']}  状态：{state['status']}")
    else:
        print(f"\n本地 API 发布结果：{state['status']}")
    for row in state.get("git", []):
        print(f"  Git {Path(row['repo']).name}: {row['status']}")
        for key in ("feature_commit", "pushed_head", "final_branch", "restored", "error"):
            if key in row:
                print(f"    {key}: {redact(row[key])}")
        for merge in row.get("merges", []):
            print(f"    merge {merge['branch']}: {merge['commit']}")
        if row.get("conflicts"):
            print("    冲突文件：" + ", ".join(row["conflicts"]))
    for row in state.get("publications", []):
        print(f"  本地 API {Path(row['repo']).name}: {row['status']}（分支 {row['branch']}）")
        if row.get("reason"):
            print("    " + row["reason"])
        for task in row["tasks"]:
            print(f"    {task['task']}: {task['status']}" + (f"；{task['reason']}" if task.get("reason") else ""))
        if row.get("error"):
            print("    " + redact(row["error"]))
    for row in state.get("deployments", []):
        print(f"  项目配置 {row['publisher']}: {row['status']}；job_id={row.get('job_id', '尚未确认')}")
        if row.get("server_environment"):
            print(f"    MCP 服务端环境：{row['server_environment']}")
        for service, branch in row.get("branch_checks", {}).items():
            print(f"    {service} 分支核对：{json.dumps(branch, ensure_ascii=False)}")
        for service in row["services"]:
            print(f"    {service} expected_commit={row['expected_commits'][service]}")
        for service_result in row.get("results", []):
            print("    " + redact(json.dumps(service_result, ensure_ascii=False)))
        if row.get("commit_mismatches"):
            print("    构建提交与推送提交不一致：" + json.dumps(row["commit_mismatches"], ensure_ascii=False))
        print("    Nacos: " + json.dumps(row.get("nacos", {"status": "not_verified"}), ensure_ascii=False))
        if row.get("error"):
            print("    错误：" + redact(row["error"]))
    for row in state.get("skipped", []):
        print(f"  部署跳过 {Path(row['repo']).name}: {row['reason']}")
    if state.get("error") and not any(row.get("error") == state["error"] for row in state.get("publications", [])):
        print("  " + redact(state["error"]))


def deployment_exit(state):
    if any(r.get("classification") == "unclassified" for r in state.get("skipped", [])):
        return 4
    if any(r.get("status") != "succeeded" for r in state.get("deployments", [])):
        return 4
    if any(r.get("commit_mismatches") or r.get("nacos", {}).get("status") != "healthy" for r in state.get("deployments", [])):
        return 5
    return 0


def execute(args):
    config = Config(args.config)
    ask = interactive() and not args.yes
    if args.dry_run:
        print("只读预览：仅检查并展示执行清单。")
    if not args.action:
        if not ask:
            raise FlowError("非交互执行必须提供 deliver（推送）、deploy（推送并部署）或 publish（本地发布 API）。")
        action = ("deliver", "deploy", "publish")[choose("选择操作", ["推送到指定目标分支", "推送到指定目标分支，并且部署", "本地发布 API（publish）"])[0]]
    else:
        action = args.action
    if not ask and not args.yes and not args.dry_run:
        raise FlowError("非交互执行需要 --yes，明确授权所选操作。")
    chosen_rows = None
    if ask and action in ("deliver", "deploy"):
        candidates = select_repos(args, config, list_only=True)
        source_overrides = overrides(args.source, candidates)
        target_overrides = overrides(args.target, candidates)
        preset = bool(args.project or args.current_repo or len(candidates) == 1)
        rows = []
        print(f"读取 {len(candidates)} 个项目的本地分支…", flush=True)
        for repo in candidates:
            current = current_branch(repo)
            rows.append(BranchRow(repo, source_overrides[repo] or current, target_overrides[repo] or "dev",
                                  tuple(branches(repo)), current, preset))
        chosen_rows = select_project_branches(rows, validate_selected=lambda selected: ensure_clean_worktrees([row.repo for row in selected]), dry_run=args.dry_run)
        if not chosen_rows:
            raise FlowError("未选择项目。")
        repos = [row.repo for row in chosen_rows]
    else:
        repos = select_repos(args, config)
    for repo in repos:
        try:
            root, binding = config.workspace(repo)
        except FlowError:
            continue
        print(f"{repo.name} 匹配项目配置：{binding['publisher']}（{root}）")
    if action == "publish":
        return execute_api_publish(args, config, repos)
    ensure_clean_worktrees(repos)
    if chosen_rows is not None:
        sources = {row.repo: row.source for row in chosen_rows}
        targets = {row.repo: row.target for row in chosen_rows}
    else:
        sources = select_branches(repos, args.source, source=True)
        targets = select_branches(repos, args.target, source=False)
    ensure_clean_worktrees(repos)
    selections = [Selection(p, sources[p], targets[p], args.base, args.remote,
                            args.include_base if args.include_base is not None else targets[p] == "dev") for p in repos]
    for selection in selections:
        selection.preflight(action)
    print_plan(selections, action)
    if args.dry_run:
        print("只读预览结束；未执行 Git 变更、远端同步或发布任务。")
        return 0
    state = {"id": uuid.uuid4().hex, "created_at": utc_now(), "status": "running",
             "action": action, "git": [s.result for s in selections], "deployments": [], "skipped": []}
    path = config.state_dir / "sessions" / (state["id"] + ".json")
    def save():
        state["updated_at"] = utc_now()
        write_json(path, state)
    save()
    code = 0
    try:
        with RepoLocks(repos, config.state_dir):
            # Recheck every selected worktree before the first mutation.
            ensure_clean_worktrees(repos)
            for i, selection in enumerate(selections):
                selection.preflight(action)
                state["git"][i] = selection.result
            save()
            for selection in selections:
                print(f"\n处理 {selection.repo.name}…")
                try:
                    selection.execute(action)
                finally:
                    save()
                if selection.result["status"] == "restoration_failed":
                    raise FlowError("Git 已推送，但开发分支恢复失败；停止后续操作。")
            state["status"] = "git_succeeded"
            save()
            if action == "deploy":
                code = publish_selections(config, selections, state, save)
            else:
                state["status"] = "succeeded"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        state["error"] = "用户中断；已接受的部署任务可能仍在执行。使用 resume 查询原任务，不要重新提交发布。"
        code = 130
    except Exception as exc:
        git_success = all(r["status"] == "delivered" for r in state["git"])
        active = any(r.get("job_id") and r.get("status") in ("queued", "running") for r in state["deployments"])
        state["status"] = "tracking_interrupted" if active else "deployment_blocked" if action == "deploy" and git_success else "git_failed"
        state["error_type"] = type(exc).__name__
        state["error"] = safe_error(exc) or "连接或运行错误；检查配置及服务端状态，未输出可能含凭据的原始异常。"
        code = 4 if action == "deploy" and git_success else 3
    finally:
        save()
        print_report(state)
        print(f"状态文件：{path}")
    return code



def publish_selections(config, selections, state, save):
    groups, skipped = prepare_groups(config, selections)
    state["deployments"], state["skipped"] = groups, skipped
    save()
    for record in groups:
        asyncio.run(release_group(config, record, save))
        if record["status"] != "succeeded":
            break
    code = deployment_exit(state)
    state["status"] = "succeeded" if code == 0 else "deployment_failed" if code == 4 else "deployed_with_warnings"
    if not groups and code == 0:
        state["status"] = "deployment_skipped_no_jvm_targets"
    return code



def execute_api_publish(args, config, repos):
    if args.source or args.target:
        raise FlowError("publish 直接发布当前工作区 API，不使用开发分支或目标分支参数。")
    rows = []
    for repo in repos:
        tasks = api_tasks(repo)
        row = {"repo": str(repo), "branch": current_branch(repo) or "detached HEAD",
               "status": "not_run" if tasks else "skipped",
               "tasks": [{"task": task, "status": "not_run"} for task in tasks]}
        if not tasks:
            row["reason"] = "没有可识别的 API 模块，跳过"
        rows.append(row)
    print("\n操作：本地发布 API；使用当前工作区内容。")
    for row in rows:
        print(f"  {Path(row['repo']).name}：" + (", ".join(task["task"] for task in row["tasks"]) or row["reason"]))
    if args.dry_run:
        print("只读预览结束；未调用 Gradle、Git 交付或 MCP。")
        return 0
    state = {"status": "running", "action": "publish", "git": [], "deployments": [], "publications": rows}
    code = 0
    active_row = active_task = None
    try:
        with RepoLocks(repos, config.state_dir):
            for row in rows:
                active_row, active_task = row, None
                if row["status"] == "skipped":
                    continue
                row["status"] = "running"
                for task in row["tasks"]:
                    active_task = task
                    task["status"] = "checking"
                    def started():
                        task["status"] = "running"
                    task.update(publish_api_task(Path(row["repo"]), task["task"], before_publish=started))
                    if task["status"] == "skipped":
                        print(f"跳过 {Path(row['repo']).name} {task['task']}：{task['reason']}")
                row["status"] = "published" if any(task["status"] == "published" for task in row["tasks"]) else "skipped"
        state["status"] = "succeeded" if any(row["status"] == "published" for row in rows) else "publication_skipped_no_api_targets"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        if active_row:
            active_row["status"] = "interrupted"
        if active_task:
            active_task["status"] = "interrupted"
        state["error"] = "本地发布已中断；已完成的制品不会自动撤回。"
        code = 130
    except Exception as exc:
        state["status"] = "publication_failed"
        state["error"] = safe_error(exc) or "本地 API 发布失败；未输出可能含凭据的原始异常。"
        if active_row:
            active_row["status"] = "failed"
            active_row["error"] = state["error"]
        if active_task:
            active_task["status"] = "failed"
            if isinstance(exc, ApiPublishError):
                active_task.update(exit_code=exc.exit_code, phase=exc.phase)
        code = 6
    finally:
        print_report(state, include_session=False)
    return code


def session_path(config, identifier):
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise FlowError("会话 ID 必须为32位小写十六进制。")
    return config.state_dir / "sessions" / (identifier + ".json")


def session_command(argv):
    p = argparse.ArgumentParser(description="查看本地记录，或跟踪原已提交/提交状态未知的部署任务。不会执行 Git 或新建发布 ID。")
    p.add_argument("action", choices=("status", "resume"))
    p.add_argument("session")
    p.add_argument("--config", type=Path)
    args = p.parse_args(argv)
    config = Config(args.config)
    path = session_path(config, args.session)
    if not path.exists():
        raise FlowError("找不到会话状态文件。")
    with RepoLocks([path], config.state_dir):
        state = json.loads(path.read_text())
        if args.action == "status":
            print("以下为本地保存的状态；resume 会连接发布器更新原任务。")
            print_report(state)
            return 0
        records = [r for r in state.get("deployments", []) if r.get("job_id") or r.get("status") == "submitting"]
        if not records:
            raise FlowError("没有已提交或提交状态未知的发布任务可恢复；不会自动启动尚未提交的任务。")
        def save():
            state["updated_at"] = utc_now()
            write_json(path, state)
        try:
            for record in records:
                asyncio.run(release_group(config, record, save))
            code = deployment_exit(state)
            state["status"] = "succeeded" if code == 0 else "deployment_failed" if code == 4 else "deployed_with_warnings"
            state.pop("error", None)
            state.pop("error_type", None)
            return code
        finally:
            save()
            print_report(state)


def publisher_command(argv):
    p = argparse.ArgumentParser(description="只读检查 MCP 连接及注册信息，不创建发布任务。")
    p.add_argument("action", choices=("check",))
    p.add_argument("name")
    p.add_argument("--config", type=Path)
    args = p.parse_args(argv)
    config = Config(args.config)
    entry = config.publisher(args.name)
    async def inspect():
        async with connect(entry) as session:
            jobs, services = await discover_publisher(session, entry)
            print(json.dumps({"publisher": args.name, "environment": jobs.get("environment"),
                              "workspace": jobs.get("workspace"), "mode": jobs.get("mode"),
                              "jobs": [{"name": j.get("name"), "enabled": j.get("enabled", True)} for j in jobs.get("jobs", [])],
                              "services": services.get("services", [])}, ensure_ascii=False, indent=2))
    asyncio.run(inspect())
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "setup":
            p = argparse.ArgumentParser(description="配置名称、项目目录地址、MCP 地址和 MCP Key；不连接、不部署。")
            p.add_argument("--config", type=Path)
            args = p.parse_args(argv[1:])
            if not interactive():
                raise FlowError("setup 需要交互终端，或手动参照 examples/config.toml 配置。")
            setup(args.config)
            return 0
        if argv and argv[0] == "publisher":
            return publisher_command(argv[1:])
        if argv and argv[0] in ("status", "resume"):
            return session_command(argv)
        return execute(parser().parse_args(argv))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except FlowError as exc:
        print(f"错误：{redact(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print("运行失败：" + (safe_error(exc) or "请检查配置、连接和状态文件；原始异常可能包含凭据，未直接输出。"), file=sys.stderr)
        return 2
