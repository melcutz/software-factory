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
    monkeypatch.setenv("FACTORY_GPU_MCP_URL", "http://127.0.0.1:9/mcp")

    import factory.lib.agy_runner as agy_runner

    def _hermetic_resolve_agy_bin(path: str | None = None) -> str:
        env_agy = (os.environ.get("FACTORY_AGY_BIN") or "").strip()
        if env_agy:
            return env_agy
        which_agy = shutil.which("agy", path=path)
        if which_agy and _is_temp_path(which_agy):
            return which_agy
        return str(fake_agy)

    monkeypatch.setattr(agy_runner, "resolve_agy_bin", _hermetic_resolve_agy_bin)

    orig_invoke = agy_runner._invoke_agy_process

    def _guarded_invoke_agy_process(cmd, cwd, *args, **kwargs):
        if not _is_temp_path(str(cwd)):
            raise AssertionError(f"Refusing to run provider subprocess outside temp dir: cwd={cwd!r}, cmd={cmd!r}")
        return orig_invoke(cmd, cwd, *args, **kwargs)

    monkeypatch.setattr(agy_runner, "_invoke_agy_process", _guarded_invoke_agy_process)
