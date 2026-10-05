# Slide Extractor

CPU만으로 강의 영상에서 **슬라이드 변경을 감지하고, 슬라이드 PDF와 타임라인 JSON을 생성하는 도구**입니다.

정적인 프레젠테이션 화면이 중심인 강의 영상을 대상으로 하며, 전체 화면 변화뿐 아니라 bullet 추가처럼 작은 국소 변화도 가능한 한 별도 상태로 보존하는 것을 목표로 합니다.

## 주요 기능

- 슬라이드 영역(ROI) 자동 감지
- 1 FPS 기반 temporal slide-change detection
- 작은 국소 변화 감지를 위한 tile-level 비교
- 일시적인 전환 프레임을 줄이기 위한 persistence 검사
- 감지된 시간 상태를 보존하는 PDF 생성
- slide start/end와 대표 프레임 시점을 기록하는 JSON timeline
- 선택적 representative-frame export
- 진단용 transition score JSON
- 과거 detector / pHash dedup 동작을 위한 legacy compatibility mode

기본 설정은 **recall 우선**입니다. 감지된 상태를 전역 pHash dedup으로 다시 제거하지 않으며, A → B → A처럼 같은 슬라이드로 돌아오는 경우도 시간 상태를 각각 보존합니다.

## 처리 흐름

```text
lecture video
  ↓
slide ROI detection
  ↓
1 FPS sampling
  ↓
local / global change detection
  ↓
temporal persistence check
  ↓
representative frame selection
  ↓
optional deduplication
  ↓
PDF + timeline JSON
```

비교 단계에서는 축소된 grayscale 이미지를 사용하고, 최종 PDF용 대표 프레임은 원본 영상 해상도에서 다시 추출합니다.

## 요구 사항

- Python 3.12+
- FFmpeg / ffprobe
- qpdf

Ubuntu / Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg qpdf
```

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

개발 환경:

```bash
pip install -e '.[test]'
```

## 빠른 사용법

```bash
python -m slide_extractor lecture.mp4
```

기본 출력:

```text
output/lecture.slides.pdf
output/lecture.slides.json
```

출력 경로 지정:

```bash
python -m slide_extractor lecture.mp4 --output slides.pdf
```

자동 ROI가 맞지 않을 때:

```bash
python -m slide_extractor lecture.mp4 --roi 100,20,1280,720
```

샘플링 속도:

```bash
python -m slide_extractor lecture.mp4 --sample-fps 0.5
```

현재 허용 범위는 0.5–1.0 FPS이며 기본값은 1.0 FPS입니다.

대표 프레임 export:

```bash
python -m slide_extractor lecture.mp4 --export-frames
```

각 감지 구간의 대표 이미지가 `<output-stem>.frames/`에 저장되고 JSON의 각 slide 항목에 `frame` 경로가 기록됩니다.

진단 정보 저장:

```bash
python -m slide_extractor lecture.mp4 --diagnostics
python -m slide_extractor lecture.mp4 --diagnostics output/scores.json
```

## 기본 detector

기본값:

```text
detector-mode = local
dedup-mode    = off
sample-fps    = 1.0
```

local detector는 폭 320px로 축소한 grayscale 이미지를 기준으로 **직전 샘플**과 **현재 anchor state**를 비교합니다.

후보 변화는 다음 조건을 사용합니다.

```text
changed_fraction >= 0.0002
AND
(
  global MAD >= 0.005
  OR
  max 20×20 tile MAD >= 0.025
)
```

`changed_fraction`은 밝기 차이가 20을 넘는 픽셀의 비율입니다. tile은 stride 10px로 겹치게 검사합니다.

후보가 발견되면 기본 2-sample persistence를 확인합니다. 다음 샘플에서도 후보 프레임과 같은 상태로 유지될 때 transition으로 승인하며, anchor로 즉시 돌아오는 변화는 transient로 간주합니다.

영상 끝에서는 미래 샘플이 부족한 안정 후보만 recall을 위해 보존할 수 있습니다.

## Deduplication

`--dedup-mode`:

- `off` — 기본값. 감지된 모든 시간 상태를 PDF에 보존
- `adjacent` — 인접 대표 이미지가 RGB 기준 완전히 동일할 때만 병합
- `legacy-global` — 과거 방식. 전체 이전 페이지와 pHash distance ≤ 6이면 제거

기본 `off`에서는 A → B → A가 **3개의 시간 상태 / 3개의 PDF 페이지**로 유지됩니다.

과거 동작을 재현하려면:

```bash
python -m slide_extractor lecture.mp4 \
  --detector-mode legacy \
  --dedup-mode legacy-global
