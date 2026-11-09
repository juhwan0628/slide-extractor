"""Shared synthetic lecture media, independent of GUI test modules."""
import subprocess
import pytest
from slide_core.media import probe_source

def video(path,codec='mpeg4',offset=0,vfr=False):
 encoders=subprocess.check_output(['ffmpeg','-hide_banner','-encoders'],stderr=subprocess.DEVNULL,text=True)
 if codec not in encoders:pytest.skip('Test encoder unavailable: '+codec)
 cmd=['ffmpeg','-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=2.4']
 if vfr:cmd+=['-vf','setpts=floor(N/2)*3+mod(N\\,2)']
 elif offset:cmd+=['-vf',f'setpts=PTS+{offset}/TB']
 cmd+=['-c:v',codec,'-g','5','-bf','2','-fps_mode','passthrough']
 if codec=='libx264':cmd+=['-preset','ultrafast','-x264-params','scenecut=0']
 if codec=='libx265':cmd+=['-preset','ultrafast','-x265-params','pools=1:frame-threads=1:scenecut=0']
 subprocess.run(cmd+[str(path)],check=True,timeout=40,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 return probe_source(path)
