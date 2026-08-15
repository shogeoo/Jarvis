#!/usr/bin/env python3
"""Ручная запись с микрофона: Enter — старт, Enter — стоп.

После каждой записи печатает вероятности VAD (silero) по чанкам и
транскрипцию (whisper). Используется для проверки VAD и как fallback,
если автоматическое распознавание речи не срабатывает.

Управление:
  Enter   — начать запись (написав "ЗАПИСЬ...") / остановить запись
  q+Enter — выход
"""

from __future__ import annotations

import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stt_stream.audio import FRAME_SIZE, SAMPLE_RATE, MicStream
from stt_stream.transcribe import Transcriber
from stt_stream.vad import SileroVAD, ensure_model


def collect_mic() -> np.ndarray:
    mic = MicStream(SAMPLE_RATE, FRAME_SIZE).start()
    frames = []
    print(">>> ЗАПИСЬ: говори. Enter — стоп.", flush=True)
    try:
        while True:
            frame = mic.frames.get(timeout=0.2)
            frames.append(frame)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        mic.stop()
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)


def vad_report(data: np.ndarray):
    vad = SileroVAD()
    probs = []
    for i in range(0, len(data) - FRAME_SIZE + 1, FRAME_SIZE):
        out = vad.session.run(None, {
            "input": data[i:i + FRAME_SIZE].astype(np.float32)[None] / 32768.0,
            "state": vad.state,
            "sr": vad.sr,
        })
        vad.state = out[1]
        probs.append(float(out[0][0][0]))
    probs = np.asarray(probs)
    print(f"VAD: max={probs.max():.3f}  prob>0.5: {int((probs > 0.5).sum())}/{len(probs)} кадров",
          flush=True)
    # полоса: 1 символ = 0.5 с
    per = int(0.5 / (FRAME_SIZE / SAMPLE_RATE))
    line = ""
    for start in range(0, len(probs), per):
        chunk = probs[start:start + per]
        line += "#" if chunk.max() >= 0.5 else "."
    print(f"  полоса(0.5с): {line}", flush=True)


def main() -> int:
    ensure_model()
    tr = Transcriber(device="cuda", language=None)
    try:
        tr.load()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    out_dir = "segments/manual"
    os.makedirs(out_dir, exist_ok=True)
    print("Enter — начать запись; Enter — остановить и расшифровать; q — выход", flush=True)
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "q":
            break
        data = collect_mic()
        if len(data) < SAMPLE_RATE // 2:
            print("Запись слишком короткая, пропускаю.", flush=True)
            continue
        path = os.path.join(out_dir, f"take_{time.strftime('%H%M%S')}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(data.tobytes())
        print(f"Сохранено: {path} ({len(data) / SAMPLE_RATE:.1f}s, "
              f"RMS={np.sqrt((data.astype(np.float64) ** 2).mean()):.0f})", flush=True)
        vad_report(data)
        print(f"Whisper: {tr.transcribe_file(path)!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
