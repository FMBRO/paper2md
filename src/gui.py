"""Tkinter desktop interface for the paper2md batch pipeline."""
from __future__ import annotations

import math
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from src.acquisition import parse_input
from src.batch_convert import run_batch
from src.config import Settings, load_settings
from src.job_store import JobStore
from src.pipeline_events import PipelineEvent
from src.pipeline import PipelineService
from src.research_models import InputKind, InputSpec, JobRecord

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


@dataclass(frozen=True)
class PipelineGuiOptions:
    """Display-independent values from the research-pipeline form."""

    input_kind: str = InputKind.ARXIV.value
    input_value: str = ""
    attachment_key: str = ""
    research_interest: str = ""
    max_cost_usd: str = "0.50"
    only_unprocessed: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> "PipelineGuiOptions":
        return cls(max_cost_usd=f"{settings.openrouter.paper_budget_usd:g}")


@dataclass(frozen=True)
class PipelineRunFinished:
    jobs: tuple[JobRecord, ...]


@dataclass(frozen=True)
class PipelineStatusFinished:
    job: JobRecord


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DiagnosticsFinished:
    checks: tuple[DiagnosticCheck, ...]


@dataclass(frozen=True)
class PipelineRunFailed:
    error: str


@dataclass
class ResearchViewModel:
    """Display-independent projection of research worker messages."""

    job_id: str = ""
    stage: str = ""
    status: str = "Ready"
    actual_cost: str = "$0.000000"
    artifact_dir: str = ""
    logs: list[str] = field(default_factory=list)

    @property
    def can_resume(self) -> bool:
        return bool(self.job_id) and self.stage in {
            "needs_input", "budget_exceeded", "failed", "completed",
        }

    def apply(self, item: QueueItem) -> None:
        if isinstance(item, PipelineEvent):
            if item.pdf_name:
                self.job_id = item.pdf_name
            if item.stage:
                self.stage = item.stage
                self.status = "Running"
            if item.stage:
                self.logs.append(f"[{item.stage}] {item.message or 'Started'}")
            elif item.message:
                self.logs.append(item.message)
            return
        if isinstance(item, (PipelineRunFinished, PipelineStatusFinished)):
            jobs = item.jobs if isinstance(item, PipelineRunFinished) else (item.job,)
            if not jobs:
                self.status = "No jobs matched"
                return
            job = jobs[-1]
            self.job_id = job.id
            self.stage = job.state.value
            self.actual_cost = f"${job.total_cost_usd:.6f}"
            self.artifact_dir = str(job.artifact_dir or "")
            self.status = job.state.value + (f": {job.error}" if job.error else "")
            if len(jobs) > 1:
                self.logs.append(f"Collection finished: {len(jobs)} jobs")
            return
        if isinstance(item, DiagnosticsFinished):
            for check in item.checks:
                self.logs.append(
                    f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}"
                )
            self.status = (
                "Diagnostics: all checks passed"
                if all(check.ok for check in item.checks)
                else "Diagnostics: attention needed"
            )
            return
        if isinstance(item, RunFailed):
            self.status = f"Failed: {item.error}"
            self.logs.append(self.status)
        if isinstance(item, PipelineRunFailed):
            self.status = f"Failed: {item.error}"
            self.logs.append(self.status)


QueueItem = (
    PipelineEvent | RunFinished | RunFailed | PipelineRunFinished
    | PipelineStatusFinished | DiagnosticsFinished | PipelineRunFailed
)
BatchRunner = Callable[[Settings, Callable[[PipelineEvent], None] | None], dict]
PipelineFactory = Callable[
    [Settings, Callable[[PipelineEvent], None] | None], PipelineService
]


def drain_gui_events(
    batch_queue: "queue.Queue[QueueItem]",
    research_queue: "queue.Queue[QueueItem]",
    on_batch: Callable[[QueueItem], None],
    on_research: Callable[[QueueItem], None],
) -> None:
    """Drain source-owned queues without guessing an event's producer."""
    for source, handler in (
        (batch_queue, on_batch),
        (research_queue, on_research),
    ):
        while True:
            try:
                handler(source.get_nowait())
            except queue.Empty:
                break


