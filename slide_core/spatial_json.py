"""Explicit spatial detector settings and actual input policy for detailed timelines."""
from dataclasses import asdict
from .config import SpatialConfig

SPATIAL_FIELDS={'pixel_delta','changed_fraction','cell_fraction','cell_changed_fraction','tile_size'}
REPRESENTATIVE_FIELDS={'support_samples','edge_delta','core_edge_fraction','overlay_fraction','overlay_missing_edges','transient_penalty'}
DETECTOR_FIELDS={'algorithm','image_max_width','mask_dilation_px','input_policy','spatial','representative'}
BOUNDARY_FIELDS={'boundary_id','left_sample_id','right_sample_id','lower_s','upper_s','observed_at_s','true_boundary_s','uncertainty_kind','status','persistence_samples','spatial_metrics'}

def detector_payload(config,input_policy="all"):
    values=asdict(config)
    return dict(algorithm='spatial-groups-v1',image_max_width=320,mask_dilation_px=1,input_policy=input_policy,
                spatial={k:values[k] for k in sorted(SPATIAL_FIELDS)},
                representative={k:values[k] for k in sorted(REPRESENTATIVE_FIELDS)})

def validate_detector(value,input_policy="all"):
    if not isinstance(value,dict) or set(value)!=DETECTOR_FIELDS:raise ValueError('Invalid spatial detector fields')
    if value['algorithm']!='spatial-groups-v1' or type(value['image_max_width']) is not int or value['image_max_width']!=320 or type(value['mask_dilation_px']) is not int or value['mask_dilation_px']!=1 or value['input_policy']!=input_policy:raise ValueError('Invalid spatial detector policy')
    if not isinstance(value['spatial'],dict) or set(value['spatial'])!=SPATIAL_FIELDS:raise ValueError('Invalid spatial thresholds')
    if not isinstance(value['representative'],dict) or set(value['representative'])!=REPRESENTATIVE_FIELDS:raise ValueError('Invalid representative settings')
    return SpatialConfig(**value['spatial'],**value['representative'])
