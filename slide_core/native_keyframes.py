"""Native packet-selected IDR adapter with explicit clean compatibility fallback."""
from __future__ import annotations
from collections import deque
from fractions import Fraction
from pathlib import Path
from queue import Queue, Empty, Full
from threading import Event, Thread
import os
import struct
import subprocess
import sys
import time
from .media import MediaError

class NativeUnsupported(MediaError):
    def __init__(self, message):
        super().__init__(message, code='NativeUnsupported')

def _invalid(message, code='DecodeFailed'):
    return MediaError('Native protocol: '+message, code=code)

def parse_frames(read, width, height, time_base, *, selection_policy='all'):
    """Validate little-endian records before allowing a cache to complete."""
    if selection_policy not in ('all','half'):
        raise ValueError('Unknown keyframe selection policy')
    stride=2 if selection_policy=='half' else 1
    if not (0 < width <= 640 and 0 < height <= 16384):
        raise _invalid('invalid expected dimensions')
    def exact(size):
        parts=[]; remaining=size
        while remaining:
            chunk=read(remaining)
            if not chunk:
                raise _invalid('truncated stream')
            if len(chunk)>remaining:
                raise _invalid('oversized read')
            parts.append(chunk);remaining-=len(chunk)
        return b''.join(parts)
    if exact(8) != b'SEKF0001':
        raise _invalid('unknown version')
    count=0; previous=None; demux=0
    header=struct.Struct('<qQiiIII')
    while True:
        kind=exact(1)[0]
        if kind == 3:
            counter=struct.unpack('<Q',exact(8))[0]
            if counter<=demux:
                raise _invalid('nonadvancing demux progress')
            demux=counter
            continue
        if kind == 2:
            total=struct.unpack('<Q',exact(8))[0]
            if total != count or count == 0:
                raise _invalid('invalid completion count')
            if read(1):
                raise _invalid('data after completion')
            return
        if kind != 1:
            raise _invalid('unknown record')
        pts,ordinal,num,den,w,h,length=header.unpack(exact(header.size))
        if ordinal != count*stride or num<=0 or den<=0 or Fraction(num,den)!=time_base:
            raise _invalid('ordinal/time base mismatch','InvalidTiming')
        if pts == -(1<<63) or (previous is not None and pts<=previous):
            raise _invalid('missing/regressing PTS','InvalidTiming')
        if (w,h)!=(width,height) or length!=width*height*3:
            raise _invalid('dimensions/payload mismatch')
        pixels=exact(length)
        previous=pts;count+=1
        yield pts,pixels

def native_command(source, pipe, selection_policy='all'):
    """Resolve explicit, bundled, or source-build helper for packet-selected preparation."""
    helper=os.environ.get('SLIDE_NATIVE_KEYFRAME_HELPER')
    if not helper:
        name='slide-keyframe-decode'+('.exe' if sys.platform=='win32' else '')
        roots=[Path(getattr(sys,'_MEIPASS',''))/'tools'] if getattr(sys,'_MEIPASS',None) else [Path(__file__).resolve().parents[1]/'build'/'native']
        helper=next((str(root/name) for root in roots if (root/name).is_file() and not (root/name).is_symlink()),None)
    if not helper or not Path(helper).is_file():
        raise NativeUnsupported('Native helper unavailable')
    return [str(Path(helper).resolve()),str(source.path),str(source.stream_index),
            pipe.cache_filter,str(pipe.cache_width),str(pipe.cache_height),
            '2' if selection_policy=='half' else '1']

def decode_native(source, pipe, *, cancelled=None, stall_timeout=120, selection_policy='all'):
    if selection_policy not in ('all','half'):
        raise ValueError('Unknown keyframe selection policy')
    if cancelled is not None and cancelled.cancelled:
        raise MediaError('Cancelled',code='Cancelled')
    try:
        proc=subprocess.Popen(native_command(source,pipe,selection_policy),stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,bufsize=0,start_new_session=os.name!='nt',
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0)
    except OSError as exc:
        raise NativeUnsupported('Native helper cannot start') from exc
    queue=Queue(maxsize=8); stop=Event(); eof=object(); errors=deque(maxlen=30)
    def reader():
        try:
            while not stop.is_set():
                data=proc.stdout.read(65536)
                item=data if data else eof
                while not stop.is_set():
                    try:queue.put(item,timeout=.05);break
                    except Full:pass
                if not data:break
        finally:
            pass
    def stderr_reader():
        # Fixed-size reads also bound a malicious/no-newline worker's diagnostics.
        while not stop.is_set():
            chunk=proc.stderr.read(1024)
            if not chunk:break
            errors.append(chunk.decode('utf-8','replace'))
    threads=[Thread(target=reader,daemon=True),Thread(target=stderr_reader,daemon=True)]
    for thread in threads:thread.start()
    pending=bytearray(); ended=False; last=time.monotonic()
    def check():
        if cancelled is not None and cancelled.cancelled:
            raise MediaError('Cancelled',code='Cancelled')
        if time.monotonic()-last >= stall_timeout:
            raise MediaError('Timeout: native decoder made no progress',code='Timeout')
    def wait_exit():
        while proc.poll() is None:
            check()
            try:proc.wait(timeout=.05)
            except subprocess.TimeoutExpired:pass
        if proc.returncode == 20:
            raise NativeUnsupported('Native input unsupported: '+''.join(errors)[-2000:])
        if proc.returncode:
            raise MediaError('Native decoder failed: '+''.join(errors)[-2000:],code='DecodeFailed')
    def read(size):
        nonlocal ended,last
        check()
        while not pending and not ended:
            check()
            try:item=queue.get(timeout=.05)
            except Empty:continue
            if item is eof:
                ended=True;wait_exit()
            else:
                pending.extend(item);last=time.monotonic()
        result=bytes(pending[:size]);del pending[:size]
        return result
    try:
        info=dict(source.metadata)
        tb=Fraction(info['time_base_num'],info['time_base_den'])
        for frame in parse_frames(read,pipe.cache_width,pipe.cache_height,tb,selection_policy=selection_policy):
            yield frame
            # JPEG/cache work and bounded-pipe backpressure are not decoder stalls.
            last=time.monotonic()
        wait_exit()
    finally:
        stop.set()
        from .pts import _kill
        _kill(proc)
        for thread in threads:thread.join(timeout=1)
        proc.stdout.close();proc.stderr.close()
