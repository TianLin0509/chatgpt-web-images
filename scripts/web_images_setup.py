"""Explicit first-run installation; never install dependencies during an MCP call."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def operate(command,args,runtime):
    w=runtime
    if os.name!='nt':
        raise w.ImageError('unsupported_platform','Version 0.2 supports Windows only.')
    if command=='configure':
        values=w.load_settings()
        changed=[]
        for key in ('data_dir','cli_entry','auth_file','output_dir'):
            value=getattr(args,key,'')
            if value:
                path=Path(value).expanduser()
                if not path.is_absolute():
                    raise w.ImageError('invalid_configuration',key+' must be absolute.')
                if key in {'cli_entry','auth_file'} and not path.is_file():
                    raise w.ImageError('invalid_configuration',key+' must be an existing file.')
                values[key]=str(path.resolve())
                changed.append(key)
        if not changed:
            raise w.ImageError('invalid_configuration','Provide --data-dir, --cli-entry, --auth-file or --output-dir.')
        w.write_json(w.CONFIG_FILE,values)
        return {'settings_file':str(w.CONFIG_FILE),'changed':changed,'restart_mcp_required':True}
    node=shutil.which('node.exe')
    npm=shutil.which('npm.cmd')
    if node:
        try:
            version=subprocess.run([node,'--version'],capture_output=True,text=True,timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            if version.returncode or int(version.stdout.strip().lstrip('v').split('.')[0])<18:
                raise ValueError('unsupported version')
        except (ValueError,subprocess.TimeoutExpired) as exc:
            raise w.ImageError('runtime_version_mismatch','Node.js 18 or newer is required.') from exc
    if command=='setup':
        if not node or not npm:
            raise w.ImageError('runtime_missing','Install Node.js 18+ with npm, then run setup again.')
        if not w.CLI_ENTRY.exists():
            expected=w.DATA/'runtime'/'node_modules'/'@playwright'/'cli'/'playwright-cli.js'
            if w.CLI_ENTRY!=expected:
                raise w.ImageError('runtime_missing','Configured CLI entry does not exist; fix configure --cli-entry.')
            install_dir=w.DATA/'runtime'
            install_dir.mkdir(parents=True,exist_ok=True)
            # Node's npm CLI avoids cmd.exe interpolation of paths containing spaces/metacharacters.
            npm_cli=Path(npm).parent/'node_modules'/'npm'/'bin'/'npm-cli.js'
            if not npm_cli.is_file():
                raise w.ImageError('runtime_missing','Cannot locate npm-cli.js beside npm.cmd.')
            try:
                result=subprocess.run([node,str(npm_cli),'install','--prefix',str(install_dir),
                    '--no-audit','--no-fund','--save-exact','@playwright/cli@0.1.19'],
                    capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=240,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            except subprocess.TimeoutExpired as exc:
                raise w.ImageError('setup_timeout','Runtime installation timed out; rerun setup to repair.') from exc
            if result.returncode:
                raise w.ImageError('setup_failed','npm install failed; check npm connectivity. Raw output withheld.')
    cli_version=None
    if w.CLI_ENTRY.is_file():
        cli_version=json.loads((w.CLI_ENTRY.parent/'package.json').read_text(encoding='utf-8')).get('version')
    if cli_version and cli_version!='0.1.19':
        raise w.ImageError('runtime_version_mismatch','Expected Playwright CLI 0.1.19; the configured runtime differs.')
    missing=[]
    if not node:missing.append('Node.js 18+')
    if not w.CLI_ENTRY.is_file():missing.append('Playwright CLI (run setup)')
    if missing:
        raise w.ImageError('runtime_missing','Missing: '+', '.join(missing))
    return {'version':w.VERSION,'platform':'Windows','cli_version':cli_version,
            'data_dir':str(w.DATA),'output_dir':str(w.DEFAULT_OUTPUT),
            'credentials_returned':False,'next_action':'Run open to sign in, then status to verify login.'}
