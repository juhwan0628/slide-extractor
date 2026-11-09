"""Qt-free M3 coordinator: transactional project adoption after valid results."""
from __future__ import annotations
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from uuid import uuid4
from PIL import Image
import cv2
import numpy as np

from .config import AnalysisSettings,DetectorConfig,KEYFRAME_ALGORITHM
from .media import probe_source,source_matches,MediaError
from .pts import keyframe_stream
from .roi import automatic_roi,build_detection_region
from .models import Project,AnalysisResult,Page,Rect
from .transitions import streaming_detect
from .profiling import AnalysisProfile,measured


def _read_cache(sample):
    path=Path(sample.cache_path)
    if not path.is_file() or path.is_symlink():
        raise MediaError('CacheUnavailable: missing cache image',code='CacheUnavailable')
    try:
        with Image.open(path) as img:
            result=np.asarray(img.convert('RGB'))
    except (OSError,ValueError) as exc:
        raise MediaError('CacheUnavailable: corrupt JPEG',code='CacheUnavailable') from exc
    if result.shape!=(sample.height,sample.width,3):
        raise MediaError('CacheUnavailable: JPEG dimensions changed',code='CacheUnavailable')
    return result


def _prepared(samples,region,profile=None,cancel_token=None):
    cache=region.cache_roi
    for sample in samples:
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        with measured(profile,'cache_prepare'):
            rgb=_read_cache(sample)
            roi=rgb[cache.y:cache.y+cache.height,cache.x:cache.x+cache.width]
            if not roi.size:raise ValueError('Empty ROI')
            gray=cv2.cvtColor(roi,cv2.COLOR_RGB2GRAY)
            shape=region.valid.shape
            prepared=cv2.resize(gray,(shape[1],shape[0]),interpolation=cv2.INTER_AREA)
        yield prepared


def _resolve_roi(samples,settings,source,cancel_token=None):
    if not samples:raise ValueError('No decoded samples')
    cw,ch=samples[0].width,samples[0].height
    dw,dh=source.transform.display_width,source.transform.display_height
    if settings.roi_mode=='auto':
        images=[]
        for sample in samples[:8]:
            if cancel_token is not None:cancel_token.raise_if_cancelled()
            images.append(_read_cache(sample))
        proposal,reason=automatic_roi(images)
        # proposal is in cached coordinates. Convert to full display conservatively.
        from math import floor,ceil
        roi=Rect(floor(proposal.x*dw/cw),floor(proposal.y*dh/ch),
                 min(dw-floor(proposal.x*dw/cw),ceil(proposal.width*dw/cw)),
                 min(dh-floor(proposal.y*dh/ch),ceil(proposal.height*dh/ch)))
    elif settings.roi_mode=='full':
        roi=Rect(0,0,dw,dh)
    else:
        roi=settings.effective_roi
    masks=settings.masks
    if len(masks)>1:raise ValueError('At most one exclusion mask is supported')
    region=build_detection_region(dw,dh,cw,ch,roi,masks[0] if masks else None)
    return roi,region


def effective_detector(settings,previous=None):
    """New analyses use spatial groups; unspecified reanalysis preserves its algorithm."""
    if settings.detector is not None:return settings.detector
    if previous is not None:
        return previous.settings.detector or DetectorConfig()
    return DetectorConfig(algorithm='spatial-groups-v1')


def _settings_key(settings,previous=None):
    """Canonical identity independent of transient GUI settings revision."""
    return (settings.roi_mode,
            settings.effective_roi if settings.roi_mode=='manual' else None,
            tuple(settings.masks),effective_detector(settings,previous))


