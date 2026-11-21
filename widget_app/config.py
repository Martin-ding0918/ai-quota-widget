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
}

# 会话 token 存 Windows 凭据管理器（与密码同一服务族、独立子键），
# config.json 不再保存任何 token。作用域键与密码一致。
KEYRING_TOKEN_SERVICE = KEYRING_SERVICE + "/token"


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
    # 旧版遗留（迁移源，migrate_tokens_to_keyring 处理后清除）：
    # raw["token"] = v1.0.1 及之前的单 token 字段；raw["tokens"] = v1.0.2 的分桶
    cfg["token"] = raw.get("token") or ""
    cfg["legacy_tokens"] = raw.get("tokens") or {}
    if not isinstance(cfg["legacy_tokens"], dict):
        cfg["legacy_tokens"] = {}
    return cfg


def migrate_tokens_to_keyring(cfg: dict) -> bool:
    """把 config.json 里的会话 token（旧单字段 + v1.0.2 分桶）迁入凭据管理器。

    按「规范化源地址|用户名」作用域写入；每条写入后**先验证可读**，全部通过
    才从 cfg 清除旧字段（调用方随后 save_config 落盘清除）。任何一步失败都
    放弃迁移并回滚已写条目——config.json 里仍保留旧值，应用照常工作。
    返回是否发生了迁移。
    """
    moved: list[tuple[str, str]] = []
    server = cfg.get("server_url") or ""
    username = cfg.get("username") or ""
    if cfg.get("token") and username:
        moved.append((credential_scope(server, username), cfg["token"]))
    for scope, token in (cfg.get("legacy_tokens") or {}).items():
        if token and scope:
            moved.append((scope, token))
    if not moved or keyring is None:
        return False
    written: list[str] = []
    try:
        for scope, token in moved:
            keyring.set_password(KEYRING_TOKEN_SERVICE, scope, token)
            written.append(scope)
            if keyring.get_password(KEYRING_TOKEN_SERVICE, scope) != token:
                raise RuntimeError("写后读校验失败")
    except Exception:
        # 回滚已写条目，保留 config.json 里的旧值
        for scope in written:
            try:
                keyring.delete_password(KEYRING_TOKEN_SERVICE, scope)
            except Exception:
                pass
        return False
    cfg["token"] = ""
    cfg["legacy_tokens"] = {}
    return True


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: cfg.get(k, DEFAULTS.get(k)) for k in DEFAULTS}
        CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"写入配置失败：{exc}") from exc


def get_stored_token(cfg: dict, server_url: str, username: str) -> str:
    """读取当前作用域的会话 token（凭据管理器优先，旧 config.json 字段回退）；
    无会话返回 ""。"""
    scope = credential_scope(server_url, username)
    if keyring is not None:
        try:
            token = keyring.get_password(KEYRING_TOKEN_SERVICE, scope)
            if token:
                return token
        except Exception:
            pass
    # 旧字段回退仅当其作用域与查询一致（同一 config 内的 server/username）
    same_scope = credential_scope(cfg.get("server_url") or "", cfg.get("username") or "") == scope
    if same_scope:
        return cfg.get("token") or ""
    return ""


def set_stored_token(cfg: dict, server_url: str, username: str, token: str) -> None:
    if keyring is None:
        raise ConfigError("keyring 不可用，无法保存会话 token")
    scope = credential_scope(server_url, username)
    if token:
        keyring.set_password(KEYRING_TOKEN_SERVICE, scope, token)
    else:
        try:
            keyring.delete_password(KEYRING_TOKEN_SERVICE, scope)
        except Exception:
            pass
    if cfg.get("token"):
        cfg["token"] = ""  # 旧字段不再维护


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
