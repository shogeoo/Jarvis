"""Озвучка ответов через Fish Audio S2 Pro (s2.cpp).

Синтез идёт в ``POST {TTS_URL}`` (multipart/form-data), голос берётся из
профиля ``.s2voice``. Сервер s2.cpp держит модель в VRAM: Jarvis поднимает его
при старте (``TTS_AUTOSTART``), если он ещё не запущен, и останавливает при
выходе. Если сервер был запущен извне, Jarvis им не управляет.
"""

from __future__ import annotations

from ..infrastructure.console import logger

import ctypes
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from jarvis.core.lifecycle import terminate_process

from .config import Config
from .ducking import SystemAudioMute, SPEECH_APPLICATION_ID

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

PR_SET_PDEATHSIG = 1


def _set_pdeathsig() -> None:
    """Ребёнок умирает вместе с Jarvis, даже если Jarvis убит сигналом."""

    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() == 1:
        raise RuntimeError("родительский процесс уже завершён")


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
        self._audio_mute: SystemAudioMute | None = None
        self._host, self._port = self._parse_url(config.tts_url)

    @staticmethod
    def _parse_url(url: str | None) -> tuple[str, int]:
        parsed = urlparse(url or "")
        return parsed.hostname or "127.0.0.1", parsed.port or 80

    def start(self) -> "Speaker":
        logger.info(
            f"TTS Fish Audio: подключение к {self.cfg.tts_url} "
            f"(голос: {self.cfg.tts_voice})...",
        )
        server_running = self._port_open()
        if server_running:
            logger.info("TTS Fish Audio: сервер уже запущен.")
        self._ensure_voice()
        if not server_running:
            self._start_server()
        else:
            self._wait_server()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        if self._stop.is_set():
            return
        logger.info("TTS Fish Audio: остановка.")
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
        ожидающее её действие озвучки получит модульное событие interruption.
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
            audio_mute = getattr(self, "_audio_mute", None)
        if player is not None and player.poll() is None:
            try:
                player.terminate()
            except ProcessLookupError:
                pass
        if audio_mute is not None:
            audio_mute.close()
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
    def _log_path(self) -> Path:
        return self.cfg.runtime_dir / "logs" / "tts-server.log"

    def _base_cmd(self) -> list[str]:
        cmd = [self._resolve_binary(), "--model", str(self.cfg.tts_model)]
        if self.cfg.tts_tokenizer.exists():
            cmd += ["--tokenizer", str(self.cfg.tts_tokenizer)]
        return cmd

    def _resolve_binary(self) -> str:
        configured = str(self.cfg.tts_server_bin).strip()
        expanded = Path(configured).expanduser()
        if expanded.is_absolute() or "/" in configured:
            if not expanded.is_file():
                raise RuntimeError(f"Не найден бинарь s2.cpp: {expanded}")
            return str(expanded)
        resolved = shutil.which(configured)
        if resolved is None:
            raise RuntimeError(f"Не найдена команда синтеза речи в PATH: {configured}")
        return resolved

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self._host, self._port), timeout=0.5):
                return True
        except OSError:
            return False

    def _http_ready(self) -> bool:
        try:
            response = requests.get(self.cfg.tts_url, timeout=0.5)
            return response.status_code < 500
        except requests.RequestException:
            return False

    def _start_server(self) -> None:
        if not self.cfg.tts_autostart:
            raise RuntimeError(
                f"TTS Fish Audio недоступен: {self.cfg.tts_url}. "
                "Запусти s2.cpp вручную или включи TTS_AUTOSTART."
            )
        self._resolve_binary()
        if not self.cfg.tts_model.exists():
            raise RuntimeError(f"Не найдена модель TTS: {self.cfg.tts_model}")
        cmd = self._base_cmd()
        cmd += ["--server", "--host", self._host, "--port", str(self._port)]
        cmd += self.cfg.tts_server_args
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._log_path().parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"TTS Fish Audio: запуск сервера: {' '.join(cmd)}",
        )
        with open(self._log_path(), "ab") as log:
            self._server = subprocess.Popen(
                cmd,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_set_pdeathsig,
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
        logger.info("TTS Fish Audio: остановка сервера...")
        terminate_process(proc, group=True)

    def _wait_server(self) -> None:
        deadline = time.time() + SERVER_START_TIMEOUT
        while time.time() < deadline:
            if self._http_ready():
                logger.info("TTS Fish Audio: веб-сервер готов.")
                return
            if self._server is not None and self._server.poll() is not None:
                raise RuntimeError(
                    f"TTS Fish Audio завершился (код {self._server.returncode}); "
                    f"лог: {self._log_path()}"
                )
            time.sleep(0.5)
        raise RuntimeError(
            f"TTS Fish Audio не поднялся за {SERVER_START_TIMEOUT:.0f} с; "
            f"лог: {self._log_path()}"
        )

    # --- voice profile ----------------------------------------------
    def _profile_path(self) -> Path:
        return self.cfg.tts_voice_dir / f"{self.cfg.tts_voice}.s2voice"

    def _ensure_voice(self) -> None:
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
        self.cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
        output = self.cfg.runtime_dir / "voice_bootstrap.wav"
        cmd = self._base_cmd()
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
        logger.info(f"Создание профиля голоса «{self.cfg.tts_voice}»...")
        self._bootstrap = subprocess.Popen(
            cmd, start_new_session=True, preexec_fn=_set_pdeathsig
        )
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
            logger.error(f"Ошибка синтеза речи: {exc}")
            raise
        finally:
            with self._lock:
                self._response = None

    def _play(self, chunks, rate: int, request: _SpeechRequest) -> None:
        chunks = iter(chunks)
        buffer = bytearray()
        target_bytes = max(1, int(rate * 2 * self.cfg.tts_start_buffer_seconds))
        while len(buffer) < target_bytes:
            if request.interrupted.is_set():
                raise SpeechInterrupted("interrupted")
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            if chunk:
                buffer.extend(chunk)
        if not buffer:
            return
        cmd = [
            "ffplay", "-autoexit", "-nodisp", "-loglevel", "error", "-infbuf",
            "-f", "s16le", "-ar", str(rate), "-ch_layout", "mono", "-",
        ]
        try:
            with self._lock:
                if self._stop.is_set() or request.interrupted.is_set():
                    raise SpeechInterrupted("interrupted")
                environment = {
                    **os.environ,
                    "SDL_AUDIODRIVER": "pulseaudio",
                    "PULSE_PROP": f"application.id={SPEECH_APPLICATION_ID} application.name=Jarvis media.role=assistant",
                }
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, start_new_session=True, env=environment)
                self._player = proc
        except FileNotFoundError:
            logger.info(
                "ffplay не найден (пакет ffmpeg). Установи ffmpeg для озвучки.",
            )
            raise RuntimeError("ffplay_not_found")
        audio_mute = None
        try:
            audio_mute = SystemAudioMute(proc.pid)
            with self._lock:
                self._audio_mute = audio_mute
            audio_mute.start()
            if request.interrupted.is_set():
                raise SpeechInterrupted("interrupted")
            try:
                proc.stdin.write(buffer)  # type: ignore[union-attr]
            except (BrokenPipeError, OSError):
                return
            for chunk in chunks:
                if request.interrupted.is_set():
                    raise SpeechInterrupted("interrupted")
                try:
                    proc.stdin.write(chunk)  # type: ignore[union-attr]
                except (BrokenPipeError, OSError):
                    break
        finally:
            try:
                if request.interrupted.is_set() and proc.poll() is None:
                    terminate_process(proc)
                try:
                    proc.stdin.close()  # type: ignore[union-attr]
                except (BrokenPipeError, OSError, ValueError):
                    pass
                proc.wait()
            finally:
                if audio_mute is not None:
                    audio_mute.close()
                with self._lock:
                    if self._player is proc:
                        self._player = None
                    if getattr(self, "_audio_mute", None) is audio_mute:
                        self._audio_mute = None
        if request.interrupted.is_set():
            raise SpeechInterrupted("interrupted")
