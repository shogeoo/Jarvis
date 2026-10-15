"""One naming contract for presets and standalone capability IDs."""

import re

ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
