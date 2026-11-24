"""한 번에 실행: TTS 생성(없을 때만) → 정렬 → 렌더 → 검증 → 업로드 문구 복사.

사용:
  python tools/run_episode.py ep01-mom-thumb                 # 실제 제작
  python tools/run_episode.py ep01-mom-thumb --force-tts     # 음성 다시 생성
  python tools/run_episode.py ep01-mom-thumb --mock --placeholders   # 키·이미지 없이 파이프라인 점검
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from common import load_episode, setup_stdout

TOOLS = Path(__file__).resolve().parent


def run(args: list[str]) -> int:
    print(f"\n$ python {' '.join(args)}")
    return subprocess.call([sys.executable, str(TOOLS / args[0]), *args[1:]])


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--force-tts", action="store_true")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--placeholders", action="store_true")
    ap.add_argument("--no-whisper", action="store_true")
    args = ap.parse_args()
    ep_dir, cfg = load_episode(args.episode)
    ep = str(ep_dir)

    tts = ["typecast_tts.py", ep] + (["--force"] if args.force_tts else []) + (["--mock"] if args.mock else [])
    if run(tts) != 0:
        sys.exit(1)
    if run(["align.py", ep]) != 0:
        sys.exit(1)
    if args.placeholders and run(["make_placeholders.py", ep]) != 0:
        sys.exit(1)
    if run(["render.py", ep] + (["--placeholders"] if args.placeholders else [])) != 0:
        sys.exit(1)
    ver = ["verify.py", ep]
    if args.no_whisper:
        ver.append("--no-whisper")
    if args.placeholders:
        ver.append("--allow-placeholders")
    rc = run(ver)
    up = ep_dir / "upload.md"
    if up.exists():
        shutil.copy(up, ep_dir / "output" / "upload.txt")
    print("\n" + ("✅ 완료" if rc == 0 else "❌ 검증 실패 — output/check/report.md 확인 후 수정"))
    sys.exit(rc)


if __name__ == "__main__":
    main()
