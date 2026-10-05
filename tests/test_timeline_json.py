import json
from pathlib import Path

from slide_extractor.models import ROI
from slide_extractor.transitions import SlideSegment
import slide_extractor.timeline as timeline


def test_default_timeline_path_replaces_pdf_suffix():
    assert timeline.default_timeline_path(Path('/tmp/lecture.slides.pdf')) == Path('/tmp/lecture.slides.json')


def test_build_timeline_preserves_all_detected_segments_and_events():
    segments = [
        SlideSegment(0.0, 10.0),
        SlideSegment(10.0, 20.0),
        SlideSegment(20.0, None),
    ]
    representative_timestamps = [8.0, 18.0, 29.0]

    payload = timeline.build_timeline_payload(
        source=Path('/videos/lecture.mp4'),
        duration_s=30.0,
        sample_fps=1.0,
        roi=ROI(1, 2, 300, 160),
        segments=segments,
        representative_timestamps=representative_timestamps,
    )

    assert payload['source'] == 'lecture.mp4'
    assert payload['duration_s'] == 30.0
    assert [{k: v for k, v in slide.items() if k in {
        'slide_id', 'start_s', 'end_s', 'representative_timestamp_s'
    }} for slide in payload['slides']] == [
        {'slide_id': 1, 'start_s': 0.0, 'end_s': 10.0, 'representative_timestamp_s': 8.0},
        {'slide_id': 2, 'start_s': 10.0, 'end_s': 20.0, 'representative_timestamp_s': 18.0},
        {'slide_id': 3, 'start_s': 20.0, 'end_s': 30.0, 'representative_timestamp_s': 29.0},
    ]
    assert payload['events'] == [
        {'type': 'slide_change', 'timestamp_s': 10.0, 'from_slide': 1, 'to_slide': 2},
        {'type': 'slide_change', 'timestamp_s': 20.0, 'from_slide': 2, 'to_slide': 3},
    ]


def test_write_timeline_json_writes_utf8_json_atomically(tmp_path: Path):
    path = tmp_path / 'lecture.slides.json'
    payload = {'source': '강의.mp4', 'slides': []}
    timeline.write_timeline_json(payload, path)

    assert json.loads(path.read_text(encoding='utf-8')) == payload


def test_timeline_maps_merged_states_without_losing_events():
    from slide_extractor.duplicates import StatePageMapping
    from slide_extractor.config import DetectorConfig
    payload = timeline.build_timeline_payload(
        source=Path("lecture.mp4"), duration_s=3, sample_fps=1,
        roi=ROI(0, 0, 20, 20),
        segments=[SlideSegment(0, 1), SlideSegment(1, 2), SlideSegment(2, None)],
        representative_timestamps=[0, 1, 2],
        config=DetectorConfig(dedup_mode="adjacent"),
        mappings=[StatePageMapping(1, 1, None, "preserved"),
                  StatePageMapping(2, 1, 1, "adjacent_exact_pixels"),
                  StatePageMapping(3, 2, None, "preserved")],
    )
    assert payload["schema_version"] == 2
    assert payload["detector_settings"]["detector_mode"] == "local"
    assert payload["dedup_mode"] == "adjacent"
    assert payload["pdf_page_count"] == 2
    assert [s["pdf_page"] for s in payload["slides"]] == [1, 1, 2]
    assert [s["duplicate_of"] for s in payload["slides"]] == [None, 1, None]
    assert len(payload["events"]) == 2
