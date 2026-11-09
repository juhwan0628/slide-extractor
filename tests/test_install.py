"""Verify installation from the CLI-only distribution, outside its source tree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest
from tests.cli_fixtures import video

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(scope='module')
def installed(tmp_path_factory):
 root=tmp_path_factory.mktemp('cli-wheel')
 wheels=root/'wheels';wheels.mkdir()
 build=subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation','-w',str(wheels),str(ROOT)],
                      capture_output=True,text=True,timeout=120)
 assert build.returncode==0,build.stdout+build.stderr
 wheel=next(wheels.glob('*.whl'))
 subprocess.run([sys.executable,'-m','venv',str(root/'venv')],check=True,timeout=40)
 bindir=root/'venv'/('Scripts' if os.name=='nt' else 'bin')
 python=bindir/('python.exe' if os.name=='nt' else 'python')
 env=dict(os.environ);env.pop('PYTHONPATH',None);env.pop('SLIDE_NATIVE_KEYFRAME_HELPER',None)
 result=subprocess.run([str(python),'-m','pip','install','-c',str(ROOT/'requirements-test.lock'),str(wheel)],
                       cwd=root,env=env,capture_output=True,text=True,timeout=180)
 assert result.returncode==0,result.stdout+result.stderr
 return root,wheel,python,bindir/('slide-extractor.exe' if os.name=='nt' else 'slide-extractor'),env

def test_cli_only_wheel(installed):
 _,wheel,_,_,_=installed
 with zipfile.ZipFile(wheel) as z:
  names=z.namelist()
  assert 'slide_core/pipeline.py' in names and 'slide_cli/cli.py' in names
  assert not any(n.startswith(('gui/','tests/','legacy_fps/','packaging/')) or n=='app.py' for n in names)
  metadata=z.read(next(n for n in names if n.endswith('/METADATA'))).decode()
  assert 'PySide6' not in metadata and 'Provides-Extra: gui' not in metadata

def test_installed_commands_without_gui(installed):
 root,_,python,command,env=installed
 result=subprocess.run([str(python),'-c',"import importlib.util;assert all(importlib.util.find_spec(x) is None for x in ['gui','app','PySide6','shiboken6'])"],
                       cwd=root,env=env,capture_output=True,text=True)
 assert result.returncode==0,result.stderr
 for args in ([str(command),'--help'],[str(command),'--version'],[str(python),'-m','slide_cli','--help']):
  result=subprocess.run(args,cwd=root,env=env,capture_output=True,text=True,timeout=20)
  assert result.returncode==0,result.stderr

def test_installed_video_and_detailed_json(installed):
 root,_,python,command,env=installed
 source=video(root/'lecture.mkv',codec='libx264')
 result=subprocess.run([str(command),str(source.path),'--json','detailed','--result-json','--quiet'],
                       cwd=root,env=env,capture_output=True,text=True,timeout=60)
 assert result.returncode==0,result.stderr
 summary=json.loads(result.stdout)
 assert summary['decoder_backend']=='cpu-keyframes' and summary['page_count']>0
 check=subprocess.run([str(python),'-c',
  "from pathlib import Path;from pypdf import PdfReader;from slide_core.keyframe_json import loads;p=Path('lecture.slides.pdf');loads(p.with_suffix('.json').read_bytes(),pdf_bytes=p.read_bytes(),pdf_page_count=len(PdfReader(p).pages))"],
  cwd=root,env=env,capture_output=True,text=True)
 assert check.returncode==0,check.stderr
 assert not list(root.glob('.slide-cli-*'))
