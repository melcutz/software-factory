"""Git-native distributed state store using orphan commits under refs/factory/*."""
from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any

from factory.lib.git_ops import _run_git, repo_lock

ZERO_SHA = "0" * 40
WORKER_TTL_SEC = 360
SKEW_MARGIN_SEC = 60
QUOTA_BACKOFF_SCHEDULE = [120, 300, 900, 3600]
TERMINAL_STAGES = {"merged", "ready", "rejected", "needs-info"}

_CHECKPOINT_TOP_FILES = (
    "intake.json",
    "intake.md",
    "spec.json",
    "spec.md",
    "plan.json",
    "qa-failure.md",
)


class LeaseLostError(RuntimeError):
    """Raised when a worker loses its distributed job lease or fencing token."""


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_ts(val: Any) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str) and val:
        try:
            return datetime.fromisoformat(val).timestamp()
        except ValueError:
            return 0.0
    return 0.0


class GitStateStore:
    """Atomic Compare-And-Swap distributed state store backed by Git refs/factory/*."""

    def __init__(
        self,
        repo_root: str,
        machine_id: str,
        remote: str = "origin",
        hostname: str | None = None,
    ) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.machine_id = machine_id
        self.remote = remote
        self.hostname = hostname or socket.gethostname()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Git plumbing helpers
    # ------------------------------------------------------------------

    def _git_raw(self, *args: str, input_bytes: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        res = subprocess.run(
            ["git", "-C", self.repo_root, *args],
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if check and res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args[:3])} failed (exit {res.returncode}): {err[:400]}")
        return res

    def _hash_blob(self, data: bytes) -> str:
        res = self._git_raw("hash-object", "-w", "--stdin", input_bytes=data)
        return res.stdout.decode("utf-8").strip()

    def _build_tree(self, files: dict[str, bytes]) -> str:
        """Build a Git tree object from nested relative paths without touching index/worktree."""
        blobs: dict[str, bytes] = {}
        subdirs: dict[str, dict[str, bytes]] = {}
        for rel_path, content in files.items():
            clean = rel_path.strip("/")
            if not clean:
                continue
            if "/" not in clean:
                blobs[clean] = content
            else:
                top, rest = clean.split("/", 1)
                subdirs.setdefault(top, {})[rest] = content

        entries: list[str] = []
        for name, content in sorted(blobs.items()):
            sha = self._hash_blob(content)
            entries.append(f"100644 blob {sha}\t{name}")
        for dirname, subfiles in sorted(subdirs.items()):
            sub_sha = self._build_tree(subfiles)
            entries.append(f"040000 tree {sub_sha}\t{dirname}")

        mktree_input = ("\n".join(entries) + ("\n" if entries else "")).encode("utf-8")
        res = self._git_raw("mktree", input_bytes=mktree_input)
        return res.stdout.decode("utf-8").strip()

    def _commit_tree(self, tree_sha: str, message: str, parent_sha: str | None = None) -> str:
        args = ["commit-tree", tree_sha]
        if parent_sha and parent_sha != ZERO_SHA:
            args.extend(["-p", parent_sha])
        args.extend(["-m", message])
        res = self._git_raw(*args)
        return res.stdout.decode("utf-8").strip()

    def _ls_remote(self, pattern: str = "refs/factory/*") -> dict[str, str]:
        res = _run_git(self.repo_root, "ls-remote", self.remote, pattern, check=False)
        if res.returncode != 0:
            return {}
        out: dict[str, str] = {}
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                out[parts[1]] = parts[0]
        return out

    def _ls_remote_one(self, ref: str) -> str | None:
        return self._ls_remote(ref).get(ref)

    def _fetch_ref(self, ref: str) -> str | None:
        with repo_lock(self.repo_root):
            res = _run_git(self.repo_root, "fetch", "--no-tags", "--quiet", self.remote, ref, check=False)
            if res.returncode != 0:
                return None
            head = _run_git(self.repo_root, "rev-parse", "FETCH_HEAD", check=False)
            if head.returncode != 0:
                return None
            return head.stdout.strip()

    def _cas_push(self, commit_sha: str, ref: str, expected_old_sha: str | None) -> bool:
        old = expected_old_sha or ZERO_SHA
        res = _run_git(
            self.repo_root,
            "push",
            "--no-verify",
            "--quiet",
            self.remote,
            f"{commit_sha}:{ref}",
            f"--force-with-lease={ref}:{old}",
            check=False,
        )
        return res.returncode == 0

    def _delete_remote_ref(self, ref: str) -> None:
        _run_git(self.repo_root, "push", "--no-verify", "--quiet", self.remote, "--delete", ref, check=False)

    def _read_tree_files(self, commit_sha: str) -> dict[str, bytes]:
        res = self._git_raw("ls-tree", "-r", "-z", commit_sha, check=False)
        if res.returncode != 0:
            return {}
        files: dict[str, bytes] = {}
        for entry in res.stdout.split(b"\x00"):
            if not entry:
                continue
            meta, path_bytes = entry.split(b"\t", 1)
            parts = meta.decode("utf-8").split()
            if len(parts) >= 3 and parts[1] == "blob":
                blob_sha = parts[2]
                blob = self._git_raw("cat-file", "blob", blob_sha, check=False)
                if blob.returncode == 0:
                    files[path_bytes.decode("utf-8")] = blob.stdout
        return files

    def _read_json_from_commit(self, commit_sha: str, filename: str) -> dict | None:
        res = self._git_raw("show", f"{commit_sha}:{filename}", check=False)
        if res.returncode != 0:
            return None
        try:
            data = json.loads(res.stdout.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def _push_json_ref(
        self,
        ref: str,
        filename: str,
        payload: dict,
        message: str,
        expected_old_sha: str | None = None,
        *,
        force: bool = False,
    ) -> bool:
        tree_sha = self._build_tree({filename: json.dumps(payload, indent=2).encode("utf-8")})
        commit_sha = self._commit_tree(tree_sha, message, parent_sha=expected_old_sha)
        if force:
            res = _run_git(
                self.repo_root,
                "push",
                "--no-verify",
                "--quiet",
                "--force",
                self.remote,
                f"{commit_sha}:{ref}",
                check=False,
            )
            return res.returncode == 0
        return self._cas_push(commit_sha, ref, expected_old_sha)

    # ------------------------------------------------------------------
    # Worker heartbeats
    # ------------------------------------------------------------------

    def write_worker_heartbeat(
        self,
        active_jobs: int,
        max_workers: int,
        paused: bool,
        *,
        now_ts: float | None = None,
        cpu_load_pct: float | None = None,
        free_ram_gb: float | None = None,
    ) -> str:
        ts = time.time() if now_ts is None else now_ts
        payload = {
            "worker_id": self.machine_id,
            "hostname": self.hostname,
            "active_jobs": active_jobs,
            "max_workers": max_workers,
            "paused": paused,
            "heartbeat_at": _iso(ts),
            "heartbeat_ts": ts,
            "expires_at_ts": ts + WORKER_TTL_SEC,
            "cpu_load_pct": cpu_load_pct,
            "free_ram_gb": free_ram_gb,
        }
        ref = f"refs/factory/workers/{self.machine_id}"
        old_sha = self._ls_remote_one(ref)
        self._push_json_ref(ref, "worker.json", payload, f"heartbeat {self.machine_id}", old_sha, force=True)
        return self.machine_id

    def delete_worker_heartbeat(self) -> None:
        self._delete_remote_ref(f"refs/factory/workers/{self.machine_id}")

    def list_worker_heartbeats(self) -> dict[str, dict]:
        refs = self._ls_remote("refs/factory/workers/*")
        workers: dict[str, dict] = {}
        for ref in sorted(refs):
            commit_sha = self._fetch_ref(ref)
            if not commit_sha:
                continue
            data = self._read_json_from_commit(commit_sha, "worker.json")
            if data:
                wid = data.get("worker_id") or ref.rsplit("/", 1)[-1]
                workers[wid] = data
        return workers

    # ------------------------------------------------------------------
    # Job leases & checkpoints
    # ------------------------------------------------------------------

    def claim_job_lease(
        self,
        job_id: str,
        initial_job: dict | None = None,
        *,
        now_ts: float | None = None,
    ) -> tuple[bool, dict | None]:
        ts = time.time() if now_ts is None else now_ts
        ref = f"refs/factory/jobs/{job_id}"
        with self._lock:
            old_sha = self._ls_remote_one(ref)
            files: dict[str, bytes] = {}
            if old_sha is None:
                if initial_job is None:
                    return False, None
                job_data = dict(initial_job)
                prev_token = int(job_data.get("_last_fencing_token") or 0)
            else:
                fetched = self._fetch_ref(ref)
                if not fetched:
                    return False, None
                old_sha = fetched
                files = self._read_tree_files(old_sha)
                raw_job = files.get("job.json")
                if raw_job:
                    try:
                        job_data = json.loads(raw_job.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        return False, None
                elif initial_job is not None:
                    job_data = dict(initial_job)
                else:
                    return False, None

                lease = job_data.get("lease")
                if isinstance(lease, dict) and lease.get("worker_id") and lease["worker_id"] != self.machine_id:
                    owner_id = lease["worker_id"]
                    workers = self.list_worker_heartbeats()
                    owner = workers.get(owner_id)
                    if owner is not None:
                        expires_at = float(owner.get("expires_at_ts") or 0.0)
                        if ts <= expires_at + SKEW_MARGIN_SEC:
                            return False, None
                    else:
                        lease_exp = float(lease.get("expires_at_ts") or 0.0)
                        if lease_exp > 0 and ts <= lease_exp + SKEW_MARGIN_SEC:
                            return False, None

                prev_token = int(
                    (job_data.get("lease") or {}).get("fencing_token")
                    or job_data.get("_last_fencing_token")
                    or 0
                )

            new_token = prev_token + 1
            job_data["lease"] = {
                "worker_id": self.machine_id,
                "hostname": self.hostname,
                "fencing_token": new_token,
                "claimed_at": _iso(ts),
                "claimed_ts": ts,
                "expires_at_ts": ts + WORKER_TTL_SEC,
            }
            job_data["_last_fencing_token"] = new_token
            files["job.json"] = json.dumps(job_data, indent=2).encode("utf-8")

            tree_sha = self._build_tree(files)
            commit_sha = self._commit_tree(tree_sha, f"claim {job_id} by {self.machine_id}", parent_sha=old_sha)
            if self._cas_push(commit_sha, ref, old_sha):
                return True, job_data
            return False, None

    def _collect_checkpoint_files(self, jdir: str, job_payload: dict) -> dict[str, bytes]:
        files: dict[str, bytes] = {
            "job.json": json.dumps(job_payload, indent=2).encode("utf-8"),
        }
        if not jdir or not os.path.isdir(jdir):
            return files

        for name in _CHECKPOINT_TOP_FILES:
            fpath = os.path.join(jdir, name)
            if os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    files[name] = f.read()

        for pattern in ("round-*/slice-*-build.json", "round-*/review-*.json"):
            for fpath in sorted(glob.glob(os.path.join(jdir, pattern))):
                if os.path.isfile(fpath):
                    rel = os.path.relpath(fpath, jdir).replace(os.sep, "/")
                    with open(fpath, "rb") as f:
                        files[rel] = f.read()
        return files

    def checkpoint_job(
        self,
        job_id: str,
        jdir: str,
        job_data: dict,
        *,
        release_lease: bool = False,
    ) -> bool:
        ref = f"refs/factory/jobs/{job_id}"
        with self._lock:
            old_sha = self._ls_remote_one(ref)
            if old_sha:
                fetched = self._fetch_ref(ref)
                if fetched:
                    old_sha = fetched
                    remote_job = self._read_json_from_commit(old_sha, "job.json")
                    if remote_job:
                        r_lease = remote_job.get("lease")
                        local_lease = job_data.get("lease") or {}
                        local_token = int(local_lease.get("fencing_token") or 0)
                        if isinstance(r_lease, dict):
                            r_owner = r_lease.get("worker_id")
                            r_token = int(r_lease.get("fencing_token") or 0)
                            if r_owner and r_owner != self.machine_id:
                                raise LeaseLostError(f"{job_id} lease owned by {r_owner}")
                            if local_token and r_token > local_token:
                                raise LeaseLostError(f"{job_id} fencing token advanced ({r_token} > {local_token})")

            payload = dict(job_data)
            if release_lease:
                last_tok = int(
                    (payload.get("lease") or {}).get("fencing_token")
                    or payload.get("_last_fencing_token")
                    or 0
                )
                if last_tok:
                    payload["_last_fencing_token"] = last_tok
                payload["lease"] = None

            files = self._collect_checkpoint_files(jdir, payload)
            tree_sha = self._build_tree(files)
            commit_sha = self._commit_tree(tree_sha, f"checkpoint {job_id}", parent_sha=old_sha)
            if self._cas_push(commit_sha, ref, old_sha):
                return True

            # Verify whether push failed because lease was stolen
            check_sha = self._fetch_ref(ref)
            if check_sha:
                remote_job = self._read_json_from_commit(check_sha, "job.json") or {}
                r_lease = remote_job.get("lease")
                if isinstance(r_lease, dict) and r_lease.get("worker_id") not in (None, self.machine_id):
                    raise LeaseLostError(f"{job_id} lease lost to {r_lease.get('worker_id')}")
            return False

    def hydrate_job(self, job_id: str, jdir: str) -> dict | None:
        ref = f"refs/factory/jobs/{job_id}"
        commit_sha = self._fetch_ref(ref)
        if not commit_sha:
            return None
        files = self._read_tree_files(commit_sha)
        if not files:
            return None
        os.makedirs(jdir, exist_ok=True)
        for rel_path, content in files.items():
            dest = os.path.join(jdir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)
        raw_job = files.get("job.json")
        if not raw_job:
            return None
        try:
            data = json.loads(raw_job.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def list_remote_jobs(self) -> list[dict]:
        refs = self._ls_remote("refs/factory/jobs/*")
        jobs: list[dict] = []
        for ref in sorted(refs):
            commit_sha = self._fetch_ref(ref)
            if not commit_sha:
                continue
            data = self._read_json_from_commit(commit_sha, "job.json")
            if data:
                jobs.append(data)
        return jobs

    # ------------------------------------------------------------------
    # Slice checkpoints
    # ------------------------------------------------------------------

    def push_slice_checkpoint(self, job_id: str, worktree: str) -> str:
        sha = _run_git(worktree, "rev-parse", "HEAD").stdout.strip()
        ref = f"refs/factory/checkpoints/{job_id}"
        _run_git(worktree, "push", "--no-verify", "--quiet", "--force", self.remote, f"{sha}:{ref}")
        return sha

    def fetch_slice_checkpoint(self, job_id: str, worktree: str) -> str | None:
        ref = f"refs/factory/checkpoints/{job_id}"
        remote_sha = self._ls_remote_one(ref)
        if not remote_sha:
            return None
        with repo_lock(self.repo_root):
            res = _run_git(worktree, "fetch", "--no-tags", "--quiet", self.remote, ref, check=False)
            if res.returncode != 0:
                return None
            _run_git(worktree, "reset", "--hard", "FETCH_HEAD")
            return _run_git(worktree, "rev-parse", "HEAD").stdout.strip()

    def delete_slice_checkpoint(self, job_id: str) -> None:
        self._delete_remote_ref(f"refs/factory/checkpoints/{job_id}")

    # ------------------------------------------------------------------
    # Distributed staging lock
    # ------------------------------------------------------------------

    def acquire_staging_lock(
        self,
        job_ids: list[str],
        *,
        ttl_sec: int = 1800,
        now_ts: float | None = None,
    ) -> bool:
        ts = time.time() if now_ts is None else now_ts
        ref = "refs/factory/locks/staging"
        old_sha = self._ls_remote_one(ref)
        if old_sha:
            fetched = self._fetch_ref(ref)
            if fetched:
                old_sha = fetched
                lock_data = self._read_json_from_commit(old_sha, "lock.json") or {}
                holder = lock_data.get("holder")
                expires_at = float(lock_data.get("expires_at_ts") or 0.0)
                if holder and holder != self.machine_id and ts <= expires_at:
                    return False

        payload = {
            "holder": self.machine_id,
            "hostname": self.hostname,
            "job_ids": list(job_ids),
            "acquired_at": _iso(ts),
            "acquired_ts": ts,
            "expires_at_ts": ts + ttl_sec,
        }
        return self._push_json_ref(ref, "lock.json", payload, f"staging lock by {self.machine_id}", old_sha)

    def release_staging_lock(self) -> None:
        ref = "refs/factory/locks/staging"
        old_sha = self._ls_remote_one(ref)
        if not old_sha:
            return
        fetched = self._fetch_ref(ref)
        if fetched:
            lock_data = self._read_json_from_commit(fetched, "lock.json") or {}
            holder = lock_data.get("holder")
            if holder and holder != self.machine_id:
                return
        self._delete_remote_ref(ref)

    # ------------------------------------------------------------------
    # Global kill switch (control)
    # ------------------------------------------------------------------

    def get_control_state(self) -> dict:
        ref = "refs/factory/control"
        commit_sha = self._fetch_ref(ref)
        if not commit_sha:
            return {"halted": False, "reason": "", "halted_by": "", "halted_at": ""}
        data = self._read_json_from_commit(commit_sha, "control.json")
        return data or {"halted": False, "reason": "", "halted_by": "", "halted_at": ""}

    def set_halted(self, halted: bool, reason: str = "") -> dict:
        ref = "refs/factory/control"
        for _ in range(3):
            old_sha = self._ls_remote_one(ref)
            if old_sha:
                old_sha = self._fetch_ref(ref) or old_sha
            payload = {
                "halted": bool(halted),
                "reason": reason,
                "halted_by": self.machine_id,
                "halted_at": _iso(time.time()),
            }
            if self._push_json_ref(ref, "control.json", payload, f"control halted={halted}", old_sha):
                return payload
        raise RuntimeError("Failed to CAS-update refs/factory/control after 3 attempts")

    # ------------------------------------------------------------------
    # Shared LLM quota cooldowns
    # ------------------------------------------------------------------

    def get_quota_state(self) -> dict:
        ref = "refs/factory/quota"
        commit_sha = self._fetch_ref(ref)
        if not commit_sha:
            return {"models": {}}
        data = self._read_json_from_commit(commit_sha, "quota.json")
        if not data or not isinstance(data.get("models"), dict):
            return {"models": {}}
        return data

    def record_quota_cooldown(
        self,
        base_model: str,
        reason: str,
        retry_after_sec: int | None = None,
        *,
        now_ts: float | None = None,
    ) -> dict:
        ts = time.time() if now_ts is None else now_ts
        ref = "refs/factory/quota"
        for _ in range(3):
            old_sha = self._ls_remote_one(ref)
            state: dict = {"models": {}}
            if old_sha:
                fetched = self._fetch_ref(ref)
                if fetched:
                    old_sha = fetched
                    state = self._read_json_from_commit(old_sha, "quota.json") or {"models": {}}
                    if not isinstance(state.get("models"), dict):
                        state["models"] = {}
            prev = state["models"].get(base_model) or {}
            consecutive = int(prev.get("consecutive_429s") or 0) + 1
            idx = min(consecutive - 1, len(QUOTA_BACKOFF_SCHEDULE) - 1)
            wait_sec = max(QUOTA_BACKOFF_SCHEDULE[idx], int(retry_after_sec or 0))
            state["models"][base_model] = {
                "consecutive_429s": consecutive,
                "cooldown_until": _iso(ts + wait_sec),
                "cooldown_until_ts": ts + wait_sec,
                "reported_by": self.machine_id,
                "reason": reason[:200],
            }
            if self._push_json_ref(ref, "quota.json", state, f"quota cooldown {base_model}", old_sha):
                return state
        raise RuntimeError("Failed to CAS-update refs/factory/quota after 3 attempts")

    def clear_quota_cooldown(self, base_model: str) -> None:
        ref = "refs/factory/quota"
        for _ in range(3):
            old_sha = self._ls_remote_one(ref)
            if not old_sha:
                return
            fetched = self._fetch_ref(ref)
            if not fetched:
                return
            old_sha = fetched
            state = self._read_json_from_commit(old_sha, "quota.json") or {"models": {}}
            models = state.get("models")
            if not isinstance(models, dict) or base_model not in models:
                return
            models.pop(base_model, None)
            if self._push_json_ref(ref, "quota.json", state, f"clear quota {base_model}", old_sha):
                return

    # ------------------------------------------------------------------
    # Shared DAG cache
    # ------------------------------------------------------------------

    def get_cached_dag(self, issue_set_hash: str) -> dict | None:
        ref = f"refs/factory/dag/{issue_set_hash}"
        commit_sha = self._fetch_ref(ref)
        if not commit_sha:
            return None
        return self._read_json_from_commit(commit_sha, "dag.json")

    def put_cached_dag(self, issue_set_hash: str, order_deps: dict, hard_deps: dict) -> None:
        ref = f"refs/factory/dag/{issue_set_hash}"
        old_sha = self._ls_remote_one(ref)
        payload = {
            "issue_set_hash": issue_set_hash,
            "order_deps": order_deps,
            "hard_deps": hard_deps,
            "cached_at": _iso(time.time()),
        }
        self._push_json_ref(ref, "dag.json", payload, f"cache dag {issue_set_hash}", old_sha, force=True)

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------

    def prune_stale_refs(self, *, max_age_days: int = 30, now_ts: float | None = None) -> int:
        ts = time.time() if now_ts is None else now_ts
        cutoff = ts - (max_age_days * 86400)
        deleted = 0
        job_refs = self._ls_remote("refs/factory/jobs/*")
        for ref in sorted(job_refs):
            commit_sha = self._fetch_ref(ref)
            if not commit_sha:
                continue
            job_data = self._read_json_from_commit(commit_sha, "job.json") or {}
            stage = job_data.get("stage")
            if stage not in TERMINAL_STAGES:
                continue
            updated_ts = _parse_ts(job_data.get("updated_at")) or _parse_ts((job_data.get("lease") or {}).get("claimed_at"))
            if updated_ts and updated_ts < cutoff:
                job_id = ref.rsplit("/", 1)[-1]
                self._delete_remote_ref(ref)
                self._delete_remote_ref(f"refs/factory/checkpoints/{job_id}")
                deleted += 1
        return deleted
