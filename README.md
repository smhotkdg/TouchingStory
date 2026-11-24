# TouchingStory

흑백 인스타툰풍 그림 + Typecast 내레이션 + 페이드 인/아웃으로 만드는 감동 이야기 쇼츠 제작 레포.

## 처음 한 번
```bash
pip install -r requirements.txt
copy .env.example .env      # (mac/linux: cp) 후 TYPECAST_API_KEY 입력
```

## 에피소드 만들기
1. `episodes/<ep>/gpt_prompts.md` 를 ChatGPT에 붙여 넣고 이미지를 `images/sceneXX.png` 로 저장
2. 로컬 Claude Code에서 실행(또는 직접 실행):
   ```bash
   python tools/run_episode.py ep01-mom-thumb
   ```
   Typecast 음성 생성(대본 전체를 한 번에) → 글자 타임스탬프로 싱크 → 렌더 → 검증까지 자동
3. 결과: `episodes/<ep>/output/<ep>.mp4`, 검증 리포트 `output/check/report.md`, 업로드 문구 `output/upload.txt`

## 도구
| 스크립트 | 하는 일 |
|---|---|
| `tools/list_voices.py` | Typecast 보이스 목록·미리듣기 |
| `tools/typecast_tts.py` | 대본 전체를 한 파일로 TTS + 타임스탬프 저장 (`--mock` 점검용) |
| `tools/align.py` | 타임스탬프 → 줄별 시작/끝 (`timeline.json`) |
| `tools/render.py` | 1080x1920 렌더 (중앙 정렬 크롭·줌, 흰색 페이드, 자막) |
| `tools/verify.py` | 싱크(타임스탬프 + Whisper 교차) · 가로 중앙 · 길이 검증 |
| `tools/make_placeholders.py` | 이미지 없이 점검할 때 쓰는 임시 이미지 |
| `tools/run_episode.py` | 위 과정을 한 번에 |

자세한 규칙은 [CLAUDE.md](CLAUDE.md).
