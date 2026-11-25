"""공통 유틸: 에피소드 로드, 기본 설정, 경로, ffmpeg/폰트 찾기."""
from __future__ import annotations

import copy
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "tts": {
        "model": "ssfm-v31",
        "language": "kor",
        "voice_name": "",
        "voice_id": "",
        "emotion": "smart",
        "emotion_preset": "normal",
        "emotion_intensity": 1.0,
        "tempo": 1.0,
        "target_lufs": -16,
    },
    "video": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "lead_in": 0.4,
        "scene_lead": 0.15,      # 이미지 전환 시점 = 해당 줄 음성 시작 0.15초 전
        "start_fade": 0.35,
        "tail_hold": 1.5,
        "end_fade": 0.8,
        "zoom_from": 1.0,
        "zoom_to": 1.05,
        "transition": "white_dip",
        "transition_dur": 0.35,
        "crf": 20,
        "preset": "veryfast",
    },
    "subtitle": {
        # 내레이션: 손글씨(나눔손글씨 펜) — 딱딱하지 않고 따뜻한 느낌
        "font": "assets/fonts/NanumPenScript-Regular.ttf",
        "font_size": 92,
        "line_spacing": 1.08,
        "max_chars": 14,
        "max_rows": 2,
        "center_y": 760,
        "side_margin": 90,
        "color": "#2e2a27",          # 새까만 검정 대신 부드러운 먹색
        "accent_color": "#9c5b3f",   # {강조} 어절 — 따뜻한 갈색
        "stroke_width": 3,           # 얇은 흰 외곽선
        "stroke_color": "#ffffff",
        "glow_radius": 9,            # 글자 주변 옅은 흰 번짐(배경 패널 아님)
        "glow_spread": 9,
        "glow_opacity": 0.85,
        "reveal": "word",            # word: 음성에 맞춰 어절마다 떠오르며 등장 | line: 한 번에
        "word_fade": 0.3,
        "word_rise": 12,
        "fade": 0.45,                # 줄이 사라지는 페이드
        "pre": 0.10,
        "post": 0.6,
        # 엄마의 임시저장 문자: 둥근 고딕(고운돋움), 회색, 한 글자씩 타이핑 + 커서
        "draft": {"font": "assets/fonts/GowunDodum-Regular.ttf", "font_size": 60, "line_spacing": 1.3,
                  "max_chars": 16, "color": "#4a4744", "post": 0.9, "cursor": True, "blink_period": 0.5},
    },
    "audio": {
        "bgm": "",
        "bgm_volume_db": -22,
        "duck": True,
        "fade_out": 2.0,
    },
}

# 레포에 포함된 폰트(OFL). 없으면 Google Fonts 저장소에서 내려받는다.
FONT_DIRS = {
    "NanumPenScript-Regular.ttf": "nanumpenscript",
    "GowunDodum-Regular.ttf": "gowundodum",
    "GowunBatang-Bold.ttf": "gowunbatang",
    "Gaegu-Bold.ttf": "gaegu",
}
FONT_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl"


def setup_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_episode(ep: str):
    """ep: 에피소드 id(episodes/ 아래 폴더명) 또는 폴더 경로."""
    p = Path(ep)
    if not (p / "episode.yaml").exists():
        p = ROOT / "episodes" / ep
    if not (p / "episode.yaml").exists():
        sys.exit(f"[오류] episode.yaml 을 찾을 수 없음: {ep}")
    raw = yaml.safe_load((p / "episode.yaml").read_text(encoding="utf-8"))
    cfg = _merge(DEFAULTS, {k: v for k, v in raw.items() if k in DEFAULTS})
    for k, v in raw.items():
        if k not in DEFAULTS:
            cfg[k] = v
    if not cfg.get("lines"):
        sys.exit("[오류] episode.yaml 에 lines 가 없음")
    for i, line in enumerate(cfg["lines"]):
        line.setdefault("style", "normal")
        line["text"] = str(line["text"]).strip()
        if not line.get("scene"):
            sys.exit(f"[오류] {i + 1}번째 줄에 scene 이 없음")
    return p.resolve(), cfg


def strip_markup(s: str) -> str:
    """자막 강조 표기 {…} 제거 → 실제로 읽는 문장."""
    return s.replace("{", "").replace("}", "")


def tts_text(cfg: dict) -> str:
    """TTS에 보내는 전체 대본 (줄을 공백으로 이어 붙임, 강조 표기 제거)."""
    return " ".join(strip_markup(line["text"]) for line in cfg["lines"])


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("[오류] ffmpeg 를 찾을 수 없음 → pip install imageio-ffmpeg 또는 ffmpeg 설치")


def find_image(ep_dir: Path, scene: str, placeholders: bool = False) -> Path | None:
    folders = [ep_dir / "images"]
    if placeholders:
        folders.append(ep_dir / "images" / "_placeholders")
    for folder in folders:
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            f = folder / f"{scene}{ext}"
            if f.exists():
                return f
    return None


def ensure_font(cfg_font: str = "") -> Path:
    """자막 폰트 경로. 지정 폰트(없으면 Google Fonts에서 받기) → 맑은 고딕 Bold → Noto CJK 순."""
    cfg_font = cfg_font or "assets/fonts/NanumPenScript-Regular.ttf"
    p = Path(cfg_font)
    p = p if p.is_absolute() else ROOT / p
    if not p.exists() and p.name in FONT_DIRS:
        try:
            import requests

            p.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(f"{FONT_BASE}/{FONT_DIRS[p.name]}/{p.name}", timeout=120)
            if r.ok and r.content[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                p.write_bytes(r.content)
                print(f"[폰트] 다운로드 완료 → {p}")
        except Exception as e:
            print(f"[경고] 폰트 다운로드 실패: {e}")
    if p.exists():
        return p
    print(f"[경고] 폰트 없음: {p}")
    for cand in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgunbd.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        if cand.exists():
            print(f"[경고] 대체 폰트 사용: {cand}")
            return cand
    sys.exit("[오류] 한글 폰트를 찾을 수 없음 → assets/fonts/ 에 폰트를 넣어 주세요")


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
