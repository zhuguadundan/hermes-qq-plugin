from pathlib import Path


def test_no_legacy_bridge_package_remains():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "napcat_qq_bridge").exists()
    assert (root / "hermes_qq" / "adapter.py").exists()
    assert (root / "gateway_platform_shim" / "qq.py").exists()
