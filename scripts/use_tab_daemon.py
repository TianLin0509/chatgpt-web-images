"""Point every Hub-bound image lane at the shared tab daemon, or revert to the Hub entry.

Each lane keeps its own tab and identity; only the transport changes. The Hub entry file
and the Hub code are never modified. A lane's previous cli_entry is kept in
settings.json under "hub_cli_entry" so --revert restores it exactly.

    python use_tab_daemon.py --pool <queue directory>            # switch
    python use_tab_daemon.py --pool <queue directory> --revert   # undo
"""
import argparse
import json
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB_ENTRY = re.compile(r'require\((".*?hub-browser-tool\.js")\)\.main\((\{.*\})\);', re.S)


def lanes(pool):
    con = sqlite3.connect('file:%s?mode=ro' % (pool / 'queue.sqlite3'), uri=True)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute('SELECT id, config_dir FROM accounts ORDER BY id')]


def write_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def switch(pool, revert=False):
    report = []
    for lane in lanes(pool):
        cfg_path = Path(lane['config_dir']) / 'settings.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        if revert:
            if cfg.get('hub_cli_entry'):
                cfg['cli_entry'] = cfg.pop('hub_cli_entry')
                write_json(cfg_path, cfg)
                report.append((lane['id'], 'reverted'))
            else:
                report.append((lane['id'], 'unchanged'))
            continue
        hub_entry = cfg.get('hub_cli_entry') or cfg.get('cli_entry', '')
        match = HUB_ENTRY.search(Path(hub_entry).read_text(encoding='utf-8')) if hub_entry.endswith('.cjs') and Path(hub_entry).is_file() else None
        if not match:
            report.append((lane['id'], 'skipped: not a Hub browser entry'))
            continue
        binding = json.loads(match.group(2))
        binding['hubCore'] = str(Path(json.loads(match.group(1))).parent)
        binding['poolRoot'] = str(pool)
        entry = pool / 'entrypoints' / (binding['id'] + '.cjs')
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("'use strict';\nrequire(%s).main(%s);\n" % (
            json.dumps(str(HERE / 'tab_client.cjs')), json.dumps(binding, ensure_ascii=False)), encoding='utf-8')
        cfg['hub_cli_entry'] = hub_entry
        cfg['cli_entry'] = str(entry)
        write_json(cfg_path, cfg)
        report.append((lane['id'], 'tab daemon (%s)' % binding['identity']))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pool', required=True)
    parser.add_argument('--revert', action='store_true')
    args = parser.parse_args()
    for lane_id, state in switch(Path(args.pool).resolve(), args.revert):
        print(lane_id, state)
