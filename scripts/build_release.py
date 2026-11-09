"""Build validated CLI release artifacts; publish only from an explicit release branch."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from verify_snapshot import verify

REPOSITORY = "juhwan0628/slide-extractor"

def digest(data):
 return hashlib.sha256(data).hexdigest()

def validate_sdist(path,root):
 manifest_bytes=(root/'source-manifest.json').read_bytes()
 expected=json.loads(manifest_bytes)['tracked_file_sha256']
 files={}
 with tarfile.open(path,'r:gz') as archive:
  for member in archive.getmembers():
   name=PurePosixPath(member.name)
   if name.is_absolute() or '..' in name.parts or member.issym() or member.islnk():raise ValueError('Unsafe sdist member')
   if not member.isfile():continue
   rel=PurePosixPath(*name.parts[1:]).as_posix()
   if rel in files:raise ValueError('Duplicate sdist member')
   files[rel]=archive.extractfile(member).read()
 for name,sha in expected.items():
  if name not in files or digest(files[name])!=sha:raise ValueError('sdist source mismatch: '+name)
 if files.get('source-manifest.json')!=manifest_bytes:raise ValueError('sdist manifest mismatch')
 for name in files:
  if name in expected or name in ('source-manifest.json','PKG-INFO','setup.cfg'):continue
  if name.split('/')[0].endswith('.egg-info'):continue
  raise ValueError('Unexpected sdist file: '+name)

def publication_context(root,version):
 if os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY:raise ValueError('Wrong publication repository')
 if os.environ.get('GITHUB_REF_NAME')!='publish/cli-'+version:raise ValueError('Explicit matching publication branch required')
 sha=os.environ.get('GITHUB_SHA','')
 if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('Exact commit required')
 head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
 main=subprocess.check_output(['gh','api','repos/'+REPOSITORY+'/branches/main','--jq','.commit.sha'],text=True).strip()
 if sha!=head or sha!=main:raise ValueError('Publish only the exact current main commit')
 tag='v'+version
 existing=subprocess.run(['gh','api','repos/'+REPOSITORY+'/git/ref/tags/'+tag],capture_output=True,text=True)
 if existing.returncode==0:raise ValueError('Tag already exists; refusing replacement')
 if '404' not in existing.stderr:raise ValueError('Could not verify tag absence: '+existing.stderr)
 return sha

def build(root,publish=False):
 manifest=verify(root)
 version=tomllib.loads((root/'pyproject.toml').read_text())['project']['version']
 if version!=manifest['package_version'] or not re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?',version):raise ValueError('Version mismatch')
 sha=publication_context(root,version) if publish else None
 out=root/'dist'
 if out.exists() and list(out.iterdir()):raise ValueError('dist must be empty')
 subprocess.run([os.sys.executable,'-m','build','--sdist','--wheel','--no-isolation','--outdir',str(out)],cwd=root,check=True)
 wheel=next(out.glob('*.whl'));sdist=next(out.glob('*.tar.gz'))
 validate_sdist(sdist,root)
 with zipfile.ZipFile(wheel) as archive:
  names=archive.namelist()
  code={n for n in names if n.startswith(('slide_core/','slide_cli/'))}
  if code!=set(manifest['source_file_sha256']):raise ValueError('Wheel runtime inventory mismatch')
  for name in names:
   if name not in code and not name.split('/')[0].endswith('.dist-info'):raise ValueError('Unexpected wheel content')
  for name,expected in manifest['source_file_sha256'].items():
   if digest(archive.read(name))!=expected:raise ValueError('Wheel source changed')
 with tempfile.TemporaryDirectory(prefix='cli-release-smoke-') as tmp:
  tmp=Path(tmp);envdir=tmp/'venv'
  subprocess.run([os.sys.executable,'-m','venv',str(envdir)],check=True)
  python=envdir/'bin/python';command=envdir/'bin/slide-extractor'
  env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('SLIDE_NATIVE_KEYFRAME_HELPER',None);env['PYTHONUTF8']='1'
  subprocess.run([str(python),'-m','pip','install','-c',str(root/'requirements-test.lock'),str(wheel)],cwd=tmp,env=env,check=True)
  subprocess.run([str(python),'-c',"import importlib.util;assert all(importlib.util.find_spec(x) is None for x in ['gui','app','PySide6','shiboken6'])"],cwd=tmp,env=env,check=True)
  video=tmp/'smoke.mkv'
  subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=96x64:rate=4:duration=2','-c:v','mpeg4','-g','2',str(video)],check=True)
  subprocess.run([str(command),str(video),'--json','detailed','--quiet'],cwd=tmp,env=env,check=True)
  subprocess.run([str(python),'-c',"from pathlib import Path;from pypdf import PdfReader;from slide_core.keyframe_json import loads;p=Path('smoke.slides.pdf');loads(p.with_suffix('.json').read_bytes(),pdf_bytes=p.read_bytes(),pdf_page_count=len(PdfReader(p).pages))"],cwd=tmp,env=env,check=True)
 (out/'source-manifest.json').write_bytes((root/'source-manifest.json').read_bytes())
 provenance=dict(repository=REPOSITORY,commit=sha,source_repository=manifest['source_repository'],source_commit=manifest['source_commit'],
                 version=version,artifacts={p.name:digest(p.read_bytes()) for p in (wheel,sdist)})
 (out/'provenance.json').write_text(json.dumps(provenance,indent=2)+'\n')
 assets=[wheel,sdist,out/'source-manifest.json',out/'provenance.json']
 sums=out/'SHA256SUMS'
 sums.write_text(''.join(digest(p.read_bytes())+'  '+p.name+'\n' for p in assets))
 assets.append(sums)
 if publish:
  notes=out/'RELEASE_NOTES.md'
  notes.write_text('Shared-engine CLI '+version+' prerelease.\n\nSource: '+manifest['source_repository']+'/commit/'+manifest['source_commit']+'\n\nInstall the wheel with Python 3.12+. FFmpeg/FFprobe are external requirements; qpdf is optional. Qt and the native decoder helper are not bundled. See README migration notes for changed commands/output/JSON defaults.\n')
  subprocess.run(['gh','release','create','v'+version,'--repo',REPOSITORY,'--target',sha,'--prerelease','--title','Slide Extractor CLI '+version,'--notes-file',str(notes),*[str(p) for p in assets]],check=True)
 print(json.dumps(provenance))

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--publish',action='store_true')
 build(Path(__file__).resolve().parents[1],parser.parse_args().publish)
