"""Local Gradle API publication, independent of Git delivery and MCP deployment."""
from collections import deque
import os
from pathlib import Path
import re
import subprocess
from .common import FlowError, redact, run


def is_api_name(name):
    name = name.lower()
    return name == "api" or name.endswith("-api")


def build_file(directory):
    return next((directory / name for name in ("build.gradle.kts", "build.gradle") if (directory / name).is_file()), None)


def api_tasks(repo):
    """Inspect registered api/*-api modules without evaluating a build script."""
    repo = Path(repo).resolve()
    settings = next((repo / name for name in ("settings.gradle.kts", "settings.gradle") if (repo / name).is_file()), None)
    text = settings.read_text() if settings else ""
    # Ignore commented declarations while preserving quoted literals and newlines.
    text = re.sub(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|//[^\n]*|/\*[\s\S]*?\*/)""",
                  lambda match: re.sub(r"[^\n]", " ", match[0]) if match[0].startswith(("//", "/*")) else match[0], text)
    modules = []
    for match in re.finditer(r"(?m)^[ \t]*include\b[ \t]*(?:\((.*?)\)|([^\n]+))", text, re.S):
        modules.extend(re.findall(r'''["']([^"'$]+)["']''', match[1] or match[2] or ""))
    remapped = {match[1].lstrip(":"): match[2] for match in re.finditer(
        r'''project\s*\(\s*["']([^"']+)["']\s*\)\s*\.projectDir\s*=\s*file\s*\(\s*["']([^"']+)["']\s*\)''', text)}
    tasks = []
    root_name = re.search(r'''rootProject\.name\s*=\s*["']([^"']+)["']''', text)
    if build_file(repo) and is_api_name(root_name[1] if root_name else repo.name):
        tasks.append(":publish")
    for module in dict.fromkeys(name.lstrip(":") for name in modules):
        parts = module.split(":")
        if not is_api_name(parts[-1]):
            continue
        if any(not part or part in (".", "..") or "/" in part or "\\" in part for part in parts):
            raise FlowError(f"API 模块路径无效：{module}")
        directory = (repo / remapped.get(module, "/".join(parts))).resolve()
        if directory != repo and repo not in directory.parents:
            raise FlowError(f"API 模块目录超出所选仓库：{module}")
        if build_file(directory):
            tasks.append(":" + module + ":publish")
    return list(dict.fromkeys(tasks))


class ApiPublishError(FlowError):
    def __init__(self, phase, exit_code, output):
        self.phase = phase
        self.exit_code = exit_code
        self.output = redact(output)[-4000:]
        super().__init__(f"API 发布失败（阶段 {phase}，退出码 {exit_code}）" + ("\n" + self.output if self.output else ""))


def publish_api_task(repo, task, *, before_publish=lambda: None):
    repo = Path(repo)
    wrapper = repo / "gradlew"
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise FlowError(f"{repo.name} 有 API 模块，但缺少可执行的 gradlew。")
    query = task.removesuffix(":publish") + ":tasks"
    metadata = run([wrapper, query, "--all", "--console=plain", "--quiet"], cwd=repo, check=False)
    if metadata.returncode:
        raise ApiPublishError("任务识别", metadata.returncode, (metadata.stdout or "") + (metadata.stderr or ""))
    if not re.search(r"(?m)^\s*(?:[\w:.-]+:)?publish(?:\s+-|\s*$)", metadata.stdout):
        return {"status": "skipped", "reason": "模块没有 publish 任务"}
    before_publish()
    print(f"发布 API：{repo.name} → {task}", flush=True)
    try:
        process = subprocess.Popen([str(wrapper), task, "--console=plain"], cwd=repo,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1)
    except OSError as exc:
        raise FlowError(f"无法启动 {repo.name} 的 Gradle 发布任务。") from exc
    tail = deque(maxlen=80)
    try:
        for line in process.stdout:
            safe = redact(line)
            tail.append(safe)
            print(safe, end="", flush=True)
        code = process.wait()
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        process.stdout.close()
    if code:
        raise ApiPublishError("publish", code, "".join(tail))
    return {"status": "published", "exit_code": 0}
