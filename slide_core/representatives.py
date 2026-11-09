"""Annotation-aware selection using three scans and image-sized working memory."""
import numpy as np

def _edges(frame, threshold):
    signed=frame.astype(np.int16)
    edges=np.zeros(frame.shape,bool)
    edges[:,1:] |= np.abs(np.diff(signed,axis=1))>threshold
    edges[1:,:] |= np.abs(np.diff(signed,axis=0))>threshold
    return edges

def select_representative(read_frame,start,end,valid_mask,config,check_cancel,*,diagnostics=None):
    """Return original index; read_frame must supply a valid uint8 observation."""
    if end<=start:raise ValueError("Empty representative interval")
    valid=np.asarray(valid_mask)
    if valid.dtype!=bool or valid.ndim!=2 or not valid.any():raise ValueError("Invalid representative mask")
    reference=np.zeros(valid.shape,np.uint8)
    for i in range(start,end):
        check_cancel();np.maximum(reference,read_frame(i),out=reference)
    mark_counts=np.zeros(valid.shape,np.uint64)
    edge_counts=np.zeros(valid.shape,np.uint64)
    reference=reference.astype(np.int16)
    for i in range(start,end):
        check_cancel();frame=read_frame(i)
        mark_counts+=((reference-frame.astype(np.int16))>config.pixel_delta)&valid
        edge_counts+=_edges(frame,config.edge_delta)&valid
    supported=mark_counts>=min(config.support_samples,end-start)
    core=edge_counts>=max(1,int(np.ceil((end-start)*config.core_edge_fraction)))
    best=None;best_key=None;best_stats=None
    for i in range(start,end):
        check_cancel();frame=read_frame(i)
        marks=((reference-frame.astype(np.int16))>config.pixel_delta)&valid
        lost=core&~(_edges(frame,config.edge_delta)&valid)
        tiles=set();size=config.tile_size
        for y in range(0,valid.shape[0],size):
            for x in range(0,valid.shape[1],size):
                mask=valid[y:y+size,x:x+size]
                if mask.any() and marks[y:y+size,x:x+size].sum()/mask.sum()>=config.overlay_fraction and lost[y:y+size,x:x+size].sum()>=config.overlay_missing_edges:
                    tiles.add((y//size,x//size))
        occluded=sum(any((y+dy,x+dx) in tiles for dy,dx in ((1,0),(-1,0),(0,1),(0,-1))) for y,x in tiles)
        retained=int((marks&supported).sum());transient=int((marks&~supported).sum())
        key=(-occluded,retained-config.transient_penalty*transient,i)
        if best_key is None or key>best_key:
            best=i;best_key=key
            best_stats=dict(missing_supported_pixels=int((supported&~marks).sum()),retained_supported_pixels=retained,occlusion_tiles=int(occluded))
    if diagnostics is not None:diagnostics.update(best_stats)
    return best
