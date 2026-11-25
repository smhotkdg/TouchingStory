"""Typecast 글자 타임스탬프 → 대본 줄별 시작/끝 시간(timeline.json).

대본(공백 제거)과 TTS 글자열(공백 제거)을 순서대로 매칭해서
각 줄의 첫 글자 시작 ~ 마지막 글자 끝을 그 줄의 음성 구간으로 잡는다.
줄 안의 글자별 시간(char_times)도 저장 → 긴 줄 자막 페이지 전환·타이핑 효과에 사용.

사용: python tools/align.py ep01-mom-thumb
"""
from __future__ import annotations

import argparse
import json
import sys
from difflib import SequenceMatcher

from common import load_episode, norm, setup_stdout, strip_markup, tts_text

FAIL_RATIO = 0.80
WARN_RATIO = 0.95


def tts_char_list(ts: dict) -> list[tuple[str, float, float]]:
    items = ts.get("characters") or []
    if not items:  # characters 가 없으면 단어 타임스탬프를 글자로 균등 분할
        items = ts.get("words") or []
    out = []
    for it in items:
        t = norm(it["text"])
        if not t:
            continue
        s, e = float(it["start"]), float(it["end"])
        d = (e - s) / len(t)
        for k, ch in enumerate(t):
            out.append((ch, s + k * d, s + (k + 1) * d))
    return out


def build_timeline(ep_dir, cfg) -> dict:
    ts_path = ep_dir / "audio" / "tts_timestamps.json"
    if not ts_path.exists():
        sys.exit("[오류] tts_timestamps.json 없음 → 먼저 tools/typecast_tts.py 실행")
    ts = json.loads(ts_path.read_text(encoding="utf-8"))
    if ts.get("text") != tts_text(cfg):
        sys.exit("[오류] 음성을 만든 뒤 대본이 바뀜 → typecast_tts.py --force 로 다시 생성")

    tchars = tts_char_list(ts)
    t_str = "".join(c[0] for c in tchars)
    s_chars, owner = [], []
    for li, line in enumerate(cfg["lines"]):
        for ch in norm(strip_markup(line["text"])):
            s_chars.append(ch)
            owner.append(li)
    s_str = "".join(s_chars)

    sm = SequenceMatcher(None, s_str, t_str, autojunk=False)
    mapping = {}
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            mapping[a + k] = b + k

    lines_out, problems = [], []
    pos = 0
    for li, line in enumerate(cfg["lines"]):
        n = len(norm(strip_markup(line["text"])))
        idxs = list(range(pos, pos + n))
        pos += n
        matched = [i for i in idxs if i in mapping]
        ratio = len(matched) / max(n, 1)
        if not matched:
            sys.exit(f"[오류] {li + 1}번째 줄을 음성에서 찾지 못함: {strip_markup(line['text'])}")
        # 글자별 시간: 매칭된 글자는 TTS 시간, 아닌 글자는 앞뒤 사이 보간
        times = [tchars[mapping[i]][1] if i in mapping else None for i in idxs]
        ends = [tchars[mapping[i]][2] if i in mapping else None for i in idxs]
        known = [k for k, t in enumerate(times) if t is not None]
        for k in range(n):
            if times[k] is None:
                prev = max([j for j in known if j < k], default=None)
                nxt = min([j for j in known if j > k], default=None)
                if prev is not None and nxt is not None:
                    times[k] = times[prev] + (times[nxt] - times[prev]) * (k - prev) / (nxt - prev)
                else:
                    times[k] = times[prev] if prev is not None else times[nxt]
        start = min(tchars[mapping[i]][1] for i in matched)
        end = max(tchars[mapping[i]][2] for i in matched)
        # 끝 글자가 문장부호면 그 길이(쉼)는 빼고 실제 발화 끝으로
        last_word = [tchars[mapping[i]] for i in matched if tchars[mapping[i]][0] not in ".,!?\"'…"]
        speech_end = max((c[2] for c in last_word), default=end)
        lines_out.append({
            "index": li,
            "scene": line["scene"],
            "style": line.get("style", "normal"),
            "text": strip_markup(line["text"]),
            "display": line["text"],
            "start": round(start, 3),
            "end": round(speech_end, 3),
            "match_ratio": round(ratio, 3),
            "char_times": [round(t, 3) for t in times],
        })
        if ratio < WARN_RATIO:
            problems.append((li, ratio))

    for a, b in zip(lines_out, lines_out[1:]):
        if b["start"] < a["end"]:
            b["start"] = a["end"]  # 겹침 방지
    tl = {
        "source": ts.get("source"),
        "audio_duration": ts.get("audio_duration"),
        "lines": lines_out,
    }
    for li, ratio in problems:
        level = "오류" if ratio < FAIL_RATIO else "경고"
        print(f"[{level}] {li + 1}번째 줄 매칭률 {ratio:.0%}: {cfg['lines'][li]['text']}")
    if any(r < FAIL_RATIO for _, r in problems):
        sys.exit("[오류] 대본과 음성이 크게 다름 → 대본/음성 확인 후 다시 생성")
    return tl


def main():
    setup_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    args = ap.parse_args()
    ep_dir, cfg = load_episode(args.episode)
    tl = build_timeline(ep_dir, cfg)
    out = ep_dir / "audio" / "timeline.json"
    out.write_text(json.dumps(tl, ensure_ascii=False, indent=1), encoding="utf-8")
    for l in tl["lines"]:
        print(f"{l['index'] + 1:>2}. {l['start']:6.2f}~{l['end']:6.2f}s  [{l['scene']}] {l['text']}")
    print(f"[정렬] 완료 → {out}")


if __name__ == "__main__":
    main()
