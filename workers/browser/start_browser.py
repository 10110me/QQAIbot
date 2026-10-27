"""Start the virtual desktop after removing only orphaned display-99 locks."""
import os
from pathlib import Path


def clean_display_lock(proc=Path('/proc'), tmp=Path('/tmp')):
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            args=(directory/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        if args and Path(os.fsdecode(args[0])).name in {'Xvfb','Xorg'} and b':99' in args:
            return
    for path in (tmp/'.X99-lock',tmp/'.X11-unix'/'X99'):
        path.unlink(missing_ok=True)


if __name__=='__main__':
    clean_display_lock()
    os.execvp('supervisord',['supervisord','-c','/app/supervisord.conf'])
