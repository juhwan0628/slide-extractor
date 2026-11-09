"""Strict, Qt-free validation and JSON encoding for keyframe timeline v4/v5/v6 payloads."""
from __future__ import annotations

from datetime import datetime
from fractions import Fraction
import hashlib
import json
import math
import re
import uuid


class InvalidTimeline(ValueError):
    """A v4 payload violates its structural or cross-reference contract."""


_SAMPLE = re.compile(r"s[0-9]{6,}\Z")
_SEGMENT = re.compile(r"g[0-9]{6,}\Z")
_BOUNDARY = re.compile(r"b[0-9]{6,}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)\Z", re.IGNORECASE)
_FIELDS = {
    "source": {"basename","size_bytes","mtime_ns","chunk_sha256","stream_index","codec","coded_width","coded_height","sar_num","sar_den","rotation_cw","display_width","display_height","time_base_num","time_base_den","origin_pts","time_origin"},
    "sampling": {"algorithm","cache_max_width","jpeg_quality"},
    "detector": {"algorithm","image_max_width","debounce_samples","global_mad","tile_mad","pixel_delta","changed_fraction","tile_size","tile_stride","min_tile_valid_fraction","mask_dilation_px","keep_unconfirmed"},
    "sample": {"sample_id","ordinal","source_pts","timestamp_s","cache_width","cache_height"},
    "metrics": {"global_mad","max_tile_mad","changed_fraction"},
    "boundary": {"boundary_id","left_sample_id","right_sample_id","lower_s","upper_s","observed_at_s","true_boundary_s","uncertainty_kind","status","persistence_samples","adjacent_metrics","anchor_metrics"},
    "segment": {"segment_id","start_s","end_s","first_sample_id","last_sample_id","auto_representative_sample_id","start_boundary_id"},
    "page": {"page_id","pdf_page","representative_sample_id","representative_timestamp_s","origin","origin_segment_id","derived_segment_id","last_edit"},
    "navigation": {"page_id","start_s","end_s","basis","is_slide_boundary"},
    "artifact": {"export_id","project_revision","created_at","generator_version","pdf_basename","pdf_page_count","pdf_sha256"},
}


def _obj(value, fields, name):
    if not isinstance(value, dict) or set(value) != fields:
        raise InvalidTimeline(f"{name}: required fields mismatch")


def _finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidTimeline("numbers must be finite")
    if isinstance(value, dict):
        for x in value.values(): _finite_tree(x)
    elif isinstance(value, list):
        for x in value: _finite_tree(x)


def _rect(rect, width, height):
    _obj(rect, {"x","y","width","height"}, "rect")
    if any(type(rect[k]) is not int for k in rect) or min(rect["width"], rect["height"]) < 1 or min(rect["x"], rect["y"]) < 0 or rect["x"]+rect["width"] > width or rect["y"]+rect["height"] > height:
        raise InvalidTimeline("rectangle outside display bounds")


def _types(value, path=()):
    """Enforce JSON Schema primitive/container types (not Python coercions)."""
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str): raise InvalidTimeline("object keys must be strings")
            _types(v, path + (k,))
    elif isinstance(value, list):
        for v in value: _types(v, path)
    elif isinstance(value, bool) or value is None or isinstance(value, str):
        pass
    elif type(value) not in (int, float):
        raise InvalidTimeline("unsupported JSON value")


_STRING_FIELDS = {"basename","chunk_sha256","codec","time_origin","algorithm","sample_id","boundary_id","left_sample_id","right_sample_id","segment_id","page_id","representative_sample_id","origin","last_edit","basis","export_id","created_at","generator_version","pdf_basename","pdf_sha256"}
_BOOL_FIELDS = {"keep_unconfirmed","is_slide_boundary"}
_INT_FIELDS = {"size_bytes","mtime_ns","stream_index","coded_width","coded_height","sar_num","sar_den","rotation_cw","display_width","display_height","time_base_num","time_base_den","origin_pts","cache_max_width","jpeg_quality","image_max_width","debounce_samples","pixel_delta","tile_size","tile_stride","mask_dilation_px","ordinal","source_pts","cache_width","cache_height","persistence_samples","pdf_page","project_revision","pdf_page_count","x","y","width","height"}
_NUM_FIELDS = {"duration_s","global_mad","tile_mad","changed_fraction","min_tile_valid_fraction","timestamp_s","max_tile_mad","lower_s","upper_s","observed_at_s","start_s","end_s","representative_timestamp_s"}
_NULLABLE_STRING_FIELDS = {"origin_segment_id","start_boundary_id","derived_segment_id"}


