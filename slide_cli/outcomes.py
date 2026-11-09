"""Machine-readable results shared by single-file and batch entry points."""
from dataclasses import asdict

class UsageError(ValueError):
    pass


def result_payload(result):
    value = asdict(result)
    value["pdf_path"] = str(result.pdf_path)
    value["json_path"] = str(result.json_path) if result.json_path is not None else None
    value["output_paths"] = [str(p) for p in (result.pdf_path, result.json_path) if p is not None]
    return value


def error_payload(exc):
    from slide_core.models import CancelledError
    from slide_core.pipeline import PipelineCleanupError, PipelineSettingsError
    from slide_core.export import ExportPublishedError
    published = isinstance(exc, (PipelineCleanupError, ExportPublishedError))
    cancelled = isinstance(exc, CancelledError) or getattr(exc, "code", None) == "Cancelled"
    usage = isinstance(exc, (UsageError, PipelineSettingsError))
    code = 130 if cancelled and not published else (2 if usage else 1)
    payload = dict(status="cancelled" if code == 130 else "error", published=published,
                   error_code="UsageError" if usage else (getattr(exc, "code", None) or type(exc).__name__),
                   message=str(exc))
    if isinstance(exc, PipelineCleanupError):
        payload.update(result_payload(exc.result))
    elif isinstance(exc, ExportPublishedError):
        paths = [str(p) for p in exc.outputs]
        payload.update(output_paths=paths, pdf_path=paths[0] if paths else None)
    return payload, code
