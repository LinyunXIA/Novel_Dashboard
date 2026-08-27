"""import_files.yaml 激活清单门控测试（app/ingest/manifest.py）。

验证：
- load_active_files 读清单返回 active:true 路径集；无清单返回 None；缺失激活路径告警。
- require_active_files 仅 prod 强制门控（缺清单报错）；dev/test 返回 None（全量，不门控）。
不连库；用 tmp_path 造临时 source_dir 与 import_files.yaml。
"""
import textwrap

import typer
import pytest

from app.ingest.manifest import MANIFEST_NAME, load_active_files, require_active_files


def _write_manifest(root, body: str):
    (root / MANIFEST_NAME).write_text(textwrap.dedent(body), encoding="utf-8")


def _cfg(root):
    import types
    return types.SimpleNamespace(source_dir=root)


class TestLoadActiveFiles:
    def test_no_manifest_returns_none(self, tmp_path):
        assert load_active_files(tmp_path) is None

    def test_only_inactive_gives_empty_set(self, tmp_path):
        _write_manifest(tmp_path, """
        version: 1
        files:
          - path: 时间线.md
            active: false
          - path: 人物/主角.md
            active: false
        """)
        assert load_active_files(tmp_path) == set()

    def test_active_subset_returned(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        (tmp_path / "b.md").write_text("x", encoding="utf-8")
        _write_manifest(tmp_path, """
        version: 1
        files:
          - path: a.md
            active: true
          - path: b.md
            active: false
        """)
        assert load_active_files(tmp_path) == {"a.md"}

    def test_active_but_missing_file_still_warns_and_included(self, tmp_path, capsys):
        _write_manifest(tmp_path, """
        version: 1
        files:
          - path: ghost.md
            active: true
        """)
        active = load_active_files(tmp_path)
        assert active == {"ghost.md"}
        captured = capsys.readouterr()
        assert "文件缺失" in (captured.out + captured.err)


class TestRequireActiveFiles:
    def test_non_prod_returns_none(self, tmp_path):
        # 即使有清单、也无清单，dev/test 一律不门控
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        _write_manifest(tmp_path, "version: 1\nfiles:\n  - path: a.md\n    active: true\n")
        for env in ("dev", "test"):
            assert require_active_files(env, _cfg(tmp_path)) is None

    def test_prod_missing_manifest_exits(self, tmp_path):
        with pytest.raises(typer.Exit):
            require_active_files("prod", _cfg(tmp_path))

    def test_prod_with_manifest_returns_active(self, tmp_path):
        (tmp_path / "时间线.md").write_text("x", encoding="utf-8")
        _write_manifest(tmp_path, """
        version: 1
        files:
          - path: 时间线.md
            active: true
        """)
        assert require_active_files("prod", _cfg(tmp_path)) == {"时间线.md"}