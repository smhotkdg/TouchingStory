"""timeline.json + 이미지 + 내레이션 → 쇼츠 mp4 렌더.

규칙(CLAUDE.md 참고)
- 1080x1920. 이미지는 높이에 맞춰 커버 스케일 → 좌우 대칭 크롭 → 정확히 가로 중앙.
- 장면마다 느린 줌인, 줌 중심은 프레임 정중앙 고정(좌우 이동 없음).
- 전환: white_dip(흰색으로 페이드아웃 → 페이드인) 또는 crossfade.
- 자막: 중심보다 약간 위(center_y), 배경 패널 없음. 손글씨 폰트 + 얇은 흰 외곽선 + 옅은 흰 번짐, 최대 2줄.
  음성에 맞춰 어절마다 부드럽게 떠오르며 등장, {강조}는 따뜻한 색. 긴 줄은 2줄씩 페이지를 넘긴다.
  draft 스타일(엄마 문자)은 둥근 폰트로 음성에 맞춰 한 글자씩 타이핑 + 커서.
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
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from common import ensure_font, ffmpeg_exe, find_image, hex_rgb, load_episode, norm, setup_stdout

PUNCT = ".,!?\"'…"


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# ───────────────────────────── 자막 ─────────────────────────────
def ease_out(x: float) -> float:
    x = clamp01(x)
    return 1 - (1 - x) ** 3


def parse_markup(s: str):
    """'{강조}' 표기를 (글자, 강조여부) 목록으로."""
    out, emph = [], False
    for ch in s:
        if ch == "{":
            emph = True
        elif ch == "}":
            emph = False
        else:
            out.append((ch, emph))
    return out


class Subtitle:
    """한 대본 줄의 자막.

    - normal: 손글씨 폰트, 음성에 맞춰 어절(단어)마다 부드럽게 떠오르며 나타남. {강조}는 따뜻한 색.
    - draft : 엄마의 임시저장 문자. 둥근 폰트로 음성에 맞춰 한 글자씩 타이핑 + 깜빡이는 커서.
    - 배경 패널 없음. 글자 주변에 아주 옅은 흰 번짐(글로우)과 얇은 흰 외곽선만.
    - 2행씩 페이지. 각 행은 실제 잉크 기준으로 정확히 가로 중앙.
    """

    def __init__(self, tl_line: dict, S: dict, fonts: dict, W: int, lead: float,
                 show0: float, show1: float, speech_end: float):
        self.style = tl_line["style"]
        draft = self.style == "draft"
        K = S["draft"] if draft else S
        self.W = W
        self.font = ImageFont.truetype(str(fonts["draft" if draft else "main"]), int(K["font_size"]))
        self.color = hex_rgb(K["color"])
        self.accent = hex_rgb(S["accent_color"])
        self.stroke = int(S["stroke_width"])
        self.stroke_color = hex_rgb(S["stroke_color"])
        self.glow_r = float(S["glow_radius"])
        self.glow_spread = int(S["glow_spread"])
        self.glow_op = float(S["glow_opacity"])
        self.line_h = int(round(self.font.size * float(K["line_spacing"])))
        self.center_y = int(S["center_y"])
        self.fade_out = float(S["fade"])
        self.word_fade = float(S["word_fade"])
        self.word_rise = float(S["word_rise"])
        self.reveal = "type" if draft else S.get("reveal", "word")
        self.cursor = bool(S["draft"].get("cursor", True))
        self.blink = float(S["draft"]["blink_period"])
        self.show0, self.show1, self.speech_end = show0, show1, speech_end

        marked = parse_markup(tl_line.get("display") or tl_line["text"])
        self.text = "".join(c for c, _ in marked)
        self.emph = [e for _, e in marked]
        chars = norm(self.text)
        # 공백 제외 글자별 시간. 문장부호는 음성상 '쉼'이라 앞 글자에 붙여서 같이 나오게.
        times = [t + lead for t in tl_line["char_times"]]
        for k, ch in enumerate(chars):
            if ch in PUNCT and k > 0:
                times[k] = min(times[k], times[k - 1] + 0.03)
        lp = 0
        while lp < len(chars) and chars[lp] in PUNCT:
            lp += 1
        if 0 < lp < len(chars):
            for k in range(lp):
                times[k] = times[lp]
        self.times = times

        # 어절 목록: 글자 위치(원문 index), 공백 제외 시작 index
        self.words, pos, nsp = [], 0, 0
        for w in self.text.split(" "):
            if w:
                self.words.append({"text": w, "pos": pos, "c0": nsp,
                                   "emph": self.emph[pos:pos + len(w)], "t": times[min(nsp, len(times) - 1)] - 0.1})
                nsp += len(norm(w))
            pos += len(w) + 1

        max_w = W - 2 * int(S["side_margin"])
        self._mc, self._maxw, self._mr = int(K["max_chars"]), max_w, int(S["max_rows"])
        self._cache: dict = {}
        self.pages, wi = [], 0
        for rows in self._paginate([w["text"] for w in self.words]):
            prow = []
            for r in rows:
                n = len(r.split(" "))
                prow.append(list(range(wi, wi + n)))
                wi += n
            c0 = self.words[prow[0][0]]["c0"]
            c1 = c0 + sum(len(norm(self.words[k]["text"])) for row in prow for k in row)
            self.pages.append({"rows": [" ".join(self.words[k]["text"] for k in row) for row in prow],
                               "row_words": prow, "c0": c0, "c1": c1})
        # 페이지 전환: 다음 페이지 첫 어절 직전. 단, 앞 페이지 마지막 어절이 다 떠오른 뒤에 넘긴다.
        for i, pg in enumerate(self.pages):
            if i == 0:
                pg["t0"] = show0
            else:
                prev_full = self._full_time(self.pages[i - 1])
                pg["t0"] = max(self.times[pg["c0"]] - 0.12, prev_full + 0.15)
        for i, pg in enumerate(self.pages):
            pg["t1"] = self.pages[i + 1]["t0"] if i + 1 < len(self.pages) else show1
        self.full_t = self._full_time(self.pages[-1])

    def _full_time(self, pg: dict) -> float:
        """페이지 글자가 전부 다 보이게 되는 시각."""
        if self.reveal == "type":
            return self.times[pg["c1"] - 1] + 0.02
        if self.reveal == "word":
            last = max(k for row in pg["row_words"] for k in row)
            return self.words[last]["t"] + self.word_fade
        return pg.get("t0", self.show0)

    def cursor_visible(self, t: float) -> bool:
        if self.reveal != "type" or not self.cursor:
            return False
        # 타이핑 중에만 커서가 깜빡이고, 다 쓰면 사라진다
        return t < self.times[-1] + 0.05 and int((t - self.show0) / self.blink) % 2 == 0

    # ── 줄바꿈/페이지 나누기 ──
    # 한 페이지 = 최대 max_rows 행. 행은 약 max_chars 자(최대 +2자 허용)이면서 화면 폭 안.
    # 2행으로 들어가면 두 행 길이를 최대한 비슷하게, 문장부호 뒤에서 끊는 것을 선호.
    # 길면 문장/쉼표 경계에서 페이지를 나누고, 마지막 페이지가 너무 짧으면 앞 페이지와 균형을 맞춘다.
    def _width(self, s: str) -> float:
        l, _, r, _ = self.font.getbbox(s, stroke_width=self.stroke)
        return r - l

    def _row_ok(self, s: str) -> bool:
        return len(s) <= self._mc + 2 and self._width(s) <= self._maxw

    def _fit(self, words: list[str]):
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
            if last is None:
                last = i + 1
            if last < len(words):
                for k in range(last, i, -1):
                    if words[k - 1][-1] in PUNCT and (k - i) * 2 >= (last - i):
                        last = k
                        break
            fit = self._fit(words[i:last])
            pages.append((words[i:last], fit[0] if fit else [" ".join(words[i:last])]))
            i = last
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

    # ── 배치 ──
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
        """페이지 행별 (텍스트, x, y) — 실제 잉크 기준으로 정확히 가로 중앙."""
        rows = page["rows"]
        top = self.center_y - self.line_h * len(rows) / 2
        _, ref_t, _, ref_b = self.font.getbbox("가나다라", stroke_width=self.stroke)
        out = []
        for i, r in enumerate(rows):
            l, _, rr, _ = self._ink_box(r)
            x = (self.W - (rr - l)) / 2 - l
            y = top + self.line_h * (i + 0.5) - (ref_t + ref_b) / 2
            out.append((r, round(x), round(y)))
        return out

    def _word_pos(self, page_i: int):
        """페이지의 어절별 그리기 위치 [(word_index, x, y)]."""
        key = ("wpos", page_i)
        if key not in self._cache:
            pg = self.pages[page_i]
            res = []
            for (row, x, y), widx in zip(self.layout(pg), pg["row_words"]):
                off = 0
                for k in widx:
                    w = self.words[k]["text"]
                    res.append((k, x + self.font.getlength(row[:off]), y))
                    off += len(w) + 1
            self._cache[key] = res
        return self._cache[key]

    # ── 그리기 ──
    def _sprite(self, runs: tuple):
        """runs: ((문자열, (r,g,b)), ...) 를 이어 그린 RGBA 조각 + 그리기 원점 위치."""
        key = ("spr", runs)
        if key in self._cache:
            return self._cache[key]
        s = "".join(t for t, _ in runs)
        l, t, r, b = self.font.getbbox(s, stroke_width=self.stroke + self.glow_spread)
        pad = int(self.glow_r * 3) + 4
        w, h = int(r - l) + 2 * pad, int(b - t) + 2 * pad
        ox, oy = pad - l, pad - t
        glow = Image.new("L", (w, h), 0)
        ImageDraw.Draw(glow).text((ox, oy), s, font=self.font, fill=255,
                                  stroke_width=self.stroke + self.glow_spread, stroke_fill=255)
        if self.glow_r > 0:
            glow = glow.filter(ImageFilter.GaussianBlur(self.glow_r))
        img = Image.new("RGBA", (w, h), self.stroke_color + (0,))
        img.putalpha(glow.point(lambda v: int(v * self.glow_op)))
        txt = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(txt)
        x = ox
        for seg, col in runs:
            if seg:
                d.text((x, oy), seg, font=self.font, fill=col + (255,),
                       stroke_width=self.stroke, stroke_fill=self.stroke_color + (255,))
            x += self.font.getlength(seg)
        img.alpha_composite(txt)
        out = (np.array(img), ox, oy)
        self._cache[key] = out
        return out

    def _word_runs(self, k: int) -> tuple:
        w = self.words[k]
        runs, cur, col = [], "", None
        for ch, e in zip(w["text"], w["emph"]):
            c = self.accent if e else self.color
            if col is not None and c != col:
                runs.append((cur, col))
                cur = ""
            cur += ch
            col = c
        if cur:
            runs.append((cur, col))
        return tuple(runs)

    def _typed_count(self, t: float) -> int:
        return sum(1 for x in self.times if x <= t + 0.02)

    def state(self, t: float):
        """t 시점의 (page_i, 완전히 보이는 글자 수(공백 제외), 줄 전체 alpha) 또는 None."""
        if t < self.show0 or t >= self.show1:
            return None
        # 사라지는 페이드는 음성이 끝나고, 글자가 다 떠오른 뒤에 시작 (남은 시간이 짧으면 빠르게)
        fo = min(self.fade_out, max(0.12, self.show1 - max(self.speech_end, self.full_t)))
        a = clamp01((t - self.show0) / 0.15) * clamp01((self.show1 - t) / fo)
        for i, pg in enumerate(self.pages):
            if pg["t0"] <= t < pg["t1"]:
                if i + 1 < len(self.pages):
                    a *= clamp01((pg["t1"] - t) / 0.1)
                if self.reveal == "type":
                    vis = self._typed_count(t)
                elif self.reveal == "word":
                    vis = sum(len(norm(w["text"])) for w in self.words if t >= w["t"] + self.word_fade)
                else:
                    vis = len(self.times)
                return i, vis, a
        return None

    def items(self, t: float | None, page_i: int | None = None):
        """그릴 조각 목록 [(rgba, x, y, alpha)]. t=None 이면 page_i 페이지를 다 보인 상태로."""
        if t is None:
            a, show_all = 1.0, True
        else:
            st = self.state(t)
            if not st:
                return []
            page_i, _, a = st
            show_all = False
        if a <= 0:
            return []
        out = []
        pg = self.pages[page_i]
        if self.reveal == "type":
            n = len(self.times) if show_all else self._typed_count(t)
            typing = not show_all and n < len(self.times)
            blink_on = (not show_all) and self.cursor_visible(t)
            remain = n - pg["c0"]
            lay = self.layout(pg)
            for ri, (row, x, y) in enumerate(lay):
                s, cnt = "", 0
                for ch in row:
                    if not ch.isspace():
                        if cnt >= remain:
                            break
                        cnt += 1
                    s += ch
                remain -= cnt
                last_row = remain <= 0 or ri == len(lay) - 1
                cur = "|" if (blink_on and last_row and (remain <= 0)) else ""
                if s or cur:
                    spr, ox, oy = self._sprite(((s, self.color), (cur, self.accent)))
                    out.append((spr, x - ox, y - oy, a))
                if remain <= 0:
                    break
            return out
        for k, x, y in self._word_pos(page_i):
            w = self.words[k]
            if show_all or self.reveal != "word":
                p = 1.0
            else:
                p = clamp01((t - w["t"]) / self.word_fade)
            if p <= 0:
                continue
            spr, ox, oy = self._sprite(self._word_runs(k))
            rise = (1 - ease_out(p)) * self.word_rise
            out.append((spr, x - ox, y - oy + rise, a * ease_out(p)))
        return out

    def overlay(self, page_i: int, visible=None):
        """검증용: 페이지 전체가 다 보이는 상태의 (RGBA 전체폭 캔버스, y0)."""
        items = self.items(None, page_i)
        y0 = int(min(y for _, _, y, _ in items))
        y1 = int(max(y + s.shape[0] for s, _, y, _ in items)) + 1
        canvas = np.zeros((y1 - y0, self.W, 4), np.float32)
        for spr, x, y, a in items:
            blend(canvas, spr, x, y - y0, a, rgba_target=True)
        return np.clip(canvas, 0, 255).astype(np.uint8), y0


def blend(dst: np.ndarray, spr: np.ndarray, x: float, y: float, a: float, rgba_target: bool = False):
    """spr(RGBA uint8)를 dst 의 (x, y)에 alpha 배율 a 로 합성 (화면 밖은 잘라냄)."""
    xi, yi = int(round(x)), int(round(y))
    h, w = spr.shape[:2]
    H, W = dst.shape[:2]
    x0, y0, x1, y1 = max(0, xi), max(0, yi), min(W, xi + w), min(H, yi + h)
    if x0 >= x1 or y0 >= y1:
        return
    s = spr[y0 - yi:y1 - yi, x0 - xi:x1 - xi].astype(np.float32)
    al = s[:, :, 3:4] / 255.0 * a
    region = dst[y0:y1, x0:x1].astype(np.float32)
    if rgba_target:  # 검증용 투명 캔버스 위 합성 (premultiplied 아님, 단순 over)
        out_a = al + region[:, :, 3:4] / 255.0 * (1 - al)
        rgb = (s[:, :, :3] * al + region[:, :, :3] * (region[:, :, 3:4] / 255.0) * (1 - al)) / np.maximum(out_a, 1e-6)
        dst[y0:y1, x0:x1, :3] = rgb
        dst[y0:y1, x0:x1, 3:4] = out_a * 255
    else:
        dst[y0:y1, x0:x1] = np.clip(region[:, :, :3] * (1 - al) + s[:, :, :3] * al + 0.5, 0, 255).astype(dst.dtype)


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
        self.fonts = {"main": ensure_font(S.get("font", "")),
                      "draft": ensure_font(S["draft"].get("font", ""))}
        self.font_path = self.fonts["main"]
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
            self.subs.append(Subtitle(l, S, self.fonts, self.W, lead, show0, show1, l["end"] + lead))

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
            for sub in self.subs:
                for spr, x, y, a in sub.items(t):
                    blend(frame, spr, x, y, a)
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
