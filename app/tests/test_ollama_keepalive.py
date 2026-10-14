"""The local fallback must remain lazy and only retain an already-used Ollama model briefly."""
import json
from novel_translator.providers.local_llm import LocalLLMProvider
from novel_translator.providers.base import TranslationRequest


class Response:
    status_code = 200
    def iter_lines(self):
        yield json.dumps({"message": {"content": "<P000001>\\n번역."}, "done": False}).encode()
        yield json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}).encode()
    def close(self):
        pass


def test_ollama_model_is_not_loaded_until_first_translation(monkeypatch):
    from novel_translator.providers import local_llm
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs["json"]))
        return Response()
    monkeypatch.setattr(local_llm.requests, "post", post)
    provider = LocalLLMProvider()
    assert not seen  # Constructing the backup provider does not start Ollama.
    request = TranslationRequest("qwen", "Translate.", "<P000001>\\nHello.", timeout=60)
    provider.translate(request, "local")
    provider.translate(request, "local")
    assert len(seen) == 2
    assert all(url.endswith("/api/chat") for url, _ in seen)
    assert all(payload["keep_alive"] == "5m" for _, payload in seen)
    assert all(payload["stream"] is True for _, payload in seen)


def test_other_local_api_not_given_ollama_keepalive(monkeypatch):
    from novel_translator.providers import local_llm
    seen = []
    class CompatibleResponse:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "<P000001>\\n번역."}, "finish_reason": "stop"}]}
    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return CompatibleResponse()
    monkeypatch.setattr(local_llm.requests, "post", post)
    p = LocalLLMProvider(LocalLLMProvider.LMSTUDIO_ENDPOINT)
    p.translate(TranslationRequest("model", "Translate.", "<P000001>\\nHello."), "local")
    assert len(seen) == 1 and "keep_alive" not in seen[0]
