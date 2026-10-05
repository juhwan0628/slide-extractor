from pathlib import Path
from PIL import Image, ImageDraw
import slide_extractor.duplicates as dup


def mk(path: Path, kind: int):
    im=Image.new("RGB",(160,90),"white")
    d=ImageDraw.Draw(im)
    if kind==1: d.rectangle((15,15,70,60),fill="black")
    if kind==2: d.ellipse((80,20,140,75),fill="black")
    im.save(path)
    return path


def test_revisited_slide_is_omitted_after_first_occurrence(tmp_path: Path):
    assert hasattr(dup,"filter_duplicate_slides")
    a=mk(tmp_path/"a.png",1); b=mk(tmp_path/"b.png",2); a2=mk(tmp_path/"a2.png",1)
    slides=[dup.SlideImage(a,0),dup.SlideImage(b,1),dup.SlideImage(a2,2)]
    out=dup.filter_duplicate_slides(slides,6)
    assert [s.path.name for s in out] == ["a.png","b.png"]


def test_default_off_preserves_a_b_a_and_mapping(tmp_path):
    slides = [dup.SlideImage(mk(tmp_path / f"{i}.png", kind), i)
              for i, kind in enumerate([1, 2, 1])]
    result = dup.deduplicate_slides(slides)
    assert result.slides == slides
    assert [m.pdf_page for m in result.mappings] == [1, 2, 3]
    assert all(m.duplicate_of is None for m in result.mappings)


def test_phash_collision_never_deletes_default_or_adjacent(tmp_path, monkeypatch):
    import imagehash
    import numpy as np
    monkeypatch.setattr(imagehash, "phash", lambda im: imagehash.ImageHash(np.zeros((8, 8), bool)))
    slides = [dup.SlideImage(mk(tmp_path / f"{i}.png", kind), i)
              for i, kind in enumerate([1, 2])]
    assert len(dup.deduplicate_slides(slides).slides) == 2
    assert len(dup.deduplicate_slides(slides, "adjacent").slides) == 2
    assert len(dup.deduplicate_slides(slides, "legacy-global").slides) == 1


def test_adjacent_exact_duplicates_merge_but_revisit_remains(tmp_path):
    slides = [dup.SlideImage(mk(tmp_path / f"{i}.png", kind), i)
              for i, kind in enumerate([1, 1, 1, 2, 1])]
    result = dup.deduplicate_slides(slides, "adjacent")
    assert [s.timestamp_s for s in result.slides] == [0, 3, 4]
    assert [m.pdf_page for m in result.mappings] == [1, 1, 1, 2, 3]
    assert [m.duplicate_of for m in result.mappings] == [None, 1, 1, None, None]
    assert result.mappings[1].reason == "adjacent_exact_pixels"


def test_explicit_legacy_global_matches_historical_filter(tmp_path):
    slides = [dup.SlideImage(mk(tmp_path / f"{i}.png", kind), i)
              for i, kind in enumerate([1, 2, 1])]
    result = dup.deduplicate_slides(slides, "legacy-global")
    assert result.slides == dup.filter_duplicate_slides(slides, 6)
    assert result.mappings[-1].duplicate_of == 1
    assert result.mappings[-1].phash_distance == 0
