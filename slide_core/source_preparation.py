"""Bounded first-frame preparation using canonical display geometry."""
from math import floor,ceil
import numpy as np
from slide_core.display import build_display_pipeline
from slide_core.media import MediaError,source_matches
from slide_core.models import Rect
from slide_core.roi import automatic_roi
from slide_core.pts import _decode_stream

def prepare_first_frame(source,*,cancel_token=None):
    if cancel_token is not None:cancel_token.raise_if_cancelled()
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    meta=dict(source.metadata)
    pipe=build_display_pipeline(width=source.width,height=source.height,
        sar=source.transform.sample_aspect_ratio,rotation=source.transform.rotation,
        color_matrix=meta.get('color_matrix'),color_range=meta.get('color_range'),
        transfer=meta.get('color_transfer'),pixel_format=meta.get('pixel_format','yuv420p'))
    try:
        frames=list(_decode_stream(source,f'showinfo@audit,showinfo@sample,{pipe.cache_filter}',
            pipe.cache_width,pipe.cache_height,timeout=20,cancelled=cancel_token,frame_limit=1))
    except MediaError as exc:
        if exc.code=='Timeout':raise MediaError('FirstFrameTimeout',code='FirstFrameTimeout') from exc
        raise
    if len(frames)!=1:raise MediaError('Unable to decode first keyframe',code='FirstFrameDecodeFailed')
    payload=frames[0][1]
    if cancel_token is not None:cancel_token.raise_if_cancelled()
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    rgb=np.frombuffer(payload,dtype=np.uint8).reshape(pipe.cache_height,pipe.cache_width,3)
    suggestion,warning=automatic_roi([rgb])
    dw,dh=source.transform.display_width,source.transform.display_height
    x=floor(suggestion.x*dw/pipe.cache_width);y=floor(suggestion.y*dh/pipe.cache_height)
    right=ceil((suggestion.x+suggestion.width)*dw/pipe.cache_width)
    bottom=ceil((suggestion.y+suggestion.height)*dh/pipe.cache_height)
    return payload,pipe.cache_width,pipe.cache_height,Rect(x,y,min(dw,right)-x,min(dh,bottom)-y),warning
