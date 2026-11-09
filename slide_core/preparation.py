"""Settings-independent, owned JPEG samples prepared before analysis."""
from dataclasses import dataclass,field,replace
from pathlib import Path
from uuid import uuid4
from .media import probe_source,source_matches,MediaError
from .pts import keyframe_stream
from .config import KEYFRAME_ALGORITHM
from .cache import discard_cache,CacheCleanupError

@dataclass(frozen=True)
class PreparedSamples:
    source:object
    samples:tuple
    directory:Path
    cache_root:Path
    cache_generation:str
    cache_pin:object=field(repr=False,compare=False)

    sampling_algorithm:str=KEYFRAME_ALGORITHM
    selection_policy:str="all"
    decoder_backend:str="cpu-keyframes"

    keyframe_index:tuple=()

    def __post_init__(self):
        from .models import validate_preparation_provenance
        validate_preparation_provenance(self.selection_policy,self.decoder_backend)
        if not self.keyframe_index and self.selection_policy=="all":object.__setattr__(self,"keyframe_index",self.samples)

    def discard(self):
        if self.cache_pin is not None:self.cache_pin.close()
        if not discard_cache(self.cache_root,self.directory) and self.directory.exists():
            raise CacheCleanupError(f'Unable to remove sample cache: {self.directory}')


def prepare_samples(source,cache_root,*,cancel_token=None,progress=None,
                    backend='auto',selection_policy='auto',profile=None):
    if cancel_token is not None:cancel_token.raise_if_cancelled()
    source=probe_source(Path(source),cancel_token=cancel_token) if not hasattr(source,'fingerprint') else source
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    from .profiling import AnalysisProfile
    decode_profile=profile if profile is not None else AnalysisProfile()
    requested=selection_policy
    packet_pts=None
    if backend=='auto' and selection_policy=='auto' and dict(source.metadata).get('codec')!='h264':
        backend='cpu-keyframes';selection_policy='all'
    if backend not in ('auto','cpu-keyframes','native-idr') or selection_policy not in ('auto','all','half'):
        raise ValueError('Invalid preparation policy')
    if selection_policy in ('auto','half'):
        from .keyframe_index import index_key_packets,choose_policy
        from .pts import _source_info
        from fractions import Fraction
        from .profiling import measured
        with measured(decode_profile,'packet_index'):packet_pts=index_key_packets(source,cancel_token)
        tb,origin=_source_info(source)
        if selection_policy=='auto':selection_policy=choose_policy(tuple((x-origin)*tb for x in packet_pts),Fraction(str(dict(source.metadata)['duration_s'])))
    if backend=='auto':backend='native-idr'

    pins=[];directory=None
    try:
        common=dict(cancelled=cancel_token,progress=progress,cache_owner=pins.append)
        if backend!='cpu-keyframes' or selection_policy!='all' or profile is not None:
            common.update(backend=backend,selection_policy=selection_policy,profile=decode_profile)
        samples,directory=keyframe_stream(source,Path(cache_root),**common)
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
        effective=decode_profile.metadata.get('selection_policy','all')
        full=tuple(samples)
        if effective=='half':
            if packet_pts is None:raise MediaError('InvalidTiming: missing full keyframe index',code='InvalidTiming')
            if [s.source_pts for s in samples]!=list(packet_pts[::2]):raise MediaError('InvalidTiming: packet/decode index mismatch',code='InvalidTiming')
            from .models import Sample
            by_pts={s.source_pts:s for s in samples};template=samples[0]
            full=tuple(replace(by_pts[pts],ordinal=i) if pts in by_pts else
                Sample(str(uuid4()),i,pts,template.time_base,str(directory/f'skipped_{i:06d}.jpg'),template.width,template.height,template.pts_origin)
                for i,pts in enumerate(packet_pts))
        decode_profile.metadata['requested_selection_policy']=requested
        decode_profile.metadata['keyframe_count']=len(full)
        return PreparedSamples(source,tuple(samples),directory,Path(cache_root),str(uuid4()),pins[0],
            selection_policy=effective,decoder_backend=decode_profile.metadata.get('decoder_backend','cpu-keyframes'),keyframe_index=full)
    except BaseException:
        for pin in pins:pin.close()
        if directory is not None and not discard_cache(cache_root,directory) and directory.exists():
            raise CacheCleanupError(f'Unable to remove sample cache: {directory}')
        raise
