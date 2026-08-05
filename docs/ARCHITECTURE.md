# FMIndex 시스템 아키텍처

## 1. 아키텍처 목표

FMIndex는 다음 조건을 만족해야 한다.

- 로컬 환경에서 전체 파이프라인을 실행할 수 있다.
- 데이터 원천, LLM, 저장소, 시장 데이터 공급자를 교체할 수 있다.
- 같은 입력과 버전으로 같은 결과를 재현할 수 있다.
- 수집 실패와 분석 실패가 지수 값으로 조용히 섞이지 않는다.
- 원문 보존 기간과 파생 데이터 보존 기간을 분리한다.
- 공개 대시보드가 원문 저장소에 직접 접근하지 않는다.

## 2. 상위 구성요소

```text
[Source Adapter]
      ↓
[Collector]
      ↓ raw event
[Normalizer]
      ↓ normalized document
[Analyzer]
      ↓ analysis result
[Index Engine]
      ↓ aggregate snapshot
[API]
      ↓
[Public Dashboard / Admin Console]
```

보조 구성요소:

- Scheduler: 수집·재처리·집계 실행
- Market Data Adapter: 승인된 가격·거래량 데이터 공급자 연결
- Review Queue: 낮은 확신도·스키마 실패·신규 은어 검수
- Audit Log: 실행 버전·입력 해시·출력·오류 기록
- Retention Worker: 원문 만료·삭제·파생 데이터 보존

## 3. 컴포넌트 경계

### 3.1 Source Adapter

데이터 원천별 DOM, URL, 페이지네이션, 시간 표기, 게시물 ID 규칙을 캡슐화한다.

필수 인터페이스 예시:

```ts
interface SourceAdapter {
  sourceId: string;
  fetchListing(cursor?: string): Promise<ListingPage>;
  fetchPost(postRef: SourcePostRef): Promise<RawPost>;
  fetchComments?(postRef: SourcePostRef): Promise<RawComment[]>;
  normalizeSourceTime(value: string): Date;
}
```

어댑터는 지수 산식이나 감성분석을 알지 못해야 한다.

### 3.2 Collector

- 목록 페이지 관찰
- 신규 게시물 ID 식별
- 중복 제거
- 선택적 본문·댓글 수집
- 요청량 제한, 지연, 백오프, 회로 차단기
- 원본 응답 메타데이터 기록

Collector는 다음 상태를 명시적으로 가진다.

```text
DISABLED
HEALTHY
DEGRADED
BACKING_OFF
STOPPED_BY_429
STOPPED_BY_POLICY
FAILED
```

### 3.3 Normalizer

- HTML 제거와 텍스트 정규화
- 게시글·댓글 구조 통일
- 작성시각과 수집시각 분리
- URL·게시물 ID·해시 생성
- 중복·수정·삭제 상태 처리
- 닉네임의 최소화 또는 해시 처리

### 3.4 Analyzer

규칙, 사전, 소형 분류기, 로컬 LLM을 단계적으로 사용한다.

```text
텍스트 정제
→ 시장 관련성 판정
→ 종목·시장·업종 대상 인식
→ 명확한 사례 규칙 처리
→ 애매한 사례 LLM 분석
→ JSON 스키마 검증
→ 확신도 기준 검수 큐 분기
```

Analyzer 출력은 자유 문장이 아니라 버전된 구조화 계약을 따른다.

### 3.5 Index Engine

- 시간창별 유효 표본 선택
- 시간 감쇠
- 참여도 로그 변환과 상한
- 중복·도배·작성자 집중도 감점
- 감성·관심도·분열도 별도 계산
- 유효 표본 수와 데이터 신선도 기록
- 산식 버전별 재계산 지원

### 3.6 API

공개 API와 내부 API를 분리한다.

공개 API는 다음만 제공한다.

- 지수 스냅샷
- 시간대별 집계
- 비식별 주요 주제
- 데이터 신선도와 표본 수
- 방법론·버전 정보

내부 API는 다음을 제공한다.

