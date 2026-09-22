"""Подготовка CUDA-библиотек до первого импорта faster-whisper.

Вызывается только из точки входа приложения: при необходимости процесс
перезапускается с ``LD_LIBRARY_PATH`` на pip-библиотеки NVIDIA.
"""

from __future__ import annotations

import os
import sys
import sysconfig

from .config import STT_DEVICE, STT_ENABLED


_REEXEC_FLAG = "JARVIS_SPEECH_CUDA_READY"


def prepare() -> None:
    if not STT_ENABLED or STT_DEVICE != "cuda":
        return
    if os.environ.get(_REEXEC_FLAG) or os.environ.get("JARVIS_NO_REEXEC"):
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
    os.execv(
        sys.executable,
        [sys.executable, "-m", "jarvis.bootstrap", *sys.argv[1:]],
    )
