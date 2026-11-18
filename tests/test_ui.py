"""Use real fzf filtering to verify that branch context cannot outrank a project name."""
import shutil
import subprocess
import unittest
from unittest.mock import patch
from tools_box.git_flow.ui import choose


@unittest.skipUnless(shutil.which("fzf"), "fzf 未安装，运行时使用编号选择")
class FzfTests(unittest.TestCase):
    def test_complete_project_name_ranks_before_loose_branch_match(self):
        real_run = subprocess.run
        def filtered(command, **kwargs):
            result = real_run([*command, "--filter=project-fds"], **kwargs)
            # Simulate Enter on the first ranked result without opening a TTY.
            return subprocess.CompletedProcess(command, result.returncode,
                                               result.stdout.splitlines()[0] + "\n")
        options = ["project-common    当前分支：feature/fds",
                   "project-fds    当前分支：feature/task"]
        with patch("tools_box.git_flow.ui.interactive", return_value=True), patch("tools_box.git_flow.ui.subprocess.run", side_effect=filtered):
            self.assertEqual([1], choose("选择项目", options, multi=True))

    def test_full_project_list_is_available_without_a_filter(self):
        real_run = subprocess.run
        def unfiltered(command, **kwargs):
            return real_run([*command, "--filter="], **kwargs)
        options = [f"project-{i:03d}    当前分支：feature/task-{i}" for i in range(62)]
        with patch("tools_box.git_flow.ui.interactive", return_value=True), patch("tools_box.git_flow.ui.subprocess.run", side_effect=unfiltered):
            selected = choose("选择项目", options, multi=True)
        self.assertEqual(list(range(62)), selected)
