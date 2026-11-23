"""入口：启动 AI 用量悬浮窗。

用法：python main.py（前台）；自启用 pythonw.exe 运行（无控制台）。
首次运行没有凭据时会自动弹出设置对话框。
"""

import ctypes
import sys

from PySide6.QtWidgets import QApplication

from widget_app.single_instance import MUTEX_NAME, SingleInstance
from widget_app.widget import QuotaWidget


def _notify_already_running() -> None:
    message = "AI Quota Widget 已在运行。"
    title = "AI 用量悬浮窗"
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    else:  # pragma: no cover - the widget targets Windows
        print(message)


def main() -> int:
    guard = SingleInstance(MUTEX_NAME)
    if guard.already_running:
        guard.close()
        _notify_already_running()
        return 0
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("kimi-quota-widget")
        widget = QuotaWidget()
        widget.show()
        if not widget.has_credentials():
            widget.show_settings()
        return app.exec()
    finally:
        guard.close()


if __name__ == "__main__":
    sys.exit(main())