def _schema_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _STRING_FIELDS and not isinstance(item, str): raise InvalidTimeline(f"{key} must be a string")
            if key in _NULLABLE_STRING_FIELDS and item is not None and not isinstance(item, str): raise InvalidTimeline(f"{key} must be a string or null")
            if key in _BOOL_FIELDS and type(item) is not bool: raise InvalidTimeline(f"{key} must be boolean")
            if key in _INT_FIELDS and type(item) is not int: raise InvalidTimeline(f"{key} must be integer")
            if key in _NUM_FIELDS and type(item) not in (int, float): raise InvalidTimeline(f"{key} must be a number")
            _schema_fields(item)
    elif isinstance(value, list):
        for item in value: _schema_fields(item)


def _uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        raise InvalidTimeline("invalid UUID")
    uuid.UUID(value)


def _validate_information(roi,masks,source,cache):
    """Use the same projection and validity threshold as runtime analysis."""
    from .models import Rect
    from .roi import detection_geometry
    try:
        detection_geometry(source["display_width"],source["display_height"],*cache,
            Rect(**roi),Rect(**masks[0]) if masks else None)
    except ValueError as exc:raise InvalidTimeline(str(exc)) from exc


def timeline_version(project):
    """Legacy all remains v4; new spatial/experimental half exports carry policy."""
    detector=getattr(project.settings,'detector',None)
    if project.selection_policy=='half' and (project.keyframe_index_complete or getattr(detector,'algorithm',None)=='spatial-groups-v1'):return 7
    return 6 if getattr(detector,'algorithm',None)=='spatial-groups-v1' or project.selection_policy=='half' else 4


