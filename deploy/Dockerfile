# hype-checker — oci-server(ARM64) 이미지. 빌드: deploy/ 에서 docker compose build (context 는 레포 루트)
# 런타임은 표준 라이브러리 + PyYAML(내 모드 workflow.yaml) + gh CLI(GitHub GET 측정, runner 허용 목록)
# + ffmpeg 와 watch 0.3.2(DM 릴스 화면·음성 읽기, HC_VIDEO=on). watch 는 deploy/vendor-watch.sh 로 미리 복사
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

ARG GH_VERSION=2.93.0
ARG GH_SHA256=c55feb33684abba57e9909737340d5b39282257c0363e1edde6785ac4a413be7

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
 && echo "${GH_SHA256}  /tmp/gh.tgz" | sha256sum -c - \
 && tar -xzf /tmp/gh.tgz -C /tmp \
 && mv "/tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh* \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir PyYAML==6.0.3 yt-dlp==2026.10.26

# 서버 사용자 ubuntu 와 같은 uid/gid(1001) — 바인드한 v2.env(600)를 읽을 수 있어야 한다
RUN groupadd -g 1001 app && useradd -u 1001 -g 1001 -m app \
 && mkdir -p /app/out /home/app/.config/hype-checker /home/app/.config/watch && chown -R app:app /app /home/app

COPY deploy/vendor/watch-0.3.2 /opt/watch

WORKDIR /app
COPY --chown=app:app hype_checker /app/hype_checker

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GH_CONFIG_DIR=/tmp/gh HC_WATCH_SCRIPT=/opt/watch/scripts/watch.py
USER app
