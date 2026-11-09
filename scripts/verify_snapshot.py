"""Verify an exported CLI snapshot before building or publishing it."""
import hashlib
import json
from pathlib import Path

def verify(root):
 root=Path(root).resolve()
 manifest=json.loads((root/'source-manifest.json').read_text())
 expected=manifest['tracked_file_sha256']
 if not isinstance(expected,dict) or not expected:raise ValueError('Empty manifest')
 for name,digest in expected.items():
  path=Path(name)
  if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe manifest path')
  target=root/path
  if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(root):
   raise ValueError('Missing or unsafe file: '+name)
  if hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError('Hash mismatch: '+name)
 ignored={'.git','.venv','.pytest_cache','__pycache__','build','dist'}
 actual=set()
 for path in root.rglob('*'):
  rel=path.relative_to(root)
  if any(p in ignored or p.endswith('.egg-info') for p in rel.parts):continue
  if path.is_symlink():raise ValueError('Symlink in snapshot: '+str(rel))
  if path.is_file():actual.add(rel.as_posix())
 if actual != set(expected)|{'source-manifest.json'}:raise ValueError('Unexpected or missing snapshot files')
 return manifest

if __name__=='__main__':
 m=verify(Path(__file__).resolve().parents[1])
 print('Snapshot verified:',m['source_commit'])
