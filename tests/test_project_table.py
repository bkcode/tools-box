from pathlib import Path
import os
import unittest
from unittest.mock import patch
try:
    import curses
except ImportError:
    curses = None
from tools_box.git_flow.common import FlowError
from tools_box.git_flow.project_table import BranchRow, TableState, fit, run_table, select_project_branches


def rows():
    available = ("feat/a", "feat/b", "dev", "test")
    return [BranchRow(Path("/projects/alpha"), "feat/a", "dev", available),
            BranchRow(Path("/projects/beta"), "feat/b", "dev", available)]


class TableModelTests(unittest.TestCase):
    def test_selection_order_and_per_project_branches_remain_independent(self):
        state = TableState(rows())
        state.move(1)
        state.toggle()
        state.move(-1)
        state.toggle()
        state.set_branch(0, 2, "test")
        state.set_branch(1, 1, "feat/a")
        chosen = state.selected()
        self.assertEqual(["beta", "alpha"], [r.repo.name for r in chosen])
        self.assertEqual([("feat/a", "dev"), ("feat/a", "test")], [(r.source, r.target) for r in chosen])
        self.assertEqual("", state.validate())

    def test_filtering_does_not_drop_hidden_selection_or_edit_other_projects(self):
        state = TableState(rows())
        state.toggle()
        state.filter("beta")
        state.select_visible()
        self.assertEqual(["alpha", "beta"], [r.repo.name for r in state.selected()])
        state.set_branch(1, 2, "test")
        self.assertEqual("dev", state.rows[0].target)
        state.clear()
        self.assertEqual([], state.selected())

    def test_editing_branch_does_not_expand_selection(self):
        state = TableState(rows())
        state.set_branch(1, 2, "test")
        self.assertEqual([], state.selected())
        with self.assertRaises(FlowError):
            state.set_branch(0, 1, "missing")

    def test_invalid_unselected_defaults_do_not_block_selected_projects(self):
        data = rows()
        data[1].source = "missing"
        state = TableState(data)
        state.toggle()
        self.assertEqual("", state.validate())
        state.move(1)
        state.toggle()
        self.assertIn("beta", state.validate())
        self.assertEqual(1, state.column)

    def test_identical_source_and_target_focuses_target_for_correction(self):
        data = rows()
        data[0].target = "feat/a"
        state = TableState(data)
        state.toggle()
        self.assertIn("不能相同", state.validate())
        self.assertEqual(2, state.column)

    def test_wide_text_fits_terminal_columns(self):
        self.assertEqual("项目a", fit("项目a", 5))
        self.assertEqual("项目", fit("项目a", 4))


class FakeScreen:
    def __init__(self, keys):
        self.keys = iter(keys)

    def keypad(self, value):
        pass

    def get_wch(self):
        return next(self.keys)


@unittest.skipUnless(curses, "curses 不可用")
class TableInteractionTests(unittest.TestCase):
    def run_keys(self, keys, data, validator=lambda selected: None):
        layout = {"height": 24, "page": 14, "starts": (1, 29, 54), "visible": list(range(len(data)))}
        with patch("tools_box.git_flow.project_table.draw", return_value=layout), patch("curses.curs_set"), patch("curses.mousemask"):
            return run_table(FakeScreen(keys), data, validator, True)

    def test_multiple_project_and_branch_edits_complete_in_one_table(self):
        data = rows()
        keys = [" ", curses.KEY_RIGHT, "\n", curses.KEY_RIGHT, "\n", curses.KEY_RIGHT,
                curses.KEY_DOWN, " ", "\n"]
        validated = []
        with patch("tools_box.git_flow.project_table.pick_in_place", side_effect=["feat/b", "test"]) as picker:
            chosen = self.run_keys(keys, data, lambda selected: validated.append(selected))
        self.assertEqual(2, picker.call_count)
        self.assertEqual(1, len(validated))
        self.assertEqual([("feat/b", "test"), ("feat/b", "dev")], [(r.source, r.target) for r in chosen])

    def test_defaults_finish_multiple_projects_without_any_branch_popup(self):
        with patch("tools_box.git_flow.project_table.pick_in_place") as picker:
            chosen = self.run_keys(["\t", "\t", "\n"], rows())
        self.assertEqual(2, len(chosen))
        picker.assert_not_called()

    def test_ssh_dumb_term_fallback_is_temporary(self):
        data = rows()
        observed = []
        with patch.dict(os.environ, {"TERM": "dumb"}), patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True), patch("sys.stdout.fileno", return_value=1), patch("curses.setupterm", side_effect=lambda **kwargs: observed.append(os.environ["TERM"])), patch("curses.tigetstr", return_value=b"cursor-addressing"), patch("curses.wrapper", return_value=data):
            self.assertEqual(data, select_project_branches(data, dry_run=True))
            self.assertEqual("dumb", os.environ["TERM"])
        self.assertEqual(["xterm-256color"], observed)

    def test_cancel_exits_without_validating_or_executing(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_keys(["\x1b"], rows(), lambda selected: self.fail("Unexpected validation"))

    def test_dirty_selected_projects_exit_instead_of_continuing_branch_setup(self):
        def invalid(selected):
            self.assertEqual(["alpha"], [r.repo.name for r in selected])
            raise FlowError("未提交改动")
        with self.assertRaisesRegex(FlowError, "未提交"):
            self.run_keys([" ", "\n"], rows(), invalid)
