import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools_box.git_flow.api_publish import api_tasks
from tools_box.git_flow.cli import main, print_report
from tools_box.git_flow.common import FlowError
from tools_box.git_flow.git import git, head, status


class ApiPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state"),
                                          "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def repository(self, name, modules=()):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
        git(repo, "config", "user.name", "Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "settings.gradle.kts").write_text('rootProject.name = "fixture"\n' + '\n'.join(f'include(":{module}")' for module in modules))
        (repo / "build.gradle.kts").write_text("")
        for module in modules:
            directory = repo.joinpath(*module.split(":"))
            directory.mkdir(parents=True)
            (directory / "build.gradle.kts").write_text('plugins { `maven-publish` }')
        (repo / ".gitignore").write_text("calls.jsonl\npublished.txt\n.gradle/\n**/build/\n")
        wrapper = repo / "gradlew"
        wrapper.write_text(f"#!{sys.executable}\n" + '''import json
from pathlib import Path
import sys
args=sys.argv[1:]
with Path('calls.jsonl').open('a') as log: log.write(json.dumps(args)+'\\n')
if args[0].endswith(':tasks'):
    print('tasks - Displays tasks' if 'empty-api' in args[0] else 'publish - Publishes all publications')
elif 'bad-api' in args[0]:
    print('repository password=fixture-secret')
    sys.exit(17)
else:
    Path('published.txt').write_text(args[0])
    print('BUILD SUCCESSFUL')
''')
        wrapper.chmod(0o755)
        git(repo, "add", ".")
        git(repo, "commit", "-m", "fixture")
        return repo

    def invoke(self, repo, *arguments):
        with patch("pathlib.Path.cwd", return_value=repo), patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO), patch("tools_box.git_flow.cli.Selection.execute") as delivery, patch("tools_box.git_flow.cli.release_group") as deploy, patch("builtins.input", side_effect=AssertionError("publish must not ask Git questions")), patch("tools_box.git_flow.cli.write_json") as persist, patch("tools_box.git_flow.cli.print_report", wraps=print_report) as report:
            code = main(["publish", "--current-repo", "--config", str(self.root / "empty-config.toml"), *arguments])
        delivery.assert_not_called()
        deploy.assert_not_called()
        persist.assert_not_called()
        self.assertFalse((self.root / "state/tools-box/git-flow/sessions").exists())
        self.assertNotIn("状态文件：", output.getvalue())
        self.assertNotIn("会话：", output.getvalue())
        self.last_report = report.call_args.args[0] if report.called else None
        return code, output.getvalue()

    def reported(self):
        self.assertIsNotNone(self.last_report)
        return self.last_report

    def test_registered_nested_and_remapped_api_modules(self):
        repo = self.repository("backend", ("contract-api", "boot", "nested:catalog-api"))
        moved = repo / "contracts/public"
        moved.mkdir(parents=True)
        (moved / "build.gradle").write_text("plugins { id 'maven-publish' }")
        with (repo / "settings.gradle.kts").open("a") as out:
            out.write('\nproject(":contract-api").projectDir = file("contracts/public")\nincludeBuild("../external-api")\n')
        orphan = repo / "orphan-api"
        orphan.mkdir()
        (orphan / "build.gradle.kts").write_text("")
        self.assertEqual([":contract-api:publish", ":nested:catalog-api:publish"], api_tasks(repo))

    def test_commented_api_declarations_and_orphan_modules_are_ignored(self):
        repo = self.repository("checkout-api", ("app",))
        orphan = repo / "orphan-api"
        orphan.mkdir()
        (orphan / "build.gradle.kts").write_text("plugins { `maven-publish` }")
        (repo / "settings.gradle.kts").write_text('/* rootProject.name = "old-api"\ninclude(":orphan-api") */\nrootProject.name = "frontend"\ninclude(":app") // ":orphan-api"\n')
        self.assertEqual([], api_tasks(repo))
        (repo / "settings.gradle.kts").write_text('rootProject.name = "frontend"\n// include(":orphan-api")\n')
        self.assertEqual([], api_tasks(repo))

    def test_api_module_outside_repository_is_rejected(self):
        repo = self.repository("backend", ("contract-api",))
        with (repo / "settings.gradle.kts").open("a") as out:
            out.write('\nproject(":contract-api").projectDir = file("../outside")\n')
        with self.assertRaises(FlowError):
            api_tasks(repo)

    def test_standalone_api_root_uses_exact_root_task(self):
        repo = self.repository("renamed-checkout")
        (repo / "settings.gradle.kts").write_text('rootProject.name = "standalone-api"')
        self.assertEqual([":publish"], api_tasks(repo))

    def test_project_without_api_is_skipped_without_starting_gradle(self):
        repo = self.repository("frontend", ("app",))
        code, output = self.invoke(repo, "--yes")
        self.assertEqual(0, code)
        self.assertFalse((repo / "calls.jsonl").exists())
        self.assertIn("跳过", output)
        self.assertEqual("publication_skipped_no_api_targets", self.reported()["status"])

    def test_api_module_without_publish_task_is_skipped(self):
        repo = self.repository("backend", ("empty-api",))
        code, _ = self.invoke(repo, "--yes")
        self.assertEqual(0, code)
        calls = [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(1, len(calls))
        self.assertEqual(":empty-api:tasks", calls[0][0])
        self.assertFalse((repo / "published.txt").exists())
        self.assertEqual("skipped", self.reported()["publications"][0]["tasks"][0]["status"])

    def test_publish_uses_current_worktree_without_git_or_mcp_mutations(self):
        repo = self.repository("backend", ("contract-api",))
        source = repo / "contract-api/contract.txt"
        source.write_text("uncommitted contract")
        before = (head(repo), git(repo, "show-ref").stdout, status(repo))
        code, _ = self.invoke(repo, "--yes")
        self.assertEqual(0, code)
        calls = [json.loads(line) for line in (repo / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([":contract-api:tasks", ":contract-api:publish"], [call[0] for call in calls])
        self.assertEqual(before, (head(repo), git(repo, "show-ref").stdout, status(repo)))
        self.assertEqual("published", self.reported()["publications"][0]["status"])

    def test_failure_preserves_exit_code_and_stops_following_modules(self):
        repo = self.repository("backend", ("bad-api", "later-api"))
        code, output = self.invoke(repo, "--yes")
        self.assertEqual(6, code)
        self.assertNotIn("fixture-secret", output)
        state = self.reported()
        self.assertNotIn("fixture-secret", json.dumps(state))
        self.assertEqual(17, state["publications"][0]["tasks"][0]["exit_code"])
        self.assertEqual("not_run", state["publications"][0]["tasks"][1]["status"])
        self.assertNotIn(":later-api:publish", (repo / "calls.jsonl").read_text())

    def test_dry_run_never_invokes_gradle_or_saves_a_session(self):
        repo = self.repository("backend", ("contract-api",))
        code, output = self.invoke(repo, "--dry-run")
        self.assertEqual(0, code)
        self.assertIn(":contract-api:publish", output)
        self.assertFalse((repo / "calls.jsonl").exists())
        self.assertFalse((self.root / "state/tools-box/git-flow/sessions").exists())

    def test_publish_menu_bypasses_branch_and_commit_questions(self):
        repo = self.repository("backend", ("contract-api",))
        with patch("pathlib.Path.cwd", return_value=repo), patch("tools_box.git_flow.cli.select_repos", return_value=[repo]), patch("tools_box.git_flow.cli.interactive", return_value=True), patch("tools_box.git_flow.cli.choose", return_value=[2]) as chooser, patch("tools_box.git_flow.cli.select_branches") as branch, patch("tools_box.git_flow.cli.select_project_branches") as table, patch("sys.stdout", new_callable=io.StringIO), patch("builtins.input", side_effect=AssertionError("Unexpected question")):
            self.assertEqual(0, main(["--current-repo", "--dry-run", "--config", str(self.root / "empty-config.toml")]))
        self.assertEqual(3, len(chooser.call_args.args[1]))
        branch.assert_not_called()
        table.assert_not_called()

    def test_multi_project_publish_skips_non_api_and_continues(self):
        first = self.repository("first", ("contract-api",))
        second = self.repository("second", ("app",))
        with patch("sys.stdout", new_callable=io.StringIO), patch("tools_box.git_flow.cli.print_report", wraps=print_report) as report, patch("tools_box.git_flow.cli.write_json") as persist:
            code = main(["publish", "--dir", str(self.root), "--project", first.name, "--project", second.name, "--yes", "--config", str(self.root / "empty-config.toml")])
        self.assertEqual(0, code)
        persist.assert_not_called()
        self.assertFalse((self.root / "state/tools-box/git-flow/sessions").exists())
        statuses = [row["status"] for row in report.call_args.args[0]["publications"]]
        self.assertEqual(["published", "skipped"], statuses)
        self.assertFalse((second / "calls.jsonl").exists())
