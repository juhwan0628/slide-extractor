"""Plan collision-free destinations, then process files in input order."""
from pathlib import Path
import unicodedata
from .outcomes import UsageError, result_payload, error_payload


def _aliases(left, right):
    # Be conservative about case aliases for portable output folders.
    return (unicodedata.normalize("NFC", str(left.resolve())).casefold()
            == unicodedata.normalize("NFC", str(right.resolve())).casefold()
            or (left.exists() and right.exists() and left.samefile(right)))


def plan_outputs(inputs: list[Path], output_dir: Path | None, *,
                 json_format: str | None = None) -> list[tuple[Path, Path]]:
    jobs = [(Path(p), (Path(output_dir) if output_dir is not None else Path(p).parent)
             / (Path(p).stem + ".slides.pdf")) for p in inputs]
    targets = []
    for source, pdf in jobs:
        for target in (pdf, pdf.with_suffix(".json")) if json_format else (pdf,):
            if any(_aliases(target, previous) for previous in targets):
                raise UsageError(f"Batch outputs collide: {target}")
            if any(_aliases(target, Path(p)) for p in inputs):
                raise UsageError(f"Batch output would overwrite an input: {target}")
            targets.append(target)
    return jobs


def run_batch(jobs, *, run_one, cancel_token):
    results = []
    for source, output in jobs:
        if cancel_token.cancelled:
            results.append(dict(input=str(source), output=str(output), status="skipped",
                                published=False, error_code="Cancelled", message="Batch cancelled"))
            continue
        try:
            result = run_one(source, output)
            payload = dict(status="success", published=True, **result_payload(result))
        except Exception as exc:
            payload, code = error_payload(exc)
            if code == 130:
                cancel_token.cancel()
        payload.update(input=str(source), output=str(output))
        results.append(payload)
    return results
