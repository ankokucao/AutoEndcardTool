from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

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


class AutoEndcardApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Auto Endcard Tool - 批量自动加片尾")
        self.root.geometry("980x790")
        self.root.minsize(860, 700)

        self.config_store = ConfigStore(application_dir() / CONFIG_FILENAME)
        config = self.config_store.load()

        self.input_dir = tk.StringVar(value=config.input_dir)
        self.output_dir = tk.StringVar(value=config.output_dir)
        self.target_aspect = tk.StringVar(value=config.target_aspect)
        self.outro_16x9 = tk.StringVar(value=config.outro_16x9)
        self.outro_1x1 = tk.StringVar(value=config.outro_1x1)
        self.outro_9x16 = tk.StringVar(value=config.outro_9x16)
        self._endcard_variables = {
            "16:9": self.outro_16x9,
            "1:1": self.outro_1x1,
            "9:16": self.outro_9x16,
        }
        self._displayed_aspect = config.target_aspect
        self.endcard_path = tk.StringVar(
            value=self._endcard_variables[self._displayed_aspect].get()
        )
        self.endcard_label = tk.StringVar(value=f"{self._displayed_aspect} 片尾文件")
        self.cut_head_seconds = tk.StringVar(value=str(config.cut_head_seconds))
        self.cut_tail_seconds = tk.StringVar(value=str(config.cut_tail_seconds))
        self.fps = tk.StringVar(value=str(config.fps))
        self.crf = tk.StringVar(value=str(config.crf))
        self.preset = tk.StringVar(value=config.preset)
        self.overwrite = tk.BooleanVar(value=config.overwrite)
        self.suffix = tk.StringVar(value=config.suffix)

        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

        self._build_ui()
        self._poll_events()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="批量自动加片尾 / v4.2 视频片尾版",
            font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            outer,
            text="每次处理一种比例；开始前扫描全部原视频并校验片尾比例。",
            foreground="#555555",
        ).pack(anchor="w", pady=(0, 10))

        task = ttk.LabelFrame(outer, text="本次任务", padding=10)
        task.pack(fill="x", pady=(0, 10))
        task.columnconfigure(1, weight=1)
        ttk.Label(task, text="生成比例").grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5,
        )
        aspect_selector = ttk.Combobox(
            task,
            textvariable=self.target_aspect,
            values=tuple(TARGETS),
            state="readonly",
            width=12,
        )
        aspect_selector.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=5)
        aspect_selector.bind("<<ComboboxSelected>>", self._on_aspect_changed)
        self._path_row(
            task,
            1,
            self.endcard_label,
            self.endcard_path,
            self.choose_endcard,
        )

        paths = ttk.LabelFrame(outer, text="路径设置", padding=10)
        paths.pack(fill="x", pady=(0, 10))
        paths.columnconfigure(1, weight=1)
        self._path_row(paths, 0, "原视频文件夹", self.input_dir, self.choose_input_dir)
        self._path_row(paths, 1, "输出文件夹", self.output_dir, self.choose_output_dir)

        export = ttk.LabelFrame(outer, text="导出设置", padding=10)
        export.pack(fill="x", pady=(0, 10))
        for column in range(8):
            export.columnconfigure(column, weight=1 if column % 2 else 0)

        self._setting_row(export, 0, 0, "裁剪开头/秒", self.cut_head_seconds)
        self._setting_row(export, 0, 2, "裁剪结尾/秒", self.cut_tail_seconds)

        ttk.Label(export, text="FPS").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=5)
        ttk.Combobox(
            export,
            textvariable=self.fps,
            values=FPS_CHOICES,
            state="readonly",
            width=8,
        ).grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=5)

        ttk.Label(export, text="CRF 画质").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=5)
        ttk.Combobox(
            export,
            textvariable=self.crf,
            values=CRF_CHOICES,
            state="readonly",
            width=8,
        ).grid(row=1, column=3, sticky="ew", padx=(0, 14), pady=5)

        ttk.Label(export, text="编码速度").grid(row=1, column=4, sticky="w", padx=(0, 6), pady=5)
        ttk.Combobox(
            export,
            textvariable=self.preset,
            values=PRESET_CHOICES,
            state="readonly",
            width=10,
        ).grid(row=1, column=5, sticky="ew", padx=(0, 14), pady=5)

        ttk.Label(export, text="文件名后缀").grid(row=1, column=6, sticky="w", padx=(0, 6), pady=5)
        ttk.Entry(export, textvariable=self.suffix, width=14).grid(
            row=1,
            column=7,
            sticky="ew",
            pady=5,
        )
        ttk.Checkbutton(export, text="覆盖同名输出", variable=self.overwrite).grid(
            row=2,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(5, 0),
        )

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(actions, text="开始批量处理", command=self.start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="打开输出文件夹", command=self.open_output).pack(
            side="left",
            padx=(0, 8),
        )
        ttk.Button(actions, text="清空日志", command=self.clear_log).pack(side="right")

        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 10))

        log_frame = ttk.LabelFrame(outer, text="处理日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = ScrolledText(
            log_frame,
            height=15,
            wrap="word",
            font=("Consolas", 10),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)

    @staticmethod
    def _path_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str | tk.StringVar,
        variable: tk.StringVar,
        command: object,
    ) -> None:
        label_options = {"textvariable": label} if isinstance(label, tk.StringVar) else {"text": label}
        ttk.Label(parent, **label_options).grid(
            row=row,
            column=0,
            sticky="w",
            padx=(0, 8),
            pady=5,
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=1,
            sticky="ew",
            padx=(0, 8),
            pady=5,
        )
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, pady=5)

    @staticmethod
    def _setting_row(
        parent: ttk.LabelFrame,
        row: int,
        column: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 6),
            pady=5,
        )
        ttk.Spinbox(
            parent,
            from_=0,
            to=300,
            increment=0.1,
            textvariable=variable,
            width=10,
        ).grid(row=row, column=column + 1, sticky="ew", padx=(0, 14), pady=5)

    def choose_input_dir(self) -> None:
        selected = filedialog.askdirectory(title="选择原视频文件夹")
        if not selected:
            return
        self.input_dir.set(selected)
        if not self.output_dir.get().strip():
            self.output_dir.set(str(Path(selected) / "output"))

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(title="选择输出文件夹")
        if selected:
            self.output_dir.set(selected)

    def choose_endcard(self) -> None:
        selected = filedialog.askopenfilename(
            title=f"选择 {self.target_aspect.get()} 片尾文件",
            filetypes=[
                (
                    "Video files",
                    "*.mp4 *.mov *.m4v *.mkv *.avi *.webm",
                ),
            ],
        )
        if selected:
            self.endcard_path.set(selected)

    def _sync_visible_endcard(self) -> None:
        self._endcard_variables[self._displayed_aspect].set(
            self.endcard_path.get().strip()
        )

    def _on_aspect_changed(self, _event: object | None = None) -> None:
        self._sync_visible_endcard()
        selected = self.target_aspect.get()
        if selected not in TARGETS:
            selected = "16:9"
            self.target_aspect.set(selected)
        self._displayed_aspect = selected
        self.endcard_path.set(self._endcard_variables[selected].get())
        self.endcard_label.set(f"{selected} 片尾文件")

    def _current_config(self) -> AppConfig:
        self._sync_visible_endcard()
        return AppConfig(
            input_dir=self.input_dir.get().strip(),
            output_dir=self.output_dir.get().strip(),
            target_aspect=self.target_aspect.get(),
            outro_16x9=self.outro_16x9.get().strip(),
            outro_1x1=self.outro_1x1.get().strip(),
            outro_9x16=self.outro_9x16.get().strip(),
            cut_head_seconds=float(self.cut_head_seconds.get()),
            cut_tail_seconds=float(self.cut_tail_seconds.get()),
            fps=int(self.fps.get()),
            crf=int(self.crf.get()),
            preset=self.preset.get(),
            overwrite=bool(self.overwrite.get()),
            suffix=self.suffix.get(),
        )

    def _build_request(self) -> tuple[BatchRequest, str, str]:
        input_dir = Path(self.input_dir.get().strip())
        if not input_dir.is_dir():
            raise ValueError("请选择有效的原视频文件夹。")

        output_text = self.output_dir.get().strip()
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

        self._sync_visible_endcard()
        target_aspect = self.target_aspect.get()
        if target_aspect not in TARGETS:
            raise ValueError("请选择有效的生成比例。")
        endcard = Path(self.endcard_path.get().strip())
        if not endcard.is_file() or endcard.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"请选择有效的 {target_aspect} 视频片尾文件。")

        try:
            cut_head = float(self.cut_head_seconds.get())
            cut_tail = float(self.cut_tail_seconds.get())
            fps = int(self.fps.get())
            crf = int(self.crf.get())
        except ValueError as exc:
            raise ValueError("导出参数必须是有效数字。") from exc

        if cut_head < 0 or cut_tail < 0:
            raise ValueError("裁剪时长不能为负数。")
        if fps not in FPS_CHOICES or crf not in CRF_CHOICES:
            raise ValueError("FPS 或 CRF 不在支持的选项中。")
        if self.preset.get() not in PRESET_CHOICES:
            raise ValueError("编码速度设置无效。")

        suffix = self.suffix.get()
        if any(character in suffix for character in '<>:"/\\|?*'):
            raise ValueError("文件名后缀包含 Windows 不允许的字符。")

        request = BatchRequest(
            input_dir=input_dir,
            output_dir=output_dir,
            target_aspect=target_aspect,
            endcard=endcard,
            options=EncodingOptions(
                cut_head_seconds=cut_head,
                cut_tail_seconds=cut_tail,
                fps=fps,
                crf=crf,
                preset=self.preset.get(),
                overwrite=bool(self.overwrite.get()),
                suffix=suffix,
            ),
        )
        return request, ffmpeg, ffprobe

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            request, ffmpeg, ffprobe = self._build_request()
            self.config_store.save(self._current_config())
        except (OSError, ValueError) as exc:
            messagebox.showerror("设置错误", str(exc))
            return

        self.stop_event.clear()
        self.progress.configure(value=0, maximum=1)
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(request, ffmpeg, ffprobe),
            daemon=True,
        )
        self.worker.start()

    def _run_batch(self, request: BatchRequest, ffmpeg: str, ffprobe: str) -> None:
        try:
            processor = BatchProcessor(ffmpeg, ffprobe)
            processor.process(
                request,
                self.stop_event,
                lambda message: self.event_queue.put(("log", message)),
                lambda current, total: self.event_queue.put(
                    ("progress", (current, total))
                ),
            )
        except Exception as exc:
            self.event_queue.put(("log", f"处理器异常：{exc}"))
        finally:
            self.event_queue.put(("finished", None))

    def stop(self) -> None:
        self.stop_event.set()
        self.event_queue.put(("log", "收到停止请求，当前文件处理完成后停止。"))

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.event_queue.get_nowait()
                if event == "log":
                    self._append_log(str(payload))
                elif event == "progress":
                    current, total = payload  # type: ignore[misc]
                    self.progress.configure(value=current, maximum=max(total, 1))
                elif event == "finished":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def open_output(self) -> None:
        path = Path(self.output_dir.get().strip())
        if not path.is_dir():
            messagebox.showinfo("提示", "输出文件夹还不存在。")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc))

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            confirmed = messagebox.askyesno(
                "正在处理",
                "当前仍在处理视频，确定退出吗？",
            )
            if not confirmed:
                return
            self.stop_event.set()
        try:
            self.config_store.save(self._current_config())
        except (OSError, ValueError):
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style(root).theme_use("vista")
    except tk.TclError:
        pass
    AutoEndcardApp(root)
    root.mainloop()