def _analyze_project(source,settings:AnalysisSettings,cache_root,*,previous:Project|None=None,
                    cancel_token=None,progress=None,profile=None,prepared=None):
    """Return a new validated project without modifying previous on error/cancel.

    Reuses immutable keyframe Samples when source is unchanged. Stale source or
    incomplete sample caches fail closed, never silently reuse previous results.
    """
    if not isinstance(settings,AnalysisSettings):raise TypeError("Keyframe analysis settings required")
    if cancel_token is not None:cancel_token.raise_if_cancelled()
    source=probe_source(Path(source),cancel_token=cancel_token) if not hasattr(source,'fingerprint') else source
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    detector=effective_detector(settings,previous)
    selection_policy=(prepared.selection_policy if prepared is not None else getattr(previous,'selection_policy','all'))
    if previous is not None:previous.validate()
    if prepared is not None and previous is not None and prepared.selection_policy!=previous.selection_policy:
        raise MediaError('SelectionPolicyMismatch',code='SelectionPolicyMismatch')
    decoder_backend=(prepared.decoder_backend if prepared is not None else getattr(previous,'decoder_backend','cpu-keyframes'))
    prepared_cache=prepared
    cache_generation=str(uuid4())
    reused=False
    if previous is not None and previous.source!=source:
        raise MediaError('SourceChanged: project source identity mismatch',code='SourceChanged')
    if previous is not None and previous.source==source and previous.settings is not None:
        if previous.sampling_algorithm!=KEYFRAME_ALGORITHM:raise ValueError('Previous sampling algorithm mismatch')
        if not isinstance(previous.settings,AnalysisSettings):raise ValueError('Previous algorithm/settings mismatch')
        full_index=previous.samples
        ids=set(previous.observation_sample_ids)
        samples=tuple(s for s in full_index if s.sample_id in ids)
        if not samples:raise MediaError('CacheUnavailable',code='CacheUnavailable')
        cache_generation=previous.cache_generation
        reused=True
    else:samples=()
    if prepared is not None:
        if prepared.source!=source:raise MediaError('SourceChanged: prepared source mismatch',code='SourceChanged')
        if prepared.sampling_algorithm!=KEYFRAME_ALGORITHM:
            raise ValueError('Prepared sampling algorithm does not match analysis')
        if not prepared.samples:raise MediaError('CacheUnavailable',code='CacheUnavailable')
        samples=prepared.samples;full_index=prepared.keyframe_index or prepared.samples;cache_generation=prepared.cache_generation;reused=True
    created_cache=None
    cache_pins=[]
    if progress:progress('sampling',None,None)
    try:
        if not reused:
            with measured(profile,'sampling'):
                from .preparation import prepare_samples
                prepared_cache=prepare_samples(source,Path(cache_root),cancel_token=cancel_token,progress=progress,profile=profile)
                samples=prepared_cache.samples;full_index=prepared_cache.keyframe_index
                created_cache=prepared_cache.directory;cache_pins.append(prepared_cache.cache_pin)
                cache_generation=prepared_cache.cache_generation
                selection_policy=prepared_cache.selection_policy;decoder_backend=prepared_cache.decoder_backend
        if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
        if progress:progress('detecting',0,len(samples))
        if profile is not None:
            profile.metadata.update(cache_reused=reused,sample_count=len(samples),
                                    sampling_algorithm=KEYFRAME_ALGORITHM,selection_policy=selection_policy,decoder_backend=decoder_backend,decoder_threads=1 if decoder_backend=='native-idr' else 0,keyframe_count=len(full_index),
                                    width=source.width,height=source.height,duration_s=float(dict(source.metadata)['duration_s']))
        with measured(profile,'roi'):
            roi,region=_resolve_roi(samples,settings,source,cancel_token)
        prepared=_prepared(samples,region,profile,cancel_token)
        if progress and detector.algorithm!='spatial-groups-v1':
            original_prepared=prepared
            def observed():
                for i,image in enumerate(original_prepared):
                    if i%16==0:progress('detecting',i,len(samples))
                    yield image
            prepared=observed()
        metadata=dict(source.metadata)
        duration=Fraction(str(metadata['duration_s']))
        with measured(profile,'detection'):
            transitions,segments=streaming_detect(samples,prepared,duration=duration,
                                                  valid_mask=region.valid,config=detector,cancel_token=cancel_token,
                                                  progress=progress if detector.algorithm=='spatial-groups-v1' else None,profile=profile)
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
        if progress:progress('detecting',len(samples),len(samples));progress('assembling',None,None)
        with measured(profile,'assembly'):
            complete_settings=replace(settings,effective_roi=roi,detector=detector)
            pages=[Page(str(uuid4()),seg.representative_sample_id,'auto',seg.segment_id,
                        seg.segment_id,'auto') for seg in segments]
            project=Project(source,complete_settings,tuple(full_index),transitions,segments,pages,
                            revision=0,cache_generation=cache_generation,
                            analysis_generation=str(uuid4()),selection_policy=selection_policy,decoder_backend=decoder_backend,observation_sample_ids=tuple(s.sample_id for s in samples),keyframe_index_complete=bool(prepared_cache.keyframe_index) if prepared_cache is not None else previous.keyframe_index_complete)
            project._cache_pin = prepared_cache.cache_pin if prepared_cache is not None else (getattr(previous, '_cache_pin', None) if reused else (cache_pins[0] if cache_pins else None))
            result=AnalysisResult(project.samples,project.transitions,project.segments,
                                  project.analysis_generation)
        if profile is not None:profile.metadata['page_count']=len(pages)
        return project,result
    except BaseException:
        # Never discard or alter caches still owned by the previous Project.
        # Only this invocation's newly created, verified owner can be cleaned.
        for pin in cache_pins:
            pin.close()
        if created_cache is not None:
            from .cache import discard_cache
            discard_cache(cache_root, created_cache)
        raise


def requires_reanalysis(project:Project,settings:AnalysisSettings):
    return _settings_key(project.settings,project)!=_settings_key(settings,project)


def analyze_project(source,settings,cache_root,*,previous=None,cancel_token=None,
                    progress=None,profile=None,profile_path=None,prepared=None):
    profile=profile if profile is not None else (AnalysisProfile() if profile_path else None)
    if profile is not None:
        backend=getattr(prepared if prepared is not None else previous,'decoder_backend','cpu-keyframes')
        profile.metadata.update(decoder_backend=backend,decoder_threads=1 if backend=='native-idr' else 0)
    try:
        with measured(profile,'total'):
            outcome=_analyze_project(source,settings,cache_root,previous=previous,
                cancel_token=cancel_token,progress=progress,profile=profile,prepared=prepared)
        if profile is not None:profile.metadata['status']='success'
        return outcome
    except BaseException as exc:
        if profile is not None:
            profile.metadata.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        if profile_path is not None and profile is not None:
            try:
                profile.save(profile_path)
                print('Analysis profile saved:',profile_path,flush=True)
            except OSError as exc:
                print('Could not save analysis profile:',type(exc).__name__,flush=True)
