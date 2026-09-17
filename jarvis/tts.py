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
import socket
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import ROOT, Config
from .lifecycle import terminate_process

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


class SpeechInterrupted(RuntimeError):
    """Реплика не была воспроизведена из-за перебивания пользователя."""


@dataclass(slots=True)
class _SpeechRequest:
    text: str
    future: Future
    interrupted: threading.Event


class Speaker:
    """Очередь реплик -> синтез на s2.cpp -> воспроизведение через ffplay."""

    def __init__(self, config: Config):
        self.cfg = config
        self._queue: "queue.Queue[_SpeechRequest]" = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._server: subprocess.Popen | None = None
        self._bootstrap: subprocess.Popen | None = None
        self._current: _SpeechRequest | None = None
        self._response: requests.Response | None = None
        self._player: subprocess.Popen | None = None
        self._host, self._port = self._parse_url(config.tts_url)

    @staticmethod
    def _parse_url(url: str | None) -> tuple[str, int]:
        parsed = urlparse(url or "")
        return parsed.hostname or "127.0.0.1", parsed.port or 80

    def start(self) -> "Speaker":
        print(
            f"TTS Fish Audio: подключение к {self.cfg.tts_url} "
            f"(голос: {self.cfg.tts_voice})...",
            flush=True,
        )
        server_running = self._port_open()
        if server_running:
            print("TTS Fish Audio: сервер уже запущен.", flush=True)
        self._ensure_voice(gpu=not server_running)
        if not server_running:
            self._start_server()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("TTS Fish Audio: готов.", flush=True)
        return self

    def stop(self, timeout: float = 2.0) -> None:
        if self._stop.is_set():
            return
        print("TTS Fish Audio: остановка.", flush=True)
        self._stop.set()
        self._ready.set()
        self.interrupt()
        with self._lock:
            player = self._player
        if player is not None:
            terminate_process(player)
        if self._bootstrap is not None:
            terminate_process(self._bootstrap, group=True)
        self._stop_server()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def submit(self, text: str) -> Future:
        text = (text or "").strip()
        future: Future = Future()
        if not text:
            future.set_result({"spoken": False})
            return future
        with self._lock:
            if self._stop.is_set():
                future.set_exception(RuntimeError("speaker_stopped"))
                return future
            self._queue.put(
                _SpeechRequest(
                    text=text,
                    future=future,
                    interrupted=threading.Event(),
                )
            )
            self._ready.set()
        return future

    def interrupt(self) -> int:
        """Остановить текущую реплику и отменить всю очередь озвучки.

        Каждая отменённая заявка получает собственное исключение, поэтому
        ожидающее её действие speech вернёт отдельный action_result.
        """

        requests: list[_SpeechRequest] = []
        with self._lock:
            if self._current is not None and not self._current.future.done():
                requests.append(self._current)
            while True:
                try:
                    requests.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            response = self._response
            player = self._player
            for request in requests:
                request.interrupted.set()
                if not request.future.done():
                    request.future.set_exception(SpeechInterrupted("interrupted"))
        if player is not None and player.poll() is None:
            try:
                player.terminate()
            except ProcessLookupError:
                pass
        if response is not None:
            # close() can wait for a concurrent socket read. Never block VAD
            # or the main shutdown path on that read.
            def close_stream():
                try:
                    response.close()
                except Exception:
                    pass
            threading.Thread(target=close_stream, daemon=True).start()
        return len(requests)

    def has_pending(self) -> bool:
        """Есть ли текущая или ожидающая реплика, которую можно перебить."""

        with self._lock:
            return self._current is not None or not self._queue.empty()

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
                f"TTS Fish Audio недоступен: {self.cfg.tts_url}. "
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
        print(
            f"TTS Fish Audio: запуск сервера: {' '.join(cmd)}",
            flush=True,
        )
        with open(SERVER_LOG, "ab") as log:
            self._server = subprocess.Popen(
                cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
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
        print("TTS Fish Audio: остановка сервера...", flush=True)
        terminate_process(proc, group=True)

    def _wait_server(self) -> None:
        deadline = time.time() + SERVER_START_TIMEOUT
        while time.time() < deadline:
            if self._port_open():
                print("TTS Fish Audio: сервер готов.", flush=True)
                return
            if self._server is not None and self._server.poll() is not None:
                raise RuntimeError(
                    f"TTS Fish Audio завершился (код {self._server.returncode}); "
                    f"лог: {SERVER_LOG}"
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"TTS Fish Audio не поднялся за {SERVER_START_TIMEOUT:.0f} с; "
            f"лог: {SERVER_LOG}"
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
        self._bootstrap = subprocess.Popen(cmd, start_new_session=True)
        self._bootstrap.wait()
        output.unlink(missing_ok=True)
        if self._bootstrap.returncode != 0 or not self._profile_path().exists():
            raise RuntimeError("Не удалось создать профиль голоса (см. вывод s2.cpp).")
        self._bootstrap = None

    # --- worker ------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                try:
                    request = self._queue.get_nowait()
                    self._current = request
                except queue.Empty:
                    request = None
                    self._ready.clear()
            if request is None:
                self._ready.wait(timeout=0.1)
                continue
            try:
                self._speak(request)
                with self._lock:
                    if not request.future.done():
                        request.future.set_result({"spoken": True})
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    if not request.future.done():
                        request.future.set_exception(exc)
            finally:
                with self._lock:
                    if self._current is request:
                        self._current = None

    def _speak(self, request: _SpeechRequest) -> None:
        if self._stop.is_set() or request.interrupted.is_set():
            raise SpeechInterrupted("interrupted")
        form = {
            "text": (None, request.text),
            "voice": (None, self.cfg.tts_voice),
            "voice_dir": (None, str(self.cfg.tts_voice_dir)),
            "params": (None, json.dumps(STREAM_PARAMS)),
        }
        try:
            with requests.post(
                self.cfg.tts_url, files=form, stream=True, timeout=(5, 600)
            ) as resp:
                with self._lock:
                    self._response = resp
                if request.interrupted.is_set():
                    raise SpeechInterrupted("interrupted")
                resp.raise_for_status()
                rate = int(resp.headers.get("X-Audio-Sample-Rate", "44100"))
                self._play(resp.iter_content(chunk_size=8192), rate, request)
        except Exception as exc:  # noqa: BLE001
            if request.interrupted.is_set():
                raise SpeechInterrupted("interrupted") from None
            if isinstance(exc, SpeechInterrupted):
                raise
            print(f"Ошибка синтеза речи: {exc}", file=os.sys.stderr, flush=True)
            raise
        finally:
            with self._lock:
                self._response = None

    def _play(self, chunks, rate: int, request: _SpeechRequest) -> None:
        cmd = [
            "ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-infbuf",
            "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono", "-",
        ]
        try:
            with self._lock:
                if self._stop.is_set() or request.interrupted.is_set():
                    raise SpeechInterrupted("interrupted")
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True)
                self._player = proc
        except FileNotFoundError:
            print(
                "ffplay не найден (пакет ffmpeg). Установи ffmpeg для озвучки.",
                file=os.sys.stderr, flush=True,
            )
            raise RuntimeError("ffplay_not_found")
        try:
            for chunk in chunks:
                if request.interrupted.is_set():
                    raise SpeechInterrupted("interrupted")
                try:
                    proc.stdin.write(chunk)  # type: ignore[union-attr]
                except (BrokenPipeError, OSError):
                    break
        finally:
            if request.interrupted.is_set() and proc.poll() is None:
                terminate_process(proc)
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError):
                pass
            proc.wait()
            with self._lock:
                if self._player is proc:
                    self._player = None
        if request.interrupted.is_set():
            raise SpeechInterrupted("interrupted")
