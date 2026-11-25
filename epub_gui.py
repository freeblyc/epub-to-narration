"""Offline Windows interface for the EPUB narration converter."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import sys
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from epub_to_tts import build_narration, existing_output, narration_path, write_narration


TITLE = "EPUB 转朗读稿"
FONT = "Microsoft YaHei UI"
PAGE = "#eff3f8"
PAPER = "#ffffff"
INK = "#17263c"
MUTED = "#475a74"
NAVY = "#182b48"
ACCENT = "#0b608b"
ACCENT_ACTIVE = "#084b6e"
ACCENT_DISABLED = "#6991a8"
EDGE = "#d5dfe9"
SUCCESS = "#176546"
ERROR = "#9b2d29"


@dataclass(frozen=True)
class ConversionEvent:
    kind: str
    source: Path | None = None
    detail: str = ""
    report: dict | None = None


def local_pandoc():
    if getattr(sys, "frozen", False):
        embedded = Path(sys._MEIPASS) / "converter" / "pandoc.exe"
        return str(embedded) if embedded.is_file() else None
    return shutil.which("pandoc")


def documents_folder():
    if sys.platform == "win32":
        from ctypes import create_unicode_buffer, windll

        location = create_unicode_buffer(32768)
        # CSIDL_PERSONAL gives the user's configured Documents folder.
        if windll.shell32.SHGetFolderPathW(None, 5, None, 0, location) == 0 and location.value:
            return Path(location.value)
    return Path.home() / "Documents"


def process_books(sources, destination, split, audit, pandoc, preview, stop, publish):
    succeeded = skipped = failed = 0
    try:
        for source in sources:
            if stop.is_set():
                break
            publish(ConversionEvent("working", source))
            target = narration_path(source, destination, split)
            try:
                if not preview:
                    collision = existing_output(target, split, audit)
                    if collision is not None:
                        skipped += 1
                        publish(ConversionEvent("skipped", source, str(collision)))
                        continue
                text, report, parts = build_narration(
                    source, pandoc, split=split,
                    progress=lambda detail: publish(ConversionEvent("progress", source, detail)),
                )
                if preview:
                    publish(ConversionEvent("preview", source, report=report))
                else:
                    write_narration(target, text, parts if split else None, report if audit else None)
                    count = len(parts) if split else 1
                    publish(ConversionEvent("done", source, f"{count} 份 TXT　{target}"))
                succeeded += 1
            except Exception as error:
                failed += 1
                publish(ConversionEvent("failed", source, str(error) or type(error).__name__))
    finally:
        if preview:
            summary = "分段预览已完成" if succeeded else "无法生成分段预览，详情见转换记录"
        else:
            ended = "已停止，当前图书已处理完毕" if succeeded + skipped + failed < len(sources) else "转换完成"
            summary = f"{ended}　成功 {succeeded}　跳过 {skipped}　失败 {failed}"
        publish(ConversionEvent("finished", detail=summary))


def configure_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), foreground=INK)
    style.configure("Page.TFrame", background=PAGE)
    style.configure("Paper.TFrame", background=PAPER)
    style.configure("Hero.TFrame", background=NAVY)
    style.configure("Title.TLabel", background=NAVY, foreground=PAPER, font=(FONT, 23, "bold"))
    style.configure("Subtitle.TLabel", background=NAVY, foreground="#d7e5f4", font=(FONT, 10))
    style.configure("Section.TLabel", background=PAPER, foreground=INK, font=(FONT, 11, "bold"))
    style.configure("Meta.TLabel", background=PAPER, foreground=MUTED)
    style.configure("Status.TLabel", background=PAGE, foreground=MUTED)
    style.configure("TButton", background=PAPER, bordercolor=EDGE, padding=(12, 7))
    style.map("TButton", background=[("active", "#edf5fb"), ("disabled", "#f3f5f8")],
              foreground=[("disabled", "#758397")], bordercolor=[("focus", ACCENT)])
    style.configure("Primary.TButton", background=ACCENT, foreground=PAPER, bordercolor=ACCENT,
                    padding=(16, 8), font=(FONT, 10, "bold"))
    style.map("Primary.TButton", background=[("disabled", ACCENT_DISABLED), ("active", ACCENT_ACTIVE)],
              bordercolor=[("focus", "#0c82bb"), ("disabled", ACCENT_DISABLED)],
              foreground=[("disabled", PAPER)])
    style.configure("TEntry", fieldbackground=PAPER, bordercolor=EDGE, padding=(8, 7))
    style.map("TEntry", bordercolor=[("focus", ACCENT)])
    style.configure("TRadiobutton", background=PAPER, indicatorcolor=PAPER, padding=(0, 4))
    style.configure("TCheckbutton", background=PAPER, indicatorcolor=PAPER, padding=(0, 4))
    style.map("TRadiobutton", background=[("active", PAPER)], indicatorcolor=[("selected", ACCENT)])
    style.map("TCheckbutton", background=[("active", PAPER)], indicatorcolor=[("selected", ACCENT)])
    style.configure("Treeview", background=PAPER, fieldbackground=PAPER, bordercolor=EDGE,
                    rowheight=max(27, round(root.winfo_fpixels("19p"))))
    style.configure("Treeview.Heading", background="#eaf0f7", bordercolor=EDGE, padding=(7, 5),
                    font=(FONT, 9, "bold"))
    style.map("Treeview", background=[("selected", "#deecf8")], foreground=[("selected", INK)])
    style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#dae5ef",
                    bordercolor=PAGE, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)


class NarrationWindow:
    def __init__(self, root):
        self.root = root
        self.sources = {}
        self.row_by_path = {}
        self.controls = []
        self.updates = Queue()
        self.stop = Event()
        self.running = False
        self.previewing = False
        self.close_after_work = False
        self.pandoc = local_pandoc()
        self.output = tk.StringVar(value=str(documents_folder() / "EPUB朗读稿"))
        self.split = tk.BooleanVar(value=True)
        self.audit = tk.BooleanVar(value=True)
        self.input_count = tk.StringVar(value="0 本")
        self.status = tk.StringVar(value="请选择一本或多本 EPUB。")

        root.title(TITLE)
        root.configure(background=PAGE)
        root.minsize(720, 605)
        screen_width, screen_height = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(900, screen_width - 44)
        height = min(710, screen_height - 70)
        root.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+{max(0, (screen_height - height) // 2 - 18)}")
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets" / "epub.ico"
        if icon.is_file():
            try:
                root.iconbitmap(str(icon))
            except tk.TclError:
                pass
        configure_style(root)
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(85, self.poll)

    def button(self, parent, label, callback):
        control = ttk.Button(parent, text=label, command=callback)
        self.controls.append(control)
        return control

    def build(self):
        header = ttk.Frame(self.root, style="Hero.TFrame", padding=(27, 17, 27, 17))
        header.pack(fill="x")
        ttk.Label(header, text=TITLE, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="本地转换 · 支持中文图书和批量处理", style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))

        workspace = ttk.Frame(self.root, style="Page.TFrame", padding=(22, 15, 22, 13))
        workspace.pack(fill="both", expand=True)
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(0, weight=2, minsize=158)
        workspace.rowconfigure(2, weight=1, minsize=105)

        source = ttk.Frame(workspace, style="Paper.TFrame", padding=(13, 10))
        source.grid(row=0, column=0, sticky="nsew")
        source.columnconfigure(0, weight=1)
        source.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(source, style="Paper.TFrame")
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="待转换图书", style="Section.TLabel").pack(side="left")
        ttk.Label(toolbar, textvariable=self.input_count, style="Meta.TLabel").pack(side="left", padx=(10, 0))
        self.button(toolbar, "移除所选", self.remove_selected).pack(side="right")
        self.button(toolbar, "添加文件夹…", self.add_folder).pack(side="right", padx=(0, 7))
        self.button(toolbar, "添加 EPUB…", self.add_files).pack(side="right", padx=(0, 7))

        self.tree = ttk.Treeview(source, columns=("book", "state"), show="headings", height=3, selectmode="extended")
        self.tree.heading("book", text="文件名", anchor="w")
        self.tree.heading("state", text="状态", anchor="w")
        self.tree.column("book", width=475, minwidth=290, stretch=True)
        self.tree.column("state", width=128, minwidth=100, stretch=False)
        self.tree.tag_configure("done", foreground=SUCCESS)
        self.tree.tag_configure("failed", foreground=ERROR)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(source, command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<Delete>", lambda _event: self.remove_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_buttons())

        output = ttk.Frame(workspace, style="Paper.TFrame", padding=(13, 10))
        output.grid(row=1, column=0, sticky="ew", pady=10)
        output.columnconfigure(1, weight=1)
        ttk.Label(output, text="输出目录", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 11))
        entry = ttk.Entry(output, textvariable=self.output)
        entry.grid(row=0, column=1, sticky="ew")
        self.controls.append(entry)
        self.button(output, "浏览…", self.choose_output).grid(row=0, column=2, padx=(8, 0))
        mode = ttk.Frame(output, style="Paper.TFrame")
        mode.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(mode, text="输出方式", style="Section.TLabel").pack(side="left", padx=(0, 12))
        for label, split in (("按章分成多份 TXT", True), ("整本合成一份 TXT", False)):
            radio = ttk.Radiobutton(mode, text=label, variable=self.split, value=split)
            radio.pack(side="left", padx=(0, 13))
            self.controls.append(radio)
        checkbox = ttk.Checkbutton(mode, text="保存清洗记录", variable=self.audit)
        checkbox.pack(side="right")
        self.controls.append(checkbox)

        log_card = ttk.Frame(workspace, style="Paper.TFrame", padding=(13, 9))
        log_card.grid(row=2, column=0, sticky="nsew")
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, text="转换记录", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log = tk.Text(log_card, height=3, wrap="word", state="disabled", background=PAPER,
                           foreground=INK, relief="flat", highlightthickness=0, font=(FONT, 9), padx=2, pady=1)
        self.log.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_card, command=self.log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.tag_configure("failed", foreground=ERROR)
        self.log.tag_configure("done", foreground=SUCCESS)

        footer = ttk.Frame(workspace, style="Page.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(11, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(footer, orient="horizontal", mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", padx=(0, 15), pady=(5, 0))
        self.preview = ttk.Button(footer, text="预览分段", command=lambda: self.start(True))
        self.preview.grid(row=0, column=1, rowspan=2, padx=(0, 7))
        ttk.Button(footer, text="打开输出目录", command=self.open_output).grid(row=0, column=2, rowspan=2, padx=(0, 7))
        self.start_button = ttk.Button(footer, text="开始转换", command=lambda: self.start(False), style="Primary.TButton")
        self.start_button.grid(row=0, column=3, rowspan=2)
        self.update_buttons()

    def choose_files_base(self):
        newest = next(reversed(self.sources.values()), None) if self.sources else None
        return str(newest.parent) if newest is not None else str(documents_folder())

    def add_files(self):
        files = filedialog.askopenfilenames(parent=self.root, title="选择 EPUB 图书",
                                           initialdir=self.choose_files_base(),
                                           filetypes=[("EPUB 图书", "*.epub")])
        self.add_paths(Path(file) for file in files)

    def add_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择包含 EPUB 的文件夹",
                                         initialdir=self.choose_files_base(), mustexist=True)
        if folder:
            try:
                matches = sorted(file for file in Path(folder).iterdir() if file.is_file() and file.suffix.lower() == ".epub")
                if matches:
                    self.add_paths(matches)
                else:
                    self.status.set("所选文件夹中没有 EPUB 文件。")
            except OSError as error:
                messagebox.showerror(TITLE, str(error), parent=self.root)

    def add_paths(self, paths):
        added = []
        for candidate in paths:
            path = candidate.resolve()
            if path.suffix.lower() != ".epub" or path in self.row_by_path:
                continue
            row = self.tree.insert("", "end", values=(path.name, "待转换"))
            self.sources[row] = path
            self.row_by_path[path] = row
            added.append(row)
        if added:
            self.tree.selection_set(added[-1])
            self.tree.see(added[-1])
            self.status.set(f"已添加 {len(added)} 本 EPUB。")
        self.update_buttons()

    def remove_selected(self):
        if not self.running:
            for row in self.tree.selection():
                self.row_by_path.pop(self.sources.pop(row))
                self.tree.delete(row)
            self.update_buttons()

    def choose_output(self):
        proposed = Path(self.output.get().strip()).expanduser()
        directory = next((folder for folder in (proposed, *proposed.parents) if folder.is_dir()), documents_folder())
        folder = filedialog.askdirectory(parent=self.root, title="选择朗读稿输出目录", initialdir=str(directory))
        if folder:
            self.output.set(folder)

    def open_output(self):
        directory = Path(self.output.get().strip()).expanduser()
        if directory.is_absolute() and directory.is_dir():
            try:
                os.startfile(str(directory))
            except OSError as error:
                messagebox.showerror(TITLE, str(error), parent=self.root)
        else:
            self.status.set("输出目录尚未创建。转换成功后可直接打开。")

    def update_buttons(self):
        self.input_count.set(f"{len(self.sources)} 本")
        for control in self.controls:
            control.state(["disabled"] if self.running else ["!disabled"])
        selected_one = len(self.tree.selection()) == 1 or len(self.sources) == 1
        self.preview.state(["!disabled"] if selected_one and not self.running else ["disabled"])
        can_start = bool(self.sources) and (not self.running or not self.stop.is_set())
        self.start_button.state(["!disabled"] if can_start else ["disabled"])

    def start(self, preview):
        if self.running:
            if not preview:
                self.stop.set()
                self.status.set("当前图书完成后停止。")
                self.start_button.state(["disabled"])
            return
        if not self.pandoc:
            messagebox.showerror(TITLE, "程序内的 Pandoc 组件缺失。请从原便携包重新解压。", parent=self.root)
            return
        if preview and len(self.sources) != 1:
            selected = self.tree.selection()
            if len(selected) != 1:
                return
            books = [self.sources[selected[0]]]
        else:
            books = list(self.sources.values())
        if not books:
            return
        folder = Path(self.output.get().strip()).expanduser()
        if not preview and not folder.is_absolute():
            messagebox.showerror(TITLE, "请用「浏览」选择一个有效的输出目录。", parent=self.root)
            return
        self.running = True
        self.previewing = preview
        self.stop.clear()
        self.progress.configure(value=0, maximum=len(books))
        self.status.set("正在读取 EPUB……")
        self.start_button.configure(text="停止队列")
        self.update_buttons()
        if not preview:
            for source in books:
                self.set_row(source, "待转换")
        worker = Thread(target=process_books, args=(books, folder, preview or self.split.get(), self.audit.get(),
                                                    self.pandoc, preview, self.stop, self.updates.put), daemon=False)
        worker.start()

    def set_row(self, source, status, tag=""):
        row = self.row_by_path.get(source)
        if row:
            self.tree.item(row, values=(source.name, status), tags=(tag,) if tag else ())

    def write_log(self, message, tag=""):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            while True:
                event = self.updates.get_nowait()
                self.handle(event)
                if self.close_after_work and not self.running:
                    self.root.destroy()
                    return
        except Empty:
            pass
        self.root.after(85, self.poll)

    def handle(self, event):
        if event.kind == "working":
            action = "正在预览" if self.previewing else "正在转换"
            self.status.set(f"{action}　{event.source.stem[:24]}")
            if not self.previewing:
                self.set_row(event.source, "转换中")
        elif event.kind == "progress":
            self.status.set(f"{event.detail}　{event.source.stem[:24]}")
        elif event.kind in {"done", "skipped", "failed"}:
            state = {"done": "已完成", "skipped": "已存在，跳过", "failed": "失败"}[event.kind]
            if not self.previewing:
                self.set_row(event.source, state, event.kind)
            self.write_log(f"{state}　{event.source.name}\n{event.detail}", event.kind)
            self.progress["value"] += 1
        elif event.kind == "preview":
            self.progress["value"] += 1
            if not self.close_after_work:
                self.show_preview(event.report)
        else:
            self.status.set(event.detail)
            self.running = False
            self.previewing = False
            self.start_button.configure(text="开始转换")
            self.update_buttons()

    def show_preview(self, report):
        window = tk.Toplevel(self.root)
        window.title("分段预览")
        window.transient(self.root)
        window.minsize(590, 370)
        body = ttk.Frame(window, style="Paper.TFrame", padding=15)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text=f"{report['书名']}　{len(report['分段'])} 份 TXT", style="Section.TLabel",
                  wraplength=590).grid(row=0, column=0, sticky="w", pady=(0, 9))
        tree = ttk.Treeview(body, columns=("name", "kind", "size"), show="headings", height=10)
        for name, label, width in (("name", "文件名", 380), ("kind", "类型", 86), ("size", "字符数", 83)):
            tree.heading(name, text=label, anchor="w")
            tree.column(name, width=width, minwidth=width - 15, stretch=name == "name")
        for part in report["分段"]:
            tree.insert("", "end", values=(part["文件"], part["类型"], f"{part['字符数']:,}"))
        tree.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, command=tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        ttk.Button(body, text="关闭", command=window.destroy).grid(row=2, column=0, sticky="e", pady=(10, 0))
        window.bind("<Escape>", lambda _event: window.destroy())

    def close(self):
        if self.running:
            if messagebox.askyesno(TITLE, "当前还有图书在处理。要在当前图书完成后退出吗？", parent=self.root):
                self.close_after_work = True
                self.stop.set()
                self.status.set("当前图书完成后退出。")
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    NarrationWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
