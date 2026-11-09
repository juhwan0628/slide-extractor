"""Spatial slide occurrences with temporary disk staging and bounded image RAM."""
import tempfile,time
from fractions import Fraction
from uuid import uuid4
import numpy as np
from .models import Transition,DetectedSegment
from .media import MediaError
from .representatives import select_representative

def spatial_metrics(previous,current,valid,config):
    changed=(np.abs(current.astype(np.int16)-previous.astype(np.int16))>config.pixel_delta)&valid
    fractions=[];size=config.tile_size
    for y in range(0,valid.shape[0],size):
        for x in range(0,valid.shape[1],size):
            mask=valid[y:y+size,x:x+size]
            if mask.any():fractions.append(changed[y:y+size,x:x+size].sum()/mask.sum())
    return float(changed.sum()/valid.sum()),float(sum(x>=config.cell_changed_fraction for x in fractions)/len(fractions))

def spatial_detect(samples,gray_frames,*,duration,valid_mask,config,cancel_token=None,progress=None,profile=None):
    samples=tuple(samples);images=iter(gray_frames);duration=Fraction(duration)
    if not samples:
        if next(images,None) is not None:raise ValueError("Sample/image cardinality mismatch")
        return (),()
    if any(a.actual_time>=b.actual_time for a,b in zip(samples,samples[1:])):raise ValueError("Nonmonotonic samples")
    if len({s.sample_id for s in samples})!=len(samples):raise ValueError("Duplicate sample IDs")
    if duration<=0 or duration<=samples[-1].actual_time:raise ValueError("Invalid duration")
    valid=np.asarray(valid_mask)
    if valid.dtype!=bool or valid.ndim!=2 or not valid.any():raise ValueError("Invalid detection mask")
    transitions=[];starts=[0];work=0;total=4*len(samples);phase="grouping"
    def check():
        if cancel_token is not None:cancel_token.raise_if_cancelled()
    def advance():
        nonlocal work
        check();work+=1
        if progress and (work%16==0 or work==len(samples) or work==total):progress(phase,min(work,total),total)
    check()
    started=time.perf_counter()
    try:
        with tempfile.TemporaryFile(mode="w+b") as stage:
            previous=None
            for i,frame in enumerate(images):
                check()
                if i>=len(samples):raise ValueError("Sample/image cardinality mismatch")
                if not isinstance(frame,np.ndarray) or frame.dtype!=np.uint8 or frame.shape!=valid.shape:raise ValueError("Invalid grayscale frame")
                if previous is not None:
                    pixels,cells=spatial_metrics(previous,frame,valid,config)
                    if pixels>=config.changed_fraction and cells>=config.cell_fraction:
                        left,right=samples[i-1],samples[i]
                        transitions.append(Transition(str(uuid4()),left.sample_id,right.sample_id,left.actual_time,right.actual_time,right.actual_time,"spatial_change",1,(("changed_fraction",pixels),("changed_cell_fraction",cells))))
                        starts.append(i)
                raw=frame.tobytes()
                if stage.write(raw)!=len(raw):raise OSError("Incomplete detection staging write")
                previous=frame.copy();advance()
            if work!=len(samples):raise ValueError("Sample/image cardinality mismatch")
            stage.flush();frame_bytes=valid.size
            staged=time.perf_counter()
            if profile is not None:
                profile.seconds['gray_staging']=profile.seconds.get('gray_staging',0)+staged-started
                profile.metadata['gray_stage_bytes']=frame_bytes*len(samples)
            def read_frame(i):
                stage.seek(i*frame_bytes);raw=stage.read(frame_bytes)
                if len(raw)!=frame_bytes:raise OSError("Incomplete detection staging read")
                return np.frombuffer(raw,np.uint8).reshape(valid.shape)
            phase="representative_selection"
            segments=[]
            for gi,(start,end) in enumerate(zip(starts,starts[1:]+[len(samples)])):
                rep=select_representative(read_frame,start,end,valid,config,advance)
                segments.append(DetectedSegment(str(uuid4()),Fraction(0) if gi==0 else samples[start].actual_time,duration if end==len(samples) else samples[end].actual_time,samples[start].sample_id,samples[end-1].sample_id,samples[rep].sample_id,None if gi==0 else transitions[gi-1].transition_id))
            check()
            if profile is not None:profile.seconds['representative_selection']=profile.seconds.get('representative_selection',0)+time.perf_counter()-staged
            return tuple(transitions),tuple(segments)
    except OSError as exc:
        raise MediaError("DetectionStorageFailed: "+str(exc),code="DetectionStorageFailed",phase="detection") from exc
