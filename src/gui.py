"""Tkinter desktop interface for the paper2md batch pipeline."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.batch_convert import run_batch
from src.config import Settings, load_settings
from src.pipeline_events import PipelineEvent

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except ImportError:  # pragma: no cover - depends on the Python distribution
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = scrolledtext = ttk = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "config.yaml"


@dataclass(frozen=True)
class GuiOptions:
    """Values editable in the desktop form."""

    input_dir: str
    output_dir: str
    enable_ocr: bool = True
    force_ocr: bool = False
    language: str = "eng"
    deskew: bool = True
    clean: bool = True
    skip_existing: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "GuiOptions":
        return cls(
            input_dir=str(settings.input_dir),
            output_dir=str(settings.output_dir),
            enable_ocr=settings.enable_ocr,
            force_ocr=settings.force_ocr,
            language=settings.language,
            deskew=settings.ocr_deskew,
            clean=settings.ocr_clean,
            skip_existing=settings.skip_existing,
        )


@dataclass(frozen=True)
class RunFinished:
    summary: dict


@dataclass(frozen=True)
class RunFailed:
    error: str


QueueItem = PipelineEvent | RunFinished | RunFailed
BatchRunner = Callable[[Settings, Callable[[PipelineEvent], None] | None], dict]


def load_gui_options(config_path: Path | str = DEFAULT_CONFIG) -> GuiOptions:
    """Load GUI defaults without changing the YAML file."""
    path = Path(config_path)
    if path.exists():
        return GuiOptions.from_settings(load_settings(path))
    return GuiOptions(input_dir="input", output_dir="output")


def list_input_pdfs(input_dir: Path | str) -> list[Path]:
    """Validate and enumerate the GUI input folder."""
    path = Path(input_dir).expanduser()
    if not path.exists():
        raise ValueError(f"Input folder does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Input path is not a folder: {path}")
    pdfs = sorted(path.glob("*.pdf"))
    if not pdfs:
        raise ValueError(f"No PDF files found in: {path}")
    return pdfs


def build_settings(options: GuiOptions) -> Settings:
    """Validate form values and map them to the existing pipeline settings."""
    input_dir = Path(options.input_dir).expanduser()
    list_input_pdfs(input_dir)

    output_dir = Path(options.output_dir).expanduser()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"Output path is not a folder: {output_dir}")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ValueError(f"Cannot create output folder: {error}") from error

    language = options.language.strip()
    if not language:
        raise ValueError("OCR language cannot be empty")

    return Settings(
        input_dir=input_dir,
        output_dir=output_dir,
        engine="marker",
        language=language,
        enable_ocr=options.enable_ocr,
        force_ocr=options.force_ocr,
        ocr_deskew=options.deskew,
        ocr_clean=options.clean,
        workers=1,
        skip_existing=options.skip_existing,
        overwrite=False,
        continue_on_error=True,
        save_logs=True,
    )


def open_output_folder(path: Path | str) -> None:
    """Open an existing output folder using the platform file manager."""
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Output folder does not exist: {folder}")
    if sys.platform.startswith("win"):
        os.startfile(str(folder))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


class BatchRunController:
    """Run one batch on a worker thread and publish results to a queue."""

    def __init__(
        self,
        event_queue: "queue.Queue[QueueItem]",
        batch_runner: BatchRunner = run_batch,
    ) -> None:
        self.event_queue = event_queue
        self.batch_runner = batch_runner
        self._running = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, settings: Settings) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("A batch conversion is already running")
            self._running = True
        self._thread = threading.Thread(
            target=self._run,
            args=(settings,),
            name="paper2md-gui-worker",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the worker; intended for tests and orderly shutdown checks."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, settings: Settings) -> None:
        try:
            summary = self.batch_runner(settings, self.event_queue.put)
            self.event_queue.put(RunFinished(summary))
        except Exception as error:
            self.event_queue.put(RunFailed(str(error)))
        finally:
            with self._lock:
                self._running = False


class Paper2MdApp:
    """English-language Tkinter view for local batch conversion."""

    _STAGE_LABELS = {
        "inspection": "Inspecting",
        "ocr": "OCR",
        "conversion": "Converting",
        "post_processing": "Post-processing",
    }

    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title("paper2md")
        self.root.minsize(900, 680)
        self.events: "queue.Queue[QueueItem]" = queue.Queue()
        self.controller = BatchRunController(self.events)
        self._is_running = False
        self._row_ids: dict[str, str] = {}
        self._interactive_widgets: list[object] = []

        try:
            defaults = load_gui_options()
            startup_warning = None
        except Exception as error:
            defaults = GuiOptions(input_dir="input", output_dir="output")
            startup_warning = f"Could not load config defaults: {error}"

        self.input_var = tk.StringVar(value=defaults.input_dir)
        self.output_var = tk.StringVar(value=defaults.output_dir)
        self.enable_ocr_var = tk.BooleanVar(value=defaults.enable_ocr)
        self.force_ocr_var = tk.BooleanVar(value=defaults.force_ocr)
        self.language_var = tk.StringVar(value=defaults.language)
        self.deskew_var = tk.BooleanVar(value=defaults.deskew)
        self.clean_var = tk.BooleanVar(value=defaults.clean)
        self.skip_existing_var = tk.BooleanVar(value=defaults.skip_existing)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="Ready")

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)
        if startup_warning:
            self._append_log(startup_warning)

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.rowconfigure(2, weight=1)

        settings_frame = ttk.LabelFrame(self.root, text="Conversion settings", padding=10)
        settings_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        settings_frame.columnconfigure(1, weight=1)

        ttk.Label(settings_frame, text="Input folder").grid(row=0, column=0, sticky="w")
        input_entry = ttk.Entry(settings_frame, textvariable=self.input_var)
        input_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        input_entry.bind("<FocusOut>", lambda _event: self._refresh_pdf_list())
        input_button = ttk.Button(settings_frame, text="Browse...", command=self._choose_input)
        input_button.grid(row=0, column=2, pady=3)

        ttk.Label(settings_frame, text="Output folder").grid(row=1, column=0, sticky="w")
        output_entry = ttk.Entry(settings_frame, textvariable=self.output_var)
        output_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        output_button = ttk.Button(settings_frame, text="Browse...", command=self._choose_output)
        output_button.grid(row=1, column=2, pady=3)

        options_frame = ttk.Frame(settings_frame)
        options_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        enable_ocr = ttk.Checkbutton(
            options_frame, text="Auto OCR", variable=self.enable_ocr_var
        )
        force_ocr = ttk.Checkbutton(
            options_frame, text="Force OCR", variable=self.force_ocr_var
        )
        deskew = ttk.Checkbutton(options_frame, text="Deskew", variable=self.deskew_var)
        clean = ttk.Checkbutton(options_frame, text="Clean", variable=self.clean_var)
        skip_existing = ttk.Checkbutton(
            options_frame, text="Skip existing", variable=self.skip_existing_var
        )
        enable_ocr.grid(row=0, column=0, padx=(0, 12))
        force_ocr.grid(row=0, column=1, padx=(0, 12))
        deskew.grid(row=0, column=2, padx=(0, 12))
        clean.grid(row=0, column=3, padx=(0, 12))
        skip_existing.grid(row=0, column=4, padx=(0, 12))
        ttk.Label(options_frame, text="OCR language").grid(row=0, column=5, padx=(8, 4))
        language_entry = ttk.Entry(options_frame, textvariable=self.language_var, width=8)
        language_entry.grid(row=0, column=6)
        ttk.Label(options_frame, text="Engine: Marker  |  Workers: 1").grid(
            row=0, column=7, padx=(20, 0)
        )

        self._interactive_widgets.extend([
            input_entry,
            input_button,
            output_entry,
            output_button,
            enable_ocr,
            force_ocr,
            deskew,
            clean,
            skip_existing,
            language_entry,
        ])

        list_frame = ttk.LabelFrame(self.root, text="PDF files", padding=8)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.pdf_tree = ttk.Treeview(
            list_frame,
            columns=("file", "status", "stage"),
            show="headings",
            height=9,
        )
        self.pdf_tree.heading("file", text="File")
        self.pdf_tree.heading("status", text="Status")
        self.pdf_tree.heading("stage", text="Current stage")
        self.pdf_tree.column("file", width=500, stretch=True)
        self.pdf_tree.column("status", width=120, stretch=False)
        self.pdf_tree.column("stage", width=180, stretch=False)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.pdf_tree.yview)
        self.pdf_tree.configure(yscrollcommand=scrollbar.set)
        self.pdf_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.pdf_tree.tag_configure("success", foreground="#207227")
        self.pdf_tree.tag_configure("failed", foreground="#a02020")
        self.pdf_tree.tag_configure("skipped", foreground="#6b6b6b")

        log_frame = ttk.LabelFrame(self.root, text="Live log", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word", state="disabled"
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        bottom = ttk.Frame(self.root, padding=(10, 5, 10, 10))
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        progress = ttk.Progressbar(bottom, variable=self.progress_var, maximum=100)
        progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(bottom, textvariable=self.progress_text_var, width=32).grid(
            row=0, column=1, padx=(0, 10)
        )
        self.open_button = ttk.Button(
            bottom, text="Open Output Folder", command=self._open_output
        )
        self.open_button.grid(row=0, column=2, padx=(0, 8))
        self.start_button = ttk.Button(bottom, text="Start conversion", command=self._start)
        self.start_button.grid(row=0, column=3)

        self._refresh_pdf_list()

    def _options(self) -> GuiOptions:
        return GuiOptions(
            input_dir=self.input_var.get(),
            output_dir=self.output_var.get(),
            enable_ocr=self.enable_ocr_var.get(),
            force_ocr=self.force_ocr_var.get(),
            language=self.language_var.get(),
            deskew=self.deskew_var.get(),
            clean=self.clean_var.get(),
            skip_existing=self.skip_existing_var.get(),
        )

    def _choose_input(self) -> None:
        selected = filedialog.askdirectory(
            title="Select input folder", initialdir=self.input_var.get() or "."
        )
        if selected:
            self.input_var.set(selected)
            self._refresh_pdf_list()

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(
            title="Select output folder", initialdir=self.output_var.get() or "."
        )
        if selected:
            self.output_var.set(selected)

    def _refresh_pdf_list(self, pdfs: list[Path] | None = None) -> None:
        if pdfs is None:
            try:
                path = Path(self.input_var.get()).expanduser()
                pdfs = sorted(path.glob("*.pdf")) if path.is_dir() else []
            except OSError:
                pdfs = []
        self.pdf_tree.delete(*self.pdf_tree.get_children())
        self._row_ids.clear()
        for index, pdf in enumerate(pdfs):
            iid = f"pdf-{index}"
            self._row_ids[pdf.name] = iid
            self.pdf_tree.insert("", "end", iid=iid, values=(pdf.name, "Pending", ""))

    def _start(self) -> None:
        if self._is_running:
            return
        try:
            settings = build_settings(self._options())
            pdfs = list_input_pdfs(settings.input_dir)
        except ValueError as error:
            messagebox.showerror("Invalid settings", str(error), parent=self.root)
            return

        self._refresh_pdf_list(pdfs)
        self.progress_var.set(0)
        self.progress_text_var.set(f"0 / {len(pdfs)} completed")
        self._clear_log()
        try:
            self.controller.start(settings)
        except RuntimeError as error:
            messagebox.showerror("Cannot start", str(error), parent=self.root)
            return
        self._set_running(True)

    def _set_running(self, running: bool) -> None:
        self._is_running = running
        state = "disabled" if running else "normal"
        for widget in self._interactive_widgets:
            widget.configure(state=state)
        self.start_button.configure(state=state)

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self.events.get_nowait()
                if isinstance(item, PipelineEvent):
                    self._handle_event(item)
                elif isinstance(item, RunFinished):
                    self._handle_finished(item.summary)
                else:
                    self._handle_worker_failure(item.error)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _handle_event(self, event: PipelineEvent) -> None:
        if event.kind == "batch_started":
            self._append_log(event.message)
        elif event.kind == "pdf_started":
            self._update_row(event.pdf_name, "Pending", "Starting")
            self._set_progress_before_current(event)
        elif event.kind == "stage_changed":
            label = self._STAGE_LABELS.get(event.stage or "", event.stage or "")
            self._update_row(event.pdf_name, label, event.message)
            self._set_progress_before_current(event)
        elif event.kind == "log":
            prefix = f"[{event.pdf_name}] " if event.pdf_name else ""
            self._append_log(f"{prefix}{event.message}")
        elif event.kind == "pdf_succeeded":
            self._update_row(event.pdf_name, "Success", "Completed", "success")
            self._set_progress_complete(event)
        elif event.kind == "pdf_failed":
            self._update_row(event.pdf_name, "Failed", event.message, "failed")
            self._append_log(f"[{event.pdf_name}] Failed: {event.message}")
            self._set_progress_complete(event)
        elif event.kind == "pdf_skipped":
            self._update_row(event.pdf_name, "Skipped", event.message, "skipped")
            self._append_log(f"[{event.pdf_name}] {event.message}")
            self._set_progress_complete(event)
        elif event.kind == "batch_finished":
            self._append_log(event.message)

    def _update_row(
        self,
        pdf_name: str | None,
        status: str,
        stage: str,
        tag: str | None = None,
    ) -> None:
        if not pdf_name or pdf_name not in self._row_ids:
            return
        iid = self._row_ids[pdf_name]
        self.pdf_tree.item(
            iid,
            values=(pdf_name, status, stage),
            tags=(tag,) if tag else (),
        )
        self.pdf_tree.see(iid)

    def _set_progress_before_current(self, event: PipelineEvent) -> None:
        if event.current is None or not event.total:
            return
        completed = max(event.current - 1, 0)
        self.progress_var.set(completed / event.total * 100)
        self.progress_text_var.set(f"{completed} / {event.total} completed")

    def _set_progress_complete(self, event: PipelineEvent) -> None:
        if event.current is None or not event.total:
            return
        self.progress_var.set(event.current / event.total * 100)
        self.progress_text_var.set(f"{event.current} / {event.total} completed")

    def _handle_finished(self, summary: dict) -> None:
        self._set_running(False)
        self.progress_var.set(100)
        self.progress_text_var.set(
            f"Complete: {summary['success']} succeeded, {summary['failed']} failed"
        )
        messagebox.showinfo(
            "Batch complete",
            f"Succeeded: {summary['success']}\nFailed: {summary['failed']}",
            parent=self.root,
        )

    def _handle_worker_failure(self, error: str) -> None:
        self._set_running(False)
        self.progress_text_var.set("Batch aborted")
        self._append_log(f"Batch aborted: {error}")
        messagebox.showerror("Batch aborted", error, parent=self.root)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 5000:
            self.log_text.delete("1.0", "1001.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _open_output(self) -> None:
        try:
            open_output_folder(self.output_var.get())
        except (OSError, ValueError) as error:
            messagebox.showerror("Cannot open output folder", str(error), parent=self.root)

    def _on_close(self) -> None:
        if self._is_running:
            messagebox.showwarning(
                "Conversion in progress",
                "Wait for the current batch to finish before closing paper2md.",
                parent=self.root,
            )
            return
        self.root.destroy()


def main() -> int:
    if tk is None:
        print(
            "Tkinter is not available. Install Tk support for your Python distribution "
            "(for example, python3-tk on Debian/Ubuntu).",
            file=sys.stderr,
        )
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Could not start the desktop GUI: {error}", file=sys.stderr)
        return 1
    Paper2MdApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

