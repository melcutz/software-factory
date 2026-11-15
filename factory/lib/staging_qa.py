"""Generic staging deploy train, secret isolation, git revert, and hook delegation."""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from types import ModuleType

from factory.lib.git_ops import _run_git, head_sha, repo_lock

_STAGING_THREAD_LOCK = threading.RLock()
_HOOKS_CACHE: dict[str, ModuleType | None] = {}
_HOOKS_LOCK = threading.Lock()


def _load_repo_hooks(repo_root: str | None) -> ModuleType | None:
    """Load optional <repo_root>/factory/staging_hooks.py plugin if present."""
    if not repo_root:
        return None
    norm_root = os.path.abspath(repo_root)
    with _HOOKS_LOCK:
        if norm_root in _HOOKS_CACHE:
            return _HOOKS_CACHE[norm_root]
        hooks_path = os.path.join(norm_root, "factory", "staging_hooks.py")
        if not os.path.isfile(hooks_path):
            _HOOKS_CACHE[norm_root] = None
            return None
        spec = importlib.util.spec_from_file_location(
            f"factory_staging_hooks_{abs(hash(norm_root))}",
            hooks_path,
        )
        if spec is None or spec.loader is None:
            _HOOKS_CACHE[norm_root] = None
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _HOOKS_CACHE[norm_root] = mod
        return mod


@contextlib.contextmanager
def staging_lock(jobs_dir: str) -> Iterator[None]:
    """Serialize merging + staging_qa across threads and OS processes."""
    os.makedirs(jobs_dir, exist_ok=True)
    lock_path = os.path.join(jobs_dir, ".staging.lock")
    with _STAGING_THREAD_LOCK:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _parse_env_value(raw_val: str) -> str:
    val = raw_val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {'"', "'"}:
        quote = val[0]
        inner = val[1:-1]
        if quote == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return re.split(r"\s+#", val, maxsplit=1)[0].strip()


def load_staging_secrets(
    env_files: list[str],
    *,
    allowed_keys: frozenset[str] | None = None,
    repo_root: str | None = None,
) -> dict[str, str]:
    """Parse staging credentials from env files without a shell."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "load_staging_secrets"):
        return hooks.load_staging_secrets(env_files)

    secrets: dict[str, str] = {}
    for path in env_files or ():
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            continue
        with open(expanded, encoding="utf-8", errors="replace") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, raw_val = line.split("=", 1)
                key = key.strip()
                if allowed_keys is not None and key not in allowed_keys:
                    continue
                val = _parse_env_value(raw_val)
                if key in {"STRIPE_SECRET_KEY", "KEEPFOR_STRIPE_TEST_KEY"} and (
                    val.startswith("sk_live_") or val.startswith("rk_live_")
                ):
                    raise ValueError(f"Refusing live Stripe key in {key}")
                if val:
                    secrets[key] = val
    return secrets


def build_deploy_env(
    secrets: dict[str, str],
    staging_cfg: dict,
    repo_root: str | None = None,
) -> dict[str, str]:
    """Build subprocess env for staging deploy, delegating to repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "build_deploy_env"):
        return hooks.build_deploy_env(secrets, staging_cfg, repo_root=repo_root)

    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    if repo_root:
        venv_py = os.path.join(repo_root, ".venv", "bin", "python")
        if os.path.isfile(venv_py):
            env.setdefault("PYTHON_BIN", venv_py)
    env["CI"] = "1"
    for k, v in (secrets or {}).items():
        if v:
            env[k] = v
    if staging_cfg.get("base_url"):
        env.setdefault("STAGING_BASE_URL", staging_cfg["base_url"])
    return env


