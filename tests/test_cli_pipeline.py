"""Shared automatic pipeline contract using actual synthetic media."""
from dataclasses import replace
from pathlib import Path
import pytest
from pypdf import PdfReader
from slide_core.config import AnalysisSettings
from slide_core.models import CancelToken, CancelledError, Rect
from tests.cli_fixtures import video

def pipeline():
    from slide_core.pipeline import run_pipeline
    return run_pipeline

@pytest.mark.parametrize('json_format',[None,'simple','detailed'])
def test_pipeline_matches_gui_core_and_cleans_cache(tmp_path,json_format):
    from slide_core.source_preparation import prepare_first_frame
    from slide_core.preparation import prepare_samples
    from slide_core.analysis import analyze_project
    from slide_core.pdf_only_export import export_pdf_only
    from slide_core.keyframe_export import export_project
    source=video(tmp_path/'강의 source.mkv')
    roi=prepare_first_frame(source)[3]
    prepared=prepare_samples(source,tmp_path/'reference-cache')
    try:
        project,_=analyze_project(source,AnalysisSettings(roi_mode='manual',effective_roi=roi),
                                  tmp_path/'reference-cache',prepared=prepared)
        expected=tuple(next(s.source_pts for s in project.samples if s.sample_id==p.representative_sample_id) for p in project.pages)
        reference=tmp_path/'reference.pdf'
        if json_format: export_project(project,reference,sidecar_format=json_format)
        else: export_pdf_only(project,reference)
        result=pipeline()(source.path,tmp_path/'result.pdf',json_format=json_format)
        assert result.representative_pts==expected
        assert result.page_count==len(project.pages)
        assert result.selection_policy==project.selection_policy
        def images(path):
            return [tuple((im.image.mode,im.image.size,im.image.tobytes()) for im in page.images) for page in PdfReader(path).pages]
        assert images(result.pdf_path)==images(reference)
        assert result.json_path==(tmp_path/'result.json' if json_format else None)
        assert not list(tmp_path.glob('.slide-cli-*'))
    finally: prepared.discard()

def test_existing_output_is_rejected_before_decoder(tmp_path,monkeypatch):
    from slide_core import pipeline as mod
    source=tmp_path/'source.mkv';source.write_bytes(b'video')
    output=tmp_path/'result.pdf';output.write_bytes(b'keep')
    monkeypatch.setattr(mod,'probe_source',lambda *a,**kw:pytest.fail('decoder/probe started'))
    with pytest.raises(Exception,match='exists'):mod.run_pipeline(source,output)
    assert output.read_bytes()==b'keep'

@pytest.mark.parametrize('failure',['analysis','export','cancel'])
def test_failure_and_cancel_remove_owned_cache(tmp_path,monkeypatch,failure):
    from slide_core import pipeline as mod
    source=video(tmp_path/'source.mkv')
    token=CancelToken()
    if failure=='cancel':
        def progress(stage,*args):
            if stage=='detecting':token.cancel()
    else:
        progress=None
        def fail(*a,**kw):raise RuntimeError('injected '+failure)
        monkeypatch.setattr(mod,'analyze_project' if failure=='analysis' else 'export_pdf_only',fail)
    with pytest.raises(CancelledError if failure=='cancel' else RuntimeError):
        mod.run_pipeline(source.path,tmp_path/'out.pdf',cancel_token=token,progress=progress)
    assert not list(tmp_path.glob('.slide-cli-*'))
    assert not (tmp_path/'out.pdf').exists()

def test_post_publication_cleanup_error_preserves_result(tmp_path,monkeypatch):
    from slide_core import pipeline as mod
    from slide_core.preparation import PreparedSamples
    source=video(tmp_path/'source.mkv')
    original=PreparedSamples.discard
    def fail(self):
        original(self)
        raise OSError('cleanup failed')
    monkeypatch.setattr(PreparedSamples,'discard',fail)
    with pytest.raises(mod.PipelineCleanupError) as err:
        mod.run_pipeline(source.path,tmp_path/'out.pdf')
    assert err.value.result.pdf_path.is_file()
    assert err.value.result.page_count>0

def test_pdf_only_does_not_touch_existing_json(tmp_path):
    source=video(tmp_path/'source.mkv')
    (tmp_path/'out.json').write_text('keep')
    pipeline()(source.path,tmp_path/'out.pdf')
    assert (tmp_path/'out.json').read_text()=='keep'

def test_invalid_roi_fails_before_preparation(tmp_path,monkeypatch):
    from slide_core import pipeline as mod
    source=video(tmp_path/'source.mkv')
    monkeypatch.setattr(mod,'prepare_samples',lambda *a,**kw:pytest.fail('preparation started'))
    with pytest.raises(ValueError):
        mod.run_pipeline(source.path,tmp_path/'out.pdf',
                         settings=AnalysisSettings(roi_mode='manual',effective_roi=Rect(0,0,999,999)))

def test_auto_roi_warning_keeps_gui_auto_mode(tmp_path,monkeypatch):
    from slide_core import pipeline as mod
    source=video(tmp_path/'source.mkv')
    original=mod.prepare_first_frame
    def warned(*args,**kwargs):
        value=original(*args,**kwargs)
        return (*value[:4],'black_intro')
    monkeypatch.setattr(mod,'prepare_first_frame',warned)
    analyze=mod.analyze_project
    def checked(source,settings,*args,**kwargs):
        assert settings.roi_mode=='auto'
        return analyze(source,settings,*args,**kwargs)
    monkeypatch.setattr(mod,'analyze_project',checked)
    mod.run_pipeline(source.path,tmp_path/'out.pdf')
