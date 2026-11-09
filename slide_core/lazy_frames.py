"""Materialize one skipped decoder-keyframe at exact original PTS on demand."""
from pathlib import Path
from threading import Lock
import io
from PIL import Image
from .media import MediaError,source_matches
from .display import build_display_pipeline
from .pts import _decode_stream

_lock=Lock()

def ensure_sample_image(project,sample,cancel_token=None,*,cache_pin=None):
    def check():
        if cancel_token is not None:cancel_token.raise_if_cancelled()
    check()
    if sample not in project.samples:raise ValueError('Unknown keyframe')
    path=Path(sample.cache_path)
    def existing():
        if path.is_symlink():raise MediaError('CacheUnavailable: unsafe cache image',code='CacheUnavailable')
        if not path.exists():return False
        try:
            with Image.open(path) as image:
                if image.size!=(sample.width,sample.height):raise ValueError('Cache dimensions')
                image.verify()
        except (OSError,ValueError) as exc:raise MediaError('CacheUnavailable: corrupt cache image',code='CacheUnavailable') from exc
        return True
    if existing():return path
    if sample.sample_id in project.observation_sample_ids or not project.keyframe_index_complete:
        raise MediaError('CacheUnavailable: missing analysis image',code='CacheUnavailable')
    observed=set(project.observation_sample_ids)
    parents={Path(s.cache_path).parent for s in project.samples if s.sample_id in observed}
    if len(parents)!=1 or path.parent not in parents or path.name!=f'skipped_{sample.ordinal:06d}.jpg':
        raise MediaError('CacheUnavailable: lazy target outside cache generation',code='CacheUnavailable')
    if not path.parent.is_dir() or path.parent.is_symlink():raise MediaError('CacheUnavailable: cache generation missing',code='CacheUnavailable')
    while not _lock.acquire(timeout=.05):check()
    try:
        check()
        if existing():return path
        if not source_matches(project.source):raise MediaError('SourceChanged',code='SourceChanged')
        src=project.source;info=dict(src.metadata)
        pipe=build_display_pipeline(width=src.width,height=src.height,sar=src.transform.sample_aspect_ratio,
            rotation=src.transform.rotation,color_matrix=info.get('color_matrix'),color_range=info.get('color_range'),
            transfer=info.get('color_transfer'),pixel_format=info.get('pixel_format','yuv420p'))
        vf=f"showinfo@audit,select='gte(pts,{sample.source_pts})',showinfo@sample,{pipe.cache_filter}"
        frames=_decode_stream(src,vf,sample.width,sample.height,timeout=120,cancelled=cancel_token,
            seek=sample.source_pts*sample.time_base,frame_limit=1)
        try:decoded=list(frames)
        finally:frames.close()
        if len(decoded)!=1 or decoded[0][0]!=sample.source_pts:raise MediaError('InvalidTiming: requested keyframe missing',code='InvalidTiming')
        check()
        if not source_matches(src):raise MediaError('SourceChanged',code='SourceChanged')
        with io.BytesIO() as file:
            Image.frombytes('RGB',(sample.width,sample.height),decoded[0][1]).save(file,format='JPEG',quality=90)
            content=file.getvalue()
        check()
        pin=cache_pin if cache_pin is not None else getattr(project,'_cache_pin',None)
        if pin is None:raise MediaError('CacheUnavailable: missing cache owner',code='CacheUnavailable')
        if pin.write_lazy_jpeg(sample.ordinal,content,cancel_token,directory=path.parent)!=path:raise MediaError('CacheUnavailable: mismatched cache owner',code='CacheUnavailable')
        return path
    finally:
        _lock.release()
