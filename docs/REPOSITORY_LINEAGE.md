# Repository Lineage

## Canonical Main Lineage

`fix/16-canonical-main-lineage`은 검증된 모든 product 구현과 #11~#15 정합성
수정을 하나의 canonical 선으로 통합한 브랜치입니다. 향후 `main` 승격 시 기초가
됩니다.

### Canonical source

- **main 승격 후 canonical source는 main**
- 현재 canonical working head: `fix/16-canonical-main-lineage`
- parent (CI gate): `fix/13-exact-head-ci` → `f4adb6a7b4dd786db86e5268c6903453efcdefc2`

### Stacked PR disposition

| PR #  | Branch / Title                    | Disposition                          |
|-------|-----------------------------------|--------------------------------------|
| #1    | agent/bootstrap-docs              | superseded by consolidation          |
| #6    | feat/integration-slice-1          | included in feat/fmkorea-live-low-frequency |
| #7    | feat/ui-light-chart-first         | included in integration candidate    |
| #8    | feat/market-kospi-hourly          | included in integration candidate    |
| #9    | feat/ui-kospi-integration-candidate | included in feat/fmkorea-live-low-frequency |
| #10   | feat/fmkorea-live-low-frequency   | product authority included           |
| #18   | fix/11-krx-calendar               | included by cherry-pick              |
| #19   | fix/12-kiwoom-request-budget      | included by cherry-pick              |
| #20   | fix/13-exact-head-ci              | branch parent                        |
| #21   | fix/14-fmkorea-safety             | included by cherry-pick              |
| #22   | fix/15-postcount-semantics        | included by cherry-pick              |

### Stale branch inventory (deletion candidates — NOT deleted)

The following branches are superseded by the canonical lineage and are
candidates for future deletion after main promotion. They are preserved
as-is for audit history.

| Branch                              | Head SHA                            | Status       |
|-------------------------------------|-------------------------------------|--------------|
| feat/integration-slice-1            | (old)                               | superseded   |
| feat/ui-light-chart-first           | (old)                               | superseded   |
| feat/market-kospi-hourly            | (old)                               | superseded   |
| feat/ui-kospi-integration-candidate | (old)                               | superseded   |
| fix/11-krx-calendar                 | 333829ec...                         | merged-equiv |
| fix/12-kiwoom-request-budget        | b983b729...                         | merged-equiv |
| fix/13-exact-head-ci                | f4adb6a7...                         | parent       |
| fix/14-fmkorea-safety               | 24e87d33...                         | merged-equiv |
| fix/15-postcount-semantics          | ab19c9f8...                         | merged-equiv |

### Invariants

- `CANONICAL_SOURCE_IS_UNAMBIGUOUS` — canonical main lineage has one parent
  (`f4adb6a`, #13) plus accepted fixes #11, #12, #14, #15.
- `MAIN_CONTAINS_VERIFIED_PRODUCT_CODE` — all changes verified by CI gate
  (exact-head, `git diff --check`, full pytest pass).
- `STACKED_PR_DISPOSITION_RECORDED` — every stacked PR is recorded above as
  included, superseded, or parent. No PR was merged or closed by this
  consolidation branch.
- `NO_DUPLICATE_OR_LOST_CHANGES` — cherry-picks applied cleanly; all
  focused and full test suites pass.
- `STALE_BRANCH_INVENTORY_COMPLETED` — stale branch inventory is recorded
  above; branches are NOT deleted.
- `PRODUCTION_BINDING_PRESERVED` — no production/deployment mutations; all
  work is data-contract and code-level only.
