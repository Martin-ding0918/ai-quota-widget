"""测试包隔离：默认把 widget_app.config 的持久化路径指向临时目录。

发现于 v1.0.1：某测试经 config.save_config 误写真实用户的 %APPDATA% 配置
（局部 patch 未覆盖的组合场景）。本文件让整个测试套件默认**无法**写真实路径；
确需读写真实路径的测试必须显式恢复并只做读取。
"""

import tempfile
from pathlib import Path

_TEST_CONFIG_DIR = Path(tempfile.gettempdir()) / "kqw-tests-config"
_TEST_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

try:
    from widget_app import config as _config

    _config.CONFIG_DIR = _TEST_CONFIG_DIR
    _config.CONFIG_PATH = _TEST_CONFIG_DIR / "config.json"
except ImportError:  # pragma: no cover - 单模块调试时 widget_app 可能不可用
    pass