- 수집 상태
- 오류·429·누락 지표
- 분석 검수 큐
- 상위 지수 기여 레코드
- 재처리와 재집계 요청

## 4. 권장 저장 계층

### 4.1 Raw Ephemeral

보존 목적: 파싱 오류 검증, 분석 재현, 단기 품질 점검

예시 필드:

```text
source_id
source_post_id
source_url
raw_title
raw_body
raw_comments
published_at
collected_at
content_hash
expires_at
```

기본 보존기간은 7일이며 정책 검토 후 조정한다.

### 4.2 Normalized

보존 목적: 모델 재분석과 라벨링

```text
document_id
source_id
source_post_id
document_type
normalized_text
author_hash
published_at
collected_at
content_hash
revision
status
```

### 4.3 Analysis

```text
analysis_id
document_id
schema_version
model_provider
model_name
model_version
prompt_version
targets
stance
emotion
action_intent
time_horizon
sarcasm_probability
relevance
confidence
created_at
```

### 4.4 Aggregate

```text
window_start
window_end
scope_type
scope_id
sentiment_index
fear_greed_index
attention_index
disagreement_index
velocity_15m
velocity_1h
sample_count
data_freshness_seconds
formula_version
status
```

## 5. 이벤트 계약

내부 처리 단계는 가능한 한 불변 이벤트를 사용한다.

```text
source.post.discovered
source.post.fetched
source.post.changed
source.post.deleted
content.normalized
content.analysis.completed
content.analysis.rejected
index.window.calculated
index.window.invalidated
retention.raw.expired
```

모든 이벤트는 다음 공통 필드를 포함한다.

```text
event_id
occurred_at
producer
producer_version
correlation_id
source_id
payload_schema_version
```

## 6. LLM 공급자 추상화

로컬 모델은 OpenAI 호환 인터페이스를 우선 사용하되 특정 실행기에 종속하지 않는다.

```ts
interface StructuredModelProvider {
  providerId: string;
  modelId: string;
  analyze<TInput, TOutput>(request: {
    input: TInput;
    schema: unknown;
    promptVersion: string;
  }): Promise<ModelResult<TOutput>>;
}
```

반드시 기록할 항목:

- 공급자
- 모델명
- 모델 파일 또는 태그
- 양자화 방식
- 프롬프트 버전
- 온도·시드 등 추론 설정
- 입력 해시
- 출력 원본과 검증 결과

## 7. 실패 격리

- 목록 수집 실패는 기존 지수를 새 값처럼 갱신하지 않는다.
- 분석 실패 데이터는 지수 계산에서 제외하고 누락량을 기록한다.
- 유효 표본이 기준 미만이면 지수 상태를 `INSUFFICIENT_DATA`로 표시한다.
- 시장 데이터가 지연되면 괴리지수만 비활성화하고 심리지수는 유지한다.
- 모델 교체 시 기존 결과를 덮어쓰지 않고 새 분석 버전을 생성한다.

## 8. 보안·개인정보 경계

- `.env`와 API 키는 Git에 저장하지 않는다.
- 공개 웹은 Raw·Normalized 저장소에 직접 접근하지 않는다.
- 작성자 식별자는 외부 API에 포함하지 않는다.
- 운영 로그에도 원문 전체를 기본 출력하지 않는다.
- 원문 내 이메일·전화번호 등 직접 식별정보는 정규화 단계에서 마스킹한다.
- 개발용 데이터 내보내기는 최소 표본과 만료일을 사용한다.

## 9. 개발 순서

1. `packages/contracts`: 공통 이벤트·분석·지수 스키마
2. `packages/source-adapters`: 목록 샘플 파서와 fixture 기반 테스트
3. `apps/collector`: 저장 없이 관찰만 하는 dry-run
4. `apps/analyzer`: 수동 fixture 분석
5. `apps/api`: 로컬 aggregate 조회
6. `apps/web`: 데이터 상태 중심 내부 화면
7. 외부 공개 대시보드

초기 구현에서 브라우저 자동화와 실제 수집부터 시작하지 않는다. 먼저 저장 계약, fixture, 오류 상태, dry-run을 만든다.
