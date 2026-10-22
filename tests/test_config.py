import os

import pytest

from devops_agent.config import Config, ConfigError, load_config, load_env_file

BASE = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": "key-12345678", "LLM_MODEL": "m"}


def test_defaults_applied():
    cfg = Config.from_env(BASE)
    assert (cfg.max_steps, cfg.timeout_seconds) == (30, 600)


def test_blank_optionals_use_defaults():
    cfg = Config.from_env({**BASE, "AGENT_MAX_STEPS": "", "AGENT_TIMEOUT_SECONDS": "  "})
    assert (cfg.max_steps, cfg.timeout_seconds) == (30, 600)


def test_overrides():
    cfg = Config.from_env({**BASE, "AGENT_MAX_STEPS": "12", "AGENT_TIMEOUT_SECONDS": "90"})
    assert (cfg.max_steps, cfg.timeout_seconds) == (12, 90)


def test_missing_required_names_every_missing_variable():
    with pytest.raises(ConfigError) as info:
        Config.from_env({"LLM_BASE_URL": "x", "LLM_API_KEY": "  "})
    assert "LLM_API_KEY" in str(info.value) and "LLM_MODEL" in str(info.value)
    assert "LLM_BASE_URL" not in str(info.value)


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "1.5"])
def test_invalid_numbers_rejected(bad):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "AGENT_MAX_STEPS": bad})


def test_repr_never_shows_api_key():
    assert "key-12345678" not in repr(Config.from_env(BASE))
    assert Config.from_env(BASE).secrets() == ["key-12345678"]


def test_env_file_parsing_and_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nLLM_MODEL=from-file\nLLM_API_KEY=\"quoted-key\"\nLLM_BASE_URL='http://x'\nno-equals-line\n=novalue\n",
        encoding="utf-8",
    )
    environ = {"LLM_MODEL": "from-env"}
    load_env_file(env_file, environ)
    assert environ["LLM_MODEL"] == "from-env"  # existing variables win
    assert environ["LLM_API_KEY"] == "quoted-key"
    assert environ["LLM_BASE_URL"] == "http://x"
    assert "" not in environ


def test_missing_env_file_is_ignored(tmp_path):
    environ = {}
    load_env_file(tmp_path / "absent.env", environ)
    assert environ == {}


def test_load_config_reads_cwd_then_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", {})  # isolate from the real environment and from other tests
    cwd, root = tmp_path / "cwd", tmp_path / "root"
    cwd.mkdir(), root.mkdir()
    (cwd / ".env").write_text("LLM_MODEL=cwd-model\n")
    (root / ".env").write_text("LLM_MODEL=root-model\nLLM_API_KEY=k-12345678\nLLM_BASE_URL=http://x\n")
    cfg = load_config(root, cwd=cwd)
    assert cfg.model == "cwd-model" and cfg.api_key == "k-12345678"