def create_pipeline_service(
    settings: Settings,
    on_event: Callable[[PipelineEvent], None] | None = None,
) -> PipelineService:
    """Compose production pipeline clients at the GUI/CLI boundary."""
    return PipelineService(settings, on_event=on_event)


def load_gui_options(config_path: Path | str = DEFAULT_CONFIG) -> GuiOptions:
    """Load GUI defaults without changing the YAML file."""
    path = Path(config_path)
    if path.exists():
        return GuiOptions.from_settings(load_settings(path))
    return GuiOptions(input_dir="input", output_dir="output")


def build_pipeline_input(
    options: PipelineGuiOptions,
    *,
    default_research_interest: str = "",
) -> tuple[InputSpec, float]:
    """Validate research form values and map them to the service contract."""
    try:
        kind = InputKind(options.input_kind)
    except ValueError as error:
        raise ValueError(f"Unsupported pipeline input type: {options.input_kind}") from error
    source = options.input_value.strip()
    if not source:
        raise ValueError("Pipeline input value cannot be empty")
    try:
        budget = float(options.max_cost_usd)
    except ValueError as error:
        raise ValueError("Budget must be a non-negative finite number") from error
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("Budget must be a non-negative finite number")
    interest = options.research_interest.strip() or default_research_interest.strip()
    attachment_key = options.attachment_key.strip().upper() or None
    if kind is InputKind.LOCAL_PDF:
        local_path = Path(source).expanduser()
        if local_path.suffix.lower() != ".pdf":
            raise ValueError("Local pipeline input must be a PDF path")
        spec = InputSpec(
            kind, str(local_path.resolve()), attachment_key, interest or None,
        )
    else:
        parse_source = (
            f"collection:{source}"
            if kind is InputKind.ZOTERO_COLLECTION and ":" not in source
            else source
        )
        spec = parse_input(
            parse_source,
            attachment_key=attachment_key,
            research_interest=interest or None,
        )
        if spec.kind is not kind:
            raise ValueError(
                f"Input value is {spec.kind.value}, not selected type {kind.value}"
            )
    return spec, budget


def run_pipeline_diagnostics(
    settings: Settings,
    *,
    service_factory: PipelineFactory = create_pipeline_service,
    environ: Mapping[str, str] = os.environ,
) -> tuple[DiagnosticCheck, ...]:
    """Probe configured boundaries without conversion, writes, or paid requests."""
    openrouter_check = DiagnosticCheck(
        "OpenRouter",
        bool(environ.get("OPENROUTER_API_KEY")),
        (
            "API key configured; no paid request sent"
            if environ.get("OPENROUTER_API_KEY")
            else "OPENROUTER_API_KEY is not configured"
        ),
    )
    try:
        _verify_local_storage(settings)
        service = service_factory(settings, None)
    except Exception as error:
        blocked = "Not checked: local storage unavailable"
        return (
            DiagnosticCheck("Local storage", False, str(error)),
            openrouter_check,
            DiagnosticCheck("Zotero local API", False, blocked),
            DiagnosticCheck("Notion data source", False, blocked),
        )
    checks = [
        DiagnosticCheck("Local storage", True, str(settings.state_path)),
        openrouter_check,
    ]
    try:
        zotero_ok = bool(service.zotero.is_available())
        checks.append(DiagnosticCheck(
            "Zotero local API", zotero_ok,
            settings.zotero.base_url if zotero_ok else "Zotero is not reachable",
        ))
    except Exception as error:
        checks.append(DiagnosticCheck("Zotero local API", False, str(error)))
    try:
        service.notion.validate_schema()
        checks.append(DiagnosticCheck(
            "Notion data source", True, "Schema valid; no content written",
        ))
    except Exception as error:
        checks.append(DiagnosticCheck("Notion data source", False, str(error)))
    return tuple(checks)


