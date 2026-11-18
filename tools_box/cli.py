"""Tool registry. New tools register here without changing git-flow."""
import sys
from . import __version__

TOOLS = {"git-flow": ("项目/分支选择、Git 推送、部署与本地 API 发布", "tools_box.git_flow.cli")}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(__version__)
        return 0
    if not args or args[0] in ("--help", "-h", "list"):
        print("用法: tools-box <工具> [参数]\n")
        for name, (description, _) in TOOLS.items():
            print(f"  {name:16} {description}")
        print("\n运行 tools-box git-flow --help 查看工具参数。")
        return 0
    entry = TOOLS.get(args[0])
    if entry is None:
        print(f"未知工具: {args[0]}", file=sys.stderr)
        return 2
    from importlib import import_module
    return import_module(entry[1]).main(args[1:])
