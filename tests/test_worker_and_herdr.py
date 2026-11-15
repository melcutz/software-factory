import io
import sys

from factory.lib.herdr_bridge import HerdrBridge


def test_herdr_bridge_noop_outside_hc(monkeypatch):
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    buf = io.StringIO()
    bridge = HerdrBridge.from_env(stream=buf)
    assert bridge.enabled is False
    bridge.emit_status("working", progress=50)
    assert buf.getvalue() == ""
    assert bridge.spawn_job_terminal("repo-1", "/tmp", ["echo", "hi"]) is None


def test_herdr_bridge_osc7501_and_spawn_job_terminal(tmp_path, monkeypatch):
    log_file = tmp_path / "hc_calls.log"
    fake_bin = tmp_path / "hc"
    fake_bin.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['run']:\n"
        "    print('t9')\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_BIN_PATH", str(fake_bin))

    buf = io.StringIO()
    bridge = HerdrBridge.from_env(stream=buf)
    assert bridge.enabled is True
    bridge.emit_status("working", progress=40)
    bridge.emit_status("blocked", kind="question")
    tid = bridge.spawn_job_terminal("repo-5", str(tmp_path), ["factory", "_exec-job", "5"])
    assert tid == "t9"

    out = buf.getvalue()
    assert "\033]7501;state=working:progress=40\033\\" in out
    assert "\033]7501;state=blocked:kind=question\033\\" in out
    lines = log_file.read_text().splitlines()
    assert any(ln.startswith("run --bg --name repo-5") and "-- factory _exec-job 5" in ln for ln in lines)
