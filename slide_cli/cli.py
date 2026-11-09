"""Single-video and sequential batch command line interface. Media and Qt imports stay out of --help."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import signal
import sys
from threading import current_thread, main_thread
from time import monotonic


from .outcomes import UsageError, result_payload as _result, error_payload


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def rectangle(value):
    from slide_core.models import Rect
    try:
        values = [int(part) for part in value.split(",")]
        return Rect(*values)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("Expected x,y,w,h with nonnegative x,y and positive w,h") from exc


def roi(value):
    return value if value in ("auto", "full") else rectangle(value)


def build_parser() -> argparse.ArgumentParser:
    from slide_core.version import VERSION
    parser = Parser(prog="slide-extractor", description="Extract slide PDF from a local lecture video")
    parser.add_argument("input", type=Path, nargs="+")
    destinations = parser.add_mutually_exclusive_group()
    destinations.add_argument("-o", "--output", type=Path)
    destinations.add_argument("--output-dir", type=Path)
    parser.add_argument("--roi", type=roi, default="auto", metavar="auto|full|x,y,w,h")
    parser.add_argument("--ignore-mask", type=rectangle, metavar="x,y,w,h")
    parser.add_argument("--json", choices=("simple", "detailed"), dest="json_format")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--result-json", action="store_true", help="write machine-readable run summary to stdout")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def _emit(payload, machine):
    if machine:
        print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    elif payload["status"] == "success":
        print(f"PDF: {payload['pdf_path']} ({payload['page_count']} pages)")
        if payload.get("json_path"):
            print(f"JSON: {payload['json_path']}")
    elif payload.get("published"):
        print("Output published: " + ", ".join(payload.get("output_paths", [])))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    machine = "--result-json" in argv
    token = None
    handler_installed = False
    previous_handler = None
    try:
        try:
            args = build_parser().parse_args(argv)
        except SystemExit as exc:
            return int(exc.code)
        machine = args.result_json
        from .batch import plan_outputs, run_batch
        if args.output is not None and len(args.input) != 1:
            raise UsageError("-o/--output requires exactly one input; use --output-dir for a batch")
        if args.output is not None and args.output.suffix != ".pdf":
            raise UsageError("Output must have a .pdf extension")
        jobs = ([(args.input[0], args.output)] if args.output is not None
                else plan_outputs(args.input, args.output_dir, json_format=args.json_format))
        from slide_core.config import AnalysisSettings
        from slide_core.models import CancelToken
        from slide_core.pipeline import run_pipeline
        settings = AnalysisSettings(
            roi_mode=args.roi if isinstance(args.roi, str) else "manual",
            effective_roi=None if isinstance(args.roi, str) else args.roi,
            masks=(args.ignore_mask,) if args.ignore_mask else ())
        token = CancelToken()
        if current_thread() is main_thread():
            previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda signum, frame: token.cancel())
            handler_installed = True
        last = [None, 0.]
        def progress(stage, done=None, total=None):
            now = monotonic()
            if args.quiet or (stage == last[0] and now - last[1] < .25):
                return
            last[:] = [stage, now]
            count = f" {done}/{total}" if done is not None and total is not None else ""
            print(f"{stage}{count}", file=sys.stderr, flush=True)
        def run_one(source, output):
            last[:] = [None, 0.]
            if len(jobs) > 1 and not args.quiet:
                print(f"input: {source}", file=sys.stderr, flush=True)
            with redirect_stdout(sys.stderr):
                return run_pipeline(source, output, settings=settings,
                                    json_format=args.json_format, overwrite=args.overwrite,
                                    cancel_token=token, progress=progress)
        if len(jobs) == 1:
            result = run_one(*jobs[0])
            _emit(dict(status="success", published=True, **_result(result)), machine)
            return 0
        results = run_batch(jobs, run_one=run_one, cancel_token=token)
        failed = sum(r["status"] == "error" for r in results)
        cancelled = token.cancelled
        summary = dict(status="cancelled" if cancelled else ("error" if failed else "success"),
                       succeeded=sum(r["status"] == "success" for r in results),
                       failed=failed, skipped=sum(r["status"] == "skipped" for r in results),
                       results=results)
        if machine:
            print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        else:
            for item in results:
                _emit(item, False)
            print(f"Batch: {summary['succeeded']} succeeded, {failed} failed, {summary['skipped']} skipped")
        for item in results:
            if item["status"] == "error":
                print(f"error: {item['input']}: {item['message']}", file=sys.stderr)
        return 130 if cancelled else (1 if failed else 0)
    except Exception as exc:
        payload, code = error_payload(exc)
        print(f"error: {exc}", file=sys.stderr)
        _emit(payload, machine)
        return code
    finally:
        if handler_installed:
            signal.signal(signal.SIGINT, previous_handler)
