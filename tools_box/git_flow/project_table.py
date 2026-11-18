"""One terminal table for repository selection and per-repository branch editing."""
from dataclasses import dataclass
from pathlib import Path
import os
import sys
import unicodedata
from .common import FlowError


@dataclass
class BranchRow:
    repo: Path
    source: str
    target: str
    available: tuple[str, ...]
    checked_out: str = ""
    selected: bool = False


class TableState:
    def __init__(self, rows):
        self.rows = rows
        self.order = [i for i, row in enumerate(rows) if row.selected]
        self.query = ""
        self.cursor = 0
        self.column = 0
        self.offset = 0
        self.message = ""

    def visible(self):
        return [i for i, row in enumerate(self.rows) if self.query.casefold() in row.repo.name.casefold()]

    def focused(self):
        visible = self.visible()
        self.cursor = max(0, min(self.cursor, len(visible) - 1))
        return visible[self.cursor] if visible else None

    def move(self, delta):
        self.cursor = max(0, min(self.cursor + delta, len(self.visible()) - 1))
        self.message = ""

    def filter(self, query):
        self.query, self.cursor, self.offset = query, 0, 0
        self.message = ""

    def toggle(self):
        index = self.focused()
        if index is None:
            return
        row = self.rows[index]
        row.selected = not row.selected
        if row.selected:
            self.order.append(index)
        else:
            self.order.remove(index)
        self.message = ""

    def select_visible(self):
        for index in self.visible():
            if not self.rows[index].selected:
                self.rows[index].selected = True
                self.order.append(index)
        self.message = ""

    def clear(self):
        for row in self.rows:
            row.selected = False
        self.order.clear()
        self.message = ""

    def selected(self):
        return [self.rows[i] for i in self.order]

    def set_branch(self, index, column, branch):
        if branch not in self.rows[index].available:
            raise FlowError("请选择该项目已有的本地分支。")
        setattr(self.rows[index], "source" if column == 1 else "target", branch)
        self.message = ""

    def validate(self):
        for index in self.order:
            row = self.rows[index]
            for column, value, label in ((1, row.source, "原分支"), (2, row.target, "目标分支")):
                if value not in row.available:
                    self.filter("")
                    self.cursor, self.column = index, column
                    return f"{row.repo.name}：请选择有效的{label}。"
            if row.source == row.target:
                self.filter("")
                self.cursor, self.column = index, 2
                return f"{row.repo.name}：原分支与目标分支不能相同。"
        return ""


def fit(text, width):
    text = "".join(char if char.isprintable() else " " for char in str(text))
    result, used = [], 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result) + " " * max(0, width - used)


def put(screen, y, x, text, width, attr=0):
    import curses
    if width <= 0:
        return
    try:
        screen.addstr(y, x, fit(text, width), attr)
    except curses.error:
        pass


