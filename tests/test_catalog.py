import json

from qoder2api.catalog import load_qoder_models, resolve_model_name


def test_load_qoder_models_normalizes_mmodel_display_name(tmp_path) -> None:
    qoder_home = tmp_path / '.qoder'
    auth_dir = qoder_home / '.auth'
    auth_dir.mkdir(parents=True)
    (auth_dir / 'models').write_text(
        json.dumps(
            {
                'chat': [
                    {
                        'key': 'mmodel',
                        'display_name': 'MiniMax-M2.5',
                        'max_input_tokens': 180000,
                        'max_output_tokens': 32768,
                    }
                ]
            }
        ),
        encoding='utf-8',
    )

    models = load_qoder_models(qoder_home)

    assert len(models) == 1
    assert models[0].id == 'mmodel'
    assert models[0].display_name == 'MiniMax-M2.5'
    assert models[0].public_id == 'MiniMax-M2.5'
    assert resolve_model_name('MiniMax-M2.5', models, 'auto') == 'mmodel'
    assert resolve_model_name('MiniMax M2.5', models, 'auto') == 'mmodel'
