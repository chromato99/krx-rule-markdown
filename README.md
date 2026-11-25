# KRX Rule Markdown

한국거래소 법무포털의 규정·개정예고와 별표·서식을 수집해 Markdown으로 변환합니다. 저장소의 [`data/`](data/)에는 한국어 문서, 제공되는 영문 전문, 첨부 원본과 변환문이 들어 있습니다.

## 설치와 기존 데이터 확인

저장소 루트에서 Python 3.12 이상으로 설치합니다. `[convert]`에는 PDF·HWP 변환 및 품질 검사에 쓰는 의존성이 포함됩니다. 이미 수집된 데이터를 확인하려면 다음 두 명령으로 충분합니다.

```bash
python3 -m pip install -e ".[convert]"
krx-rule-markdown validate --data-dir data --release --quality
```

문서별 경로와 ID는 [매니페스트](data/manifest.json)에서, 변환 경고는 [품질 보고서](data/reports/data-quality.json)에서 확인할 수 있습니다.

## Corpus 생성

포털에서 한국어 규정·개정예고와 가능한 영문 전문을 다시 수집하려면 아래 순서로 실행합니다. 기존 `data/`를 갱신할 때는 같은 파일시스템에 임시 복사본을 만들 공간과 디렉터리 원자 교체를 지원하는 Linux 환경이 필요합니다.

```bash
krx-rule-markdown sync --all --data-dir data
krx-rule-markdown reconvert --data-dir data
krx-rule-markdown assets --data-dir data --download-inline
krx-rule-markdown pdf-comparisons --data-dir data --apply
krx-rule-markdown clean --data-dir data --drop-past-rule-attachments --prune-unreferenced-attachments
krx-rule-markdown quality --data-dir data --update-metadata --fail-on error
krx-rule-markdown validate --data-dir data --release --quality
```

`sync --all`은 첨부도 내려받습니다. `reconvert`는 저장된 원본을 다시 읽고, `assets`는 본문·HWP의 이미지를 보존하며, `pdf-comparisons`는 지원하는 신·구조문대비표를 복원합니다. `clean`은 현행 규정의 과거 개정자료와 참조가 끊긴 첨부 파일을 정리합니다. 마지막 두 명령으로 품질 메타데이터를 반영하고 배포할 데이터 전체를 검증합니다.

**한 건만 시험할 때**는 별도 디렉터리를 사용합니다. 한 언어만 갱신하려면 `sync --all`에 `--language ko` 또는 `--language en`을 추가합니다. 기존 데이터에 실행하면 선택한 언어의 수집 결과를 병합하고 다른 언어의 문서는 유지합니다.

```bash
sample_dir="$(mktemp -d)/data"
krx-rule-markdown sync --rule-id 210202173 --language ko --download-attachments --data-dir "$sample_dir"
krx-rule-markdown validate --data-dir "$sample_dir" --release --quality
```

## 기존 데이터 관리

| 하려는 일 | 방법 |
| --- | --- |
| 특정 문서 재변환 | `reconvert --document-id <id>`로 범위를 좁힙니다. 같은 원본과 변환기 버전의 첨부까지 다시 처리하려면 `--force`를 추가합니다. |
| 변경 전 확인 | `reconvert`, `assets`, `clean`은 `--dry-run`을 지원합니다. `pdf-comparisons`는 기본으로 분류 결과만 출력합니다. |
| 실패 원인 확인 | `sync`·`reconvert`의 최근 실행 기록은 데이터 디렉터리 옆의 `.krx-rule-runs/latest.json`에 있습니다. [품질 검사와 실패 처리](docs/data-quality.md) |
| 다른 서버에 배포 | 검증한 `data/` 디렉터리 전체를 대상 서버의 새 경로에 복사합니다. |

데이터 경로의 기본값은 `KRX_DATA_DIR`(미설정 시 `data`), 수집 언어는 `KRX_SYNC_LANGUAGE`(기본 `all`)로 정합니다. `KRX_QUALITY_REPORT`로 품질 보고서 경로를 바꿀 수 있습니다. [데이터 형식과 변환 규칙](docs/data-format.md) · [원문 이용 안내](docs/legal-notice.md)

테스트는 `python3 -m unittest discover -s tests`로 실행합니다. CI는 push와 PR에서 설치·구문·단위 테스트와 CLI 동작을 검사합니다. 포털 수집 자동화는 비활성화되어 있습니다. [CI 설정](.github/workflows/ci.yml) · [수집 워크플로](.github/workflows/sync.yml.disabled)
