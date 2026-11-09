"""Single-video CLI: actual media plus failure boundaries."""
import importlib
import json
import signal
import subprocess
import sys
from pathlib import Path
import pytest
from tests.cli_fixtures import video
from slide_core.version import VERSION

ROOT=Path(__file__).resolve().parents[1]
def cli():
    return importlib.import_module('slide_cli.cli')
def invoke(*args):
    return subprocess.run([sys.executable,'-m','slide_cli',*map(str,args)],cwd=ROOT,
                          text=True,capture_output=True,timeout=40)

def test_help_and_version():
    assert invoke('--help').returncode==0
    assert VERSION in invoke('--version').stdout

@pytest.mark.parametrize('args',[
    [],['missing.mp4','--roi','-1,0,10,10'],
    ['missing.mp4','--roi','1,2,3'],
    ['missing.mp4','--json','wrong'],
    ['missing.mp4','-o','bad.txt'],
    ['a.mp4','b.mp4','-o','out.pdf'],
])
def test_usage_errors_are_two_and_json_is_machine_readable(args,capsys):
    assert cli().main([*args,'--result-json'])==2
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='error' and not result['published']
    assert result['error_code']=='UsageError'

def test_actual_default_output_and_json_summary(tmp_path):
    source=video(tmp_path/'한글 강의.mkv')
    result=invoke(source.path,'--json','simple','--result-json')
    assert result.returncode==0,result.stderr
    summary=json.loads(result.stdout)
    assert summary['status']=='success' and summary['published']
    assert summary['page_count']>0
    assert Path(summary['pdf_path'])==tmp_path/'한글 강의.slides.pdf'
    assert Path(summary['json_path']).is_file()
    assert 'sampling' in result.stderr or 'detecting' in result.stderr
    again=invoke(source.path,'--result-json')
    assert again.returncode==1
    assert not json.loads(again.stdout)['published']
    overwrite=invoke(source.path,'--overwrite','--quiet','--result-json')
    assert overwrite.returncode==0,overwrite.stderr
    assert overwrite.stderr==''
    assert not list(tmp_path.glob('.slide-cli-*'))

def test_missing_source_reports_processing_failure(tmp_path):
    result=invoke(tmp_path/'missing.mp4','--result-json')
    assert result.returncode==1
    assert json.loads(result.stdout)['status']=='error'
    assert result.stderr

def test_invalid_display_roi_is_usage_error(tmp_path):
    source=video(tmp_path/'source.mkv')
    result=invoke(source.path,'--roi','0,0,9999,9999','--result-json')
    assert result.returncode==2
    assert not (tmp_path/'source.slides.pdf').exists()

def test_same_source_output_protected(tmp_path):
    source=tmp_path/'source.pdf';source.write_bytes(b'original')
    assert invoke(source,'-o',source,'--overwrite').returncode==1
    assert source.read_bytes()==b'original'

def test_cancel_restores_signal_handler(tmp_path,monkeypatch,capsys):
    from slide_core import pipeline
    old=signal.getsignal(signal.SIGINT)
    def cancel(*args,**kwargs):
        signal.getsignal(signal.SIGINT)(signal.SIGINT,None)
        kwargs['cancel_token'].raise_if_cancelled()
    monkeypatch.setattr(pipeline,'run_pipeline',cancel)
    assert cli().main([str(tmp_path/'source.mp4'),'--result-json'])==130
    assert json.loads(capsys.readouterr().out)['status']=='cancelled'
    assert signal.getsignal(signal.SIGINT)==old

def test_published_cleanup_failure_reports_output(tmp_path,monkeypatch,capsys):
    from slide_core import pipeline
    from slide_core.pipeline import PipelineResult,PipelineCleanupError
    result=PipelineResult(tmp_path/'out.pdf',None,2,(0,1),'all','cpu-keyframes',1.)
    def fail(*args,**kwargs):raise PipelineCleanupError(result)
    monkeypatch.setattr(pipeline,'run_pipeline',fail)
    assert cli().main([str(tmp_path/'source.mp4'),'--result-json'])==1
    summary=json.loads(capsys.readouterr().out)
    assert summary['published'] and summary['pdf_path']==str(result.pdf_path)

def test_cli_runs_with_qt_imports_blocked(tmp_path):
    source=video(tmp_path/'source.mkv')
    script="""import sys
class NoQt:
 def find_spec(self,name,*args):
  if name.startswith(('PySide6','shiboken6','gui')):
   raise RuntimeError('Qt imported: '+name)
sys.meta_path.insert(0,NoQt())
from slide_cli.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
    result=subprocess.run([sys.executable,'-c',script,str(source.path),'--quiet'],
                          cwd=ROOT,text=True,capture_output=True,timeout=40)
    assert result.returncode==0,result.stderr

def test_export_published_error_exposes_saved_paths(tmp_path,monkeypatch,capsys):
    from slide_core import pipeline
    from slide_core.export import ExportPublishedError
    output=tmp_path/'saved.pdf'
    def fail(*a,**kw):raise ExportPublishedError((output,),OSError('verification failed'))
    monkeypatch.setattr(pipeline,'run_pipeline',fail)
    assert cli().main([str(tmp_path/'source.mp4'),'--result-json'])==1
    summary=json.loads(capsys.readouterr().out)
    assert summary['published'] and summary['output_paths']==[str(output)]

def test_uppercase_pdf_output_is_rejected_explicitly(tmp_path):
    result=invoke(tmp_path/'source.mp4','-o',tmp_path/'out.PDF','--result-json')
    assert result.returncode==2
    assert json.loads(result.stdout)['error_code']=='UsageError'

@pytest.mark.skipif(sys.platform=='win32',reason='POSIX subprocess signal integration')
def test_real_sigint_cancels_without_publishing_or_cache(tmp_path):
    source=video(tmp_path/'source.mkv')
    proc=subprocess.Popen([sys.executable,'-m','slide_cli',str(source.path),'--result-json'],
                          cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        import selectors
        selector=selectors.DefaultSelector()
        selector.register(proc.stderr,selectors.EVENT_READ)
        assert selector.select(timeout=15),'CLI did not reach preparation'
        first=proc.stderr.readline()
        assert 'roi' in first,first
        proc.send_signal(signal.SIGINT)
        stdout,stderr=proc.communicate(timeout=15)
        assert proc.returncode==130,stderr
        assert json.loads(stdout)['status']=='cancelled'
        assert not (tmp_path/'source.slides.pdf').exists()
        assert not list(tmp_path.glob('.slide-cli-*'))
    finally:
        if proc.poll() is None:proc.kill();proc.wait()

def test_processing_value_error_is_not_usage_error(tmp_path,monkeypatch,capsys):
    from slide_core import pipeline
    def fail(*args,**kwargs):raise ValueError("Incomplete frame extraction")
    monkeypatch.setattr(pipeline,"run_pipeline",fail)
    assert cli().main([str(tmp_path/"source.mp4"),"--result-json"])==1
    summary=json.loads(capsys.readouterr().out)
    assert summary["error_code"]=="ValueError"
