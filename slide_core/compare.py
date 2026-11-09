from __future__ import annotations
from dataclasses import dataclass
import cv2
from math import ceil
import numpy as np


@dataclass(frozen=True)
class LocalChangeMetrics:
    global_mad: float
    max_tile_mad: float
    changed_fraction: float
    tile_location: tuple[int, int]  # x, y in comparison image
    valid_count: int = 0
    eligible_tiles: int = 0


def local_change_metrics(a, b, *, tile_size=20, tile_stride=10, pixel_delta=20, valid_mask=None):
    if a.shape != b.shape or a.ndim!=2 or not a.size or a.dtype!=np.uint8 or b.dtype!=np.uint8:
        raise ValueError('Must use same-size uint8 grayscale frames')
    if min(tile_size,tile_stride)<1 or pixel_delta<0:raise ValueError('Invalid metric parameters')
    valid=np.ones(a.shape,dtype=bool) if valid_mask is None else valid_mask
    if valid.shape!=a.shape or valid.dtype!=bool:raise ValueError('Invalid mask shape or dtype')
    n=int(valid.sum())
    if n==0:raise ValueError('No valid comparison pixels')
    delta=cv2.absdiff(a,b)
    masked=np.where(valid,delta,0)
    h,w=delta.shape;th,tw=min(tile_size,h),min(tile_size,w)
    ys=np.unique(np.append(np.arange(0,h-th+1,tile_stride),h-th))
    xs=np.unique(np.append(np.arange(0,w-tw+1,tile_stride),w-tw))
    integral=cv2.integral(masked.astype(np.float64))
    count_integral=cv2.integral(valid.astype(np.uint8))
    def windows(I):
        return I[(ys+th)[:,None],(xs+tw)[None,:]]-I[ys[:,None],(xs+tw)[None,:]]-I[(ys+th)[:,None],xs[None,:]]+I[ys[:,None],xs[None,:]]
    sums=windows(integral);counts=windows(count_integral)
    eligible=counts>=ceil(.5*th*tw)
    tile=np.where(eligible,sums/np.maximum(counts,1)/255.,-1.)
    if eligible.any():
        iy,ix=np.unravel_index(tile.argmax(),tile.shape)
        value=float(tile[iy,ix]);place=(int(xs[ix]),int(ys[iy]))
    else:value,place=0.,(0,0)
    return LocalChangeMetrics(float(masked.sum()/(255*n)),value,
                              float(np.count_nonzero((delta>pixel_delta)&valid)/n),
                              place,n,int(eligible.sum()))
