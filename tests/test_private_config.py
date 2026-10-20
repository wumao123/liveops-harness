import os

import pytest

from liveops.private_config import load_private_settings, request_options


def test_settings_are_explicit_and_do_not_override_environment(tmp_path, monkeypatch):
    path = tmp_path / "settings"
    path.write_text("LIVEOPS_LLM_MODEL=fixture\nLIVEOPS_LLM_THINKING=disabled\n")
    path.chmod(0o600)
    monkeypatch.setenv("LIVEOPS_LLM_MODEL", "keep-existing")
    monkeypatch.delenv("LIVEOPS_LLM_THINKING", raising=False)
    load_private_settings(path)
    assert os.environ["LIVEOPS_LLM_MODEL"] == "keep-existing"
    assert request_options() == {"thinking": {"type": "disabled"}}


def test_private_settings_reject_broad_permissions(tmp_path):
    path = tmp_path / "settings"
    path.write_text("LIVEOPS_LLM_MODEL=fixture\n")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_private_settings(path)


def test_default_options_do_not_add_vendor_fields(monkeypatch):
    monkeypatch.delenv("LIVEOPS_LLM_THINKING", raising=False)
    assert request_options() == {}
