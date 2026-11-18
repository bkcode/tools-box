import shutil
import subprocess
import sys
from .common import FlowError


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def choose(title, options, *, multi=False):
    """Return option indices; labels never become shell commands or paths."""
    if not options:
        raise FlowError(f"没有可选择的项目：{title}")
    if not interactive():
        raise FlowError("当前不是交互终端；请提供项目、分支、操作、提交范围及 --yes。")
    if shutil.which("fzf"):
        rows = [f"{i + 1}\t{label.replace(chr(10), ' ').replace(chr(9), ' ')}" for i, label in enumerate(options)]
        command = ["fzf", "--delimiter=\t", "--with-nth=2..", "--prompt", title + "> ", "--layout=reverse", "--sort"]
        bindings = "ctrl-c:abort"
        if multi:
            command.extend(["--multi", f"--header=全部 {len(options)} 项；输入筛选；↑↓ / PgUp / PgDn 浏览\nTab 多选；Ctrl-A 全选；Ctrl-D 清空；Enter 继续；Esc 退出"])
            bindings += ",ctrl-a:select-all,ctrl-d:deselect-all"
        else:
            command.append("--header=↑↓ 选择；Enter 确认并继续；Esc 退出")
        command.append("--bind=" + bindings)
        result = subprocess.run(command, input="\n".join(rows), text=True, stdout=subprocess.PIPE)
        if result.returncode in (1, 130):
            raise KeyboardInterrupt
        if result.returncode:
            raise FlowError("fzf 选择失败。")
        return [int(row.split("\t", 1)[0]) - 1 for row in result.stdout.splitlines()]
    print(f"\n{title}")
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}")
    value = input("输入编号" + ("（逗号分隔多选）" if multi else "") + "，留空取消: ").strip()
    if not value:
        raise KeyboardInterrupt
    try:
        selected = list(dict.fromkeys(int(v.strip()) - 1 for v in value.split(",")))
    except ValueError as exc:
        raise FlowError("请输入有效编号。") from exc
    if (not multi and len(selected) != 1) or any(i < 0 or i >= len(options) for i in selected):
        raise FlowError("选择编号超出范围。")
    return selected
