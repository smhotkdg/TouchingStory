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
        "font": "",              # 비우면 assets/fonts/Gaegu-Bold.ttf (없으면 자동 다운로드)
        "center_y": 760,
        "font_size": 64,
        "max_chars": 14,
        "max_rows": 2,
        "side_margin": 90,
        "line_spacing": 1.28,
        "color": "#1a1a1a",
        "stroke_width": 7,
        "stroke_color": "#ffffff",
        "fade": 0.25,
        "pre": 0.10,             # 음성 시작보다 먼저 자막을 띄우는 시간
        "post": 0.25,            # 음성 끝난 뒤 자막 유지 시간
        "draft": {"font_size": 56, "max_chars": 16, "color": "#444444", "blink_period": 0.45,
                  "post": 0.9},           # 타이핑이 끝난 뒤 문장이 다 보이는 시간
    },
    "audio": {
        "bgm": "",
        "bgm_volume_db": -22,
        "duck": True,
        "fade_out": 2.0,
    },
}

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/gaegu/Gaegu-Bold.ttf"
FONT_LICENSE_URL = "https://github.com/google/fonts/raw/main/ofl/gaegu/OFL.txt"


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


def tts_text(cfg: dict) -> str:
    """TTS에 보내는 전체 대본 (줄을 공백으로 이어 붙임)."""
    return " ".join(line["text"] for line in cfg["lines"])


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
    """자막 폰트 경로. 지정 폰트 → Gaegu Bold(자동 다운로드) → 맑은 고딕 Bold → Noto CJK 순."""
    if cfg_font:
        p = Path(cfg_font)
        p = p if p.is_absolute() else ROOT / p
        if p.exists():
            return p
        print(f"[경고] 지정 폰트 없음: {p}")
    gaegu = ROOT / "assets" / "fonts" / "Gaegu-Bold.ttf"
    if not gaegu.exists():
        try:
            import requests

            gaegu.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(FONT_URL, timeout=60)
            if r.ok and r.content[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                gaegu.write_bytes(r.content)
                lic = requests.get(FONT_LICENSE_URL, timeout=60)
                if lic.ok:
                    (gaegu.parent / "OFL-Gaegu.txt").write_bytes(lic.content)
                print(f"[폰트] Gaegu Bold 다운로드 완료 → {gaegu}")
        except Exception as e:  # 네트워크 없음 등
            print(f"[경고] Gaegu 폰트 다운로드 실패: {e}")
    if gaegu.exists():
        return gaegu
    for cand in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "malgunbd.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ):
        if cand.exists():
            print(f"[경고] Gaegu 대신 대체 폰트 사용: {cand}")
            return cand
    sys.exit("[오류] 한글 폰트를 찾을 수 없음 → assets/fonts/ 에 Gaegu-Bold.ttf 를 넣어 주세요")


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
