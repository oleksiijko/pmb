"""The selected Ollama settings must reach real HTTP requests, including auto detection."""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from pmb.cli.main import app
from pmb.config import Config
from pmb.core.workspace import detect_workspace
from pmb.health.consolidate import ClaudeCLIClient, OllamaClient, resolve_llm_client


@pytest.fixture
def config(tmp_pmb_home, monkeypatch):
    monkeypatch.setenv("PMB_WORKSPACE", "ollama-test")
    for key in ("PMB_OLLAMA_MODEL", "PMB_OLLAMA_URL", "OLLAMA_HOST",
                "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    workspace = detect_workspace()
    config = Config(workspace.storage_dir, tmp_pmb_home)
    config.set_global("ollama.model", "selected:3b")
    config.set_global("ollama.url", "http://localhost:12345/")
    return config


@pytest.mark.parametrize("backend", ["ollama", "auto"])
def test_resolver_pings_and_generates_with_selected_settings(config, monkeypatch, backend):
    requests = []
    def respond(request, timeout):
        requests.append(request)
        response = io.BytesIO(json.dumps({"response": "PONG"}).encode())
        response.status = 200
        return response
    monkeypatch.setattr(ClaudeCLIClient, "available", staticmethod(lambda: False))
    monkeypatch.setattr("urllib.request.urlopen", respond)
    client = resolve_llm_client(backend)
    assert client.complete("ping") == "PONG"
    assert [request.full_url for request in requests] == [
        "http://localhost:12345/api/tags", "http://localhost:12345/api/generate"]
    assert json.loads(requests[1].data)["model"] == "selected:3b"


def test_model_and_url_precedence(config, monkeypatch):
    config.set_workspace("ollama.model", "workspace:3b")
    assert OllamaClient().model == "workspace:3b"
    monkeypatch.setenv("PMB_OLLAMA_MODEL", "env:3b")
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11435")
    assert OllamaClient().model == "env:3b"
    assert OllamaClient().base_url == "http://localhost:11435"
    monkeypatch.setenv("PMB_OLLAMA_URL", "http://localhost:11436")
    assert OllamaClient().base_url == "http://localhost:11436"
    client = OllamaClient(model="explicit:3b", base_url="http://localhost:11437/")
    assert (client.model, client.base_url) == ("explicit:3b", "http://localhost:11437")


def test_ollama_use_reaches_client(config, monkeypatch):
    monkeypatch.setattr(OllamaClient, "ping", staticmethod(lambda **kwargs: True))
    result = CliRunner().invoke(app, ["ollama", "use", "llama3.2:3b", "--for", "consolidate"])
    assert result.exit_code == 0, result.output
    assert OllamaClient().model == "llama3.2:3b"
    with patch("pmb.health.consolidate.run_consolidation", return_value={}) as run:
        from pmb.core.engine import Engine
        with Engine() as engine:
            engine.consolidate(dry_run=True)
            assert run.call_args.kwargs["backend"] == "ollama"
            engine.consolidate(dry_run=True, backend="auto")
            assert run.call_args.kwargs["backend"] == "auto"


def test_unknown_ollama_target_does_not_change_config(config, monkeypatch):
    monkeypatch.setattr(OllamaClient, "ping", staticmethod(lambda **kwargs: True))
    result = CliRunner().invoke(app, ["ollama", "use", "new:3b", "--for", "typo"])
    assert result.exit_code != 0
    assert OllamaClient().model == "selected:3b"


@pytest.mark.parametrize("operation", ["complete", "consolidate"])
def test_missing_model_reports_http_error_and_selected_model(config, monkeypatch, operation):
    import urllib.error

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {},
                                     io.BytesIO(b'{"error":"model not found"}'))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(RuntimeError, match="Ollama HTTP 404.*selected:3b"):
        getattr(OllamaClient(), operation)("test" if operation == "complete" else ["test"])


@pytest.mark.parametrize("payload", ["not json", "[]", '{"response": 123}', '{"error":"failed"}'])
def test_invalid_generation_is_not_reported_as_success(config, monkeypatch, payload):
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(payload.encode()))
    with pytest.raises(RuntimeError):
        OllamaClient().complete("test")


def test_explicit_engine_config_does_not_use_process_workspace(config, monkeypatch, tmp_path):
    other = Config(tmp_path / "another-workspace", tmp_path / "another-home")
    other.set_workspace("ollama.model", "other:7b")
    other.set_workspace("ollama.url", "http://localhost:15555")
    monkeypatch.setattr(OllamaClient, "ping", staticmethod(lambda **kwargs: True))
    client = resolve_llm_client("ollama", config=other)
    assert client.model == "other:7b"
    assert client.base_url == "http://localhost:15555"
