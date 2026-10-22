import os

import pytest

from devops_agent.config import Config, ConfigError, load_config, load_env_file

BASE = {"LLM_BASE_URL": "https://llm.example/v1", "LLM_API_KEY": "key-12345678", "LLM_MODEL": "m"}


def test_defaults_applied():
    cfg = Config.from_env(BASE)
    assert (cfg.max_steps, cfg.timeout_seconds) == (15, 600)


def test_blank_optionals_use_defaults():
    cfg = Config.from_env({**BASE, "AGENT_MAX_STEPS": "", "AGENT_TIMEOUT_SECONDS": "  "})
    assert (cfg.max_steps, cfg.timeout_seconds) == (15, 600)


def test_overrides():
    cfg = Config.from_env({**BASE, "AGENT_MAX_STEPS": "12", "AGENT_TIMEOUT_SECONDS": "90"})
    assert (cfg.max_steps, cfg.timeout_seconds) == (12, 90)


def test_missing_required_names_every_missing_variable():
    with pytest.raises(ConfigError) as info:
        Config.from_env({"LLM_BASE_URL": "x", "LLM_API_KEY": "  "})
    listed = str(info.value).split(". ")[0]  # the "missing required setting(s): ..." part
    assert "LLM_API_KEY" in listed and "LLM_MODEL" in listed
    assert "LLM_BASE_URL" not in listed


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


def test_min_interval_default_and_parsing():
    assert Config.from_env(BASE).min_interval_seconds == 0
    assert Config.from_env({**BASE, "LLM_MIN_INTERVAL_SECONDS": "12.5"}).min_interval_seconds == 12.5
    assert Config.from_env({**BASE, "LLM_MIN_INTERVAL_SECONDS": ""}).min_interval_seconds == 0


@pytest.mark.parametrize("bad", ["fast", "-1"])
def test_min_interval_rejects_invalid(bad):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE, "LLM_MIN_INTERVAL_SECONDS": bad})


def test_context_chars_default_override_and_validation():
    assert Config.from_env(BASE).context_chars == 80_000
    assert Config.from_env({**BASE, "LLM_CONTEXT_CHARS": "12000"}).context_chars == 12000
    assert Config.from_env({**BASE, "LLM_CONTEXT_CHARS": ""}).context_chars == 80_000
    for bad in ("abc", "0", "-5"):
        with pytest.raises(ConfigError):
            Config.from_env({**BASE, "LLM_CONTEXT_CHARS": bad})


# ---- a key alone is enough for the common providers ---------------------------------------

@pytest.mark.parametrize(
    "key, host, model_fragment",
    [
        ("sk-abcdefghijklmnop", "api.openai.com", "gpt"),
        ("AIzaSyAbcdefghijklmnopqrstuvwxyz0123456", "generativelanguage.googleapis.com", "gemini"),
        ("AQ.Ab8RN6abcdefghijklmnop", "generativelanguage.googleapis.com", "gemini"),
        ("gsk_abcdefghijklmnopqrstuvwx", "api.groq.com", "gpt-oss"),
        ("sk-ant-abcdefghijklmnop", "api.anthropic.com", "claude"),
    ],
)
def test_llm_api_key_alone_infers_provider_from_its_prefix(key, host, model_fragment):
    cfg = Config.from_env({"LLM_API_KEY": key})
    assert host in cfg.base_url and model_fragment in cfg.model
    assert len(cfg.notes) == 2 and key not in " ".join(cfg.notes)


@pytest.mark.parametrize(
    "variable, host",
    [
        ("OPENAI_API_KEY", "api.openai.com"),
        ("GEMINI_API_KEY", "generativelanguage.googleapis.com"),
        ("GOOGLE_API_KEY", "generativelanguage.googleapis.com"),
        ("GROQ_API_KEY", "api.groq.com"),
        ("ANTHROPIC_API_KEY", "api.anthropic.com"),
    ],
)
def test_provider_specific_key_variables_are_accepted(variable, host):
    cfg = Config.from_env({variable: "opaque-key-without-a-known-prefix"})
    assert host in cfg.base_url and cfg.api_key == "opaque-key-without-a-known-prefix"


def test_llm_api_key_takes_priority_over_provider_variables():
    cfg = Config.from_env({"OPENAI_API_KEY": "sk-openai-key-1234", "LLM_API_KEY": "gsk_groq_key_1234567"})
    assert cfg.api_key == "gsk_groq_key_1234567" and "groq" in cfg.base_url


def test_explicit_settings_win_and_produce_no_notes():
    cfg = Config.from_env({"LLM_API_KEY": "sk-abcdefghijklmnop", "LLM_BASE_URL": "http://localhost:11434/v1", "LLM_MODEL": "mine"})
    assert (cfg.base_url, cfg.model, cfg.notes) == ("http://localhost:11434/v1", "mine", ())


def test_known_base_url_without_model_gets_that_providers_default_model():
    cfg = Config.from_env({"LLM_API_KEY": "opaque-key", "LLM_BASE_URL": "https://api.groq.com/openai/v1"})
    assert "gpt-oss" in cfg.model and len(cfg.notes) == 1


def test_unknown_provider_still_requires_url_and_model():
    with pytest.raises(ConfigError) as info:
        Config.from_env({"LLM_API_KEY": "opaque-key-without-prefix"})
    listed = str(info.value).split(". ")[0]
    assert "LLM_BASE_URL" in listed and "LLM_MODEL" in listed
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Config.from_env({"LLM_API_KEY": "opaque-key", "LLM_BASE_URL": "http://localhost:11434/v1"})


def test_no_key_at_all_is_an_error_that_explains_the_shortcut():
    with pytest.raises(ConfigError, match="LLM_API_KEY alone is enough"):
        Config.from_env({})
