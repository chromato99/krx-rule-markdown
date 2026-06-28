# KRX Rule Markdown

한국거래소 법무포털의 공개 최신 규정과 규정 제·개정예고를 수집해 AI/RAG가 읽기 쉬운 Markdown corpus로 만드는 프로젝트입니다. MCP 서버나 검색 인덱스 생성은 이 레포의 책임이 아니며, 생성된 corpus는 [`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp) 같은 별도 런타임이 읽어서 사용합니다.

## 제공 기능

- KRX 법무포털 공개 규정과 규정 제·개정예고 수집
- 가능한 경우 규정의 영문전문 파일 다운로드 및 Markdown 변환
- 한국어/영문 corpus를 `ko/`, `en/` 디렉터리로 분리
- 별표·서식·첨부 원본 보존
- HWP/HWPX/PDF/HTML 첨부의 Markdown 텍스트 변환
- HWP EqEdit 수식 원본 보존 및 RAG 참조용 LaTeX(best-effort) 블록 생성
- 변환 품질 점검과 metadata 반영
- corpus 참조 무결성 검증
- 더 이상 참조하지 않는 첨부 산출물 정리

## 설치

```bash
python3 -m pip install -e ".[convert]"
```

`[convert]` extra는 PDF/HWP 변환 라이브러리를 포함합니다. 변환 없이 파서와 검증 코드만 다룰 때는 `python3 -m pip install -e .`도 가능합니다.

## Corpus 생성

```bash
krx-rule-markdown sync --all --data-dir data
krx-rule-markdown clean --data-dir data --drop-past-rule-attachments --prune-unreferenced-attachments
krx-rule-markdown quality --data-dir data --output data/reports/data-quality.json --update-metadata
krx-rule-markdown validate --data-dir data --quality
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

## HWP 수식 변환 정책

HWP 첨부에서 EqEdit 수식 블록을 찾으면 변환 Markdown 끝에 `## HWP 수식` 섹션을 추가합니다. 이 섹션은 RAG가 놓치기 쉬운 수식을 명시적으로 읽을 수 있도록 다음 두 블록을 항상 함께 제공합니다.

- `hwp-equation`: HWP EqEdit 원본 수식
- `math`: Markdown/RAG 참조용 LaTeX 자동 변환 결과

LaTeX는 `best-effort` 변환입니다. `over`, 첨자/윗첨자, `sum`, `prod`, `sqrt`, `hat`, `bar`, `LEFT/RIGHT`, `cases`, `eqalign`, `GEQ/LEQ/NEQ`, 한국어 텍스트 래핑 같은 KRX 첨부에서 확인된 주요 패턴을 변환하지만, 원본 HWP 렌더링과 100% 동일하다는 법적·수학적 보증은 하지 않습니다. 그래서 각 문서에는 “수식을 인용하거나 검증할 때는 원본 HWP 수식과 LaTeX 변환을 함께 참조하라”는 안내문이 함께 들어갑니다.

RAG 사용자는 LaTeX 블록을 우선 읽어도 되지만, 답변 근거를 엄밀하게 확인할 때는 바로 위의 `hwp-equation` 원본도 함께 확인해야 합니다. 변환기가 원본에서 닫히지 않은 괄호 같은 불완전한 EqEdit 스크립트를 만나면 LaTeX가 깨지지 않도록 보정할 수 있습니다.

## 산출물 구조

```text
data/
  ko/
    rules/
      <규정-제목>/
        index.md           # 한국어 최신 규정 Markdown
        raw/               # 이 규정의 원본 첨부
        attachments/       # 이 규정의 변환 Markdown 첨부
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
```

각 Markdown frontmatter에는 `language: "ko"` 또는 `language: "en"`이 들어갑니다. 영문 규정 문서는 한국어 규정과 구분되는 `{한국어 id}-en` id를 사용하고, `source_id`로 원 한국어 규정 id를 보존합니다.
규정/예고의 별표, 서식, 첨부는 해당 문서 디렉터리 안에 함께 저장되므로 RAG 처리 시 본문과 부속 문서를 한 단위로 추적할 수 있습니다.
HWP 첨부에 수식이 있으면 변환된 첨부 Markdown의 `## HWP 수식` 섹션에 원본 EqEdit와 LaTeX(best-effort)가 나란히 저장됩니다.

`data/index`는 이 프로젝트가 만들지 않습니다. BM25/vector index는 [`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp)의 `krx-rule-index`가 이 corpus를 입력으로 받아 생성합니다.

## Corpus 배포

운영 환경에서는 생성된 `data/`를 별도 경로에 복사하거나 CI artifact, release asset, object storage, 서버 볼륨 등으로 전달하세요. 예시는 다음과 같습니다.

```bash
export KRX_RULE_DATA_DIR=/opt/krx-rule-data
mkdir -p "$KRX_RULE_DATA_DIR"
rsync -a data/ "$KRX_RULE_DATA_DIR"/
```

[`krx-rule-mcp`](https://github.com/chromato99/krx-rule-mcp)는 위 경로를 읽기 전용 corpus 디렉터리로 사용하고, 필요한 경우 그 안의 `index/` 하위에 검색 snapshot을 생성합니다.

## 자동 갱신

`.github/workflows/sync.yml`는 정기적으로 corpus를 갱신하고 변경분이 있으면 PR을 생성합니다. workflow는 수집, 정리, 품질 점검, 검증까지만 수행하며 검색 index는 만들지 않습니다.

## 테스트

```bash
python3 -m unittest discover -s tests
```

실제 KRX 포털 접근이 필요한 장기/live 테스트는 기본 테스트에 포함하지 않습니다.

## 주의

이 프로젝트는 공개 문서를 수집해 개발 및 검색 보조용 corpus를 만드는 소프트웨어입니다. 규정 원문 데이터의 출처와 재배포 유의사항은 `docs/legal-notice.md`를 확인하세요.
