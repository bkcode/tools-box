"""Optional real Gradle integration: publishes only to a temporary file repository."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("TOOLS_BOX_TEST_GRADLE"), "设置 TOOLS_BOX_TEST_GRADLE 可运行隔离 Gradle 发布测试")
class GradlePublishIntegration(unittest.TestCase):
    def test_real_gradle_publishes_api_and_skips_module_without_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            (repo / "settings.gradle").write_text("rootProject.name='fixture-local'\ninclude 'sample-api', 'empty-api', 'backend'\n")
            (repo / "build.gradle").write_text("allprojects { group='example.fixture'; version='1.0' }\n")
            (repo / "gradle.properties").write_text("org.gradle.daemon=false\norg.gradle.jvmargs=-Xmx256m\n")
            for name in ("sample-api", "empty-api", "backend"):
                (repo / name).mkdir()
            (repo / "sample-api/build.gradle").write_text("""
plugins { id 'java-library'; id 'maven-publish' }
publishing {
  publications { mavenJava(MavenPublication) { from components.java } }
  repositories { maven { url=uri(rootProject.file('local-maven')) } }
}
""")
            (repo / "empty-api/build.gradle").write_text("plugins { id 'java-library' }\n")
            (repo / "backend/build.gradle").write_text("")
            wrapper = repo / "gradlew"
            wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(os.environ["TOOLS_BOX_TEST_GRADLE"]) + ' "$@"\n')
            wrapper.chmod(0o755)
            (repo / ".gitignore").write_text(".gradle/\n**/build/\nlocal-maven/\n")
            env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GRADLE_USER_HOME": str(root / "gradle-home"), "XDG_STATE_HOME": str(root / "state"),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
            def command(*args):
                result = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout[-3000:] + result.stderr[-3000:])
                return result.stdout
            command("git", "init", "-b", "main")
            command("git", "config", "user.name", "Fixture")
            command("git", "config", "user.email", "fixture@example.invalid")
            command("git", "add", ".")
            command("git", "commit", "-m", "fixture")
            before = command("git", "rev-parse", "HEAD")
            output = command(sys.executable, "-m", "tools_box", "git-flow", "publish", "--current-repo", "--yes", "--config", str(root / "empty-config.toml"))
            self.assertTrue((repo / "local-maven/example/fixture/sample-api/1.0/sample-api-1.0.jar").is_file())
            self.assertIn(":sample-api:publish: published", output)
            self.assertIn(":empty-api:publish: skipped", output)
            self.assertFalse((root / "state/tools-box/git-flow/sessions").exists())
            self.assertNotIn("状态文件：", output)
            self.assertNotIn("会话：", output)
            self.assertEqual(before, command("git", "rev-parse", "HEAD"))
            self.assertEqual("", command("git", "--no-optional-locks", "status", "--porcelain"))
