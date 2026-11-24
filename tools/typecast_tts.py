"""Typecast TTS: 대본 전체를 "한 번에" 하나의 음성 파일로 생성하고, 글자 단위 타임스탬프를 함께 저장한다.

사용:
  python tools/typecast_tts.py ep01-mom-thumb            # 이미 있으면 건너뜀(크레딧 절약)
  python tools/typecast_tts.py ep01-mom-thumb --force    # 다시 생성
  python tools/typecast_tts.py ep01-mom-thumb --mock     # API 없이 파이프라인 점검용 가짜 음성

출력:
  episodes/<ep>/audio/narration.wav
  episodes/<ep>/audio/tts_timestamps.json   (words/characters 타임스탬프, 초 단위)
  episodes/<ep>/audio/tts_request.json      (재현용 요청 내용, API 키 제외)
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import sys
import wave

import requests
from dotenv import load_dotenv

from common import ROOT, load_episode, setup_stdout, tts_text

API = "https://api.typecast.ai"
MAX_CHARS = 2000


def build_payload(cfg: dict, voice_id: str, text: str) -> dict:
    t = cfg["tts"]
    if t["emotion"] == "smart":
        prompt = {"emotion_type": "smart"}
    else:
        prompt = {
            "emotion_type": "preset",
            "emotion_preset": t["emotion_preset"],
            "emotion_intensity": float(t["emotion_intensity"]),
        }
    output = {"audio_format": "wav", "audio_tempo": float(t["tempo"])}
    if t.get("target_lufs") is not None:
        output["target_lufs"] = float(t["target_lufs"])
    return {
        "voice_id": voice_id,
        "text": text,
        "model": t["model"],
        "language": t["language"],
        "prompt": prompt,
        "output": output,
    }


def make_mock(text: str, wav_path, sr: int = 24000):
    """API 없이 렌더/싱크 파이프라인을 점검하기 위한 가짜 음성(글자마다 작은 톤) + 타임스탬프."""
    chars, t = [], 0.3
    for ch in text:
        if ch.isspace():
            d = 0.06
        elif ch in ".!?":
            d = 0.38
        elif ch in ",":
            d = 0.22
        elif ch in "\"'…":
            d = 0.02
        else:
            d = 0.12
        chars.append({"text": ch, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    total = t + 0.3
    n = int(total * sr)
    buf = bytearray()
    spans = [(c["start"], c["end"]) for c in chars if c["text"].strip() and c["text"] not in ".,!?\"'…"]
    j = 0
    for i in range(n):
        tt = i / sr
        while j < len(spans) and spans[j][1] < tt:
            j += 1
        v = 0.0
        if j < len(spans) and spans[j][0] <= tt <= spans[j][1]:
            v = 0.15 * math.sin(2 * math.pi * 220 * tt)
        buf += struct.pack("<h", int(v * 32767))
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(buf))
    words, cur = [], None
    for c in chars:
        if c["text"].isspace():
            if cur:
                words.append(cur)
                cur = None
            continue
        if cur is None:
            cur = dict(c)
        else:
            cur["text"] += c["text"]
            cur["end"] = c["end"]
    if cur:
        words.append(cur)
    return {"audio_duration": round(total, 3), "words": words, "characters": chars}


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 생성")
    ap.add_argument("--mock", action="store_true", help="API 없이 가짜 음성 생성(점검용)")
    args = ap.parse_args()

    ep_dir, cfg = load_episode(args.episode)
    audio_dir = ep_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    wav_path = audio_dir / "narration.wav"
    ts_path = audio_dir / "tts_timestamps.json"

    if wav_path.exists() and ts_path.exists() and not args.force:
        old = json.loads(ts_path.read_text(encoding="utf-8"))
        if old.get("text") == tts_text(cfg) and (old.get("source") == "mock") == args.mock:
            print(f"[TTS] 이미 생성됨 → 건너뜀 ({wav_path.name}). 다시 만들려면 --force")
            return
        print("[TTS] 대본 또는 모드가 바뀌어서 다시 생성합니다.")

    text = tts_text(cfg)
    if len(text) > MAX_CHARS:
        sys.exit(f"[오류] 대본이 {len(text)}자 — Typecast 한 번 요청 한도({MAX_CHARS}자) 초과")

    if args.mock:
        data = make_mock(text, wav_path)
        meta = {"source": "mock", "text": text, **data}
        ts_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[TTS-mock] {wav_path} ({data['audio_duration']:.2f}s) — 실제 음성 아님, 점검용")
        return

    load_dotenv(ROOT / ".env")
    key = os.getenv("TYPECAST_API_KEY", "").strip()
    if not key:
        sys.exit("[오류] .env 에 TYPECAST_API_KEY 가 없음 (.env.example 참고)")
    voice_id = (cfg["tts"].get("voice_id") or os.getenv("TYPECAST_VOICE_ID", "")).strip()
    if not voice_id:
        sys.exit("[오류] voice_id 없음 → python tools/list_voices.py 로 고른 뒤 episode.yaml tts.voice_id 에 기록")

    payload = build_payload(cfg, voice_id, text)
    print(f"[TTS] Typecast 요청: {len(text)}자, voice={voice_id}, model={payload['model']}")
    r = requests.post(
        f"{API}/v1/text-to-speech/with-timestamps",
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json=payload,
        timeout=600,
    )
    if r.status_code != 200:
        sys.exit(f"[오류] Typecast {r.status_code}: {r.text[:500]}")
    data = r.json()
    audio = base64.b64decode(data["audio"])
    fmt = data.get("audio_format", "wav")
    if fmt != "wav":
        sys.exit(f"[오류] 예상치 못한 오디오 형식: {fmt}")
    wav_path.write_bytes(audio)
    meta = {
        "source": "typecast",
        "text": text,
        "voice_id": voice_id,
        "model": payload["model"],
        "audio_duration": data.get("audio_duration"),
        "words": data.get("words"),
        "characters": data.get("characters"),
    }
    ts_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (audio_dir / "tts_request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    n_chars = len(meta["characters"] or [])
    print(f"[TTS] 완료 → {wav_path} ({meta['audio_duration']}s, 글자 타임스탬프 {n_chars}개)")


if __name__ == "__main__":
    main()
