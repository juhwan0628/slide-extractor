from __future__ import annotations
import argparse
from dataclasses import asdict
import shutil
import tempfile
import time
from pathlib import Path
import cv2

from .compare import prepare_comparison_image
from .config import DetectorConfig
from .duplicates import SlideImage, deduplicate_slides
from .media import MediaError, extract_frames, probe_video, sample_video
from .models import PipelineResult, ROI
from .pdf import write_slides_pdf
from .roi import crop_to_roi, detect_slide_roi, parse_roi
from .transitions import PreparedSample, build_segments, detect_transitions, select_representative_sample
from .timeline import build_timeline_payload, default_timeline_path, write_timeline_json


def default_output_path(input_path: Path) -> Path:
    return Path.cwd() / "output" / f"{input_path.stem}.slides.pdf"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="slide-extractor", description="Extract slide pages from a local lecture video")
    p.add_argument("input", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--sample-fps", type=float, default=1.0)
    p.add_argument("--roi", type=str, help="manual source-frame ROI: x,y,w,h")
    p.add_argument("--keep-workdir", action="store_true")
    p.add_argument("--export-frames", action="store_true", help="export one representative image for every detected slide segment")
    p.add_argument("--detector-mode", choices=("local", "legacy"), default="local")
    p.add_argument("--dedup-mode", choices=("off", "adjacent", "legacy-global"), default="off")
    p.add_argument("--diagnostics", nargs="?", const=True, type=Path,
                   help="write diagnostic JSON (default: PDF stem + .diagnostics.json)")
    return p


def run_pipeline(input_path: Path, output_path: Path, sample_fps: float, roi_override: ROI | None, keep_workdir: bool, export_frames: bool = False, *,
                 detector_mode: str = "local", dedup_mode: str = "off",
                 diagnostics_path: Path | bool | None = None) -> PipelineResult:
    started = time.perf_counter()
    if input_path.resolve() == output_path.resolve():
        raise ValueError("output path must be different from input video path")
    if not (0.5 <= sample_fps <= 1.0):
        raise ValueError("sample fps must be between 0.5 and 1.0")
    config = DetectorConfig(detector_mode=detector_mode, dedup_mode=dedup_mode)
    if diagnostics_path is True:
        diagnostics_path = output_path.with_suffix(".diagnostics.json")
    destinations = [input_path, output_path, default_timeline_path(output_path)]
    if diagnostics_path:
        destinations.append(Path(diagnostics_path))
    if len({p.resolve() for p in destinations}) != len(destinations):
        raise ValueError("input, PDF, timeline and diagnostics paths must differ")
    meta = probe_video(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=".slide-extractor-", dir=output_path.parent))
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

        dedup = deduplicate_slides(slide_images, config.dedup_mode, config.duplicate_phash_distance)
        unique_slides = dedup.slides
        if not unique_slides:
            raise RuntimeError("fewer than one usable slide could be selected")
        write_slides_pdf(unique_slides, output_path)

        exported_frame_paths = None
        frame_dir = None
        if export_frames:
            frame_dir = output_path.parent / f"{output_path.stem}.frames"
            frame_dir.mkdir(parents=True, exist_ok=True)
            exported_frame_paths = []
            for index, slide_image in enumerate(slide_images, start=1):
                destination = frame_dir / f"slide_{index:04d}.jpg"
                image = cv2.imread(str(slide_image.path), cv2.IMREAD_COLOR)
                if image is None or not cv2.imwrite(str(destination), image, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                    raise RuntimeError(f"cannot export representative frame: {destination}")
                exported_frame_paths.append(Path(frame_dir.name) / destination.name)

        timeline_path = default_timeline_path(output_path)
        timeline_payload = build_timeline_payload(
            source=input_path,
            duration_s=meta.duration_s,
            sample_fps=sample_fps,
            roi=roi,
            segments=segments,
            representative_timestamps=[rep.timestamp_s for rep in representatives],
            frame_paths=exported_frame_paths,
            config=config,
            mappings=dedup.mappings,
        )
        if timeline_payload["pdf_page_count"] != len(unique_slides):
            raise RuntimeError("timeline and PDF page count mismatch")
        write_timeline_json(timeline_payload, timeline_path)
        if diagnostics_path:
            write_timeline_json({
                "schema_version": 1, "settings": asdict(config),
                "accepted_transition_timestamps": diagnostics.accepted_transition_timestamps,
                "samples": [asdict(score) for score in diagnostics.candidate_scores],
                "state_mapping": [asdict(mapping) for mapping in dedup.mappings],
            }, Path(diagnostics_path))

        candidate_count = sum(1 for c in diagnostics.candidate_scores if c.candidate)
        success = True
        return PipelineResult(
            output_path=output_path,
            timeline_path=timeline_path,
            roi=roi,
            final_page_count=len(unique_slides),
            candidate_transition_count=candidate_count,
            accepted_transition_count=len(diagnostics.accepted_transition_timestamps),
            runtime_s=time.perf_counter() - started,
            workdir=workdir if keep_workdir else None,
            frame_dir=frame_dir,
            diagnostics_path=Path(diagnostics_path) if diagnostics_path else None,
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
        result = run_pipeline(args.input, output, args.sample_fps, roi, args.keep_workdir, args.export_frames,
                              detector_mode=args.detector_mode, dedup_mode=args.dedup_mode,
                              diagnostics_path=args.diagnostics)
        print(f"output: {result.output_path}")
        print(f"timeline: {result.timeline_path}")
        print(f"roi: {result.roi.x},{result.roi.y},{result.roi.width},{result.roi.height}")
        print(f"pages: {result.final_page_count}")
        print(f"candidate transitions: {result.candidate_transition_count}")
        print(f"accepted transitions: {result.accepted_transition_count}")
        print(f"runtime: {result.runtime_s:.2f}s")
        if result.diagnostics_path:
            print(f"diagnostics: {result.diagnostics_path}")
        if result.frame_dir:
            print(f"frames: {result.frame_dir}")
        if result.workdir:
            print(f"workdir: {result.workdir}")
        return 0
    except (MediaError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 2
