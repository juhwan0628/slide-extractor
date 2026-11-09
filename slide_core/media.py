from __future__ import annotations
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import queue
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from .config import PROCESS_CANCEL_POLL_S, PROCESS_STDERR_TAIL_BYTES, PROCESS_TERMINATE_GRACE_S, PROCESS_TIMEOUTS
from .models import CancelToken, ProgressRecord
from .display import build_display_pipeline, UnsupportedDisplayTransform
from .tools import executable

class MediaError(RuntimeError):
    def __init__(self, message, *, code=None, tool=None, phase=None, exit_code=None):
        super().__init__(message)
        self.code, self.tool, self.phase, self.exit_code = code, tool, phase, exit_code


@dataclass(frozen=True)
class _ProcessResult:
    stdout: str | bytes
    stderr_tail: str
    returncode: int


def _run_process(argv, *, tool: str, phase: str, timeout: float, cancel_token: CancelToken | None = None,
                 stall_timeout: float | None = None, terminate_grace: float = PROCESS_TERMINATE_GRACE_S,
                 progress=None, clock=time.monotonic, accepted_returncodes=(0,), binary_stdout=False, stdout_line_filter=None):
    """Run argv while draining both pipes and always reaping the child/readers."""
    args = list(map(str, argv))
    if not args or not Path(args[0]).is_absolute():
        try:
            found = executable(args[0]) if args and args[0] in ('ffmpeg','ffprobe','qpdf') else shutil.which(args[0]) if args else None
        except FileNotFoundError:
            found = None
        if found is None:
            raise MediaError(f"MissingTool: tool={tool} phase={phase}", code="MissingTool", tool=tool, phase=phase)
        args[0] = str(Path(found).resolve())
    stdout_file = tempfile.TemporaryFile()
    stderr_tail = deque()
    tail_size = [0]
    readers = []
    last_progress = [clock()]
    progress_values = {b"frame": 0, b"out_time_us": 0, b"out_time_ms": 0}
    progress_queue = queue.Queue(maxsize=1)
    callback_errors = []
    reader_errors = []
    reader_lock = threading.Lock()
    proc = None
    try:
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "shell": False, "bufsize": 0}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(args, **kwargs)
        except FileNotFoundError as exc:
            raise MediaError(f"MissingTool: tool={tool} phase={phase}", code="MissingTool", tool=tool, phase=phase) from exc

        def drain(stream, output, tail=False):
            try:
                pending = b""
                while True:
                    block = stream.read(8192)
                    if not block:
                        break
                    if tail:
                        stderr_tail.append(block)
                        tail_size[0] += len(block)
                        while tail_size[0] > PROCESS_STDERR_TAIL_BYTES:
                            excess = tail_size[0] - PROCESS_STDERR_TAIL_BYTES
                            first = stderr_tail[0]
                            if len(first) <= excess:
                                stderr_tail.popleft()
                                tail_size[0] -= len(first)
                            else:
                                stderr_tail[0] = first[excess:]
                                tail_size[0] -= excess
                    else:
                        if stdout_line_filter is None:output.write(block)
                        pending += block
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if stdout_line_filter is not None:
                                filtered=stdout_line_filter(line)
                                if filtered is not None:output.write(filtered+b"\n")
                            key, sep, value = line.partition(b"=")
                            if sep and key in (b"frame", b"out_time_us", b"out_time_ms"):
                                try:
                                    candidate = int(value.strip())
                                except ValueError:
                                    continue
                                if candidate > progress_values[key]:
                                    progress_values[key] = candidate
                                    last_progress[0] = clock()
                                    if progress is not None and key == b"frame":
                                        try:
                                            progress_queue.put_nowait(candidate)
                                        except queue.Full:
                                            try:
                                                progress_queue.get_nowait()
                                            except queue.Empty:
                                                pass
                                            try:
                                                progress_queue.put_nowait(candidate)
                                            except queue.Full:
                                                pass
                if not tail and stdout_line_filter is not None and pending:
                    filtered=stdout_line_filter(pending)
                    if filtered is not None:output.write(filtered+b"\n")
            except Exception as exc:
                with reader_lock:
                    reader_errors.append(exc)

        readers = [threading.Thread(target=drain, args=(proc.stdout, stdout_file), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, None, True), daemon=True)]
        callback_stop = threading.Event()
        def deliver_progress():
            last_sent = 0.0
            while not callback_stop.is_set() or not progress_queue.empty():
                try:
                    value = progress_queue.get(timeout=PROCESS_CANCEL_POLL_S)
                except queue.Empty:
                    continue
                delay = 0.1 - (time.monotonic() - last_sent)
                if delay > 0:
                    callback_stop.wait(delay)
                try:
                    progress(ProgressRecord(phase, value))
                    last_sent = time.monotonic()
                except Exception as exc:
                    callback_errors.append(exc)
                    return
        callback_thread = threading.Thread(target=deliver_progress, daemon=True) if progress is not None else None
        if callback_thread:
            callback_thread.start()
        for reader in readers:
            reader.start()
        started = clock()
        cancel_seen = False
        timed_out = False
        while proc.poll() is None:
            now = clock()
            if cancel_token is not None and cancel_token.cancelled:
                cancel_seen = True
                break
            if now - started >= timeout or now - last_progress[0] >= (stall_timeout or timeout):
                timed_out = True
                break
            if reader_errors:
                break
            try:
                proc.wait(timeout=PROCESS_CANCEL_POLL_S)
            except subprocess.TimeoutExpired:
                pass
        reader_failed = bool(reader_errors)
        if cancel_seen or timed_out or reader_failed:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                proc.terminate()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=terminate_grace)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    proc.kill()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                proc.wait()
        else:
            proc.wait()
        # The process session/group owns descendants that may keep inherited pipes open.
        if proc.poll() is not None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for reader in readers:
            reader.join(terminate_grace)
        if any(reader.is_alive() for reader in readers):
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for reader in readers:
                reader.join(terminate_grace)
        if any(reader.is_alive() for reader in readers):
            raise MediaError(f"ReaderFailed: tool={tool} phase={phase} exit_code={proc.returncode}", code="ReaderFailed", tool=tool, phase=phase, exit_code=proc.returncode)
        if reader_errors:
            raise MediaError(f"ReaderFailed: tool={tool} phase={phase} exit_code={proc.returncode}: {reader_errors[0]}", code="ReaderFailed", tool=tool, phase=phase, exit_code=proc.returncode)
        callback_stop.set()
        if callback_thread and not (cancel_seen or timed_out or reader_failed):
            callback_thread.join(terminate_grace)
        if callback_errors:
            exc = callback_errors[0]
            raise MediaError(f"ProgressFailed: tool={tool} phase={phase} exit_code={proc.returncode}: {exc}", code="ProgressFailed", tool=tool, phase=phase, exit_code=proc.returncode) from exc
        stderr = b"".join(stderr_tail).decode("utf-8", "replace")
        if cancel_seen:
            raise MediaError(f"Cancelled: tool={tool} phase={phase} exit_code={proc.returncode}; {stderr}", code="Cancelled", tool=tool, phase=phase, exit_code=proc.returncode)
        if timed_out:
            raise MediaError(f"Timeout: tool={tool} phase={phase} exit_code={proc.returncode}; {stderr}", code="Timeout", tool=tool, phase=phase, exit_code=proc.returncode)
        stdout_file.seek(0)
        stdout = stdout_file.read()
        if not binary_stdout:stdout = stdout.decode("utf-8", "replace")
        if proc.returncode not in accepted_returncodes:
            raise MediaError(f"ProcessFailed: tool={tool} phase={phase} exit_code={proc.returncode}; {stderr}", code="ProcessFailed", tool=tool, phase=phase, exit_code=proc.returncode)
        return _ProcessResult(stdout, stderr, proc.returncode)
    finally:
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream:
                stream.close()
        for reader in readers:
            if reader.is_alive():
                reader.join(PROCESS_TERMINATE_GRACE_S)
        stdout_file.close()

