# KRX Rule Markdown

한국거래소 법무포털의 공개 최신 규정과 규정 제·개정예고를 수집해 AI/RAG가 읽기 쉬운 Markdown corpus로 만드는 프로젝트입니다. MCP 서버나 검색 인덱스 생성은 이 레포의 책임이 아니며, 생성된 corpus는 [`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp) 같은 별도 런타임이 읽어서 사용합니다.

## 제공 기능

- KRX 법무포털 공개 규정과 규정 제·개정예고 수집
- 가능한 경우 규정의 영문전문 파일 다운로드 및 Markdown 변환
- 한국어/영문 corpus를 `ko/`, `en/` 디렉터리로 분리
- 별표·서식·첨부 원본 보존
- HWP/HWPX/PDF/HTML 첨부의 Markdown 텍스트 변환
- HWP/HWPX 표 구조를 가능한 경우 Markdown/HTML table로 보존
- HWP EqEdit 수식 원본 보존 및 RAG 참조용 LaTeX(best-effort) 블록 생성
- KRX inline 이미지와 HWP BinData 이미지를 hash·MIME·dimensions가 있는 bundle asset으로 보존
- 확인된 amendment comparison PDF template을 좌표 기반 현행·개정안 표로 복원
- 수집 시점의 정제된 source HTML과 비밀값을 제거한 request descriptor 보존
- 변환 품질 점검과 metadata 반영
- 본문·원본·변환 텍스트별 SHA-256과 release hash 검증
- 단일 writer lock, staging 검증, generation 단위 원자 publish
- 갱신 실패 시 기존 정상 raw/text를 유지하는 last-known-good 정책
- 더 이상 참조하지 않는 첨부 산출물 정리

## 설치

```bash
python3 -m pip install -e ".[convert]"
```

`[convert]` extra는 PDF/HWP 변환 라이브러리를 포함합니다. 변환 없이 파서와 검증 코드만 다룰 때는 `python3 -m pip install -e .`도 가능합니다.

## Corpus 생성

```bash
krx-rule-markdown sync --all --data-dir data
krx-rule-markdown reconvert --data-dir data
krx-rule-markdown assets --data-dir data --download-inline
krx-rule-markdown pdf-comparisons --data-dir data --apply
krx-rule-markdown clean --data-dir data --drop-past-rule-attachments --prune-unreferenced-attachments
krx-rule-markdown quality \
  --data-dir data \
  --output data/reports/data-quality.json \
  --update-metadata \
  --fail-on error
krx-rule-markdown validate --data-dir data --release --quality
```

`sync`는 기본적으로 한국어 규정/예고와 가능한 영문 규정 전문을 함께 수집합니다. 필요한 경우 언어를 제한할 수 있습니다.

```bash
krx-rule-markdown sync --all --language ko --data-dir data  # 한국어 규정/예고만
krx-rule-markdown sync --all --language en --data-dir data  # 영문전문이 있는 규정만
```

특정 규정 하나만 확인하려면:

```bash
krx-rule-markdown sync --rule-id 210203562 --download-attachments --data-dir /tmp/krx-rule-smoke
krx-rule-markdown sync --rule-id 210203562 --language en --data-dir /tmp/krx-rule-smoke-en
krx-rule-markdown validate --data-dir /tmp/krx-rule-smoke --quality
```

단일 규정 smoke test는 위처럼 임시 디렉터리에서 실행하세요. 기존 전체 corpus 디렉터리에서는 부분 sync 결과와 정리 명령 조합에 따라 의도하지 않은 문서 삭제 위험이 생길 수 있습니다.

이미 받은 raw 첨부를 새 변환 로직으로 다시 Markdown화하려면 `reconvert`를 사용합니다. HWP/HWPX 표·수식 변환 로직을 개선한 뒤 기존 corpus에 반영할 때 유용합니다.

PDF는 좌표 읽기 순서를 사용해 가운데 정렬된 장 제목이 조문 제목 뒤로 이동하지 않게 합니다. PDF 변환 캐시는 `2+pdf-coordinate-order` 식별자를 사용하므로 일반 `reconvert`로 이전 PDF를 갱신할 수 있으며, HWP 변환 캐시는 유지합니다. 원본의 장→조문→본문 순서를 회귀 검사하고, 신·구조문대비표는 기존 좌표 grid 복원을 사용합니다. 재변환 환경에는 `[convert]`의 PDF와 HWP 의존성을 모두 설치해야 전체 release 품질 검사를 통과할 수 있습니다.

```bash
krx-rule-markdown reconvert --data-dir data
krx-rule-markdown reconvert --data-dir data --document-id 210217137
```

`sync`, 실제 변경을 수행하는 `reconvert`·`clean`, `quality --update-metadata`는 활성 corpus를 직접 고치지 않습니다. 같은 파일시스템의 sibling staging generation을 만든 뒤 release 검증을 통과한 경우에만 Linux `renameat2(RENAME_EXCHANGE)`로 전체 디렉터리를 교체합니다. 동시에 두 writer를 실행하면 두 번째 작업은 즉시 실패합니다. `sync` 중 일부 refresh가 실패하면 기존 정상 raw/text를 유지하고 실행 리포트와 deterministic `stale_due_to_refresh_failure` 진단을 남길 수 있습니다. `reconvert` 중 새 실패가 발생하면 staging generation 전체를 폐기하므로 active release에는 새 stale 진단이 기록되지 않고, 상세 실패 정보만 release 밖 실행 리포트에 남습니다.

`--dry-run`은 corpus, manifest, 품질 리포트와 실행 리포트를 변경하지 않습니다. 정기 release에서는 `validate --release --quality`를 사용하고, 원본은 보존되었지만 의도적으로 검색에서 제외할 실패 항목만 검토된 ID를 `--allow-failure-id`로 명시하세요.

`assets`는 HWP의 실제 JPEG/BMP/PNG/GIF BinData만 검사해 bundle의 `assets/`에 보존합니다. `--download-inline`을 주면 본문의 KRX `/dataFile/law/img/` URL도 같은 host·redirect, MIME/signature, byte·pixel 제한 아래 다운로드합니다. 본문에는 로컬 경로 대신 `krx-asset:<id>`만 남고 실제 경로와 bytes hash는 frontmatter metadata에만 기록됩니다. `pdf-comparisons`는 코드에 이름이 고정된 현재 7개 PDF만 분류하며, 좌표 grid와 header가 모두 확인된 template만 `--apply`로 복원합니다. 신뢰도 기준을 통과하지 못한 PDF는 원문을 추측해 재배열하지 않고 degraded로 남깁니다.

## HWP 수식 변환 정책

HWP 첨부에서 EqEdit 수식 블록을 찾으면 문단이나 표 안의 수식 placeholder 위치에 최대한 가깝게 LaTeX 참조를 삽입합니다. 원본 확인이 가능하도록 해당 문단 또는 표 근처에 다음 두 블록도 함께 제공합니다.

- `hwp-equation`: HWP EqEdit 원본 수식
- `math`: Markdown/RAG 참조용 LaTeX 자동 변환 결과

LaTeX는 `best-effort` 변환입니다. `over`, 첨자/윗첨자, `sum`, `prod`, `sqrt`, `hat`, `bar`, `LEFT/RIGHT`, `cases`, `eqalign`, `GEQ/LEQ/NEQ`, 한국어 텍스트 래핑 같은 KRX 첨부에서 확인된 주요 패턴을 변환하지만, 원본 HWP 렌더링과 100% 동일하다는 법적·수학적 보증은 하지 않습니다. 그래서 각 문서에는 “수식을 인용하거나 검증할 때는 원본 HWP 수식과 LaTeX 변환을 함께 참조하라”는 안내문이 함께 들어갑니다.

RAG 사용자는 원문 위치 근처의 LaTeX 참조를 우선 읽어도 되지만, 답변 근거를 엄밀하게 확인할 때는 근처의 `hwp-equation` 원본도 함께 확인해야 합니다. 변환기가 원본에서 닫히지 않은 괄호 같은 불완전한 EqEdit 스크립트를 만나면 LaTeX가 깨지지 않도록 보정할 수 있습니다. 원문 위치를 안정적으로 복원하지 못한 수식만 `## 위치 미확정 HWP 수식` 섹션으로 분리합니다.

## 표 변환 정책

HWPX 표는 행과 셀을 파싱해 일반 표는 Markdown table로 변환합니다. 병합 셀처럼 Markdown table로 표현하기 어려운 구조는 `rowspan`, `colspan`을 포함한 HTML table로 변환해 행/열 모양을 최대한 유지합니다.

HWP 파일은 `pyhwp` 모델에서 표 행/셀과 병합 정보를 읽어 Markdown table 또는 HTML table로 변환합니다. 모델 기반 복원이 실패한 경우에도 텍스트 추출 결과에서 `<셀><셀>` 형태로 드러나는 표 행은 Markdown table block으로 후처리합니다. 이 방식은 행/열 구조를 RAG가 읽기 쉽게 보존하기 위한 것이며, 원본 HWP의 픽셀 단위 너비, 테두리, 정렬, 셀 배경색까지 동일하게 재현하지는 않습니다.

HWPX 변환은 현재 `data/`에 검증 가능한 HWPX raw fixture가 없어 experimental입니다. ZIP entry 수·압축 해제 크기·압축 비율·암호화 여부를 먼저 제한하고, 파싱 가능한 문단과 표만 best-effort로 변환합니다. source-order나 복잡한 drawing/layout의 완전 복원을 보장하지 않으며, 실제 corpus fixture가 확보되기 전에는 stable 지원으로 간주하지 않습니다.

## 산출물 구조

```text
data/
  ko/
    rules/
      <규정-제목>/
        index.md           # 한국어 최신 규정 Markdown
        raw/               # 원본 첨부, 정제 source.html, request.json
        attachments/       # 이 규정의 변환 Markdown 첨부
        assets/            # hash로 검증되는 보존 이미지(검색 비대상)
    notices/
      <예고-제목>/
        index.md           # 한국어 규정 제·개정예고 Markdown
        raw/
        attachments/
  en/
    rules/
      <영문-규정-제목>/
        index.md           # 영문전문에서 변환한 영문 규정 Markdown
        raw/               # 영문전문 원본 파일
        attachments/       # 영문전문 변환 Markdown
  manifest.json          # 수집 manifest
  reports/               # 품질 리포트
../.krx-rule-runs/       # 응답 시각·실패 등 release 밖 실행 이력
```

각 Markdown frontmatter에는 `language: "ko"` 또는 `language: "en"`이 들어갑니다. 영문 규정 문서는 한국어 규정과 구분되는 `{한국어 id}-en` id를 사용하고, `source_id`로 원 한국어 규정 id를 보존합니다.
규정/예고의 별표, 서식, 첨부는 해당 문서 디렉터리 안에 함께 저장되므로 RAG 처리 시 본문과 부속 문서를 한 단위로 추적할 수 있습니다.
HWP 첨부에 수식이 있으면 가능한 경우 원문 문단의 placeholder 위치에 가까운 곳에 LaTeX(best-effort)가 삽입되며, 원본 EqEdit 블록은 해당 문단 또는 표 근처에 함께 저장됩니다. 원위치가 복원되지 않은 수식만 별도 위치 미확정 섹션으로 분리됩니다.

`data/index`는 이 프로젝트가 만들지 않습니다. BM25/vector index는 [`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp)의 `krx-rule-index`가 이 corpus를 입력으로 받아 생성합니다.

`manifest.json`의 `index_source_hash`는 검색 결과에 영향을 주는 canonical metadata와 텍스트를 묶은 producer/consumer 공통 해시입니다. `release_hash`는 manifest의 재현 가능한 release 내용 해시이며 `generated_at`, `last_checked_at`, 응답 시점 hash 같은 운영 필드는 제외합니다. 필드별 의미와 canonicalization은 [`docs/data-format.md`](docs/data-format.md)를 참고하세요.

## Corpus 배포

운영 환경에서는 생성된 `data/`를 별도 경로에 복사하거나 CI artifact, release asset, object storage, 서버 볼륨 등으로 전달하세요. 예시는 다음과 같습니다.

```bash
export KRX_RULE_DATA_DIR=/opt/krx-rule-data
mkdir -p "$KRX_RULE_DATA_DIR"
rsync -a data/ "$KRX_RULE_DATA_DIR"/
```

[`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp)는 위 경로를 읽기 전용 corpus 디렉터리로 사용하고, BM25/vector 검색 snapshot은 MCP 프로젝트가 소유하는 별도 index 디렉터리(`KRX_RULE_INDEX_DIR`)에 생성합니다.

## 환경변수

| 변수 | 사용처 | 기본값 |
|---|---|---|
| `KRX_DATA_DIR` | `sync`, `reconvert`, `clean`, `quality`, `validate`의 corpus 경로 | `data` |
| `KRX_SYNC_LANGUAGE` | `sync --language` 기본값 (`all`, `ko`, `en`) | `all` |
| `KRX_QUALITY_REPORT` | `quality --output` 기본 경로 | `data/reports/data-quality.json` |

## 자동화

`.github/workflows/ci.yml`는 push와 pull request에서 패키지 설치, 문법 검사, 단위 테스트, CLI smoke test만 수행합니다. 실제 KRX 포털 sync는 네트워크와 실행 시간이 필요한 작업이라 기본 CI에 포함하지 않습니다.

기존 sync workflow는 `.github/workflows/sync.yml.disabled`로 보존되어 있지만 현재 GitHub Actions에서는 실행되지 않습니다. 다시 자동 갱신을 켜려면 실패 원인을 수정한 뒤 `.github/workflows/sync.yml`로 되돌려 사용하세요.

## 테스트

```bash
python3 -m unittest discover -s tests
```

실제 KRX 포털 접근이 필요한 장기/live 테스트는 기본 테스트에 포함하지 않습니다.

## 주의

이 프로젝트는 공개 문서를 수집해 개발 및 검색 보조용 corpus를 만드는 소프트웨어입니다. 규정 원문 데이터의 출처와 재배포 유의사항은 `docs/legal-notice.md`를 확인하세요.
