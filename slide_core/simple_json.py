"""Compact PDF-page navigation, not a claim about true slide boundaries."""
from __future__ import annotations
import json
from fractions import Fraction
from .models import Project,ExportSnapshot

def simple_timeline(project):
    if isinstance(project,Project):project.validate()
    elif not isinstance(project,ExportSnapshot):raise TypeError("Project or export snapshot required")
    samples={s.sample_id:s for s in project.samples}
    duration=Fraction(str(dict(project.source.metadata)["duration_s"]))
    times=[samples[p.representative_sample_id].actual_time for p in project.pages]
    entries=[]
    for i,t in enumerate(times):
        start=Fraction(0) if i==0 else t
        end=times[i+1] if i+1<len(times) else duration
        if start<0 or end<=start:raise ValueError("Invalid page navigation range")
        entries.append(dict(page=i+1,start=float(start),end=float(end)))
    return dict(slides=entries)

def encode_simple(project):
    return (json.dumps(simple_timeline(project),ensure_ascii=False,indent=2,allow_nan=False)+"\n").encode("utf-8")
