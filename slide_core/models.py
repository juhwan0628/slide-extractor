"""Qt-free records shared by analysis, editing, and export code."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from fractions import Fraction
from pathlib import Path
from threading import Event
from collections.abc import Mapping
from typing import Any
import math
import uuid


KEYFRAME_ALGORITHM = "decoder-keyframes-v1"


def _fraction(value: Fraction | int | float) -> Fraction:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("time must be finite")
        return Fraction(str(value))
    return Fraction(value)


def _freeze(value):
    if value is None or type(value) in (bool, int, str) or isinstance(value, (Fraction, Path)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("settings must be finite")
        return value
    if isinstance(value, Mapping):
        if any(type(k) not in (bool, int, str) for k in value):
            raise ValueError("invalid settings key")
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    if is_dataclass(value) and getattr(type(value), "__dataclass_params__").frozen:
        updates = {item.name: _freeze(getattr(value, item.name)) for item in fields(value) if item.init}
        return replace(value, **updates)
    raise ValueError("settings must use immutable scalar and collection values")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    def __post_init__(self):
        if any(type(v) is not int for v in (self.x, self.y, self.width, self.height)) or self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("invalid rectangle")


@dataclass(frozen=True)
class DisplayTransform:
    coded_width: int
    coded_height: int
    sample_aspect_ratio: Fraction = Fraction(1)
    rotation: int = 0
    display_width: int | None = None
    display_height: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "sample_aspect_ratio", _fraction(self.sample_aspect_ratio))
        dims = (self.coded_width, self.coded_height)
        explicit = (self.display_width, self.display_height)
        if any(type(v) is not int or v <= 0 for v in dims) or any(v is not None and (type(v) is not int or v <= 0) for v in explicit) or self.sample_aspect_ratio <= 0 or self.rotation not in (0, 90, 180, 270):
            raise ValueError("invalid display transform")
        w = self.display_width if self.display_width is not None else (self.coded_width if self.rotation in (0, 180) else self.coded_height)
        h = self.display_height if self.display_height is not None else (self.coded_height if self.rotation in (0, 180) else self.coded_width)
        if w <= 0 or h <= 0:
            raise ValueError("invalid display dimensions")
        object.__setattr__(self, "display_width", w)
        object.__setattr__(self, "display_height", h)


@dataclass(frozen=True)
class SourceRef:
    path: Path
    size_bytes: int
    mtime_ns: int
    stream_index: int
    width: int
    height: int
    transform: DisplayTransform
    fingerprint: str = ""
    metadata: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path).resolve())
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        if (any(type(v) is not int for v in (self.size_bytes, self.mtime_ns, self.stream_index, self.width, self.height))
                or min(self.size_bytes, self.mtime_ns, self.stream_index) < 0
                or min(self.width, self.height) <= 0):
            raise ValueError("invalid source identity")


@dataclass(frozen=True)
class Sample:
    sample_id: str
    ordinal: int
    source_pts: int
    time_base: Fraction
    cache_path: str
    width: int
    height: int
    pts_origin: int = 0

    def __post_init__(self):
        object.__setattr__(self, "time_base", _fraction(self.time_base))
        if (not self.sample_id or type(self.ordinal) is not int or self.ordinal < 0
                or type(self.source_pts) is not int or type(self.pts_origin) is not int
                or self.time_base <= 0 or any(type(v) is not int or v <= 0 for v in (self.width, self.height))
                or self.actual_time < 0):
            raise ValueError("invalid sample")

    @property
    def actual_time(self) -> Fraction:
        return (self.source_pts - self.pts_origin) * self.time_base


@dataclass(frozen=True)
class Transition:
    transition_id: str
    left_sample_id: str
    right_sample_id: str
    lower_time: Fraction
    upper_time: Fraction
    observed_time: Fraction
    reason: str
    persistence_samples: int
    scores: tuple[tuple[str, float], ...] = ()

    def __post_init__(self):
        for name in ("lower_time", "upper_time", "observed_time"):
            object.__setattr__(self, name, _fraction(getattr(self, name)))
        object.__setattr__(self, "scores", tuple((k, float(v)) for k, v in self.scores))
        if (self.reason not in ("confirmed", "eof_unconfirmed", "spatial_change") or self.lower_time < 0
                or not self.lower_time <= self.observed_time <= self.upper_time
                or type(self.persistence_samples) is not int or self.persistence_samples < 0
                or (self.reason=="spatial_change" and self.persistence_samples!=1)
                or any(not math.isfinite(v) for _, v in self.scores)):
            raise ValueError("invalid transition")


@dataclass(frozen=True)
class DetectedSegment:
    segment_id: str
    start_time: Fraction
    end_time: Fraction
    first_sample_id: str
    last_sample_id: str
    representative_sample_id: str
    start_boundary_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "start_time", _fraction(self.start_time))
        object.__setattr__(self, "end_time", _fraction(self.end_time))
        if self.start_time < 0 or self.end_time <= self.start_time:
            raise ValueError("invalid segment interval")


@dataclass(frozen=True)
class Page:
    page_id: str
    representative_sample_id: str
    origin: str
    origin_segment_id: str | None
    derived_segment_id: str | None
    last_edit_kind: str

    def __post_init__(self):
        try:
            uuid.UUID(self.page_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("page_id must be a UUID") from exc
        if self.origin not in ("auto", "manual"):
            raise ValueError("invalid page origin")


@dataclass(frozen=True)
class SlideImage:
    path: Path
    timestamp_s: float

    @property
    def timestamp(self) -> Fraction:
        return _fraction(self.timestamp_s)


def _validate_analysis_records(samples, transitions=(), segments=()):
    for records, label, key in ((samples, "sample", "sample_id"),
                                (transitions, "transition", "transition_id"),
                                (segments, "segment", "segment_id")):
        ids = [getattr(x, key) for x in records]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate {label} id")
    if any(s.ordinal!=i for i,s in enumerate(samples)):
        raise ValueError("sample ordinals must be contiguous observation order")
    if samples and any(s.time_base!=samples[0].time_base or s.pts_origin!=samples[0].pts_origin for s in samples):
        raise ValueError("sample clocks must use one time base and origin")
    sample_times = {s.sample_id: s.actual_time for s in samples}
    if any(a.actual_time >= b.actual_time for a, b in zip(samples, samples[1:])):
        raise ValueError("sample times must strictly increase")
    for transition in transitions:
        if transition.left_sample_id not in sample_times or transition.right_sample_id not in sample_times:
            raise ValueError("unknown transition sample reference")
        left = sample_times[transition.left_sample_id]
        right = sample_times[transition.right_sample_id]
        # Bounds describe observations, not an interpolated transition time.
        if (left >= right or transition.lower_time != left
                or transition.upper_time != right or transition.observed_time != right):
            raise ValueError("transition times must match referenced sample times")
    if any(a.lower_time > b.lower_time or a.observed_time > b.observed_time or a.upper_time > b.upper_time
           for a, b in zip(transitions, transitions[1:])):
        raise ValueError("transitions must be sorted by time")
    transition_ids = {t.transition_id for t in transitions}
    for segment in segments:
        if not {segment.first_sample_id, segment.last_sample_id, segment.representative_sample_id} <= sample_times.keys():
            raise ValueError("unknown segment sample reference")
        if segment.start_boundary_id is not None and segment.start_boundary_id not in transition_ids:
            raise ValueError("unknown segment boundary reference")
        if not (sample_times[segment.first_sample_id]
                <= sample_times[segment.representative_sample_id]
                <= sample_times[segment.last_sample_id]):
            raise ValueError("segment sample times must be ordered")
    if any(a.start_time > b.start_time for a, b in zip(segments, segments[1:])):
        raise ValueError("segments must be sorted by time")
    return sample_times


def _validate_source_samples(source,samples):
    if not samples:return
    meta=dict(source.metadata)
    if {"time_base_num","time_base_den","origin_pts"}<=meta.keys():
        tb=Fraction(meta["time_base_num"],meta["time_base_den"])
        if samples[0].time_base!=tb or samples[0].pts_origin!=meta["origin_pts"]:
            raise ValueError("sample clock does not match source")
    if "duration_s" in meta and samples[-1].actual_time>=_fraction(meta["duration_s"]):
        raise ValueError("sample must precede source duration")


def validate_preparation_provenance(selection_policy,decoder_backend):
    if selection_policy not in ('all','half') or decoder_backend not in ('cpu-keyframes','native-idr'):
        raise ValueError('Invalid preparation provenance')
    if selection_policy=='half' and decoder_backend!='native-idr':
        raise ValueError('Half preparation requires native IDR backend')


@dataclass
class Project:
    source: SourceRef
    settings: Any = None
    samples: tuple[Sample, ...] = ()
    transitions: tuple[Transition, ...] = ()
    segments: tuple[DetectedSegment, ...] = ()
    pages: list[Page] = field(default_factory=list)
    revision: int = 0
    cache_generation: str = field(default_factory=lambda: str(uuid.uuid4()))
    analysis_generation: str = field(default_factory=lambda: str(uuid.uuid4()))
    sampling_algorithm: str = KEYFRAME_ALGORITHM
    selection_policy: str = "all"
    decoder_backend: str = "cpu-keyframes"
    observation_sample_ids: tuple[str,...] | None = None
    keyframe_index_complete: bool | None = None

    def __post_init__(self):
        self.samples = tuple(self.samples)
        self.transitions = tuple(self.transitions)
        self.segments = tuple(self.segments)
        self.pages = list(self.pages)
        self.validate()

    def validate(self):
        if self.observation_sample_ids is None:self.observation_sample_ids=tuple(s.sample_id for s in self.samples)
        else:self.observation_sample_ids=tuple(self.observation_sample_ids)
        if self.keyframe_index_complete is None:self.keyframe_index_complete=self.selection_policy=='all'
        if type(self.keyframe_index_complete) is not bool:raise ValueError('Invalid complete index flag')
        sample_ids=tuple(s.sample_id for s in self.samples)
        observations=self.observation_sample_ids;observed_set=set(observations)
        if len(observed_set)!=len(observations) or tuple(x for x in sample_ids if x in observed_set)!=observations:
            raise ValueError('Invalid analysis observation membership')
        if self.samples and not observations:raise ValueError('Missing analysis observations')
        if self.keyframe_index_complete and observations!=sample_ids[::2 if self.selection_policy=='half' else 1]:
            raise ValueError('Observation selection does not match policy')
        if any(t.left_sample_id not in observed_set or t.right_sample_id not in observed_set for t in self.transitions):raise ValueError('Boundary uses unobserved frame')
        if any(not {g.first_sample_id,g.last_sample_id,g.representative_sample_id}<=observed_set for g in self.segments):raise ValueError('Segment uses unobserved frame')
        validate_preparation_provenance(self.selection_policy,self.decoder_backend)
        if self.sampling_algorithm!=KEYFRAME_ALGORITHM:raise ValueError("Unsupported sampling algorithm")
        _validate_source_samples(self.source,self.samples)
        sample_times = _validate_analysis_records(self.samples, self.transitions, self.segments)
        page_ids = [p.page_id for p in self.pages]
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("duplicate page id")
        for page in self.pages:
            if page.representative_sample_id not in sample_times:
                raise ValueError("unknown page sample reference")
            if page.origin_segment_id is not None and page.origin_segment_id not in {s.segment_id for s in self.segments}:
                raise ValueError("unknown page origin segment reference")
            if page.derived_segment_id is not None and page.derived_segment_id not in {s.segment_id for s in self.segments}:
                raise ValueError("unknown page derived segment reference")
        if len({p.representative_sample_id for p in self.pages}) != len(self.pages):
            raise ValueError("sample cannot represent multiple pages")
        if any(sample_times[a.representative_sample_id] >= sample_times[b.representative_sample_id]
               for a, b in zip(self.pages, self.pages[1:])):
            raise ValueError("pages must be sorted by sample time")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        self._sample_times = sample_times
        self._indexed_samples = self.samples

    @property
    def keyframe_index(self):
        """Complete edit index, including uncached keys skipped by half analysis."""
        if not self.keyframe_index_complete:raise ValueError('FullKeyframesRequired')
        return self.samples

    @property
    def _selection_policy(self):
        # Compatibility with detector-integration diagnostics.
        return self.selection_policy

    def sample_time(self, sample_id: str) -> Fraction:
        if self._indexed_samples is not self.samples or not isinstance(self.samples, tuple):
            self._sample_times = {s.sample_id: s.actual_time for s in self.samples}
            self._indexed_samples = self.samples
        try:
            return self._sample_times[sample_id]
        except KeyError:
            raise StopIteration from None


@dataclass(frozen=True)
class ExportSnapshot:
    source: SourceRef
    settings: Any
    samples: tuple[Sample, ...]
    transitions: tuple[Transition, ...]
    segments: tuple[DetectedSegment, ...]
    pages: tuple[Page, ...]
    revision: int
    export_id: str
    cache_generation: str
    analysis_generation: str
    sampling_algorithm: str = KEYFRAME_ALGORITHM
    selection_policy: str = "all"
    decoder_backend: str = "cpu-keyframes"
    observation_sample_ids: tuple[str,...] | None = None
    keyframe_index_complete: bool | None = None

    def __post_init__(self):
        object.__setattr__(self, "settings", _freeze(self.settings))
        for name in ("samples", "transitions", "segments", "pages"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        candidate=Project(self.source, self.settings, self.samples, self.transitions, self.segments,
                self.pages, self.revision, self.cache_generation, self.analysis_generation,self.sampling_algorithm,self.selection_policy,self.decoder_backend,self.observation_sample_ids,self.keyframe_index_complete)
        object.__setattr__(self,'observation_sample_ids',candidate.observation_sample_ids)
        object.__setattr__(self,'keyframe_index_complete',candidate.keyframe_index_complete)


def make_export_snapshot(project: Project) -> ExportSnapshot:
    project.validate()
    return ExportSnapshot(project.source, _freeze(project.settings), tuple(project.samples),
                          tuple(project.transitions), tuple(project.segments),
                          tuple(project.pages), project.revision, str(uuid.uuid4()),
                          project.cache_generation, project.analysis_generation,project.sampling_algorithm,
                          project.selection_policy,project.decoder_backend,project.observation_sample_ids,project.keyframe_index_complete)


@dataclass(frozen=True)
class AnalysisRequest:
    source: SourceRef
    settings: Any
    samples: tuple[Sample, ...] = ()
    cache_generation: str = ""

    def __post_init__(self):
        object.__setattr__(self, "settings", _freeze(self.settings))
        object.__setattr__(self, "samples", tuple(self.samples))
        _validate_analysis_records(self.samples)
        _validate_source_samples(self.source,self.samples)


@dataclass(frozen=True)
class AnalysisResult:
    samples: tuple[Sample, ...]
    transitions: tuple[Transition, ...]
    segments: tuple[DetectedSegment, ...]
    analysis_generation: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self):
        for name in ("samples", "transitions", "segments"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        _validate_analysis_records(self.samples, self.transitions, self.segments)


@dataclass(frozen=True)
class ExportResult:
    export_id: str
    paths: tuple[Path, Path]


@dataclass(frozen=True)
class EditResult:
    status: str
    focus_page_id: str | None = None
    affected_rows: tuple[int, ...] = ()
    code: str = ""


@dataclass(frozen=True)
class ToolPaths:
    ffmpeg: Path
    ffprobe: Path


@dataclass(frozen=True)
class ProgressRecord:
    stage: str
    completed: int = 0
    total: int | None = None
    message: str = ""


class CancelToken:
    def __init__(self):
        self._event = Event()
        self._committed = Event()

    def mark_committed(self):
        """Record a successfully published export; later cancel cannot undo it."""
        self._committed.set()

    @property
    def committed(self) -> bool:
        return self._committed.is_set()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self):
        if self.cancelled:
            raise CancelledError("operation cancelled")


class StructuredError(Exception):
    def __init__(self, code: str, message: str, details: tuple[tuple[str, str], ...] = ()):
        super().__init__(message)
        self.code, self.message, self.details = code, message, tuple(details)


class CancelledError(StructuredError):
    def __init__(self, message="operation cancelled"):
        super().__init__("cancelled", message)


@dataclass(frozen=True)
class JobResult:
    job_id: str
    generation: str
    outcome: str
    result: Any = None
    error: StructuredError | None = None

    def __post_init__(self):
        if self.outcome not in ("success", "cancel", "error") or (self.outcome == "error" and self.error is None):
            raise ValueError("invalid job result")
