# Starguard 정독 — 포크할지, 기준만 가져올지

> 2026-10-23 · 대상 `m-ahmed-elbeskeri/Starguard` 커밋 `6e44047`(2025-06-14, 마지막 push)
> 스크래치패드에 클론해 **코드만 읽음. 설치·실행 안 함**
> 실측: ★500 · fork 9 · open issue 4 · Apache-2.0 · archived 아님

## 결론: 포크하지 않는다. 판정 기준(임계값·신호)만 출처를 밝혀 옮긴다

| 판단 축 | 관찰 | 영향 |
|---|---|---|
| 정답셋 대상과 맞는가 | `paginate()`는 오류가 나면 **예외 없이 그때까지 받은 것만 반환**한다(`github_api_rest.py:48-55,75-83`). 그래서 상위의 "pagination is limited" 처리(`:146-161`)에는 도달하지 않는다. 시각이 **전부** 없고 GraphQL 보완도 실패하면 생성일~오늘에 균등 분배한 가짜 날짜를 만든다(`:214-264`). ~~4만 초과면 첫 100개~~(정정, Codex 리뷰 2026-10-24) | 대형 레포에서 **불완전한 수집을 성공처럼 반환**하고, 특정 조건에서 합성 날짜 위에서 판정한다. 게다가 2026-06-30부터 stargazer 목록 자체가 막혀(아래) 이 수집 계층은 전부 쓸 수 없다 |
| 정확성 | lock-step 검출(`burst_detector.py:109-158`)이 교집합 없는 쌍을 건너뛰고(`:120-122`) sparse 행렬을 `toarray()`로 펴서, 미기록 거리가 0 = **"완전히 같음"**이 된다. 단 모든 쌍이 무관하면 전원 0점으로 끝난다(`:129-131`) | 교집합이 있는 쌍이 하나라도 섞이면 무관한 사용자들이 한 군집으로 묶일 수 있다 → lock-step **거짓 양성**. 실제 -10 패널티는 소비 코드(`trust_score.py`의 `clusters`)를 거친다 |
| 정확성 | clone 사칭 검사(`clone_impersonation.py`)가 "가장 이른 커밋 20개"라고 하면서 **최근 커밋 20개**를 뒤집어 쓴다(`:56-66`). 더 근본적으로 다른 레포의 `pushed_at`과 대상 커밋의 **작성자 날짜**를 비교해(`:88-90`) 어느 쪽에 먼저 있었는지 증명하지 못하고, 예외를 `False`로 돌려 "검사 실패"와 "사칭 아님"이 섞인다(`:106-108`) | 사칭 판정 근거가 성립하지 않는다. search API 호출은 검사당 최대 20회(조기 종료 있음) |
| API 비용 | 버스트 사용자 최대 1만 명 × (starred 목록 + 프로필 + GraphQL) | 토큰 하나의 시간당 5천 회를 넘는다. open issue #5 "slow" |
| 메모리 | 1만 명 dense 거리 행렬 = 1억 칸 | 약 800MB |
| 의존성 | pandas·scikit-learn·matplotlib·networkx + 선언에 없는 scipy | 단건 CLI에 비해 무겁고, 선언에 빠진 의존성이 있다 |
| 테스트 | `tests/test_github_api.py`·`test_star_analyzer.py`는 빈 파일. `test_burst_detector.py`는 테스트가 아니라 `main()` 수동 스크립트 | 자동 테스트 **0개**. 포크해도 회귀를 지킬 장치가 없다 |
| 유지보수 | 16개월 정체. issue #1(패키지 설치), #2(샘플링·기간 지정), #6(uvx 예제 실패) 열린 채 | 포크하면 업스트림 없이 혼자 유지해야 한다 |

→ 포크해서 얻는 것은 "임계값 묶음과 점수 구조"인데, 그건 상수 몇십 줄이다. 수집 계층은 우리 용도(대형 바이럴 레포)에서 동작하지 않고, 핵심 신호 두 개(lock-step·clone)에 버그가 있다. **포크 + 수정 비용 > 기준만 이식하는 비용.**

## 가져갈 것 (Apache-2.0 — 출처 표기, 상수는 재구현)

**스타 급증 (MAD)** — `core/constants.py:55-58`
- 28일 이동창, 임계 `3 × 1.48 × MAD`
- 스타 30개 미만이면 MAD 대신 증가율 300%를 기준으로 쓴다

