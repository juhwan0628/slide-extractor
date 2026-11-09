from __future__ import annotations
import cv2
import numpy as np

BLACK_THRESHOLD = 8


def _outer_black_extent(profile: np.ndarray, reverse: bool = False) -> int:
    values = profile[::-1] if reverse else profile
    count = 0
    for value in values:
        if value <= BLACK_THRESHOLD:
            count += 1
        else:
            break
    return count


from dataclasses import dataclass
from math import floor, ceil
from .models import Rect

@dataclass(frozen=True)
class DetectionRegion:
    display_roi: Rect
    cache_roi: Rect
    detector_roi: Rect
    valid: np.ndarray
    outside_mask_warning: bool = False


def _project_rect(rect, sw, sh, dw, dh):
    if rect.x+rect.width>sw or rect.y+rect.height>sh or min(sw,sh,dw,dh)<=0:
        raise ValueError('ROI outside display')
    x0=max(0,min(dw-1,floor(rect.x*dw/sw)))
    y0=max(0,min(dh-1,floor(rect.y*dh/sh)))
    x1=min(dw,max(x0+1,ceil((rect.x+rect.width)*dw/sw)))
    y1=min(dh,max(y0+1,ceil((rect.y+rect.height)*dh/sh)))
    return Rect(x0,y0,x1-x0,y1-y0)


def detection_geometry(sw,sh,cw,ch,roi=None,mask=None,*,max_detector_width=320):
    """Shared display/cache/detector projection; no pixel allocation."""
    roi=roi or Rect(0,0,sw,sh)
    cached=_project_rect(roi,sw,sh,cw,ch)
    dw=min(cached.width,max_detector_width)
    dh=max(1,round(cached.height*dw/cached.width))
    if dw<64 or dh<36:raise ValueError('Detector crop must be at least 64x36')
    excluded=None;outside=False
    if mask is not None:
        if mask.x+mask.width>sw or mask.y+mask.height>sh:raise ValueError('Invalid mask')
        x0,y0=max(roi.x,mask.x),max(roi.y,mask.y)
        x1,y1=min(roi.x+roi.width,mask.x+mask.width),min(roi.y+roi.height,mask.y+mask.height)
        outside=(x0,y0,x1,y1)!=(mask.x,mask.y,mask.x+mask.width,mask.y+mask.height)
        if x0<x1 and y0<y1:
            xa=max(0,floor((x0-roi.x)*dw/roi.width)-1)
            ya=max(0,floor((y0-roi.y)*dh/roi.height)-1)
            xb=min(dw,ceil((x1-roi.x)*dw/roi.width)+1)
            yb=min(dh,ceil((y1-roi.y)*dh/roi.height)+1)
            excluded=Rect(xa,ya,xb-xa,yb-ya)
    valid_count=dw*dh-(excluded.width*excluded.height if excluded is not None else 0)
    if valid_count<max(64,ceil(.05*dw*dh)):raise ValueError('Insufficient valid detector pixels')
    return roi,cached,Rect(0,0,dw,dh),excluded,outside,valid_count

def build_detection_region(sw,sh,cw,ch,roi=None,mask=None,*,max_detector_width=320):
    roi,cached,detector,excluded,outside,_=detection_geometry(
        sw,sh,cw,ch,roi,mask,max_detector_width=max_detector_width)
    valid=np.ones((detector.height,detector.width),dtype=bool)
    if excluded is not None:
        valid[excluded.y:excluded.y+excluded.height,excluded.x:excluded.x+excluded.width]=False
    return DetectionRegion(roi,cached,detector,valid,outside)


def automatic_roi(cache_images):
    if not cache_images:raise ValueError('No cache samples')
    gray=[]
    for im in cache_images[:8]:
        if im.ndim==3:im=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY)
        if im.ndim!=2:raise ValueError('Invalid image')
        gray.append(im)
    h,w=gray[0].shape
    if any(im.shape!=(h,w) for im in gray):raise ValueError('Inconsistent dimensions')
    full=Rect(0,0,w,h)
    stable=np.median(np.stack(gray).astype(np.float32),axis=0)
    if np.count_nonzero(stable>BLACK_THRESHOLD)/stable.size<.25:return full,'black_intro'
    columns=np.median(stable,axis=0);rows=np.median(stable,axis=1)
    left=_outer_black_extent(columns);right=_outer_black_extent(columns,True)
    top=_outer_black_extent(rows);bottom=_outer_black_extent(rows,True)
    rw,rh=w-left-right,h-top-bottom
    if rw<=0 or rh<=0 or rw<.25*w or rh<.25*h:return full,'implausible_roi'
    return Rect(left,top,rw,rh),None
