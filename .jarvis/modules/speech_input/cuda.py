import os
import sys
import sysconfig


_REEXEC_FLAG = "JARVIS_SPEECH_INPUT_CUDA_READY"


def _enabled():
    stt_enabled = os.environ.get("STT_ENABLED", "true").strip().lower()
    device = os.environ.get("STT_DEVICE", "cuda").strip().lower()
    return stt_enabled in {"1", "true", "yes", "on"} and device == "cuda"


def prepare_cuda_env(config):
    """Добавить pip-библиотеки NVIDIA до импорта faster-whisper."""

    if (
        not _enabled()
        or os.environ.get(_REEXEC_FLAG)
        or os.environ.get("JARVIS_NO_REEXEC")
    ):
        return
    site = sysconfig.get_paths()["purelib"]
    candidates = [
        os.path.join(site, "nvidia", "cublas", "lib"),
        os.path.join(site, "nvidia", "cudnn", "lib"),
    ]
    libraries = [path for path in candidates if os.path.isdir(path)]
    if not libraries:
        return
    current = [
        path
        for path in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if path
    ]
    if all(path in current for path in libraries):
        return
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(libraries + current)
    os.environ[_REEXEC_FLAG] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "jarvis", *sys.argv[1:]])
