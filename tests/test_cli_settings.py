import json

import pytest
from click.testing import CliRunner
from datasette.cli import cli


@pytest.mark.parametrize("option", ["-c", "--config"])
@pytest.mark.parametrize("format", ["json", "yml"])
def test_chat_config_file(tmp_path, monkeypatch, option, format):
    captured = {}
    config = {
        "plugins": {"datasette-llm": {"default_model": "echo", "require_keys": False}},
        "settings": {"default_page_size": 12},
    }
    path = tmp_path / f"datasette.{format}"
    path.write_text(
        json.dumps(config)
        if format == "json"
        else """plugins:
  datasette-llm:
    default_model: echo
    require_keys: false
settings:
  default_page_size: 12
"""
    )

    async def fake_run_chat(datasette, **kwargs):
        captured["llm"] = datasette.plugin_config("datasette-llm")
        captured["page_size"] = datasette.setting("default_page_size")

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    result = CliRunner().invoke(cli, ["agent", "chat", option, str(path)])
    assert result.exit_code == 0, result.output
    assert captured == {"llm": config["plugins"]["datasette-llm"], "page_size": 12}


def test_chat_settings_deep_merge_config_file(tmp_path, monkeypatch):
    captured = {}
    endpoint = {
        "model": "local",
        "model_name": "remote",
        "responses": True,
        "api_base": "http://localhost:1234/v1",
        "options": {"temperature": 0.2, "max_tokens": 100},
    }
    path = tmp_path / "datasette.json"
    path.write_text(
        json.dumps({"plugins": {"datasette-llm": {"default_model": endpoint}}})
    )

    async def fake_run_chat(datasette, **kwargs):
        captured.update(datasette.plugin_config("datasette-llm"))

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            "-c",
            str(path),
            "-s",
            "plugins.datasette-llm.default_model.options.temperature",
            "0.7",
            "-s",
            "plugins.datasette-llm.require_keys",
            "false",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "default_model": {
            **endpoint,
            "options": {"temperature": 0.7, "max_tokens": 100},
        },
        "require_keys": False,
    }


def test_chat_model_overrides_config_file_and_settings(tmp_path, monkeypatch):
    captured = {}
    path = tmp_path / "datasette.json"
    path.write_text(
        json.dumps(
            {
                "plugins": {
                    "datasette-llm": {
                        "default_model": "file-model",
                        "require_keys": False,
                    }
                }
            }
        )
    )

    async def fake_run_chat(datasette, **kwargs):
        captured.update(datasette.plugin_config("datasette-llm"))

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            "-m",
            "echo",
            "-c",
            str(path),
            "-s",
            "plugins.datasette-llm.default_model",
            "setting-model",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"default_model": "echo", "require_keys": False}


def test_chat_config_file_runs_prompt(tmp_path):
    path = tmp_path / "datasette.yml"
    path.write_text("plugins:\n  datasette-llm:\n    default_model: echo\n")
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            ":memory:",
            "-c",
            str(path),
            "-p",
            "hello from config file",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "hello from config file" in result.output


def test_chat_missing_config_file(tmp_path):
    result = CliRunner().invoke(
        cli, ["agent", "chat", "-c", str(tmp_path / "missing.yml")]
    )
    assert result.exit_code == 2
    assert "missing.yml" in result.output
    assert "No such file or directory" in result.output


@pytest.mark.parametrize("option", ["-s", "--setting"])
def test_chat_endpoint_setting(monkeypatch, option):
    captured = {}
    endpoint = {
        "model": "lmstudio-qwen35-27b",
        "model_name": "qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2",
        "api_base": "http://localhost:1234/v1",
        "responses": True,
        "options": {"temperature": 0.2},
    }

    async def fake_run_chat(
        datasette, initial_prompt=None, actor=None, auto_approve=False
    ):
        captured["config"] = datasette.plugin_config("datasette-llm")
        captured["prompt"] = initial_prompt

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            ":memory:",
            option,
            "plugins.datasette-llm.default_model",
            json.dumps(endpoint),
            "-p",
            "hello",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"config": {"default_model": endpoint}, "prompt": "hello"}


def test_chat_repeated_settings(monkeypatch):
    captured = {}

    async def fake_run_chat(datasette, **kwargs):
        captured["llm"] = datasette.plugin_config("datasette-llm")
        captured["page_size"] = datasette.setting("default_page_size")

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            "-s",
            "plugins.datasette-llm.default_model",
            "echo",
            "--setting",
            "plugins.datasette-llm.require_keys",
            "false",
            "-s",
            "plugins.datasette-llm.models",
            '["echo"]',
            "-s",
            "default_page_size",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == {
        "llm": {"default_model": "echo", "require_keys": False, "models": ["echo"]},
        "page_size": 10,
    }


@pytest.mark.parametrize("model_first", [False, True])
def test_chat_model_overrides_default_setting(monkeypatch, model_first):
    captured = {}

    async def fake_run_chat(datasette, **kwargs):
        captured.update(datasette.plugin_config("datasette-llm"))

    monkeypatch.setattr("datasette_agent.cli_chat.run_chat", fake_run_chat)
    model_args = ["-m", "echo"]
    setting_args = [
        "-s",
        "plugins.datasette-llm.default_model",
        '{"model":"another-model"}',
        "-s",
        "plugins.datasette-llm.require_keys",
        "false",
    ]
    args = model_args + setting_args if model_first else setting_args + model_args
    result = CliRunner().invoke(cli, ["agent", "chat", *args])
    assert result.exit_code == 0, result.output
    assert captured == {"default_model": "echo", "require_keys": False}


def test_chat_setting_runs_prompt():
    result = CliRunner().invoke(
        cli,
        [
            "agent",
            "chat",
            ":memory:",
            "-s",
            "plugins.datasette-llm.default_model",
            "echo",
            "-p",
            "hello from settings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "hello from settings" in result.output


def test_chat_setting_uses_datasette_validation():
    result = CliRunner().invoke(
        cli, ["agent", "chat", "-s", "default_page_size", "invalid"]
    )
    assert result.exit_code == 2
    assert '"settings.default_page_size" should be an integer' in result.output
