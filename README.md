# FMIndex

FMIndex는 한국 개인투자자 커뮤니티의 게시글과 댓글에서 **시장 방향성, 감정, 행동 의도, 관심도 변화**를 추출하고 시간대별 지수로 집계하는 로컬 우선(local-first) 연구 프로젝트입니다.

첫 번째 데이터 원천은 FMKorea 주식 게시판이며, 공개 제품명은 **펨코지수**, 내부 엔진명은 **K-Retail Sentiment Engine**으로 사용합니다. 특정 사이트의 원문을 재배포하는 서비스가 아니라, 최소한의 수집과 파생 데이터 분석을 통해 시장 분위기의 변화를 관측하는 것이 목적입니다.

> 현재 단계: **Phase 0 — 데이터 접근성·수집 안정성·분류체계 검증**

## 1. 제품 목표

FMIndex MVP는 다음 질문에 답합니다.

1. 현재 게시판의 시장심리는 긍정·중립·부정 중 어디에 가까운가?
2. 15분·1시간 전보다 심리가 얼마나 변했는가?
3. 공포·탐욕·분노·체념 등 어떤 감정이 우세한가?
4. 어떤 시장·업종·종목의 언급량이 비정상적으로 증가했는가?
5. 게시판 심리와 실제 시장가격은 동행하는가, 괴리되는가?

## 2. 초기 비목표

초기 버전에서는 다음을 하지 않습니다.

- 매수·매도 추천 또는 자동매매
- 특정 종목의 수익률 보장이나 익일 가격 예측
- 작성자 개인의 성향·신뢰도 공개 평가
- 게시글·댓글 원문 검색 또는 원문 재배포
- 여러 커뮤니티의 동시 통합
- 공격적인 대량 크롤링이나 접근 제한 우회

## 3. MVP 산출물

- 15분 단위 시장심리지수(0~100)
- 공포·탐욕지수
- 관심도지수
- 의견분열지수
- 심리변화속도
- 시장가격 대비 심리괴리지수
- 수집·분석 상태를 확인하는 내부 운영화면
- 지수 산식과 모델 버전을 추적하는 감사 로그

## 4. 처리 흐름

```text
게시판 목록 감시
  → 신규 게시물 식별·중복 제거
  → 중요 게시물 본문 및 제한적 댓글 수집
  → 정규화·종목/시장 대상 연결
  → 규칙 기반 분석 + 로컬 LLM 구조화 분류
  → 시간대별 지수 계산
  → 시장 데이터 결합
  → 대시보드·연구 보고서
```

## 5. 기술 원칙

- **Local first:** 원문과 분석 파이프라인은 우선 로컬에서 실행합니다.
- **Incremental collection:** 전체 재수집이 아니라 신규·변경 데이터만 처리합니다.
- **Low request volume:** 목록 전수 관찰, 본문 선택 수집, 댓글 제한 수집의 3계층 구조를 사용합니다.
- **Reproducibility:** 동일 데이터·모델·프롬프트·산식 버전이면 동일 지수가 나와야 합니다.
- **Provider abstraction:** 로컬 LLM은 교체 가능한 공급자 인터페이스 뒤에 둡니다.
- **Derived-data product:** 장기 자산은 원문 복제본이 아니라 시간대별 파생지표와 검증 결과입니다.
- **No silent failure:** 누락, 429, 파싱 실패, 모델 오류, 지수 표본 부족을 화면과 로그에 표시합니다.

## 6. 저장소 구조

```text
fmindex/
├─ docs/               # 제품·운영·데이터 정책·라벨링·계보 문서
├─ fmindex/            # Python product package
│  ├─ dashboard/       # 정적 대시보드와 localhost 서버
│  ├─ fmkorea/         # FMKorea fixture/parser/live collector
│  ├─ llm/             # LLM provider abstraction and mock provider
│  ├─ market/          # 65stock bridge, Kiwoom client, KOSPI collector
│  ├─ fmindex_calc.py  # 시간대별 sentiment index 계산
│  ├─ market_join.py   # 시장 데이터와 FM index 결합
│  └─ pipeline.py      # one-shot pipeline runner
├─ scripts/            # Windows bootstrap/doctor scripts
├─ tests/              # offline contract tests
├─ package.json        # Node/pnpm command surface
├─ pnpm-workspace.yaml
└─ pyproject.toml
```

## 7. Windows 로컬 시작

현재 로컬 작업 경로:

```powershell
E:\fmindex260901
```

새로 복제하는 경우:

```powershell
cd E:\
git clone https://github.com/skerishKang/fmindex.git fmindex260901
cd .\fmindex260901
```

이미 폴더와 원격 저장소를 연결했다면:

