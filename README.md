# 한국 FM 라디오 — BitChord addon

BitChord v1.9.1에서 한국 라디오 채널을 검색하고 재생하는 간단한 HTTP addon입니다. 방송국 목록은 프로젝트의 `stations_seed.json` 기본 목록으로 즉시 준비되며, 서버는 공개 라디오 디렉터리(`radio.bsod.kr`)에서 백그라운드로 목록을 갱신합니다. 디렉터리를 사용할 수 없어도 기존 목록은 유지하고 5분 뒤 다시 시도합니다. 방송사별 HLS 재생목록과 오디오 조각은 서버가 중계합니다. AAC·MP3·MPEG-TS 조각을 제공하는 채널은 짧은 HLS 목록 창을 하나의 연속 응답으로 이어 보냅니다. 오디오는 저장하지 않지만 네트워크 트래픽은 이 서버를 통과합니다.

## BitChord에 추가

1. BitChord의 **설정 → Sources(소스) → 애드온 추가(Add Addon)**를 엽니다.
2. 실행 중인 서버의 공개 기본 주소를 붙여넣고 저장합니다. `manifest.json`은 주소에 포함하지 않습니다.
3. 검색 화면에서 **Addons**를 선택하고 `KBS 1FM`, `MBC FM4U`, `SBS 파워FM`, `CBS 음악FM`처럼 방송국 이름을 검색해 결과를 누릅니다. `라디오`를 검색하면 주요 채널을 먼저 보여줍니다.

## 실행

Python 3.10+ 필요:

```bash
python3 -m pip install -r requirements.txt
PORT=8080 PUBLIC_BASE_URL=https://your-host.example python3 app.py
```

다른 기기에서 사용하려면 이 서버를 계속 실행되는 HTTPS 호스트에 배포하고, 그 공개 기본 URL을 BitChord의 소스로 등록하세요. Docker로도 실행할 수 있습니다.

```bash
docker build -t korean-fm-bitchord-addon .
docker run --rm -p 8080:8080 -e PORT=8080 -e PUBLIC_BASE_URL=https://your-host.example korean-fm-bitchord-addon
```

서버가 재시작될 수 있는 배포 환경에서는 프록시 링크의 서명이 유지되도록 같은 `PROXY_SECRET` 값을 계속 사용하세요.

## Render 배포 (Blueprint)

프로젝트 루트의 `render.yaml`은 싱가포르의 무료 Python 웹 서비스, `/healthz` 검사와 한 번 생성되어 계속 유지되는 `PROXY_SECRET`을 설정합니다. Render가 제공하는 `PORT`와 `RENDER_EXTERNAL_URL`을 앱이 자동으로 사용하므로 별도 URL 환경변수를 입력할 필요가 없습니다. [Render Blueprint 문서](https://render.com/docs/blueprint-spec)와 [기본 환경변수 문서](https://render.com/docs/environment-variables)를 참고하세요.

GitHub 저장소의 프로젝트 루트에 이 파일들을 올린 다음 Render에서 **New → Blueprint**를 선택해 저장소를 연결하고 생성하면 됩니다. 자동 배포는 꺼 두었으므로 이후 변경은 Render 대시보드에서 수동 배포해야 합니다. 서비스가 만들어지면 Render가 표시하는 `https://<서비스명>.onrender.com` 주소를 BitChord의 애드온 기본 주소로 사용하세요.

무료 웹 서비스는 인바운드 요청이 15분 없으면 잠들고, 다음 요청 때 다시 시작하는 데 약 1분 걸릴 수 있습니다. 따라서 오래 계속 듣는 스트림은 중간에 끊기거나 첫 연결이 늦어질 수 있습니다. [Render 무료 플랜 제한](https://render.com/docs/free)을 확인하세요.

## API

- `GET /manifest.json` — addon 정보
- `GET /search?q=KBS%201FM` — 검색 결과
- `GET /stream/{id}` — 재생용 안정 URL 반환
- `/live/{id}/master.m3u8`, `/proxy/{id}` — HLS 재생목록·세그먼트 중계
- `/continuous/{id}.{container}` — 지원되는 AAC·MP3·MPEG-TS HLS 조각을 연속 스트림으로 전송
- `/direct/{id}` — 프로그레시브 오디오 중계
- `GET /healthz` — 상태 확인

## 유의사항

- `stations_seed.json`은 목록 장애 시 검색을 살리기 위한 기본 스냅샷입니다. 정상 갱신 시 디렉터리 목록을 6시간마다 새로 읽습니다.
- 채널 목록 및 주소 조회는 제3자 디렉터리 `radio.bsod.kr`에 의존하므로, 목록·스트림이 바뀌거나 일시 중단될 수 있습니다.
- 디렉터리는 JIBS 뉴파워FM을 `SBS / PowerFM / Jeju`로 분류하고 `123.140.197.22`의 HTTP HLS 재생목록을 돌려줍니다. 확인한 재생목록은 오디오 조각을 `media.jibs.co.kr`의 HTTPS 주소에서 가져옵니다. 애드온은 이 방송 조합과 고정 호스트만 허용하며, 최초 재생목록 요청은 암호화되지 않습니다.
- 모든 방송사가 같은 방식으로 계속 열리는 것을 보장하지 않습니다. 오디오는 일시적으로 중계되지만 저장하지 않습니다. 이용 가능 여부와 각 방송사의 이용 조건을 따라 주세요.
- 로그에는 서명 URL이나 쿼리 문자열을 기록하지 않습니다.

BitChord addon 안내: [공식 v1.9.1 릴리스](https://github.com/kushagrasinghx/BitChord/releases/tag/v1.9.1) · 채널 디렉터리: [radio.bsod.kr](https://radio.bsod.kr/)
