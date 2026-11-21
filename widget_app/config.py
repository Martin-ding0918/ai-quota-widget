"""配置与凭据。

- config.json 存在 %APPDATA%\\kimi-quota-widget\\（不在同步目录里，遵守红线 5：
  敏感信息不进同步目录），保存服务器地址、用户名、token（按「规范化源地址|用户名」
  分桶）、窗口位置、贴边状态。
- 密码存 Windows 凭据管理器（keyring），键按「规范化源地址|用户名」绑定——更换
  服务器或用户名后不会复用旧密码；历史版本仅按用户名保存的密码不自动迁移，由
  用户重新输入（保存时顺带清理旧条目）。
- 开机自启写 HKCU\\...\\Run 注册表（winreg）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

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
    "window_x": None,
    "window_y": None,
    "snap_edge": "",        # 贴边收起："" | top | bottom | left | right
    "blink_enabled": True,  # 低额度（5h 剩余 <10%）时收起卡闪烁提醒
    "tokens": {},           # {"scheme://host[:port]|用户名": token}，按凭据作用域分桶
}


class ConfigError(RuntimeError):
    """配置或凭据写入失败。"""


def normalize_origin(server_url: str) -> str:
    """规范化为 scheme://host[:port]（默认端口省略、主机小写、剥离路径），
    作为凭据绑定的服务器身份；无法解析返回 ""。"""
    text = (server_url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text  # 裸 "host:port"（urlsplit 会整体当路径）
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    scheme = (parts.scheme or "http").lower()
    host = (parts.hostname or "").strip().lower()
    if not host:
        return ""
    port = parts.port
    if port and port != {"http": 80, "https": 443}.get(scheme):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def credential_scope(server_url: str, username: str) -> str:
    """凭据作用域键：「规范化源地址|用户名」。密码与 token 都绑定到它。"""
    username = (username or "").strip().lower()
    return f"{normalize_origin(server_url)}|{username}"


def load_config() -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError):
        return cfg  # 配置损坏时回退默认，不崩
    if not isinstance(raw, dict):
        return cfg
    for k in DEFAULTS:
        if k in raw:
            cfg[k] = raw[k]
    if not isinstance(cfg.get("tokens"), dict):
        cfg["tokens"] = {}
    # 旧版单 token 字段迁移：token 与其 server_url/username 同属一个 config，
    # 可以确定所属作用域，做同源迁移（不是跨服务器复用）。
    legacy_token = raw.get("token")
    if legacy_token and raw.get("username"):
        scope = credential_scope(cfg["server_url"], cfg["username"])
        if scope and not cfg["tokens"].get(scope):
            cfg["tokens"][scope] = legacy_token
        cfg.pop("token", None)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: cfg.get(k, DEFAULTS.get(k)) for k in DEFAULTS}
        payload["tokens"] = cfg.get("tokens") or {}
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"写入配置失败：{exc}") from exc


def get_stored_token(cfg: dict, server_url: str, username: str) -> str:
    """读取当前作用域的 token；无会话返回 ""。"""
    return (cfg.get("tokens") or {}).get(credential_scope(server_url, username), "")


def set_stored_token(cfg: dict, server_url: str, username: str, token: str) -> None:
    tokens = cfg.setdefault("tokens", {})
    if token:
        tokens[credential_scope(server_url, username)] = token
    else:
        tokens.pop(credential_scope(server_url, username), None)


# ---- Windows 凭据管理器（密码，键 = 凭据作用域） ----

def get_password(server_url: str, username: str):
    if not username or keyring is None:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, credential_scope(server_url, username))
    except Exception:
        return None


def set_password(server_url: str, username: str, password: str) -> None:
    if not username:
        raise ConfigError("用户名为空，无法保存密码")
    if keyring is None:
        raise ConfigError("keyring 不可用，无法保存密码")
    try:
        keyring.set_password(KEYRING_SERVICE, credential_scope(server_url, username), password)
        # 历史版本仅按用户名保存；新格式写入后清理旧条目，避免残留
        try:
            keyring.delete_password(KEYRING_SERVICE, username)
        except Exception:
            pass
    except Exception as exc:
        raise ConfigError(f"写入凭据管理器失败：{exc}") from exc


def delete_password(server_url: str, username: str) -> None:
    if not username or keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, credential_scope(server_url, username))
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
