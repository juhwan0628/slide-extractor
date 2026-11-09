"""Sequential batch planning and execution contracts."""
import json
from pathlib import Path
import pytest
from slide_core.models import CancelToken,CancelledError
from slide_core.pipeline import PipelineResult
from tests.test_cli import invoke,cli
from tests.cli_fixtures import video

def batch():
    from slide_cli import batch
    return batch

def test_default_and_output_dir_mapping(tmp_path):
    sources=[tmp_path/'가.mp4',tmp_path/'나.mp4']
    assert batch().plan_outputs(sources,None)==[(p,p.with_name(p.stem+'.slides.pdf')) for p in sources]
    assert batch().plan_outputs(sources,tmp_path/'out')[0][1]==tmp_path/'out/가.slides.pdf'

@pytest.mark.parametrize('kind',['same-stem','case','symlink','hardlink'])
def test_output_aliases_rejected_before_execution(tmp_path,kind):
    a=tmp_path/'a.mp4';b=tmp_path/'b.mp4';out=tmp_path/'out';out.mkdir()
    if kind=='same-stem':b=tmp_path/'a.mkv'
    if kind=='case':b=tmp_path/'A.mp4'
    if kind in ('symlink','hardlink'):
        x=out/'a.slides.pdf';x.write_bytes(b'x');y=out/'b.slides.pdf'
        if kind=='symlink':y.symlink_to(x)
        else:
            import os
            os.link(x,y)
    with pytest.raises(ValueError):batch().plan_outputs([a,b],out)

def test_output_cannot_overwrite_another_batch_input(tmp_path):
    source=tmp_path/'a.mp4';other=tmp_path/'a.slides.pdf'
    other.write_bytes(b'input')
    with pytest.raises(ValueError):batch().plan_outputs([source,other],None)

def test_json_sidecar_cannot_overwrite_another_input(tmp_path):
    with pytest.raises(ValueError):
        batch().plan_outputs([tmp_path/'a.mp4',tmp_path/'a.slides.json'],None,json_format='detailed')

def test_failure_continues_and_cancel_stops(tmp_path):
    jobs=[(tmp_path/f'{n}.mp4',tmp_path/f'{n}.pdf') for n in range(3)]
    calls=[]
    def run(source,out):
        calls.append(source)
        if len(calls)==1:raise OSError('bad input')
        return PipelineResult(out,None,1,(0,),'all','cpu-keyframes',1.)
    results=batch().run_batch(jobs,run_one=run,cancel_token=CancelToken())
    assert len(calls)==3 and [r['status'] for r in results]==['error','success','success']
    calls.clear();token=CancelToken()
    def cancel(source,out):
        calls.append(source);token.cancel();token.raise_if_cancelled()
    results=batch().run_batch(jobs,run_one=cancel,cancel_token=token)
    assert len(calls)==1 and [r['status'] for r in results]==['cancelled','skipped','skipped']

def test_cancellation_between_jobs_preserves_completed_output(tmp_path):
    token=CancelToken();jobs=[(tmp_path/f'{n}.mp4',tmp_path/f'{n}.pdf') for n in range(2)]
    def run(source,out):
        token.cancel()
        return PipelineResult(out,None,1,(0,),'all','cpu-keyframes',1.)
    results=batch().run_batch(jobs,run_one=run,cancel_token=token)
    assert [r['status'] for r in results]==['success','skipped']
    assert results[0]['published']

def test_cli_batch_real_media_failure_and_json(tmp_path):
    a=video(tmp_path/'첫 영상.mkv').path;b=video(tmp_path/'둘째.mkv').path
    missing=tmp_path/'missing.mp4';out=tmp_path/'out'
    result=invoke(a,missing,b,'--output-dir',out,'--json','detailed','--result-json','--quiet')
    assert result.returncode==1,result.stderr
    summary=json.loads(result.stdout)
    assert summary['status']=='error'
    assert summary['succeeded']==2 and summary['failed']==1
    assert [r['status'] for r in summary['results']]==['success','error','success']
    assert len(list(out.glob('*.pdf')))==2 and len(list(out.glob('*.json')))==2
    assert not list(out.glob('.slide-cli-*'))

def test_cli_conflicts_and_multi_input_output_rejected(tmp_path):
    for flags in [[],['--overwrite']]:
        result=invoke(tmp_path/'a.mp4',tmp_path/'a.mkv','--output-dir',tmp_path/'out','--result-json',*flags)
        assert result.returncode==2
        assert not (tmp_path/'out').exists()
    result=invoke('a.mp4','b.mp4','-o','out.pdf','--result-json')
    assert result.returncode==2

def test_single_input_output_dir_supported(tmp_path):
    source=video(tmp_path/'source.mkv')
    result=invoke(source.path,'--output-dir',tmp_path/'out','--quiet','--result-json')
    assert result.returncode==0,result.stderr
    assert Path(json.loads(result.stdout)['pdf_path'])==tmp_path/'out/source.slides.pdf'

def test_cli_batch_cancel_returns_130(tmp_path,monkeypatch,capsys):
    import signal
    from slide_core import pipeline
    calls=[]
    def run(*args,**kwargs):
        calls.append(args[0]);signal.getsignal(signal.SIGINT)(signal.SIGINT,None)
        kwargs['cancel_token'].raise_if_cancelled()
    monkeypatch.setattr(pipeline,'run_pipeline',run)
    assert cli().main(['a.mp4','b.mp4','--result-json'])==130
    summary=json.loads(capsys.readouterr().out)
    assert len(calls)==1 and summary['status']=='cancelled'
    assert summary['results'][1]['status']=='skipped'

def test_unicode_normalization_aliases_rejected(tmp_path):
    import unicodedata
    composed="강의"
    decomposed=unicodedata.normalize("NFD",composed)
    sources=[tmp_path/"first"/(composed+".mp4"),tmp_path/"second"/(decomposed+".mp4")]
    with pytest.raises(ValueError,match="collide"):
        batch().plan_outputs(sources,tmp_path/"out")
