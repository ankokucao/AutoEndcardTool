from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import glfw
import skia
from neui import App, cui, ui
from neui.core.renderer import Renderer

from .config import ConfigStore
from .constants import (
    CONFIG_FILENAME,
    CRF_CHOICES,
    FPS_CHOICES,
    PRESET_CHOICES,
    TARGETS,
    VIDEO_EXTENSIONS,
)
from .models import AppConfig, BatchRequest, EncodingOptions
from .paths import application_dir, find_binary
from .processor import BatchProcessor


WINDOW_TITLE = "Auto Endcard Tool - 批量自动加片尾"
COLOR = {
    "bg": "#090D15",
    "surface": "#111827",
    "surface2": "#151E2E",
    "input": "#0D1422",
    "border": "#26334A",
    "text": "#F4F7FB",
    "muted": "#8D9AAF",
    "accent": "#2F80ED",
    "hover": "#58A6FF",
    "pressed": "#1F64C3",
    "danger": "#D94F64",
    "disabled": "#344054",
}


def _install_chinese_font() -> None:
    if getattr(Renderer, "_auto_endcard_font_installed", False):
        return
    typeface = skia.Typeface("Microsoft YaHei UI")
    original_init = Renderer.__init__

    def renderer_init(renderer: Renderer) -> None:
        original_init(renderer)
        renderer.default_typeface = typeface
        renderer.default_font = skia.Font(typeface, 14)

    Renderer.__init__ = renderer_init  # type: ignore[method-assign]
    Renderer._auto_endcard_font_installed = True  # type: ignore[attr-defined]


_install_chinese_font()


def _set_input(control: ui.Input, value: object) -> None:
    control.text = str(value)
    control.cursor_pos = len(control.text)


def _message(title: str, text: str, kind: str = "info") -> int:
    flags = {"info": 0x40, "error": 0x10, "question": 0x24}.get(kind, 0x40)
    return int(ctypes.windll.user32.MessageBoxW(None, text, title, flags))


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _native_dialog(script: str) -> str:
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _choose_folder(title: str, initial: str) -> str:
    selected = ""
    if initial and Path(initial).is_dir():
        selected = f"$d.SelectedPath='{_ps_quote(initial)}';"
    return _native_dialog(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object Windows.Forms.FolderBrowserDialog;"
        f"$d.Description='{_ps_quote(title)}';{selected}"
        "if($d.ShowDialog() -eq [Windows.Forms.DialogResult]::OK){"
        "[Console]::Write($d.SelectedPath)}"
    )


def _choose_video(title: str, initial: str) -> str:
    preset = ""
    current = Path(initial) if initial else None
    if current and current.parent.is_dir():
        preset = (
            f"$d.InitialDirectory='{_ps_quote(str(current.parent))}';"
            f"$d.FileName='{_ps_quote(current.name)}';"
        )
    return _native_dialog(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object Windows.Forms.OpenFileDialog;"
        f"$d.Title='{_ps_quote(title)}';"
        "$d.Filter='视频文件|*.mp4;*.mov;*.m4v;*.mkv;*.avi;*.webm|所有文件|*.*';"
        f"$d.RestoreDirectory=$true;{preset}"
        "if($d.ShowDialog() -eq [Windows.Forms.DialogResult]::OK){"
        "[Console]::Write($d.FileName)}"
    )


