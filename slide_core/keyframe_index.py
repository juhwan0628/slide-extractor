"""Packet-only original-PTS index and strict two-second GOP policy."""
from fractions import Fraction
from .media import _run_process,source_matches,MediaError
from .pts import _source_info

def choose_policy(times,duration):
    times=tuple(Fraction(t) for t in times);duration=Fraction(duration)
    if not times or duration<=0 or times[0]<0 or times[-1]>=duration or any(a>=b for a,b in zip(times,times[1:])):
        raise ValueError('Invalid keyframe times')
    gaps=(times[0],*(b-a for a,b in zip(times,times[1:])),duration-times[-1])
    return 'half' if max(gaps)<2 else 'all'

def index_key_packets(source,cancel_token=None):
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    def keys(line):
        fields=dict(x.split(b'=',1) for x in line.split(b'|') if b'=' in x)
        return line if b'K' in fields.get(b'flags',b'') else None
    result=_run_process(['ffprobe','-v','error','-select_streams',str(source.stream_index),
        '-show_packets','-show_entries','packet=pts,flags','-of','compact=p=0:nk=0',str(source.path)],
        tool='ffprobe',phase='keyframe_index',timeout=120,cancel_token=cancel_token,stdout_line_filter=keys)
    tb,origin=_source_info(source);pts=[]
    try:
        for line in result.stdout.splitlines():
            fields=dict(x.split('=',1) for x in line.split('|') if '=' in x)
            value=int(fields['pts'])
            if value<origin or (pts and value<=pts[-1]):raise ValueError('Invalid key packet PTS')
            pts.append(value)
    except (ValueError,KeyError) as exc:raise MediaError('InvalidTiming: key packet PTS',code='InvalidTiming') from exc
    choose_policy(tuple((x-origin)*tb for x in pts),Fraction(str(dict(source.metadata)['duration_s'])))
    if not source_matches(source):raise MediaError('SourceChanged',code='SourceChanged')
    return tuple(pts)
