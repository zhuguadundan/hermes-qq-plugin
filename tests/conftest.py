"""Never let adapter tests read or write the live Hermes group archive."""
import pytest


@pytest.fixture(autouse=True)
def isolated_group_archive(tmp_path, monkeypatch):
    from gateway.platforms.qq import NapCatQQAdapter
    monkeypatch.setattr(NapCatQQAdapter, '_group_file_root', lambda self: tmp_path / 'qq_group_files')