**가짜 계정 점수** — `USER_SCORE_THRESHOLDS`, 합계 ≥ 4.5면 의심
| 신호 | 기준 | 가중치 |
|---|---|---|
| 계정 나이 | < 60일 | 2.0 |
| 팔로워 | < 5 | 1.0 |
| 공개 레포 | < 2 | 1.0 |
| 누른 스타 수 | < 3 | 1.0 |
| 기본 아바타 | 예 | 0.5 |
| 최장 비활동 | > 60일 | 1.0 |
| lock-step 밀도 | > 0.05 | 2.0 |

**레포 단위 신호**
- fork/star 비(`signals/fsr.py`): ≥ 0.5 의심, ≥ 0.25 주의
- 스타 시각의 시간대 엔트로피 < 1.5 → 패널티. 기여 Gini > 0.6 → 패널티
- 버스트 평균 지속 ≤ 2일 → 패널티
- trust score 구성: 스타 40 · 의존성 25 · 라이선스 20 · 메인테이너 15, 가짜 스타 패널티 상한 60

**lock-step 아이디어**: starred 레포 집합의 Jaccard 거리 + DBSCAN(eps 0.2, 최소 4명). 단 **교집합 0인 쌍의 거리는 1로 채워야** 한다(원본 버그 수정)

이 기준들의 원 출처는 StarScout 논문(He et al., arXiv:2412.13459)의 lockstep·low-activity 신호다. 카드에 인용할 때는 논문을 1차 출처로 쓴다.

## ⚠️ 추가 실측 (2026-10-24) — stargazer 목록 자체가 막혔다
- GitHub이 **2026-06-30부터 stargazer·watcher 목록을 레포 admin·collaborator 전용으로 바꿨다**([changelog](https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/)). 실측 결과:
  - REST `/repos/{o}/{r}/stargazers` → **404** (비인증은 401). kordoc·Ruflo·LiteLLM·ida-pro-mcp·Starguard 모두 같음
  - GraphQL `stargazers` → `totalCount: 0`, edges 빈 배열 (orderBy와 무관)
  - `/subscribers` → 404
  - REA 카드(10-14)의 "스타 목록 API 404"도 같은 원인이다. 그때 "조회 환경 제약"으로 본 판단이 맞았다
- **대체 API**([changelog 2026-09-04](https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data/)):
  - `GET /repos/{o}/{r}/stargazers/history` → 주 단위 `{week, total, days[7]}`, 한 쪽에 30주, 최대 100쪽. **일별 개수는 그대로 나온다**
  - `GET /repos/{o}/{r}/stargazers/count` → `{count}`
  - 실측 예: REA 최근 3주 78,051 → 232,138 → 62,892(진행 중). 하루 최대 54,162
  - 그대로 쓸 수 있는 것: 포크 목록(`/forks`), 사용자 프로필, 사용자가 스타를 누른 레포 목록(`/users/{u}/starred`)
- **영향**: Starguard 점수의 절반(가짜 계정 점수, lock-step, 시간대 엔트로피, 버스트 사용자 다양성)은 **남의 레포에 대해서는 계산할 수 없다**. StarScout도 GHArchive 기반이라 단건 조회로는 대체할 수 없다
- → **이식 범위 축소**: 개수 시계열 신호(MAD 급증, 증가율, 버스트 지속)와 fork/star 비만 그대로 쓴다. 계정 신호(위 점수표)는 **포크 소유자 표본**에 적용한다. REA 카드가 이미 쓴 방법이다

## hype-checker v1에서 다르게 할 것
1. **스타 시계열은 `/stargazers/history`로 받는다.** 4만 상한 문제와 사용자 목록 문제를 한꺼번에 피한다. 일별 개수에 MAD(28일 이동창)를 건다
2. **계정 표본은 포크 소유자로, 작게 고정한다**(40명). REA 카드의 "포크 계정 40명 샘플"이 기준이다. 리포트에는 "스타 계정이 아니라 포크 계정 표본"이라고 표시하고, API 예산을 적는다
3. **가짜 날짜를 만들지 않는다.** 시각을 못 얻으면 "측정 불가"로 표시한다(AGENTS.md "확인 못 하면 그렇게 표시")
4. 점수 하나로 합치지 않는다. 카드 형식(✅❌⚠️❓)에 맞게 **신호별 근거를 그대로 낸다**. 합산 점수는 보조로만 쓴다
5. 정답셋 검증 기준: REA "★215k는 실사용과 안 맞음(부풀림 단정 불가)", Ruflo "스타 ≠ 성숙도"를 ⚠️로 재현해야 한다. **단정(❌)을 내면 오답**이다
