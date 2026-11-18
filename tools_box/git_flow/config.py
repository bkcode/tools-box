import getpass
import json
import os
import tempfile
import tomllib
from pathlib import Path
from .common import FlowError, bearer_authorization, private_dir


def default_config():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dev-flow" / "config.toml"


def default_state():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tools-box" / "git-flow"


class Config:
    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path else default_config()
        try:
            self.data = tomllib.loads(self.path.read_text()) if self.path.exists() else {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise FlowError(f"配置不可读取或 TOML 格式错误：{self.path}") from exc
        self.state_dir = default_state()
        projects = self.data.get("projects", [])
        if not isinstance(projects, list):
            raise FlowError("项目配置应使用 [[projects]] 数组格式。")
        names, directories = set(self.data.get("publishers", {})), set()
        for project in projects:
            if not isinstance(project, dict) or not project.get("name") or not project.get("directory"):
                raise FlowError("每个项目配置需要 name 和 directory。")
            root = Path(project["directory"]).expanduser().resolve()
            if project["name"] in names or root in directories:
                raise FlowError("项目配置名称或目录重复，请保留唯一映射。")
            names.add(project["name"])
            directories.add(root)

    def project_directories(self):
        """List selectable names/directories without exposing connection credentials."""
        entries = [{"name": p["name"], "directory": Path(p["directory"]).expanduser().resolve()}
                   for p in self.data.get("projects", [])]
        seen = {(p["name"], p["directory"]) for p in entries}
        for workspace in self.data.get("workspaces", []):
            entry = {"name": workspace["publisher"], "directory": Path(workspace["root"]).expanduser().resolve()}
            identity = (entry["name"], entry["directory"])
            if identity not in seen:
                entries.append(entry)
                seen.add(identity)
        return entries

    def publisher(self, name):
        # Normalize the four-field project format for the transport layer.
        project = next((p for p in self.data.get("projects", []) if p["name"] == name), None)
        if project is not None:
            entry = {k: v for k, v in project.items() if k != "mcp_key"}
            entry.update(transport="http", url=project.get("mcp_url", ""),
                         http_headers={"Authorization": bearer_authorization(project.get("mcp_key", ""))})
            return entry
        # Existing split publisher/workspace configurations remain readable.
        entry = self.data.get("publishers", {}).get(name)
        if not entry:
            raise FlowError(f"未找到项目配置 {name}；运行 dev-flow setup。")
        return entry

    def workspace(self, repo):
        repo = Path(repo).expanduser().resolve()
        matches = []
        for entry in self.data.get("workspaces", []):
            root = Path(entry["root"]).expanduser().resolve()
            if repo == root or root in repo.parents:
                matches.append((len(root.parts), 0, root, entry))
        for project in self.data.get("projects", []):
            root = Path(project["directory"]).expanduser().resolve()
            if repo == root or root in repo.parents:
                binding = {"publisher": project["name"], "service_map": project.get("service_map", {})}
                matches.append((len(root.parts), 1, root, binding))
        if not matches:
            raise FlowError(f"执行目录未匹配项目配置：{repo}")
        _, _, root, entry = max(matches, key=lambda x: (x[0], x[1]))
        return root, entry


def setup(path):
    """Add one project using only name, directory, MCP URL and MCP Key."""
    cfg = Config(path)
    name = input("名称（如 project）: ").strip()
    if not name or any(ord(c) < 32 for c in name):
        raise FlowError("名称不能为空或包含控制字符。")
    if name in cfg.data.get("publishers", {}) or any(p["name"] == name for p in cfg.data.get("projects", [])):
        raise FlowError("同名项目配置已存在，请编辑配置文件；不会覆盖现有配置。")
    root = Path(input("项目目录地址: ").strip()).expanduser().resolve()
    if not root.is_dir():
        raise FlowError("项目目录不存在。")
    existing = [Path(p["directory"]).expanduser().resolve() for p in cfg.data.get("projects", [])]
    existing.extend(Path(w["root"]).expanduser().resolve() for w in cfg.data.get("workspaces", []))
    if root in existing:
        raise FlowError("该目录已有项目配置，请编辑现有配置。")
    from urllib.parse import urlsplit
    url = input("MCP 地址（完整 /mcp URL）: ").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FlowError("请输入不含凭据、查询参数和片段的 HTTP(S) URL。")
    authorization = bearer_authorization(getpass.getpass("MCP Key（隐藏输入，自动发送 Authorization: Bearer）: "))
    quoted = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ["", "[[projects]]", f"name = {quoted(name)}", f"directory = {quoted(str(root))}",
             f"mcp_url = {quoted(url)}", f"mcp_key = {quoted(authorization.removeprefix('Bearer '))}", ""]
    text = (cfg.path.read_text() if cfg.path.exists() else "# dev-flow 用户私有配置；包含 MCP Key，请勿提交到仓库。\n") + "\n".join(lines)
    tomllib.loads(text)
    private_dir(cfg.path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=".dev-flow-config-", dir=cfg.path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(text)
        os.replace(temp_name, cfg.path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    print(f"已保存配置：{cfg.path}\n尚未连接或创建部署任务。")
