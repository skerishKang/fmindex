# Integration Slice 1 — 설계 문서

## 목표

65stock 키움 데이터 + 펨코 Fixture + LLM 심리분석 + Chart.js 대시보드를 한 번의 명령으로 실행하는 수직 통합 기능.

## 데이터 흐름

```
65stock 데이터 (또는 샘플)
  → 코스피 1시간 봉 정규화
  → FMKorea Fixture 파싱
  → LLM 심리분석 (Mock)
  → 시간대별 펨코지수 계산
  → 코스피와 시간축 결합
  → Chart.js 대시보드 JSON
  → 로컬 대시보드 표시
```

## 실행 명령

```bash
python -m fmindex.pipeline --once
```

대시보드 서버 실행:

```bash
python -m fmindex.pipeline --serve
```

## 컴포넌트

### 1. Market Bridge (`fmindex/market/bridge.py`)
- 65stock 데이터 루트에서 JSON/CSV 읽기
- 환경변수 `FMINDEX_65STOCK_DATA_ROOT`로 경로 설정
- 1시간 OHLC 집계
- 중복 시간 제거
- Asia/Seoul 시간대 정규화

### 2. FMKorea Parser (`fmindex/fmkorea/parser.py`)
- HTML Fixture에서 게시글·댓글 파싱
- `tests/fixtures/fmkorea/`에 오프라인 Fixture
- 네트워크 접속 없음

### 3. LLM Provider (`fmindex/llm/provider.py`)
- 공통 인터페이스 (`LLMProvider`)
- Mock provider (키워드 기반 결정론적 분석)
- 환경변수로 실제 LLM 연결 가능 (`FMINDEX_LLM_PROVIDER`)

### 4. FM Index Calculator (`fmindex/fmindex_calc.py`)
- 게시글을 1시간 버킷으로 집계
- `fmIndex = (averageScore + 1) × 50`
- `firstSeenAt` 기준 버킷팅 (미래 누출 방지)

### 5. Market Joiner (`fmindex/market_join.py`)
- 코스피 정규화 (첫 close = 100)
- 시간축 결합
- 야간 심리 vs 다음 장 결과 평가

### 6. Dashboard (`fmindex/dashboard/server.py`)
- Chart.js 기반 대시보드
- 펨코지수 + 코스피 듀얼 축 차트
- 야간 심리 카드
- 표본·신뢰도 카드

## 데이터 계약

### 코스피 시간 데이터
```json
{
  "timestamp": "2026-08-05T10:00:00+09:00",
  "market": "KOSPI",
  "open": 3210.24,
  "high": 3217.58,
  "low": 3204.17,
  "close": 3214.81,
  "changeRate": 0.14,
  "source": "kiwoom-65stock",
  "observedAt": "2026-08-05T11:03:15+09:00"
}
```

### 펨코 시간대별 심리
```json
{
  "timestamp": "2026-08-05T10:00:00+09:00",
  "fmIndex": 38.4,
  "positiveRatio": 0.21,
  "negativeRatio": 0.55,
  "neutralRatio": 0.24,
  "postCount": 74,
  "commentCount": 312,
  "analyzedPostCount": 69,
  "confidence": 0.82,
  "methodologyVersion": "fmindex-v1"
}
```

### 결합 데이터
```json
{
  "timestamp": "2026-08-05T10:00:00+09:00",
  "market": "KOSPI",
  "fmIndex": 38.4,
  "marketNormalized": 99.72,
  "marketChangeRate": -0.28,
  "postCount": 74,
  "confidence": 0.82
}
```

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `FMINDEX_65STOCK_DATA_ROOT` | `../65stock/05_data/stocks` | 65stock 데이터 경로 |
| `FMINDEX_MARKET` | `KOSPI` | 시장 식별자 |
| `FMINDEX_TIMEZONE` | `Asia/Seoul` | 시간대 |
| `FMINDEX_LLM_PROVIDER` | `mock` | LLM 제공자 |
| `FMINDEX_LLM_MODEL` | `mock-deterministic-v1` | LLM 모델명 |
| `FMINDEX_LLM_API_KEY` | (없음) | LLM API 키 |

## 재사용 자산

| 자산 | 원본 | 방식 |
|------|------|------|
| 키움 인증·토큰 | 65stock | 브리지 (데이터만 읽기) |
| 수익률·랭킹 계산 | 10000-fm-stock | 참고 (패턴 일반화) |
| Chart.js 패턴 | 100tradediary | 참고 (구조만) |
| 대시보드 셸 | 10000-fm-stock | 참고 |

## 하지 않는 것

- 65stock 코드 직접 복사
- 키움 API 실호출
- FMKorea 실시간 수집
- 나스닥 연동
- Hermes Cron 등록
- 실제 LLM 호출 (Mock 사용)
