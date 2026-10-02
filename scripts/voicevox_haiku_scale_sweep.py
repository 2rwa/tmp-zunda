#!/usr/bin/env python3
import argparse
import copy
import datetime as dt
import json
import math
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENGINE = "http://127.0.0.1:50021"

BPMS = [80, 100, 120, 140, 160]
GRIDS = [("loose", 1.5), ("eighth", 2.0), ("triplet", 3.0)]
SWINGS = [0.00, 0.08, 0.16, 0.24]

NOTE_MIDI = {
    "C4": 60, "D4": 62, "E4": 64, "F4": 65,
    "G4": 67, "A4": 69, "B4": 71, "C5": 72,
}
MELODIES = {
    "ascending": ["C4", "D4", "E4", "F4", "G4", "A4", "B4", "C5"],
    "descending": ["C5", "B4", "A4", "G4", "F4", "E4", "D4", "C4"],
    "mountain": ["C4", "D4", "E4", "F4", "G4", "F4", "E4", "D4"],
    "valley": ["G4", "F4", "E4", "D4", "C4", "D4", "E4", "F4"],
    "bounce": ["C4", "G4", "D4", "A4", "E4", "B4", "F4", "C5"],
    "pedal": ["C4", "C4", "D4", "C4", "E4", "C4", "F4", "C4"],
}
UNVOICED_VOWELS = {"pau", "cl", "I", "U"}

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--haiku-file", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--output-root", default="results")
    return p.parse_args()

def get_json(url, method="GET", data=None, timeout=120):
    headers = {}
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))

def get_speaker():
    speakers = get_json(ENGINE + "/speakers")
    z = next(s for s in speakers if s["name"] == "ずんだもん")
    style = next((x for x in z["styles"] if x["name"] == "ノーマル"), z["styles"][0])
    return int(style["id"]), style["name"]

def get_audio_query(text, speaker):
    qs = urllib.parse.urlencode({"speaker": speaker, "text": text})
    return get_json(ENGINE + "/audio_query?" + qs, method="POST")

def midi_to_log_f0(midi):
    hz = 440.0 * (2.0 ** ((midi - 69) / 12.0))
    return math.log(hz), hz

def retime_and_tune(base, bpm, grid_divisor, swing, melody_name):
    q = copy.deepcopy(base)
    beat = 60.0 / bpm
    mora_target = beat / grid_divisor
    sequence = MELODIES[melody_name]
    mora_index = 0
    voiced_notes = []

    for phrase in q["accent_phrases"]:
        for mora in phrase["moras"]:
            factor = (1.0 + swing) if (mora_index % 2) else (1.0 - swing)
            target = mora_target * factor
            c = mora.get("consonant_length")
            v = mora.get("vowel_length") or 0.0

            if c is None:
                mora["vowel_length"] = max(0.035, target)
            else:
                base_total = max(0.001, c + v)
                ratio = min(0.55, max(0.18, c / base_total))
                new_c = max(0.018, target * ratio)
                new_v = max(0.035, target - new_c)
                mora["consonant_length"] = new_c
                mora["vowel_length"] = new_v

            note_name = sequence[mora_index % len(sequence)]
            if mora.get("vowel") not in UNVOICED_VOWELS:
                log_f0, hz = midi_to_log_f0(NOTE_MIDI[note_name])
                mora["pitch"] = log_f0
                voiced_notes.append({
                    "mora": mora["text"],
                    "note": note_name,
                    "hz": round(hz, 3),
                })
            else:
                mora["pitch"] = 0.0
            mora_index += 1

        if phrase.get("pause_mora"):
            phrase["pause_mora"]["vowel_length"] = max(0.05, mora_target)

    q["speedScale"] = 1.0
    q["pitchScale"] = 0.0
    q["intonationScale"] = 1.0
    q["prePhonemeLength"] = min(0.12, mora_target * 0.5)
    q["postPhonemeLength"] = min(0.12, mora_target * 0.5)
    q["pauseLength"] = None
    q["pauseLengthScale"] = 1.0
    q["outputSamplingRate"] = 24000
    q["outputStereo"] = False
    return q, mora_index, mora_target, voiced_notes

def synthesize(query, speaker, wav_path):
    body = json.dumps(query, ensure_ascii=False).encode("utf-8")
    url = ENGINE + "/synthesis?" + urllib.parse.urlencode({"speaker": speaker})
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        wav_path.write_bytes(res.read())

def encode_mp3(wav_path, mp3_path, metadata):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(wav_path),
        "-codec:a", "libmp3lame", "-q:a", "5",
        "-ar", "24000", "-ac", "1",
    ]
    for key, value in metadata.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(mp3_path))
    subprocess.run(cmd, check=True)

def probe(mp3_path):
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,bit_rate,size",
        "-of", "json", str(mp3_path),
    ], text=True)
    fmt = json.loads(out)["format"]
    return {
        "duration_seconds": round(float(fmt["duration"]), 3),
        "bit_rate": int(fmt.get("bit_rate", 0) or 0),
        "size_bytes": int(fmt.get("size", mp3_path.stat().st_size)),
    }

