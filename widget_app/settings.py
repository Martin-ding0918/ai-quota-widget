"""Non-blocking settings for the connection and the two card lists."""

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from .config import credential_scope, http_security

SETTINGS_STYLE = """
QDialog { background: #14171c; color: #e8ebef; }
QLabel { color: #bec5cf; }
QTabWidget::pane { background: #1b2027; border: 1px solid #303741; border-radius: 8px; }
QTabBar::tab { padding: 8px 16px; background: #1b2027; color: #aab5c1; border-radius: 6px; }
QTabBar::tab:selected { color: #f2f5f8; background: #252c35; }
QLineEdit, QComboBox, QListWidget {
    background: #15191e; color: #edf0f4; border: 1px solid #363e49; border-radius: 6px;
}
QLineEdit, QComboBox { padding: 5px 8px; }
QLineEdit:focus, QComboBox:focus, QListWidget:focus { border-color: #7ac7a0; }
QCheckBox { color: #bec5cf; }
QListWidget { alternate-background-color: #1c2229; outline: none; padding: 3px; }
QListWidget::item { padding: 6px 5px; }
QListWidget::item:selected, QComboBox QAbstractItemView::item:selected {
    background: #29423a; color: #f1f8f5;
}
QComboBox QAbstractItemView { background: #1b2027; color: #edf0f4; selection-background-color: #29423a; }
QPushButton {
    background: #252c35; color: #e8ebef; padding: 5px 14px;
    border: 1px solid #3a4450; border-radius: 6px;
}
QPushButton:hover, QPushButton:focus { border-color: #7ac7a0; }
QPushButton#saveSettings { background: #81ceaa; color: #15241d; border-color: #81ceaa; font-weight: bold; }
QPushButton#saveSettings:hover, QPushButton#saveSettings:focus { background: #a0e1bf; }
"""

