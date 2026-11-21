"""入口：启动 Kimi 额度悬浮窗。

用法：python main.py（前台）；自启用 pythonw.exe 运行（无控制台）。
首次运行没有凭据时会自动弹出设置对话框。
"""

import sys

from PySide6.QtWidgets import QApplication

from widget_app.widget import QuotaWidget


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("kimi-quota-widget")
    widget = QuotaWidget()
    widget.show()
    if not widget.has_credentials():
        widget.show_settings()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