def update_global_index(output_root):
    rows = []
    total_files = 0
    total_bytes = 0
    for path in sorted(output_root.glob("*/manifest.json")):
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows.append({
            "slug": m["slug"],
            "author": m["author"],
            "text": m["text"],
            "reading": m["reading"],
            "success": m["summary"]["success"],
            "failure": m["summary"]["failure"],
            "elapsed_seconds": m["summary"]["elapsed_seconds"],
            "total_mp3_bytes": m["summary"]["total_mp3_bytes"],
            "manifest": f"{m['slug']}/manifest.json",
        })
        total_files += m["summary"]["success"]
        total_bytes += m["summary"]["total_mp3_bytes"]

    index = {
        "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "haiku_completed": len(rows),
        "mp3_files": total_files,
        "total_mp3_bytes": total_bytes,
        "haiku": rows,
    }
    (output_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

def main():
    args = parse_args()
    all_haiku = json.loads(Path(args.haiku_file).read_text(encoding="utf-8"))
    item = next(x for x in all_haiku if x["slug"] == args.slug)

    speaker, style = get_speaker()
    base = get_audio_query(item["reading"], speaker)

    output_root = Path(args.output_root)
    out = output_root / item["slug"]
    mp3_root = out / "mp3"
    mp3_root.mkdir(parents=True, exist_ok=True)
    for old in mp3_root.glob("*.mp3"):
        old.unlink()

    expected = len(BPMS) * len(GRIDS) * len(SWINGS) * len(MELODIES)
    variants = []
    failures = []
    started = time.monotonic()
    started_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    n = 0

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "tmp.wav"
        for bpm in BPMS:
            for grid_name, grid_divisor in GRIDS:
                for swing in SWINGS:
                    for melody_name in MELODIES:
                        n += 1
                        key = (
                            f"b{bpm:03d}_{grid_name}_"
                            f"s{int(round(swing*100)):02d}_{melody_name}"
                        )
                        mp3_path = mp3_root / f"{key}.mp3"
                        t0 = time.monotonic()
                        try:
                            query, mora_count, target, notes = retime_and_tune(
                                base, bpm, grid_divisor, swing, melody_name
                            )
                            synthesize(query, speaker, wav)
                            encode_mp3(
                                wav, mp3_path,
                                {
                                    "title": item["text"],
                                    "artist": "VOICEVOX:ずんだもん",
                                    "comment": f"{key}; {item['author']}",
                                },
                            )
                            info = probe(mp3_path)
                            if info["duration_seconds"] <= 0.5 or info["size_bytes"] <= 1000:
                                raise RuntimeError(f"invalid MP3 metrics: {info}")
                            variants.append({
                                "id": key,
                                "file": f"mp3/{mp3_path.name}",
                                "bpm": bpm,
                                "grid": grid_name,
                                "moras_per_beat": grid_divisor,
                                "target_mora_seconds": round(target, 5),
                                "swing": swing,
                                "melody": melody_name,
                                "melody_notes": MELODIES[melody_name],
                                "mora_count": mora_count,
                                "voiced_note_preview": notes[:16],
                                **info,
                                "generation_seconds": round(time.monotonic() - t0, 3),
                            })
                        except Exception as exc:
                            failures.append({"id": key, "error": repr(exc)})
                            if mp3_path.exists():
                                mp3_path.unlink()

                        if n % 20 == 0 or n == expected:
                            print(
                                f"[{item['slug']}] {n}/{expected} "
                                f"success={len(variants)} failure={len(failures)} "
                                f"elapsed={time.monotonic()-started:.1f}s",
                                flush=True,
                            )

    elapsed = round(time.monotonic() - started, 3)
    total_bytes = sum((out / v["file"]).stat().st_size for v in variants)
    manifest = {
        "slug": item["slug"],
        "author": item["author"],
        "text": item["text"],
        "reading": item["reading"],
        "credit": "VOICEVOX:ずんだもん",
        "speaker": "ずんだもん",
        "style": style,
        "speaker_id": speaker,
        "started_at_utc": started_iso,
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "parameter_space": {
            "bpm": BPMS,
            "grid": [{"name": n, "moras_per_beat": d} for n, d in GRIDS],
            "swing": SWINGS,
            "melodies": MELODIES,
            "notes": NOTE_MIDI,
        },
        "summary": {
            "expected": expected,
            "success": len(variants),
            "failure": len(failures),
            "elapsed_seconds": elapsed,
            "total_mp3_bytes": total_bytes,
        },
        "variants": variants,
        "failures": failures,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "base-query.json").write_text(
        json.dumps(base, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "summary.md").write_text(
        "\n".join([
            f"# {item['text']}",
            "",
            f"- 作者: {item['author']}",
            f"- 読み: {item['reading']}",
            "- Credit: VOICEVOX:ずんだもん",
            f"- Expected: {expected}",
            f"- Success: {len(variants)}",
            f"- Failure: {len(failures)}",
            f"- Elapsed: {elapsed} sec",
            f"- MP3 bytes: {total_bytes}",
            "",
        ]),
        encoding="utf-8",
    )
    update_global_index(output_root)
    print(json.dumps(manifest["summary"], ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
