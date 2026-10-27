"""通过带版本校验的管理 API 原子部署中文 V4 人设，保留模型和身份绑定。"""
import argparse
import json
from pathlib import Path

import httpx

from osa.application.nova_v4 import prepare_configuration
from osa.config import Settings
from osa.infrastructure.runtime_config import RuntimeConfiguration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8080')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    settings = Settings()
    headers = {'Authorization': 'Bearer ' + settings.admin_token.get_secret_value()}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
        response = client.get('/api/v1/config')
        response.raise_for_status()
        before = response.json()['data']
        draft = prepare_configuration(before)
        RuntimeConfiguration.model_validate({**draft, 'providers': [
            {key: value for key, value in provider.items() if key != 'has_api_key'}
            for provider in draft.get('providers', [])]})
        if args.verify:
            assert draft == before
            print(json.dumps({'verified': True, 'revision': before['revision'], 'prompt_version': 'Nova V4 中文'}))
            return
        backup = Path(settings.data_dir) / 'private' / ('before-nova-v4-rev-' + str(before['revision']) + '.json')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open('x', encoding='utf-8') as stream:
            json.dump(before, stream, ensure_ascii=False, indent=2)
        encrypted = Path(settings.data_dir) / 'private' / 'runtime-config.enc'
        if encrypted.exists():
            with backup.with_suffix('.enc').open('xb') as stream:
                stream.write(encrypted.read_bytes())
        response = client.put('/api/v1/config', json={'configuration': draft, 'revision': before['revision']})
        response.raise_for_status()
        after = response.json()['data']
        for key in ('providers', 'models', 'task_configs'):
            assert after[key] == before[key]
        for key in ('name', 'aliases', 'owner_qq_ids', 'owner_address', 'self_update_enabled'):
            assert after['persona'][key] == before['persona'][key]
        print(json.dumps({'updated': True, 'revision': after['revision'], 'backup': str(backup),
            'models_unchanged': True, 'identity_bindings_unchanged': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
