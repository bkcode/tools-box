import asyncio
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from tools_box.cli import main as toolbox_main
from tools_box.git_flow.cli import main, overrides, parser, select_repos
from tools_box.git_flow.common import FlowError, bearer_authorization
from tools_box.git_flow.project_table import BranchRow
from tools_box.git_flow.git import branches as repository_branches
from tools_box.git_flow.config import Config, default_config, setup
from tools_box.git_flow.git import Selection, discover, git, git_root, head, ensure_clean_worktrees, status
from tools_box.git_flow.publish import (check_nacos, discover as publisher_discover,
    eligible_jvm, follow_release, prepare_groups, release_group, tool_data)


def command(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                          "XDG_STATE_HOME": str(self.root / "state"), "GIT_TERMINAL_PROMPT": "0"})
        self.env.start()
        self.repo = self.root / "service"
        self.repo.mkdir()
        command("git", "init", "-b", "main", str(self.repo))
        command("git", "-C", str(self.repo), "config", "user.name", "Test User")
        command("git", "-C", str(self.repo), "config", "user.email", "test@example.invalid")
        (self.repo / "file.txt").write_text("base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "branch", "dev")
        git(self.repo, "branch", "test")
        git(self.repo, "switch", "-c", "codex/task")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def remote(self):
        remote = self.root / "origin.git"
        command("git", "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "main", "dev", "test")
        return remote

    def selection(self, **kwargs):
        return Selection(self.repo, "codex/task", **kwargs)

    def edit(self, text="feature\n"):
        (self.repo / "file.txt").write_text(text)

    def commit_edit(self, text="feature\n"):
        self.edit(text)
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-m", "fixture feature")

    def row(self, repo=None, source="codex/task", target="dev"):
        repo = (repo or self.repo).resolve()
        return BranchRow(repo, source, target, tuple(repository_branches(repo)),
                         git(repo, "branch", "--show-current").stdout.strip(), True)

    def test_dirty_worktree_cannot_be_delivered(self):
        self.edit()
        before = head(self.repo)
        with self.assertRaisesRegex(FlowError, "未提交"):
            self.selection().preflight("deliver")
        self.assertEqual(before, head(self.repo))
        self.assertEqual("feature\n", (self.repo / "file.txt").read_text())

    def test_full_delivery_includes_main_pushes_only_target_and_restores(self):
        remote = self.remote()
        git(self.repo, "switch", "main")
        (self.repo / "main.txt").write_text("main advance")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "advance main")
        main_head = head(self.repo)
        git(self.repo, "push", "origin", "main")
        git(self.repo, "switch", "codex/task")
        self.commit_edit()
        s = self.selection()
        s.preflight("deliver")
        s.execute("deliver")
        self.assertEqual("delivered", s.result["status"])
        self.assertEqual("codex/task", s.result["final_branch"])
        self.assertTrue(s.result["restored"])
        self.assertEqual(head(self.repo, "dev"), command("git", "--git-dir", str(remote), "rev-parse", "dev"))
        self.assertEqual(0, git(self.repo, "merge-base", "--is-ancestor", main_head, "dev").returncode)
        self.assertNotIn("codex/task", command("git", "--git-dir", str(remote), "branch"))
        self.assertEqual(2, len(s.result["merges"]))

    def test_custom_target_does_not_include_base(self):
        self.remote()
        s = self.selection(target="test", include_base=False)
        self.commit_edit()
        s.preflight("deliver")
        s.execute("deliver")
        self.assertEqual(head(self.repo, "test"), s.result["pushed_head"])
        self.assertEqual("codex/task", s.result["final_branch"])

    def test_conflict_preserves_merge_state_without_push(self):
        remote = self.remote()
        old_dev = head(self.repo, "dev")
        git(self.repo, "switch", "dev")
        self.edit("dev change\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "dev change")
        git(self.repo, "push", "origin", "dev")
        remote_dev = head(self.repo)
        git(self.repo, "switch", "codex/task")
        self.commit_edit("feature change\n")
        s = self.selection()
        s.preflight("deliver")
        with self.assertRaises(FlowError):
            s.execute("deliver")
        self.assertNotEqual(old_dev, remote_dev)
        self.assertEqual("dev", s.result["final_branch"])
        self.assertFalse(s.result["restored"])
        self.assertIn("file.txt", s.result["conflicts"])
        self.assertEqual(remote_dev, command("git", "--git-dir", str(remote), "rev-parse", "dev"))
        self.assertTrue(git(self.repo, "ls-files", "--unmerged").stdout)

    def test_push_rejection_preserves_local_commits_and_restores(self):
        remote = self.remote()
        old_dev = head(self.repo, "dev")
        hook = remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.commit_edit()
        s = self.selection()
        s.preflight("deliver")
        with self.assertRaises(FlowError):
            s.execute("deliver")
        self.assertEqual("codex/task", s.result["final_branch"])
        self.assertNotEqual(old_dev, head(self.repo, "dev"))
        self.assertNotIn("pushed_head", s.result)
        self.assertEqual(old_dev, command("git", "--git-dir", str(remote), "rev-parse", "dev"))

    def test_switch_with_dirty_worktree_is_blocked(self):
        self.edit()
        s = Selection(self.repo, "test")
        with self.assertRaises(FlowError):
            s.preflight("deliver")
        self.assertEqual("codex/task", git(self.repo, "branch", "--show-current").stdout.strip())

    def test_staged_delivery_blocks_unrelated_work_before_commit(self):
        self.remote()
        initial = head(self.repo)
        self.edit()
        git(self.repo, "add", ".")
        (self.repo / "untracked").write_text("preserve")
        with self.assertRaises(FlowError):
            self.selection().preflight("deliver")
        self.assertEqual(initial, head(self.repo))

    def test_discovery_worktree_symlink_and_ancestor_filter(self):
        (self.root / "plain").mkdir()
        (self.repo / "ordinary-subdir").mkdir()
        worktree = self.root / "linked"
        git(self.repo, "worktree", "add", str(worktree), "test")
        (self.root / "alias").symlink_to(self.repo, target_is_directory=True)
        found = discover(self.root)
        self.assertEqual({self.repo.resolve(), worktree.resolve()}, {p.resolve() for p in found})
        self.assertEqual([], discover(self.repo))
        self.assertEqual(self.repo.resolve(), git_root(self.repo / "ordinary-subdir"))

    def test_clean_delivery_does_not_create_a_source_commit(self):
        self.remote()
        s = self.selection()
        before = head(self.repo)
        s.preflight("deliver")
        s.execute("deliver")
        self.assertEqual(before, head(self.repo))
        self.assertFalse(s.result["created_commit"])
        self.assertEqual("delivered", s.result["status"])

    def test_deploy_accepts_dynamic_target_branch(self):
        self.remote()
        self.selection(target="test", include_base=False).preflight("deploy")

    def test_dev_can_be_source_for_another_selected_target(self):
        self.remote()
        Selection(self.repo, "dev", target="test", include_base=False).preflight("deliver")

    def test_cli_delivery_and_session(self):
        self.remote()
        self.commit_edit()
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("sys.stdout", new_callable=io.StringIO):
            result = main(["deliver", "--yes"])
        self.assertEqual(0, result)
        states = list((self.root / "state/tools-box/git-flow/sessions").glob("*.json"))
        self.assertEqual(1, len(states))
        self.assertEqual("succeeded", json.loads(states[0].read_text())["status"])

    def test_batch_reports_all_dirty_projects_and_exits_before_any_mutation(self):
        second = self.root / "second"
        command("git", "clone", "--no-local", str(self.repo), str(second))
        self.edit("staged content\n")
        git(self.repo, "add", "file.txt")
        self.edit("unstaged content\n")
        (second / "file.txt").unlink()
        (second / "untracked.txt").write_text("preserve")
        before = {str(repo): (head(repo), git(repo, "show-ref").stdout, (repo / ".git/index").read_bytes(), status(repo)) for repo in (self.repo, second)}
        for action in ("deliver", "deploy"):
            with self.subTest(action=action), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as errors, patch("tools_box.git_flow.cli.select_branches") as branches, patch("tools_box.git_flow.cli.Selection.execute") as execute, patch("tools_box.git_flow.cli.release_group") as deploy, patch("tools_box.git_flow.cli.write_json") as persist, patch("tools_box.git_flow.cli.RepoLocks") as locks:
                code = main([action, "--dir", str(self.root), "--project", "service", "--project", "second", "--yes"])
            self.assertEqual(2, code)
            for value in ("2 个项目", "service", "second", str(self.repo.resolve()), str(second.resolve()), "file.txt", "untracked.txt", "??"):
                self.assertIn(value, errors.getvalue())
            branches.assert_not_called()
            execute.assert_not_called()
            deploy.assert_not_called()
            persist.assert_not_called()
            locks.assert_not_called()
        after = {str(repo): (head(repo), git(repo, "show-ref").stdout, (repo / ".git/index").read_bytes(), status(repo)) for repo in (self.repo, second)}
        self.assertEqual(before, after)

    def test_dirty_preview_reports_error_and_preserves_everything(self):
        self.remote()
        self.edit()
        before_head = head(self.repo)
        index = self.repo / ".git/index"
        before_index = (index.read_bytes(), index.stat().st_mtime_ns)
        for action in ("deliver", "deploy"):
            with self.subTest(action=action), patch("pathlib.Path.cwd", return_value=self.repo), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as errors, patch("tools_box.git_flow.cli.Selection.execute") as execute, patch("tools_box.git_flow.cli.prepare_groups") as publish, patch("tools_box.git_flow.cli.write_json") as persist, patch("tools_box.git_flow.cli.RepoLocks") as locks:
                self.assertEqual(2, main([action, "--dry-run"]))
            self.assertIn("未提交", errors.getvalue())
            execute.assert_not_called()
            publish.assert_not_called()
            persist.assert_not_called()
            locks.assert_not_called()
        self.assertEqual(before_head, head(self.repo))
        self.assertEqual(before_index, (index.read_bytes(), index.stat().st_mtime_ns))
        self.assertEqual("feature\n", (self.repo / "file.txt").read_text())

    def test_configured_workspace_root_selects_projects_collection(self):
        backend = self.repo / "projects/backend"
        backend.parent.mkdir()
        command("git", "clone", "--no-local", str(self.repo), str(backend))
        knowledge = self.repo / "knowledge"
        command("git", "clone", "--no-local", str(self.repo), str(knowledge))
        path = self.root / "config.toml"
        project = dict(name="project", directory=str(self.repo), mcp_url="http://example.invalid/mcp", mcp_key="fixture-only")
        path.write_text("[[projects]]\n" + "\n".join(f"{k}={json.dumps(v)}" for k,v in project.items()))
        config = Config(path)
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.choose", return_value=[0]) as choose:
            selected = select_repos(parser().parse_args([]), config)
            self.assertEqual([backend.resolve()], selected)
            choose.assert_called_once()
            self.assertEqual("选择子项目", choose.call_args.args[0])
            self.assertIn("backend", choose.call_args.args[1][0])
            self.assertNotIn("knowledge", choose.call_args.args[1][0])
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("tools_box.git_flow.cli.choose") as choose:
            self.assertEqual([self.repo.resolve()], select_repos(parser().parse_args(["--current-repo"]), config))
            choose.assert_not_called()
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.choose") as choose:
            self.assertEqual([backend.resolve()], select_repos(parser().parse_args(["--project", "backend"]), config))
            choose.assert_not_called()
        with patch("pathlib.Path.cwd", return_value=backend), patch("tools_box.git_flow.cli.choose") as choose:
            self.assertEqual([backend.resolve()], select_repos(parser().parse_args([]), config))
            choose.assert_not_called()
        with patch("pathlib.Path.cwd", return_value=backend.parent), patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.choose", return_value=[0]):
            self.assertEqual([backend.resolve()], select_repos(parser().parse_args([]), config))

    def test_unmatched_directory_lists_all_configured_directories_then_children(self):
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        backend = second / "projects/backend"
        backend.parent.mkdir()
        command("git", "clone", "--no-local", str(self.repo), str(backend))
        missing = self.root / "removed-project"
        projects = [dict(name="first", directory=str(first), mcp_url="http://first.invalid/mcp", mcp_key="first-fixture-key"),
                    dict(name="second", directory=str(second), mcp_url="http://second.invalid/mcp", mcp_key="second-fixture-key"),
                    dict(name="removed", directory=str(missing), mcp_url="http://removed.invalid/mcp", mcp_key="third-fixture-key")]
        path = self.root / "config.toml"
        path.write_text("\n".join("[[projects]]\n" + "\n".join(f"{k}={json.dumps(v)}" for k,v in project.items()) for project in projects))
        config = Config(path)
        with patch("pathlib.Path.cwd", return_value=self.root), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.choose", side_effect=[[1], [0]]) as chooser:
            selected = select_repos(parser().parse_args([]), config)
        self.assertEqual([backend.resolve()], selected)
        first_prompt = chooser.call_args_list[0]
        self.assertIn("选择项目目录", first_prompt.args[0])
        self.assertEqual(3, len(first_prompt.args[1]))
        self.assertFalse(any("[当前目录]" in label for label in first_prompt.args[1]))
        self.assertIn("removed", first_prompt.args[1][2])
        self.assertNotIn("fixture-key", repr(first_prompt))
        self.assertEqual("选择子项目", chooser.call_args_list[1].args[0])
        self.assertEqual("second", config.workspace(selected[0])[1]["publisher"])
        self.assertEqual("http://second.invalid/mcp", config.publisher("second")["url"])
        with patch("pathlib.Path.cwd", return_value=first), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.choose") as chooser:
            self.assertEqual([backend.resolve()], select_repos(parser().parse_args(["--name", "second", "--project", "backend"]), config))
            chooser.assert_not_called()
        with patch("pathlib.Path.cwd", return_value=self.root), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.choose", return_value=[2]), self.assertRaises(FlowError):
            select_repos(parser().parse_args([]), config)





    def test_successful_resume_clears_old_tracking_error(self):
        identifier = "f" * 32
        directory = self.root / "state/tools-box/git-flow/sessions"
        directory.mkdir(parents=True)
        path = directory / (identifier + ".json")
        record = {"publisher": "project", "services": ["service"], "expected_commits": {"service": "expected"},
                  "job_id": "a" * 32, "status": "running"}
        path.write_text(json.dumps({"id": identifier, "status": "tracking_interrupted", "error": "old tracking error", "error_type": "ConnectionError", "git": [], "deployments": [record]}))
        async def completed(config, task, save):
            task.update(status="succeeded", nacos={"status": "healthy"})
        with patch("tools_box.git_flow.cli.release_group", side_effect=completed), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, main(["resume", identifier]))
        saved = json.loads(path.read_text())
        self.assertEqual("succeeded", saved["status"])
        self.assertNotIn("error", saved)
        self.assertNotIn("error_type", saved)

    def test_operation_menu_contains_the_three_requested_flows(self):
        self.remote()
        labels = ["推送到指定目标分支", "推送到指定目标分支，并且部署", "本地发布 API（publish）"]
        for index in (0, 1):
            with self.subTest(index=index), patch("pathlib.Path.cwd", return_value=self.repo), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.select_repos", return_value=[self.repo.resolve()]), patch("tools_box.git_flow.cli.choose", return_value=[index]) as chooser, patch("tools_box.git_flow.cli.select_project_branches", return_value=[self.row()]) as table, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(0, main(["--dry-run"]))
            chooser.assert_called_once_with("选择操作", labels)
            table.assert_called_once()
            self.assertIn("操作：" + labels[index], output.getvalue())

    def test_interactive_delivery_and_deployment_start_without_final_confirmation(self):
        self.remote()
        repo = self.repo.resolve()
        def published(config, selections, state, save):
            state["status"] = "succeeded"
            return 0
        for action in ("deliver", "deploy"):
            self.commit_edit(f"{action} without extra confirmation\n")
            with self.subTest(action=action), patch("pathlib.Path.cwd", return_value=repo), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.select_repos", return_value=[repo]), patch("tools_box.git_flow.cli.select_project_branches", return_value=[self.row()]), patch("builtins.input", side_effect=AssertionError("Unexpected final confirmation")) as prompt, patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.publish_selections", side_effect=published) as publish:
                self.assertEqual(0, main([action]))
            prompt.assert_not_called()
            self.assertEqual(1 if action == "deploy" else 0, publish.call_count)
            self.assertEqual(head(repo, "dev"), head(repo, "refs/remotes/origin/dev"))
            self.assertEqual("codex/task", git(repo, "branch", "--show-current").stdout.strip())

    def test_multi_project_flow_uses_one_table_and_keeps_each_branch_pair(self):
        self.remote()
        second = self.root / "second"
        command("git", "clone", "--no-local", str(self.repo), str(second))
        git(second, "branch", "dev", "origin/dev")
        git(second, "branch", "test", "origin/test")
        git(second, "branch", "feat/second")
        candidates = [self.repo.resolve(), second.resolve()]
        chosen = [self.row(), self.row(second, "feat/second", "test")]
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.select_repos", return_value=candidates) as select, patch("tools_box.git_flow.cli.select_project_branches", return_value=chosen) as table, patch("tools_box.git_flow.cli.choose") as popup, patch("tools_box.git_flow.cli.select_branches") as sequential, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(0, main(["deliver", "--dry-run"]))
        select.assert_called_once()
        self.assertTrue(select.call_args.kwargs["list_only"])
        table.assert_called_once()
        self.assertEqual(candidates, [row.repo for row in table.call_args.args[0]])
        popup.assert_not_called()
        sequential.assert_not_called()
        self.assertIn("service: codex/task → dev", output.getvalue())
        self.assertIn("second: feat/second → test", output.getvalue())

    def test_dirty_table_submission_exits_before_any_delivery(self):
        self.edit()
        def submit(rows, *, validate_selected, **kwargs):
            validate_selected(rows)
            self.fail("Dirty table must not continue")
        with patch("pathlib.Path.cwd", return_value=self.repo), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.select_repos", return_value=[self.repo.resolve()]), patch("tools_box.git_flow.cli.select_project_branches", side_effect=submit), patch("tools_box.git_flow.cli.Selection.execute") as execute, patch("tools_box.git_flow.cli.release_group") as deploy, patch("tools_box.git_flow.cli.write_json") as persist, patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(2, main(["deliver", "--dry-run", "--current-repo"]))
        self.assertIn("未提交", errors.getvalue())
        self.assertIn("file.txt", errors.getvalue())
        execute.assert_not_called()
        deploy.assert_not_called()
        persist.assert_not_called()

    def test_removed_actions_are_not_public_commands(self):
        for action in ("commit", "redeploy"):
            with self.subTest(action=action), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                parser().parse_args([action])
            self.assertEqual(2, error.exception.code)

    def test_late_uncommitted_change_cannot_be_automatically_staged_or_committed(self):
        self.remote()
        selection = self.selection()
        selection.preflight("deliver")
        self.edit("late local work\n")
        before = (head(self.repo), git(self.repo, "show-ref").stdout, (self.repo / ".git/index").read_bytes())
        with self.assertRaisesRegex(FlowError, "未提交"):
            selection.execute("deliver")
        self.assertEqual(before, (head(self.repo), git(self.repo, "show-ref").stdout, (self.repo / ".git/index").read_bytes()))
        self.assertEqual("late local work\n", (self.repo / "file.txt").read_text())
        self.assertEqual("codex/task", selection.result["final_branch"])

    def test_provider_detection_requires_actual_boot_entry(self):
        (self.repo / "gradlew").touch()
        self.assertFalse(eligible_jvm(self.repo))
        src = self.repo / "boot/src/main/kotlin/App.kt"
        src.parent.mkdir(parents=True)
        src.write_text("@SpringBootApplication\nclass App")
        self.assertTrue(eligible_jvm(self.repo))