def _verify_local_storage(settings: Settings) -> None:
    """Create/write/read local output and create/open/check the SQLite store."""
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    handle, probe_name = tempfile.mkstemp(
        prefix=".paper2md-diagnostic-", dir=settings.output_dir,
    )
    probe = Path(probe_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(b"paper2md storage diagnostic")
            stream.flush()
            os.fsync(stream.fileno())
        if probe.read_bytes() != b"paper2md storage diagnostic":
            raise OSError("Local output storage failed its write/read check")
    finally:
        probe.unlink(missing_ok=True)
    JobStore(settings.state_path)
    with sqlite3.connect(settings.state_path) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()
    if result is None or result[0] != "ok":
        raise OSError("Local SQLite state failed its integrity check")


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


class PipelineRunController:
    """Run research-pipeline operations on one worker and publish queue items."""

    def __init__(
        self,
        event_queue: "queue.Queue[QueueItem]",
        *,
        service_factory: PipelineFactory = create_pipeline_service,
        diagnostics_runner: Callable[..., tuple[DiagnosticCheck, ...]] = (
            run_pipeline_diagnostics
        ),
    ) -> None:
        self.event_queue = event_queue
        self.service_factory = service_factory
        self.diagnostics_runner = diagnostics_runner
        self._running = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _start(self, target: Callable[[], None]) -> None:
        with self._lock:
            if self._running:
                raise RuntimeError("A research pipeline operation is already running")
            self._running = True
        self._thread = threading.Thread(
            target=self._run,
            args=(target,),
            name="paper2md-research-worker",
            daemon=True,
        )
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def start_ingest(
        self,
        settings: Settings,
        spec: InputSpec,
        max_cost_usd: float,
        *,
        only_unprocessed: bool = False,
    ) -> None:
        def operation() -> None:
            service = self.service_factory(settings, self.event_queue.put)
            if spec.kind is InputKind.ZOTERO_COLLECTION:
                jobs = service.ingest_collection(
                    spec,
                    max_cost_usd=max_cost_usd,
                    only_unprocessed=only_unprocessed,
                )
            else:
                if only_unprocessed:
                    raise ValueError(
                        "Only-unprocessed requires a Zotero collection"
                    )
                jobs = [service.ingest(spec, max_cost_usd)]
            self.event_queue.put(PipelineRunFinished(tuple(jobs)))

        self._start(operation)

    def start_resume(
        self,
        settings: Settings,
        job_id: str,
        *,
        max_cost_usd: float | None = None,
        attachment_key: str | None = None,
        research_interest: str | None = None,
    ) -> None:
        def operation() -> None:
            service = self.service_factory(settings, self.event_queue.put)
            job = service.resume(
                job_id,
                max_cost_usd,
                attachment_key=attachment_key,
                research_interest=research_interest,
            )
            self.event_queue.put(PipelineRunFinished((job,)))

        self._start(operation)

    def start_status(self, settings: Settings, job_id: str) -> None:
        def operation() -> None:
            service = self.service_factory(settings, self.event_queue.put)
            self.event_queue.put(PipelineStatusFinished(service.status(job_id)))

        self._start(operation)

    def start_diagnostics(self, settings: Settings) -> None:
        def operation() -> None:
            checks = self.diagnostics_runner(
                settings, service_factory=self.service_factory,
            )
            self.event_queue.put(DiagnosticsFinished(tuple(checks)))

        self._start(operation)

    def _run(self, operation: Callable[[], None]) -> None:
        try:
            operation()
        except Exception as error:
            self.event_queue.put(PipelineRunFailed(str(error)))
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
        self.root.minsize(960, 720)
        self.batch_events: "queue.Queue[QueueItem]" = queue.Queue()
        self.research_events: "queue.Queue[QueueItem]" = queue.Queue()
        self.controller = BatchRunController(self.batch_events)
        self.pipeline_controller = PipelineRunController(self.research_events)
        self.research_model = ResearchViewModel()
        self._is_running = False
        self._pipeline_running = False
        self._row_ids: dict[str, str] = {}
        self._interactive_widgets: list[object] = []
        self._pipeline_widgets: list[object] = []

        try:
            configured_settings = load_settings(DEFAULT_CONFIG)
            defaults = GuiOptions.from_settings(configured_settings)
            pipeline_defaults = PipelineGuiOptions.from_settings(configured_settings)
            startup_warning = None
        except Exception as error:
            defaults = GuiOptions(input_dir="input", output_dir="output")
            pipeline_defaults = PipelineGuiOptions()
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
        self.pipeline_kind_var = tk.StringVar(value=InputKind.ARXIV.value)
        self.pipeline_input_var = tk.StringVar()
        self.pipeline_attachment_var = tk.StringVar()
        self.pipeline_interest_var = tk.StringVar()
        self.pipeline_budget_var = tk.StringVar(value=pipeline_defaults.max_cost_usd)
        self.pipeline_only_unprocessed_var = tk.BooleanVar(value=False)
        self.pipeline_job_id_var = tk.StringVar()
        self.pipeline_stage_var = tk.StringVar(value="-")
        self.pipeline_cost_var = tk.StringVar(value="$0.000000")
        self.pipeline_status_var = tk.StringVar(value="Ready")
        self.pipeline_artifact_var = tk.StringVar(value="-")

        self._build_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_queue)
        if startup_warning:
            self._append_log(startup_warning)

    def _build_layout(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=0, column=0, sticky="nsew")
        legacy_tab = ttk.Frame(notebook)
        research_tab = ttk.Frame(notebook)
        notebook.add(legacy_tab, text="Batch conversion")
        notebook.add(research_tab, text="Research pipeline")
        legacy_tab.columnconfigure(0, weight=1)
        legacy_tab.rowconfigure(1, weight=1)
        legacy_tab.rowconfigure(2, weight=1)

        settings_frame = ttk.LabelFrame(legacy_tab, text="Conversion settings", padding=10)
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

        list_frame = ttk.LabelFrame(legacy_tab, text="PDF files", padding=8)
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

        log_frame = ttk.LabelFrame(legacy_tab, text="Live log", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, wrap="word", state="disabled"
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        bottom = ttk.Frame(legacy_tab, padding=(10, 5, 10, 10))
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

        self._build_research_layout(research_tab)
        self._refresh_pdf_list()

    def _build_research_layout(self, parent: object) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        form = ttk.LabelFrame(parent, text="Paper research pipeline", padding=10)
        form.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Input type").grid(row=0, column=0, sticky="w")
        kind = ttk.Combobox(
            form,
            textvariable=self.pipeline_kind_var,
            values=tuple(value.value for value in InputKind),
            state="readonly",
            width=22,
        )
        kind.grid(row=0, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(form, text="Input value").grid(row=1, column=0, sticky="w")
        source = ttk.Entry(form, textvariable=self.pipeline_input_var)
        source.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        browse = ttk.Button(form, text="Browse PDF...", command=self._choose_pipeline_pdf)
        browse.grid(row=1, column=2, pady=3)

        ttk.Label(form, text="Attachment key").grid(row=2, column=0, sticky="w")
        attachment = ttk.Entry(form, textvariable=self.pipeline_attachment_var)
        attachment.grid(row=2, column=1, sticky="ew", padx=8, pady=3)
        ttk.Label(
            form, text="Use when a Zotero item has multiple PDF attachments"
        ).grid(row=2, column=2, sticky="w")

        ttk.Label(form, text="Research interest").grid(row=3, column=0, sticky="w")
        interest = ttk.Entry(form, textvariable=self.pipeline_interest_var)
        interest.grid(row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=3)

        ttk.Label(form, text="Maximum cost (USD)").grid(row=4, column=0, sticky="w")
        budget = ttk.Entry(form, textvariable=self.pipeline_budget_var, width=12)
        budget.grid(row=4, column=1, sticky="w", padx=8, pady=3)
        only_unprocessed = ttk.Checkbutton(
            form,
            text="Collection: only unprocessed items",
            variable=self.pipeline_only_unprocessed_var,
        )
        only_unprocessed.grid(row=4, column=2, sticky="w")

        ttk.Label(form, text="Job ID").grid(row=5, column=0, sticky="w")
        job_id = ttk.Entry(form, textvariable=self.pipeline_job_id_var)
        job_id.grid(row=5, column=1, columnspan=2, sticky="ew", padx=8, pady=3)

        actions = ttk.Frame(parent, padding=(10, 5))
        actions.grid(row=1, column=0, sticky="ew")
        self.pipeline_start_button = ttk.Button(
            actions, text="Start pipeline", command=self._start_pipeline,
        )
        self.pipeline_start_button.grid(row=0, column=0, padx=(0, 8))
        self.pipeline_resume_button = ttk.Button(
            actions, text="Resume", command=self._resume_pipeline,
        )
        self.pipeline_resume_button.grid(row=0, column=1, padx=(0, 8))
        self.pipeline_status_button = ttk.Button(
            actions, text="Refresh status", command=self._pipeline_status,
        )
        self.pipeline_status_button.grid(row=0, column=2, padx=(0, 8))
        self.pipeline_diagnostics_button = ttk.Button(
            actions, text="Run diagnostics", command=self._pipeline_diagnostics,
        )
        self.pipeline_diagnostics_button.grid(row=0, column=3)

        results = ttk.LabelFrame(parent, text="Job status and diagnostics", padding=8)
        results.grid(row=2, column=0, sticky="nsew", padx=10, pady=(5, 10))
        results.columnconfigure(1, weight=1)
        results.rowconfigure(4, weight=1)
        ttk.Label(results, text="Stage").grid(row=0, column=0, sticky="nw")
        ttk.Label(results, textvariable=self.pipeline_stage_var).grid(
            row=0, column=1, sticky="nw",
        )
        ttk.Label(results, text="Actual cost").grid(row=1, column=0, sticky="nw")
        ttk.Label(results, textvariable=self.pipeline_cost_var).grid(
            row=1, column=1, sticky="nw",
        )
        ttk.Label(results, text="Status").grid(row=2, column=0, sticky="nw")
        ttk.Label(results, textvariable=self.pipeline_status_var).grid(
            row=2, column=1, sticky="nw",
        )
        ttk.Label(results, text="Artifacts").grid(row=3, column=0, sticky="nw")
        ttk.Label(results, textvariable=self.pipeline_artifact_var).grid(
            row=3, column=1, sticky="nw",
        )
        self.pipeline_log_text = scrolledtext.ScrolledText(
            results, height=14, wrap="word", state="disabled",
        )
        self.pipeline_log_text.grid(
            row=4, column=0, columnspan=2, sticky="nsew", pady=(8, 0),
        )

        self._pipeline_widgets.extend((
            kind, source, browse, attachment, interest, budget, only_unprocessed,
            job_id, self.pipeline_start_button, self.pipeline_resume_button,
            self.pipeline_status_button, self.pipeline_diagnostics_button,
        ))

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

    def _choose_pipeline_pdf(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select paper PDF",
            filetypes=(("PDF files", "*.pdf"), ("All files", "*.*")),
        )
        if selected:
            self.pipeline_kind_var.set(InputKind.LOCAL_PDF.value)
            self.pipeline_input_var.set(selected)

    def _pipeline_options(self) -> PipelineGuiOptions:
        return PipelineGuiOptions(
            input_kind=self.pipeline_kind_var.get(),
            input_value=self.pipeline_input_var.get(),
            attachment_key=self.pipeline_attachment_var.get(),
            research_interest=self.pipeline_interest_var.get(),
            max_cost_usd=self.pipeline_budget_var.get(),
            only_unprocessed=self.pipeline_only_unprocessed_var.get(),
        )

    def _pipeline_settings(self) -> Settings:
        return load_settings(DEFAULT_CONFIG)

    def _start_pipeline(self) -> None:
        if self._pipeline_running:
            return
        try:
            settings = self._pipeline_settings()
            options = self._pipeline_options()
            spec, budget = build_pipeline_input(
                options,
                default_research_interest=settings.research_interest,
            )
            self.pipeline_controller.start_ingest(
                settings, spec, budget,
                only_unprocessed=options.only_unprocessed,
            )
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot start pipeline", str(error), parent=self.root)
            return
        self.research_model = ResearchViewModel(status="Running")
        self._set_pipeline_running(True)
        self._render_research_model()

    def _resume_pipeline(self) -> None:
        if self._pipeline_running:
            return
        job_id = self.pipeline_job_id_var.get().strip()
        if not job_id:
            messagebox.showerror("Cannot resume", "Job ID cannot be empty", parent=self.root)
            return
        try:
            settings = self._pipeline_settings()
            options = self._pipeline_options()
            try:
                budget = float(options.max_cost_usd)
            except ValueError as error:
                raise ValueError("Budget must be a non-negative finite number") from error
            if not math.isfinite(budget) or budget < 0:
                raise ValueError("Budget must be a non-negative finite number")
            self.pipeline_controller.start_resume(
                settings,
                job_id,
                max_cost_usd=budget,
                attachment_key=options.attachment_key.strip() or None,
                research_interest=options.research_interest.strip() or None,
            )
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot resume", str(error), parent=self.root)
            return
        self.research_model.status = "Running"
        self._set_pipeline_running(True)
        self._render_research_model()

    def _pipeline_status(self) -> None:
        if self._pipeline_running:
            return
        job_id = self.pipeline_job_id_var.get().strip()
        if not job_id:
            messagebox.showerror(
                "Cannot load status", "Job ID cannot be empty", parent=self.root,
            )
            return
        try:
            self.pipeline_controller.start_status(self._pipeline_settings(), job_id)
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot load status", str(error), parent=self.root)
            return
        self.research_model.status = "Loading status"
        self._set_pipeline_running(True)
        self._render_research_model()

    def _pipeline_diagnostics(self) -> None:
        if self._pipeline_running:
            return
        try:
            self.pipeline_controller.start_diagnostics(self._pipeline_settings())
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("Cannot run diagnostics", str(error), parent=self.root)
            return
        self.research_model.status = "Running diagnostics"
        self._set_pipeline_running(True)
        self._render_research_model()

    def _set_pipeline_running(self, running: bool) -> None:
        self._pipeline_running = running
        state = "disabled" if running else "normal"
        for widget in self._pipeline_widgets:
            widget.configure(state=state)
        if not running:
            # A readonly combobox must be restored explicitly.
            self._pipeline_widgets[0].configure(state="readonly")

    def _render_research_model(self) -> None:
        self.pipeline_job_id_var.set(self.research_model.job_id)
        self.pipeline_stage_var.set(self.research_model.stage or "-")
        self.pipeline_cost_var.set(self.research_model.actual_cost)
        self.pipeline_status_var.set(self.research_model.status)
        self.pipeline_artifact_var.set(self.research_model.artifact_dir or "-")
        self.pipeline_log_text.configure(state="normal")
        self.pipeline_log_text.delete("1.0", "end")
        if self.research_model.logs:
            self.pipeline_log_text.insert("end", "\n".join(self.research_model.logs) + "\n")
        self.pipeline_log_text.see("end")
        self.pipeline_log_text.configure(state="disabled")

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
        drain_gui_events(
            self.batch_events,
            self.research_events,
            self._handle_batch_queue_item,
            self._handle_research_queue_item,
        )
        self.root.after(100, self._drain_queue)

    def _handle_batch_queue_item(self, item: QueueItem) -> None:
        if isinstance(item, PipelineEvent):
            self._handle_event(item)
        elif isinstance(item, RunFinished):
            self._handle_finished(item.summary)
        elif isinstance(item, RunFailed):
            self._handle_worker_failure(item.error)

    def _handle_research_queue_item(self, item: QueueItem) -> None:
        self.research_model.apply(item)
        if not isinstance(item, PipelineEvent):
            self._set_pipeline_running(False)
        self._render_research_model()

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
        if self._is_running or self._pipeline_running:
            messagebox.showwarning(
                "Work in progress",
                "Wait for the current operation to finish before closing paper2md.",
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

