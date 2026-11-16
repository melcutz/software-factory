from factory.lib.onboarding import generate_repo_map


def test_generate_repo_map_includes_ast_symbols_test_pairs_and_line_cap(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "alpha.py").write_text(
        "class AlphaEngine:\n    pass\n\ndef run_alpha():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "tests" / "test_alpha.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    out = generate_repo_map(str(tmp_path))
    lines = out.splitlines()
    assert len(lines) <= 150
    assert "## Test Mapping" in out
    assert "`src/alpha.py` -> `tests/test_alpha.py`" in out
    assert "## Python Symbol Index" in out
    assert "`src/alpha.py`" in out
    assert "`AlphaEngine` (L1)" in out
    assert "`run_alpha` (L4)" in out
