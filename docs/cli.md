# CLI 설치와 사용법

단일 영상과 여러 영상의 순차 처리를 지원한다. 기본 작업 폴더에서 다음처럼 실행한다.

```bash
.venv/bin/python -m slide_cli lecture.mp4
.venv/bin/python -m slide_cli lecture.mp4 -o slides.pdf
.venv/bin/python -m slide_cli lecture.mp4 --json simple
.venv/bin/python -m slide_cli lecture.mp4 --roi full --quiet
.venv/bin/python -m slide_cli lecture.mp4 --roi 100,50,1280,720 --ignore-mask 1200,50,180,180
.venv/bin/python -m slide_cli lecture.mp4 --result-json
```

기본 출력은 입력과 같은 폴더의 lecture.slides.pdf다. 출력 확장자는 소문자 .pdf를 사용한다.
--json simple 또는 --json detailed를 지정하면 같은 stem의 .json도 기존 엔진으로 함께 게시한다.
기존 출력은 보호하며 --overwrite로 명시적으로 덮어쓴다. PDF 단독 모드는 기존 JSON을 건드리지 않는다.
--roi auto가 기본이며 GUI 최초 ROI 제안과 같은 경로를 따른다. 검은 인트로 등 경고가 있을 때도 GUI와 같은 자동 분석 경로를 유지한다.
좌표는 회전과 SAR 보정 후 표시 영상 기준이다. --ignore-mask는 최대 한 개이며 검출에서 제외할 뿐 PDF를 가리지 않는다.
진행과 오류는 stderr, 최종 결과는 stdout이다. --quiet는 진행만 숨긴다.
--result-json은 타임라인 JSON과 별개인 실행 결과 요약을 출력한다. 오류 때도 published 및 출력 경로를 확인하면 이미 게시된 결과를 구분할 수 있다.
Ctrl+C는 실행 중인 디코더에 취소를 전달하고 캐시를 정리한다.
종료 코드는 성공 0, 처리 실패 1, 옵션·설정 오류 2, 게시 전 사용자 취소 130이다.
게시 후 검증이나 정리 실패는 1이며 published=true와 저장된 경로를 보고한다.

실행 환경은 Python 3.12+, 현재 requirements의 코어 의존성, FFmpeg/FFprobe가 필요하다.
코드 자체는 Qt를 import하지 않는다. 네이티브 도우미가 없는 현재 소스 실행은 기존 CPU 대체 경로를 사용하며 네이티브 GUI 설치본과 속도가 다를 수 있다.

## 설치

Python 3.12 이상의 가상환경에서 저장소 루트를 기준으로 설치한다.

```bash
python -m pip install .
slide-extractor lecture.mp4 --json detailed
```

빌드한 wheel 파일로도 `python -m pip install /절대경로/slide_extractor-0.5.0b2-py3-none-any.whl`처럼 설치할 수 있다. 가상환경을 활성화하지 않았다면 Mac/Linux에서는 `.venv/bin/slide-extractor`, Windows에서는 `.venv\Scripts\slide-extractor.exe`를 사용한다. 모듈 실행도 계속 지원한다.

이 배포 저장소는 CLI와 공통 코어만 포함하며 GUI 선택 설치는 제공하지 않는다. GUI 사용 및 개발은 원본 slide-extractor-gui 저장소를 이용한다.

FFmpeg/FFprobe가 PATH에 필요하다. qpdf는 선택적 PDF 압축이며 없어도 생성·검증이 가능하다. wheel에는 FFmpeg/FFprobe·qpdf·네이티브 도우미가 들어 있지 않다. 도우미가 없으면 H264도 CPU/all 정책으로 대체된다. 네이티브 도우미의 명시 경로는 SLIDE_NATIVE_KEYFRAME_HELPER 환경 변수로 지정할 수 있다. 일반 wheel CPU 성능과 도우미를 포함한 네이티브 GUI 성능을 동일하게 해석하지 않는다.

PyPI·GitHub 공개 릴리스는 이 설치 작업과 별도다.

## 여러 영상 순차 처리

```bash
.venv/bin/python -m slide_cli first.mp4 second.mp4 --output-dir slides --json detailed
.venv/bin/python -m slide_cli lectures/*.mp4 --output-dir slides --result-json
```

입력은 주어진 순서대로 하나씩 처리한다. 출력 폴더를 생략하면 각 영상 옆에 <stem>.slides.pdf를 저장한다. --output-dir은 단일 입력에도 사용할 수 있다. -o는 단일 입력에서만 허용하며 --output-dir과 동시에 지정할 수 없다. ROI·마스크·JSON 옵션은 배치 전체에 공통 적용된다.

하나가 실패해도 다음 영상은 계속 처리한다. 종료 코드는 전체 성공 0, 일부 실패 1, 배치 경로·옵션 오류 2, 사용자 취소 130이다. Ctrl+C로 취소하면 완료된 출력은 유지하며 이후 입력은 skipped로 기록한다.

--result-json은 단일 실행에서는 기존 결과 형식을 유지하고, 여러 입력에서는 status/succeeded/failed/skipped/results 필드를 가진 JSON 문서 하나를 출력한다. results는 입력 순서대로 input/output/status/error_code/published와 성공 시 출력 상세 정보를 담는다. status는 success/error/cancelled/skipped다. 게시 후 정리 오류는 error이면서 published=true일 수 있다.

중복 stem, 대소문자·유니코드 정규화(NFC/NFD)·심볼릭 링크·하드링크로 겹치는 출력 및 다른 입력을 덮어쓰는 출력은 분석 전에 전체 거부한다. JSON 동반 출력 경로도 검사한다. --overwrite로도 배치 내부 충돌은 허용하지 않는다. 대소문자만 다른 경로는 이식성을 위해 Linux에서도 보수적으로 충돌로 취급한다.

와일드카드는 셸에서 확장된 파일 목록만 지원한다. 프로그램 자체의 glob 확장·폴더 재귀 검색·병렬 처리는 포함하지 않는다.

Windows에서 한글 경로를 사용하는 JSON 리다이렉션과 자동화는 UTF-8 모드를 사용한다. PowerShell에서는 `$env:PYTHONUTF8="1"`을 지정한다. 배포 CI에도 동일하게 적용한다.
