"""렌더 결과 검증 → episodes/<ep>/output/check/report.md

1. 싱크(기준): Typecast 글자 타임스탬프 ↔ 대본 매칭률, 자막 표시 구간이 음성 구간을 덮는지
2. 싱크(교차 확인): 완성 mp4 오디오를 faster-whisper로 받아쓰기 → 줄별 내용 일치·시작 오차
3. 자막 가로 중앙: 자막 오버레이의 좌우 여백 차이(≤1px) + 실제 mp4 프레임에서 글자 좌우 여백 차이(≤4px)
4. 이미지 가로 중앙: 크롭/줌 변환이 좌우 대칭인지
5. 자막 줄 수(≤2), 플레이스홀더 사용 여부, 영상 길이

사용: python tools/verify.py ep01-mom-thumb [--no-whisper] [--allow-placeholders]
종료 코드: 실패 항목이 있으면 1
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

from common import ffmpeg_exe, load_episode, norm, setup_stdout
from render import Renderer

PUNCT_RE = re.compile(r"[\s.,!?\"'…~·]")


def clean(s: str) -> str:
    return PUNCT_RE.sub("", s)


class Report:
    def __init__(self):
        self.items = []

    def add(self, name, ok, detail="", level=None):
        lv = level or ("PASS" if ok else "FAIL")
        self.items.append((lv, name, detail))
        print(f"[{lv}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self):
        return any(lv == "FAIL" for lv, _, _ in self.items)


def mp4_duration(path: Path) -> float | None:
    p = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True, errors="ignore")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def extract_frames(mp4: Path, idxs: list[int], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("_raw_*.png"):
        f.unlink()
    expr = "+".join(f"eq(n\\,{i})" for i in idxs)
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(mp4), "-vf", f"select={expr}",
                    "-vsync", "0", str(out_dir / "_raw_%03d.png")], check=True)
    return sorted(out_dir.glob("_raw_*.png"))


def sample_times(r: Renderer):
    """페이지마다 자막이 완전히 보이는 시점 하나."""
    out = []
    for li, sub in enumerate(r.subs):
        for pi, pg in enumerate(sub.pages):
            t0, t1 = pg["t0"] + 0.2, pg["t1"] - 0.2
            if sub.style == "draft":
                t0 = max(t0, sub.times[min(pg["c1"], len(sub.times)) - 1] + 0.05)
            t = t0 if t1 <= t0 else (t0 + min(t1, t0 + 0.4)) / 2
            # 자막이 완전히 보이는 순간 찾기 (draft 깜빡임으로 ... 이 숨은 순간은 피함)
            ok = False
            while t < pg["t1"]:
                st = sub.state(t)
                if (st and st[0] == pi and st[2] > 0.99 and (st[1] is None or st[1] >= pg["c1"])
                        and not sub.cursor_visible(t)):
                    ok = True
                    break
                t += 1 / r.fps
            out.append((li, pi, t if ok else None))
    return out


def check_overlay_centering(r: Renderer, rep: Report):
    worst, bad, too_many = 0, [], []
    max_rows = int(r.cfg["subtitle"]["max_rows"])
    for li, sub in enumerate(r.subs):
        for pi, pg in enumerate(sub.pages):
            if len(pg["rows"]) > max_rows:
                too_many.append(f"{li + 1}번 줄 {len(pg['rows'])}행")
            # 행마다 실제로 따로 그려서 잉크 좌우 여백 확인 (각 행이 개별적으로 중앙이어야 함)
            for ri, (row, x, y) in enumerate(sub.layout(pg)):
                canvas = Image.new("L", (r.W, sub.line_h * 3), 0)
                ImageDraw.Draw(canvas).text((x, sub.line_h), row, font=sub.font, fill=255,
                                            stroke_width=sub.stroke, stroke_fill=255)
                cols = np.where(np.array(canvas).max(axis=0) > 0)[0]
                if not len(cols):
                    continue
                left, right = int(cols[0]), r.W - 1 - int(cols[-1])
                diff = abs(left - right)
                worst = max(worst, diff)
                if diff > 1:
                    bad.append(f"{li + 1}번 줄 p{pi + 1} {ri + 1}행: 좌{left}/우{right}")
    rep.add("자막 오버레이 가로 중앙(≤1px)", not bad,
            f"최대 좌우 차이 {worst}px" + (f"; {', '.join(bad)}" if bad else ""))
    rep.add(f"자막 줄 수(페이지당 ≤{max_rows}행)", not too_many, ", ".join(too_many))


def check_image_centering(r: Renderer, rep: Report):
    V = r.cfg["video"]
    worst = 0.0
    for scene, img in r.images.items():
        ih, iw = img.shape[:2]
        test = np.zeros((ih, iw, 3), np.uint8)
        test[:, iw // 2:] = 255 if iw % 2 == 0 else 0
        if iw % 2:  # 홀수 폭: 가운데 열 기준 대칭 패턴
            test[:, iw // 2 + 1:] = 255
            test[:, iw // 2] = 128
        for z in (float(V["zoom_from"]), float(V["zoom_to"])):
            w = r._warp(test, z)[r.H // 2, :, 0].astype(float)
            # 좌우 대칭이면 w[x] + w[W-1-x] ≈ 255
            asym = np.abs(w + w[::-1] - 255).max()
            worst = max(worst, asym)
        crop = (iw * max(r.W / iw, r.H / ih) - r.W) / 2
        if crop > 0.2 * r.W:
            rep.add(f"{scene} 비율", True, f"좌우 각각 {crop:.0f}px 잘림 — 중요한 내용이 가장자리에 있는지 확인", "WARN")
    rep.add("이미지 크롭·줌 가로 대칭", worst <= 2, f"대칭 오차 최대 {worst:.1f} (밝기값)")


def check_frames(r: Renderer, mp4: Path, check_dir: Path, rep: Report):
    samples = sample_times(r)
    never = [f"{li + 1}-p{pi + 1}" for li, pi, t in samples if t is None]
    rep.add("자막이 완전히 보이는 순간 존재", not never, f"없음: {', '.join(never)}" if never else "")
    samples = [x for x in samples if x[2] is not None]
    idxs = sorted({int(round(t * r.fps)) for _, _, t in samples})
    files = extract_frames(mp4, idxs, check_dir)
    by_idx = dict(zip(idxs, files))
    S = r.cfg["subtitle"]
    band0 = max(0, int(S["center_y"]) - 260)
    band1 = min(r.H, int(S["center_y"]) + 260)
    worst, missing, lines = 0, [], []
    for li, pi, t in samples:
        k = int(round(t * r.fps))
        f = by_idx.get(k)
        if f is None:
            missing.append(f"{li + 1}")
            continue
        got = cv2.cvtColor(cv2.imread(str(f)), cv2.COLOR_BGR2RGB)
        base = r.frame(k / r.fps, with_subs=False)
        diff = np.abs(got.astype(int) - base.astype(int)).max(axis=2)
        gray = got.mean(axis=2)
        mask = (diff > 40) & (gray < 140)  # 글자색·강조색(따뜻한 갈색)만, 흰 번짐 제외
        mask[:band0] = False
        mask[band1:] = False
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)) > 0
        cols = np.where(mask.any(axis=0))[0]
        rows = np.where(mask.any(axis=1))[0]
        name = f"line{li + 1:02d}_p{pi + 1}.png"
        f.rename(check_dir / name)
        if len(cols) < 2:
            missing.append(f"{li + 1}")
            continue
        left, right = int(cols[0]), r.W - 1 - int(cols[-1])
        d = abs(left - right)
        worst = max(worst, d)
        cy = (rows[0] + rows[-1]) / 2
        lines.append(f"{li + 1}-p{pi + 1}: 좌{left}/우{right} (차이 {d}px), 세로중심 {cy:.0f}px")
    for f in check_dir.glob("_raw_*.png"):
        f.unlink()
    rep.add("실제 프레임 자막 가로 중앙(≤4px, 압축 오차 포함)", worst <= 4 and not missing,
            f"최대 차이 {worst}px" + (f"; 자막 미검출: {', '.join(missing)}번 줄" if missing else ""))
    return lines


def check_whisper(r: Renderer, mp4: Path, check_dir: Path, rep: Report, model_size: str):
    if r.tl.get("source") == "mock":
        rep.add("Whisper 교차 확인", True, "mock 음성이라 건너뜀", "SKIP")
        return []
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        rep.add("Whisper 교차 확인", True, "faster-whisper 미설치 → pip install faster-whisper", "WARN")
        return []
    wav = check_dir / "_audio16k.wav"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "16000",
                    str(wav)], check=True)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(wav), language="ko", word_timestamps=True, vad_filter=False,
                               initial_prompt=" ".join(l["text"] for l in r.tl["lines"])[:200])
    words = [w for s in segs for w in (s.words or [])]
    wav.unlink(missing_ok=True)
    rows, sims, offs = [], [], []
    for l in r.tl["lines"]:
        a0, a1 = l["start"] + r.lead, l["end"] + r.lead
        inside = [w for w in words if a0 - 0.15 <= (w.start + w.end) / 2 <= a1 + 0.15]
        heard = "".join(w.word for w in inside)
        sim = SequenceMatcher(None, clean(heard), clean(l["text"])).ratio()
        sims.append(sim)
        off = None
        if inside:
            off = inside[0].start - a0
            offs.append(off)
        rows.append(f"{l['index'] + 1:>2}. 유사도 {sim:.2f}, 시작오차 "
                    f"{'-' if off is None else f'{off:+.2f}s'} | 들림: {heard.strip()[:40]}")
    ok_lines = sum(s >= 0.6 for s in sims)
    med = statistics.median(offs) if offs else 0
    rep.add("Whisper 내용 일치(줄별 유사도 ≥0.6, 85% 이상)", ok_lines >= 0.85 * len(sims),
            f"{ok_lines}/{len(sims)}줄 통과")
    rep.add("Whisper 시작 시점 오차(중앙값 ≤0.15s)", abs(med) <= 0.15, f"중앙값 {med:+.3f}s")
    return rows


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--video")
    ap.add_argument("--no-whisper", action="store_true")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--allow-placeholders", action="store_true")
    args = ap.parse_args()

    ep_dir, cfg = load_episode(args.episode)
    mp4 = Path(args.video) if args.video else ep_dir / "output" / f"{cfg['id']}.mp4"
    if not mp4.exists():
        sys.exit(f"[오류] 영상 없음: {mp4}")
    check_dir = mp4.parent / "check"
    check_dir.mkdir(parents=True, exist_ok=True)
    for f in check_dir.glob("line*.png"):
        f.unlink()
    r = Renderer(ep_dir, cfg, placeholders=True)
    rep = Report()

    # 1. 기준 싱크
    tl = r.tl
    low = [l for l in tl["lines"] if l["match_ratio"] < 0.95]
    rep.add("대본↔TTS 타임스탬프 매칭(≥95%)", not low,
            ", ".join(f"{l['index'] + 1}번 {l['match_ratio']:.0%}" for l in low) or f"{len(tl['lines'])}줄 모두 통과")
    cover_bad = [i + 1 for i, (s, l) in enumerate(zip(r.subs, tl["lines"]))
                 if not (s.show0 <= l["start"] + r.lead + 1e-6 and s.show1 >= l["end"] + r.lead - 1e-6)]
    rep.add("자막 표시 구간이 음성 구간을 덮음", not cover_bad, f"문제 줄: {cover_bad}" if cover_bad else "")
    overlap = [i + 1 for i in range(len(r.subs) - 1) if r.subs[i].show1 > r.subs[i + 1].show0 + 1e-6]
    rep.add("자막끼리 겹치지 않음", not overlap, f"문제 줄: {overlap}" if overlap else "")
    if tl.get("source") == "mock":
        rep.add("TTS 음성", True, "mock(가짜) 음성 — 실제 Typecast 음성으로 다시 생성 필요", "WARN")

    # 2. 이미지/플레이스홀더
    if r.used_placeholder:
        rep.add("이미지", args.allow_placeholders, f"플레이스홀더 사용: {', '.join(r.used_placeholder)}",
                "WARN" if args.allow_placeholders else "FAIL")
    check_image_centering(r, rep)

    # 3. 자막 중앙
    check_overlay_centering(r, rep)
    frame_rows = check_frames(r, mp4, check_dir, rep)

    # 4. 길이
    dur = mp4_duration(mp4)
    rep.add("영상 길이", dur is not None and abs(dur - r.total) < 0.15, f"{dur:.2f}s (예상 {r.total:.2f}s)")

    # 5. Whisper
    w_rows = [] if args.no_whisper else check_whisper(r, mp4, check_dir, rep, args.whisper_model)
    if args.no_whisper:
        rep.add("Whisper 교차 확인", True, "--no-whisper", "SKIP")

    md = [f"# 검증 리포트 — {cfg['id']}", "", f"- 영상: `{mp4.name}` ({r.total:.1f}s)", ""]
    md += ["| 결과 | 항목 | 내용 |", "|---|---|---|"]
    md += [f"| {lv} | {n} | {d} |" for lv, n, d in rep.items]
    md += ["", "## 줄별 타임라인(영상 기준 초)", ""]
    md += [f"- {l['index'] + 1}. {l['start'] + r.lead:.2f}~{l['end'] + r.lead:.2f} [{l['scene']}] {l['text']}"
           for l in tl["lines"]]
    md += ["", "## 프레임 자막 위치", ""] + [f"- {x}" for x in frame_rows]
    if w_rows:
        md += ["", "## Whisper 교차 확인", ""] + [f"- {x}" for x in w_rows]
    (check_dir / "report.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n리포트 → {check_dir / 'report.md'}  (프레임 이미지: {check_dir})")
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
