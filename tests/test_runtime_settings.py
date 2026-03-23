import asyncio

import qoder2api.qoder_api as qoder_api_module
from qoder2api import app as app_module
from qoder2api.runtime_settings import RuntimeSettingsManager, get_runtime_settings_path, load_runtime_settings
from qoder2api.schemas import RuntimeSettingsUpdateRequest
from qoder2api.settings import Settings


def test_runtime_settings_roundtrip(tmp_path) -> None:
    settings = Settings(qoder_home=str(tmp_path / '.qoder'), workspace=str(tmp_path))
    manager = RuntimeSettingsManager(settings)

    snapshot = manager.save(
        {
            'proxy_url': 'http://127.0.0.1:7890',
            'llmapi_api_key': 'sk-demo',
            'account_pool_max_concurrency': 7,
            'default_stream_openai': False,
            'default_stream_anthropic': False,
        }
    )
    loaded = load_runtime_settings(settings)

    assert manager.path == get_runtime_settings_path(settings)
    assert snapshot.proxy_url == 'http://127.0.0.1:7890'
    assert loaded.llmapi_api_key == 'sk-demo'
    assert loaded.account_pool_max_concurrency == 7
    assert loaded.default_stream_openai is False
    assert loaded.default_stream_anthropic is False


def test_runtime_settings_loads_legacy_default_stream(tmp_path) -> None:
    settings = Settings(qoder_home=str(tmp_path / '.qoder'), workspace=str(tmp_path))
    path = get_runtime_settings_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"default_stream": false}', encoding='utf-8')

    loaded = load_runtime_settings(settings)

    assert loaded.default_stream_openai is False
    assert loaded.default_stream_anthropic is False


def test_qoder_api_default_client_uses_runtime_proxy(monkeypatch, tmp_path) -> None:
    settings = Settings(qoder_home=str(tmp_path / '.qoder'), workspace=str(tmp_path))
    RuntimeSettingsManager(settings).save({'proxy_url': 'http://127.0.0.1:7890'})
    captured: dict[str, object] = {}

    class DummyAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(qoder_api_module.httpx, 'AsyncClient', DummyAsyncClient)

    client = qoder_api_module.QoderAPIClient(settings)
    resolved = client._default_client()

    assert isinstance(resolved, DummyAsyncClient)
    assert captured['proxy'] == 'http://127.0.0.1:7890'
    assert captured['timeout'] == settings.request_timeout_seconds
    assert captured['follow_redirects'] is True


def test_webui_runtime_settings_routes(monkeypatch, tmp_path) -> None:
    settings = Settings(qoder_home=str(tmp_path / '.qoder'), workspace=str(tmp_path))
    monkeypatch.setattr(app_module, 'get_settings', lambda: settings)

    payload = asyncio.run(app_module.webui_get_runtime_settings())
    assert payload['settings']['account_pool_max_concurrency'] == 4
    assert payload['settings']['default_stream_openai'] is True
    assert payload['settings']['default_stream_anthropic'] is False
    assert payload['limiter']['limit'] == 4

    updated = asyncio.run(
        app_module.webui_update_runtime_settings(
            RuntimeSettingsUpdateRequest(
                proxy_url='http://127.0.0.1:7890',
                llmapi_api_key='sk-demo',
                account_pool_max_concurrency=9,
                default_stream_openai=False,
                default_stream_anthropic=False,
            )
        )
    )

    assert updated['success'] is True
    assert updated['settings']['proxy_url'] == 'http://127.0.0.1:7890'
    assert updated['settings']['llmapi_api_key'] == 'sk-demo'
    assert updated['limiter']['limit'] == 9
    assert updated['settings']['default_stream_openai'] is False
    assert updated['settings']['default_stream_anthropic'] is False