def draw(screen, state, dry_run):
    import curses
    height, width = screen.getmaxyx()
    screen.erase()
    if height < 12 or width < 66:
        put(screen, 0, 0, "请将终端调整到至少 66 列、12 行；Esc 退出。", width - 1)
        screen.refresh()
        return None
    put(screen, 0, 1, f"选择项目与分支   已选 {len(state.order)}/{len(state.rows)}", width - 2, curses.A_BOLD)
    put(screen, 1, 1, "筛选项目：" + (state.query or "（直接输入名称）"), width - 2)
    put(screen, 2, 1, "↑↓/PgUp/PgDn 浏览；Tab/空格勾选；←→ 切列", width - 2)
    put(screen, 3, 1, "分支列 Enter 编辑；项目列 Enter 完成", width - 2)
    put(screen, 4, 1, "Ctrl-A 全选筛选结果；Ctrl-D 清空；Esc 退出", width - 2)
    name_width = max(24, (width - 8) * 2 // 5)
    source_width = max(18, (width - 8 - name_width) // 2)
    target_width = width - 8 - name_width - source_width
    starts = (1, name_width + 3, name_width + source_width + 5)
    widths = (name_width, source_width, target_width)
    for x, size, title in zip(starts, widths, ("项目", "原分支", "目标分支")):
        put(screen, 5, x, title, size, curses.A_BOLD)
    visible = state.visible()
    focused = state.focused()
    page = height - 10
    if state.cursor < state.offset:
        state.offset = state.cursor
    if state.cursor >= state.offset + page:
        state.offset = state.cursor - page + 1
    for line, index in enumerate(visible[state.offset:state.offset + page], 6):
        row = state.rows[index]
        values = (("[x] " if row.selected else "[ ] ") + row.repo.name,
                  row.source or "（未选择）", row.target or "（未选择）")
        for column, (x, size, value) in enumerate(zip(starts, widths, values)):
            attr = curses.A_REVERSE if index == focused and column == state.column else curses.A_BOLD if row.selected else 0
            put(screen, line, x, value, size, attr)
    put(screen, height - 3, 1, "[ 完成选择 ]", 14, curses.A_REVERSE)
    hint = state.message or ("完成后只读预览" if dry_run else "完成后执行") + "；仅执行已勾选项目，修改分支不改变勾选。"
    put(screen, height - 2, 1, hint, width - 2, curses.A_BOLD if state.message else 0)
    if focused is not None:
        row = state.rows[focused]
        detail = str(row.repo) if state.column == 0 else ("原分支：" + row.source if state.column == 1 else "目标分支：" + row.target)
        put(screen, height - 1, 1, detail, width - 2)
    screen.refresh()
    return {"height": height, "starts": starts, "widths": widths, "page": page, "visible": visible}


def pick_in_place(screen, row, column):
    import curses
    if not row.available:
        return None
    selected = row.source if column == 1 else row.target
    options = sorted(row.available, key=lambda branch: (branch != selected, branch))
    cursor, offset, query = 0, 0, ""
    while True:
        h, w = screen.getmaxyx()
        if h < 10 or w < 50:
            return None
        visible = [branch for branch in options if query.casefold() in branch.casefold()]
        cursor = max(0, min(cursor, len(visible) - 1))
        box_h, box_w = min(h - 4, max(8, len(options) + 5)), min(w - 4, max(48, w * 3 // 4))
        top, left = (h - box_h) // 2, (w - box_w) // 2
        page = box_h - 5
        offset = min(offset, cursor)
        if cursor >= offset + page:
            offset = cursor - page + 1
        for line in range(top, top + box_h):
            put(screen, line, left, "", box_w, curses.A_NORMAL)
        put(screen, top, left, "+" + "-" * (box_w - 2) + "+", box_w, curses.A_BOLD)
        title = row.repo.name + (" · 原分支" if column == 1 else " · 目标分支")
        put(screen, top + 1, left + 2, title, box_w - 4, curses.A_BOLD)
        put(screen, top + 2, left + 2, "筛选：" + query, box_w - 4)
        for line, branch in enumerate(visible[offset:offset + page], top + 3):
            marker = " [当前选中]" if branch == selected else ""
            if branch == row.checked_out:
                marker += " [当前检出]"
            put(screen, line, left + 2, branch + marker, box_w - 4,
                curses.A_REVERSE if line - top - 3 + offset == cursor else 0)
        put(screen, top + box_h - 2, left + 2, "Enter 选择；Esc 返回表格", box_w - 4)
        put(screen, top + box_h - 1, left, "+" + "-" * (box_w - 2) + "+", box_w)
        screen.refresh()
        key = screen.get_wch()
        if key in ("\x1b", "\x03"):
            return None
        if key in ("\n", "\r", curses.KEY_ENTER) and visible:
            return visible[cursor]
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            cursor = max(0, min(cursor + (-1 if key == curses.KEY_UP else 1), len(visible) - 1))
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            query, cursor, offset = query[:-1], 0, 0
        elif key == "\x15":
            query, cursor, offset = "", 0, 0
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, event = curses.getmouse()
                index = offset + y - top - 3
                if event & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED) and left <= x < left + box_w and top + 3 <= y < top + 3 + page and 0 <= index < len(visible):
                    return visible[index]
            except curses.error:
                pass
        elif isinstance(key, str) and key.isprintable():
            query += key
            cursor = offset = 0


def run_table(screen, rows, validate_selected, dry_run):
    import curses
    screen.keypad(True)
    try:
        curses.curs_set(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass
    state = TableState(rows)
    while True:
        layout = draw(screen, state, dry_run)
        key = screen.get_wch()
        if key in ("\x1b", "\x03"):
            raise KeyboardInterrupt
        if layout is None:
            continue
        finished = False
        if key in (curses.KEY_UP, curses.KEY_DOWN):
            state.move(-1 if key == curses.KEY_UP else 1)
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            state.move(-layout["page"] if key == curses.KEY_PPAGE else layout["page"])
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            state.column = (state.column + (-1 if key == curses.KEY_LEFT else 1)) % 3
        elif key in (" ", "\t"):
            state.toggle()
            if key == "\t":
                state.move(1)
        elif key == "\x01":
            state.select_visible()
        elif key == "\x04":
            state.clear()
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            state.filter(state.query[:-1])
        elif key == "\x15":
            state.filter("")
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if state.column == 0:
                finished = True
            elif state.focused() is not None:
                branch = pick_in_place(screen, rows[state.focused()], state.column)
                if branch is not None:
                    state.set_branch(state.focused(), state.column, branch)
        elif key == curses.KEY_MOUSE:
            try:
                _, x, y, _, event = curses.getmouse()
                if event & curses.BUTTON4_PRESSED:
                    state.move(-3)
                elif event & getattr(curses, "BUTTON5_PRESSED", 0):
                    state.move(3)
                elif event & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    if y == layout["height"] - 3 and 1 <= x <= 15:
                        finished = True
                    else:
                        index = state.offset + y - 6
                        if 6 <= y < 6 + layout["page"] and 0 <= index < len(layout["visible"]):
                            state.cursor = index
                            state.column = next((i for i in (2, 1, 0) if x >= layout["starts"][i]), 0)
                            if state.column == 0:
                                state.toggle()
                            else:
                                branch = pick_in_place(screen, rows[state.focused()], state.column)
                                if branch is not None:
                                    state.set_branch(state.focused(), state.column, branch)
            except curses.error:
                pass
        elif isinstance(key, str) and key.isprintable():
            state.filter(state.query + key)
        if finished:
            chosen = state.selected()
            if not chosen:
                state.message = "请先用 Tab 或空格勾选项目。"
                continue
            # The caller checks every selected worktree before branch validation.
            validate_selected(chosen)
            issue = state.validate()
            if issue:
                state.message = issue
                continue
            return chosen


def select_project_branches(rows, *, validate_selected=lambda rows: None, dry_run=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise FlowError("项目分支表格需要交互终端；批量执行请使用 --project/--source/--target 和 --yes。")
    try:
        import curses
    except ImportError as exc:
        raise FlowError("当前 Python 缺少 curses；请使用支持 curses 的终端或用 --yes 参数执行。") from exc
    previous_term = os.environ.get("TERM")
    # Modern SSH/PTYS may advertise dumb despite supporting the ANSI interface
    # already used by the selector. Keep the fallback local to this invocation.
    fallback_term = previous_term in (None, "", "dumb")
    if fallback_term:
        os.environ["TERM"] = "xterm-256color"
    try:
        curses.setupterm(fd=sys.stdout.fileno())
        if not curses.tigetstr("cup"):
            raise FlowError("当前 TERM 不支持交互表格；请使用 xterm-256color 等终端，或通过 --project/--source/--target --yes 执行。")
        return curses.wrapper(run_table, rows, validate_selected, dry_run)
    except curses.error as exc:
        raise FlowError("终端表格初始化失败；请检查 TERM，或通过 --project/--source/--target --yes 执行。") from exc
    finally:
        if fallback_term:
            if previous_term is None:
                os.environ.pop("TERM", None)
            else:
                os.environ["TERM"] = previous_term