```

## Timeline JSON

현재 timeline은 `schema_version: 2`를 사용합니다.

주요 정보:

- source video
- duration
- ROI
- detector / dedup 설정
- `pdf_page_count`
- 각 slide state의 `start_s`, `end_s`
- `representative_timestamp_s`
- `pdf_page`
- `duplicate_of`
- 선택적 representative-frame 경로

dedup을 사용하는 경우에도 시간 구간 자체는 JSON에서 유지됩니다.

## Diagnostics

local diagnostics에는 각 sample에 대해 다음 정보를 기록합니다.

- adjacent / anchor metrics
- global MAD
- max tile MAD와 위치
- changed-pixel fraction
- trigger 조건
- persistence count
- accepted 여부
- 판정 reason

threshold를 조정하거나 false positive / false negative를 분석할 때 사용할 수 있습니다.

## 검증 현황

현재 regression suite는 **65 tests**이며 local detector, legacy detector, timeline, deduplication, media sampling 및 end-to-end pipeline 동작을 검사합니다.

실제 강의 영상에서는 동일한 강의 시리즈 5개 영상에 대해 새 기본 설정을 검증했습니다.

| Video | Previous detected states | Current detected states |
| --- | ---: | ---: |
| Lecture 1 | 55 | 60 |
| Lecture 2 | 45 | 47 |
| Lecture 3 | 30 | 31 |
| Lecture 4 | 30 | 31 |
| Lecture 5 | 17 | 18 |

기존 global pHash dedup 때문에 PDF에서 일부 감지 상태가 다시 제거되던 동작은 기본 설정에서 제거했습니다.

### Generalization status

현재 실제 영상 benchmark의 대부분은 **동일한 강의 시리즈와 유사한 slide layout**에 기반합니다. 따라서 현재 threshold가 다양한 강의 스타일, screen recording, code demo, talking-head overlay, dark-theme deck 등에도 최적이라고 주장하지 않습니다.

별도의 짧은 영상에서는 pipeline과 transition detection이 동작하는 것을 확인했지만, 보다 폭넓은 영상 집합에 대한 정량 benchmark는 아직 필요합니다.

## 알려진 한계

- 기본 1 FPS 사이에 나타났다 사라지는 매우 짧은 build는 감지할 수 없습니다.
- 1-sample 변화는 transient로 제외될 수 있습니다.
- 커서, annotation, 오래 지속되는 animation처럼 실제 픽셀 상태가 변하는 요소는 별도 state로 감지될 수 있습니다.
- 자동 ROI는 화면 구성이 특이한 영상에서 실패할 수 있으며 이 경우 `--roi`를 직접 지정해야 합니다.
- 현재 threshold는 고정 heuristic이며 content-adaptive thresholding이나 OCR/VLM 추론은 사용하지 않습니다.
- 일부 비표준/편집 영상은 마지막 구간의 frame seeking 특성에 따라 대표 프레임 추출이 실패할 가능성이 있습니다.
- 결과 PDF는 이미지 기반입니다.

## 테스트

```bash
python -m pytest -q
```

현재 기준:

```text
65 passed
```

## 프로젝트 구조

```text
slide-extractor/
├── slide_extractor/
│   ├── cli.py
│   ├── compare.py
│   ├── config.py
│   ├── duplicates.py
│   ├── media.py
│   ├── models.py
│   ├── pdf.py
│   ├── roi.py
│   ├── timeline.py
│   └── transitions.py
├── tests/
├── output/
├── pyproject.toml
└── README.md
```

생성된 영상/PDF/JSON은 Git에 포함하지 않으며 `output/.gitkeep`만 추적합니다.
