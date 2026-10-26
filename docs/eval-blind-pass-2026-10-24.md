# 블라인드 판정 세션 기록 (2026-10-24)

정답셋을 볼 수 없는 별도 에이전트에게 코드 단계 묶음 6개(`out/20261024-033744-*`, 결과 `eval/results/2026-10-24T033913Z.json`)를 주고 `resume`·`drop`만 허용했다. 금지 경로: `eval/`, `README.md`, `docs/`, `tests/`, `HANDOFF.md`, `~/Desktop/Career/`, 웹 검색.
이 시점의 `resume`은 아직 개입 기록(`interventions`)을 남기지 않아서, 근거는 에이전트 보고를 여기에 옮긴다.

| 묶음 | 조치 | 근거(에이전트 보고) |
|---|---|---|
| Liner | 없음 | frame_0004 "Liner Search MCP", platform.liner.com — 기존 식별과 일치 |
| Ruflo | 없음 | 전사 "루플로" → ruvnet/ruflo, 이름이 나온 프레임 없음 |
| 디자인 MCP 5 | drop 5 + resume 5 (`--kind mcp`) | frame_0002~0006 화면 표기 Mobbin·Nicelydone·Refero·Lazyweb·InspoAI MCP와 도메인. 엔드포인트를 모르는 3개는 `--identifier` 넣지 않음 |
| graphify | resume `--identifier Graphify-Labs/graphify` + 중복 drop | frame_0004 화면의 `github.com/Graphify-Labs/graphify` |
| UI 팁 3 | drop "Plaeright" + resume "Playwright"(plugin) | 전사 "플러그인 … 플레이라이트 검색해서 설치" + 상식. TastySkill·Image Code는 확신 없어 그대로 |
| Figma 커넥터 | 없음 | 화면에 커넥터 이름이 없음 |

결과: 자기 패스(정답셋을 아는 세션)보다 엄격 재현율이 높거나 같다(0.818 vs 0.727) → 오염으로 점수가 부풀지는 않았다.
