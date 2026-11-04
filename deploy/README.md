# hype-checker 서버 배포 (oci-server)

근거: ADR `ops/adr/0017-hype-checker-secrets-on-oci.md`. 서버 규칙: `projects/oci-server/docs/server.md`

## 구성
- 이미지 하나, 서비스 둘: `serve`(웹훅 수신, 외부 네트워크 `edge`에 별칭 `hype-checker`) · `work`(큐 처리·답장, `--loop --send`)
- 공개: 터널 `oci-edge` Public Hostname `hype.example.com` → `http://hype-checker:8787`. 호스트 포트 없음
- 웹훅 경로: `/webhook/<HC_WEBHOOK_PATH 의 무작위 부분>` — 그 밖의 경로는 Cloudflare WAF가 막고, 앱도 403/400
- 데이터: 볼륨 `hype-checker_data` → `/app/out`. done·failed·한도·정정 기록은 7일 뒤 자동 삭제, 측정 묶음은 수동 정리(`du -sh`로 월 1회 확인, 건당 12~68KB)
- 컨테이너: uid 1001, 읽기 전용 루트, `cap_drop: ALL`, `no-new-privileges`, docker.sock 없음

## 처음 배포
1. 맥에서 코드 동기화(레포 main 이력은 GitHub에 없다):
   `rsync -a --delete --exclude-from=deploy/rsync-exclude ./ oci:~/stacks/hype-checker/`
2. 서버에서 비밀값(600, ubuntu 소유):
   - `deploy/secrets/serve.env` — 수신기 최소값만(IG_VERIFY_TOKEN, IG_APP_SECRETS, IG_ALLOWED_SENDERS(`*`=공개), IG_UNLIMITED_SENDERS, IG_ACCOUNT_ID, IG_OWNER_SENDER, HC_WEBHOOK_PATH). 답장 토큰·Claude 키 넣지 않음
   - `deploy/secrets/v2.env` — worker용, 맥 `~/.config/hype-checker/v2.env` + `HC_WEBHOOK_PATH`, `HC_EXTRACT=claude`, `HC_CLAUDE_MODEL`, `HC_PRICE_IN_PER_MTOK`, `HC_PRICE_OUT_PER_MTOK`
   - `deploy/secrets/workflow.yaml` — 내 모드 정본(없으면 `items: []`)
   - `deploy/secrets/watch.env` — `GROQ_API_KEY=` 한 줄만(DM 릴스 음성 받아쓰기). 영상 읽기를 켜려면 `v2.env`와 `serve.env` **둘 다**에 `HC_VIDEO=on`(수신기가 꺼져 있으면 영상 링크를 큐에 넣지 않는다)
   - `deploy/.env` — `GH_TOKEN=`(권한 없는 fine-grained PAT, Public repositories 읽기)
3. `cd ~/stacks/hype-checker/deploy && docker compose up -d --build`
4. 확인: `docker compose logs serve | grep listening` · 내부 확인 `docker run --rm --network edge curlimages/curl -s -o /dev/null -w '%{http_code}' http://hype-checker:8787/` → 403
5. **먼저** Cloudflare WAF(존 example.com): 호스트 `hype.example.com`에서 경로가 웹훅 경로가 아니거나 메서드가 GET·POST가 아니면 Block. Rate limiting: 같은 IP 10초 30회 초과 Block
6. **그다음** Zero Trust → Tunnels → `oci-edge` → Public Hostname `hype.example.com` → `http://hype-checker:8787` (외부 공개). `edge`에 같은 별칭이 없는지 먼저 확인: `docker network inspect edge`
7. Meta 대시보드 → Webhooks → 콜백 URL을 `https://hype.example.com/webhook/…`로 바꾸고 확인·저장
8. 맥 수신기·Funnel 내리기: `tailscale funnel reset`, 맥 `serve` 종료

## 업데이트
맥에서 `deploy/vendor-watch.sh`(watch 0.3.2 스크립트를 `deploy/vendor/`로 복사, 커밋 안 함) → 1번 rsync → 서버 `docker compose up -d --build`. 비밀값·볼륨은 그대로

## 남는 위험(수용)
- 같은 `edge` 네트워크의 다른 앱 컨테이너는 Cloudflare를 거치지 않고 8787에 닿는다. 서명이 없으면 본문 전 403이라 위조는 못 하고, 연결 점유 방해만 가능(내부 앱 침해 전제)
- 하루 $ 한도에 Groq 비용은 안 들어간다. Claude 상한은 Console workspace 월 $20
- 전송 직전 종료는 '결과 불명'으로 남고 자동 재전송하지 않는다 → 읽기만: `docker compose exec work sh -c 'grep -l attempted_at /app/out/v2/done/*.json'`로 해당 기록을 찾아 Instagram에서 실제 수신 여부를 보고 판단(큐를 처리하는 `work` 명령을 따로 돌리지 않는다 — 상시 worker와 잠금 충돌)

## 배포 리뷰 판정 (Codex 2회, 상한)
- 7일 넘게 멈춘 뒤 재처리: done이 정리돼 큐가 다시 처리될 수 있지만 24시간 답장 창 검사로 전송은 막힌다 → 수용(추가 측정 비용만)
- 공개 전 검증: `HC_VERDICT=off` 확인, WAF는 규칙 생성 직후 외부 요청으로 확인한 뒤 Meta 콜백을 바꾼다

## 되돌리기
Meta 콜백 URL을 맥 Funnel 주소로 되돌리고 맥에서 `serve`·`tailscale funnel --bg 8787` → 서버 `docker compose down`
