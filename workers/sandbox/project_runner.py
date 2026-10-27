"""Runs INSIDE the untrusted, networkless runner, never in the controller/app."""
import json
import os
import platform
import shutil
import sys
import unittest
from pathlib import Path

root=Path('/output/project')
root.mkdir()
files=json.loads(Path('/input/files.json').read_text())
for name,content in files.items():
    target=root/name
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(content)
if Path('/inputs').is_dir() and not (root/'inputs').exists():
    (root/'inputs').symlink_to('/inputs', target_is_directory=True)
os.chdir(root)
sys.path.insert(0,str(root))
try:
    suite=unittest.defaultTestLoader.discover(str(root/'tests'),pattern='test*.py',top_level_dir=str(root))
    result=unittest.TextTestRunner(stream=sys.stderr,verbosity=2).run(suite)
    report={'tests_run':result.testsRun,'passed':result.testsRun-len(result.errors)-len(result.failures)-len(result.skipped),
            'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
            'runtime':platform.python_version(),'exit_code':0 if result.wasSuccessful() and result.testsRun>len(result.skipped) else 1}
except Exception as exc:
    report={'tests_run':0,'passed':0,'failures':0,'errors':1,'runtime':platform.python_version(),'error':type(exc).__name__,'exit_code':1}
Path('/output/test-report.json').write_text(json.dumps(report))
if report['exit_code']==0:
    total=0
    for output in sorted(root.iterdir()):
        if output.name in files or output.name=='test-report.json' or output.name.startswith('.') or output.is_symlink() or not output.is_file():
            continue
        size=output.stat().st_size
        total+=size
        if size>5_000_000 or total>10_000_000:continue
        shutil.copyfile(output,Path('/output')/output.name)
raise SystemExit(report['exit_code'])
