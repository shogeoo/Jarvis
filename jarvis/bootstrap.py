"""Точка входа и подготовка окружения.

CUDA-библиотеки cuBLAS/cuDNN из pip-пакетов ``nvidia-*`` лежат вне
стандартных путей динамического линкера. До загрузки CTranslate2 их каталоги
добавляются в ``LD_LIBRARY_PATH``; если переменную пришлось изменить, процесс
перезапускается, чтобы новый env унаследовал динамический линкер.
"""

from __future__ import annotations

import os
import sys
import sysconfig

_REEXEC_FLAG = "JARVIS_CUDA_ENV_READY"


def _nvidia_lib_dirs() -> list[str]:
    site = sysconfig.get_paths()["purelib"]
    candidates = [
        os.path.join(site, "nvidia", "cublas", "lib"),
        os.path.join(site, "nvidia", "cudnn", "lib"),
    ]
    return [path for path in candidates if os.path.isdir(path)]


def prepare_cuda_env() -> None:
    """Добавить каталоги nvidia-* в LD_LIBRARY_PATH, при необходимости — re-exec."""
    if os.environ.get(_REEXEC_FLAG) or os.environ.get("JARVIS_NO_REEXEC"):
        return
    lib_dirs = _nvidia_lib_dirs()
    if not lib_dirs:
        return
    current = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if p]
    if all(path in current for path in lib_dirs):
        return
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(lib_dirs + current)
    os.environ[_REEXEC_FLAG] = "1"
    os.execv(sys.executable, [sys.executable, "-m", "jarvis", *sys.argv[1:]])


def main() -> int:
    prepare_cuda_env()
    from .cli import main as cli_main

    return cli_main()
