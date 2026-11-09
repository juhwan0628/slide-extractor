"""Verified PDF-only publishing, isolated from existing JSON files.

Both export modes coordinate on the same canonical PDF resource lock.
"""
from __future__ import annotations
import os
import tempfile
import shutil
import uuid
from pathlib import Path
from PIL import Image,UnidentifiedImageError
from .models import make_export_snapshot,SlideImage
from .media import source_matches,MediaError
from .pts import extract_selected
from .pdf import write_slides_pdf,validate_slides_pdf
from .export import OutputCollision,OutputOverwriteRequired,RecoveryRequired,_state,sync_directory,export_file_lease,output_lock_path,ExportPublishedError

def export_pdf_only(project,destination,*,overwrite=False,cancel_token=None,progress=None,snapshot=None):
    project.validate()
    if not project.pages:raise ValueError('Select at least one page')
    if not source_matches(project.source):raise MediaError('SourceChanged',code='SourceChanged')
    destination=Path(destination).absolute().with_suffix('.pdf')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise OutputCollision('Unsafe PDF destination')
    if destination==project.source.path or (destination.exists() and destination.samefile(project.source.path)):
        raise OutputCollision('PDF would overwrite source')
    if list(destination.parent.glob('.slide-export-*.journal.json')):
        raise RecoveryRequired('Resolve interrupted paired export first')
    initial_state=_state(destination)
    if snapshot is not None and (snapshot.path!=destination or snapshot.state!=initial_state):
        raise OutputOverwriteRequired('PDF changed after approval; approve the current file')
    if initial_state is not None and not overwrite:raise OutputOverwriteRequired('PDF exists')
    snap=make_export_snapshot(project)
    ids={s.sample_id:s for s in snap.samples}
    selections=[ids[p.representative_sample_id] for p in snap.pages]
    for sample in selections:
        from .lazy_frames import ensure_sample_image
        ensure_sample_image(snap,sample,cancel_token,cache_pin=getattr(project,'_cache_pin',None))
        path=Path(sample.cache_path)
        if path.is_symlink() or not path.is_file():
            raise MediaError('CacheUnavailable',code='CacheUnavailable')
        try:
            with Image.open(path) as image:
                if image.size!=(sample.width,sample.height):raise ValueError('Invalid cached sample size')
                image.verify()
        except (OSError,ValueError,UnidentifiedImageError) as exc:
            raise MediaError('CacheUnavailable',code='CacheUnavailable') from exc
    lock=output_lock_path(destination)
    temp=Path(tempfile.mkdtemp(prefix='.slide-pdf-',dir=destination.parent))
    try:
        frames=extract_selected(snap.source,selections,temp,cancelled=cancel_token,roi=snap.settings.effective_roi,progress=progress)
        if len(frames)!=len(snap.pages):raise ValueError('Incomplete frame extraction')
        images=[SlideImage(path,float(sample.actual_time))
                for path,sample in zip(frames,selections,strict=True)]
        pdf=temp/'render.pdf'
        export_id=str(uuid.uuid4())
        if progress:progress('writing',None,None)
        write_slides_pdf(images,pdf,export_id=export_id,cancel_token=cancel_token)
        if progress:progress('verifying',None,None)
        validate_slides_pdf(pdf,images,export_id=export_id)
        if progress:progress('publishing',None,None)
        published=False
        try:
            with export_file_lease(lock):
                if cancel_token is not None:cancel_token.raise_if_cancelled()
                if not source_matches(snap.source):raise MediaError('SourceChanged',code='SourceChanged')
                if project.revision!=snap.revision or project.analysis_generation!=snap.analysis_generation:
                    raise RuntimeError('Project changed during PDF export')
                if destination.is_symlink() or _state(destination)!=initial_state:
                    raise OutputOverwriteRequired('PDF changed while exporting')
                os.replace(pdf,destination)
                published=True
                # Publication is irrevocable here; cancellation must not disguise it.
                if cancel_token is not None:cancel_token.mark_committed()
                validate_slides_pdf(destination,images,export_id=export_id)
                sync_directory(destination.parent)
        except Exception as exc:
            if published:
                raise ExportPublishedError((destination,), exc) from exc
            raise
        return (destination,)
    finally:
        shutil.rmtree(temp,ignore_errors=True)
