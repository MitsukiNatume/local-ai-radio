#!/usr/bin/env python3
"""ローカルAIラジオ: 台本JSONから音声生成→タイミング付与→episodes.json更新→push→公開まで一括で行う。

usage:
  python3 produce.py <script.json> [--no-publish]

script.json は monoradio/news-1 形式（opening/news/closing、各行に say/show）。
音声は COEIROINK（リリンちゃん style 90）で合成する。エンジンが落ちていれば自動起動する。
公開先リポジトリのクローン: ~/workspace/projects/local-ai-radio-clone
"""
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:50032"
UUID = "cb11bdbd-78fc-4f16-b528-a400bae1782d"  # リリンちゃん
STYLE = 90  # のーまる
SR = 44100
LEAD, INTRA, BOUNDARY, TRAIL = 0.6, 0.25, 1.05, 2.05

HOME = os.path.expanduser("~")
CLONE = os.path.join(HOME, "workspace/projects/local-ai-radio-clone")
ENGINE_ROOT = os.path.join(HOME, "workspace/tools/coeiroink/pkg/COEIROINK_LINUX_CPU_v.2.13.0")
WORKROOT = os.path.join(HOME, "workspace/tools/local-ai-radio/work")
SSH_CMD = ("ssh -o ProxyCommand=python3\\ " + HOME + "/workspace/tools/ssh_proxy_connect.py"
           "\\ %h\\ %p -o IdentitiesOnly=yes -i " + HOME + "/.ssh/id_ed25519"
           " -o StrictHostKeyChecking=accept-new")
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def engine_ready():
    try:
        with urllib.request.urlopen(BASE + "/v1/speakers", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_engine():
    if engine_ready():
        return
    log = open(os.path.join(HOME, "workspace/tools/coeiroink/engine.log"), "ab")
    subprocess.Popen(["./engine/engine"], cwd=ENGINE_ROOT, stdout=log, stderr=log,
                     start_new_session=True)
    for _ in range(60):
        if engine_ready():
            return
        time.sleep(2)
    raise RuntimeError("COEIROINK engine did not start within 120s")


def synth(text, out):
    body = {
        "speakerUuid": UUID, "styleId": STYLE, "text": text,
        "speedScale": 1.0, "volumeScale": 1.0, "pitchScale": 0.0,
        "intonationScale": 1.0, "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.4, "outputSamplingRate": SR,
    }
    req = urllib.request.Request(BASE + "/v1/synthesis",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    with open(out, "wb") as f:
        f.write(data)


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    return float(out.stdout.strip())


def speech_span(path):
    p = subprocess.run(["ffmpeg", "-i", path, "-af",
                        "silencedetect=noise=-45dB:d=0.03", "-f", "null", "-"],
                       capture_output=True, text=True)
    log = p.stderr
    dur = probe_duration(path)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    speech_start = 0.0
    for s, e in zip(starts, ends):
        if s <= 0.001:
            speech_start = e
            break
    speech_end = dur
    for s, e in zip(starts, ends):
        if abs(e - dur) < 0.01:
            speech_end = s
    return speech_start, speech_end


def collect_lines(ep):
    lines = []
    for ln in ep["opening"]:
        lines.append({**ln, "kind": "opening", "index": None, "part": "opening"})
    for i, item in enumerate(ep["news"]):
        for ln in item["lines"]:
            lines.append({**ln, "kind": "news", "index": i, "part": f"news{i}"})
    for ln in ep["closing"]:
        lines.append({**ln, "kind": "closing", "index": None, "part": "closing"})
    return lines


def main():
    script_path = sys.argv[1]
    do_publish = "--no-publish" not in sys.argv
    ep = json.load(open(script_path, encoding="utf-8"))
    date = ep["date"]
    lines = collect_lines(ep)
    work = os.path.join(WORKROOT, date)
    os.makedirs(work, exist_ok=True)

    ensure_engine()

    results = []
    for i, ln in enumerate(lines):
        raw = os.path.join(work, f"line{i:02d}_raw.wav")
        tag = hashlib.md5(ln["say"].encode()).hexdigest()
        tagfile = raw + ".tag"
        cached = (os.path.exists(raw) and os.path.getsize(raw) > 10000
                  and os.path.exists(tagfile) and open(tagfile).read() == tag)
        if not cached:
            synth(ln["say"], raw)
            with open(tagfile, "w") as f:
                f.write(tag)
        ss, se = speech_span(raw)
        trimmed = os.path.join(work, f"line{i:02d}.wav")
        run(["ffmpeg", "-y", "-v", "error", "-i", raw,
             "-af", f"atrim=start={ss:.4f}:end={se:.4f},asetpts=N/SR/TB",
             "-ar", str(SR), "-ac", "1", trimmed])
        results.append({**ln, "trimmed": trimmed, "duration": probe_duration(trimmed)})
        print(f"line {i:2d} [{ln['part']}] {results[-1]['duration']:.3f}s", flush=True)

    def make_silence(name, secs):
        p = os.path.join(work, name)
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anullsrc=r={SR}:cl=mono", "-t", str(secs),
             "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", p])
        return p

    sil = {LEAD: make_silence("sil_lead.wav", LEAD),
           INTRA: make_silence("sil_intra.wav", INTRA),
           BOUNDARY: make_silence("sil_boundary.wav", BOUNDARY),
           TRAIL: make_silence("sil_trail.wav", TRAIL)}

    concat = [sil[LEAD]]
    t = LEAD
    lines_timed = []
    seg_first_start = {}
    order = []
    for i, r in enumerate(results):
        start = t
        end = start + r["duration"]
        lines_timed.append({"start": round(start, 3), "end": round(end, 3),
                            "show": r["show"], "part": r["part"]})
        key = (r["kind"], r["index"])
        if key not in seg_first_start:
            seg_first_start[key] = start
            order.append(key)
        concat.append(r["trimmed"])
        t = end
        if i < len(results) - 1:
            gap = INTRA if results[i + 1]["part"] == r["part"] else BOUNDARY
            concat.append(sil[gap])
            t += gap
    concat.append(sil[TRAIL])
    total = t + TRAIL

    segments = []
    for j, key in enumerate(order):
        start = seg_first_start[key]
        end = seg_first_start[order[j + 1]] if j < len(order) - 1 else total
        segments.append({"kind": key[0], "index": key[1],
                         "start": round(start, 3), "end": round(end, 3)})

    listfile = os.path.join(work, "concat.txt")
    with open(listfile, "w") as f:
        for p in concat:
            f.write(f"file '{p}'\n")
    full_wav = os.path.join(work, "episode_full.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", listfile, "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", full_wav])

    os.makedirs(os.path.join(CLONE, "audio"), exist_ok=True)
    mp3 = os.path.join(CLONE, "audio", f"{date}-ja.mp3")
    run(["ffmpeg", "-y", "-v", "error", "-i", full_wav,
         "-codec:a", "libmp3lame", "-b:a", "96k", "-ar", str(SR), "-ac", "1", mp3])
    seconds = round(probe_duration(mp3), 2)
    print(f"MP3 {mp3} {seconds}s {os.path.getsize(mp3)} bytes")

    ep["seconds"] = seconds
    ep["lines_timed"] = lines_timed
    ep["segments"] = segments
    ep["made_at"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ep_out = os.path.join(CLONE, "episodes", f"{date}-ja.json")
    with open(ep_out, "w", encoding="utf-8") as f:
        json.dump(ep, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        f.write("\n")

    idx_path = os.path.join(CLONE, "episodes.json")
    idx = json.load(open(idx_path, encoding="utf-8"))
    idx = [e for e in idx if e.get("date") != date]
    idx.insert(0, {"date": date, "ja": {"title": ep["title"], "seconds": seconds}})
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
        f.write("\n")

    if do_publish:
        run(["git", "-C", CLONE, "add", "episodes.json", f"episodes/{date}-ja.json"])
        run(["git", "-C", CLONE, "commit", "-q", "-m",
             f"{date}の放送を追加（自動生成）"])
        run(["git", "-C", CLONE, "push", "origin", "main"])
        env = dict(os.environ)
        env["GIT_SSH_COMMAND"] = SSH_CMD
        env["SHARE_PYTHON"] = "python3"
        env["SHARE_FONT"] = FONT
        run(["bash", "tools/publish.sh"], cwd=CLONE, env=env)
    print(json.dumps({"date": date, "title": ep["title"], "seconds": seconds,
                      "published": do_publish}, ensure_ascii=False))


if __name__ == "__main__":
    main()
