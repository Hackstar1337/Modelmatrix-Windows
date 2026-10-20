"""Start the controller without inheriting PowerShell/CI capture handles."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    command = config.get('command')
    if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
        parser.error('command must be a non-empty string array')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    with open(config['stdout'], 'ab', buffering=0) as stdout, open(config['stderr'], 'ab', buffering=0) as stderr:
        process = subprocess.Popen(command, cwd=config['cwd'], stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, close_fds=True,
                                   creationflags=flags, start_new_session=os.name != 'nt')
    print(json.dumps({'pid': process.pid}, separators=(',', ':')), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
