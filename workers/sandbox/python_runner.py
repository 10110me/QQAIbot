"""Runs inside the untrusted runner: restore this task's files, then execute."""
import json
import os
import sys
from pathlib import Path

data = json.loads(Path('/input/program.json').read_text())
root = Path('/output')
for name, content in data.get('files', {}).items():
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
if Path('/inputs').is_dir() and not (root/'inputs').exists():
    (root/'inputs').symlink_to('/inputs', target_is_directory=True)
os.chdir(root)
sys.path.insert(0, str(root))
exec(compile(data['code'], '<workspace-program>', 'exec'), {'__name__': '__main__'})