class PumpedRoot(ui.Box):
    def __init__(self, pump: Callable[[], None], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.pump = pump

    def render(self, canvas: object, renderer: Renderer) -> None:
        self.pump()
        super().render(canvas, renderer)


class LogText(ui.Text):
    def __init__(self, **kwargs: object) -> None:
        super().__init__("", **kwargs)
        self.lines: list[str] = []

    def append(self, line: str) -> None:
        self.lines.append(line)
        self.lines = self.lines[-500:]

    def clear(self) -> None:
        self.lines.clear()

    def measure(self, parent_w: float, parent_h: float) -> tuple[float, float]:
        del parent_h
        return parent_w, max(140, max(1, len(self.lines)) * 20 + 8)

    def render(self, canvas: object, renderer: Renderer) -> None:
        lines = self.lines or ["等待任务开始…"]
        bounds = self.computed_bounds
        for index, line in enumerate(lines):
            renderer.draw_text(
                canvas,
                line,
                bounds["x"],
                bounds["y"] + index * 20,
                self.style,
            )


class StateButton(cui.Button):
    def __init__(
        self,
        text: str,
        handler: Callable[[], None],
        *,
        enabled: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(text=text, on_click=handler, **kwargs)
        self.enabled = enabled
        self.active_bg = str(self.style["bg"])
        self.set_enabled(enabled)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.normal_bg = self.active_bg if enabled else COLOR["disabled"]
        self.hover_bg = COLOR["hover"] if enabled else COLOR["disabled"]
        self.pressed_bg = COLOR["pressed"] if enabled else COLOR["disabled"]
        self.style["bg"] = self.normal_bg

    def on_mouse_enter(self) -> None:
        if self.enabled:
            super().on_mouse_enter()

    def on_mouse_leave(self) -> None:
        self.style["bg"] = self.normal_bg

    def on_mouse_down(self, x: float = 0, y: float = 0) -> None:
        if self.enabled:
            super().on_mouse_down(x, y)

    def on_mouse_up(self) -> None:
        if self.enabled:
            super().on_mouse_up()

    def on_click(self) -> None:
        if self.enabled:
            super().on_click()


class AutoEndcardApp:
    def __init__(self) -> None:
        self.config_store = ConfigStore(application_dir() / CONFIG_FILENAME)
        config = self.config_store.load()
        self.target_aspect = (
            config.target_aspect if config.target_aspect in TARGETS else "16:9"
        )
        self.displayed_aspect = self.target_aspect
        self.endcards = {
            "16:9": config.outro_16x9,
            "1:1": config.outro_1x1,
            "9:16": config.outro_9x16,
        }
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.scroll_log_next_frame = False

        self.app = App(title=WINDOW_TITLE, width=1220, height=900, theme="dark")
        glfw.set_window_size_limits(
            self.app.window, 1120, 820, glfw.DONT_CARE, glfw.DONT_CARE
        )
        glfw.set_window_close_callback(self.app.window, self._on_close)
        self._build_ui(config)

    @staticmethod
    def _text(text: str, muted: bool = False, **style: object) -> ui.Text:
        return ui.Text(
            text,
            style={
                "color": COLOR["muted"] if muted else COLOR["text"],
                "font_size": 14,
                **style,
            },
        )

    @staticmethod
    def _input(width: int, placeholder: str = "") -> ui.Input:
        return ui.Input(
            placeholder=placeholder,
            style={
                "w": width,
                "font_size": 14,
                "padding": 10,
                "bg": COLOR["input"],
                "color": COLOR["text"],
                "radius": 7,
                "border_color": COLOR["border"],
                "border_width": 1,
            },
        )

    @staticmethod
    def _dropdown(options: list[object], value: object, width: int) -> cui.Dropdown:
        return cui.Dropdown(
            options=options,
            value=value,
            style={
                "w": width,
                "h": 36,
                "bg": COLOR["input"],
                "color": COLOR["text"],
                "radius": 7,
                "border_color": COLOR["border"],
                "border_width": 1,
            },
        )

    def _card(self, title: str, height: int | None = None) -> cui.Card:
        style: dict[str, object] = {
            "w": "100%",
            "layout": "col",
            "gap": 10,
            "padding": 16,
            "bg": COLOR["surface"],
            "radius": 12,
            "border_color": COLOR["border"],
            "border_width": 1,
        }
        if height:
            style["h"] = height
        card = cui.Card(style=style)
        card.add(self._text(title, font_size=15))
        return card

    def _path_row(
        self,
        label: str | ui.Text,
        control: ui.Input,
        handler: Callable[[], None],
    ) -> ui.Box:
        row = ui.Box(
            style={
                "w": "100%",
                "h": 36,
                "layout": "row",
                "align": "center",
                "justify": "space-between",
                "gap": 10,
            }
        )
        label_box = ui.Box(
            style={"w": 112, "h": 36, "layout": "row", "align": "center"}
        )
        label_box.add(
            label if isinstance(label, ui.Text) else self._text(label, True, font_size=13)
        )
        row.add(label_box)
        row.add(control)
        row.add(
            StateButton(
                "选择",
                handler,
                style={
                    "w": 96,
                    "h": 36,
                    "bg": "#263A56",
                    "radius": 7,
                    "font_size": 13,
                },
            )
        )
        return row

    def _field(self, label: str, control: object, width: int) -> ui.Box:
        box = ui.Box(style={"w": width, "layout": "col", "gap": 7})
        box.add(self._text(label, True, font_size=12))
        box.add(control)
        return box

    def _build_ui(self, config: AppConfig) -> None:
        root = PumpedRoot(
            self._pump_events,
            style={
                "layout": "col",
                "gap": 12,
                "padding": 20,
                "bg": COLOR["bg"],
            },
        )

        header = ui.Box(
            style={
                "w": "100%",
                "h": 52,
                "layout": "row",
                "justify": "space-between",
            }
        )
        heading = ui.Box(style={"w": 800, "layout": "col", "gap": 4})
        heading.add(self._text("Auto Endcard Tool", font_size=22))
        heading.add(
            self._text(
                "每次生成一种比例的片尾；执行前自动扫描并校验全部原视频。",
                True,
                font_size=13,
            )
        )
        badge = ui.Box(
            style={
                "w": 142,
                "h": 32,
                "layout": "row",
                "align": "center",
                "justify": "center",
                "bg": "#153157",
                "radius": 16,
                "border_color": "#285C99",
                "border_width": 1,
            }
        )
        badge.add(self._text("NEUI  ·  GPU UI", color="#8CC4FF", font_size=12))
        header.add(heading)
        header.add(badge)
        root.add(header)

        task = self._card("本次任务")
        ratio_row = ui.Box(
            style={"w": "100%", "h": 36, "layout": "row", "align": "center", "gap": 16}
        )
        ratio_label = ui.Box(
            style={"w": 112, "h": 36, "layout": "row", "align": "center"}
        )
        ratio_label.add(self._text("生成比例", True, font_size=13))
        self.aspect = cui.Dropdown(
            options=list(TARGETS),
            value=self.target_aspect,
            on_change=self._aspect_changed,
            style={
                "w": 150,
                "h": 36,
                "bg": COLOR["input"],
                "color": COLOR["text"],
                "radius": 7,
                "border_color": COLOR["border"],
                "border_width": 1,
            },
        )
        ratio_row.add(ratio_label)
        ratio_row.add(self.aspect)
        ratio_row.add(
            self._text(
                "仅处理比例一致的视频；分辨率不同时自动拉伸适配。",
                True,
                font_size=12,
            )
        )
        task.add(ratio_row)
        self.endcard_label = self._text(
            f"{self.target_aspect} 片尾文件", True, font_size=13
        )
        self.endcard_input = self._input(800, "请选择视频片尾文件")
        _set_input(self.endcard_input, self.endcards[self.target_aspect])
        task.add(self._path_row(self.endcard_label, self.endcard_input, self.choose_endcard))
        root.add(task)

        paths = self._card("路径设置")
        self.input_dir = self._input(800, "选择包含原视频的文件夹")
        self.output_dir = self._input(800, "选择处理结果的保存位置")
        _set_input(self.input_dir, config.input_dir)
        _set_input(self.output_dir, config.output_dir)
        paths.add(self._path_row("原视频文件夹", self.input_dir, self.choose_input_dir))
        paths.add(self._path_row("输出文件夹", self.output_dir, self.choose_output_dir))
        root.add(paths)

        export = self._card("导出设置")
        settings = ui.Box(
            style={
                "w": "100%",
                "layout": "row",
                "gap": 12,
                "justify": "space-between",
            }
        )
        self.cut_head = self._input(120, "0")
        self.cut_tail = self._input(120, "0")
        self.fps = self._dropdown(list(FPS_CHOICES), config.fps, 90)
        self.crf = self._dropdown(list(CRF_CHOICES), config.crf, 90)
        self.preset = self._dropdown(list(PRESET_CHOICES), config.preset, 120)
        self.suffix = self._input(150, "_endcard")
        _set_input(self.cut_head, config.cut_head_seconds)
        _set_input(self.cut_tail, config.cut_tail_seconds)
        _set_input(self.suffix, config.suffix)
        settings.add(self._field("裁剪开头 / 秒", self.cut_head, 120))
        settings.add(self._field("裁剪结尾 / 秒", self.cut_tail, 120))
        settings.add(self._field("FPS", self.fps, 90))
        settings.add(self._field("CRF 画质", self.crf, 90))
        settings.add(self._field("编码速度", self.preset, 120))
        settings.add(self._field("文件名后缀", self.suffix, 150))
        overwrite = ui.Box(style={"w": 140, "layout": "col", "gap": 10})
        overwrite.add(self._text("输出策略", True, font_size=12))
        overwrite_row = ui.Box(
            style={"w": 140, "h": 28, "layout": "row", "align": "center", "gap": 9}
        )
        self.overwrite = cui.Checkbox(
            checked=config.overwrite,
            style={
                "w": 20,
                "h": 20,
                "radius": 5,
                "border_color": COLOR["border"],
                "border_width": 1,
            },
        )
        overwrite_row.add(self.overwrite)
        overwrite_row.add(self._text("覆盖同名输出", font_size=13))
        overwrite.add(overwrite_row)
        settings.add(overwrite)
        export.add(settings)
        root.add(export)

        actions = ui.Box(
            style={
                "w": "100%",
                "h": 42,
                "layout": "row",
                "justify": "space-between",
            }
        )
        action_left = ui.Box(style={"w": 430, "h": 42, "layout": "row", "gap": 10})
        button_style = {"h": 42, "radius": 8, "font_size": 14}
        self.start_button = StateButton(
            "开始批量处理",
            self.start,
            style={"w": 152, "bg": COLOR["accent"], **button_style},
        )
        self.stop_button = StateButton(
            "停止",
            self.stop,
            enabled=False,
            style={"w": 92, "bg": COLOR["danger"], **button_style},
        )
        action_left.add(self.start_button)
        action_left.add(self.stop_button)
        action_left.add(
            StateButton(
                "打开输出文件夹",
                self.open_output,
                style={"w": 156, "bg": "#263A56", **button_style},
            )
        )
        actions.add(action_left)
        actions.add(
            StateButton(
                "清空日志",
                self.clear_log,
                style={"w": 104, "bg": "#263A56", **button_style},
            )
        )
        root.add(actions)

        progress_box = ui.Box(
            style={
                "w": "100%",
                "layout": "col",
                "gap": 8,
                "padding": 12,
                "bg": COLOR["surface2"],
                "radius": 10,
                "border_color": COLOR["border"],
                "border_width": 1,
            }
        )
        status = ui.Box(
            style={
                "w": "100%",
                "h": 18,
                "layout": "row",
                "justify": "space-between",
            }
        )
        self.status = self._text("就绪", True, font_size=12)
        self.percent = self._text("0%", True, font_size=12)
        status.add(self.status)
        status.add(self.percent)
        progress_box.add(status)
        self.progress = cui.ProgressBar(
            value=0,
            fill_color=COLOR["accent"],
            style={"w": "100%", "h": 8, "bg": "#26334A", "radius": 4},
        )
        progress_box.add(self.progress)
        root.add(progress_box)

        log_card = self._card("处理日志", 205)
        self.log_scroll = ui.ScrollView(
            style={
                "w": "100%",
                "h": 150,
                "padding": 10,
                "bg": COLOR["input"],
                "radius": 8,
                "border_color": COLOR["border"],
                "border_width": 1,
                "scrollbar_width": 6,
                "scrollbar_color": "#52647D",
                "scrollbar_hover_color": COLOR["hover"],
            }
        )
        self.log = LogText(
            style={
                "w": "100%",
                "font_size": 13,
                "line_height": 20,
                "color": "#C7D2E3",
            }
        )
        self.log_scroll.add(self.log)
        log_card.add(self.log_scroll)
        root.add(log_card)
        self.app.add(root)

    def choose_input_dir(self) -> None:
        selected = _choose_folder("选择原视频文件夹", self.input_dir.text.strip())
        if not selected:
            return
        _set_input(self.input_dir, selected)
        if not self.output_dir.text.strip():
            _set_input(self.output_dir, Path(selected) / "output")

    def choose_output_dir(self) -> None:
        selected = _choose_folder("选择输出文件夹", self.output_dir.text.strip())
        if selected:
            _set_input(self.output_dir, selected)

    def choose_endcard(self) -> None:
        selected = _choose_video(
            f"选择 {self.target_aspect} 视频片尾", self.endcard_input.text.strip()
        )
        if selected:
            _set_input(self.endcard_input, selected)

    def _sync_endcard(self) -> None:
        self.endcards[self.displayed_aspect] = self.endcard_input.text.strip()

    def _aspect_changed(self, value: object) -> None:
        self._sync_endcard()
        selected = str(value)
        if selected not in TARGETS:
            selected = "16:9"
            self.aspect.value = selected
        self.target_aspect = selected
        self.displayed_aspect = selected
        _set_input(self.endcard_input, self.endcards[selected])
        self.endcard_label.text = f"{selected} 片尾文件"

    def _current_config(self) -> AppConfig:
        self._sync_endcard()
        return AppConfig(
            input_dir=self.input_dir.text.strip(),
            output_dir=self.output_dir.text.strip(),
            target_aspect=self.target_aspect,
            outro_16x9=self.endcards["16:9"].strip(),
            outro_1x1=self.endcards["1:1"].strip(),
            outro_9x16=self.endcards["9:16"].strip(),
            cut_head_seconds=float(self.cut_head.text),
            cut_tail_seconds=float(self.cut_tail.text),
            fps=int(self.fps.value),
            crf=int(self.crf.value),
            preset=str(self.preset.value),
            overwrite=bool(self.overwrite.checked),
            suffix=self.suffix.text,
        )

    def _build_request(self) -> tuple[BatchRequest, str, str]:
        input_dir = Path(self.input_dir.text.strip())
        if not input_dir.is_dir():
            raise ValueError("请选择有效的原视频文件夹。")
        output_text = self.output_dir.text.strip()
        if not output_text:
            raise ValueError("请选择输出文件夹。")
        output_dir = Path(output_text)
        if input_dir.resolve() == output_dir.resolve():
            raise ValueError("输出文件夹不能与原视频文件夹相同。")

        ffmpeg = find_binary("ffmpeg")
        ffprobe = find_binary("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ValueError(
                "找不到 ffmpeg/ffprobe。请放入 tools 文件夹，或加入系统 PATH。"
            )

        self._sync_endcard()
        if self.target_aspect not in TARGETS:
            raise ValueError("请选择有效的生成比例。")
        endcard = Path(self.endcard_input.text.strip())
        if not endcard.is_file() or endcard.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"请选择有效的 {self.target_aspect} 视频片尾文件。")
        try:
            cut_head = float(self.cut_head.text)
            cut_tail = float(self.cut_tail.text)
            fps = int(self.fps.value)
            crf = int(self.crf.value)
        except (TypeError, ValueError) as exc:
            raise ValueError("导出参数必须是有效数字。") from exc
        if cut_head < 0 or cut_tail < 0:
            raise ValueError("裁剪时长不能为负数。")
        if fps not in FPS_CHOICES or crf not in CRF_CHOICES:
            raise ValueError("FPS 或 CRF 不在支持的选项中。")
        preset = str(self.preset.value)
        if preset not in PRESET_CHOICES:
            raise ValueError("编码速度设置无效。")
        suffix = self.suffix.text
        if any(character in suffix for character in '<>:"/\\|?*'):
            raise ValueError("文件名后缀包含 Windows 不允许的字符。")

        request = BatchRequest(
            input_dir=input_dir,
            output_dir=output_dir,
            target_aspect=self.target_aspect,
            endcard=endcard,
            options=EncodingOptions(
                cut_head_seconds=cut_head,
                cut_tail_seconds=cut_tail,
                fps=fps,
                crf=crf,
                preset=preset,
                overwrite=bool(self.overwrite.checked),
                suffix=suffix,
            ),
        )
        return request, ffmpeg, ffprobe

    def start(self) -> None:
        if self.running:
            return
        try:
            request, ffmpeg, ffprobe = self._build_request()
            self.config_store.save(self._current_config())
        except (OSError, ValueError) as exc:
            _message("设置错误", str(exc), "error")
            return
        self.stop_event.clear()
        self.progress.set_value(0)
        self.percent.text = "0%"
        self.status.text = "正在扫描原视频…"
        self.running = True
        self.start_button.text = "处理中…"
        self.start_button.set_enabled(False)
        self.stop_button.set_enabled(True)
        self._append_log(f"任务已启动：目标比例 {request.target_aspect}")
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(request, ffmpeg, ffprobe),
            daemon=True,
        )
        self.worker.start()

    def _run_batch(self, request: BatchRequest, ffmpeg: str, ffprobe: str) -> None:
        try:
            BatchProcessor(ffmpeg, ffprobe).process(
                request,
                self.stop_event,
                lambda text: self.events.put(("log", text)),
                lambda current, total: self.events.put(
                    ("progress", (current, total))
                ),
            )
        except Exception as exc:
            self.events.put(("log", f"处理器异常：{exc}"))
        finally:
            self.events.put(("finished", None))

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self.stop_button.set_enabled(False)
        self.status.text = "等待当前文件处理完成后停止…"
        self._append_log("收到停止请求，当前文件处理完成后停止。")

    def _pump_events(self) -> None:
        if self.scroll_log_next_frame:
            bottom = self.log.computed_bounds["y"] + self.log.computed_bounds["h"]
            content_height = bottom - self.log_scroll.computed_bounds["y"]
            self.log_scroll.scroll_y = max(
                0, content_height - self.log_scroll.computed_bounds["h"]
            )
            self.scroll_log_next_frame = False
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._append_log(str(payload))
            elif event == "progress":
                current, total = payload  # type: ignore[misc]
                total = max(int(total), 1)
                current = int(current)
                value = current / total
                self.progress.set_value(value)
                self.percent.text = f"{round(value * 100)}%"
                self.status.text = f"进度 {current} / {total}"
            elif event == "finished":
                self.running = False
                self.start_button.text = "开始批量处理"
                self.start_button.set_enabled(True)
                self.stop_button.set_enabled(False)
                self.status.text = "处理结束"

    def _append_log(self, text: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        self.scroll_log_next_frame = True

    def clear_log(self) -> None:
        self.log.clear()
        self.log_scroll.scroll_y = 0
        self.scroll_log_next_frame = False

    def open_output(self) -> None:
        path = Path(self.output_dir.text.strip())
        if not path.is_dir():
            _message("提示", "输出文件夹还不存在。")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            _message("打开失败", str(exc), "error")

    def _save_config(self) -> None:
        try:
            self.config_store.save(self._current_config())
        except (OSError, ValueError):
            pass

    def _on_close(self, window: object) -> None:
        if self.running:
            if _message("正在处理", "当前仍在处理视频，确定退出吗？", "question") != 6:
                glfw.set_window_should_close(window, False)
                return
            self.stop_event.set()
        self._save_config()

    def run(self) -> None:
        self.app.run()
        if self.running:
            self.stop_event.set()
        self._save_config()


def main() -> None:
    try:
        AutoEndcardApp().run()
    except Exception as exc:
        _message(
            "Auto Endcard Tool 启动失败",
            f"NEUI 图形界面无法启动：\n\n{exc}\n\n"
            "请确认显卡支持 OpenGL，并已安装项目依赖。",
            "error",
        )
