#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载/安装结束后自动关机（Windows 单文件程序）。

默认 DRY_RUN = True：只模拟倒计时，绝不执行真正的关机命令。
确认无误后，把下方 DRY_RUN 改为 False，再运行或重新打包。
"""

from __future__ import annotations

import configparser
import ctypes
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------

DRY_RUN = False  # 当前已启用真实关机；如需仅模拟，改回 True 并重新打包

# 默认阈值；实际值从程序同目录的 config.ini 读取，也可在主界面“设置”中修改。
DEFAULT_NET_LOW_VALUE = 100.0
DEFAULT_NET_UNIT = "KB/s"
DEFAULT_DISK_LOW_VALUE = 700.0
DEFAULT_DISK_UNIT = "KB/s"
DEFAULT_CPU_LOW_PERCENT = 10.0
DEFAULT_LOW_ACTIVITY_SECONDS = 10 * 60
DEFAULT_COUNTDOWN_SECONDS = 120

RATE_UNIT_FACTORS = {
    "B/s": 1,
    "KB/s": 1024,
    "MB/s": 1024 ** 2,
    "GB/s": 1024 ** 3,
}
RATE_UNIT_CHOICES = tuple(RATE_UNIT_FACTORS)

NET_LOW_VALUE = DEFAULT_NET_LOW_VALUE
NET_UNIT = DEFAULT_NET_UNIT
DISK_LOW_VALUE = DEFAULT_DISK_LOW_VALUE
DISK_UNIT = DEFAULT_DISK_UNIT
CPU_LOW_PERCENT = DEFAULT_CPU_LOW_PERCENT
LOW_ACTIVITY_SECONDS = DEFAULT_LOW_ACTIVITY_SECONDS
COUNTDOWN_SECONDS = DEFAULT_COUNTDOWN_SECONDS
SAMPLE_INTERVAL_MS = 1000     # 每秒采样一次

LOG_MAX_BYTES = 1024 * 1024   # 单个日志约 1 MB
LOG_BACKUP_COUNT = 3          # 保留 3 个旧日志

APP_NAME = "下载结束自动关机"
APP_VERSION = "1.3.0"
APP_ID = "download_finished_auto_shutdown"

NET_LOW_BYTES = NET_LOW_VALUE * RATE_UNIT_FACTORS[NET_UNIT]
DISK_LOW_BYTES = DISK_LOW_VALUE * RATE_UNIT_FACTORS[DISK_UNIT]

MODE_LABEL = "模拟模式" if DRY_RUN else "真实模式"
BG = "#F3F5F8"
CARD_BG = "#FFFFFF"
TEXT = "#1F2937"
MUTED = "#667085"
BORDER = "#DCE2E8"
BLUE = "#2563EB"
BLUE_ACTIVE = "#1D4ED8"
RED = "#DC2626"
RED_ACTIVE = "#B91C1C"
GRAY_BTN = "#E7EBF0"
GRAY_ACTIVE = "#D8DEE7"
DISABLED_BG = "#E9EDF2"
DISABLED_FG = "#9AA4B2"
GREEN = "#067647"
AMBER = "#B54708"

FONT_FAMILY = "Microsoft YaHei UI"
MONO_FAMILY = "Consolas"


# ---------------------------------------------------------------------------
# 依赖检查与基础工具
# ---------------------------------------------------------------------------


def _native_message(title: str, text: str, error: bool = True) -> None:
    """在 Tk 尚未启动时也能显示错误。"""
    try:
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


if sys.platform != "win32":
    raise SystemExit("本程序仅支持 Windows。")

try:
    import psutil
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except ImportError as exc:  # pragma: no cover - 仅在缺少依赖时触发
    _native_message(
        "无法启动",
        "缺少运行依赖：\n\n"
        f"{exc}\n\n"
        "请先执行：\npy -m pip install psutil pystray pillow",
    )
    raise SystemExit(1) from exc


def enable_dpi_awareness() -> None:
    """让 Tk 在高 DPI 屏幕上保持清晰。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def application_dir() -> Path:
    """程序所在目录；打包成 exe 后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_dir()
CONFIG_FILE = APP_DIR / "config.ini"
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"

LOGGER = logging.getLogger(APP_ID)

def format_number(value: float) -> str:
    """用于界面和配置文件的简洁数字格式。"""
    if not math.isfinite(value):
        return "0"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.10g}"


def format_rate_threshold(value: float, unit: str) -> str:
    return f"{format_number(value)} {unit}"


def normalize_rate_unit(raw: str) -> str:
    """把用户填写的速率单位归一化为 B/s、KB/s、MB/s 或 GB/s。"""
    text = raw.strip().lower().replace(" ", "").replace("／", "/")
    for suffix in ("/秒", "/sec", "/s", "ps"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    aliases = {
        "b": "B/s",
        "byte": "B/s",
        "bytes": "B/s",
        "字节": "B/s",
        "k": "KB/s",
        "kb": "KB/s",
        "kib": "KB/s",
        "千": "KB/s",
        "千字节": "KB/s",
        "m": "MB/s",
        "mb": "MB/s",
        "mib": "MB/s",
        "兆": "MB/s",
        "兆字节": "MB/s",
        "g": "GB/s",
        "gb": "GB/s",
        "gib": "GB/s",
        "吉": "GB/s",
        "吉字节": "GB/s",
    }
    unit = aliases.get(text)
    if unit is None:
        raise ValueError("单位无效，请填写 B/s、KB/s、MB/s 或 GB/s。")
    return unit


def render_config_text(
    net_value: float,
    net_unit: str,
    disk_value: float,
    disk_unit: str,
    cpu_low_percent: float,
    low_activity_minutes: float,
    countdown_seconds: int,
) -> str:
    return (
        "# 下载结束自动关机 - 阈值配置\n"
        "# 可以直接编辑本文件后重启程序，或在软件主界面点击“设置”修改。\n"
        "# 速率单位可填写：B/s、KB/s、MB/s、GB/s（默认 KB/s）；"
        "CPU 单位：%；持续时间单位：分钟；倒计时单位：秒。\n"
        "\n"
        "[thresholds]\n"
        "# 网络下载速度低于该值时，视为网络活动较低\n"
        f"net_low_value = {format_number(net_value)}\n"
        f"net_low_unit = {net_unit}\n"
        "\n"
        "# 磁盘写入速度低于该值时，视为磁盘活动较低\n"
        f"disk_low_value = {format_number(disk_value)}\n"
        f"disk_low_unit = {disk_unit}\n"
        "\n"
        "# CPU 占用率低于该值时，视为 CPU 活动较低（大于 0 且不超过 100）\n"
        f"cpu_low_percent = {format_number(cpu_low_percent)}\n"
        "\n"
        "# 三项同时低于阈值并持续该时长后，进入关机倒计时（支持小数，如 0.5）\n"
        f"low_activity_minutes = {format_number(low_activity_minutes)}\n"
        "\n"
        "# 触发后等待多少秒再关机，期间可以取消（范围 1~3600）\n"
        f"countdown_seconds = {countdown_seconds}\n"
    )


DEFAULT_CONFIG_TEXT = render_config_text(
    DEFAULT_NET_LOW_VALUE,
    DEFAULT_NET_UNIT,
    DEFAULT_DISK_LOW_VALUE,
    DEFAULT_DISK_UNIT,
    DEFAULT_CPU_LOW_PERCENT,
    DEFAULT_LOW_ACTIVITY_SECONDS / 60.0,
    DEFAULT_COUNTDOWN_SECONDS,
)


def setup_logging() -> Optional[Path]:
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for handler in list(LOGGER.handlers):
        LOGGER.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        LOGGER.addHandler(logging.NullHandler())
        _native_message(
            "日志不可用",
            "无法创建日志文件：\n"
            f"{LOG_FILE}\n\n"
            f"{exc}\n\n"
            "程序将继续运行，但不会记录日志。",
        )
        return None

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOGGER.addHandler(handler)
    return LOG_FILE


def create_default_config() -> None:
    CONFIG_FILE.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def save_configuration(
    net_value: float,
    net_unit: str,
    disk_value: float,
    disk_unit: str,
    cpu_low_percent: float,
    low_activity_minutes: float,
    countdown_seconds: int,
) -> None:
    """原子写入 config.ini，避免写入中断损坏旧配置。"""
    net_unit = normalize_rate_unit(net_unit)
    disk_unit = normalize_rate_unit(disk_unit)
    text = render_config_text(
        net_value,
        net_unit,
        disk_value,
        disk_unit,
        cpu_low_percent,
        low_activity_minutes,
        countdown_seconds,
    )
    temporary = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, CONFIG_FILE)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_configuration() -> list[str]:
    """读取 config.ini，校验失败的项目使用默认值。

    返回给用户看的错误/提示列表；空列表表示一切正常。
    """
    global NET_LOW_VALUE, NET_UNIT, NET_LOW_BYTES
    global DISK_LOW_VALUE, DISK_UNIT, DISK_LOW_BYTES
    global CPU_LOW_PERCENT, LOW_ACTIVITY_SECONDS, COUNTDOWN_SECONDS

    NET_LOW_VALUE = DEFAULT_NET_LOW_VALUE
    NET_UNIT = DEFAULT_NET_UNIT
    DISK_LOW_VALUE = DEFAULT_DISK_LOW_VALUE
    DISK_UNIT = DEFAULT_DISK_UNIT
    CPU_LOW_PERCENT = DEFAULT_CPU_LOW_PERCENT
    LOW_ACTIVITY_SECONDS = DEFAULT_LOW_ACTIVITY_SECONDS
    COUNTDOWN_SECONDS = DEFAULT_COUNTDOWN_SECONDS
    NET_LOW_BYTES = NET_LOW_VALUE * RATE_UNIT_FACTORS[NET_UNIT]
    DISK_LOW_BYTES = DISK_LOW_VALUE * RATE_UNIT_FACTORS[DISK_UNIT]

    issues: list[str] = []
    created = False
    if not CONFIG_FILE.exists():
        try:
            create_default_config()
            created = True
            LOGGER.info("已生成默认配置文件：%s", CONFIG_FILE)
        except OSError as exc:
            issues.append(f"无法创建配置文件 {CONFIG_FILE}：{exc}")
            LOGGER.error("无法创建配置文件：%s", exc)
            return issues

    parser = configparser.ConfigParser(
        interpolation=None,
        inline_comment_prefixes=("#", ";"),
    )
    try:
        raw = CONFIG_FILE.read_bytes()
        config_text = None
        for encoding in ("utf-8-sig", "utf-16", "gbk"):
            try:
                config_text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if config_text is None:
            raise UnicodeError("文件不是有效的 UTF-8、UTF-16 或 GBK 文本")
        parser.read_string(config_text, source=str(CONFIG_FILE))
    except (OSError, UnicodeError, configparser.Error) as exc:
        issues.append(f"无法读取配置文件 {CONFIG_FILE}：{exc}")
        LOGGER.error("无法读取配置文件：%s", exc)
        return issues

    section = "thresholds"
    if not parser.has_section(section):
        issues.append(f"配置文件缺少 [{section}] 段，全部使用默认阈值。")
        LOGGER.error("配置文件缺少 [thresholds] 段。")
        return issues

    def read_float(
        key: str,
        default: float,
        minimum: float,
        maximum: float,
        label: str,
    ) -> float:
        raw = parser.get(section, key, fallback=None)
        if raw is None or not raw.strip():
            issues.append(f"{label}（{key}）未配置，使用默认值 {default:g}。")
            return default
        try:
            value = float(raw.strip())
        except ValueError:
            issues.append(
                f"{label}（{key}）不是有效数字：{raw.strip()!r}，"
                f"使用默认值 {default:g}。"
            )
            return default
        if not math.isfinite(value) or not (minimum < value <= maximum):
            issues.append(
                f"{label}（{key}）超出范围（应大于 {minimum:g} 且不超过 "
                f"{maximum:,.0f}）：{raw.strip()!r}，使用默认值 {default:g}。"
            )
            return default
        return value

    def read_int(
        key: str,
        default: int,
        minimum: int,
        maximum: int,
        label: str,
    ) -> int:
        raw = parser.get(section, key, fallback=None)
        if raw is None or not raw.strip():
            issues.append(f"{label}（{key}）未配置，使用默认值 {default}。")
            return default
        try:
            value = int(raw.strip())
        except ValueError:
            issues.append(
                f"{label}（{key}）必须是整数：{raw.strip()!r}，"
                f"使用默认值 {default}。"
            )
            return default
        if not (minimum <= value <= maximum):
            issues.append(
                f"{label}（{key}）超出范围（{minimum}~{maximum}）："
                f"{raw.strip()!r}，使用默认值 {default}。"
            )
            return default
        return value

    def read_rate(
        prefix: str,
        default_value: float,
        default_unit: str,
        label: str,
    ) -> tuple[float, str]:
        value_key = f"{prefix}_value"
        unit_key = f"{prefix}_unit"
        old_key = f"{prefix}_kbps"
        if parser.has_option(section, value_key) or parser.has_option(
            section, unit_key
        ):
            issue_count_before = len(issues)
            value = read_float(
                value_key,
                default_value,
                0.0,
                1_000_000_000.0,
                f"{label}数值",
            )
            if len(issues) > issue_count_before:
                return default_value, default_unit
            raw_unit = parser.get(section, unit_key, fallback=None)
            if raw_unit is None or not raw_unit.strip():
                issues.append(
                    f"{label}单位（{unit_key}）未配置，使用默认单位 "
                    f"{default_unit}。"
                )
                unit = default_unit
            else:
                try:
                    unit = normalize_rate_unit(raw_unit)
                except ValueError as exc:
                    issues.append(
                        f"{label}单位（{unit_key}）无效：{raw_unit.strip()!r}，"
                        f"{exc} 使用默认单位 {default_unit}。"
                    )
                    unit = default_unit
            bytes_per_second = value * RATE_UNIT_FACTORS[unit]
            if not math.isfinite(bytes_per_second) or bytes_per_second <= 0:
                issues.append(
                    f"{label}换算后必须大于 0，使用默认值 "
                    f"{format_number(default_value)} {default_unit}。"
                )
                return default_value, default_unit
            return value, unit

        if parser.has_option(section, old_key):
            value = read_float(
                old_key,
                default_value,
                0.0,
                1_000_000_000.0,
                label,
            )
            return value, "KB/s"

        issues.append(
            f"{label}未配置（{value_key}/{unit_key}），使用默认值 "
            f"{format_number(default_value)} {default_unit}。"
        )
        return default_value, default_unit

    NET_LOW_VALUE, NET_UNIT = read_rate(
        "net_low",
        DEFAULT_NET_LOW_VALUE,
        DEFAULT_NET_UNIT,
        "网络下载阈值",
    )
    DISK_LOW_VALUE, DISK_UNIT = read_rate(
        "disk_low",
        DEFAULT_DISK_LOW_VALUE,
        DEFAULT_DISK_UNIT,
        "磁盘写入阈值",
    )
    CPU_LOW_PERCENT = read_float(
        "cpu_low_percent",
        DEFAULT_CPU_LOW_PERCENT,
        0.0,
        100.0,
        "CPU 占用阈值",
    )
    low_activity_minutes = read_float(
        "low_activity_minutes",
        DEFAULT_LOW_ACTIVITY_SECONDS / 60.0,
        0.0,
        1440.0,
        "低活动持续时间",
    )
    LOW_ACTIVITY_SECONDS = max(1, int(round(low_activity_minutes * 60.0)))
    COUNTDOWN_SECONDS = read_int(
        "countdown_seconds",
        DEFAULT_COUNTDOWN_SECONDS,
        1,
        3600,
        "关机倒计时",
    )

    NET_LOW_BYTES = NET_LOW_VALUE * RATE_UNIT_FACTORS[NET_UNIT]
    DISK_LOW_BYTES = DISK_LOW_VALUE * RATE_UNIT_FACTORS[DISK_UNIT]

    if created:
        LOGGER.info("配置文件已生成，本次使用默认阈值。")
    for issue in issues:
        LOGGER.warning("配置提示：%s", issue)
    if issues:
        LOGGER.warning("共发现 %d 个配置问题，相关项目已使用默认值。", len(issues))
    return issues


def format_rate(bytes_per_second: Optional[float]) -> str:
    if bytes_per_second is None:
        return "—"
    kbps = max(0.0, bytes_per_second) / 1024.0
    if kbps >= 1024.0:
        return f"{kbps / 1024.0:.2f} MB/s"
    if kbps >= 100.0:
        return f"{kbps:.0f} KB/s"
    return f"{kbps:.1f} KB/s"


def format_timer(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(math.ceil(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_seconds_for_log(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{seconds:.1f} 秒"


def format_duration_text(seconds: float) -> str:
    """把秒数转换成适合界面显示的简短中文时长。"""
    total = max(1, int(round(seconds)))
    minutes, remaining = divmod(total, 60)
    if remaining == 0:
        return f"{minutes} 分钟"
    if minutes == 0:
        return f"{remaining} 秒"
    return f"{minutes} 分 {remaining} 秒"


@dataclass(frozen=True)
class Metrics:
    net_bps: float
    disk_bps: float
    cpu_percent: float
    valid: bool = True
    error: str = ""

    @property
    def low(self) -> bool:
        return (
            self.valid
            and self.net_bps < NET_LOW_BYTES
            and self.disk_bps < DISK_LOW_BYTES
            and self.cpu_percent < CPU_LOW_PERCENT
        )


class State(Enum):
    IDLE = "idle"
    MONITORING = "monitoring"
    COUNTDOWN = "countdown"
    SHUTTING_DOWN = "shutting_down"


def build_icon_image(size: int = 64) -> Image.Image:
    """程序内绘制图标，避免额外资源文件。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, size // 32)
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=size // 5,
        fill="#17324D",
        outline="#2A4B6B",
        width=max(1, size // 32),
    )
    gold = "#F5C451"
    inset = size * 0.22
    draw.arc(
        (inset, inset + size * 0.02, size - inset, size - inset + size * 0.02),
        start=128,
        end=412,
        fill=gold,
        width=max(3, size // 10),
    )
    center = size / 2
    draw.rounded_rectangle(
        (
            center - size * 0.045,
            size * 0.17,
            center + size * 0.045,
            size * 0.51,
        ),
        radius=size * 0.045,
        fill=gold,
    )
    return image


def _subprocess_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def shutdown_executable() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "shutdown.exe"
    return str(candidate) if candidate.exists() else "shutdown"


# ---------------------------------------------------------------------------
# 主窗口 + 实时监控窗口 + 托盘
# ---------------------------------------------------------------------------


class AutoShutdownApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = State.IDLE
        self._tick_after_id: Optional[str] = None
        self._poll_after_id: Optional[str] = None
        self._exiting = False

        # 采样基线
        self._last_net_bytes: Optional[int] = None
        self._last_disk_bytes: Optional[int] = None
        self._last_sample_time: Optional[float] = None
        self._last_tick_time: Optional[float] = None
        self._last_metrics: Optional[Metrics] = None
        self._metric_error_last_logged = 0.0

        # 低活动 / 倒计时
        self._low_since: Optional[float] = None
        self._countdown_end: Optional[float] = None

        # 托盘
        self._event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._tray_icon: Optional[pystray.Icon] = None
        self._tray_thread: Optional[threading.Thread] = None
        self._tray_available = False
        self._tray_notice_shown = False
        self._monitor_positioned = False
        self.settings_window: Optional[tk.Toplevel] = None

        self._build_style()
        self._build_main_window()
        self._build_monitor_window()
        self._set_window_icon()
        self._start_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._on_main_close)
        self.root.report_callback_exception = self._on_tk_exception
        self._poll_after_id = self.root.after(100, self._poll_tray_events)

        self._set_status("未监控")
        self._refresh_controls()
        self._reset_realtime("未监控", color=MUTED)
        LOGGER.info("界面已就绪，等待开始监控。")

    # ---------------- 界面构建 ----------------

    def _build_style(self) -> None:
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.title(f"{APP_NAME}  [{MODE_LABEL}]")
        self._center_window(self.root, 520, 188)

    def _build_main_window(self) -> None:
        outer = tk.Frame(self.root, bg=BG, padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(master=self.root, value="未监控")
        self.status_label = tk.Label(
            outer,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 10),
            anchor="center",
            justify="center",
            wraplength=470,
            height=2,
        )
        self.status_label.pack(fill="x", pady=(0, 14))

        buttons = tk.Frame(outer, bg=BG)
        buttons.pack(fill="x")
        for column in range(4):
            buttons.grid_columnconfigure(column, weight=1, uniform="button")

        self.start_button = self._make_button(buttons, "开始监控", self._on_start_clicked)
        self.stop_button = self._make_button(buttons, "停止监控", self._on_stop_clicked)
        self.cancel_button = self._make_button(buttons, "取消关机", self._on_cancel_clicked)
        self.settings_button = self._make_button(buttons, "设置", self._open_settings)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.cancel_button.grid(row=0, column=2, sticky="ew", padx=4)
        self.settings_button.grid(row=0, column=3, sticky="ew", padx=(4, 0))

    def _make_button(
        self, parent: tk.Widget, text: str, command: object
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=(FONT_FAMILY, 10),
            relief="flat",
            bd=0,
            padx=8,
            pady=9,
            cursor="hand2",
            takefocus=True,
            highlightthickness=0,
        )

    def _build_monitor_window(self) -> None:
        self.monitor_window = tk.Toplevel(self.root)
        self.monitor_window.withdraw()
        self.monitor_window.title(f"实时监控  [{MODE_LABEL}]")
        self.monitor_window.configure(bg=BG)
        self.monitor_window.resizable(False, False)
        self.monitor_window.protocol("WM_DELETE_WINDOW", self._hide_monitor)

        outer = tk.Frame(self.monitor_window, bg=BG, padx=20, pady=16)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(
            header,
            text="实时监控",
            bg=BG,
            fg=TEXT,
            font=(FONT_FAMILY, 15, "bold"),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            header,
            text="每秒刷新 · 关闭仅隐藏",
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            anchor="e",
        ).pack(side="right")

        self.monitor_state_var = tk.StringVar(master=self.monitor_window, value="未监控")
        self.monitor_state_label = tk.Label(
            outer,
            textvariable=self.monitor_state_var,
            bg="#EEF2F6",
            fg=MUTED,
            font=(FONT_FAMILY, 10, "bold"),
            anchor="w",
            padx=12,
            pady=8,
        )
        self.monitor_state_label.pack(fill="x", pady=(0, 10))

        card = tk.Frame(
            outer,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=16,
            pady=10,
        )
        card.pack(fill="both", expand=True)
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(1, weight=0)

        self.rt_net_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_disk_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_cpu_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_low_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_elapsed_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_remaining_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_countdown_var = tk.StringVar(master=self.monitor_window, value="—")
        self.rt_net_hint_var = tk.StringVar(master=self.monitor_window, value="")
        self.rt_disk_hint_var = tk.StringVar(master=self.monitor_window, value="")
        self.rt_cpu_hint_var = tk.StringVar(master=self.monitor_window, value="")
        self.rt_low_hint_var = tk.StringVar(master=self.monitor_window, value="")
        self.rt_countdown_hint_var = tk.StringVar(master=self.monitor_window, value="")

        rows = [
            ("网络下载", self.rt_net_hint_var, self.rt_net_var),
            ("磁盘写入", self.rt_disk_hint_var, self.rt_disk_var),
            ("CPU 占用", self.rt_cpu_hint_var, self.rt_cpu_var),
        ]
        row_index = 0
        for title, hint_variable, variable in rows:
            self._add_metric_row(
                card,
                row_index,
                title,
                "",
                variable,
                mono=False,
                hint_variable=hint_variable,
            )
            row_index += 1

        self._add_separator(card, row_index)
        row_index += 1
        self.rt_low_label = self._add_metric_row(
            card,
            row_index,
            "三项均低于阈值",
            "",
            self.rt_low_var,
            mono=False,
            value_font=(FONT_FAMILY, 12, "bold"),
        )
        row_index += 1

        self._add_separator(card, row_index)
        row_index += 1
        self._add_metric_row(
            card,
            row_index,
            "低活动已持续",
            "",
            self.rt_elapsed_var,
            mono=True,
        )
        row_index += 1
        self._add_metric_row(
            card,
            row_index,
            "距离触发还需",
            "",
            self.rt_remaining_var,
            mono=True,
            hint_variable=self.rt_low_hint_var,
        )
        row_index += 1
        self.rt_countdown_label = self._add_metric_row(
            card,
            row_index,
            "关机倒计时",
            "",
            self.rt_countdown_var,
            mono=True,
            value_font=(FONT_FAMILY, 14, "bold"),
            hint_variable=self.rt_countdown_hint_var,
        )

        tk.Label(
            outer,
            text="关闭此窗口只会隐藏，后台监控不受影响。",
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="center", pady=(12, 0))

        self._refresh_threshold_hints()

    def _add_metric_row(
        self,
        parent: tk.Frame,
        row: int,
        title: str,
        hint: str,
        variable: tk.StringVar,
        *,
        mono: bool,
        value_font: Optional[tuple] = None,
        hint_variable: Optional[tk.StringVar] = None,
    ) -> tk.Label:
        name_frame = tk.Frame(parent, bg=CARD_BG)
        name_frame.grid(row=row, column=0, sticky="w", pady=8)
        tk.Label(
            name_frame,
            text=title,
            bg=CARD_BG,
            fg=TEXT,
            font=(FONT_FAMILY, 10),
        ).pack(side="left")
        if hint_variable is not None:
            tk.Label(
                name_frame,
                textvariable=hint_variable,
                bg=CARD_BG,
                fg=MUTED,
                font=(FONT_FAMILY, 8),
                padx=6,
            ).pack(side="left")
        elif hint:
            tk.Label(
                name_frame,
                text=f"  {hint}",
                bg=CARD_BG,
                fg=MUTED,
                font=(FONT_FAMILY, 8),
            ).pack(side="left")

        font = value_font or (
            (MONO_FAMILY, 14, "bold") if mono else (FONT_FAMILY, 12, "bold")
        )
        value = tk.Label(
            parent,
            textvariable=variable,
            bg=CARD_BG,
            fg=TEXT,
            font=font,
            anchor="e",
        )
        value.grid(row=row, column=1, sticky="e", pady=8, padx=(16, 0))
        return value

    def _add_separator(self, parent: tk.Frame, row: int) -> None:
        separator = tk.Frame(parent, bg="#E8EDF2", height=1)
        separator.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)

    def _refresh_threshold_hints(self) -> None:
        if not hasattr(self, "rt_net_hint_var"):
            return
        self.rt_net_hint_var.set(
            f"低于 {format_rate_threshold(NET_LOW_VALUE, NET_UNIT)}"
        )
        self.rt_disk_hint_var.set(
            f"低于 {format_rate_threshold(DISK_LOW_VALUE, DISK_UNIT)}"
        )
        self.rt_cpu_hint_var.set(f"低于 {format_number(CPU_LOW_PERCENT)}%")
        self.rt_low_hint_var.set(
            f"满 {format_duration_text(LOW_ACTIVITY_SECONDS)}触发"
        )
        self.rt_countdown_hint_var.set(f"触发后 {COUNTDOWN_SECONDS} 秒")

    # ---------------- 设置窗口 ----------------

    def _open_settings(self) -> None:
        if self._exiting or self.state in (State.COUNTDOWN, State.SHUTTING_DOWN):
            return
        if self.settings_window is None or not self.settings_window.winfo_exists():
            self._build_settings_window()
        self._load_settings_form()
        self._set_settings_error("")
        window = self.settings_window
        window.deiconify()
        window.lift()
        window.focus_force()
        self._settings_net_entry.focus_set()
        window.after(20, self._grab_settings)

    def _grab_settings(self) -> None:
        window = self.settings_window
        if window is None or self._exiting:
            return
        try:
            window.grab_set()
        except tk.TclError:
            pass

    def _build_settings_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.title(f"设置阈值  [{MODE_LABEL}]")
        window.configure(bg=BG)
        window.resizable(False, False)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_settings)
        window.bind("<Escape>", lambda event: self._close_settings())
        self.settings_window = window

        outer = tk.Frame(window, bg=BG, padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="设置阈值",
            bg=BG,
            fg=TEXT,
            font=(FONT_FAMILY, 15, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            outer,
            text="修改后点击“保存并应用”立即生效，同时写入 config.ini。",
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            anchor="w",
        ).pack(fill="x", pady=(3, 12))

        card = tk.Frame(
            outer,
            bg=CARD_BG,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=18,
            pady=12,
        )
        card.pack(fill="x")
        card.grid_columnconfigure(1, weight=1)

        self.settings_net_value_var = tk.StringVar(master=window)
        self.settings_net_unit_var = tk.StringVar(master=window)
        self.settings_disk_value_var = tk.StringVar(master=window)
        self.settings_disk_unit_var = tk.StringVar(master=window)
        self.settings_cpu_var = tk.StringVar(master=window)
        self.settings_low_minutes_var = tk.StringVar(master=window)
        self.settings_countdown_var = tk.StringVar(master=window)
        self.settings_error_var = tk.StringVar(master=window)

        style = ttk.Style(window)
        try:
            style.configure("Settings.TCombobox", padding=5)
        except tk.TclError:
            pass

        def entry(variable: tk.StringVar) -> tk.Entry:
            widget = tk.Entry(
                card,
                textvariable=variable,
                font=(FONT_FAMILY, 11),
                bg="white",
                fg=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=BLUE,
                insertbackground=TEXT,
            )
            return widget

        def unit_combo(variable: tk.StringVar) -> ttk.Combobox:
            return ttk.Combobox(
                card,
                textvariable=variable,
                values=RATE_UNIT_CHOICES,
                state="normal",
                width=8,
                style="Settings.TCombobox",
            )

        rows = [
            ("网络下载低于", entry(self.settings_net_value_var), unit_combo(self.settings_net_unit_var)),
            ("磁盘写入低于", entry(self.settings_disk_value_var), unit_combo(self.settings_disk_unit_var)),
            ("CPU 占用低于", entry(self.settings_cpu_var), tk.Label(card, text="%", bg=CARD_BG, fg=MUTED, font=(FONT_FAMILY, 10))),
            ("低活动持续", entry(self.settings_low_minutes_var), tk.Label(card, text="分钟", bg=CARD_BG, fg=MUTED, font=(FONT_FAMILY, 10))),
            ("关机倒计时", entry(self.settings_countdown_var), tk.Label(card, text="秒", bg=CARD_BG, fg=MUTED, font=(FONT_FAMILY, 10))),
        ]
        for row_index, (label, field, unit_widget) in enumerate(rows):
            tk.Label(
                card,
                text=label,
                bg=CARD_BG,
                fg=TEXT,
                font=(FONT_FAMILY, 10),
                anchor="w",
            ).grid(row=row_index, column=0, sticky="w", pady=9, padx=(0, 12))
            field.grid(row=row_index, column=1, sticky="ew", pady=9, ipady=5)
            unit_widget.grid(row=row_index, column=2, sticky="w", padx=(12, 0))

        self._settings_net_entry = rows[0][1]

        tk.Label(
            outer,
            text=(
                "速率单位可直接填写：B/s、KB/s、MB/s、GB/s（默认 KB/s）。"
                "填写 KB、MB 等也会按每秒速率理解。"
            ),
            bg=BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            justify="left",
            wraplength=520,
            anchor="w",
        ).pack(fill="x", pady=(12, 4))

        self.settings_error_label = tk.Label(
            outer,
            textvariable=self.settings_error_var,
            bg=BG,
            fg=RED,
            font=(FONT_FAMILY, 9),
            justify="left",
            wraplength=520,
            anchor="w",
            height=2,
        )
        self.settings_error_label.pack(fill="x")

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(8, 0))
        actions.grid_columnconfigure(0, weight=1, uniform="settings_action")
        actions.grid_columnconfigure(1, weight=1, uniform="settings_action")
        save_button = self._make_button(
            actions, "保存并应用", self._save_settings_from_form
        )
        cancel_button = self._make_button(actions, "取消", self._close_settings)
        self._style_button(save_button, "blue", True)
        self._style_button(cancel_button, "gray", True)
        save_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        cancel_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self._load_settings_form()
        window.update_idletasks()
        height = max(520, window.winfo_reqheight() + 8)
        self._center_settings_window(570, height)

    def _center_settings_window(self, width: int, height: int) -> None:
        window = self.settings_window
        if window is None:
            return
        window.update_idletasks()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        self.root.update_idletasks()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_width = self.root.winfo_width()
        root_height = self.root.winfo_height()
        x = root_x + (root_width - width) // 2
        y = root_y + (root_height - height) // 2
        x = min(max(16, x), max(16, screen_width - width - 16))
        y = min(max(16, y), max(16, screen_height - height - 56))
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _load_settings_form(self) -> None:
        if not hasattr(self, "settings_net_value_var"):
            return
        self.settings_net_value_var.set(format_number(NET_LOW_VALUE))
        self.settings_net_unit_var.set(NET_UNIT)
        self.settings_disk_value_var.set(format_number(DISK_LOW_VALUE))
        self.settings_disk_unit_var.set(DISK_UNIT)
        self.settings_cpu_var.set(format_number(CPU_LOW_PERCENT))
        self.settings_low_minutes_var.set(
            format_number(LOW_ACTIVITY_SECONDS / 60.0)
        )
        self.settings_countdown_var.set(str(COUNTDOWN_SECONDS))

    def _set_settings_error(self, message: str) -> None:
        if hasattr(self, "settings_error_var"):
            self.settings_error_var.set(message)

    def _close_settings(self) -> None:
        window = self.settings_window
        if window is None:
            return
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.withdraw()
        except tk.TclError:
            pass
        if hasattr(self, "settings_error_var"):
            self.settings_error_var.set("")

    @staticmethod
    def _parse_settings_number(
        raw: str,
        label: str,
        minimum: float,
        maximum: float,
    ) -> float:
        text = raw.strip().replace(",", ".")
        if not text:
            raise ValueError(f"{label}不能为空。")
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"{label}必须是数字。") from exc
        if not math.isfinite(value) or not (minimum < value <= maximum):
            raise ValueError(
                f"{label}必须大于 {format_number(minimum)} 且不超过 "
                f"{format_number(maximum)}。"
            )
        return value

    @staticmethod
    def _parse_countdown(raw: str) -> int:
        text = raw.strip()
        if not text:
            raise ValueError("关机倒计时不能为空。")
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError("关机倒计时必须是整数秒。") from exc
        if not 1 <= value <= 3600:
            raise ValueError("关机倒计时必须在 1~3600 秒之间。")
        return value

    def _save_settings_from_form(self) -> None:
        if self.settings_window is None:
            return
        if self.state in (State.COUNTDOWN, State.SHUTTING_DOWN):
            self._set_settings_error("关机倒计时期间不能修改设置。")
            return

        try:
            net_value = self._parse_settings_number(
                self.settings_net_value_var.get(),
                "网络下载阈值",
                0.0,
                1_000_000_000.0,
            )
            net_unit = normalize_rate_unit(self.settings_net_unit_var.get())
            disk_value = self._parse_settings_number(
                self.settings_disk_value_var.get(),
                "磁盘写入阈值",
                0.0,
                1_000_000_000.0,
            )
            disk_unit = normalize_rate_unit(self.settings_disk_unit_var.get())
            cpu_low = self._parse_settings_number(
                self.settings_cpu_var.get(),
                "CPU 占用阈值",
                0.0,
                100.0,
            )
            low_minutes = self._parse_settings_number(
                self.settings_low_minutes_var.get(),
                "低活动持续时间",
                0.0,
                1440.0,
            )
            countdown = self._parse_countdown(
                self.settings_countdown_var.get()
            )
        except ValueError as exc:
            self._set_settings_error(str(exc))
            return

        low_seconds = max(1, int(round(low_minutes * 60.0)))
        try:
            save_configuration(
                net_value,
                net_unit,
                disk_value,
                disk_unit,
                cpu_low,
                low_minutes,
                countdown,
            )
        except Exception as exc:
            LOGGER.exception("保存阈值配置失败")
            self._set_settings_error(f"保存 config.ini 失败：{exc}")
            return

        self._apply_new_settings(
            net_value,
            net_unit,
            disk_value,
            disk_unit,
            cpu_low,
            low_seconds,
            countdown,
        )
        LOGGER.info(
            "设置已保存并应用：网络 < %s，磁盘写入 < %s，CPU < %s%%，"
            "持续 %d 秒，倒计时 %d 秒。",
            format_rate_threshold(net_value, net_unit),
            format_rate_threshold(disk_value, disk_unit),
            format_number(cpu_low),
            low_seconds,
            countdown,
        )
        self._close_settings()
        self._notify("阈值已保存并立即生效。", "设置已保存")

    def _apply_new_settings(
        self,
        net_value: float,
        net_unit: str,
        disk_value: float,
        disk_unit: str,
        cpu_low: float,
        low_seconds: int,
        countdown: int,
    ) -> None:
        global NET_LOW_VALUE, NET_UNIT, NET_LOW_BYTES
        global DISK_LOW_VALUE, DISK_UNIT, DISK_LOW_BYTES
        global CPU_LOW_PERCENT, LOW_ACTIVITY_SECONDS, COUNTDOWN_SECONDS

        NET_LOW_VALUE = net_value
        NET_UNIT = net_unit
        DISK_LOW_VALUE = disk_value
        DISK_UNIT = disk_unit
        CPU_LOW_PERCENT = cpu_low
        LOW_ACTIVITY_SECONDS = low_seconds
        COUNTDOWN_SECONDS = countdown
        NET_LOW_BYTES = NET_LOW_VALUE * RATE_UNIT_FACTORS[NET_UNIT]
        DISK_LOW_BYTES = DISK_LOW_VALUE * RATE_UNIT_FACTORS[DISK_UNIT]

        if self._low_since is not None:
            LOGGER.info("离开低活动：阈值设置已更新，低活动计时重置。")
            self._low_since = None
        self._refresh_threshold_hints()
        if self.state is State.IDLE:
            self._set_status("设置已保存")
        elif self.state is State.MONITORING:
            self._set_status("设置已保存，低活动计时已重置", GREEN)
        self._refresh_controls()

    def _set_window_icon(self) -> None:
        try:
            self._window_icon = ImageTk.PhotoImage(build_icon_image(64))
            self.root.iconphoto(True, self._window_icon)
        except Exception:
            LOGGER.exception("设置窗口图标失败")

    # ---------------- 托盘 ----------------

    def _start_tray(self) -> None:
        try:
            menu = pystray.Menu(
                pystray.MenuItem(
                    "显示主窗口",
                    self._tray_show_main,
                    default=True,
                ),
                pystray.MenuItem(
                    "查看实时监控",
                    self._tray_show_monitor,
                    enabled=lambda item: self.state in (
                        State.MONITORING,
                        State.COUNTDOWN,
                        State.SHUTTING_DOWN,
                    ),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "开始监控",
                    self._tray_start,
                    enabled=lambda item: self.state is State.IDLE,
                ),
                pystray.MenuItem(
                    "停止监控",
                    self._tray_stop,
                    enabled=lambda item: self.state is State.MONITORING,
                ),
                pystray.MenuItem(
                    "取消关机",
                    self._tray_cancel,
                    enabled=lambda item: self.state is State.COUNTDOWN,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("打开配置文件", self._tray_open_config),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._tray_exit),
            )
            icon = pystray.Icon(
                APP_ID,
                build_icon_image(64),
                f"{APP_NAME} - 未监控",
                menu,
            )
            self._tray_icon = icon
            self._tray_thread = threading.Thread(
                target=self._run_tray,
                args=(icon,),
                name="tray-icon",
                daemon=True,
            )
            self._tray_available = True
            self._tray_thread.start()
            LOGGER.info("系统托盘已启动。")
        except Exception:
            self._tray_available = False
            LOGGER.exception("系统托盘启动失败")
            self.root.after(
                0,
                lambda: messagebox.showwarning(
                    "托盘不可用",
                    "系统托盘启动失败，关闭主窗口将退出程序。\n"
                    "详细信息请查看日志。",
                    parent=self.root,
                ),
            )

    def _run_tray(self, icon: pystray.Icon) -> None:
        try:
            icon.run()
        except Exception as exc:
            LOGGER.exception("系统托盘运行失败")
            self._event_queue.put(("tray_error", str(exc)))

    def _tray_show_main(self, icon: object, item: object) -> None:
        self._event_queue.put(("show_main", None))

    def _tray_show_monitor(self, icon: object, item: object) -> None:
        self._event_queue.put(("show_monitor", None))

    def _tray_start(self, icon: object, item: object) -> None:
        self._event_queue.put(("start", None))

    def _tray_stop(self, icon: object, item: object) -> None:
        self._event_queue.put(("stop", None))

    def _tray_cancel(self, icon: object, item: object) -> None:
        self._event_queue.put(("cancel", None))

    def _tray_open_config(self, icon: object, item: object) -> None:
        self._event_queue.put(("open_config", None))

    def _tray_exit(self, icon: object, item: object) -> None:
        self._event_queue.put(("exit", None))

    def _poll_tray_events(self) -> None:
        if self._exiting:
            return
        try:
            while not self._exiting:
                action, payload = self._event_queue.get_nowait()
                self._handle_tray_action(action, payload)
        except queue.Empty:
            pass
        if self._exiting:
            return
        try:
            if self.root.winfo_exists():
                self._poll_after_id = self.root.after(100, self._poll_tray_events)
        except tk.TclError:
            pass

    def _handle_tray_action(self, action: str, payload: object) -> None:
        if action == "show_main":
            self._show_main_window()
        elif action == "show_monitor":
            self._on_show_monitor()
        elif action == "start":
            self._start_monitoring()
        elif action == "stop":
            self._stop_monitoring()
        elif action == "cancel":
            self._cancel_countdown()
        elif action == "open_config":
            self._open_config_file()
        elif action == "exit":
            self._exit_app()
        elif action == "tray_error":
            self._tray_available = False
            LOGGER.error("托盘线程异常：%s", payload)
            messagebox.showwarning(
                "托盘不可用",
                "系统托盘出现异常，关闭主窗口将退出程序。\n"
                "详细信息请查看日志。",
                parent=self.root,
            )

    def _update_tray(self, *, refresh_menu: bool = True) -> None:
        icon = self._tray_icon
        if icon is None:
            return
        if self.state is State.IDLE:
            suffix = "未监控"
        elif self.state is State.MONITORING:
            suffix = "监控中"
        elif self.state is State.COUNTDOWN:
            remaining = self._countdown_remaining()
            suffix = f"关机倒计时 {format_timer(remaining)}"
        else:
            suffix = "等待系统关机"
        try:
            icon.title = f"{APP_NAME} - {suffix}"
            if refresh_menu:
                icon.update_menu()
        except Exception:
            pass

    def _notify(self, message: str, title: str = APP_NAME) -> None:
        if not self._tray_available or self._tray_icon is None:
            return
        try:
            self._tray_icon.notify(message, title)
        except Exception:
            LOGGER.debug("托盘通知发送失败", exc_info=True)

    def _open_config_file(self) -> None:
        try:
            if not CONFIG_FILE.exists():
                create_default_config()
            os.startfile(str(CONFIG_FILE))
        except OSError:
            LOGGER.exception("打开配置文件失败：%s", CONFIG_FILE)
            self._show_main_window()
            messagebox.showerror(
                "无法打开配置文件",
                f"无法打开：\n{CONFIG_FILE}\n\n请手动用记事本打开该文件。",
                parent=self.root,
            )
            return
        LOGGER.info("已打开配置文件：%s", CONFIG_FILE)
        self._notify("修改阈值后，请退出并重新启动程序。", "阈值配置")

    # ---------------- 控制逻辑 ----------------

    def _on_start_clicked(self) -> None:
        if self.state is State.IDLE:
            self._start_monitoring()
        else:
            self._show_monitor_window()

    def _on_stop_clicked(self) -> None:
        self._stop_monitoring()

    def _on_cancel_clicked(self) -> None:
        self._cancel_countdown()

    def _on_show_monitor(self) -> None:
        if self.state is State.IDLE:
            self._show_main_window()
            return
        self._show_monitor_window()

    def _start_monitoring(self) -> None:
        if self.state is not State.IDLE or self._exiting:
            if self.state is not State.IDLE:
                self._show_monitor_window()
            return

        self.state = State.MONITORING
        self._low_since = None
        self._countdown_end = None
        self._last_metrics = None
        self._last_tick_time = time.monotonic()
        self._prime_sampler()

        LOGGER.info(
            "开始监控：网络 < %s，磁盘写入 < %s，CPU < %g%%，"
            "三项同时满足并持续 %d 秒后触发关机倒计时。",
            format_rate_threshold(NET_LOW_VALUE, NET_UNIT),
            format_rate_threshold(DISK_LOW_VALUE, DISK_UNIT),
            CPU_LOW_PERCENT,
            LOW_ACTIVITY_SECONDS,
        )
        self._set_status("监控中：等待下载/安装结束…")
        self._reset_realtime("监控中：等待低活动", color=MUTED)
        self._refresh_controls()
        self._schedule_tick()
        self._show_monitor_window()

    def _stop_monitoring(self) -> None:
        if self.state is not State.MONITORING:
            return

        if self._low_since is not None:
            LOGGER.info(
                "离开低活动：停止监控，低活动已持续 %s。",
                format_seconds_for_log(time.monotonic() - self._low_since),
            )
        LOGGER.info("停止监控。")
        self._enter_idle("已停止监控")

    def _cancel_countdown(self) -> None:
        if self.state is not State.COUNTDOWN or self._exiting:
            return

        if not DRY_RUN:
            result = self._run_shutdown_command(["/a"])
            if result is None or result.returncode != 0:
                detail = ""
                if result is not None:
                    detail = (result.stderr or result.stdout or "").strip()
                    detail = f"（返回码 {result.returncode}）{detail}"
                LOGGER.error("取消关机失败：shutdown /a 未成功%s", detail)
                self._set_status("取消关机失败，倒计时仍在继续，请重试。")
                self._show_main_window()
                self._notify("取消关机失败，请重试。", "取消关机失败")
                return
            LOGGER.info("取消关机：已执行 shutdown /a。")
        else:
            LOGGER.info("取消关机：模拟模式，未执行 shutdown /a。")

        if self._low_since is not None:
            LOGGER.info(
                "离开低活动：用户取消关机，低活动已持续 %s。",
                format_seconds_for_log(time.monotonic() - self._low_since),
            )
        LOGGER.info("停止监控：取消关机后回到未监控状态，不自动恢复监控。")
        self._enter_idle("已取消关机，未监控")
        self._show_main_window()
        self._notify("已取消关机，监控已停止。", "已取消关机")

    def _enter_idle(self, status: str, final_log: Optional[str] = None) -> None:
        self._cancel_tick()
        self.state = State.IDLE
        self._low_since = None
        self._countdown_end = None
        self._last_metrics = None
        self._reset_sampler()
        self._set_status(status)
        self._reset_realtime(status, color=MUTED)
        self._refresh_controls()
        if final_log:
            LOGGER.info(final_log)

    def _prime_sampler(self) -> None:
        try:
            psutil.cpu_percent(interval=None)
            net = psutil.net_io_counters()
            disk = psutil.disk_io_counters()
            if net is None or disk is None:
                raise RuntimeError("系统不提供网络或磁盘计数器")
            self._last_net_bytes = int(net.bytes_recv)
            self._last_disk_bytes = int(disk.write_bytes)
            self._last_sample_time = time.monotonic()
        except Exception:
            self._reset_sampler()
            LOGGER.exception("初始化性能计数器失败")

    def _reset_sampler(self) -> None:
        self._last_net_bytes = None
        self._last_disk_bytes = None
        self._last_sample_time = None
        self._last_tick_time = None

    def _read_metrics(self) -> Metrics:
        now = time.monotonic()
        if (
            self._last_net_bytes is None
            or self._last_disk_bytes is None
            or self._last_sample_time is None
        ):
            self._prime_sampler()
            raise RuntimeError("性能计数器正在初始化")

        net = psutil.net_io_counters()
        disk = psutil.disk_io_counters()
        if net is None or disk is None:
            raise RuntimeError("无法读取网络或磁盘计数器")

        elapsed = now - self._last_sample_time
        if elapsed < 0.2:
            raise RuntimeError("采样间隔过短")
        if elapsed > 15.0:
            # 系统休眠、界面阻塞等情况下不把中断时间算作低活动。
            self._last_net_bytes = int(net.bytes_recv)
            self._last_disk_bytes = int(disk.write_bytes)
            self._last_sample_time = now
            raise RuntimeError(f"采样中断 {elapsed:.1f} 秒，计时已重置")

        net_delta = max(0, int(net.bytes_recv) - self._last_net_bytes)
        disk_delta = max(0, int(disk.write_bytes) - self._last_disk_bytes)
        cpu = float(psutil.cpu_percent(interval=None))

        self._last_net_bytes = int(net.bytes_recv)
        self._last_disk_bytes = int(disk.write_bytes)
        self._last_sample_time = now

        return Metrics(
            net_bps=net_delta / elapsed,
            disk_bps=disk_delta / elapsed,
            cpu_percent=cpu,
        )

    def _schedule_tick(self) -> None:
        self._cancel_tick()
        self._tick_after_id = self.root.after(SAMPLE_INTERVAL_MS, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_after_id is not None:
            try:
                self.root.after_cancel(self._tick_after_id)
            except Exception:
                pass
            self._tick_after_id = None

    def _tick(self) -> None:
        self._tick_after_id = None
        if self.state not in (
            State.MONITORING,
            State.COUNTDOWN,
            State.SHUTTING_DOWN,
        ):
            return
        if self._exiting:
            return

        now = time.monotonic()
        gap = 0.0
        if self._last_tick_time is not None:
            gap = now - self._last_tick_time
        self._last_tick_time = now

        # 休眠、卡顿等造成的长间隔不能算作“持续低活动”。
        if (
            self.state is State.MONITORING
            and self._low_since is not None
            and gap > max(5.0, SAMPLE_INTERVAL_MS / 1000.0 * 3)
        ):
            LOGGER.info(
                "离开低活动：监控采样中断约 %.1f 秒，低活动计时已重置。",
                gap,
            )
            self._low_since = None
            self._reset_sampler()

        try:
            metrics = self._read_metrics()
            self._last_metrics = metrics
            self._metric_error_last_logged = 0.0
        except Exception as exc:
            metrics = Metrics(0.0, 0.0, 0.0, valid=False, error=str(exc))
            self._last_metrics = metrics
            if now - self._metric_error_last_logged >= 60.0:
                self._metric_error_last_logged = now
                LOGGER.warning("性能数据采集异常：%s", exc)

        if self.state is State.MONITORING:
            self._process_low_activity(metrics, now)
        elif self.state is State.COUNTDOWN:
            self._update_low_tracking_in_countdown(metrics, now)
            if self._countdown_remaining() <= 0:
                self._on_countdown_finished()
                return

        self._update_realtime(metrics, now)
        self._update_main_status(metrics, now)
        self._schedule_tick()

    def _process_low_activity(self, metrics: Metrics, now: float) -> None:
        if metrics.low:
            if self._low_since is None:
                self._low_since = now
                LOGGER.info(
                    "进入低活动：网络 %s，磁盘写入 %s，CPU %.1f%%。",
                    format_rate(metrics.net_bps),
                    format_rate(metrics.disk_bps),
                    metrics.cpu_percent,
                )
            elapsed = now - self._low_since
            if elapsed >= LOW_ACTIVITY_SECONDS:
                self._begin_countdown(elapsed)
        else:
            if self._low_since is not None:
                LOGGER.info(
                    "离开低活动：网络 %s，磁盘写入 %s，CPU %.1f%%，"
                    "低活动已持续 %s。",
                    format_rate(metrics.net_bps),
                    format_rate(metrics.disk_bps),
                    metrics.cpu_percent,
                    format_seconds_for_log(now - self._low_since),
                )
                self._low_since = None

    def _update_low_tracking_in_countdown(
        self, metrics: Metrics, now: float
    ) -> None:
        """倒计时期间继续展示低活动时长，但不再影响已启动的倒计时。"""
        if metrics.low:
            if self._low_since is None:
                self._low_since = now
                LOGGER.info("进入低活动（关机倒计时期间）。")
        elif self._low_since is not None:
            LOGGER.info(
                "离开低活动（关机倒计时期间）：低活动已持续 %s。",
                format_seconds_for_log(now - self._low_since),
            )
            self._low_since = None

    def _begin_countdown(self, low_duration: float) -> None:
        if self.state is not State.MONITORING:
            return

        self._close_settings()
        self.state = State.COUNTDOWN
        self._countdown_end = time.monotonic() + COUNTDOWN_SECONDS
        self._show_main_window()
        self._set_status(f"关机倒计时：{COUNTDOWN_SECONDS} 秒，可取消")
        self._refresh_controls()

        if DRY_RUN:
            LOGGER.warning(
                "触发关机：低活动持续 %s，进入 %d 秒倒计时"
                "（模拟模式，不执行 shutdown /s /t %d）。",
                format_seconds_for_log(low_duration),
                COUNTDOWN_SECONDS,
                COUNTDOWN_SECONDS,
            )
            self._notify(
                f"低活动已持续 {format_duration_text(LOW_ACTIVITY_SECONDS)}。"
                f"模拟关机倒计时 {COUNTDOWN_SECONDS} 秒，不会真正关机。",
                "模拟关机倒计时",
            )
            return

        LOGGER.warning(
            "触发关机：低活动持续 %s，执行 shutdown /s /t %d。",
            format_seconds_for_log(low_duration),
            COUNTDOWN_SECONDS,
        )
        result = self._run_shutdown_command(["/s", "/t", str(COUNTDOWN_SECONDS)])
        if result is None or result.returncode != 0:
            detail = ""
            if result is not None:
                detail = (result.stderr or result.stdout or "").strip()
                detail = f"（返回码 {result.returncode}）{detail}"
            LOGGER.error("触发关机失败：shutdown /s /t %d 未成功%s", COUNTDOWN_SECONDS, detail)
            self.state = State.MONITORING
            self._countdown_end = None
            self._low_since = None
            self._set_status("关机命令执行失败，已继续监控。")
            self._refresh_controls()
            self._notify("执行关机命令失败，请查看日志。", "自动关机失败")
            return

        self._countdown_end = time.monotonic() + COUNTDOWN_SECONDS
        self._refresh_controls()
        self._notify(
            f"低活动已持续 {format_duration_text(LOW_ACTIVITY_SECONDS)}。"
            f"将在 {COUNTDOWN_SECONDS} 秒后关机，可取消。",
            "自动关机倒计时",
        )

    def _countdown_remaining(self) -> float:
        if self._countdown_end is None:
            return 0.0
        return max(0.0, self._countdown_end - time.monotonic())

    def _on_countdown_finished(self) -> None:
        if self.state is not State.COUNTDOWN:
            return
        if DRY_RUN:
            LOGGER.warning("模拟关机倒计时结束：未执行 shutdown，程序回到未监控状态。")
            self._enter_idle(
                "模拟关机完成（未实际关机）",
                final_log="停止监控：模拟倒计时结束。",
            )
            self._show_main_window()
            self._notify("模拟倒计时结束，未实际关机。", "模拟关机完成")
        else:
            LOGGER.warning("关机倒计时结束：等待系统执行关机。")
            self.state = State.SHUTTING_DOWN
            self._countdown_end = None
            self._set_status("关机时间已到，等待系统关机…")
            self._refresh_controls()
            self._update_realtime(
                self._last_metrics
                or Metrics(0.0, 0.0, 0.0, valid=False, error="无数据"),
                time.monotonic(),
            )
            self._update_tray()

    def _run_shutdown_command(
        self, arguments: list[str]
    ) -> Optional[subprocess.CompletedProcess[str]]:
        try:
            return subprocess.run(
                [shutdown_executable(), *arguments],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
                creationflags=_subprocess_flags(),
                check=False,
            )
        except Exception:
            LOGGER.exception("调用 shutdown 命令失败：%s", arguments)
            return None

    # ---------------- 状态显示 ----------------

    def _refresh_controls(self) -> None:
        if self.state is State.IDLE:
            self.start_button.configure(text="开始监控")
            self._style_button(self.start_button, "blue", True)
            self._style_button(self.stop_button, "gray", False)
            self._style_button(self.cancel_button, "red", False)
        elif self.state is State.MONITORING:
            self.start_button.configure(text="查看监控")
            self._style_button(self.start_button, "blue", True)
            self._style_button(self.stop_button, "gray", True)
            self._style_button(self.cancel_button, "red", False)
        elif self.state is State.COUNTDOWN:
            self.start_button.configure(text="查看监控")
            self._style_button(self.start_button, "blue", True)
            self._style_button(self.stop_button, "gray", False)
            self._style_button(self.cancel_button, "red", True)
        else:
            self.start_button.configure(text="查看监控")
            self._style_button(self.start_button, "blue", True)
            self._style_button(self.stop_button, "gray", False)
            self._style_button(self.cancel_button, "red", False)
        self._style_button(
            self.settings_button,
            "gray",
            self.state in (State.IDLE, State.MONITORING),
        )
        self._update_tray()

    def _style_button(self, button: tk.Button, kind: str, enabled: bool) -> None:
        if not enabled:
            button.configure(
                state="disabled",
                bg=DISABLED_BG,
                fg=DISABLED_FG,
                activebackground=DISABLED_BG,
                activeforeground=DISABLED_FG,
                disabledforeground=DISABLED_FG,
                cursor="arrow",
            )
            return
        if kind == "blue":
            background, foreground, active = BLUE, "#FFFFFF", BLUE_ACTIVE
        elif kind == "red":
            background, foreground, active = RED, "#FFFFFF", RED_ACTIVE
        else:
            background, foreground, active = GRAY_BTN, TEXT, GRAY_ACTIVE
        button.configure(
            state="normal",
            bg=background,
            fg=foreground,
            activebackground=active,
            activeforeground=foreground,
            disabledforeground=foreground,
            cursor="hand2",
        )

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _update_main_status(self, metrics: Metrics, now: float) -> None:
        if self.state is State.MONITORING:
            if not metrics.valid:
                self._set_status("监控中：数据采集异常，低活动计时暂停", AMBER)
            elif self._low_since is not None:
                self._set_status("监控中：低活动计时中…", GREEN)
            else:
                self._set_status("监控中：等待下载/安装结束…", MUTED)
        elif self.state is State.COUNTDOWN:
            remaining = int(math.ceil(self._countdown_remaining()))
            if DRY_RUN:
                self._set_status(f"模拟关机倒计时：{remaining} 秒，可取消", RED)
            else:
                self._set_status(f"关机倒计时：{remaining} 秒，可取消", RED)
            self._update_tray(refresh_menu=False)
        elif self.state is State.SHUTTING_DOWN:
            self._set_status("关机时间已到，等待系统关机…", RED)

    def _reset_realtime(self, status: str, *, color: str = MUTED) -> None:
        if not hasattr(self, "rt_net_var"):
            return
        for variable in (
            self.rt_net_var,
            self.rt_disk_var,
            self.rt_cpu_var,
            self.rt_low_var,
            self.rt_elapsed_var,
            self.rt_remaining_var,
            self.rt_countdown_var,
        ):
            variable.set("—")
        self.monitor_state_var.set(status)
        self._set_monitor_banner(color)
        self.rt_low_label.configure(fg=TEXT)
        self.rt_countdown_label.configure(fg=TEXT)

    def _set_monitor_banner(self, color: str) -> None:
        backgrounds = {
            GREEN: "#E9F7EF",
            AMBER: "#FFF4E5",
            RED: "#FDECEC",
            MUTED: "#EEF2F6",
        }
        self.monitor_state_label.configure(
            bg=backgrounds.get(color, "#EEF2F6"),
            fg=color,
        )

    def _update_realtime(self, metrics: Metrics, now: float) -> None:
        if metrics.valid:
            self.rt_net_var.set(format_rate(metrics.net_bps))
            self.rt_disk_var.set(format_rate(metrics.disk_bps))
            self.rt_cpu_var.set(f"{metrics.cpu_percent:.1f}%")
        else:
            self.rt_net_var.set("—")
            self.rt_disk_var.set("—")
            self.rt_cpu_var.set("—")

        if not metrics.valid:
            self.rt_low_var.set("数据采集异常")
            self.rt_low_label.configure(fg=RED)
            self._set_monitor_banner(AMBER)
            self.monitor_state_var.set("数据采集异常，低活动计时暂停")
        elif metrics.low:
            self.rt_low_var.set("是")
            self.rt_low_label.configure(fg=GREEN)
            if self.state is State.MONITORING:
                self.monitor_state_var.set("低活动计时中")
                self._set_monitor_banner(GREEN)
        else:
            self.rt_low_var.set("否")
            self.rt_low_label.configure(fg=AMBER)
            if self.state is State.MONITORING:
                self.monitor_state_var.set("监控中：等待低活动")
                self._set_monitor_banner(MUTED)

        if self._low_since is None:
            self.rt_elapsed_var.set("—")
        else:
            self.rt_elapsed_var.set(format_timer(now - self._low_since))

        if self.state is State.MONITORING:
            if self._low_since is None:
                self.rt_remaining_var.set("—")
            else:
                self.rt_remaining_var.set(
                    format_timer(LOW_ACTIVITY_SECONDS - (now - self._low_since))
                )
            self.rt_countdown_var.set("—")
            self.rt_countdown_label.configure(fg=TEXT)
        elif self.state is State.COUNTDOWN:
            self.rt_remaining_var.set("已触发")
            remaining = self._countdown_remaining()
            self.rt_countdown_var.set(format_timer(remaining))
            self.rt_countdown_label.configure(fg=RED)
            if DRY_RUN:
                self.monitor_state_var.set("模拟关机倒计时（不会真正关机）")
            else:
                self.monitor_state_var.set("自动关机倒计时，可取消")
            self._set_monitor_banner(RED)
        elif self.state is State.SHUTTING_DOWN:
            self.rt_remaining_var.set("已触发")
            self.rt_countdown_var.set("00:00")
            self.rt_countdown_label.configure(fg=RED)
            self.monitor_state_var.set("关机指令已执行，等待系统关机")
            self._set_monitor_banner(RED)
        else:
            self.rt_remaining_var.set("—")
            self.rt_countdown_var.set("—")
            self.rt_countdown_label.configure(fg=TEXT)

    # ---------------- 窗口显隐与退出 ----------------

    def _center_window(self, window: tk.Misc, width: int, height: int) -> None:
        window.update_idletasks()
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 24)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _position_monitor_window(self) -> None:
        """优先把实时监控窗口放在主窗口右侧，尽量不遮挡主窗口。"""
        width, height = 500, 500
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_width = self.root.winfo_width()
        root_height = self.root.winfo_height()
        gap = 24

        x = root_x + root_width + gap
        y = root_y + (root_height - height) // 2
        if x + width > screen_width - 16:
            x = root_x - width - gap
        if x < 16:
            x = max(16, (screen_width - width) // 2)
            y = root_y - height - 16
            if y < 16:
                y = root_y + root_height + 16
            if y + height > screen_height - 56:
                y = max(16, (screen_height - height) // 2)
        y = min(max(16, y), max(16, screen_height - height - 56))
        self.monitor_window.geometry(f"{width}x{height}+{x}+{y}")

    def _show_main_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _hide_main_window(self) -> None:
        if not self._tray_available:
            self._exit_app()
            return
        self.root.withdraw()
        LOGGER.info("主窗口已隐藏到系统托盘。")
        if not self._tray_notice_shown:
            self._tray_notice_shown = True
            self._notify("程序仍在托盘后台运行，关闭窗口不会退出。", "已最小化到托盘")

    def _show_monitor_window(self) -> None:
        if self.state is State.IDLE:
            self._show_main_window()
            return
        try:
            if not self._monitor_positioned:
                self._position_monitor_window()
                self._monitor_positioned = True
            self.monitor_window.deiconify()
            self.monitor_window.lift()
            self.monitor_window.focus_force()
        except tk.TclError:
            pass

    def _hide_monitor(self) -> None:
        try:
            self.monitor_window.withdraw()
        except tk.TclError:
            pass

    def _on_main_close(self) -> None:
        self._hide_main_window()

    def _exit_app(self) -> None:
        if self._exiting:
            return
        self._close_settings()
        LOGGER.info("退出程序。")
        if self.state is State.COUNTDOWN and not DRY_RUN:
            result = self._run_shutdown_command(["/a"])
            if result is None or result.returncode != 0:
                LOGGER.error("退出时取消系统关机失败，保留退出选择。")
                if not messagebox.askyesno(
                    "系统关机仍在倒计时",
                    "退出时无法取消已计划的系统关机。\n"
                    "现在退出后，电脑仍会在倒计时结束时关机。\n\n"
                    "仍要退出吗？",
                    parent=self.root,
                ):
                    LOGGER.info("用户选择取消退出。")
                    return
            else:
                LOGGER.warning("退出时已执行 shutdown /a，取消待执行的系统关机。")

        self._exiting = True
        self._cancel_tick()
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self._low_since = None

        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                LOGGER.debug("停止托盘图标失败", exc_info=True)
        if self._tray_thread is not None and self._tray_thread.is_alive():
            self._tray_thread.join(timeout=2.0)

        try:
            self.monitor_window.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        LOGGER.info("程序已退出。")
        logging.shutdown()

    def _on_tk_exception(
        self, exc_type: type[BaseException], exc_value: BaseException, tb: object
    ) -> None:
        LOGGER.error(
            "界面回调发生未处理异常",
            exc_info=(exc_type, exc_value, tb),
        )
        try:
            if self.state in (State.MONITORING, State.COUNTDOWN):
                self._set_status("程序内部错误，请查看日志。", AMBER)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    enable_dpi_awareness()
    log_path = setup_logging()

    LOGGER.info("=" * 72)
    LOGGER.info(
        "启动 %s v%s，模式=%s（DRY_RUN=%s），Python=%s，目录=%s。",
        APP_NAME,
        APP_VERSION,
        MODE_LABEL,
        DRY_RUN,
        sys.version.split()[0],
        APP_DIR,
    )
    if log_path is not None:
        LOGGER.info(
            "日志文件：%s（单文件上限 %d bytes，保留 %d 个备份）。",
            log_path,
            LOG_MAX_BYTES,
            LOG_BACKUP_COUNT,
        )

    config_issues = load_configuration()
    LOGGER.info("配置文件：%s。", CONFIG_FILE)
    LOGGER.info(
        "生效阈值：网络 < %s，磁盘写入 < %s，CPU < %g%%，持续 %d 秒；"
        "关机倒计时 %d 秒。",
        format_rate_threshold(NET_LOW_VALUE, NET_UNIT),
        format_rate_threshold(DISK_LOW_VALUE, DISK_UNIT),
        CPU_LOW_PERCENT,
        LOW_ACTIVITY_SECONDS,
        COUNTDOWN_SECONDS,
    )

    if config_issues:
        details = "\n".join(f"• {issue}" for issue in config_issues[:8])
        if len(config_issues) > 8:
            details += f"\n• ……另有 {len(config_issues) - 8} 项"
        _native_message(
            "阈值配置提示",
            f"配置文件：{CONFIG_FILE}\n\n"
            f"{details}\n\n"
            "有问题的项目已使用默认值。程序仍会正常启动。",
            error=False,
        )

    previous_thread_hook = threading.excepthook

    def thread_exception_hook(args: threading.ExceptHookArgs) -> None:
        LOGGER.error(
            "线程 %s 发生未处理异常",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        previous_thread_hook(args)

    threading.excepthook = thread_exception_hook

    try:
        root = tk.Tk()
        app = AutoShutdownApp(root)
        root.mainloop()
    except Exception:
        LOGGER.exception("程序发生未处理异常")
        _native_message(
            "程序异常",
            "程序发生未处理异常，详情请查看 logs\\app.log。",
        )
        logging.shutdown()
        return 1

    # 正常情况下由 _exit_app 清理；若 mainloop 被外部结束，则补一次退出日志。
    if not getattr(app, "_exiting", False):
        LOGGER.info("主窗口循环结束，程序退出。")
        logging.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
