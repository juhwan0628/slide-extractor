from __future__ import annotations
import json
import subprocess
from pathlib import Path
import cv2
from .models import VideoMetadata

class MediaError(RuntimeError):
    pass

def _parse_rate(text: str) -> float:
    if "/" in text:
        n, d = text.split("/", 1)
        return float(n) / float(d) if float(d) else 0.0
    return float(text)

def probe_video(path: Path) -> VideoMetadata:
    if not path.exists():
        raise MediaError(f"input file does not exist: {path}")
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height,avg_frame_rate:format=duration", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        data = json.loads(proc.stdout)
        stream = data["streams"][0]
        return VideoMetadata(int(stream["width"]), int(stream["height"]),
                             float(data["format"]["duration"]), _parse_rate(stream["avg_frame_rate"]))
    except Exception as exc:
        raise MediaError(f"ffprobe could not read video: {path}") from exc

def keyframes_support_sampling(path: Path, fps: float) -> bool:
    """Return true when keyframes are dense enough to safely feed the target sample rate."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        return False
    times: list[float] = []
    for line in proc.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                continue
    if len(times) < 2:
        return False
    target_interval = 1.0 / fps
    gaps = [b - a for a, b in zip(times, times[1:]) if b >= a]
    if not gaps:
        return False
    return max(gaps) <= target_interval * 1.25

def _sample_video_by_seek(path: Path, fps: float, output_dir: Path, *, roi=None, max_width: int | None = None, max_frames: int | None = None):
    from .models import SampleFrame
    meta = probe_video(path)
    total = int(meta.duration_s * fps + 0.999999)
    if max_frames is not None:
        total = min(total, max_frames)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise MediaError(f"OpenCV could not open video: {path}")
    samples = []
    try:
        for i in range(total):
            timestamp = i / fps
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            if roi is not None:
                frame = frame[roi.y:roi.y + roi.height, roi.x:roi.x + roi.width]
            if max_width is not None and frame.shape[1] > max_width:
                scale = max_width / frame.shape[1]
                frame = cv2.resize(frame, (max_width, max(1, round(frame.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            out = output_dir / f"frame_{i:06d}.jpg"
            if not cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 90]):
                raise MediaError(f"could not write sampled frame: {out}")
            samples.append(SampleFrame(out, timestamp))
    finally:
        cap.release()
    return samples


def sample_video(path: Path, fps: float, output_dir: Path, *, roi=None, max_width: int | None = None, max_frames: int | None = None):
    from .models import SampleFrame
    if fps <= 0:
        raise ValueError("fps must be positive")
    if max_frames is not None and max_frames <= 0:
        raise ValueError("max_frames must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Lecture recordings commonly carry ~1s keyframes. Seeking directly to
    # requested timestamps avoids decoding every intervening source frame.
    if keyframes_support_sampling(path, fps):
        return _sample_video_by_seek(path, fps, output_dir, roi=roi, max_width=max_width, max_frames=max_frames)

    pattern = output_dir / "frame_%06d.jpg"
    filters = [f"fps={fps}"]
    if roi is not None:
        filters.append(f"crop={roi.width}:{roi.height}:{roi.x}:{roi.y}")
    if max_width is not None:
        filters.append(f"scale={max_width}:-2")
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(path), "-vf", ",".join(filters), "-q:v", "3", "-start_number", "0"]
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [str(pattern)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"FFmpeg frame extraction failed: {path}") from exc
    frames = sorted(output_dir.glob("frame_*.jpg"))
    return [SampleFrame(frame, i / fps) for i, frame in enumerate(frames)]

def extract_frame(path: Path, timestamp_s: float, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{timestamp_s:.3f}", "-i", str(path), "-frames:v", "1", str(output_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"FFmpeg frame extraction failed at {timestamp_s:.3f}s") from exc
    if not output_path.exists():
        raise MediaError(f"FFmpeg produced no frame at {timestamp_s:.3f}s")
    return output_path


def extract_frames(path: Path, timestamps_s, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise MediaError(f"OpenCV could not open video: {path}")
    outputs: list[Path] = []
    try:
        for index, timestamp_s in enumerate(timestamps_s):
            cap.set(cv2.CAP_PROP_POS_MSEC, float(timestamp_s) * 1000.0)
            ok, frame = cap.read()
            if not ok:
                raise MediaError(f"could not extract frame at {float(timestamp_s):.3f}s")
            out = output_dir / f"source_{index:04d}.png"
            if not cv2.imwrite(str(out), frame):
                raise MediaError(f"could not write source frame: {out}")
            outputs.append(out)
    finally:
        cap.release()
    return outputs
