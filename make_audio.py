#!/usr/bin/env python3
"""Spoken-summary clips, one MP3 per store key.

The dashboard build (dashboard.py) scans ./audio to decide which items get an
inline player (AUDIO_KEYS). This script keeps that folder in step with
store.json: it renders a clip for every item that has a plain-English summary,
re-renders one whose summary text has changed, and prunes a clip whose item has
left the store.

Voice is Kokoro af_heart at 24 kHz, re-encoded to match the existing clips
(mono 44.1 kHz 64 kbps MP3 via ffmpeg). audio_manifest.json records the hash of
the text each clip was rendered from, so a summary edit is picked up without
re-rendering the whole list.

Env:
  AUDIO_MAX    cap on clips rendered per run (default 60). Excess items are
               left for the next run - a mass summary change or the first run
               after a gap doesn't blow the CI budget. Newest items first.
  AUDIO_PRUNE  set to "0" to keep clips whose item has left the store
               (default: prune, so the folder stays 1:1 with the store).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

STORE_PATH = "store.json"
AUDIO_DIR = "audio"
MANIFEST_PATH = "audio_manifest.json"
VOICE = "af_heart"
LANG = "a"  # Kokoro: American English
SAMPLE_RATE = 24000  # Kokoro native; ffmpeg resamples to 44100 to match old clips


def text_hash(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def spoken_text(item):
    # Feed the summary as written - the existing 773 clips were rendered from
    # the raw plain_english. Only collapse whitespace so a stray newline in the
    # source doesn't become an odd pause.
    return re.sub(r"\s+", " ", (item.get("plain_english") or "").strip())


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_manifest(manifest):
    # Only touch the file when it would actually change - a quiet day must not
    # produce a commit on its own (the workflow's "nothing to publish" check
    # keys off the staged diff).
    new = json.dumps(manifest, indent=1, sort_keys=True)
    if load_json(MANIFEST_PATH, None) != json.loads(new):
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            f.write(new)


def encode_mp3(samples, sample_rate, out_path):
    """samples: 1-D float array in [-1, 1]. Write via a temp WAV + ffmpeg so the
    output matches the existing clips exactly (mono, 44.1 kHz, 64 kbps)."""
    import soundfile as sf

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        sf.write(wav_path, samples, sample_rate)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
             "-ar", "44100", "-ac", "1", "-b:a", "64k", out_path],
            check=True,
        )
    finally:
        os.unlink(wav_path)


def render(pipeline, text, out_path):
    import numpy as np

    chunks = [audio for _, _, audio in pipeline(text, voice=VOICE)]
    if not chunks:
        raise RuntimeError("Kokoro produced no audio")
    samples = np.concatenate([np.asarray(c, dtype="float32") for c in chunks])
    encode_mp3(samples, SAMPLE_RATE, out_path)


def main():
    audio_max = int(os.environ.get("AUDIO_MAX", "60"))
    prune = os.environ.get("AUDIO_PRUNE", "1") != "0"

    os.makedirs(AUDIO_DIR, exist_ok=True)
    store = load_json(STORE_PATH, {})
    manifest = load_json(MANIFEST_PATH, {})

    # Every item with a summary, newest first - so when the per-run cap bites,
    # the items a reader is most likely to open are the ones that got a clip.
    items = sorted(
        (v for v in store.values() if spoken_text(v)),
        key=lambda v: v.get("date") or "0000-00-00",
        reverse=True,
    )
    want = {v["key"]: spoken_text(v) for v in items}

    todo = []  # (key, text, reason)
    for v in items:
        key, text = v["key"], spoken_text(v)
        mp3 = os.path.join(AUDIO_DIR, key + ".mp3")
        if not os.path.exists(mp3):
            todo.append((key, text, "new"))
        elif key in manifest and manifest[key] != text_hash(text):
            todo.append((key, text, "changed"))
        else:
            # Clip already on disk and either unchanged or predates the
            # manifest (the committed backlog) - trust it, just record the hash.
            manifest[key] = text_hash(text)

    pruned = 0
    if prune:
        for name in os.listdir(AUDIO_DIR):
            if name.endswith(".mp3") and name[:-4] not in want:
                os.unlink(os.path.join(AUDIO_DIR, name))
                manifest.pop(name[:-4], None)
                pruned += 1

    if not todo:
        print(f"Audio: up to date ({len(want)} clips"
              + (f", {pruned} pruned" if pruned else "") + ")")
        save_manifest(manifest)
        return

    capped = todo[:audio_max]
    print(f"Audio: {len(todo)} to render "
          f"({sum(r == 'new' for _, _, r in todo)} new, "
          f"{sum(r == 'changed' for _, _, r in todo)} changed)"
          + (f"; capped at {audio_max} this run" if len(todo) > audio_max else "")
          + (f"; {pruned} pruned" if pruned else ""))

    from kokoro import KPipeline

    pipeline = KPipeline(lang_code=LANG)
    done = 0
    for key, text, reason in capped:
        out_path = os.path.join(AUDIO_DIR, key + ".mp3")
        try:
            render(pipeline, text, out_path)
            manifest[key] = text_hash(text)
            done += 1
            print(f"  {reason:7} {key}")
        except Exception as e:  # one bad clip must not lose the rest
            print(f"  FAILED  {key}: {type(e).__name__}: {e}", file=sys.stderr)
            if os.path.exists(out_path) and reason == "new":
                os.unlink(out_path)

    save_manifest(manifest)

    remaining = len(todo) - done
    print(f"Audio: rendered {done}"
          + (f", {remaining} left for the next run" if remaining > 0 else ""))


if __name__ == "__main__":
    main()
