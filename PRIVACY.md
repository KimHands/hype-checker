# 개인정보처리방침 (Privacy Policy)

시행일 2026-11-02 · 대상: Meta 앱 `hype-checker-dm`과 Instagram 계정 `@hyper_checker`

hype-checker는 운영자 한 명이 운영하는 무료 도구입니다. 누구나 `@hyper_checker`에 DM으로 공유한 게시물·릴스의 캡션과 릴스 영상(화면 글자·음성)을 읽고, 거기 나온 오픈소스 도구를 GitHub·npm 공개 정보로 확인합니다. 계정당 하루 2건, 전체 하루 60건까지 처리하며 결과는 자동으로 만들어지므로 틀릴 수 있습니다.

## 받는 정보
- `@hyper_checker`로 온 DM의 웹훅 이벤트: 보낸 계정의 Instagram 범위 ID(IGSID), 메시지 ID, 시각, 공유된 게시물·릴스의 캡션, 미디어 ID, Meta가 이벤트에 넣어 준 릴스 영상 링크
- 그 밖의 정보(이름, 연락처, 위치 등)는 요청하지 않습니다. 다만 캡션이나 영상에 들어 있으면 그 내용과 함께 처리되고, 캡션·전사·화면 글자는 측정 묶음에 남을 수 있습니다

## 처리 방식
- **`@hyper_checker`로 온 모든 계정의 DM을 받습니다.** 게시물·릴스 공유와 GitHub·npm·PyPI 링크만 분석합니다. 일반 문자 메시지는 외부 서비스로 보내지 않고 안내 답장만 보내지만, 처리 기록에는 7일 동안 남습니다
- 하루 한도를 지키려고 보낸 계정의 ID와 처리 건수를 기록합니다
- 릴스 영상은 Meta가 DM 이벤트에 넣어 준 그 릴스의 링크로, 로그인·쿠키 없이 한 번만 받습니다. 영상 파일, 장면 이미지, 영상 링크는 처리하는 동안만 쓰고 처리가 끝나면 바로 지웁니다
- 처리는 운영자가 관리하는 서버(Oracle Cloud, 일본 오사카 리전)에서 이루어집니다
- 처리에 쓰는 외부 서비스: 캡션·전사·장면 이미지에서 도구 이름을 찾고, 게시물의 주장을 공개 자료(GitHub·npm 등)와 대조해 자동 확인 결과를 만드는 데 Anthropic Claude API, 음성 받아쓰기에 Groq API. 두 서비스에는 그 처리에 필요한 내용만 보내며, 보낸 계정 ID는 보내지 않습니다. 장면 이미지는 화면 전체이므로 영상에 찍힌 얼굴·계정명 등이 함께 전송될 수 있습니다
- 수집한 정보를 제3자에게 판매하지 않습니다. 광고나 프로파일링에 쓰지 않습니다

## 보관 기간
- 처리 대기·완료 기록, 하루 한도 기록, `정정` 요청 기록(모두 보낸 계정 ID 포함): 7일이 지나면 삭제합니다
- 같은 게시물·같은 도구의 확인 결과 캐시(보낸 계정 ID 없음): 게시물 7일, 도구 3일
- 판정 근거로 남기는 측정 묶음(캡션·영상 전사·화면 글자 포함, 영상·이미지는 없음)은 운영자가 직접 지울 때까지 서버에만 보관합니다

## 삭제 요청
데이터 삭제를 원하시면 `@hyper_checker`에 DM을 보내거나 https://github.com/KimHands/hype-checker/issues 에 요청을 남겨 주세요. 확인 후 30일 안에 관련 기록을 지웁니다.

## 변경
이 방침이 바뀌면 이 파일을 고치고 시행일을 갱신합니다.

---

**English summary.** hype-checker is a free tool run by a single operator. It accepts Instagram DMs to `@hyper_checker` from any account, within limits of 2 checks per account and 60 overall per day, reading shared post/reel captions and reel video (on-screen text and speech) to look up open-source tools on public registries. Results are automatic and may be wrong. Plain text DMs are not analyzed or sent to outside services, but stay in processing records for 7 days. Sender IDs are kept in processing, limit and correction records for 7 days. Each reel is fetched once, anonymously (no login or cookies), only through the link Meta includes in the DM event; the video, frames and link are used only during processing and deleted right after. Processing runs on the operator's server (Oracle Cloud, Osaka region in Japan) and uses the Anthropic Claude API (finding tool names in captions, transcripts and frames, and checking the post's claims against public sources to produce an automatic assessment) and the Groq API (speech-to-text), sending them only the content needed and never the sender ID. Data is never sold or used for ads or profiling, and queue records are deleted after 7 days. To request deletion, DM `@hyper_checker` or open an issue at https://github.com/KimHands/hype-checker/issues.
