# TouchingStory — 감동 이야기 쇼츠 제작

흑백 인스타툰풍 이미지(ChatGPT로 생성) + Typecast TTS 내레이션 + 페이드 인/아웃으로 만드는 감동 스토리 YouTube Shorts.

## 작업 방식 (가장 중요)
- **중간에 확인을 묻지 말고 한 번에 끝까지 진행한다.** 오류가 나면 원인을 찾아 스스로 고치고 다시 실행한다.
- 실제 사용자 결정이 필요한 경우(대본 내용 변경, 크레딧을 많이 쓰는 재생성 반복 등)만 마지막에 정리해서 알린다.
- 끝나면 `output/check/report.md` 결과와 함께 쇼츠 **제목 후보 + 설명 텍스트**(`upload.md`)를 같이 보여 준다.

## 분업
1. Claude(채팅): 스토리·대본(`episode.yaml`), GPT 이미지 요청(`gpt_prompts.md`), 제목·설명(`upload.md`)
2. 사용자: ChatGPT로 이미지 생성 → `episodes/<ep>/images/sceneXX.png` 에 넣기
3. 로컬 Claude Code: Typecast TTS 생성 → 싱크 정렬 → 렌더 → 검증 → 보고

## 실행
```bash
pip install -r requirements.txt
python tools/run_episode.py ep01-mom-thumb              # 전체 (TTS는 없을 때만 생성)
python tools/run_episode.py ep01-mom-thumb --force-tts  # 대본 수정 후 음성 다시 생성
python tools/run_episode.py ep01-mom-thumb --mock --placeholders --no-whisper   # 키·이미지 없이 점검
```
단계별: `typecast_tts.py` → `align.py` → `render.py` → `verify.py` (모두 `tools/`, 인자는 에피소드 id)

## TTS 규칙 (Typecast)
- `.env` 에 `TYPECAST_API_KEY`(필수), `TYPECAST_VOICE_ID`(선택). **.env 는 절대 커밋하지 않는다.**
- **대본 전체를 반드시 한 번의 요청으로 하나의 음성 파일로 생성한다.** 문장별로 나눠 생성하면 감정 흐름이 끊기므로 금지.
- 엔드포인트 `POST /v1/text-to-speech/with-timestamps` → 음성 + 글자 단위 타임스탬프를 같이 받아 싱크 기준으로 쓴다.
- 이미 생성된 음성은 대본이 같으면 다시 만들지 않는다(크레딧 절약). 대본을 바꿨으면 `--force-tts`.
- **기본 목소리는 Typecast 필재**(중저음 중년 남성). `episode.yaml` 의 `tts.voice_name: 필재` 로 두면 첫 실행 때
  API 보이스 목록에서 찾아 `tts.voice_id` 와 지원 모델을 자동 기록한다. 목록에 없으면(새 에디터 전용 등) 멈추지 말고
  `python tools/list_voices.py --name 필재` 결과와 함께 원인을 보고한다. 다른 목소리로 임의 대체하지 않는다.
- 감정은 기본 `smart`(문맥 기반). 너무 밝거나 어색하면 `emotion: preset`, `emotion_preset: sad` 또는 `tonedown` 으로 바꿔 재생성.

## 영상 규칙
- 1080x1920, 30fps. 이미지는 높이에 맞춰 커버 스케일 후 **좌우 대칭 크롭 → 정확히 가로 중앙**.
- 장면마다 느린 줌인(1.00→1.05), 줌 중심은 정중앙 고정. 전환은 흰색 페이드(white_dip).
- **자막 위치: 화면 맨 위·맨 아래가 아니라 중심보다 약간 위**(세로 중심 y≈760, 화면의 약 40%). 아이폰에서 읽기 쉽게.
- **자막에 배경(패널·박스·이미지) 금지.** 부드러운 먹색 글씨 + 얇은 흰 외곽선 + 글자 주변 옅은 흰 번짐만. 최대 2행, 행당 약 14자.
- **상단 제목, 단계별 진행 칩 등 다른 텍스트는 넣지 않는다.**
- 감성 연출: 내레이션 자막은 음성에 맞춰 **어절마다 부드럽게 떠오르며** 나타나고, 줄이 끝나면 천천히 사라진다.
  대본의 `{…}` 부분은 따뜻한 갈색으로 강조(TTS는 괄호 없이 읽음). 강조는 한 줄에 한 곳, 핵심 말에만.
