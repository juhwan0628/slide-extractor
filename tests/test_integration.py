from pathlib import Path
import subprocess
from PIL import Image, ImageDraw
import pymupdf
from slide_extractor.cli import run_pipeline


def make_frame(path: Path, kind: int):
    im=Image.new("RGB",(320,180),"black")
    d=ImageDraw.Draw(im)
    d.rectangle((20,10,299,169),fill="white")
    if kind==1:
        d.rectangle((50,40,120,120),fill="black")
    elif kind==2:
        d.ellipse((150,35,250,135),fill="black")
    elif kind==3:
        d.polygon([(80,140),(160,30),(250,140)],fill="black")
    elif kind==9:
        d.line((30,20,290,160),fill="red",width=8)
    im.save(path)


def test_end_to_end_three_unique_slides(tmp_path: Path):
    seq=[(1,3),(9,1),(1,2),(2,3),(3,3)]
    concat=tmp_path/"list.txt"
    lines=[]
    for i,(kind,dur) in enumerate(seq):
        p=tmp_path/f"s{i}.png"; make_frame(p,kind)
        lines += [f"file '{p}'",f"duration {dur}"]
    lines.append(f"file '{tmp_path / f's{len(seq)-1}.png'}'")
    concat.write_text("\n".join(lines))
    video=tmp_path/"lecture.mp4"
    subprocess.run(["ffmpeg","-loglevel","error","-y","-f","concat","-safe","0","-i",str(concat),"-vf","fps=10,format=yuv420p",str(video)],check=True)
    out=tmp_path/"slides.pdf"
    result=run_pipeline(video,out,1.0,None,False)
    doc=pymupdf.open(out)
    assert len(doc)==3
    assert result.roi.x == 20
    assert result.roi.y == 10
    assert result.final_page_count == 3


def test_end_to_end_writes_timeline_json_next_to_pdf(tmp_path: Path):
    seq=[(1,3),(2,3),(3,3)]
    concat=tmp_path/"timeline-list.txt"
    lines=[]
    for i,(kind,dur) in enumerate(seq):
        p=tmp_path/f"timeline-s{i}.png"; make_frame(p,kind)
        lines += [f"file '{p}'",f"duration {dur}"]
    lines.append(f"file '{tmp_path / f'timeline-s{len(seq)-1}.png'}'")
    concat.write_text("\n".join(lines))
    video=tmp_path/"timeline-lecture.mp4"
    subprocess.run(["ffmpeg","-loglevel","error","-y","-f","concat","-safe","0","-i",str(concat),"-vf","fps=10,format=yuv420p",str(video)],check=True)
    out=tmp_path/"timeline.slides.pdf"

    result=run_pipeline(video,out,1.0,None,False)

    timeline_path=tmp_path/"timeline.slides.json"
    assert result.timeline_path == timeline_path
    assert timeline_path.exists()
    import json
    payload=json.loads(timeline_path.read_text(encoding="utf-8"))
    assert payload["source"] == video.name
    assert payload["slides"]
    assert payload["slides"][-1]["end_s"] == payload["duration_s"]
    assert len(payload["events"]) == result.accepted_transition_count


def test_default_pipeline_keeps_revisited_states_and_diagnostics(tmp_path):
    import json
    concat = tmp_path / "revisit-list.txt"
    lines = []
    for i, kind in enumerate([1, 2, 1]):
        path = tmp_path / f"revisit-{i}.png"
        make_frame(path, kind)
        lines += [f"file '{path}'", "duration 3"]
    lines.append(f"file '{path}'")
    concat.write_text("\n".join(lines))
    video = tmp_path / "revisit.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-vf", "fps=10,format=yuv420p", str(video)], check=True)
    out = tmp_path / "revisit.pdf"
    result = run_pipeline(video, out, 1.0, None, False, diagnostics_path=True)
    payload = json.loads(result.timeline_path.read_text())
    diagnostic = json.loads(out.with_suffix(".diagnostics.json").read_text())
    with pymupdf.open(out) as doc:
        assert len(doc) == result.final_page_count == len(payload["slides"]) == 3
        assert len(doc) == result.accepted_transition_count + 1
        assert [s["pdf_page"] for s in payload["slides"]] == [1, 2, 3]
    assert diagnostic["settings"]["dedup_mode"] == "off"
    assert len(diagnostic["state_mapping"]) == 3
    assert any(s["accepted"] and s["triggers"] for s in diagnostic["samples"])
