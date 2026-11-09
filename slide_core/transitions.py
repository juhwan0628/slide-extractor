"""Streaming PTS-based transition detection with bounded image retention."""
from fractions import Fraction
from uuid import uuid4
from .compare import local_change_metrics, LocalChangeMetrics
from .config import DetectorConfig
from .models import Transition as ModelTransition, DetectedSegment


def _gates(metrics: LocalChangeMetrics, config: DetectorConfig) -> tuple[str, ...]:
    changed = metrics.changed_fraction >= config.candidate_changed_fraction
    triggers: list[str] = []
    if changed and metrics.global_mad >= config.candidate_mad:
        triggers.extend(("global_mad", "changed_fraction"))
    if changed and metrics.max_tile_mad >= config.candidate_tile_mad:
        triggers.extend(("max_tile_mad", "changed_fraction"))
    return tuple(dict.fromkeys(triggers))


def streaming_detect(samples, gray_frames, *, duration, valid_mask=None, config=None,cancel_token=None,progress=None,profile=None):
    """Consume matching sample/image iterators, retaining at most three gray frames.

    Returns immutable transition/segment records and each segment's representative.
    The source samples must be supplied as immutable metadata and are not copied
    into PreparedSample arrays. Every sample belongs to exactly one segment.
    """
    cfg=config or DetectorConfig()
    if cfg.algorithm=='spatial-groups-v1':
        from .spatial_groups import spatial_detect
        return spatial_detect(samples,gray_frames,duration=duration,valid_mask=valid_mask,
                              config=cfg.spatial,cancel_token=cancel_token,progress=progress,profile=profile)
    samples=tuple(samples)
    if not samples:return (),()
    if any(a.actual_time >= b.actual_time for a,b in zip(samples,samples[1:])):
        raise ValueError('Nonmonotonic samples')
    duration=Fraction(duration)
    if duration<=samples[-1].actual_time or duration<=0:
        raise ValueError('Invalid duration')
    imgs=iter(gray_frames)
    first=next(imgs)
    anchor=first
    previous=first
    transitions=[]
    groups=[]
    group_start=0
    suppressed=None
    last_stable=0
    # Single look-ahead: (previous, current, next) plus anchor.
    def similar(a,b):
        return not _gates(local_change_metrics(a,b,tile_size=cfg.tile_size,
            tile_stride=cfg.tile_stride,pixel_delta=cfg.candidate_pixel_delta,
            valid_mask=valid_mask),cfg)
    def significant(a,b):
        return bool(_gates(local_change_metrics(a,b,tile_size=cfg.tile_size,
            tile_stride=cfg.tile_stride,pixel_delta=cfg.candidate_pixel_delta,
            valid_mask=valid_mask),cfg))
    def commit(i,reason,persistence):
        nonlocal anchor,group_start,last_stable
        left=samples[i-1];right=samples[i]
        adj=local_change_metrics(previous,current,tile_size=cfg.tile_size,
            tile_stride=cfg.tile_stride,pixel_delta=cfg.candidate_pixel_delta,valid_mask=valid_mask)
        anc=local_change_metrics(anchor,current,tile_size=cfg.tile_size,
            tile_stride=cfg.tile_stride,pixel_delta=cfg.candidate_pixel_delta,valid_mask=valid_mask)
        metrics=(('global_mad',anc.global_mad),('max_tile_mad',anc.max_tile_mad),
                 ('changed_fraction',anc.changed_fraction),
                 ('adjacent_global_mad',adj.global_mad),
                 ('adjacent_max_tile_mad',adj.max_tile_mad),
                 ('adjacent_changed_fraction',adj.changed_fraction))
        transitions.append(ModelTransition(str(uuid4()),left.sample_id,right.sample_id,
                       left.actual_time,right.actual_time,right.actual_time,reason,persistence,metrics))
        groups.append((group_start,i, last_stable if last_stable>=group_start else group_start))
        group_start=i
        last_stable=i
    next_img=next(imgs,None)
    i=1
    while next_img is not None:
        current=next_img
        future=next(imgs,None)
        # A pending future must be within a bounded sampling distance.
        if significant(anchor,current):
            if future is None:
                # A gap/unstable observation is not enough to confirm at EOF.
                # With a new future observation, however, reconsider persistence.
                if cfg.keep_unconfirmed and (suppressed is None or not similar(suppressed,current)):
                    commit(i,'eof_unconfirmed',1)
                    anchor=current
            else:
                delta=samples[i].actual_time-samples[i-1].actual_time
                future_delta=samples[i+1].actual_time-samples[i].actual_time
                if (future_delta<=Fraction(3,2)*delta and
                        significant(anchor,future) and similar(current,future)):
                    commit(i,'confirmed',2)
                    anchor=current
                    suppressed=None
                else:
                    suppressed=current
        elif similar(previous,current):
            last_stable=i
            suppressed=None
        previous=current
        next_img=future
        i+=1
    if i != len(samples) or next(imgs,None) is not None:
        raise ValueError('Sample/image cardinality mismatch')
    # Unstable singletons retain the initial representative of their segment.
    groups.append((group_start,len(samples),last_stable if last_stable>=group_start else group_start))
    segments=[]
    for gi,(begin,end,rep) in enumerate(groups):
        members=samples[begin:end]
        if not members:raise ValueError('Empty segment')
        seg_start=Fraction(0) if gi==0 else samples[begin].actual_time
        seg_end=duration if end==len(samples) else samples[end].actual_time
        segments.append(DetectedSegment(str(uuid4()),seg_start,seg_end,
                 members[0].sample_id,members[-1].sample_id,samples[rep].sample_id,
                 None if gi==0 else transitions[gi-1].transition_id))
    return tuple(transitions),tuple(segments)
