"""Find app-bundled FFmpeg tools before user PATH, without shell invocation."""
from __future__ import annotations
import os,shutil,sys
from pathlib import Path


def executable(name:str):
    if name not in ('ffmpeg','ffprobe','qpdf'):
        raise ValueError('Unknown media tool')
    suffix='.exe' if sys.platform=='win32' else ''
    candidates=[]
    frozen=getattr(sys,'_MEIPASS',None)
    if frozen:
        root=Path(frozen)
        candidates.extend([root/'tools'/(name+suffix),root/(name+suffix)])
    for item in candidates:
        if item.is_file() and not item.is_symlink():
            return str(item.resolve())
    found=shutil.which(name)
    if found:return str(Path(found).resolve())
    raise FileNotFoundError(f'{name} executable unavailable; install FFmpeg and FFprobe')