def validate(payload, *, pdf_bytes=None, pdf_page_count=None, pdf_export_id=None):
    """Validate and return a v4/v5/v6 payload; optional PDF facts verify artifact identity."""
    try:
        _types(payload); _schema_fields(payload)
        _finite_tree(payload)
        top={"schema_version","source","duration_s","duration_kind","sampling","roi","ignore_masks","detector","samples","detected_boundaries","detected_segments","pages","navigation_ranges","artifact"}
        _obj(payload,top,"payload")
        if type(payload["schema_version"]) is not int or payload["schema_version"] not in (4,5,6,7): raise InvalidTimeline("schema_version must be 4, 5, 6 or 7")
        index_version=payload["schema_version"]==7
        policy_version=payload["schema_version"] in (6,7)
        spatial_version=payload["schema_version"]==5 or (policy_version and isinstance(payload["detector"],dict) and payload["detector"].get("algorithm")=="spatial-groups-v1")
        for key in ("samples","detected_boundaries","detected_segments","pages","navigation_ranges"):
            if not isinstance(payload[key],list): raise InvalidTimeline(f"{key} must be an array")
        s=payload["source"]; _obj(s,_FIELDS["source"],"source")
        if not isinstance(s["basename"],str) or not s["basename"] or "/" in s["basename"] or "\\" in s["basename"]: raise InvalidTimeline("source basename must be a basename")
        if not s["codec"] or "/" in s["codec"] or "\\" in s["codec"]: raise InvalidTimeline("invalid codec")
        if type(s["chunk_sha256"]) is not str or not _HASH.fullmatch(s["chunk_sha256"]): raise InvalidTimeline("invalid source hash")
        for k in ("size_bytes","coded_width","coded_height","sar_num","sar_den","display_width","display_height","time_base_num","time_base_den"):
            if type(s[k]) is not int or s[k] <= 0: raise InvalidTimeline(f"invalid source {k}")
        if type(s["mtime_ns"]) is not int or type(s["stream_index"]) is not int or s["stream_index"]<0 or type(s["origin_pts"]) is not int or s["rotation_cw"] not in (0,90,180,270) or not s["codec"] or s["time_origin"] != "first_display_frame": raise InvalidTimeline("invalid source metadata")
        dw=Fraction(s["coded_width"]*s["sar_num"],s["sar_den"]); dh=Fraction(s["coded_height"])
        if s["rotation_cw"] in (90,270): dw,dh=dh,dw
        if (s["display_width"],s["display_height"]) != (round(dw),round(dh)): raise InvalidTimeline("display dimensions mismatch transform")
        duration=payload["duration_s"]
        if type(duration) not in (float,int) or duration<=0 or payload["duration_kind"] not in ("video_metadata","container_estimate"): raise InvalidTimeline("invalid duration")
        _obj(payload["sampling"],_FIELDS["sampling"]|({'selection_policy'} if policy_version else set())|({'keyframe_index_complete'} if index_version else set()),"sampling"); sm=payload["sampling"]
        expected_sampling={"algorithm":"decoder-keyframes-v1","cache_max_width":640,"jpeg_quality":90}
        if policy_version:
            if sm['selection_policy'] not in ('all','half'):raise InvalidTimeline('invalid selection policy')
            if spatial_version and not index_version and sm['selection_policy']!='all':raise InvalidTimeline('spatial detector requires all keyframes')
            expected_sampling['selection_policy']=sm['selection_policy']
        if index_version:
            if type(sm['keyframe_index_complete']) is not bool:raise InvalidTimeline('Invalid complete keyframe index flag')
            expected_sampling['keyframe_index_complete']=sm['keyframe_index_complete']
        if sm != expected_sampling: raise InvalidTimeline("invalid sampling settings")
        detector=payload["detector"]
        spatial_config=None
        if spatial_version:
            from .spatial_json import validate_detector
            try:spatial_config=validate_detector(detector,sm['selection_policy'] if index_version else 'all')
            except ValueError as exc:raise InvalidTimeline(str(exc)) from exc
        else:
            _obj(payload["detector"],_FIELDS["detector"],"detector")
            detector=payload["detector"]
            fixed={"algorithm":"masked-anchor-tile-v1","image_max_width":320,"debounce_samples":2,
                   "min_tile_valid_fraction":.5,"mask_dilation_px":1}
            if any(detector[k]!=v for k,v in fixed.items()):raise InvalidTimeline("unsupported detector settings")
            if any(not 0<=detector[k]<=1 for k in ("global_mad","tile_mad","changed_fraction")):
                raise InvalidTimeline("invalid detector thresholds")
            if not 0<=detector["pixel_delta"]<=255 or min(detector["tile_size"],detector["tile_stride"])<1:
                raise InvalidTimeline("invalid detector geometry")
        _rect(payload["roi"],s["display_width"],s["display_height"])
        if not isinstance(payload["ignore_masks"],list) or len(payload["ignore_masks"])>1: raise InvalidTimeline("at most one ignore mask")
        for r in payload["ignore_masks"]: _rect(r,s["display_width"],s["display_height"])
        samples=payload["samples"]
        if not samples: raise InvalidTimeline("samples required")
        sample_map={}; prev_pts=None; dims=None; exact_times={}; tb=Fraction(s["time_base_num"],s["time_base_den"])
        for ordinal,x in enumerate(samples):
            _obj(x,_FIELDS["sample"]|({"is_analysis_observation"} if index_version else set()),"sample")
            if index_version and type(x["is_analysis_observation"]) is not bool:raise InvalidTimeline("Invalid analysis observation flag")
            if not isinstance(x["sample_id"],str) or not _SAMPLE.fullmatch(x["sample_id"]) or x["sample_id"] in sample_map: raise InvalidTimeline("invalid or duplicate sample id")
            if any(type(x[k]) is not int for k in ("ordinal","source_pts","cache_width","cache_height")) or x["ordinal"]<0 or not 1<=x["cache_width"]<=640 or x["cache_height"]<1: raise InvalidTimeline("invalid sample fields")
            if x["ordinal"]!=ordinal or x["sample_id"]!=f"s{ordinal:06d}":
                raise InvalidTimeline("sample ordinal/id mismatch")
            elapsed=(x["source_pts"]-s["origin_pts"])*tb
            t=x["timestamp_s"]
            if elapsed<0 or type(t) not in (int,float) or t!=float(elapsed):
                raise InvalidTimeline("sample timestamp must derive from original PTS")
            if prev_pts is not None and x["source_pts"]<=prev_pts:
                raise InvalidTimeline("sample PTS must strictly increase")
            if elapsed>=Fraction(str(duration)):raise InvalidTimeline("sample must precede duration")
            exact_times[x["sample_id"]]=elapsed
            thisdims=(x["cache_width"],x["cache_height"])
            if dims is not None and dims!=thisdims: raise InvalidTimeline("cache dimensions changed")
            dims=thisdims; sample_map[x["sample_id"]]=x; prev_pts=x["source_pts"]
        expected_w=min(640,s["display_width"])
        expected_h=max(1,round(s["display_height"]*expected_w/s["display_width"]))
        if dims != (expected_w,expected_h): raise InvalidTimeline("cache must preserve full display aspect without enlargement")
        _validate_information(payload["roi"],payload["ignore_masks"],s,dims)
        observed_samples=[x for x in samples if not index_version or x['is_analysis_observation']]
        if not observed_samples:raise InvalidTimeline('No analysis observations')
        if index_version and sm['keyframe_index_complete']:
            wanted=samples[::2 if sm['selection_policy']=='half' else 1]
            if observed_samples!=wanted:raise InvalidTimeline('Observation membership disagrees with selection policy')
        if index_version and not sm['keyframe_index_complete'] and observed_samples!=samples:
            raise InvalidTimeline('Incomplete keyframe index cannot contain skipped observations')
        observed_order={x['sample_id']:i for i,x in enumerate(observed_samples)}
        boundaries=payload["detected_boundaries"]; bmap={}; prev=-1
        for bi,b in enumerate(boundaries):
            from .spatial_json import BOUNDARY_FIELDS
            _obj(b,BOUNDARY_FIELDS if spatial_version else _FIELDS["boundary"],"boundary")
            if not isinstance(b["boundary_id"],str) or not _BOUNDARY.fullmatch(b["boundary_id"]) or b["boundary_id"] in bmap: raise InvalidTimeline("invalid boundary id")
            for key in (("spatial_metrics",) if spatial_version else ("adjacent_metrics","anchor_metrics")):
                _obj(b[key],{"changed_fraction","changed_cell_fraction"} if spatial_version else _FIELDS["metrics"],"metrics")
                if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in b[key].values()): raise InvalidTimeline("invalid metric")
            left=sample_map.get(b["left_sample_id"]); right=sample_map.get(b["right_sample_id"])
            if (not left or not right or left["sample_id"] not in observed_order or right["sample_id"] not in observed_order or observed_order[right["sample_id"]]!=observed_order[left["sample_id"]]+1 or
                    b["lower_s"]!=left["timestamp_s"] or b["upper_s"]!=right["timestamp_s"] or
                    b["observed_at_s"]!=right["timestamp_s"] or right["ordinal"]<=prev or
                    b["true_boundary_s"] is not None or b["uncertainty_kind"]!="keyframe_and_detector"):
                raise InvalidTimeline("invalid keyframe boundary reference or interval")
            if b["boundary_id"]!=f"b{bi:06d}":raise InvalidTimeline("boundary ordinal mismatch")
            if spatial_version:
                if b["status"]!="spatial_change" or b["persistence_samples"]!=1 or b["spatial_metrics"]["changed_fraction"]<spatial_config.changed_fraction or b["spatial_metrics"]["changed_cell_fraction"]<spatial_config.cell_fraction:
                    raise InvalidTimeline("invalid spatial boundary evidence")
            else:
                if (b["status"] not in ("confirmed","eof_unconfirmed") or
                        b["persistence_samples"] != (2 if b["status"]=="confirmed" else 1) or
                        (b["status"]=="eof_unconfirmed" and (right is not observed_samples[-1] or not detector["keep_unconfirmed"]))):
                    raise InvalidTimeline("invalid boundary status")
            prev=right["ordinal"]; bmap[b["boundary_id"]]=b
        segments=payload["detected_segments"]
        if len(segments)!=len(boundaries)+1:raise InvalidTimeline("segment/boundary count mismatch")
        segmap={}
        for i,g in enumerate(segments):
            _obj(g,_FIELDS["segment"],"segment")
            if g["segment_id"]!=f"g{i:06d}":raise InvalidTimeline("invalid segment ordinal/id")
            begin=0 if i==0 else observed_order[boundaries[i-1]["right_sample_id"]]
            end=len(observed_samples) if i==len(segments)-1 else observed_order[boundaries[i]["right_sample_id"]]
            start_time=Fraction(0) if i==0 else exact_times[observed_samples[begin]["sample_id"]]
            end_time=Fraction(str(duration)) if end==len(observed_samples) else exact_times[observed_samples[end]["sample_id"]]
            if (end<=begin or end_time<=start_time or g["start_s"]!=float(start_time) or
                    g["end_s"]!=float(end_time) or
                    g["start_boundary_id"]!=(None if i==0 else boundaries[i-1]["boundary_id"])):
                raise InvalidTimeline("invalid segment interval")
            selected=observed_samples[begin:end]
            if (g["first_sample_id"]!=selected[0]["sample_id"] or g["last_sample_id"]!=selected[-1]["sample_id"] or
                    g["auto_representative_sample_id"] not in {x["sample_id"] for x in selected}):
                raise InvalidTimeline("invalid segment sample membership")
            segmap[g["segment_id"]]=(g,begin,end)
        pages=payload["pages"]; nav=payload["navigation_ranges"]
        if not pages or len(nav)!=len(pages): raise InvalidTimeline("pages and navigation required")
        ids=set(); reps=set(); page_ids=[]; previous_page=-1
        for i,p in enumerate(pages):
            _obj(p,_FIELDS["page"],"page"); n=nav[i]; _obj(n,_FIELDS["navigation"],"navigation")
            try: uuid.UUID(p["page_id"])
            except Exception as exc: raise InvalidTimeline("invalid page UUID") from exc
            if p["page_id"] in ids or type(p["pdf_page"]) is not int or p["pdf_page"]!=i+1 or p["representative_sample_id"] in reps: raise InvalidTimeline("duplicate page or representative")
            sample=sample_map.get(p["representative_sample_id"]); group=segmap.get(p["derived_segment_id"])
            if (not sample or not group or p["representative_timestamp_s"]!=sample["timestamp_s"] or
                    not group[0]["start_s"]<=sample["timestamp_s"]<group[0]["end_s"]):
                raise InvalidTimeline("invalid page sample")
            if p["origin"] not in ("auto","manual") or (p["origin"]=="manual") != (p["origin_segment_id"] is None) or (p["origin_segment_id"] is not None and p["origin_segment_id"] not in segmap) or p["last_edit"] not in ("auto","insert","replace") or (p["origin"]=="auto" and p["last_edit"]=="insert") or (p["origin"]=="manual" and p["last_edit"]=="auto"): raise InvalidTimeline("invalid page provenance")
            if sample["ordinal"]<=previous_page:raise InvalidTimeline("page observations must increase")
            previous_page=sample["ordinal"]
            if n["page_id"]!=p["page_id"] or n["basis"]!="representative_order" or n["is_slide_boundary"] is not False: raise InvalidTimeline("navigation mismatch")
            start=0 if i==0 else sample["timestamp_s"]; end=(sample_map[pages[i+1]["representative_sample_id"]]["timestamp_s"] if i+1<len(pages) else duration)
            if n["start_s"]!=start or n["end_s"]!=end: raise InvalidTimeline("invalid navigation range")
            ids.add(p["page_id"]); reps.add(p["representative_sample_id"]); page_ids.append(p["page_id"])
        a=payload["artifact"]; _obj(a,_FIELDS["artifact"],"artifact")
        try:
            _uuid(a["export_id"])
            if not isinstance(a["created_at"],str) or not _RFC3339.fullmatch(a["created_at"]): raise ValueError("not RFC3339")
            stamp=a["created_at"]
            if stamp[-1:] in ("Z","z"):
                stamp=stamp[:-1]+"+00:00"
            else:
                offset=stamp[-6:]
                if int(offset[1:3])>23 or int(offset[4:6])>59: raise ValueError("invalid UTC offset")
            datetime.fromisoformat(stamp.replace("t","T"))
        except Exception as exc: raise InvalidTimeline("invalid artifact UUID or date-time") from exc
        for p in pages: _uuid(p["page_id"])
        if type(a["project_revision"]) is not int or a["project_revision"]<0 or not a["generator_version"] or "/" in a["generator_version"] or "\\" in a["generator_version"] or len(a["pdf_basename"])<5 or "/" in a["pdf_basename"] or "\\" in a["pdf_basename"] or not a["pdf_basename"].endswith(".pdf") or a["pdf_page_count"]!=len(pages) or not _HASH.fullmatch(a["pdf_sha256"]): raise InvalidTimeline("invalid artifact metadata")
        if pdf_page_count is not None and pdf_page_count!=a["pdf_page_count"]: raise InvalidTimeline("PDF page count mismatch")
        if pdf_export_id is not None and pdf_export_id!=a["export_id"]: raise InvalidTimeline("PDF export UUID mismatch")
        if pdf_bytes is not None and hashlib.sha256(pdf_bytes).hexdigest()!=a["pdf_sha256"]: raise InvalidTimeline("PDF hash mismatch")
        return payload
    except (KeyError, TypeError, OverflowError, ZeroDivisionError) as exc:
        raise InvalidTimeline(f"malformed timeline: {exc}") from exc