class SettingsDialog(QDialog):
    save_requested = Signal()

    def __init__(self, cfg, services, panel_ids, mini_ids):
        super().__init__()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle("AI 用量悬浮窗 — 设置")
        self.setStyleSheet(SETTINGS_STYLE)
        self.resize(485, 500)
        self.scope = credential_scope(cfg["server_url"], cfg["username"])
        self.selection_changed = False
        self.services = services
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        tabs = QTabWidget()
        root.addWidget(tabs)
        account = QWidget()
        form = QFormLayout(account)
        form.setContentsMargins(14, 20, 14, 20)
        form.setSpacing(12)
        self.server = QLineEdit(cfg["server_url"])
        self.user = QLineEdit(cfg["username"])
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("留空则保留当前帐户的密码")
        form.addRow("服务器地址", self.server)
        form.addRow("用户名", self.user)
        form.addRow("密码", self.password)
        self.allow_insecure = QCheckBox("允许不安全的 HTTP（密码和 token 将明文传输）")
        self.allow_insecure.setChecked(bool(cfg.get("allow_insecure_http", False)))
        self.insecure_warning = QLabel("")
        self.insecure_warning.setWordWrap(True)
        self.insecure_warning.setStyleSheet("color: #e0a860;")
        form.addRow(self.allow_insecure)
        form.addRow(self.insecure_warning)
        tabs.addTab(account, "连接")

        display = QWidget()
        display_layout = QVBoxLayout(display)
        display_layout.setContentsMargins(14, 14, 14, 14)
        self.hint = QLabel("勾选展开面板要显示的服务。列表顺序就是卡片顺序。")
        self.hint.setWordWrap(True)
        display_layout.addWidget(self.hint)
        self.items = QListWidget()
        self.items.setAlternatingRowColors(True)
        display_layout.addWidget(self.items, 1)
        order_buttons = QHBoxLayout()
        for label, delta in (("上移", -1), ("下移", 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked, step=delta: self._move(step))
            order_buttons.addWidget(button)
        order_buttons.addStretch()
        display_layout.addLayout(order_buttons)
        mini_form = QFormLayout()
        self.mini_a = QComboBox()
        self.mini_b = QComboBox()
        mini_form.addRow("迷你卡第 1 项", self.mini_a)
        mini_form.addRow("迷你卡第 2 项", self.mini_b)
        display_layout.addLayout(mini_form)
        tabs.addTab(display, "显示服务")
        self.display = display
        self.items.itemChanged.connect(self._selection_edit)
        self.mini_a.currentIndexChanged.connect(self._mini_edit)
        self.mini_b.currentIndexChanged.connect(self._mini_edit)
        self.server.textChanged.connect(self._check_scope)
        self.user.textChanged.connect(self._check_scope)
        self.allow_insecure.toggled.connect(self._check_scope)
        self.populate(services, panel_ids, mini_ids)
        if services:
            tabs.setCurrentWidget(display)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("saveSettings")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save_requested)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def credentials(self):
        return self.server.text().strip(), self.user.text().strip(), self.password.text()

    def allow_insecure_http(self):
        return self.allow_insecure.isChecked()

    def populate(self, services, panel_ids, mini_ids):
        if self.selection_changed:
            return
        self.services = services
        with QSignalBlocker(self.items):
            self.items.clear()
            for service_id in dict.fromkeys([*panel_ids, *services]):
                service = services[service_id]
                row = QListWidgetItem(f"{service.name}  ·  {service.kind}  ·  #{service_id}")
                row.setData(Qt.ItemDataRole.UserRole, service_id)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Checked if service_id in panel_ids else Qt.CheckState.Unchecked)
                self.items.addItem(row)
        self._populate_mini(mini_ids)
        self._check_scope()

    def _check_scope(self):
        server, user, _ = self.credentials()
        insecure = http_security(server) == "insecure"
        self.allow_insecure.setVisible(insecure)
        self.allow_insecure.setEnabled(insecure)
        self.insecure_warning.setVisible(insecure)
        if insecure:
            if self.allow_insecure.isChecked():
                self.insecure_warning.setText(
                    "已允许不安全的 HTTP：密码和 token 将明文传输。建议通过反向代理改用 HTTPS。")
            else:
                self.insecure_warning.setText(
                    "此地址使用 HTTP，密码和 token 将明文传输；保存前请勾选上面的选项。")
        current = credential_scope(server, user) == self.scope
        self.items.setEnabled(current and bool(self.services))
        self.mini_a.setEnabled(current and bool(self._panel_ids()))
        self.mini_b.setEnabled(current and len(self._panel_ids()) > 1)
        if not current:
            message = "先保存新连接，再为新帐户选择服务。"
        elif not self.services:
            message = "连接成功后会在这里列出服务。"
        else:
            message = "勾选服务，使用上移或下移调整展开面板的顺序。"
        self.hint.setText(message)

    def _panel_ids(self):
        return [self.items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.items.count())
                if self.items.item(i).checkState() == Qt.CheckState.Checked]

    def _populate_mini(self, selected):
        panel = self._panel_ids()
        selected = [i for i in dict.fromkeys(selected) if i in panel]
        first = selected[0] if selected else (panel[0] if panel else None)
        second = selected[1] if len(selected) > 1 else None
        for index, combo, value in ((0, self.mini_a, first), (1, self.mini_b, second)):
            with QSignalBlocker(combo):
                combo.clear()
                if index == 1 or not panel:
                    combo.addItem("不显示", None)
                for service_id in panel:
                    if index == 0 or service_id != first:
                        combo.addItem(self.services[service_id].name, service_id)
                combo.setCurrentIndex(max(0, combo.findData(value)))

    def _selection_edit(self):
        self.selection_changed = True
        self._populate_mini((self.mini_a.currentData(), self.mini_b.currentData()))
        self._check_scope()

    def _mini_edit(self):
        self.selection_changed = True
        self._populate_mini((self.mini_a.currentData(), self.mini_b.currentData()))

    def _move(self, delta):
        row = self.items.currentRow()
        if not self.items.isEnabled() or row < 0 or not 0 <= row + delta < self.items.count():
            return
        entry = self.items.takeItem(row)
        self.items.insertItem(row + delta, entry)
        self.items.setCurrentRow(row + delta)
        self.selection_changed = True

    def preferences(self):
        return {
            "panel_ids": self._panel_ids(),
            "mini_ids": [i for i in (self.mini_a.currentData(), self.mini_b.currentData()) if i is not None],
        }

