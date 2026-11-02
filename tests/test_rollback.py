"""Code and config rollback."""
import shutil
from pathlib import Path

from hermes_lite import rollback


def _project(tmp_path, build):
    root = tmp_path / "proj" / "hermes-lite"
    pkg = root / "hermes_lite"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'BUILD = "{build}"\n')
    (root / "install.sh").write_text("echo\n")
    return root


def test_code_rollback_and_undo(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = _project(tmp_path, "v1")
    monkeypatch.setattr(rollback, "PROJECT", root)
    rollback.snapshot_code(root, quiet=True)            # install of v1
    (root / "hermes_lite" / "__init__.py").write_text('BUILD = "v2"\n')
    import time; time.sleep(1.1)
    rollback.snapshot_code(root, quiet=True)            # install of v2
    assert rollback.rollback_code() == 0
    assert 'v1' in (root / "hermes_lite" / "__init__.py").read_text()
    assert rollback.rollback_code(latest=True) == 0
    assert 'v2' in (root / "hermes_lite" / "__init__.py").read_text()


def test_config_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LITE_HOME", str(tmp_path / "home"))
    cfg = tmp_path / "home" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("server:\n  n_cpu_moe: 39\n")
    rollback.snapshot_config(cfg)                       # a launch
    import time; time.sleep(1.1)
    cfg.write_text("server:\n  n_cpu_moe: 99\n")
    rollback.snapshot_config(cfg)                       # next launch
    assert rollback.rollback_config(cfg) == 0
    assert "39" in cfg.read_text()
