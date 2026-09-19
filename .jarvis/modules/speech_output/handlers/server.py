import os
import threading


class SpeechOutput:
    def __init__(self, tasks):
        self.tasks = tasks
        self.speaker = None
        self.lock = threading.Lock()

    def start(self, ctx):
        enabled = os.environ.get("TTS_ENABLED", "true").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            self._reject_tasks(ctx, "Синтез речи отключён конфигурацией")
            return
        try:
            from ..config import load_config
            from ..tts import Speaker

            self.speaker = Speaker(load_config())
            self.speaker.start()
            ctx.services["speech_output"] = self.speaker
            ctx.emit("speech_output.status", {"ready": True, "message": "ready"})
            while not ctx.stop_event.is_set():
                task = self.tasks.get()
                if task is not None:
                    self._speak(task, ctx)
        except Exception as exc:
            if not ctx.stop_event.is_set():
                ctx.emit(
                    "speech_output.status",
                    {"ready": False, "message": str(exc)},
                )
                self._reject_tasks(ctx, str(exc))
        finally:
            self.stop(ctx)

    def stop(self, ctx):
        self.tasks.clear()
        self.tasks.close()
        with self.lock:
            ctx.services.pop("speech_output", None)
            speaker, self.speaker = self.speaker, None
        if speaker is not None:
            speaker.stop()

    def _speak(self, task, ctx):
        try:
            self.speaker.submit(task.data["text"]).result()
        except Exception as exc:
            status, error = "error", str(exc)
        else:
            status, error = "success", None
        ctx.emit(
            "speech_output.result",
            {"status": status, "text": task.data["text"], "error": error},
            target=task.agent_id,
        )

    def _reject_tasks(self, ctx, error):
        while not ctx.stop_event.is_set():
            task = self.tasks.get()
            if task is not None:
                ctx.emit(
                    "speech_output.result",
                    {"status": "error", "text": task.data["text"], "error": error},
                    target=task.agent_id,
                )