class ConfigTests(unittest.TestCase):
    def test_nearest_workspace_and_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            config_path.write_text('[publishers.project]\ntransport="http"\n[[workspaces]]\nroot=' + json.dumps(str(root)) + '\npublisher="project"\n')
            config = Config(config_path)
            _, workspace = config.workspace(root / "projects/service")
            self.assertEqual("project", workspace["publisher"])
            self.assertEqual("http", config.publisher("project")["transport"])

    def test_setup_uses_http_key_without_transport_or_mode_prompts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            with patch("builtins.input", side_effect=["project", str(root), "http://127.0.0.1:8123/mcp"]) as prompt, patch("getpass.getpass", return_value="fixture-only") as hidden, patch("sys.stdout", new_callable=io.StringIO) as output:
                setup(path)
            config = Config(path)
            publisher = config.publisher("project")
            self.assertEqual("http", publisher["transport"])
            self.assertEqual("http://127.0.0.1:8123/mcp", publisher["url"])
            self.assertEqual("Bearer fixture-only", publisher["http_headers"]["Authorization"])
            self.assertNotIn("token_env", publisher)
            self.assertEqual({"name", "directory", "mcp_url", "mcp_key"}, set(config.data["projects"][0]))
            self.assertEqual("fixture-only", config.data["projects"][0]["mcp_key"])
            self.assertNotIn("environment", publisher)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn("fixture-only", output.getvalue())
            hidden.assert_called_once()
            self.assertNotIn("expected_mode", publisher)
            self.assertNotIn("expected_workspace", publisher)
            prompts = [call.args[0] for call in prompt.call_args_list]
            self.assertFalse(any("连接方式" in item or "预期发布模式" in item for item in prompts))
            self.assertEqual("project", config.workspace(root)[1]["publisher"])

    def test_local_and_remote_servers_use_same_http_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            for name, url in (("project-remote", "https://remote.example.invalid/mcp"),
                              ("project-local", "http://192.0.2.10:8123/mcp")):
                workspace = root / name
                workspace.mkdir()
                with patch("builtins.input", side_effect=[name, str(workspace), url]), patch("getpass.getpass", return_value="Authorization: Bearer fixture-only"), patch("sys.stdout", new_callable=io.StringIO):
                    setup(path)
            config = Config(path)
            for name in ("project-remote", "project-local"):
                self.assertEqual("http", config.publisher(name)["transport"])
                self.assertEqual("Bearer fixture-only", config.publisher(name)["http_headers"]["Authorization"])
            self.assertEqual(2, len(config.data["projects"]))

    def test_key_input_forms_and_invalid_whitespace(self):
        for value in ("fixture-only", "Bearer fixture-only", "Authorization: Bearer fixture-only"):
            self.assertEqual("Bearer fixture-only", bearer_authorization(value))
        for value in ("", "Bearer ", "Bearer with space", "fixture\r\nInjected: value"):
            with self.assertRaises(FlowError):
                bearer_authorization(value)

    def test_default_config_location(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            self.assertEqual(Path(directory) / "dev-flow/config.toml", default_config())

    def test_setup_keeps_other_publishers_and_restricts_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text('[publishers.previous]\ntransport="http"\nurl="http://previous.invalid/mcp"\n')
            path.chmod(0o644)
            with patch("builtins.input", side_effect=["project", str(root), "http://publisher.invalid/mcp"]), patch("getpass.getpass", return_value="fixture-only"), patch("sys.stdout", new_callable=io.StringIO):
                setup(path)
            config = Config(path)
            self.assertEqual("http://previous.invalid/mcp", config.publisher("previous")["url"])
            self.assertEqual("Bearer fixture-only", config.publisher("project")["http_headers"]["Authorization"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_setup_empty_key_does_not_write_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            with patch("builtins.input", side_effect=["project", str(root), "http://publisher.invalid/mcp"]), patch("getpass.getpass", return_value=""), self.assertRaises(FlowError):
                setup(path)
            self.assertFalse(path.exists())

    def test_project_directory_matching_uses_deepest_real_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            nested = root / "projects/special"
            nested.mkdir(parents=True)
            path = root / "config.toml"
            entries = [dict(name="project", directory=str(root), mcp_url="http://outer.invalid/mcp", mcp_key="outer-key"),
                       dict(name="special", directory=str(nested), mcp_url="http://inner.invalid/mcp", mcp_key="inner-key")]
            path.write_text("\n".join("[[projects]]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in item.items()) for item in entries))
            config = Config(path)
            self.assertEqual("special", config.workspace(nested / "src")[1]["publisher"])
            self.assertEqual("project", config.workspace(root / "projects/another")[1]["publisher"])
            self.assertEqual("http://inner.invalid/mcp", config.publisher("special")["url"])
            with self.assertRaises(FlowError):
                config.workspace(Path(str(root) + "-unrelated"))

    def test_duplicate_project_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            entry = lambda name: f"[[projects]]\nname={json.dumps(name)}\ndirectory={json.dumps(directory)}\n"
            path.write_text(entry("first") + entry("second"))
            with self.assertRaises(FlowError):
                Config(path)

    def test_project_name_cannot_shadow_an_existing_legacy_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[publishers.project]\nurl="http://old.invalid/mcp"\n[[projects]]\nname="project"\ndirectory=' + json.dumps(directory) + '\n')
            with self.assertRaises(FlowError):
                Config(path)

    def test_project_directory_listing_contains_only_names_and_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[[projects]]\nname="project"\ndirectory=' + json.dumps(directory) + '\nmcp_url="http://example.invalid/mcp"\nmcp_key="fixture-key"\n')
            entries = Config(path).project_directories()
            self.assertEqual([{"name": "project", "directory": Path(directory).resolve()}], entries)
            self.assertNotIn("fixture-key", repr(entries))

    def test_per_project_overrides(self):
        a, b = Path("a"), Path("b")
        self.assertEqual({a: "feature", b: "special"}, overrides(["feature", "b=special"], [a, b]))
        with self.assertRaises(FlowError):
            overrides(["wrong=branch"], [a, b])

    def test_registry_help(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(0, toolbox_main(["list"]))
            self.assertIn("git-flow", out.getvalue())


def wrapped(data, error=False):
    return SimpleNamespace(isError=error, structuredContent=data, content=[])


class FakeSession:
    def __init__(self, polls=None):
        self.calls = []
        self.polls = list(polls or [])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "list_jobs":
            return wrapped({"environment": "dev", "workspace": "project", "mode": "local",
                            "jobs": [{"name": "deploy-services", "enabled": True}]})
        if name == "list_services":
            return wrapped({"services": ["service"], "workspace": "project"})
        if name == "run_job":
            return wrapped({"job_id": "a" * 32, "status": "queued"})
        if name == "get_job":
            return wrapped(self.polls.pop(0))
        raise AssertionError(name)


class PublishTests(unittest.IsolatedAsyncioTestCase):
    def record(self):
        return {"services": ["service"], "expected_commits": {"service": "expected"},
                "target_branches": {"service": "test"}, "request_id": "same-id", "publisher": "project"}

    async def test_submission_cursor_and_commit_mismatch(self):
        session = FakeSession([
            {"status": "running", "log": "token=secret\n", "next_log_cursor": 12},
            {"status": "succeeded", "next_log_cursor": 12,
             "result": {"results": [{"service": "service", "commit": "different", "build_tag": "tag"}]}}])
        record = self.record()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            await follow_release(session, record, lambda: None, poll_seconds=0)
        self.assertNotIn("secret", output.getvalue())
        self.assertIn("service", record["commit_mismatches"])
        self.assertEqual(12, session.calls[-1][1]["log_cursor"])
        submits = [a for name, a in session.calls if name == "run_job"]
        self.assertEqual(1, len(submits))
        self.assertEqual("same-id", submits[0]["request_id"])
        self.assertEqual({"services": ["service"]}, submits[0]["parameters"])

    async def test_resume_existing_job_never_resubmits(self):
        session = FakeSession([{"status": "failed", "error": {"code": "BUILD_FAILED", "process_exit_code": 17}}])
        record = self.record()
        record["job_id"] = "b" * 32
        with patch("sys.stdout", new_callable=io.StringIO):
            await follow_release(session, record, lambda: None, poll_seconds=0)
        self.assertFalse(any(n == "run_job" for n, _ in session.calls))
        self.assertEqual(17, record["error"]["process_exit_code"])

    async def test_uncertain_submission_retains_request_id(self):
        record = self.record()
        class FailingSession:
            async def call_tool(self, name, args):
                raise ConnectionError("connection lost")
        with self.assertRaises(ConnectionError):
            await follow_release(FailingSession(), record, lambda: None)
        self.assertEqual("submitting", record["status"])
        self.assertEqual("same-id", record["request_id"])
        session = FakeSession([{"status": "interrupted"}])
        with patch("sys.stdout", new_callable=io.StringIO):
            await follow_release(session, record, lambda: None, poll_seconds=0)
        self.assertEqual("same-id", session.calls[0][1]["request_id"])

    async def test_identity_mismatch_blocks_without_submission(self):
        session = FakeSession()
        with self.assertRaises(FlowError):
            await publisher_discover(session, {"expected_workspace": "other-project"})
        self.assertFalse(any(n == "run_job" for n, _ in session.calls))

    async def test_environment_is_controlled_by_server(self):
        class UnknownEnvironment(FakeSession):
            async def call_tool(self, name, args):
                if name == "list_jobs":
                    return wrapped({"jobs": []})
                return await super().call_tool(name, args)
        jobs, _ = await publisher_discover(UnknownEnvironment(), {})
        self.assertEqual([], jobs["jobs"])
        class ServerEnvironment(FakeSession):
            async def call_tool(self, name, args):
                if name == "list_jobs":
                    return wrapped({"environment": "testing", "jobs": []})
                return await super().call_tool(name, args)
        jobs, _ = await publisher_discover(ServerEnvironment(), {})
        self.assertEqual("testing", jobs["environment"])

    async def test_top_level_terminal_status_wins(self):
        session = FakeSession([{"status": "timed_out", "result": {"status": "running"}}])
        record = self.record()
        with patch("sys.stdout", new_callable=io.StringIO):
            await follow_release(session, record, lambda: None, poll_seconds=0)
        self.assertEqual("timed_out", record["status"])

    async def test_drain_terminal_log_without_resubmission(self):
        session = FakeSession([{"status": "succeeded", "has_more": True, "next_log_cursor": 16},
                               {"status": "succeeded", "has_more": False, "next_log_cursor": 20}])
        with patch("sys.stdout", new_callable=io.StringIO):
            await follow_release(session, self.record(), lambda: None, poll_seconds=0)
        self.assertEqual(1, sum(n == "run_job" for n, _ in session.calls))


    async def test_server_release_task_needs_no_extra_api_approval(self):
        class ApiReleaseSession(FakeSession):
            async def call_tool(self, name, args):
                if name == "list_jobs":
                    return wrapped({"environment": "dev", "workspace": "project", "jobs": [{"name": "deploy-services", "description": "Build, publish API and deploy registered services"}]})
                return await super().call_tool(name, args)
        session = ApiReleaseSession([{"status": "failed"}])
        @asynccontextmanager
        async def connector(entry):
            yield session
        config = SimpleNamespace(publisher=lambda name: {"expected_workspace": "project"})
        with patch("sys.stdout", new_callable=io.StringIO):
            await release_group(config, self.record(), lambda: None, connector=connector)
        submits = [args for name, args in session.calls if name == "run_job"]
        self.assertEqual(1, len(submits))
        self.assertEqual("deploy-services", submits[0]["job"])
        self.assertEqual({"services": ["service"]}, submits[0]["parameters"])

    async def test_disabled_job_still_allows_polling_accepted_task(self):
        class DisabledSession(FakeSession):
            async def call_tool(self, name, args):
                if name == "list_jobs":
                    return wrapped({"environment": "dev", "jobs": [{"name": "deploy-services", "enabled": False}]})
                return await super().call_tool(name, args)
        session = DisabledSession([{"status": "failed"}])
        @asynccontextmanager
        async def connector(entry):
            yield session
        config = SimpleNamespace(publisher=lambda name: {})
        record = self.record()
        record["job_id"] = "b" * 32
        with patch("sys.stdout", new_callable=io.StringIO):
            await release_group(config, record, lambda: None, connector=connector)
        self.assertEqual("failed", record["status"])
        self.assertFalse(any(n == "run_job" for n, _ in session.calls))

    async def test_successful_job_without_result_records_commit_unknown(self):
        session = FakeSession([{"status": "succeeded"}])
        record = self.record()
        with patch("sys.stdout", new_callable=io.StringIO):
            await follow_release(session, record, lambda: None, poll_seconds=0)
        self.assertEqual(None, record["commit_mismatches"]["service"]["actual"])

    async def test_declared_build_branch_mismatch_blocks_submission(self):
        class BranchSession(FakeSession):
            async def call_tool(self, name, args):
                if name == "list_services":
                    return wrapped({"services": ["service"], "details": [{"name": "service", "expected_branch": "release", "source_ready": True}]})
                return await super().call_tool(name, args)
        session = BranchSession()
        @asynccontextmanager
        async def connector(entry):
            yield session
        config = SimpleNamespace(publisher=lambda name: {})
        record = self.record()
        with self.assertRaisesRegex(FlowError, "test.*release"):
            await release_group(config, record, lambda: None, connector=connector)
        self.assertEqual("mismatched", record["branch_checks"]["service"]["status"])
        self.assertFalse(any(n == "run_job" for n, _ in session.calls))

    async def test_dynamic_target_matches_server_branch_and_sends_only_services(self):
        for registry_style in (False, True):
            class BranchSession(FakeSession):
                async def call_tool(self, name, args):
                    if name == "list_services":
                        if registry_style:
                            return wrapped({"services": ["service"], "_local_publisher": {"services": {"service": {"branch": "test"}}}})
                        return wrapped({"services": ["service"], "details": [{"name": "service", "expected_branch": "test", "source_ready": True}]})
                    return await super().call_tool(name, args)
            session = BranchSession([{"status": "failed"}])
            @asynccontextmanager
            async def connector(entry):
                yield session
            config = SimpleNamespace(publisher=lambda name: {})
            record = self.record()
            with patch("sys.stdout", new_callable=io.StringIO):
                await release_group(config, record, lambda: None, connector=connector)
            self.assertEqual("matched", record["branch_checks"]["service"]["status"])
            payload = next(args for name, args in session.calls if name == "run_job")
            self.assertEqual({"services": ["service"]}, payload["parameters"])

    async def test_group_records_selected_branch_instead_of_fixed_dev(self):
        selection = SimpleNamespace(repo=Path("/projects/service"), target="test", result={"pushed_head": "expected"})
        config = SimpleNamespace(workspace=lambda repo: (repo.parent, {"publisher": "project"}), publisher=lambda name: {})
        with patch("tools_box.git_flow.publish.eligible_jvm", return_value=True):
            groups, skipped = prepare_groups(config, [selection])
        self.assertEqual({"service": "test"}, groups[0]["target_branches"])
        self.assertEqual([], skipped)

    async def test_poll_reconnect_preserves_job_and_cursor_without_second_release(self):
        class Interrupted(FakeSession):
            async def call_tool(self, name, args):
                if name == "get_job" and not self.polls:
                    raise ConnectionError("fixture disconnect")
                return await super().call_tool(name, args)
        first = Interrupted([{"status": "running", "next_log_cursor": 17}])
        second = FakeSession([{"status": "failed"}])
        sessions = iter((first, second))
        @asynccontextmanager
        async def connector(entry):
            yield next(sessions)
        record = self.record()
        config = SimpleNamespace(publisher=lambda name: {})
        with patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.publish.asyncio.sleep", new_callable=AsyncMock):
            await release_group(config, record, lambda: None, connector=connector)
        self.assertEqual("failed", record["status"])
        self.assertEqual(1, record["connection_retries"])
        self.assertEqual(1, sum(name == "run_job" for session in (first, second) for name, _ in session.calls))
        self.assertEqual(17, next(args for name, args in second.calls if name == "get_job")["log_cursor"])

    async def test_uncertain_submission_is_not_automatically_resubmitted(self):
        class Uncertain(FakeSession):
            async def call_tool(self, name, args):
                if name == "run_job":
                    self.calls.append((name, args))
                    raise ConnectionError("fixture lost response")
                return await super().call_tool(name, args)
        session = Uncertain()
        connections = []
        @asynccontextmanager
        async def connector(entry):
            connections.append(True)
            yield session
        config = SimpleNamespace(publisher=lambda name: {})
        record = self.record()
        with patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(ConnectionError):
            await release_group(config, record, lambda: None, connector=connector)
        self.assertEqual(1, len(connections))
        self.assertEqual("submitting", record["status"])
        self.assertEqual(1, sum(name == "run_job" for name, _ in session.calls))

    async def test_tool_error_over_http_success(self):
        with self.assertRaises(FlowError):
            tool_data(wrapped({}, error=True))


class NacosTests(unittest.TestCase):
    def entries(self):
        entry = {"nacos": {"url": "https://nacos.invalid/nacos"},
                 "services": {"service": {"nacos_service": "registered", "instances": [
                     {"target": "ssh-target", "ip": "192.0.2.10", "port": 8080}]}}}
        record = {"services": ["service"], "results": [{"service": "service", "targets": ["ssh-target"]}]}
        return entry, record

    def test_healthy_other_instance_does_not_count(self):
        entry, record = self.entries()
        result = check_nacos(entry, record, deadline_seconds=0,
                             fetch=lambda *a, **kw: {"hosts": [{"ip": "192.0.2.99", "port": 8080, "healthy": True}]})
        self.assertEqual("unhealthy", result["status"])

    def test_exact_target_healthy(self):
        entry, record = self.entries()
        result = check_nacos(entry, record,
                             fetch=lambda *a, **kw: {"hosts": [{"ip": "192.0.2.10", "port": 8080, "healthy": True}]})
        self.assertEqual("healthy", result["status"])

    def test_target_mapping_missing_is_not_verified(self):
        entry, record = self.entries()
        record["results"][0]["targets"] = ["other-target"]
        self.assertEqual("not_verified", check_nacos(entry, record)["status"])

    def test_missing_configuration_is_not_verified(self):
        self.assertEqual("not_verified", check_nacos({}, {"services": ["service"]})["status"])

    def test_auth_error_is_not_healthy_and_does_not_leak(self):
        entry, record = self.entries()
        def failure(*args, **kwargs):
            raise RuntimeError("password=secret")
        result = check_nacos(entry, record, fetch=failure)
        self.assertEqual("not_verified", result["status"])
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
