# Repository Lineage

## Canonical Main Lineage

`fix/16-canonical-main-lineage`은 검증된 모든 product 구현과 #11~#15 정합성 수정을 하나의 canonical 선으로 통합한 브랜치입니다. 향후 `main` 승격 시 기초가 됩니다.

### Canonical source

- **main 승격 후 canonical source는 main**
- 현재 canonical working head: `fix/16-canonical-main-lineage`
- parent (CI gate): `fix/13-exact-head-ci` → `f4adb6a7b4dd786db86e5268c6903453efcdefc2`

## Stacked PR disposition

| PR # | Branch / title | Disposition |
|------|----------------|-------------|
| #1 | `agent/bootstrap-docs` | Superseded by consolidation |
| #6 | `feat/integration-slice-1` | Included in `feat/fmkorea-live-low-frequency` |
| #7 | `feat/ui-light-chart-first` | Included in integration candidate |
| #8 | `feat/market-kospi-hourly` | Included in integration candidate |
| #9 | `feat/ui-kospi-integration-candidate` | Included in `feat/fmkorea-live-low-frequency` |
| #10 | `feat/fmkorea-live-low-frequency` | Product authority included |
| #18 | `fix/11-krx-calendar` | Included by cherry-pick |
| #19 | `fix/12-kiwoom-request-budget` | Included by cherry-pick |
| #20 | `fix/13-exact-head-ci` | Branch parent |
| #21 | `fix/14-fmkorea-safety` | Included by cherry-pick |
| #22 | `fix/15-postcount-semantics` | Included by cherry-pick |

## Stale branch inventory

The following branches are deletion candidates only after main promotion and final owner approval. They are preserved as-is for audit history. No branch was deleted by #16.

| Branch | Head SHA | Status |
|--------|----------|--------|
| `agent/bootstrap-docs` | `78581bccd5b3a5cea12c448e4f08b1a9333e421b` | superseded |
| `feat/integration-slice-1` | `ead696b64f772cb181cbdffaaf8e3c48d90ba1a3` | superseded |
| `feat/ui-light-chart-first` | `15688b2e3b78d043a5bb341b914df996b3baaef0` | superseded |
| `feat/market-kospi-hourly` | `235ab25fc52abe49e7333b0d29203ed37c00f7ef` | superseded |
| `feat/ui-kospi-integration-candidate` | `33b68eb06c3b7ab85ba51b7b85843b70f0fa4465` | superseded |
| `feat/fmkorea-live-low-frequency` | `6a4cf26ecc5aa207d1415ec201bc322adc1d699b` | product authority included |
| `fix/11-krx-calendar` | `333829ec643bcae5a4e3be4d0dd843905f2ee385` | merged-equivalent |
| `fix/12-kiwoom-request-budget` | `b983b729d8cc281b1ea703b53dbafec85b331140` | merged-equivalent |
| `fix/13-exact-head-ci` | `f4adb6a7b4dd786db86e5268c6903453efcdefc2` | parent |
| `fix/14-fmkorea-safety` | `24e87d33e4bbad3d2f6e4494951409f86243dfdb` | merged-equivalent |
| `fix/15-postcount-semantics` | `ab19c9f85eabad7d54302aa033eecd52f5514609` | merged-equivalent |

## Accepted blocker integration

| Issue | Accepted head | Integration method |
|-------|---------------|--------------------|
| #11 KRX calendar | `333829ec643bcae5a4e3be4d0dd843905f2ee385` | cherry-pick |
| #12 Kiwoom hard request budget | `b983b729d8cc281b1ea703b53dbafec85b331140` | cherry-pick |
| #13 exact-head CI | `f4adb6a7b4dd786db86e5268c6903453efcdefc2` | parent |
| #14 FMKorea safety | `24e87d33e4bbad3d2f6e4494951409f86243dfdb` | cherry-pick |
| #15 postCount semantics | `ab19c9f85eabad7d54302aa033eecd52f5514609` | cherry-pick |

## Invariants

- `CANONICAL_SOURCE_IS_UNAMBIGUOUS` — canonical main lineage has one parent (`f4adb6a`, #13) plus accepted fixes #11, #12, #14, and #15.
- `MAIN_CONTAINS_VERIFIED_PRODUCT_CODE` — all changes are intended for promotion from the consolidation PR into `main` after exact-head CI passes.
- `STACKED_PR_DISPOSITION_RECORDED` — every stacked PR is recorded above as included, superseded, or parent. No stacked PR was merged or closed by this consolidation branch.
- `NO_DUPLICATE_OR_LOST_CHANGES` — accepted blocker heads are recorded and included once in the consolidation branch.
- `STALE_BRANCH_INVENTORY_COMPLETED` — stale branch inventory is recorded above; branches are not deleted.
- `PRODUCTION_BINDING_PRESERVED` — no production/deployment mutations were performed by #16.
