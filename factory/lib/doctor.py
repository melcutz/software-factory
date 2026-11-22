"""Repository and host prerequisite health checks (`factory doctor`)."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys

from factory.lib.agy_runner import classify_model_provider, resolve_agy_bin
from factory.lib.config import expand_path_prepend, load_config_with_layers, model_chain
from factory.lib.staging_qa import build_deploy_env, load_staging_secrets


def run_doctor(repo_root: str) -> tuple[bool, list[str]]:
    """Verify repository, CLI prerequisites, config, checks.sh, and .gitignore."""
    repo_root = os.path.abspath(repo_root)
    factory_dir = os.path.join(repo_root, "factory")
    messages: list[str] = []
    ok = True

    if sys.version_info < (3, 10):
        ok = False
        messages.append(f"FAIL: Python >= 3.10 required (found {sys.version.split()[0]})")
    else:
        messages.append(f"OK: Python {sys.version.split()[0]}")

    cfg: dict | None = None
    cfg_layers: list[str] = []
    cfg_err: Exception | None = None
    try:
        cfg, cfg_layers = load_config_with_layers(factory_dir)
    except Exception as exc:
        cfg_err = exc

    search_path = os.environ.get("PATH", "")
    prepend_dirs = expand_path_prepend(cfg)
    if prepend_dirs:
        search_path = os.pathsep.join([*prepend_dirs, search_path])

    def _which_cmd(cmd_str: str) -> str | None:
        if prepend_dirs:
            try:
                return shutil.which(cmd_str, path=search_path)
            except TypeError:
                return shutil.which(cmd_str)
        return shutil.which(cmd_str)

    all_models: list[str] = []
    if cfg and isinstance(cfg.get("models"), dict):
        for role in cfg["models"]:
            all_models.extend(model_chain(cfg, role))

    needs_agy = (cfg is None) or (not all_models) or any(
        classify_model_provider(m)[0] == "agy" for m in all_models
    )
    required_bins: list[tuple[str, list[str]]] = [
        ("git", ["--version"]),
        ("gh", ["auth", "status"]),
    ]
    if needs_agy:
        required_bins.append(("agy", ["--version"]))

    for binary, args in required_bins:
        bin_target = binary
        if binary == "agy":
            try:
                bin_target = resolve_agy_bin(search_path, config=cfg)
            except TypeError:
                bin_target = resolve_agy_bin(search_path)
        resolved = _which_cmd(bin_target) or (
            bin_target if os.path.isfile(bin_target) and os.access(bin_target, os.X_OK) else None
        )
        if not resolved:
            ok = False
            messages.append(f"FAIL: `{binary}` not found on PATH")
            continue
        res = subprocess.run([resolved, *args], cwd=repo_root, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            ok = False
            messages.append(f"FAIL: `{binary} {' '.join(args)}` exited {res.returncode}")
        else:
            messages.append(f"OK: `{binary} {' '.join(args)}`")

    rem = subprocess.run(["git", "-C", repo_root, "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    if rem.returncode != 0:
        ok = False
        messages.append("FAIL: git remote `origin` is not configured")
    else:
        messages.append(f"OK: git origin -> {rem.stdout.strip()}")

    res_name = subprocess.run(
        ["git", "-C", repo_root, "config", "user.name"],
        capture_output=True,
        text=True,
        check=False,
    )
    user_name = res_name.stdout.strip()
    if res_name.returncode != 0 or not user_name:
        ok = False
        messages.append("MISSING: git config user.name is not set")
    else:
        messages.append(f"OK: git user.name -> {user_name}")

    res_email = subprocess.run(
        ["git", "-C", repo_root, "config", "user.email"],
        capture_output=True,
        text=True,
        check=False,
    )
    user_email = res_email.stdout.strip()
    if res_email.returncode != 0 or not user_email:
        ok = False
        messages.append("MISSING: git config user.email is not set")
    else:
        messages.append(f"OK: git user.email -> {user_email}")

    if cfg_err is None:
        layers_str = ", ".join(cfg_layers) if cfg_layers else "defaults only"
        messages.append(f"OK: factory/config.json valid (layers: {layers_str})")
    else:
        ok = False
        messages.append(f"FAIL: factory/config.json invalid: {cfg_err}")

    if cfg and all_models:
        if any(m.startswith(("opencode/", "opencode-go/", "openrouter/")) for m in all_models):
            from factory.lib.opencode_runner import resolve_opencode_bin

            oc_bin = resolve_opencode_bin(config=cfg, path=search_path)
            oc_resolved = _which_cmd(oc_bin) or (
                oc_bin if os.path.isfile(oc_bin) and os.access(oc_bin, os.X_OK) else None
            )
            if not oc_resolved:
                ok = False
                messages.append("FAIL: opencode not found; set providers.opencode.bin or path_prepend")
            else:
                try:
                    res_oc = subprocess.run(
                        [oc_resolved, "--version"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    if res_oc.returncode != 0:
                        ok = False
                        messages.append(f"FAIL: `{oc_resolved} --version` exited {res_oc.returncode}")
                    else:
                        ver_text = (res_oc.stdout or res_oc.stderr or "").strip()
                        first_line = ver_text.splitlines()[0] if ver_text else ""
                        detail = f" ({first_line})" if first_line else ""
                        messages.append(f"OK: `opencode --version`{detail}")
                except Exception as exc:
                    ok = False
                    messages.append(f"FAIL: `{oc_resolved} --version` failed: {exc}")

        if any(m.startswith("gpu:") for m in all_models):
            from factory.lib.gpu_runner import gpu_available, resolve_gpu_mcp_url, resolve_gpu_tools

            mcp_url = resolve_gpu_mcp_url(factory_dir=factory_dir, config=cfg)
            gpu_tools = resolve_gpu_tools(cfg)
            if not mcp_url:
                messages.append("OK: gpu decision MCP: disabled (gpu.mcp_url not set)")
            elif gpu_available(mcp_url, force=True, status_tool=gpu_tools.get("status")):
                messages.append(f"OK: GPU MCP reachable ({mcp_url})")
            else:
                messages.append(
                    f"WARN: GPU MCP unreachable ({mcp_url}); gpu: tier will abstain"
                )

    checks_sh = os.path.join(factory_dir, "checks.sh")
    if not (os.path.isfile(checks_sh) and os.access(checks_sh, os.X_OK)):
        ok = False
        messages.append("FAIL: factory/checks.sh missing or not executable")
    else:
        messages.append("OK: factory/checks.sh present and executable")

    if os.path.isfile(checks_sh):
        try:
            with open(checks_sh, encoding="utf-8", errors="replace") as f:
                checks_content = f.read()
        except Exception:
            checks_content = ""
        if "uv " in checks_content or "uv\n" in checks_content:
            if not shutil.which("uv"):
                ok = False
                messages.append("MISSING: `uv` not found on PATH (required by factory/checks.sh)")
            else:
                messages.append("OK: `uv` found")
        if "pytest" in checks_content:
            try:
                res_pytest = subprocess.run(
                    ["python3", "-m", "pytest", "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res_pytest.returncode != 0:
                    ok = False
                    messages.append(f"MISSING: python3 -m pytest (exit code {res_pytest.returncode})")
                else:
                    ver = res_pytest.stdout.strip() or res_pytest.stderr.strip()
                    detail = f" ({ver})" if ver else ""
                    messages.append(f"OK: python3 -m pytest{detail}")
            except Exception as exc:
                ok = False
                messages.append(f"MISSING: python3 -m pytest ({exc})")

    ig = subprocess.run(
        ["git", "-C", repo_root, "check-ignore", "factory/jobs/probe-job", ".worktrees/probe-job"],
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = {line.strip() for line in ig.stdout.splitlines() if line.strip()}
    if {"factory/jobs/probe-job", ".worktrees/probe-job"} <= ignored:
        messages.append("OK: .gitignore excludes factory/jobs/* and .worktrees/*")
    else:
        ok = False
        messages.append("FAIL: .gitignore must ignore `/factory/jobs/*` and `/.worktrees/`")

    if cfg and (cfg.get("staging_qa") or {}).get("enabled"):
        sqa = cfg["staging_qa"]
        try:
            secrets = load_staging_secrets(
                sqa.get("env_files") or [],
                repo_root=repo_root,
                test_mode_secret_keys=sqa.get("test_mode_secret_keys"),
            )
            messages.append(
                f"OK: staging_qa enabled (base_url={sqa.get('base_url')}, secrets_loaded={len(secrets)})"
            )
        except Exception as exc:
            ok = False
            messages.append(f"FAIL: staging_qa secret loading failed: {exc}")

        pw_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "from playwright.sync_api import sync_playwright; "
                "p = sync_playwright().start(); "
                "b = p.chromium.launch(headless=True); "
                "b.close(); p.stop()",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if pw_probe.returncode != 0:
            ok = False
            detail = (pw_probe.stderr or pw_probe.stdout or "").strip().splitlines()[-1:] or ["failed"]
            messages.append(f"FAIL: playwright headless chromium check failed ({detail[0]})")
        else:
            messages.append("OK: playwright headless chromium")

        wrangler_ok = bool(shutil.which("wrangler"))
        if not wrangler_ok:
            try:
                dep_env = build_deploy_env({}, sqa, repo_root=repo_root)
            except Exception:
                dep_env = {}
            configured_wrangler = str(dep_env.get("WRANGLER") or "").strip()
            if configured_wrangler:
                wr_argv = shlex.split(configured_wrangler)
                if wr_argv and shutil.which(wr_argv[0]):
                    wr = subprocess.run(
                        [*wr_argv, "--version"],
                        cwd=repo_root,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    wrangler_ok = wr.returncode == 0
        if not wrangler_ok and shutil.which("npx"):
            wr = subprocess.run(
                ["npx", "--no-install", "wrangler", "--version"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            wrangler_ok = wr.returncode == 0
        if wrangler_ok:
            messages.append("OK: wrangler available")
        else:
            ok = False
            messages.append("MISSING: wrangler (install globally or via npm/npx for staging_qa deploy)")

    return ok, messages
