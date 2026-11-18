from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
from .common import FlowError, run


def git(repo, *args, check=True):
    # Suppress optional index refresh writes during status/diff previews.
    # Git still takes all essential locks for explicitly requested mutations.
    return run(["git", "-C", str(repo), *args], check=check,
               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def git_root(path):
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def discover(directory):
    directory = Path(directory).expanduser().resolve()
    if not directory.is_dir():
        raise FlowError(f"扫描目录不存在：{directory}")
    repos = {}
    for child in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
        if child.is_dir() and not child.name.startswith("."):
            root = git_root(child)
            # An ordinary directory in an ancestor repo is not an independent repo.
            if root == child.resolve():
                repos.setdefault(root, child)
    return list(repos.values())


def current_branch(repo):
    return git(repo, "branch", "--show-current").stdout.strip()


def branches(repo):
    return git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout.splitlines()


def status(repo):
    return git(repo, "status", "--porcelain=v1", "-z").stdout



def ensure_clean_worktrees(repos):
    dirty = []
    for repo in dict.fromkeys(Path(path).resolve() for path in repos):
        changes = git(repo, "-c", "core.quotePath=false", "status", "--short", "--untracked-files=all").stdout.rstrip("\n")
        if changes:
            dirty.append((repo, current_branch(repo) or "detached HEAD", changes))
    if dirty:
        lines = [f"发现 {len(dirty)} 个项目存在未提交改动，已退出："]
        for repo, branch, changes in dirty:
            lines.extend([f"  - {repo.name}（当前分支：{branch}）", f"    仓库：{repo}"])
            lines.extend("    " + line for line in changes.splitlines())
        lines.append("请先处理这些改动，使工作区干净后重新运行。")
        raise FlowError("\n".join(lines))


def in_progress(repo):
    if git(repo, "ls-files", "--unmerged").stdout:
        return True
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = Path(git(repo, "rev-parse", "--git-path", marker).stdout.strip())
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return True
    return False


def head(repo, ref="HEAD"):
    return git(repo, "rev-parse", "--verify", ref).stdout.strip()


def require_branch(repo, branch):
    if not branch or branch.startswith("-") or git(repo, "check-ref-format", "--branch", branch, check=False).returncode:
        raise FlowError(f"无效分支：{branch!r}")
    if git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode:
        raise FlowError(f"{repo.name} 缺少本地分支 {branch}；请先明确创建或跟踪该分支。")


@dataclass
class Selection:
    repo: Path
    source: str
    target: str = "dev"
    base: str = "main"
    remote: str = "origin"
    include_base: bool = True
    result: dict = field(default_factory=dict)

    def preflight(self, action):
        if action not in ("deliver", "deploy"):
            raise FlowError("仅支持推送或推送并部署已提交的代码。")
        self.repo = self.repo.resolve()
        if git_root(self.repo) != self.repo:
            raise FlowError(f"不是独立 Git 根目录：{self.repo}")
        if in_progress(self.repo):
            raise FlowError(f"{self.repo.name} 有未完成的 merge/rebase/cherry-pick；请先处理。")
        ensure_clean_worktrees([self.repo])
        require_branch(self.repo, self.source)
        original = current_branch(self.repo)
        if not original:
            raise FlowError(f"{self.repo.name} 当前是 detached HEAD；请先切换到明确分支。")
        require_branch(self.repo, self.target)
        if self.source == self.target:
            raise FlowError(f"{self.repo.name} 开发分支与目标分支不能相同。")
        if self.include_base:
            require_branch(self.repo, self.base)
        if self.remote.startswith("-") or self.remote not in git(self.repo, "remote").stdout.splitlines():
            raise FlowError(f"{self.repo.name} 缺少远端 {self.remote}。")
        self.result = {"repo": str(self.repo), "source": self.source, "target": self.target,
                       "original_branch": original, "status": "pending", "validation": "skipped"}

    def execute(self, action):
        if action not in ("deliver", "deploy"):
            raise FlowError("仅支持推送或推送并部署已提交的代码。")
        repo = self.repo
        restoration = self.source
        self.result["status"] = "running"
        should_restore = False
        try:
            ensure_clean_worktrees([repo])
            if in_progress(repo):
                raise FlowError("仓库状态已改变：存在未完成 Git 操作。")
            if current_branch(repo) != self.source:
                git(repo, "switch", self.source)
            should_restore = True
            self.result["created_commit"] = False
            self.result["feature_commit"] = head(repo)
            refs = list(dict.fromkeys(([self.base] if self.include_base else []) + [self.target]))
            git(repo, "fetch", self.remote, *refs)
            for branch in refs:
                git(repo, "switch", branch)
                git(repo, "merge", "--ff-only", f"refs/remotes/{self.remote}/{branch}")
            git(repo, "switch", self.target)
            self.result["feature_commit"] = head(repo, self.source)
            merges = []
            for branch in ([self.base] if self.include_base else []) + [self.source]:
                ancestor = git(repo, "merge-base", "--is-ancestor", branch, self.target, check=False).returncode
                if ancestor == 0:
                    continue
                if ancestor != 1:
                    raise FlowError("无法判断分支祖先关系。")
                git(repo, "merge", "--no-ff", branch, "-m", f"Merge branch '{branch}' into {self.target}")
                merges.append({"branch": branch, "commit": head(repo)})
                self.result["merges"] = merges
            self.result["local_target_head"] = head(repo)
            git(repo, "-c", "push.followTags=false", "push", self.remote,
                f"refs/heads/{self.target}:refs/heads/{self.target}")
            self.result["pushed_head"] = self.result["local_target_head"]
            self.result["status"] = "delivered"
            return self.result
        except (FlowError, KeyboardInterrupt) as exc:
            self.result["status"] = "failed"
            self.result["error"] = str(exc) or "用户中断；请检查 Git 状态。"
            raise
        finally:
            if should_restore and not in_progress(repo):
                if current_branch(repo) != restoration:
                    restored = git(repo, "switch", restoration, check=False)
                    self.result["restored"] = restored.returncode == 0
                else:
                    self.result["restored"] = True
            elif should_restore:
                self.result["restored"] = False
                self.result["conflicts"] = git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            else:
                self.result["restored"] = current_branch(repo) == restoration
            self.result["final_branch"] = current_branch(repo)
            if self.result["status"] == "delivered" and not self.result["restored"]:
                self.result["status"] = "restoration_failed"


class RepoLocks:
    def __init__(self, repos, state_dir):
        self.repos = sorted(set(Path(x).resolve() for x in repos))
        self.directory = state_dir / "locks"
        self.held = []

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            for repo in self.repos:
                path = self.directory / (hashlib.sha256(str(repo).encode()).hexdigest() + ".lock")
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError as exc:
                    raise FlowError(f"项目已被本工具锁定：{repo}；锁文件 {path}。异常退出后请核查进程再手动移除。") from exc
                self.held.append(path)
                with os.fdopen(fd, "w") as out:
                    out.write(f"pid={os.getpid()}\nrepo={repo}\n")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        for path in reversed(self.held):
            path.unlink(missing_ok=True)

