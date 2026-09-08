"""Check the exact tracked publication set without printing matched secret values."""
import json
from pathlib import Path
import re
import subprocess

root=Path(__file__).resolve().parents[1]
proc=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,check=True)
files=[Path(p.decode('utf-8')) for p in proc.stdout.split(b'\0') if p]
assert files,'No tracked files; stage the reviewed source before running this check.'
problems=[]
blocked={'node_modules','profile','runtime','jobs','requests','output','artifacts','.venv','transient-scripts','__pycache__'}
for relative in files:
    if blocked.intersection(relative.parts) or relative.name in {'auth-state.json','active.json','settings.json','.env'}:
        problems.append([str(relative),'private/runtime file'])
        continue
    data=(root/relative).read_bytes()
    try:text=data.decode('utf-8')
    except UnicodeDecodeError:
        problems.append([str(relative),'non-text tracked file; explicit review required'])
        continue
    normalized=text.replace('\\\\','\\')
    if re.search(r'[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_]',normalized):
        problems.append([str(relative),'machine-specific home path'])
    patterns=[r'gh[pousr]_[A-Za-z0-9]{20,}',r'github_pat_[A-Za-z0-9_]{30,}',
              r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
              r'"cookies"\s*:\s*\[\s*\{',r'https://chatgpt\.com/c/[a-f0-9-]{30,}']
    if any(re.search(pattern,text) for pattern in patterns):
        problems.append([str(relative),'potential secret or private conversation'])
    if re.search(r'data:image/\w+;base64,[A-Za-z0-9+/]{100,}',text):
        problems.append([str(relative),'embedded image'])
print(json.dumps({'checked_files':len(files),'problems':problems},ensure_ascii=False))
raise SystemExit(bool(problems))
