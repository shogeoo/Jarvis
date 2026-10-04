"""Keep runtime diagnostics out of the model-facing console trace."""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
import logging
import os
from pathlib import Path
import sys


logger = logging.getLogger("jarvis")
logger.addHandler(logging.NullHandler())
logger.propagate = False


def configure_logging(root: Path) -> None:
    path = Path(root) / "runtime" / "logs" / "core.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@contextmanager
def runtime_console(root: Path):
    """Capture Python/native/subprocess noise; retain a separate protocol stream."""
    configure_logging(root)
    path = Path(root) / "runtime" / "logs" / "core.log"
    saved = {fd: os.dup(fd) for fd in (1, 2)}
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        with path.open("a", encoding="utf-8", buffering=1) as diagnostic:
            for fd in (1, 2):
                os.dup2(diagnostic.fileno(), fd)
            with redirect_stdout(diagnostic), redirect_stderr(diagnostic):
                yield protocol
    finally:
        protocol.flush()
        for fd, original in saved.items():
            os.dup2(original, fd)
            os.close(original)
        protocol.close()
