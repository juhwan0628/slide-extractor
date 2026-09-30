from __future__ import annotations
import argparse
import shutil
import tempfile
import time
from pathlib import Path
import cv2

from .compare import prepare_comparison_image
from .config import DetectorConfig
from .duplicates import SlideImage, filter_duplicate_slides
from .media import MediaError, extract_frames, probe_video, sample_video
from .models import PipelineResult, ROI
from .pdf import write_slides_pdf
from .roi import crop_to_roi, detect_slide_roi, parse_roi
from .transitions import PreparedSample, build_segments, detect_transitions, select_representative_sample


def default_output_path(input_path: Path) -> Path:
    return Path.cwd() / "output" / f"{input_path.stem}.slides.pdf"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slide-extractor", description="Extract slide pages from a local lecture video")
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--sample-fps", type=float, default=1.0)
    p.add_argument("--roi", type=str, help="manual source-frame ROI: x,y,w,h")
    p.add_argument("--keep-workdir", action="store_true")
    return p


def run_pipeline(input_path: Path, output_path: Path, sample_fps: float, roi_override: ROI | None, keep_workdir: bool) -> PipelineResult:
    started = time.perf_counter()
    if input_path.resolve() == output_path.resolve():
        raise ValueError("output path must be different from input video path")
    if not (0.5 <= sample_fps <= 1.0):
        raise ValueError("sample fps must be between 0.5 and 1.0")
    meta = probe_video(input_path)
    workdir = Path(tempfile.mkdtemp(prefix="slide-extractor-"))
    success = False
    try:
        if roi_override is None:
            roi_probe_dir = workdir / "roi-probe"
            roi_samples = sample_video(input_path, 1.0, roi_probe_dir, max_frames=8)
            if not roi_samples:
                raise RuntimeError("no frames were extracted for ROI detection")
            roi = detect_slide_roi([s.path for s in roi_samples])
        else:
            roi = roi_override

        sampled_dir = workdir / "sampled"
        samples = sample_video(input_path, sample_fps, sampled_dir, roi=roi, max_width=320)
        if not samples:
            raise RuntimeError("no sample frames were extracted")

        prepared: list[PreparedSample] = []
        for sample in samples:
            image = cv2.imread(str(sample.path), cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError(f"cannot read sampled frame: {sample.path}")
            prepared.append(PreparedSample(sample.timestamp_s, prepare_comparison_image(image)))

        config = DetectorConfig()
        diagnostics = detect_transitions(prepared, config)
        segments = build_segments(samples, diagnostics.accepted_transition_timestamps)
        representatives = [
            select_representative_sample(segment, samples, prepared, config)
            for segment in segments
        ]
        if not representatives:
            raise RuntimeError("fewer than one usable slide could be selected")

        selected_dir = workdir / "selected"
        selected_dir.mkdir(parents=True, exist_ok=True)
        source_frames = extract_frames(input_path, [rep.timestamp_s for rep in representatives], selected_dir)
        slide_images: list[SlideImage] = []
        for index, (rep, source_frame) in enumerate(zip(representatives, source_frames)):
            image = cv2.imread(str(source_frame), cv2.IMREAD_COLOR)
            if image is None:
                raise RuntimeError(f"cannot read source frame: {source_frame}")
            cropped = crop_to_roi(image, roi)
            cropped_path = selected_dir / f"slide_{index:04d}.png"
            if not cv2.imwrite(str(cropped_path), cropped):
                raise RuntimeError(f"cannot write cropped slide: {cropped_path}")
            slide_images.append(SlideImage(cropped_path, rep.timestamp_s))

        unique_slides = filter_duplicate_slides(slide_images, config.duplicate_phash_distance)
        if not unique_slides:
            raise RuntimeError("fewer than one usable slide could be selected")
        write_slides_pdf(unique_slides, output_path)

        candidate_count = sum(1 for c in diagnostics.candidate_scores if c.difference >= config.ambiguous_low)
        success = True
        return PipelineResult(
            output_path=output_path,
            roi=roi,
            final_page_count=len(unique_slides),
            candidate_transition_count=candidate_count,
            accepted_transition_count=len(diagnostics.accepted_transition_timestamps),
            runtime_s=time.perf_counter() - started,
            workdir=workdir if keep_workdir else None,
        )
    finally:
        if success and not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        meta = probe_video(args.input)
        roi = parse_roi(args.roi, meta.width, meta.height) if args.roi else None
        output = args.output or default_output_path(args.input)
        result = run_pipeline(args.input, output, args.sample_fps, roi, args.keep_workdir)
        print(f"output: {result.output_path}")
        print(f"roi: {result.roi.x},{result.roi.y},{result.roi.width},{result.roi.height}")
        print(f"pages: {result.final_page_count}")
        print(f"candidate transitions: {result.candidate_transition_count}")
        print(f"accepted transitions: {result.accepted_transition_count}")
        print(f"runtime: {result.runtime_s:.2f}s")
        if result.workdir:
            print(f"workdir: {result.workdir}")
        return 0
    except (MediaError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
