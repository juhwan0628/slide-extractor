"""Qt-free automatic extraction using the same preparation and exporters as GUI."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .analysis import analyze_project
from .config import AnalysisSettings
from .export import OutputCollision, OutputOverwriteRequired, snapshot_pdf, snapshot_export
from .media import probe_source
from .models import CancelToken
from .preparation import prepare_samples
from .source_preparation import prepare_first_frame
from .pdf_only_export import export_pdf_only
from .keyframe_export import export_project
from .roi import build_detection_region


@dataclass(frozen=True)
class PipelineResult:
    pdf_path: Path
    json_path: Path | None
    page_count: int
    representative_pts: tuple[int, ...]
    selection_policy: str
    decoder_backend: str
    elapsed_s: float


class PipelineSettingsError(ValueError):
    """Invalid extraction settings, distinct from decoding/export failures."""


class PipelineCleanupError(RuntimeError):
    def __init__(self, result: PipelineResult):
        super().__init__("Output published, but temporary cache cleanup failed")
        self.result = result


def _preflight(input_path, output_path, json_format, overwrite):
    if output_path.suffix != ".pdf":
        raise ValueError("Output must have a .pdf extension")
    if json_format not in (None, "simple", "detailed"):
        raise ValueError("Invalid JSON format")
    if json_format:
        return snapshot_export(input_path, output_path, overwrite=overwrite)
    if output_path.is_symlink() or (output_path.exists() and not output_path.is_file()):
        raise OutputCollision("Unsafe PDF destination")
    if (input_path.resolve() == output_path.resolve()
            or (input_path.exists() and output_path.exists() and input_path.samefile(output_path))):
        raise OutputCollision("Output must differ from input video")
    if output_path.exists() and not overwrite:
        raise OutputOverwriteRequired("PDF exists; use --overwrite")
    return snapshot_pdf(output_path)


def run_pipeline(input_path: Path, output_path: Path, *,
                 settings: AnalysisSettings | None = None,
                 json_format: str | None = None, overwrite: bool = False,
                 cancel_token: CancelToken | None = None, progress=None) -> PipelineResult:
    started = perf_counter()
    token = cancel_token if cancel_token is not None else CancelToken()
    token.raise_if_cancelled()
    input_path, output_path = Path(input_path).absolute(), Path(output_path).absolute()
    snapshot = _preflight(input_path, output_path, json_format, overwrite)
    settings = settings if settings is not None else AnalysisSettings()
    if not isinstance(settings, AnalysisSettings):
        raise TypeError("AnalysisSettings required")
    source = probe_source(input_path, cancel_token=token)
    if progress:
        progress("roi", None, None)
    if settings.roi_mode == "auto":
        preview = prepare_first_frame(source, cancel_token=token)
        if preview[4] is None:
            settings = replace(settings, roi_mode="manual", effective_roi=preview[3])
    dw, dh = source.transform.display_width, source.transform.display_height
    if len(settings.masks) > 1:
        raise ValueError("At most one exclusion mask is supported")
    try:
        build_detection_region(dw, dh, dw, dh, settings.effective_roi if settings.roi_mode == "manual" else None,
                               settings.masks[0] if settings.masks else None)
    except ValueError as exc:
        raise PipelineSettingsError(str(exc)) from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = TemporaryDirectory(prefix=".slide-cli-", dir=output_path.parent)
    prepared = None
    result = None
    failure = None
    try:
        prepared = prepare_samples(source, Path(workspace.name), cancel_token=token, progress=progress)
        project, _ = analyze_project(source, settings, Path(workspace.name),
                                     prepared=prepared, cancel_token=token, progress=progress)
        kwargs = dict(overwrite=overwrite, cancel_token=token, progress=progress, snapshot=snapshot)
        if json_format:
            export_project(project, output_path, sidecar_format=json_format, **kwargs)
        else:
            export_pdf_only(project, output_path, **kwargs)
        samples = {s.sample_id: s for s in project.samples}
        result = PipelineResult(
            output_path, output_path.with_suffix(".json") if json_format else None,
            len(project.pages),
            tuple(samples[p.representative_sample_id].source_pts for p in project.pages),
            project.selection_policy, project.decoder_backend, perf_counter() - started)
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        errors = []
        for cleanup in ([prepared.discard] if prepared is not None else []) + [workspace.cleanup]:
            try:
                cleanup()
            except Exception as exc:
                errors.append(exc)
        if errors:
            if failure is not None:
                failure.add_note("Cache cleanup failed: " + str(errors[0]))
            elif result is not None:
                raise PipelineCleanupError(result) from errors[0]
            else:
                raise errors[0]
