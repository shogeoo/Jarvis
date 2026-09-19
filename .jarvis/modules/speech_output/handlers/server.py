import os
import threading

class SpeechOutput:
    def __init__(self):
        self.speaker = None
        self.lock = threading.Lock()

    def start(self, ctx):
        enabled = os.environ.get("TTS_ENABLED", "true").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            while not ctx.stop_event.wait(0.2):
                pass
            return
        try:
            from ..config import load_config
            from ..tts import Speaker

            self.speaker = Speaker(load_config())
            self.speaker.start()
            ctx.services["speech_output"] = self.speaker
            ctx.emit("speech_output.status", {"ready": True, "message": "ready"})
            while not ctx.stop_event.wait(0.2):
                pass
        except Exception as exc:
            if not ctx.stop_event.is_set():
                ctx.emit(
                    "speech_output.status",
                    {"ready": False, "message": str(exc)},
                )
        finally:
            self.stop(ctx)

    def stop(self, ctx):
        with self.lock:
            ctx.services.pop("speech_output", None)
            speaker, self.speaker = self.speaker, None
        if speaker is not None:
            speaker.stop()