- 긴 대사는 글자 타임스탬프에 맞춰 2행씩 페이지를 넘긴다. `style: draft` 줄(엄마 문자)은 둥근 글씨로 음성에 맞춰 한 글자씩 타이핑 + 커서.
- 폰트(레포 `assets/fonts/`, OFL): 내레이션 **나눔손글씨 펜**, 문자 **고운돋움**. 딱딱한 고딕 금지.
  리포트에 "대체 폰트 사용" 경고가 뜨면 폰트 경로를 고치고 다시 렌더.
- BGM: `bgm/` 폴더의 첫 파일(또는 `audio.bgm`), 내레이션보다 충분히 작게(-22dB) + 덕킹. 비어 있으면 저작권 문제없는 오픈 라이선스 잔잔한 피아노 곡을 찾아 넣고 출처를 `bgm/CREDITS.md` 에 기록.

## 검증 (렌더 후 반드시, `verify.py`)
- 싱크: 대본↔Typecast 타임스탬프 매칭 ≥95%, 자막 표시 구간이 음성 구간을 덮고 서로 겹치지 않음
- 교차 확인: 완성 mp4 오디오를 faster-whisper로 받아쓰기 → 줄별 내용 일치, 시작 오차 중앙값 ≤0.15s
- 가로 중앙: 자막 행마다 좌우 여백 차이 ≤1px(오버레이), 실제 프레임 ≤4px(압축 오차), 이미지 크롭 대칭
- `output/check/line*.png` 프레임을 직접 열어 눈으로도 확인(글자 잘림, 이미지와 겹쳐 안 읽히는 곳)
- FAIL 이 있으면 고쳐서 다시 렌더·검증. 플레이스홀더 이미지가 남아 있으면 업로드용이 아님.

## 대본 규칙
- 화자는 필재 목소리에 맞게 남성(어른이 된 아들, 아빠, 남편 등) 시점으로 쓴다.
- 마지막 줄은 "댓글 ○○! 좋아요 꾹!" 같은 직접 요청 대신 **질문으로 마무리**
  (예: "여러분 부모님의 답장은 어떤가요? 여러분의 이야기가 궁금합니다.")
- 실화가 아닌 창작 이야기이므로 제목·설명에 "실화"라고 쓰지 않는다.
- 참고한 웹툰 등 다른 작가의 줄거리·캐릭터를 그대로 가져오지 않는다(그림체 분위기만 참고).

## 폴더 구조
```
episodes/<ep>/
  episode.yaml      대본(줄=자막) + 장면 + TTS/영상 설정
  gpt_prompts.md    ChatGPT 이미지 요청(한 번에 복사)
  upload.md         쇼츠 제목 후보 + 설명 텍스트
  images/           scene01.png ... (사용자가 넣음, 2:3 1024x1536 권장)
  audio/            narration.wav, tts_timestamps.json, timeline.json (커밋해서 다른 PC와 공유)
  output/           mp4, check/ (커밋 안 함)
bgm/                배경음악 (+ CREDITS.md)
tools/              파이프라인 스크립트
```

## 새 에피소드
`episodes/ep01-mom-thumb` 를 복사해 `epNN-<slug>` 로 만들고 `episode.yaml`(id·lines), `gpt_prompts.md`, `upload.md` 를 새로 쓴다. `audio/`, `images/`, `output/` 은 비운다.
