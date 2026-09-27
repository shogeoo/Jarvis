"""Mute other PulseAudio/PipeWire playback streams while Jarvis speaks."""

import json
import subprocess
import threading

from ..core.lifecycle import terminate_process
from ..infrastructure.console import logger

SPEECH_APPLICATION_ID = "jarvis.speech"


class SystemAudioMute:
    def __init__(self, player_pid: int):
        self.player_pid = str(player_pid)
        self._lock = threading.RLock()
        self._closed = False
        self._saved = {}
        self._subscription = None
        self._thread = None

    @staticmethod
    def _streams():
        result = subprocess.run(["pactl", "--format=json", "list", "sink-inputs"],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    @staticmethod
    def _identity(stream):
        properties = stream.get("properties", {})
        return (properties.get("application.process.id"), properties.get("object.serial"))

    @staticmethod
    def _set_mute(index, muted):
        subprocess.run(["pactl", "set-sink-input-mute", str(index), "1" if muted else "0"],
                       capture_output=True, text=True, check=True)

    def start(self):
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Speech audio muting was cancelled")
                self._subscription = subprocess.Popen(
                    ["pactl", "subscribe"], stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, start_new_session=True,
                )
                self._refresh()
                self._thread = threading.Thread(target=self._watch, name="jarvis-audio-mute", daemon=True)
                self._thread.start()
            return self
        except Exception:
            self.close()
            raise

    def _refresh(self):
        with self._lock:
            if self._closed:
                return
            streams = self._streams()
            live = {stream["index"] for stream in streams}
            for index in set(self._saved) - live:
                self._saved.pop(index)
            for stream in streams:
                properties = stream.get("properties", {})
                if (properties.get("application.id") == SPEECH_APPLICATION_ID or
                        str(properties.get("application.process.id")) == self.player_pid):
                    if stream["mute"]:
                        self._set_mute(stream["index"], False)
                    continue
                index = stream["index"]
                identity = self._identity(stream)
                previous = self._saved.get(index)
                if previous is None or previous[0] != identity:
                    self._saved[index] = (identity, stream["mute"])
                if not stream["mute"]:
                    self._set_mute(index, True)

    def _watch(self):
        for line in self._subscription.stdout:
            if " on sink-input " in line:
                try:
                    self._refresh()
                except Exception:
                    logger.exception("Could not mute a system playback stream")
            with self._lock:
                if self._closed:
                    return

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                live = {stream["index"]: stream for stream in self._streams()}
                for index, (identity, muted) in self._saved.items():
                    if index in live and self._identity(live[index]) == identity:
                        try:
                            self._set_mute(index, muted)
                        except Exception:
                            logger.exception("Could not restore system playback stream %s", index)
            except Exception:
                logger.exception("Could not restore system audio after speech")
            self._saved.clear()
            subscription = self._subscription
        if subscription is not None:
            terminate_process(subscription, group=True)
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=1)
            subscription.stdout.close()
