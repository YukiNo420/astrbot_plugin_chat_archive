"""Use the installed framework or a logger stub for standalone CI."""

import logging
import sys
import types

try:
    import astrbot.api  # noqa: F401
except ImportError:
    framework = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("archive-tests")
    sys.modules.setdefault("astrbot", framework)
    sys.modules.setdefault("astrbot.api", api)
