"""MCP transport, durable release tracking and caller-side Nacos checks."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid
from .common import FlowError, bearer_authorization, redact, write_json, utc_now


def tool_data(result):
    if result.isError:
        raise FlowError("MCP 工具返回错误；未将可能含敏感信息的原始响应写入报告。")
    data = getattr(result, "structuredContent", None)
    if data is None:
        text = next((b.text for b in result.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise FlowError("MCP 返回的业务数据不是有效 JSON。") from exc
    if not isinstance(data, dict):
        raise FlowError("MCP 返回的业务数据不是对象。")
    return data


@asynccontextmanager
async def connect(entry):
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        import httpx
    except ImportError as exc:
        raise FlowError('部署需要可选依赖：在 tools-box 目录执行 .venv/bin/python -m pip install -e ".[deploy]"。') from exc
    kind = entry.get("transport", "http")
    if kind == "http":
        url = entry.get("url", "")
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FlowError("MCP URL 格式错误；不能包含凭据或查询参数。")
        configured = entry.get("http_headers", {}).get("Authorization")
        if configured is not None:
            authorization = bearer_authorization(configured)
        else:
            # Compatibility for previously configured environment references.
            token_env = entry.get("token_env", "")
            token = os.environ.get(token_env, "")
            if not token:
                raise FlowError("缺少 MCP Key；请配置 http_headers.Authorization，或提供旧配置引用的 Key 环境变量。")
            authorization = bearer_authorization(token)
        async with httpx.AsyncClient(headers={"Authorization": authorization},
                                     timeout=httpx.Timeout(40, connect=20)) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    elif kind == "stdio":
        command = entry.get("command")
        if not command:
            raise FlowError("stdio 发布器缺少 command。")
        child_env = dict(os.environ)
        for name, source in entry.get("env_refs", {}).items():
            if source not in os.environ:
                raise FlowError(f"缺少子进程所需环境变量：{source}")
            child_env[name] = os.environ[source]
        params = StdioServerParameters(command=command, args=entry.get("args", []), env=child_env)
        # Child diagnostic output may contain secrets; never relay it to the terminal.
        with open(os.devnull, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
    else:
        raise FlowError("发布器 transport 必须为 http 或 stdio。")


async def discover(session, entry):
    jobs = tool_data(await session.call_tool("list_jobs", {}))
    services = tool_data(await session.call_tool("list_services", {}))
    meta = services.get("_local_publisher", {})
    for key, expected_key in (("workspace", "expected_workspace"), ("mode", "expected_mode")):
        expected = entry.get(expected_key)
        if expected:
            actual = jobs.get(key, services.get(key, meta.get(key)))
            if actual != expected:
                raise FlowError(f"发布器身份不匹配：{key} 应为 {expected}。")
    return jobs, services


def eligible_jvm(repo):
    """Require a build and an actual JVM Spring Boot entry point, not just a name."""
    repo = Path(repo)
    if not ((repo / "gradlew").is_file() or (repo / "pom.xml").is_file()):
        return False
    for parent, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in {".git", ".gradle", "build", "node_modules", "dist", ".venv"}]
        path = Path(parent)
        if "src" not in path.parts or "main" not in path.parts:
            continue
        for filename in files:
            if filename.endswith((".kt", ".java")):
                try:
                    if "@SpringBootApplication" in (path / filename).read_text(errors="replace"):
                        return True
                except OSError:
                    pass
    return False


def prepare_groups(config, selections):
    groups = []
    seen_services = set()
    skipped = []
    for selection in selections:
        if not eligible_jvm(selection.repo):
            known_other = any((selection.repo / f).exists() for f in ("package.json", "pubspec.yaml", "go.mod", "Cargo.toml"))
            build_files = [selection.repo / f for f in ("build.gradle", "build.gradle.kts", "pom.xml")]
            library = any(p.is_file() and ("java-library" in p.read_text(errors="replace") or "<packaging>pom</packaging>" in p.read_text(errors="replace")) for p in build_files)
            skipped.append({"repo": str(selection.repo), "classification": "excluded" if known_other or library else "unclassified",
                            "reason": "前端/其他运行时/库不部署。" if known_other or library else "运行时或可执行入口未分类，部署阻塞；不能只凭 Gradle 文件判断。"})
            continue
        _, workspace = config.workspace(selection.repo)
        publisher = workspace["publisher"]
        config.publisher(publisher)
        service = workspace.get("service_map", {}).get(selection.repo.name, selection.repo.name)
        if not groups or groups[-1]["publisher"] != publisher:
            groups.append({"publisher": publisher, "services": [], "expected_commits": {}, "target_branches": {},
                           "request_id": "git-flow-" + uuid.uuid4().hex, "status": "prepared"})
        group = groups[-1]
        if (publisher, service) in seen_services:
            raise FlowError(f"多个仓库映射到了同一个服务：{service}")
        seen_services.add((publisher, service))
        if not selection.result.get("pushed_head"):
            raise FlowError(f"{selection.repo.name} 没有成功推送的目标分支 HEAD。")
        group["services"].append(service)
        group["expected_commits"][service] = selection.result["pushed_head"]
        group["target_branches"][service] = selection.target
    return groups, skipped


async def follow_release(session, record, save, *, poll_seconds=3):
    """Only resume a known task or repeat an uncertain submission with the same ID."""
    if not record.get("job_id"):
        record["status"] = "submitting"
        save()
        accepted = tool_data(await session.call_tool("run_job", {
            "job": "deploy-services", "parameters": {"services": record["services"]},
            "request_id": record["request_id"]}))
        if not accepted.get("job_id"):
            raise FlowError("发布器未返回 job_id；请用相同 request_id 恢复，不能创建新发布。")
        record["job_id"] = accepted["job_id"]
        save()
    last_status = None
    while True:
        response = tool_data(await session.call_tool("get_job", {
            "job_id": record["job_id"], "log_cursor": record.get("log_cursor", 0), "limit": 16000}))
        record["status"] = response.get("status", "unknown")
        if response.get("environment"):
            record["server_environment"] = response["environment"]
        # Save before displaying logs; never persist raw MCP log/error text.
        record["log_cursor"] = response.get("next_log_cursor", record.get("log_cursor", 0))
        result = response.get("result") or {}
        if "results" in result:
            record["results"] = [
                {k: row[k] for k in ("service", "status", "step", "targets", "commit", "build_tag", "deployment_applied") if k in row}
                for row in result["results"]]
        error = response.get("error") or result.get("error") or {}
        if error:
            record["error"] = {k: error[k] for k in ("code", "process_exit_code") if k in error}
            record["failed_step"] = result.get("current_step")
            record["failed_service"] = result.get("current_service")
        if record["status"] != last_status:
            print(f"发布任务 {record['job_id']}：{record['status']}")
            last_status = record["status"]
        if response.get("log"):
            print(redact(response["log"]), end="" if response["log"].endswith("\n") else "\n")
        save()
        if record["status"] not in ("queued", "running"):
            if record["status"] not in ("succeeded", "failed", "timed_out", "interrupted"):
                raise FlowError("发布器返回未知任务状态，停止跟踪。")
            if response.get("has_more"):
                continue
            break
        if not response.get("has_more"):
            await asyncio.sleep(poll_seconds)
    if record["status"] == "succeeded":
        actual = {r.get("service"): r.get("commit") for r in record.get("results", [])}
        record["commit_mismatches"] = {
            service: {"expected": expected, "actual": actual.get(service)}
            for service, expected in record["expected_commits"].items() if actual.get(service) != expected}
    save()


def transient_connection_error(exc):
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    children = getattr(exc, "exceptions", [])
    if children:
        return all(transient_connection_error(child) for child in children)
    try:
        import httpx
        import anyio
        from mcp.shared.exceptions import McpError
        if isinstance(exc, (httpx.TransportError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError)):
            return True
        if isinstance(exc, McpError) and exc.error.message.lower() == "connection closed":
            return True
    except ImportError:
        pass
    return False


async def release_group(config, record, save, *, connector=connect):
    # Reconnect only for an accepted job; never repeat an uncertain submission here.
    for attempt in range(3):
        try:
            await _release_group_once(config, record, save, connector=connector)
            return
        except Exception as exc:
            if not record.get("job_id") or not transient_connection_error(exc):
                raise
            if attempt == 2:
                raise FlowError(f"任务 {record['job_id']} 已接受，但跟踪连接中断；请用 resume 查询原任务。") from exc
            record["connection_retries"] = record.get("connection_retries", 0) + 1
            save()
            print(f"连接中断，重新连接并查询原任务 {record['job_id']}（{attempt + 1}/2）；不重复发布。")
            await asyncio.sleep((1, 3)[attempt])


async def _release_group_once(config, record, save, *, connector=connect):
    entry = config.publisher(record["publisher"])
    async with connector(entry) as session:
        jobs, services = await discover(session, entry)
        record["server_environment"] = jobs.get("environment") or services.get("environment")
        job = next((j for j in jobs.get("jobs", []) if j.get("name") == "deploy-services"), None)
        # Polling an accepted job remains allowed if new submissions have been disabled.
        if not record.get("job_id") and (not job or job.get("enabled") is False):
            raise FlowError("发布器 deploy-services 未登记或已禁用。")
        registered = set(services.get("services", []))
        missing = set(record["services"]) - registered
        if missing:
            raise FlowError("未登记的发布服务：" + ", ".join(sorted(missing)))
        details = {d["name"]: d for d in services.get("details", [])}
        registry = services.get("_local_publisher", {}).get("services", {})
        if not record.get("job_id") and record.get("status") != "submitting":
            record["branch_checks"] = {}
            for service in record["services"]:
                detail = details.get(service, {})
                if detail.get("source_ready") is False:
                    raise FlowError(f"{service} 的服务端源码未就绪。")
                target = record.get("target_branches", {}).get(service)
                declared = detail.get("expected_branch") or registry.get(service, {}).get("branch")
                check = {"target_branch": target, "publisher_branch": declared,
                         "status": "matched" if target and declared else "not_declared"}
                record["branch_checks"][service] = check
                if target and declared and target.removeprefix("refs/heads/") != declared.removeprefix("refs/heads/"):
                    check["status"] = "mismatched"
                    raise FlowError(f"{service} 已推送目标分支 {target}，MCP 声明构建分支 {declared}；停止部署，不修改服务端配置。")
                if check["status"] == "not_declared":
                    print(f"{service}：MCP 未声明构建分支或旧会话未记录目标分支；发布后核对实际 commit。")
            save()
        await follow_release(session, record, save)
    if record["status"] == "succeeded":
        record["nacos"] = await asyncio.to_thread(check_nacos, entry, record)
        save()


def http_json(url, *, data=None):
    request = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def deployed_targets(row):
    targets = []
    for target in row.get("targets", []):
        if isinstance(target, str):
            targets.append(target)
        elif isinstance(target, dict):
            name = target.get("target") or target.get("ssh")
            if name:
                targets.append(name)
    return targets


def check_nacos(entry, record, *, deadline_seconds=120, interval=5, fetch=http_json):
    """Naming v1-compatible API. Explicit mapping prevents checking unrelated instances."""
    nacos = entry.get("nacos", {})
    url = nacos.get("url", "").rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return {"status": "not_verified", "reason": "未配置有效 nacos.url（包含实际 context path）。"}
    token = os.environ.get(nacos.get("token_env", ""), "")
    if nacos.get("token_env") and not token:
        return {"status": "not_verified", "reason": "缺少 Nacos Token 环境变量。"}
    if not token and nacos.get("username_env"):
        username = os.environ.get(nacos["username_env"], "")
        password = os.environ.get(nacos.get("password_env", ""), "")
        if not username or not password:
            return {"status": "not_verified", "reason": "缺少 Nacos 认证环境变量。"}
        try:
            token = fetch(url + "/v1/auth/login", data=urlencode({"username": username, "password": password}).encode())["accessToken"]
        except Exception:
            return {"status": "not_verified", "reason": "Nacos 登录失败。"}
    mappings = entry.get("services", {})
    rows = {r.get("service"): r for r in record.get("results", [])}
    checks = []
    for service in record["services"]:
        info = mappings.get(service, {})
        targets = deployed_targets(rows.get(service, {}))
        instances = info.get("instances", [])
        if (not info.get("nacos_service") or not targets or not instances
                or any(not i.get("ip") or not isinstance(i.get("port"), int) for i in instances)
                or set(targets) != {i.get("target") for i in instances}):
            return {"status": "not_verified", "reason": f"{service} 缺少与实际部署 targets 精确对应的 Nacos 实例配置。"}
        checks.append((service, info))
    deadline = time.monotonic() + deadline_seconds
    states = {}
    while True:
        for service, info in checks:
            params = {"serviceName": info["nacos_service"], "groupName": info.get("group", "DEFAULT_GROUP"),
                      "namespaceId": info.get("namespace", ""), "healthyOnly": "false"}
            if token:
                params["accessToken"] = token
            try:
                hosts = fetch(url + "/v1/ns/instance/list?" + urlencode(params)).get("hosts", [])
                states[service] = [{"ip": i["ip"], "port": i["port"],
                                    "healthy": any(h.get("ip") == i["ip"] and h.get("port") == i["port"] and h.get("healthy") is True for h in hosts)}
                                   for i in info["instances"]]
            except Exception:
                return {"status": "not_verified", "reason": f"{service} 的 Nacos 查询失败。"}
        if all(i["healthy"] for values in states.values() for i in values):
            return {"status": "healthy", "instances": states}
        if time.monotonic() >= deadline:
            return {"status": "unhealthy", "instances": states}
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
