import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


class FlowError(Exception):
    pass


def run(argv, *, cwd=None, check=True, capture=True, env=None):
    result = subprocess.run([str(x) for x in argv], cwd=cwd,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            text=True, env=env)
    if check and result.returncode:
        operation = " ".join(str(x) for x in argv[:4])
        detail = redact((result.stderr or "")[-2000:]).strip()
        raise FlowError(f"命令失败（退出码 {result.returncode}）：{operation}" + (f"\n{detail}" if detail else ""))
    return result


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_json(path, data):
    private_dir(path.parent)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        out.write("\n")
    os.replace(temp, path)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact(value):
    value = str(value)
    value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:password|passwd|access_token|accessToken|api_key|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]+", r"\1[REDACTED]", value)
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)
    return value


def safe_error(exc):
    """Unwrap SDK task groups while keeping unknown network exceptions private."""
    if isinstance(exc, FlowError):
        return redact(exc)
    for child in getattr(exc, "exceptions", []):
        found = safe_error(child)
        if found:
            return found
    return None


def bearer_authorization(value):
    """Accept a Key or pasted Bearer header without ever including it in errors."""
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise FlowError("MCP Key 格式无效，不能包含换行。")
    token = value.strip()
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower() == "bearer":
        token = ""
    elif token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(c.isspace() for c in token):
        raise FlowError("请输入 MCP Key 或 Authorization: Bearer <MCP Key>，Key 不能为空或包含空白。")
    return "Bearer " + token
