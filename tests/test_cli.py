from pathlib import Path
import slide_extractor.cli as cli


def test_default_output_path_uses_input_stem(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hasattr(cli, "default_output_path")
    assert cli.default_output_path(Path("lecture.mp4")) == tmp_path / "output" / "lecture.slides.pdf"


def test_cli_parser_accepts_required_options():
    assert hasattr(cli, "build_parser")
    parser = cli.build_parser()
    args = parser.parse_args(["lecture.mp4", "--sample-fps", "0.5", "--roi", "1,2,3,4", "--keep-workdir"])
    assert args.sample_fps == 0.5
    assert args.roi == "1,2,3,4"
    assert args.keep_workdir is True


def test_run_pipeline_rejects_output_that_is_input(tmp_path: Path):
    from slide_extractor.cli import run_pipeline
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"not-a-real-video")
    try:
        run_pipeline(video, video, 1.0, None, False)
    except ValueError as exc:
        assert "output" in str(exc).lower()
        assert "input" in str(exc).lower()
    else:
        raise AssertionError("same input/output path must be rejected")


def test_cli_defaults_and_diagnostic_options():
    parser = cli.build_parser()
    args = parser.parse_args(["lecture.mp4"])
    assert (args.detector_mode, args.dedup_mode, args.sample_fps) == ("local", "off", 1.0)
    assert args.diagnostics is None
    assert parser.parse_args(["lecture.mp4", "--diagnostics"]).diagnostics is True
    args = parser.parse_args(["lecture.mp4", "--detector-mode", "legacy",
                              "--dedup-mode", "legacy-global", "--diagnostics", "scores.json"])
    assert args.diagnostics == Path("scores.json")
