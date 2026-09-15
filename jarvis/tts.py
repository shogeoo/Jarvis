"""Озвучка ответов через Fish Audio S2 Pro (s2.cpp).

Синтез идёт в ``POST {TTS_URL}`` (multipart/form-data), голос берётся из
профиля ``.s2voice``. Сервер s2.cpp держит модель в VRAM: Jarvis поднимает его
при старте (``TTS_AUTOSTART``), если он ещё не запущен, и останавливает при
выходе. Если сервер был запущен извне, Jarvis им не управляет.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import ROOT, Config

RUNTIME_DIR = ROOT / ".jarvis"
SERVER_LOG = RUNTIME_DIR / "tts-server.log"
SERVER_START_TIMEOUT = 240.0
VOICE_BOOTSTRAP_TEXT = "Инициализация завершена."
STREAM_PARAMS = {
    "stream": True,
    "chunked": True,
    "output_format": "pcm_s16le",
    "segment_sentences": True,
    "sentence_pause_ms": 180,
    "max_new_tokens": 1024,
}


class Speaker:
    """Очередь реплик -> синтез на s2.cpp -> воспроизведение через ffplay."""

    def __init__(self, config: Config):
        self.cfg = config
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: subprocess.Popen | None = None
        self._host, self._port = self._parse_url(config.tts_url)

    @staticmethod
    def _parse_url(url: str | None) -> tuple[str, int]:
        parsed = urlparse(url or "")
        return parsed.hostname or "127.0.0.1", parsed.port or 80

    def start(self) -> "Speaker":
        server_running = self._port_open()
        self._ensure_voice(gpu=not server_running)
        if not server_running:
            self._start_server()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 20.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._stop_server()

    def submit(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._queue.put(text)

    # --- server ------------------------------------------------------
    def _base_cmd(self) -> list[str]:
        cmd = [str(self.cfg.tts_server_bin), "--model", str(self.cfg.tts_model)]
        if self.cfg.tts_tokenizer.exists():
            cmd += ["--tokenizer", str(self.cfg.tts_tokenizer)]
        return cmd

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self._host, self._port), timeout=0.5):
                return True
        except OSError:
            return False

    def _start_server(self) -> None:
        if not self.cfg.tts_autostart:
            raise RuntimeError(
                f"TTS-сервер недоступен: {self.cfg.tts_url}. "
                "Запусти s2.cpp вручную или включи TTS_AUTOSTART."
            )
        if not self.cfg.tts_server_bin.exists():
            raise RuntimeError(f"Не найден бинарь s2.cpp: {self.cfg.tts_server_bin}")
        if not self.cfg.tts_model.exists():
            raise RuntimeError(f"Не найдена модель TTS: {self.cfg.tts_model}")
        cmd = self._base_cmd()
        cmd += ["--server", "--host", self._host, "--port", str(self._port)]
        cmd += self.cfg.tts_server_args
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Запуск TTS-сервера: {' '.join(cmd)}", flush=True)
        log = open(SERVER_LOG, "ab")
        self._server = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        log.close()
        try:
            self._wait_server()
        except Exception:
            self._stop_server()
            raise

    def _stop_server(self) -> None:
        """Остановить сервер, если его запустил Jarvis (в своём процессе-группе)."""
        proc = self._server
        if proc is None:
            return
        self._server = None
        if proc.poll() is not None:
            return
        print("Остановка TTS-сервера...", flush=True)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    def _wait_server(self) -> None:
        deadline = time.time() + SERVER_START_TIMEOUT
        while time.time() < deadline:
            if self._port_open():
                print("TTS-сервер готов.", flush=True)
                return
            if self._server is not None and self._server.poll() is not None:
                raise RuntimeError(
                    f"TTS-сервер завершился (код {self._server.returncode}); "
                    f"лог: {SERVER_LOG}"
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"TTS-сервер не поднялся за {SERVER_START_TIMEOUT:.0f} с; лог: {SERVER_LOG}"
        )

    # --- voice profile ----------------------------------------------
    def _profile_path(self) -> Path:
        return self.cfg.tts_voice_dir / f"{self.cfg.tts_voice}.s2voice"

    def _ensure_voice(self, gpu: bool = True) -> None:
        if self._profile_path().exists():
            return
        ref = self.cfg.tts_reference
        ref_text = self.cfg.tts_reference_text
        if not ref.exists() or not ref_text.exists():
            raise RuntimeError(
                f"Нет профиля {self._profile_path()} и референса "
                f"({ref} / {ref_text}) для его создания."
            )
        self.cfg.tts_voice_dir.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        output = RUNTIME_DIR / "voice_bootstrap.wav"
        cmd = self._base_cmd()
        if gpu:
            cmd += self.cfg.tts_server_args
        cmd += [
            "--prompt-audio", str(ref),
            "--prompt-text", ref_text.read_text(encoding="utf-8").strip(),
            "--voice", self.cfg.tts_voice,
            "--voice-dir", str(self.cfg.tts_voice_dir),
            "--save-voice",
            "--text", VOICE_BOOTSTRAP_TEXT,
            "--output", str(output),
        ]
        print(f"Создание профиля голоса «{self.cfg.tts_voice}»...", flush=True)
        result = subprocess.run(cmd)
        output.unlink(missing_ok=True)
        if result.returncode != 0 or not self._profile_path().exists():
            raise RuntimeError("Не удалось создать профиль голоса (см. вывод s2.cpp).")

    # --- worker ------------------------------------------------------
    def _run(self) -> None:
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                text = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            self._speak(text)

    def _speak(self, text: str) -> None:
        form = {
            "text": (None, text),
            "voice": (None, self.cfg.tts_voice),
            "voice_dir": (None, str(self.cfg.tts_voice_dir)),
            "params": (None, json.dumps(STREAM_PARAMS)),
        }
        try:
            with requests.post(
                self.cfg.tts_url, files=form, stream=True, timeout=(5, 600)
            ) as resp:
                resp.raise_for_status()
                rate = int(resp.headers.get("X-Audio-Sample-Rate", "44100"))
                self._play(resp.iter_content(chunk_size=8192), rate)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка синтеза речи: {exc}", file=os.sys.stderr, flush=True)

    def _play(self, chunks, rate: int) -> None:
        cmd = [
            "ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-infbuf",
            "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono", "-",
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except FileNotFoundError:
            print(
                "ffplay не найден (пакет ffmpeg). Установи ffmpeg для озвучки.",
                file=os.sys.stderr, flush=True,
            )
            return
        try:
            for chunk in chunks:
                try:
                    proc.stdin.write(chunk)  # type: ignore[union-attr]
                except (BrokenPipeError, OSError):
                    break
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError):
                pass
            proc.wait()
