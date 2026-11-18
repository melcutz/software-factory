import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _is_temp_path(path_str: str) -> bool:
    if not path_str:
        return False
    real = os.path.realpath(path_str)
    tmp_real = os.path.realpath(tempfile.gettempdir())
    return real == tmp_real or real.startswith(tmp_real + os.sep) or real.startswith("/tmp/")


@pytest.fixture(autouse=True)
def _hermetic_coder_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Prevent any test from invoking real opencode, real agy, or real GPU MCP servers."""
    hermetic_bin = tmp_path / "_hermetic_bins"
    hermetic_bin.mkdir(parents=True, exist_ok=True)

    fake_oc = hermetic_bin / "opencode"
    fake_oc.write_text(
        "#!/bin/sh\necho 'real opencode called in tests' >&2\nexit 127\n",
        encoding="utf-8",
    )
    fake_oc.chmod(0o755)

    fake_agy = hermetic_bin / "agy"
    fake_agy.write_text(
        "#!/bin/sh\necho 'real agy called in tests' >&2\nexit 127\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    monkeypatch.setenv("FACTORY_USER_CONFIG", str(tmp_path / "_nonexistent_user_config.json"))
    monkeypatch.setenv("FACTORY_OPENCODE_BIN", str(fake_oc))
    monkeypatch.delenv("FACTORY_GPU_MCP_URL", raising=False)

    import factory.lib.agy_runner as agy_runner
    from factory.lib.config import expand_path_prepend
    import factory.lib.gpu_runner as gpu_runner

    def _hermetic_resolve_agy_bin(path: str | None = None, config: dict | None = None) -> str:
        env_agy = (os.environ.get("FACTORY_AGY_BIN") or "").strip()
        if env_agy:
            return env_agy
        if isinstance(config, dict):
            providers = config.get("providers")
            if isinstance(providers, dict) and isinstance(providers.get("agy"), dict):
                cfg_bin = providers["agy"].get("bin")
                if isinstance(cfg_bin, str) and cfg_bin.strip():
                    expanded = os.path.expanduser(cfg_bin.strip())
                    if _is_temp_path(expanded):
                        return expanded
        search_path = path if path is not None else os.environ.get("PATH", "")
        prepend = expand_path_prepend(config)
        if prepend:
            search_path = os.pathsep.join([*prepend, search_path]) if search_path else os.pathsep.join(prepend)
        try:
            which_agy = shutil.which("agy", path=search_path)
        except TypeError:
            which_agy = shutil.which("agy")
        if which_agy and _is_temp_path(which_agy):
            return which_agy
        return str(fake_agy)

    monkeypatch.setattr(agy_runner, "resolve_agy_bin", _hermetic_resolve_agy_bin)

    orig_rpc = gpu_runner.McpStreamableClient._rpc

    def _guarded_rpc(self, *args, **kwargs):
        if not self.url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise AssertionError(f"Refusing non-local GPU MCP call in tests: {self.url!r}")
        return orig_rpc(self, *args, **kwargs)

    monkeypatch.setattr(gpu_runner.McpStreamableClient, "_rpc", _guarded_rpc)

    orig_invoke = agy_runner._invoke_agy_process

    def _guarded_invoke_agy_process(cmd, cwd, *args, **kwargs):
        if not _is_temp_path(str(cwd)):
            raise AssertionError(f"Refusing to run provider subprocess outside temp dir: cwd={cwd!r}, cmd={cmd!r}")
        return orig_invoke(cmd, cwd, *args, **kwargs)

    monkeypatch.setattr(agy_runner, "_invoke_agy_process", _guarded_invoke_agy_process)
