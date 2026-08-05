# Windows 로컬 설치 가이드

## 1. 기준 환경

- 운영체제: Windows 10 또는 11
- 작업 경로: `G:\Ddrive\BatangD\task\workdiary\fmindex`
- Git
- Node.js 24 LTS
- Corepack
- pnpm 11.4.0
- PowerShell 5.1 이상 또는 PowerShell 7
- 선택 사항: 로컬 LLM 서버(Ollama, LM Studio, llama.cpp 호환 서버 등)

## 2. 저장소 연결 확인

이미 로컬 폴더를 만든 경우 PowerShell에서 실행한다.

```powershell
cd G:\Ddrive\BatangD\task\workdiary\fmindex
git status -sb
git remote -v
```

`origin`이 없으면 다음과 같이 연결한다.

```powershell
git remote add origin https://github.com/skerishKang/fmindex.git
git fetch origin
git branch --set-upstream-to=origin/main main
```

로컬 폴더가 비어 있고 새로 복제할 경우:

```powershell
cd G:\Ddrive\BatangD\task\workdiary
git clone https://github.com/skerishKang/fmindex.git
cd .\fmindex
```

## 3. 부트스트랩 PR 확인

PR이 병합되기 전에 작업 브랜치를 로컬에서 확인하려면:

```powershell
git fetch origin
git switch --create agent/bootstrap-docs --track origin/agent/bootstrap-docs
```

이미 같은 브랜치가 있으면:

```powershell
git switch agent/bootstrap-docs
git pull --ff-only
```

PR이 `main`에 병합된 뒤에는:

```powershell
git switch main
git pull --ff-only origin main
```

## 4. Node.js 확인

```powershell
node --version
corepack --version
```

Node.js는 `v24.x.x`여야 한다. 다른 주 버전이 설치돼 있다면 Node 버전 관리 도구 또는 공식 설치 프로그램으로 Node.js 24 LTS를 설치한 뒤 새 터미널을 연다.

## 5. 자동 준비

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

이 스크립트는 다음을 수행한다.

- Git·Node.js 확인
- Node.js 24 주 버전 확인
- pnpm이 없으면 Corepack으로 pnpm 11.4.0 활성화
- 앱·패키지·데이터·로그 디렉터리 생성
- `.env.example`을 `.env`로 복사
- `pnpm install`
- 환경 진단 실행

기존 `.env`는 덮어쓰지 않는다.

## 6. 수동 준비

자동 스크립트를 사용하지 않는 경우:

```powershell
corepack enable
corepack prepare pnpm@11.4.0 --activate
pnpm --version

Copy-Item .env.example .env

$directories = @(
  'apps/collector',
  'apps/analyzer',
  'apps/api',
  'apps/web',
  'packages/contracts',
  'packages/taxonomy',
  'packages/source-adapters',
  'data/raw',
  'data/normalized',
  'data/aggregates',
  'data/checkpoints',
  'data/quarantine',
  'logs',
  'run'
)
$directories | ForEach-Object { New-Item -ItemType Directory -Force $_ | Out-Null }

pnpm install
```

## 7. 환경 진단

```powershell
pnpm run doctor
```

진단 항목:

- Git·Node.js·pnpm 설치
- Node 24·pnpm 11 주 버전
- Git 저장소와 `origin`
- 필수 문서·설정 파일
- 런타임 디렉터리
- `.env`
- 수집기 활성화 상태
- 로컬 LLM 모델 설정
- `node_modules`

경고는 개발을 완전히 막지 않을 수 있지만 `FAIL`은 해결해야 한다.

## 8. `.env` 초기 설정

초기에는 다음 값을 유지한다.

```dotenv
COLLECTOR_ENABLED=false
COLLECTOR_DETAIL_CONCURRENCY=1
COLLECTOR_STOP_ON_429=true
COLLECTOR_RAW_RETENTION_DAYS=7
MARKET_DATA_ENABLED=false
```

로컬 LLM 서버가 OpenAI 호환 API를 제공하면 다음을 입력한다.

```dotenv
LLM_PROVIDER=openai-compatible
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=local-only
LLM_MODEL=<실제 모델 이름>
LLM_MAX_CONCURRENCY=1
```

수집기는 Source Policy 검토와 dry-run 구현이 끝나기 전까지 활성화하지 않는다.

## 9. 권장 로컬 디렉터리 역할

```text
data/raw          단기 원문·응답

data/normalized   정규화 텍스트

data/aggregates   지수 스냅샷

data/checkpoints  마지막 처리 위치

data/quarantine   파싱·분석 실패 표본

logs              실행 로그

run               PID·상태 파일
```

이 디렉터리는 기본적으로 Git에 올라가지 않는다.

## 10. 초기 개발 순서

부트스트랩 뒤 바로 실제 수집기를 만들지 않고 다음 순서로 진행한다.

1. `packages/contracts`에 Zod 또는 JSON Schema 계약 정의
2. 저장된 HTML fixture를 대상으로 목록 파서 작성
3. fixture 기반 중복·시간 파싱 테스트 작성
4. 실제 요청 없이 동작하는 collector dry-run 작성
5. 수동 텍스트 fixture로 analyzer 작성
6. 로컬 LLM의 구조화 JSON 출력 검증
7. SQLite 스키마와 migration 추가
8. 제한된 실제 수집 검토

## 11. 자주 발생하는 문제

### 실행 정책 오류

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
```

현재 프로세스만 우회하므로 시스템 전체 실행정책을 변경할 필요가 없다.

### pnpm을 찾지 못함

```powershell
corepack enable
corepack prepare pnpm@11.4.0 --activate
```

이후 터미널을 다시 열고 `pnpm --version`을 확인한다.

### Node 버전 불일치

`node --version`이 v24가 아니면 부트스트랩이 중단된다. 여러 Node가 설치된 경우 `where.exe node`로 실제 실행 경로를 확인한다.

### 원격 저장소 불일치

```powershell
git remote set-url origin https://github.com/skerishKang/fmindex.git
git remote -v
```

### 로컬 변경 때문에 pull 실패

```powershell
git status -sb
git diff
```

변경 내용을 확인하지 않고 `reset --hard`를 실행하지 않는다. 필요한 변경을 별도 브랜치에 커밋하거나 stash한 후 pull한다.

## 12. 준비 완료 기준

다음이 모두 충족되면 로컬 개발 준비가 끝난다.

- `pnpm run doctor`가 PASS
- `origin`이 `skerishKang/fmindex`를 가리킴
- `.env`가 존재하고 Git에서 제외됨
- Collector가 비활성화됨
- 데이터·로그 디렉터리가 Git에서 제외됨
- Node.js 24와 pnpm 11이 확인됨
- 다음 작업 브랜치를 만들 수 있음

첫 구현 브랜치 권장명:

```text
agent/phase-0-contracts-fixtures
```
