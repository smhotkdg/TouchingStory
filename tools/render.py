"""timeline.json + 이미지 + 내레이션 → 쇼츠 mp4 렌더.

규칙(CLAUDE.md 참고)
- 1080x1920. 이미지는 높이에 맞춰 커버 스케일 → 좌우 대칭 크롭 → 정확히 가로 중앙.
- 장면마다 느린 줌인, 줌 중심은 프레임 정중앙 고정(좌우 이동 없음).
- 전환: white_dip(흰색으로 페이드아웃 → 페이드인) 또는 crossfade.
- 자막: 중심보다 약간 위(center_y), 배경 패널 없음, 검정 글씨 + 흰 외곽선, 최대 2줄.
  긴 줄은 글자 타임스탬프에 맞춰 2줄씩 페이지를 넘긴다. draft 스타일은 음성에 맞춰 타이핑.
- 상단 제목/진행 칩 등 다른 텍스트 없음.

사용: python tools/render.py ep01-mom-thumb [--placeholders] [--out 경로]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import wave
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from common import ensure_font, ffmpeg_exe, find_image, hex_rgb, load_episode, norm, setup_stdout

PUNCT = ".,!?\"'…"


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# ───────────────────────────── 자막 ─────────────────────────────
class Subtitle:
    """한 대본 줄의 자막. 여러 페이지(최대 max_rows 줄씩)로 나뉠 수 있다."""

    def __init__(self, tl_line: dict, sub_cfg: dict, font_path: Path, W: int, lead: float,
                 show0: float, show1: float):
        self.style = tl_line["style"]
        self.text = tl_line["text"]
        self.W = W
        draft = self.style == "draft"
        size = sub_cfg["draft"]["font_size"] if draft else sub_cfg["font_size"]
        self.color = hex_rgb(sub_cfg["draft"]["color"] if draft else sub_cfg["color"])
        self.stroke = int(sub_cfg["stroke_width"])
        self.stroke_color = hex_rgb(sub_cfg["stroke_color"])
        self.font = ImageFont.truetype(str(font_path), size)
        self.line_h = int(round(size * sub_cfg["line_spacing"]))
        self.center_y = int(sub_cfg["center_y"])
        self.fade = float(sub_cfg["fade"])
        self.blink = float(sub_cfg["draft"]["blink_period"])
        # 공백 제외 글자별 시간. 문장부호(쉼표·마침표·따옴표·...)는 음성상 '쉼' 구간이라
        # 그대로 쓰면 타이핑/페이지 전환이 늦어지므로 바로 앞 글자에 붙여서 같이 나오게 한다.
        chars = norm(self.text)
        times = [t + lead for t in tl_line["char_times"]]
        for k, ch in enumerate(chars):
            if ch in PUNCT and k > 0:
                times[k] = min(times[k], times[k - 1] + 0.03)
        lead_p = 0
        while lead_p < len(chars) and chars[lead_p] in PUNCT:
            lead_p += 1
        if 0 < lead_p < len(chars):  # 여는 따옴표 등은 첫 글자와 같이
            for k in range(lead_p):
                times[k] = times[lead_p]
        self.times = times
        self.show0, self.show1 = show0, show1
        max_w = W - 2 * int(sub_cfg["side_margin"])
        mc = sub_cfg["draft"].get("max_chars", sub_cfg["max_chars"]) if draft else sub_cfg["max_chars"]
        self._mc, self._maxw, self._mr = int(mc), max_w, int(sub_cfg["max_rows"])
        self.pages = []
        idx = 0
        for prow in self._paginate(self.text.split()):
            n = sum(len(norm(r)) for r in prow)
            self.pages.append({"rows": prow, "c0": idx, "c1": idx + n})
            idx += n
        for i, pg in enumerate(self.pages):
            pg["t0"] = show0 if i == 0 else self.times[pg["c0"]] - 0.08
        for i, pg in enumerate(self.pages):
            pg["t1"] = self.pages[i + 1]["t0"] if i + 1 < len(self.pages) else show1
        self._cache: dict = {}

    def _width(self, s: str) -> float:
        l, _, r, _ = self.font.getbbox(s, stroke_width=self.stroke)
        return r - l

    # ── 줄바꿈/페이지 나누기 ──
    # 한 페이지 = 최대 max_rows 행. 행은 약 max_chars 자(최대 +2자 허용)이면서 화면 폭 안.
    # 2행으로 들어가면 두 행 길이를 최대한 비슷하게, 문장부호 뒤에서 끊는 것을 선호.
    # 길면 문장/쉼표 경계에서 페이지를 나누고, 마지막 페이지가 너무 짧으면 앞 페이지와 균형을 맞춘다.
    def _row_ok(self, s: str) -> bool:
        return len(s) <= self._mc + 2 and self._width(s) <= self._maxw

    def _fit(self, words: list[str]):
        """words 를 max_rows 행 이하로 배치 → (행 목록, 점수) 또는 None. 점수 낮을수록 좋음."""
        one = " ".join(words)
        if len(one) <= self._mc and self._width(one) <= self._maxw:
            return [one], 0.0
        if self._mr < 2 or len(words) < 2:
            return ([one], 50.0) if self._row_ok(one) else None
        best = None
        for k in range(1, len(words)):
            r1, r2 = " ".join(words[:k]), " ".join(words[k:])
            if not (self._row_ok(r1) and self._row_ok(r2)):
                continue
            score = abs(len(r1) - len(r2)) + 3 * max(0, max(len(r1), len(r2)) - self._mc)
            if r1[-1] in PUNCT:
                score -= 2.5
            if best is None or score < best[1]:
                best = ([r1, r2], score)
        return best

    def _paginate(self, words: list[str]) -> list[list[str]]:
        pages, i = [], 0
        while i < len(words):
            last = None
            for j in range(i + 1, len(words) + 1):
                if self._fit(words[i:j]) is None:
                    break
                last = j
            if last is None:  # 한 어절이 너무 김 → 그대로 한 행
                last = i + 1
            if last < len(words):  # 문장부호 경계에서 끊기 선호
                for k in range(last, i, -1):
                    if words[k - 1][-1] in PUNCT and (k - i) * 2 >= (last - i):
                        last = k
                        break
            fit = self._fit(words[i:last])
            pages.append((words[i:last], fit[0] if fit else [" ".join(words[i:last])]))
            i = last
        # 마지막 페이지가 너무 짧으면(공백 제외 6자 이하) 앞 페이지와 다시 나누기
        if len(pages) >= 2 and len(norm(" ".join(pages[-1][0]))) <= 6:
            ws = pages[-2][0] + pages[-1][0]
            best = None
            for k in range(1, len(ws)):
                fa, fb = self._fit(ws[:k]), self._fit(ws[k:])
                if not fa or not fb:
                    continue
                la, lb = len(" ".join(ws[:k])), len(" ".join(ws[k:]))
                score = abs(la - lb) + fa[1] + fb[1] - (4 if ws[k - 1][-1] in PUNCT else 0)
                if best is None or score < best[0]:
                    best = (score, (ws[:k], fa[0]), (ws[k:], fb[0]))
            if best:
                pages[-2:] = [best[1], best[2]]
        return [rows for _, rows in pages]

    def _visible(self, t: float) -> int:
        """draft: 지금까지 보여줄 (공백 제외) 글자 수. 끝의 ... 는 깜빡임."""
        n = sum(1 for x in self.times if x <= t + 0.02)
        total = len(self.times)
        if n >= total and self.text.rstrip().endswith("...") and self.blink > 0:
            since = t - (self.times[-1] + 0.3)
            if since > 0 and int(since / self.blink) % 2 == 1:
                n = total - 3
        return n

    def _ink_box(self, s: str):
        """실제로 그려 본 잉크 경계 (l, t, r, b) — 폰트 메트릭이 아닌 픽셀 기준."""
        key = ("ink", s)
        if key not in self._cache:
            pad = self.font.size * 2
            l, t, r, b = self.font.getbbox(s, stroke_width=self.stroke)
            img = Image.new("L", (int(r - l + 2 * pad), int(b - t + 2 * pad)), 0)
            ImageDraw.Draw(img).text((pad - l, pad - t), s, font=self.font, fill=255,
                                     stroke_width=self.stroke, stroke_fill=255)
            a = np.array(img)
            cols = np.where(a.max(axis=0) > 0)[0]
            rows = np.where(a.max(axis=1) > 0)[0]
            self._cache[key] = (cols[0] - (pad - l), rows[0] - (pad - t),
                                cols[-1] + 1 - (pad - l), rows[-1] + 1 - (pad - t))
        return self._cache[key]

    def layout(self, page: dict):
        """페이지 행별 (텍스트, x, y) — 실제 잉크 기준으로 정확히 가로 중앙.
        세로는 행 높이 기준으로 고정(행마다 글자 모양에 따라 들쭉날쭉하지 않게)."""
        rows = page["rows"]
        block_h = self.line_h * len(rows)
        top = self.center_y - block_h / 2
        _, ref_t, _, ref_b = self.font.getbbox("가나다라", stroke_width=self.stroke)
        out = []
        for i, r in enumerate(rows):
            l, _, rr, _ = self._ink_box(r)
            ink_w = rr - l
            x = (self.W - ink_w) / 2 - l
            row_mid = top + self.line_h * (i + 0.5)
            y = row_mid - (ref_t + ref_b) / 2
            out.append((r, round(x), round(y)))
        return out

    def overlay(self, page_i: int, visible: int | None):
        """(RGBA ndarray, y0) — 가로 전체 폭 캔버스."""
        key = (page_i, visible)
        if key in self._cache:
            return self._cache[key]
        page = self.pages[page_i]
        lay = self.layout(page)
        y_min = min(y for _, _, y in lay) - self.line_h
        y_max = max(y for _, _, y in lay) + 2 * self.line_h
        y0 = max(0, int(y_min))
        canvas = Image.new("RGBA", (self.W, int(y_max - y0)), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        remain = None if visible is None else max(0, visible - page["c0"])
        for r, x, y in lay:
            s = r
            if remain is not None:
                out, cnt = "", 0
                for ch in r:
                    if not ch.isspace():
                        if cnt >= remain:
                            break
                        cnt += 1
                    out += ch
                remain -= cnt
                s = out
            if s:
                d.text((x, y - y0), s, font=self.font, fill=self.color + (255,),
                       stroke_width=self.stroke, stroke_fill=self.stroke_color + (255,))
        arr = np.array(canvas)
        self._cache[key] = (arr, y0)
        return arr, y0

    def state(self, t: float):
        """t 시점의 (page_i, visible, alpha) 또는 None."""
        if t < self.show0 or t >= self.show1:
            return None
        a = clamp01((t - self.show0) / self.fade) * clamp01((self.show1 - t) / self.fade)
        for i, pg in enumerate(self.pages):
            if pg["t0"] <= t < pg["t1"]:
                if len(self.pages) > 1:  # 페이지 사이 짧은 페이드
                    a *= clamp01((t - pg["t0"]) / 0.1) if i > 0 else 1.0
                    a *= clamp01((pg["t1"] - t) / 0.1) if i + 1 < len(self.pages) else 1.0
                vis = self._visible(t) if self.style == "draft" else None
                return i, vis, a
        return None


# ───────────────────────────── 렌더러 ─────────────────────────────
class Renderer:
    def __init__(self, ep_dir: Path, cfg: dict, placeholders: bool = False):
        self.ep_dir, self.cfg = ep_dir, cfg
        V, S = cfg["video"], cfg["subtitle"]
        self.W, self.H, self.fps = int(V["width"]), int(V["height"]), int(V["fps"])
        tl_path = ep_dir / "audio" / "timeline.json"
        if not tl_path.exists():
            sys.exit("[오류] timeline.json 없음 → tools/align.py 먼저 실행")
        self.tl = json.loads(tl_path.read_text(encoding="utf-8"))
        lead = float(V["lead_in"])
        self.lead = lead
        lines = self.tl["lines"]
        with wave.open(str(ep_dir / "audio" / "narration.wav"), "rb") as w:
            self.audio_dur = w.getnframes() / w.getframerate()
        last_end = lines[-1]["end"] + lead
        self.speech_end = last_end
        self.total = max(last_end + float(V["tail_hold"]) + float(V["end_fade"]),
                         self.audio_dur + lead + 0.2)

        # 장면 구간
        segs = []
        for l in lines:
            if segs and segs[-1]["scene"] == l["scene"]:
                continue
            t0 = 0.0 if not segs else l["start"] + lead - float(V["scene_lead"])
            segs.append({"scene": l["scene"], "t0": t0})
        for i, s in enumerate(segs):
            s["t1"] = segs[i + 1]["t0"] if i + 1 < len(segs) else self.total
        self.segs = segs

        # 이미지
        self.images, self.missing, self.used_placeholder = {}, [], []
        for s in segs:
            if s["scene"] in self.images:
                continue
            p = find_image(ep_dir, s["scene"], placeholders)
            if p is None:
                self.missing.append(s["scene"])
                continue
            if p.parent.name == "_placeholders":
                self.used_placeholder.append(s["scene"])
            self.images[s["scene"]] = self._load(p)
        if self.missing:
            sys.exit(f"[오류] 이미지 없음: {', '.join(self.missing)} → images/ 에 넣거나 --placeholders")

        # 자막
        font = ensure_font(S.get("font", ""))
        self.font_path = font
        self.subs = []
        for i, l in enumerate(lines):
            show0 = l["start"] + lead - float(S["pre"])
            post = float(S["draft"]["post"]) if l["style"] == "draft" else float(S["post"])
            show1 = l["end"] + lead + post
            if i + 1 < len(lines):
                show1 = min(show1, lines[i + 1]["start"] + lead - float(S["pre"]) - 0.02)
            else:
                show1 = self.total - float(V["end_fade"]) * 0.3
            show1 = max(show1, l["end"] + lead)
            self.subs.append(Subtitle(l, S, font, self.W, lead, show0, show1))

    def _load(self, path: Path) -> np.ndarray:
        im = Image.open(path)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im)
            im = bg
        return np.array(im.convert("RGB"))

    def _warp(self, img: np.ndarray, zoom: float) -> np.ndarray:
        ih, iw = img.shape[:2]
        s = max(self.W / iw, self.H / ih) * zoom
        cx_in, cy_in = (iw - 1) / 2, (ih - 1) / 2
        cx_out, cy_out = (self.W - 1) / 2, (self.H - 1) / 2
        M = np.array([[s, 0, cx_out - s * cx_in], [0, s, cy_out - s * cy_in]], dtype=np.float64)
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
        return cv2.warpAffine(img, M, (self.W, self.H), flags=interp,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

    def _seg_img(self, i: int, t: float) -> np.ndarray:
        V = self.cfg["video"]
        s = self.segs[i]
        p = clamp01((t - s["t0"]) / max(s["t1"] - s["t0"], 1e-6))
        z = float(V["zoom_from"]) + (float(V["zoom_to"]) - float(V["zoom_from"])) * p
        return self._warp(self.images[s["scene"]], z)

    def base_frame(self, t: float) -> np.ndarray:
        V = self.cfg["video"]
        d = float(V["transition_dur"])
        i = next((k for k, s in enumerate(self.segs) if s["t0"] <= t < s["t1"]), len(self.segs) - 1)
        white = 0.0
        if V["transition"] == "crossfade":
            frame = self._seg_img(i, t)
            if i + 1 < len(self.segs) and t >= self.segs[i]["t1"] - d:
                w = clamp01((t - (self.segs[i]["t1"] - d)) / (2 * d))
                frame = cv2.addWeighted(frame, 1 - w, self._seg_img(i + 1, t), w, 0)
            elif i > 0 and t < self.segs[i]["t0"] + d:
                w = clamp01((t - (self.segs[i]["t0"] - d)) / (2 * d))
                frame = cv2.addWeighted(self._seg_img(i - 1, t), 1 - w, frame, w, 0)
        else:  # white_dip
            frame = self._seg_img(i, t)
            s = self.segs[i]
            if i + 1 < len(self.segs) and t > s["t1"] - d:
                white = max(white, clamp01((t - (s["t1"] - d)) / d))
            if i > 0 and t < s["t0"] + d:
                white = max(white, 1 - clamp01((t - s["t0"]) / d))
        sf = float(V["start_fade"])
        if sf > 0 and t < sf:
            white = max(white, 1 - t / sf)
        if white > 0:
            frame = cv2.addWeighted(frame, 1 - white, np.full_like(frame, 255), white, 0)
        return frame

    def sub_states(self, t: float):
        out = []
        for sub in self.subs:
            st = sub.state(t)
            if st:
                out.append((sub, *st))
        return out

    def frame(self, t: float, with_subs: bool = True) -> np.ndarray:
        frame = self.base_frame(t)
        if with_subs:
            for sub, page_i, vis, a in self.sub_states(t):
                if a <= 0:
                    continue
                rgba, y0 = sub.overlay(page_i, vis)
                h = rgba.shape[0]
                y1 = min(self.H, y0 + h)
                region = frame[y0:y1].astype(np.float32)
                alpha = rgba[: y1 - y0, :, 3:4].astype(np.float32) / 255.0 * a
                region = region * (1 - alpha) + rgba[: y1 - y0, :, :3].astype(np.float32) * alpha
                frame[y0:y1] = np.clip(region + 0.5, 0, 255).astype(np.uint8)
        V = self.cfg["video"]
        ef = float(V["end_fade"])
        if ef > 0 and t > self.total - ef:
            w = clamp01((t - (self.total - ef)) / ef)
            frame = cv2.addWeighted(frame, 1 - w, np.full_like(frame, 255), w, 0)
        return frame

    def n_frames(self) -> int:
        return int(round(self.total * self.fps))

    def bgm_path(self) -> Path | None:
        A = self.cfg["audio"]
        folder = self.ep_dir.parent.parent / "bgm"
        if A.get("bgm"):
            p = folder / A["bgm"]
            if not p.exists():
                sys.exit(f"[오류] BGM 없음: {p}")
            return p
        if folder.exists():
            files = sorted(f for f in folder.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg", ".flac"))
            if files:
                return files[0]
        return None

    def render(self, out: Path) -> dict:
        V, A = self.cfg["video"], self.cfg["audio"]
        out.parent.mkdir(parents=True, exist_ok=True)
        ff = ffmpeg_exe()
        bgm = self.bgm_path()
        lead_ms = int(round(self.lead * 1000))
        fo = float(A["fade_out"])
        afade = f"afade=t=out:st={max(0.0, self.total - fo):.3f}:d={fo:.3f}"
        cmd = [ff, "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.W}x{self.H}", "-r", str(self.fps), "-i", "-",
               "-i", str(self.ep_dir / "audio" / "narration.wav")]
        nar = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,"
               f"adelay={lead_ms}:all=1,apad[nar]")
        if bgm:
            cmd += ["-stream_loop", "-1", "-i", str(bgm)]
            bg = f"[2:a]aresample=48000,aformat=channel_layouts=stereo,volume={float(A['bgm_volume_db'])}dB[bg]"
            if A.get("duck", True):
                fc = (f"{nar};{bg};[nar]asplit=2[n1][sc];"
                      f"[bg][sc]sidechaincompress=threshold=0.03:ratio=4:attack=30:release=500[bgd];"
                      f"[n1][bgd]amix=inputs=2:duration=first:normalize=0,{afade}[aout]")
            else:
                fc = f"{nar};{bg};[nar][bg]amix=inputs=2:duration=first:normalize=0,{afade}[aout]"
        else:
            fc = f"{nar};[nar]{afade}[aout]"
        cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]", "-t", f"{self.total:.3f}",
                "-c:v", "libx264", "-preset", str(V["preset"]), "-crf", str(V["crf"]), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        n = self.n_frames()
        try:
            for k in range(n):
                proc.stdin.write(self.frame(k / self.fps).tobytes())
                if k % (self.fps * 5) == 0:
                    print(f"\r[렌더] {k / self.fps:5.1f}/{self.total:.1f}s", end="", flush=True)
        finally:
            proc.stdin.close()
            rc = proc.wait()
        print()
        if rc != 0:
            sys.exit(f"[오류] ffmpeg 실패 (코드 {rc})")
        info = {
            "output": str(out),
            "total": round(self.total, 3),
            "fps": self.fps,
            "lead_in": self.lead,
            "bgm": str(bgm) if bgm else None,
            "font": str(self.font_path),
            "placeholders_used": self.used_placeholder,
            "segments": self.segs,
        }
        (out.parent / "render_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        return info


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--placeholders", action="store_true", help="없는 이미지는 images/_placeholders 사용")
    ap.add_argument("--out")
    args = ap.parse_args()
    ep_dir, cfg = load_episode(args.episode)
    r = Renderer(ep_dir, cfg, args.placeholders)
    out = Path(args.out) if args.out else ep_dir / "output" / f"{cfg['id']}.mp4"
    info = r.render(out)
    if info["placeholders_used"]:
        print(f"[경고] 플레이스홀더 이미지 사용: {', '.join(info['placeholders_used'])} — 업로드용 아님")
    print(f"[렌더] 완료 → {out} ({info['total']:.1f}s)")


if __name__ == "__main__":
    main()
