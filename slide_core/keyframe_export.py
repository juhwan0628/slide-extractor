"""Keyframe-only paired PDF/JSON publishing using the shared durable transaction."""
from __future__ import annotations
import hashlib,shutil,tempfile
from pathlib import Path
from PIL import Image,UnidentifiedImageError
from .models import make_export_snapshot,SlideImage
from .media import source_matches,MediaError
from .pts import extract_selected
from .pdf import write_slides_pdf,validate_slides_pdf
from .keyframe_json import payload_keyframes,dumps,loads
from .simple_json import encode_simple
from .export import snapshot_export,ExportTransaction,_recover_one,OutputOverwriteRequired,RecoveryRequired,ExportPublishedError

def _unchanged(project,snap):
    if any(getattr(project,name)!=getattr(snap,name) for name in
           ("source","settings","samples","transitions","segments","revision",
            "cache_generation","analysis_generation","sampling_algorithm","selection_policy","decoder_backend","observation_sample_ids","keyframe_index_complete")) or tuple(project.pages)!=snap.pages:
        raise RuntimeError("Project changed during export")

def _representative_images(snap,cancel_token=None,cache_pin=None):
    samples={s.sample_id:s for s in snap.samples}
    selected=[samples[p.representative_sample_id] for p in snap.pages]
    for sample in selected:
        from .lazy_frames import ensure_sample_image
        ensure_sample_image(snap,sample,cancel_token,cache_pin=cache_pin)
        path=Path(sample.cache_path)
        if path.is_symlink() or not path.is_file():
            raise MediaError("CacheUnavailable: representative image missing",code="CacheUnavailable")
        try:
            with Image.open(path) as image:
                if image.size!=(sample.width,sample.height):raise ValueError("Cached image shape mismatch")
                image.verify()
        except (OSError,ValueError,UnidentifiedImageError) as exc:
            raise MediaError("CacheUnavailable: corrupt representative image",code="CacheUnavailable") from exc
    return selected

def export_project(project,destination,*,overwrite=False,sidecar_format="detailed",
                   cancel_token=None,progress=None,snapshot=None):
    if sidecar_format not in ("simple","detailed"):raise ValueError("Unsupported JSON format")
    if cancel_token is not None:cancel_token.raise_if_cancelled()
    snap=make_export_snapshot(project)
    if not snap.pages:raise ValueError("Select at least one page")
    if not source_matches(snap.source):raise MediaError("SourceChanged",code="SourceChanged")
    destination=Path(destination).with_suffix(".pdf")
    destination.parent.mkdir(parents=True,exist_ok=True)
    if list(destination.parent.glob(".slide-export-*.journal.json")):
        raise RecoveryRequired("Unfinished export in folder: "+str(destination.parent))
    paths=snapshot_export(snap.source.path,destination,overwrite=overwrite)
    if snapshot is not None:
        if paths.paths!=snapshot.paths or paths.states!=snapshot.states:
            raise OutputOverwriteRequired("Output paths changed after approval; approve current files")
        paths=snapshot
    selections=_representative_images(snap,cancel_token,getattr(project,'_cache_pin',None))
    def publication_guard():
        _unchanged(project,snap)
        if cancel_token is not None:cancel_token.raise_if_cancelled()
    tx=ExportTransaction(snap.source.path,paths,publication_guard=publication_guard)
    temp=None;failure=None
    try:
        tx.check_filesystem()
        temp=Path(tempfile.mkdtemp(prefix=".pts-render-",dir=destination.parent))
        frames=extract_selected(snap.source,selections,temp,cancelled=cancel_token,
            roi=snap.settings.effective_roi,progress=progress)
        if len(frames)!=len(snap.pages):raise ValueError("Frame extraction count mismatch")
        images=[SlideImage(p,float(s.actual_time)) for p,s in zip(frames,selections,strict=True)]
        pdf=temp/"staged.pdf"
        if progress:progress("writing",None,None)
        write_slides_pdf(images,pdf,export_id=tx.export_id,cancel_token=cancel_token)
        if progress:progress("verifying",None,None)
        validate_slides_pdf(pdf,images,export_id=tx.export_id)
        raw=pdf.read_bytes()
        if sidecar_format=="detailed":
            value=payload_keyframes(snap,destination,hashlib.sha256(raw).hexdigest(),tx.export_id)
            sidecar=dumps(value).encode("utf-8")
        else:sidecar=encode_simple(snap)
        def validate_pair(pdf_path,json_path):
            validate_slides_pdf(pdf_path,images,export_id=tx.export_id)
            data=Path(json_path).read_bytes()
            if sidecar_format=="detailed":
                loads(data,pdf_bytes=Path(pdf_path).read_bytes(),
                      pdf_page_count=len(images),pdf_export_id=tx.export_id)
            if data!=sidecar:raise ValueError("JSON changed during publication")
        tx.validator=validate_pair
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        if not source_matches(snap.source):raise MediaError("SourceChanged",code="SourceChanged")
        _unchanged(project,snap)
        if progress:progress("publishing",None,None)
        _unchanged(project,snap)
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        tx.prepare(raw,sidecar)
        _unchanged(project,snap)
        if cancel_token is not None:cancel_token.raise_if_cancelled()
        tx.commit()
        if cancel_token is not None:cancel_token.mark_committed()
        _recover_one(tx.journal_path,tx.paths[0].parent,validator=validate_pair)
        return paths.paths
    except BaseException as exc:
        failure=exc
        if tx._committed:
            if cancel_token is not None:cancel_token.mark_committed()
            raise ExportPublishedError(paths.paths,exc) from exc
        raise
    finally:
        errors=[]
        for cleanup in (tx.discard_unpublished,tx.close):
            try:cleanup()
            except Exception as exc:errors.append(exc)
        if not tx._prepared and not tx.journal_path.exists():
            try:tx._remove_stage()
            except Exception as exc:errors.append(exc)
        if temp is not None:
            try:shutil.rmtree(temp)
            except Exception as exc:errors.append(exc)
        if errors:
            if tx._committed:
                if cancel_token is not None:cancel_token.mark_committed()
                raise ExportPublishedError(paths.paths,errors[0]) from errors[0]
            if failure is not None:
                failure.add_note("Export cleanup failed: "+str(errors[0]))
            else:raise errors[0]