```powershell
cd E:\fmindex260901
git remote -v
git pull origin main
```

통합 브랜치 또는 main 승격 후에는 다음 명령으로 준비와 검진을 실행합니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

권장 런타임 기준은 Node.js 24 LTS와 pnpm 11 계열입니다.

## 8. 현재 구현 상태 (canonical main lineage)

`fix/16-canonical-main-lineage` 브랜치는 검증된 모든 product 구현과 #11~#15 정합성 수정을 하나의 canonical 선으로 통합한 브랜치입니다. 향후 `main` 승격 시 기초가 됩니다.

### 구현 완료

| 영역 | 구현 내용 |
|------|-----------|
| Python package | `fmindex` 패키지 (`fmindex_calc`, `market_join`, `pipeline`, `dashboard`, `fmkorea`, `market`, `llm`) |
| Dashboard UI | Chart.js 기반 정적 대시보드 (light/dark 테마, 코스피·나스닥 전환, 기간 필터) |
| KOSPI collector | Kiwoom/65stock 시간당 OHLC 수집, 개별 종목 reject, 하드 요청 예산 |
| FMKorea live collector | 저빈도 수집, 하드 안전 경계 (`request_delay >= 3.0`, `max_posts <= 3`) |
| Exact-head CI | GitHub Actions 정합성 검증 + `git diff --check` 게이트 |
| postCount semantics | FM bucket이 없으면 `postCount=0`, sentiment 없어도 `postCount` 보존 |

### 보류 / 비목표

- 투자 자문·매매 추천 (MVP 범위 외)
- 공격적 대량 크롤링 (low-frequency only)
- auth bypass / CAPTCHA bypass
- real LLM provider productionization (MockLLMProvider 사용)
- NASDAQ 실시간 데이터 피드 (샘플 데이터 사용)

### 저장소 체계

통합된 변경사항은 `docs/REPOSITORY_LINEAGE.md`에서 확인할 수 있습니다.

## 9. 운영상 필수 기록

모든 수집·분석 실행은 최소 다음 정보를 기록해야 합니다.

- 수집 시작·종료 시각과 데이터 원천
- 요청 수, 성공 수, 실패 수, 429 발생 수
- 신규·중복·수정·삭제 감지 건수
- 모델 이름, 모델 버전, 프롬프트 버전
- 스키마 검증 실패와 재처리 결과
- 지수 산식 버전과 유효 표본 수
- 원문 보존 만료 시각

## 10. 데이터·법적 원칙

- 공개 접근 가능 여부와 별개로 사이트 정책, 저작권, 데이터베이스 권리, 개인정보 문제를 검토합니다.
- 접근 제한 우회, 인증 우회, CAPTCHA 우회, 과도한 병렬 요청을 구현하지 않습니다.
- 닉네임 등 작성자 식별자는 외부 화면에 표시하지 않으며 내부 저장 시에도 해시 또는 최소화 원칙을 적용합니다.
- 원문은 분석·검증 목적의 제한된 기간만 보관하고 장기적으로 파생 데이터와 해시를 중심으로 보존합니다.
- 외부 공개 전에는 출처별 수집 범위와 이용 조건을 다시 검토합니다.

## 11. 문서

현재 주요 문서는 다음과 같습니다.

- `docs/PRODUCT.md` — 제품 범위와 성공 기준
- `docs/ARCHITECTURE.md` — 시스템 경계와 데이터 흐름
- `docs/OPERATIONS.md` — 수집·분석 운영 절차와 장애 대응
- `docs/DATA_POLICY.md` — 원문·식별자·파생 데이터 보존 원칙
- `docs/LABELING_GUIDE.md` — 사람 라벨링 기준
- `docs/LOCAL_SETUP.md` — Windows 로컬 설치 절차
- `docs/ROADMAP.md` — Phase 0부터 공개 MVP까지의 단계
- `docs/REPOSITORY_LINEAGE.md` — stacked PR 정리와 canonical main 승격 계보

## 12. 현재 승인 기준

Phase 0는 다음 조건을 충족해야 종료합니다.

- 데이터 원천 구조와 요청 실패 양상을 문서화했다.
- 저빈도 증분 수집으로 신규 게시물을 안정적으로 식별한다.
- 동일 게시물을 반복 저장하지 않는다.
- 수집 누락률과 실패율을 측정할 수 있다.
- 원문 보존·삭제 정책이 구현 가능한 수준으로 정의됐다.
- 라벨링 대상과 분석 스키마가 확정됐다.

## 13. 면책

FMIndex는 시장심리 연구·정보 제공 프로젝트입니다. 지수와 분석 결과는 투자 권유, 투자자문, 매매 신호 또는 수익 보장을 의미하지 않습니다.