def dumps(payload):
    validate(payload)
    raw=json.dumps(payload,ensure_ascii=False,allow_nan=False,indent=2)+"\n"
    loads(raw)
    return raw

def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise InvalidTimeline("duplicate JSON field: "+key)
        result[key]=value
    return result

def loads(raw,**pdf_facts):
    try:
        if isinstance(raw,bytes):raw=raw.decode("utf-8")
        value=json.loads(raw,object_pairs_hook=_unique_object,
            parse_constant=lambda x:(_ for _ in ()).throw(InvalidTimeline("invalid number "+x)))
    except (json.JSONDecodeError,UnicodeDecodeError,TypeError,RecursionError) as exc:
        raise InvalidTimeline("invalid JSON: "+str(exc)) from exc
    return validate(value,**pdf_facts)


def payload_keyframes(snap,pdf_path,pdf_sha,export_id):
    """Project immutable observations into schema4; integers remain authoritative."""
    from datetime import timezone
    from pathlib import Path
    from .config import DetectorConfig,KEYFRAME_ALGORITHM,AnalysisSettings
    from .models import ExportSnapshot
    from .version import VERSION
    if not isinstance(snap,ExportSnapshot) or snap.sampling_algorithm!=KEYFRAME_ALGORITHM:
        raise InvalidTimeline("keyframe snapshot required")
    sett=snap.settings
    if not isinstance(sett,AnalysisSettings) or sett.effective_roi is None:
        raise InvalidTimeline("resolved analysis settings required")
    src=snap.source;meta=dict(src.metadata);cfg=sett.detector or DetectorConfig()
    sample_map={s.sample_id:s for s in snap.samples}
    sample_ids={s.sample_id:f"s{s.ordinal:06d}" for s in snap.samples}
    boundaries={t.transition_id:f"b{i:06d}" for i,t in enumerate(snap.transitions)}
    segments={g.segment_id:f"g{i:06d}" for i,g in enumerate(snap.segments)}
    def rect(r):return dict(x=r.x,y=r.y,width=r.width,height=r.height)
    def metric(scores,adjacent=False):
        value=dict(scores);prefix="adjacent_" if adjacent else ""
        return {k:float(value.get(prefix+k,0)) for k in ("global_mad","max_tile_mad","changed_fraction")}
    rows=[]
    for page in snap.pages:
        sample=sample_map[page.representative_sample_id]
        edit={"add":"insert","create":"auto" if page.origin=="auto" else "insert"}.get(page.last_edit_kind,page.last_edit_kind)
        rows.append(dict(page_id=page.page_id,pdf_page=len(rows)+1,
            representative_sample_id=sample_ids[sample.sample_id],
            representative_timestamp_s=float(sample.actual_time),origin=page.origin,
            origin_segment_id=segments.get(page.origin_segment_id),
            derived_segment_id=segments.get(page.derived_segment_id),last_edit=edit))
    duration=float(meta["duration_s"])
    value=dict(schema_version=4,
        source=dict(basename=src.path.name,size_bytes=src.size_bytes,mtime_ns=src.mtime_ns,
            chunk_sha256=src.fingerprint,stream_index=src.stream_index,codec=str(meta["codec"]),
            coded_width=src.width,coded_height=src.height,
            sar_num=src.transform.sample_aspect_ratio.numerator,sar_den=src.transform.sample_aspect_ratio.denominator,
            rotation_cw=src.transform.rotation,display_width=src.transform.display_width,
            display_height=src.transform.display_height,time_base_num=int(meta["time_base_num"]),
            time_base_den=int(meta["time_base_den"]),origin_pts=int(meta["origin_pts"]),
            time_origin="first_display_frame"),
        duration_s=duration,duration_kind=meta["duration_kind"],
        sampling=dict(algorithm=KEYFRAME_ALGORITHM,cache_max_width=640,jpeg_quality=90),
        roi=rect(sett.effective_roi),ignore_masks=[rect(m) for m in sett.masks],
        detector=dict(algorithm="masked-anchor-tile-v1",image_max_width=320,
            debounce_samples=2,global_mad=cfg.candidate_mad,tile_mad=cfg.candidate_tile_mad,
            pixel_delta=cfg.candidate_pixel_delta,changed_fraction=cfg.candidate_changed_fraction,
            tile_size=cfg.tile_size,tile_stride=cfg.tile_stride,min_tile_valid_fraction=.5,
            mask_dilation_px=1,keep_unconfirmed=cfg.keep_unconfirmed),
        samples=[dict(sample_id=sample_ids[s.sample_id],ordinal=s.ordinal,source_pts=s.source_pts,
            timestamp_s=float(s.actual_time),cache_width=s.width,cache_height=s.height) for s in snap.samples],
        detected_boundaries=[dict(boundary_id=boundaries[t.transition_id],
            left_sample_id=sample_ids[t.left_sample_id],right_sample_id=sample_ids[t.right_sample_id],
            lower_s=float(t.lower_time),upper_s=float(t.upper_time),observed_at_s=float(t.observed_time),
            true_boundary_s=None,uncertainty_kind="keyframe_and_detector",status=t.reason,
            persistence_samples=t.persistence_samples,adjacent_metrics=metric(t.scores,True),
            anchor_metrics=metric(t.scores)) for t in snap.transitions],
        detected_segments=[dict(segment_id=segments[g.segment_id],start_s=float(g.start_time),
            end_s=float(g.end_time),first_sample_id=sample_ids[g.first_sample_id],
            last_sample_id=sample_ids[g.last_sample_id],
            auto_representative_sample_id=sample_ids[g.representative_sample_id],
            start_boundary_id=boundaries.get(g.start_boundary_id)) for g in snap.segments],
        pages=rows,
        navigation_ranges=[dict(page_id=p["page_id"],start_s=0 if i==0 else p["representative_timestamp_s"],
            end_s=rows[i+1]["representative_timestamp_s"] if i+1<len(rows) else duration,
            basis="representative_order",is_slide_boundary=False) for i,p in enumerate(rows)],
        artifact=dict(export_id=export_id,project_revision=snap.revision,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            generator_version=VERSION,pdf_basename=Path(pdf_path).name,
            pdf_page_count=len(rows),pdf_sha256=pdf_sha))
    if cfg.algorithm=="spatial-groups-v1":
        from .spatial_json import detector_payload
        value['schema_version']=5
        value['detector']=detector_payload(cfg.spatial)
        for boundary,transition in zip(value['detected_boundaries'],snap.transitions,strict=True):
            scores=dict(transition.scores)
            boundary.pop('adjacent_metrics');boundary.pop('anchor_metrics')
            boundary['spatial_metrics']={k:float(scores[k]) for k in ('changed_fraction','changed_cell_fraction')}
    version=timeline_version(snap)
    if version in (6,7):
        value['schema_version']=version
        value['sampling']['selection_policy']=snap.selection_policy
    if version==7:
        value['sampling']['keyframe_index_complete']=snap.keyframe_index_complete
        observed=set(snap.observation_sample_ids)
        for sample,row in zip(snap.samples,value['samples'],strict=True):row['is_analysis_observation']=sample.sample_id in observed
        if cfg.algorithm=='spatial-groups-v1':value['detector']['input_policy']=snap.selection_policy
    return validate(value)
