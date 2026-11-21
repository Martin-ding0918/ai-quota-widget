"""配置与凭据。

- config.json 存在 %APPDATA%\\kimi-quota-widget\\（不在同步目录里，遵守红线 5：
  敏感信息不进同步目录），保存服务器地址、用户名、token、窗口位置。
- 密码存 Windows 凭据管理器（keyring），不明文落盘。
- 开机自启写 HKCU\\...\\Run 注册表（winreg）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import keyring
except Exception:  # pragma: no cover - keyring 不可用时降级为凭据缺失
    keyring = None

try:
    import winreg
except ImportError:  # pragma: no cover - 仅 Windows 需要
    winreg = None

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "kimi-quota-widget"
CONFIG_PATH = CONFIG_DIR / "config.json"
KEYRING_SERVICE = "kimi-quota-widget"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "KimiQuotaWidget"

DEFAULTS = {
    "server_url": "http://localhost:4188",
    "username": "",
    "token": "",
    "window_x": None,
    "window_y": None,
    "snap_edge": "",        # 贴边收起："" | top | bottom | left | right
    "blink_enabled": True,  # 低额度（5h 剩余 <10%）时收起卡闪烁提醒
}


class ConfigError(RuntimeError):
    """配置或凭据写入失败。"""


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError):
        # 配置损坏时回退默认，不崩
        return cfg
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: cfg.get(k, DEFAULTS.get(k)) for k in DEFAULTS}
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"写入配置失败：{exc}") from exc


def get_password(username: str):
    """从 Windows 凭据管理器读密码；未设置或 keyring 不可用返回 None。"""
    if not username or keyring is None:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


def set_password(username: str, password: str) -> None:
    if not username:
        raise ConfigError("用户名为空，无法保存密码")
    if keyring is None:
        raise ConfigError("keyring 不可用，无法保存密码")
    try:
        keyring.set_password(KEYRING_SERVICE, username, password)
    except Exception as exc:
        raise ConfigError(f"写入凭据管理器失败：{exc}") from exc


def delete_password(username: str) -> None:
    if not username or keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
    except Exception:
        pass


# ---- 开机自启（HKCU Run 注册表） ----

def autostart_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return f'"{sys.executable}"'
    main_py = Path(sys.argv[0]).resolve()
    python_exe = Path(sys.executable)
    pythonw = python_exe.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python_exe
    return f'"{exe}" "{main_py}"'


def autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if winreg is None:
        raise ConfigError("仅支持 Windows 注册表")
    try:
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise ConfigError(f"注册表写入失败：{exc}") from exc
