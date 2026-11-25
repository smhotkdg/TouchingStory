"""Typecast 보이스 목록 조회 (한국어 이름이 있는 보이스만).

사용:
  python tools/list_voices.py --name 필재
  python tools/list_voices.py --gender female --age young_adult
  python tools/list_voices.py --gender female --previews      # 미리듣기 mp3를 voices_preview/ 에 저장
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

from common import ROOT, setup_stdout

API = "https://api.typecast.ai"


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="ssfm-v31")
    ap.add_argument("--gender", choices=["male", "female"])
    ap.add_argument("--age", choices=["child", "teenager", "young_adult", "middle_age", "elder"])
    ap.add_argument("--name", help="한국어 이름 일부로 찾기 (예: 필재)")
    ap.add_argument("--emotion", help="이 감정을 지원하는 보이스만 (예: sad, tonedown)")
    ap.add_argument("--previews", action="store_true", help="미리듣기 mp3 저장")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("TYPECAST_API_KEY", "").strip()
    if not key:
        sys.exit("[오류] .env 에 TYPECAST_API_KEY 가 없음")
    params = {"model": args.model, "voice_type": "original"}
    if args.gender:
        params["gender"] = args.gender
    if args.age:
        params["age"] = args.age
    r = requests.get(f"{API}/v3/voices", headers={"X-API-KEY": key}, params=params, timeout=60)
    if r.status_code != 200:
        sys.exit(f"[오류] {r.status_code}: {r.text[:300]}")
    voices = r.json()
    if isinstance(voices, dict):
        voices = voices.get("voices") or voices.get("data") or []

    rows = []
    for v in voices:
        name = (v.get("voice_name") or {}).get("kor")
        if not name:
            continue
        emos = sorted({e for m in v.get("models", []) for e in m.get("emotions", [])})
        if args.name and args.name not in name:
            continue
        if args.emotion and args.emotion not in emos:
            continue
        rows.append({**v, "_kor": name, "_emotions": emos})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        for v in rows:
            print(f"{v['voice_id']}  {v['_kor']:<8} {v.get('gender')}/{v.get('age')}  "
                  f"감정={','.join(v['_emotions'])}  용도={','.join(v.get('use_cases') or [])}")
        print(f"\n총 {len(rows)}개")

    if args.previews:
        out = ROOT / "voices_preview"
        out.mkdir(exist_ok=True)
        for v in rows:
            url = v.get("preview_url")
            if not url:
                continue
            try:
                (out / f"{v['_kor']}_{v['voice_id']}.mp3").write_bytes(requests.get(url, timeout=60).content)
            except Exception as e:
                print(f"[경고] 미리듣기 실패 {v['_kor']}: {e}")
        print(f"미리듣기 저장 → {out}")


if __name__ == "__main__":
    main()
