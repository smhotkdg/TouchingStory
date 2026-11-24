"""GPT 이미지가 나오기 전에 파이프라인을 점검하기 위한 플레이스홀더 이미지(1024x1536) 생성.
→ episodes/<ep>/images/_placeholders/sceneXX.png  (render --placeholders 일 때만 사용)

사용: python tools/make_placeholders.py ep01-mom-thumb
"""
from __future__ import annotations

import argparse

from PIL import Image, ImageDraw, ImageFont

from common import ensure_font, load_episode, setup_stdout


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    args = ap.parse_args()
    ep_dir, cfg = load_episode(args.episode)
    out = ep_dir / "images" / "_placeholders"
    out.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(str(ensure_font(cfg["subtitle"].get("font", ""))), 90)
    small = ImageFont.truetype(str(ensure_font(cfg["subtitle"].get("font", ""))), 40)
    scenes = list(dict.fromkeys(l["scene"] for l in cfg["lines"]))
    for i, s in enumerate(scenes):
        W, H = 1024, 1536
        im = Image.new("RGB", (W, H), (250, 250, 248))
        d = ImageDraw.Draw(im)
        # 중앙 85% 안전영역, 9:16 크롭 경계, 자막 영역 표시
        crop = (W - H * 9 / 16) / 2
        d.rectangle([crop, 0, W - crop, H - 1], outline=(200, 200, 200), width=3)
        d.rectangle([W * 0.075, 0, W * 0.925, H - 1], outline=(225, 225, 225), width=2)
        d.rectangle([crop, H * 0.35, W - crop, H * 0.5], fill=(240, 240, 236))
        # 단순한 인물 실루엣(동그란 머리 + 몸)
        cx, cy = W // 2 + (i % 3 - 1) * 120, int(H * 0.72)
        d.ellipse([cx - 80, cy - 260, cx + 80, cy - 100], outline=(30, 30, 30), width=5, fill=(255, 255, 255))
        d.ellipse([cx - 35, cy - 190, cx - 25, cy - 180], fill=(30, 30, 30))
        d.ellipse([cx + 25, cy - 190, cx + 35, cy - 180], fill=(30, 30, 30))
        d.rounded_rectangle([cx - 110, cy - 95, cx + 110, cy + 200], radius=60, outline=(30, 30, 30), width=5,
                            fill=(215, 215, 215))
        d.text((W / 2, H * 0.2), s, font=font, fill=(150, 150, 150), anchor="mm")
        d.text((W / 2, H * 0.27), "PLACEHOLDER", font=small, fill=(170, 170, 170), anchor="mm")
        im.save(out / f"{s}.png")
    print(f"[플레이스홀더] {len(scenes)}장 → {out}")


if __name__ == "__main__":
    main()