# T004 — bounded, non-destructive source identity/probe.
def fingerprint_source(path: Path) -> str:
    """SHA256 of size plus first/middle/last at most 1 MiB blocks; no full video scan."""
    import hashlib
    candidate = Path(path)
    size = candidate.stat().st_size
    if size <= 0:
        raise MediaError("InvalidTiming: empty source", code="InvalidTiming", tool="ffprobe", phase="probe")
    length = min(size, 1024 * 1024)
    offsets = sorted({0, max(0, (size - length) // 2), max(0, size - length)})
    sha = hashlib.sha256()
    sha.update(f"size:{size}\n".encode())
    with candidate.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise MediaError("SourceChanged: short read", code="SourceChanged", tool="ffprobe", phase="probe")
            sha.update(f"{offset}:{len(data)}\n".encode())
            sha.update(data)
    return sha.hexdigest()


def source_matches(source) -> bool:
    """Verify file identity before export, including same-size, same-mtime mutation."""
    try:
        current = Path(source.path).stat()
        if current.st_size != source.size_bytes or current.st_mtime_ns != source.mtime_ns:
            return False
        for k, v in source.metadata:
            if k == "file_id" and v is not None and current.st_ino != v:
                return False
            if k == "device_id" and v is not None and current.st_dev != v:
                return False
        return fingerprint_source(source.path) == source.fingerprint
    except OSError:
        return False


def probe_source(path: Path, *, cancel_token: CancelToken | None = None):
    """Read selected primary video stream, display origin and bounded source identity."""
    from fractions import Fraction
    import math
    from .models import DisplayTransform, SourceRef
    candidate = Path(path).resolve(strict=True)
    before = candidate.stat()
    if not candidate.is_file() or before.st_size == 0:
        raise MediaError("InvalidTiming: source is empty or not a file", code="InvalidTiming", tool="ffprobe", phase="probe")
    before_digest = fingerprint_source(candidate)
    probe = _run_process(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(candidate)],
        tool="ffprobe", phase="probe", timeout=PROCESS_TIMEOUTS["probe"], cancel_token=cancel_token)
    try:
        info = json.loads(probe.stdout)
        streams = [s for s in info["streams"] if s.get("codec_type") == "video"
                   and int(s.get("disposition", {}).get("attached_pic", 0)) != 1]
        if not streams:
            raise ValueError("no non-attached video stream")
        stream = streams[0]
        index = int(stream["index"])
        coded_w = int(stream.get("coded_width") or stream["width"])
        coded_h = int(stream.get("coded_height") or stream["height"])
        if coded_w <= 0 or coded_h <= 0:
            raise ValueError("invalid source dimensions")
        sar_value = stream.get("sample_aspect_ratio", "1:1")
        sar = Fraction(str(sar_value).replace(":", "/")) if sar_value not in ("N/A", "0:1", "0/1") else Fraction(1)
        if sar <= 0:
            raise ValueError("invalid sample aspect ratio")
        rotation = stream.get("tags", {}).get("rotate", 0)
        for side in stream.get("side_data_list", []):
            if "rotation" in side:
                rotation = side["rotation"]
        rotation = int(float(rotation)) % 360
        visible_w = int(stream.get("width") or coded_w)
        visible_h = int(stream.get("height") or coded_h)
        if min(visible_w, visible_h) <= 0:
            raise ValueError("invalid visible dimensions")
        display_w = max(1, round(visible_w * sar))
        display_h = visible_h
        if rotation in (90, 270):
            display_w, display_h = display_h, display_w
        field_order = stream.get("field_order", "progressive")
        pipeline = build_display_pipeline(
            width=visible_w, height=visible_h, sar=sar, rotation=rotation,
            color_matrix=stream.get("color_space"),
            color_range=stream.get("color_range"),
            transfer=stream.get("color_transfer"),
            pixel_format=stream.get("pix_fmt", "yuv420p"),
            interlaced=field_order not in (None, "unknown", "progressive"))
        transform = DisplayTransform(coded_w, coded_h, sar, rotation,
                                     pipeline.transform.display_width,
                                     pipeline.transform.display_height)
        # Stream duration is preferred. Fallback to format is explicitly approximate.
        def valid_duration(value):
            try:
                result = float(value)
                return result if math.isfinite(result) and result > 0 else None
            except (ValueError, TypeError):
                return None
        duration = valid_duration(stream.get("duration"))
        duration_kind = "video_metadata"
        if duration is None:
            duration = valid_duration(info.get("format", {}).get("duration"))
            duration_kind = "container_estimate"
        if duration is None:
            raise ValueError("duration must be finite and positive")
        timebase = Fraction(str(stream["time_base"]))
        if timebase <= 0:
            raise ValueError("invalid stream time base")
        first = _run_process(
            ["ffprobe", "-v", "error", "-select_streams", str(index),
             "-read_intervals", "%+#1", "-show_frames",
             "-show_entries", "frame=best_effort_timestamp,pts,pict_type", "-of", "json", str(candidate)],
            tool="ffprobe", phase="first_frame", timeout=PROCESS_TIMEOUTS["probe"], cancel_token=cancel_token)
        frames = json.loads(first.stdout).get("frames", [])
        pts = next((int(f[k]) for f in frames for k in ("best_effort_timestamp", "pts")
                    if f.get(k) not in (None, "N/A")), None)
        if pts is None:
            raise ValueError("missing first display PTS")
        if duration <= 0:
            raise ValueError("invalid duration")
    except UnsupportedDisplayTransform as exc:
        raise MediaError(f"UnsupportedDisplayTransform: {exc}",
                         code="UnsupportedDisplayTransform", tool="ffprobe", phase="probe") from exc
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        raise MediaError(f"InvalidTiming: {exc}", code="InvalidTiming", tool="ffprobe", phase="probe") from exc
    after = candidate.stat()
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or
        fingerprint_source(candidate) != before_digest):
        raise MediaError("SourceChanged during probe", code="SourceChanged", tool="ffprobe", phase="probe")
    # Keep estimates as metadata, never silently overwrite a valid prior source object.
    meta = tuple(sorted({
        "codec": str(stream.get("codec_name", "unknown")),
        "time_base_num": timebase.numerator, "time_base_den": timebase.denominator,
        "origin_pts": pts, "duration_s": duration, "duration_kind": duration_kind,
        "file_id": getattr(after, "st_ino", None), "device_id": getattr(after, "st_dev", None),
        "first_display_pts": pts,
        "color_matrix": stream.get("color_space"),
        "color_range": stream.get("color_range"),
        "color_transfer": stream.get("color_transfer"),
        "pixel_format": stream.get("pix_fmt", "yuv420p"),
        "display_color_warning": pipeline.color_warning,
        "display_filter": pipeline.full_filter,
        "cache_filter": pipeline.cache_filter,
    }.items()))
    return SourceRef(
        path=candidate, size_bytes=after.st_size, mtime_ns=after.st_mtime_ns,
        stream_index=index, width=coded_w, height=coded_h,
        transform=transform, fingerprint=before_digest, metadata=meta)