def build_qa_agent_env(
    secrets: dict[str, str],
    staging_cfg: dict,
    repo_root: str | None = None,
) -> dict[str, str]:
    """Build least-privilege env for the QA agent, delegating to repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "build_qa_agent_env"):
        return hooks.build_qa_agent_env(secrets, staging_cfg)

    qa_env: dict[str, str] = {}
    if staging_cfg.get("base_url"):
        qa_env["STAGING_BASE_URL"] = staging_cfg["base_url"]
    if staging_cfg.get("ops_base_url"):
        qa_env["STAGING_OPS_BASE_URL"] = staging_cfg["ops_base_url"]
    return qa_env


def redact_secrets(text: str, secrets: dict[str, str]) -> str:
    """Replace any loaded secret values (>= 6 chars) with [REDACTED]."""
    out = text or ""
    sensitive_values = sorted(
        {
            v
            for k, v in (secrets or {}).items()
            if v and len(v) >= 6 and k != "QA_SIGNUP_ALLOWLIST"
        },
        key=len,
        reverse=True,
    )
    for secret in sensitive_values:
        out = out.replace(secret, "[REDACTED]")
    return out


def ensure_staging_deploy_worktree(repo_root: str, base_branch: str) -> tuple[str, str]:
    """Create or reset the dedicated detached deploy worktree at origin/<base_branch>."""
    with repo_lock(repo_root):
        _run_git(repo_root, "fetch", "origin", base_branch)
        _run_git(repo_root, "worktree", "prune")
        wt_path = os.path.join(repo_root, ".worktrees", "staging-deploy")
        if os.path.exists(os.path.join(wt_path, ".git")):
            _run_git(wt_path, "checkout", "--detach", f"origin/{base_branch}")
            _run_git(wt_path, "reset", "--hard", f"origin/{base_branch}")
            _run_git(wt_path, "clean", "-fd")
        else:
            os.makedirs(os.path.dirname(wt_path), exist_ok=True)
            _run_git(
                repo_root,
                "worktree",
                "add",
                "--detach",
                wt_path,
                f"origin/{base_branch}",
            )
        return wt_path, head_sha(wt_path)


@dataclass
class DeployPlan:
    """Selective staging deployment plan derived from git diff."""

    skip_non_runtime: bool
    extra_args: list[str]
    reason: str


@dataclass
class TrainOutcome:
    """Outcome delivered to a worker thread waiting on a coalesced staging train."""

    action: str  # "return" | "continue"
    job: dict | None = None
    error: BaseException | None = None


@dataclass
class TrainTicket:
    """Per-job ticket submitted to StagingTrainCoordinator."""

    job_id: str
    issue_number: int
    jdir: str
    worktree: str
    branch: str
    issue_md: str
    initial_stage: str
    solo: bool = False
    pre_train_head_sha: str = ""
    merged_sha: str = ""
    pre_merge_sha: str = ""
    done_event: threading.Event = field(default_factory=threading.Event)
    outcome: TrainOutcome | None = None


class StagingTrainCoordinator:
    """Thread-safe Leader/Followers coordinator for coalesced merge + staging QA trains."""

    def __init__(
        self,
        max_batch_size: int = 4,
        coalesce_window_sec: float = 15.0,
        has_near_merge_peers: Callable[[set[str]], bool] | None = None,
    ) -> None:
        if isinstance(max_batch_size, bool) or int(max_batch_size) < 1:
            raise ValueError("max_batch_size must be an integer >= 1")
        if isinstance(coalesce_window_sec, bool) or float(coalesce_window_sec) < 0:
            raise ValueError("coalesce_window_sec must be >= 0")
        self.max_batch_size = int(max_batch_size)
        self.coalesce_window_sec = float(coalesce_window_sec)
        self._has_near_merge_peers = has_near_merge_peers
        self._cond = threading.Condition()
        self._pending: list[TrainTicket] = []
        self._leader_active = False

    def _safe_has_near_merge_peers(self, queued_ids: set[str]) -> bool:
        if self._has_near_merge_peers is None:
            return False
        try:
            return bool(self._has_near_merge_peers(queued_ids))
        except Exception:
            return False

    def run_ticket(
        self,
        ticket: TrainTicket,
        execute_batch_fn: Callable[[list[TrainTicket]], None],
    ) -> TrainOutcome:
        """Queue ticket, coalesce up to max_batch_size, and wait for TrainOutcome."""
        with self._cond:
            self._pending.append(ticket)
            self._cond.notify_all()

        while not ticket.done_event.is_set():
            batch: list[TrainTicket] = []
            became_leader = False
            try:
                with self._cond:
                    while not ticket.done_event.is_set() and (
                        self._leader_active
                        or (self._pending and self._pending[0] is not ticket)
                    ):
                        self._cond.wait()

                    if ticket.done_event.is_set():
                        break

                    self._leader_active = True
                    became_leader = True

                if (
                    not ticket.solo
                    and self.max_batch_size > 1
                    and self.coalesce_window_sec > 0
                ):
                    deadline = time.monotonic() + self.coalesce_window_sec
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        with self._cond:
                            if (
                                len(self._pending) >= self.max_batch_size
                                or any(p.solo for p in self._pending[: self.max_batch_size])
                            ):
                                break
                            queued_ids = {t.job_id for t in self._pending}

                        if self._has_near_merge_peers is not None:
                            if not self._safe_has_near_merge_peers(queued_ids):
                                break
                            wait_sec = min(0.2, max(0.0, deadline - time.monotonic()))
                        else:
                            wait_sec = max(0.0, deadline - time.monotonic())

                        if wait_sec <= 0:
                            break
                        with self._cond:
                            if len(self._pending) < self.max_batch_size and not any(
                                p.solo for p in self._pending[: self.max_batch_size]
                            ):
                                self._cond.wait(timeout=wait_sec)

                with self._cond:
                    if self._pending and self._pending[0].solo:
                        batch = [self._pending.pop(0)]
                    else:
                        take: list[TrainTicket] = []
                        for pending_t in self._pending:
                            if pending_t.solo or len(take) >= self.max_batch_size:
                                break
                            take.append(pending_t)
                        batch = take
                        del self._pending[: len(batch)]

                execute_batch_fn(batch)
            except BaseException as exc:
                for item in batch or [ticket]:
                    if not item.done_event.is_set():
                        item.outcome = TrainOutcome(action="return", error=exc)
                        item.done_event.set()
            finally:
                if became_leader:
                    for item in batch:
                        if not item.done_event.is_set():
                            item.outcome = TrainOutcome(
                                action="return",
                                error=RuntimeError(
                                    f"Train batch completed without outcome for {item.job_id}"
                                ),
                            )
                            item.done_event.set()
                    with self._cond:
                        if ticket in self._pending and ticket.done_event.is_set():
                            self._pending.remove(ticket)
                        self._leader_active = False
                        self._cond.notify_all()

        assert ticket.outcome is not None
        if ticket.outcome.error is not None:
            raise ticket.outcome.error
        return ticket.outcome


def plan_staging_deploy(
    changed_files: list[str],
    *,
    repo_root: str | None = None,
) -> DeployPlan:
    """Compute a DeployPlan from changed files, delegating to repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "plan_staging_deploy"):
        res = hooks.plan_staging_deploy(changed_files)
        return DeployPlan(
            skip_non_runtime=bool(res.skip_non_runtime),
            extra_args=list(res.extra_args),
            reason=str(res.reason),
        )
    if not changed_files:
        return DeployPlan(skip_non_runtime=True, extra_args=[], reason="No files changed.")
    non_runtime_only = all(
        p.strip().startswith(("docs/", "factory/jobs/", ".github/", "tests/"))
        or p.strip().endswith((".md", ".txt", ".rst"))
        for p in changed_files
        if p.strip()
    )
    if non_runtime_only:
        return DeployPlan(
            skip_non_runtime=True,
            extra_args=[],
            reason="Non-runtime files only.",
        )
    return DeployPlan(skip_non_runtime=False, extra_args=[], reason="Full deploy.")


