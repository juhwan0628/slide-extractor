"""Low-overhead wall-time accounting; overlapping phases are not additive."""
import json
import platform
import time
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path

class AnalysisProfile:
    def __init__(self):
        self.seconds={};self.metadata={'status':'running','platform':platform.system(),'machine':platform.machine()}
    @contextmanager
    def measure(self,key):
        started=time.perf_counter()
        try:yield
        finally:self.seconds[key]=self.seconds.get(key,0)+time.perf_counter()-started
    def report(self):
        values=dict(self.seconds)
        if 'detection' in values:
            values['detector_exclusive']=max(0,values['detection']-values.get('cache_prepare',0))
        note=('total includes all stages; sampling includes decode_wait/JPEG/cache work; detection includes cache_prepare. '
              if 'click_to_result_total' not in values else
              'GUI total includes detector pipeline stages; preparation_wait and click_to_result_total record click latency. preparation_total overlaps background/UI time and is not additive. ')
        return {'schema_version':1,**self.metadata,'seconds':values,
                'timing_note':note+'decode_wait is consumer wait for FFmpeg/pipe data, not isolated decoder CPU time.'}
    def save(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(self.report(),indent=2)+'\n');temp.replace(path)

def measured(profile,key):
    return profile.measure(key) if profile is not None else nullcontext()


def profile_directory(source_root):
    """Installed Mac apps keep mutable reports outside the signed bundle."""
    if getattr(sys,"frozen",False) and sys.platform=="darwin":
        return Path.home()/"Library"/"Application Support"/"SlideExtractor"/"profiles"
    return Path(source_root)/"profiles"