def revert_commit_on_base(deploy_wt: str, base_branch: str, merged_sha: str) -> str:
    """Revert merged_sha on origin/<base_branch> (handling merge and single-parent commits)."""
    _run_git(deploy_wt, "fetch", "origin", base_branch)
    _run_git(deploy_wt, "checkout", "--detach", f"origin/{base_branch}")
    _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}")
    _run_git(deploy_wt, "clean", "-fd")

    rev_line = _run_git(deploy_wt, "rev-list", "--parents", "-n", "1", merged_sha).stdout.strip()
    tokens = rev_line.split()
    parent_count = max(0, len(tokens) - 1)
    if parent_count >= 2:
        _run_git(deploy_wt, "revert", "--no-edit", "-m", "1", merged_sha)
    else:
        _run_git(deploy_wt, "revert", "--no-edit", merged_sha)

    revert_sha = head_sha(deploy_wt)
    _run_git(deploy_wt, "push", "origin", f"HEAD:{base_branch}")
    return revert_sha


def try_revert_commits_on_base(
    deploy_wt: str,
    base_branch: str,
    merged_shas: Sequence[str],
) -> tuple[bool, dict[str, str], str]:
    """Attempt local git revert of merged_shas in order and push once if conflict-free."""
    shas = [s.strip() for s in merged_shas if s and s.strip()]
    if not shas:
        return True, {}, ""

    _run_git(deploy_wt, "fetch", "origin", base_branch)
    _run_git(deploy_wt, "checkout", "--detach", f"origin/{base_branch}")
    _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}")
    _run_git(deploy_wt, "clean", "-fd")

    revert_map: dict[str, str] = {}
    for sha in shas:
        rev_line = _run_git(deploy_wt, "rev-list", "--parents", "-n", "1", sha).stdout.strip()
        parent_count = max(0, len(rev_line.split()) - 1)
        cmd = (
            ["revert", "--no-edit", "-m", "1", sha]
            if parent_count >= 2
            else ["revert", "--no-edit", sha]
        )
        res = _run_git(deploy_wt, *cmd, check=False)
        if res.returncode != 0:
            detail = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
            _run_git(deploy_wt, "revert", "--abort", check=False)
            _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}", check=False)
            _run_git(deploy_wt, "clean", "-fd", check=False)
            return False, {}, detail
        revert_map[sha] = head_sha(deploy_wt)

    push_res = _run_git(deploy_wt, "push", "origin", f"HEAD:{base_branch}", check=False)
    if push_res.returncode != 0:
        detail = ((push_res.stdout or "") + "\n" + (push_res.stderr or "")).strip()
        _run_git(deploy_wt, "reset", "--hard", f"origin/{base_branch}", check=False)
        _run_git(deploy_wt, "clean", "-fd", check=False)
        return False, {}, detail

    return True, revert_map, ""


def _restore_pr_branch_after_revert(
    repo_root: str,
    worktree: str,
    new_branch: str,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    del pre_train_head_sha
    with repo_lock(repo_root):
        _run_git(worktree, "fetch", "origin", base_branch)
        _run_git(worktree, "checkout", "--", ".", check=False)
        _run_git(worktree, "clean", "-fd", check=False)
        _run_git(worktree, "checkout", "-B", new_branch, f"origin/{base_branch}")
        rev = _run_git(worktree, "revert", "--no-edit", revert_sha, check=False)
        if rev.returncode != 0:
            _run_git(worktree, "revert", "--abort", check=False)
            _run_git(worktree, "reset", "--hard", f"origin/{base_branch}", check=False)
            _run_git(worktree, "clean", "-fd", check=False)
            raise subprocess.CalledProcessError(
                rev.returncode,
                rev.args,
                output=rev.stdout,
                stderr=rev.stderr,
            )
    return new_branch


def prepare_qa_fix_branch(
    repo_root: str,
    worktree: str,
    job_id: str,
    next_qa_round: int,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    """Create factory/<job_id>-qa<N> off reverted base with the PR's code restored."""
    new_branch = f"factory/{job_id}-qa{int(next_qa_round)}"
    return _restore_pr_branch_after_revert(
        repo_root,
        worktree,
        new_branch,
        base_branch,
        revert_sha,
        pre_train_head_sha=pre_train_head_sha,
    )


def prepare_requeue_branch(
    repo_root: str,
    worktree: str,
    job_id: str,
    requeue_num: int,
    base_branch: str,
    revert_sha: str,
    pre_train_head_sha: str = "",
) -> str:
    """Create factory/<job_id>-requeue<N> off reverted base for an innocent passing PR."""
    new_branch = f"factory/{job_id}-requeue{int(requeue_num)}"
    return _restore_pr_branch_after_revert(
        repo_root,
        worktree,
        new_branch,
        base_branch,
        revert_sha,
        pre_train_head_sha=pre_train_head_sha,
    )


def snapshot_staging_versions(
    deploy_wt: str,
    deploy_env: dict[str, str],
    *,
    repo_root: str | None = None,
) -> dict[str, str]:
    """Capture pre-deploy staging versions via repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "snapshot_staging_versions"):
        return hooks.snapshot_staging_versions(deploy_wt, deploy_env)
    return {}


def ensure_staging_qa_accounts(
    deploy_wt: str,
    deploy_env: dict[str, str],
    *,
    repo_root: str | None = None,
) -> None:
    """Seed or verify staging QA accounts via repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "ensure_staging_qa_accounts"):
        hooks.ensure_staging_qa_accounts(deploy_wt, deploy_env)


def deploy_to_staging(
    deploy_wt: str,
    deploy_env: dict[str, str],
    timeout_sec: int,
    extra_args: list[str] | None = None,
    *,
    repo_root: str | None = None,
    staging_cfg: dict | None = None,
) -> tuple[bool, str]:
    """Run staging deploy via repo staging_hooks or staging_qa.deploy_cmd."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "deploy_to_staging"):
        return hooks.deploy_to_staging(deploy_wt, deploy_env, timeout_sec, extra_args=extra_args)

    deploy_cmd = (staging_cfg or {}).get("deploy_cmd")
    if not deploy_cmd:
        return True, "No staging deploy hook or deploy_cmd configured."

    cmd = deploy_cmd if isinstance(deploy_cmd, list) else ["bash", "-lc", str(deploy_cmd)]
    proc = subprocess.run(
        cmd,
        cwd=deploy_wt,
        env=deploy_env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode == 0, out


def rollback_staging(
    deploy_wt: str,
    deploy_env: dict[str, str],
    snapshot: dict[str, str],
    *,
    repo_root: str | None = None,
) -> tuple[bool, str]:
    """Roll back staging deployment to snapshot via repo staging_hooks if present."""
    hooks = _load_repo_hooks(repo_root)
    if hooks is not None and hasattr(hooks, "rollback_staging"):
        return hooks.rollback_staging(deploy_wt, deploy_env, snapshot)
    return True, "No staging rollback hook configured."
