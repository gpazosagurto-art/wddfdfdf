import sys, os, re, json, traceback, subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton,
    QFileDialog, QLabel, QSpinBox, QCheckBox, QProgressBar, QTextEdit, QMessageBox,
    QAbstractItemView,
)

APP_NAME = "Batch BPM Converter"
CONFIG_FILE = "bpm_converter.config.json"

# -------------------------- Utilidades --------------------------

VALID_EXTS = {".wav", ".flac", ".aiff", ".aif", ".ogg", ".mp3", ".m4a"}
BPM_RANGE = (40, 220)

RE_BPM_TAGGED = re.compile(r"(?i)bpm[\s_\-]*([0-9]{2,3})(?!\d)")
RE_ANY_NUMBER_2_3 = re.compile(r"(?<!\d)(\d{2,3})(?!\d)")

def guess_bpm_from_name(name: str) -> Optional[int]:
    """Detecta BPM desde el nombre del archivo con heurística robusta."""
    base = Path(name).stem
    m = RE_BPM_TAGGED.search(base)
    if m:
        bpm = int(m.group(1))
        if BPM_RANGE[0] <= bpm <= BPM_RANGE[1]:
            return bpm
    nums = [int(n) for n in RE_ANY_NUMBER_2_3.findall(base)]
    candidates = [n for n in nums if BPM_RANGE[0] <= n <= BPM_RANGE[1]]
    if not candidates:
        return None
    keywords = ("loop", "drum", "beat", "kick", "snare", "hats", "perc", "groove")
    scored: List[Tuple[int, int]] = []
    for c in candidates:
        score = 0
        idx = base.lower().rfind(str(c))
        if idx >= 0:
            score += 2 * (len(base) - idx)
        if any(k in base.lower() for k in keywords):
            score += 50
        scored.append((score, c))
    scored.sort(key=lambda t: (t[0], base.rfind(str(t[1]))))
    return scored[-1][1]

def list_audio_files(paths: List[str]) -> List[Path]:
    out = []
    for p in paths:
        pth = Path(p)
        if pth.is_file() and pth.suffix.lower() in VALID_EXTS:
            out.append(pth.resolve())
        elif pth.is_dir():
            for f in pth.rglob("*"):
                if f.is_file() and f.suffix.lower() in VALID_EXTS:
                    out.append(f.resolve())
    seen = set()
    uniq = []
    for f in out:
        if f not in seen:
            uniq.append(f)
            seen.add(f)
    return uniq

# -------------------------- FFmpeg helpers --------------------------

def resource_path(rel: str) -> Path:
    """Soporta PyInstaller (_MEIPASS) y ejecución normal."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / rel

def which_ffmpeg() -> Optional[Path]:
    """
    Devuelve la ruta a ffmpeg si existe en:
    1) junto al exe/script      → ./ffmpeg.exe
    2) carpeta ./ffmpeg/        → ./ffmpeg/ffmpeg.exe
    3) carpeta ./assets/        → ./assets/ffmpeg.exe
    4) _MEIPASS (PyInstaller)   → (si existe) ffmpeg/ffmpeg.exe o ffmpeg.exe
    5) PATH del sistema
    """
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    names = ["ffmpeg.exe"] if os.name == "nt" else ["ffmpeg"]

    # 1) junto al exe/script
    for n in names:
        cand = exe_dir / n
        if cand.exists():
            return cand

    # 2) ./ffmpeg/
    for n in names:
        cand = exe_dir / "ffmpeg" / n
        if cand.exists():
            return cand

    # 3) ./assets/
    for n in names:
        cand = exe_dir / "assets" / n
        if cand.exists():
            return cand

    # 4) _MEIPASS (cuando PyInstaller onefile/onedir)
    base = Path(getattr(sys, "_MEIPASS", exe_dir))
    for n in names:
        cand = base / n
        if cand.exists():
            return cand
        cand2 = base / "ffmpeg" / n
        if cand2.exists():
            return cand2
        cand3 = base / "assets" / n
        if cand3.exists():
            return cand3

    # 5) PATH
    cmd = "where" if os.name == "nt" else "which"
    try:
        out = subprocess.check_output([cmd, "ffmpeg"], stderr=subprocess.STDOUT, text=True).strip()
        if out:
            return Path(out.splitlines()[0])
    except Exception:
        pass

    return None

def atempo_chain(ratio: float) -> str:
    """
    FFmpeg atempo admite [0.5, 2.0]. Para ratios fuera de ese rango,
    encadenamos varios atempo hasta cubrirlo.
    """
    if ratio <= 0:
        raise ValueError("ratio debe ser > 0")
    factors = []
    r = ratio
    while r > 2.0:
        factors.append(2.0)
        r /= 2.0
    while r < 0.5:
        factors.append(0.5)
        r /= 0.5
    if abs(r - 1.0) > 1e-6:
        factors.append(r)
    if not factors:
        factors = [1.0]
    return ",".join(f"atempo={f:.6f}" for f in factors)

def convert_with_ffmpeg(src: Path, dst: Path, ratio: float) -> Tuple[bool, str]:
    """Usa FFmpeg 'atempo' para cambiar tempo sin alterar el tono."""
    ffmpeg_path = which_ffmpeg()
    if not ffmpeg_path:
        return False, "FFmpeg no encontrado. (Ponlo junto al .exe o en ./ffmpeg/ffmpeg.exe o ./assets/ffmpeg.exe o en PATH)"

    dst.parent.mkdir(parents=True, exist_ok=True)
    filter_str = atempo_chain(ratio)

    out_args = []
    if dst.suffix.lower() in {".wav", ".aiff", ".aif"}:
        out_args = ["-c:a", "pcm_s24le"]

    cmd = [
        str(ffmpeg_path),
        "-hide_banner", "-loglevel", "error", "-y",
        "-i", src.as_posix(),
        "-filter:a", filter_str,
        *out_args,
        dst.as_posix(),
    ]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return True, "ok"
    except subprocess.CalledProcessError as e:
        msg = e.output.decode(errors="ignore") if isinstance(e.output, bytes) else str(e.output)
        return False, f"FFmpeg error:\n{msg}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

# ---------- SANITIZACIÓN DE NOMBRES (nuevo) ----------
_SANITIZE_STOPWORDS = re.compile(r"(?i)\b(drum|loop|full)\b")

def _sanitize_base(base: str) -> str:
    """
    - Quita números
    - Elimina palabras: drum/loop/full (case-insensitive)
    - Colapsa múltiples separadores
    - Elimina separadores finales y cualquier char final no alfabético
    - Asegura que no termine en '_' ni '-'
    """
    # 1) quitar números
    s = re.sub(r"\d+", "", base)

    # 2) normalizar separadores a espacios para borrar palabras por \b
    s_spaces = s.replace("_", " ").replace("-", " ")

    # 3) quitar stopwords
    s_spaces = _SANITIZE_STOPWORDS.sub("", s_spaces)

    # 4) colapsar espacios y volver a underscores
    s_spaces = re.sub(r"\s+", " ", s_spaces).strip()
    s = s_spaces.replace(" ", "_")

    # 5) colapsar underscores repetidos
    s = re.sub(r"_+", "_", s)

    # 6) eliminar cualquier sufijo no alfabético (incluye '_' o '-')
    s = re.sub(r"[^A-Za-z]+$", "", s)

    # 7) si quedó vacío, usar 'converted'
    if not s:
        s = "converted"

    return s

def safe_out_path(src: Path, out_dir: Path, target_bpm: int, flat_names: bool, suffix: str) -> Path:
    """
    Construye el nombre de salida SÓLO con base saneada + (opcional) suffix + misma extensión.
    - Sin '__to{bpm}bpm'
    - Evita dobles guiones bajos y terminaciones no alfabéticas
    """
    rel = src.name if flat_names else src.as_posix().replace(":", "").replace("/", "_")
    base = Path(rel).stem
    ext = src.suffix.lower()

    clean = _sanitize_base(base)

    # añadir etiqueta si viene (p.ej. "_mix"), evitando dobles '_'
    if suffix:
        # normalizamos el suffix por si tiene espacios o mayúsculas raras
        suf = suffix.strip()
        suf = suf.replace(" ", "_")
        suf = re.sub(r"_+", "_", suf)
        suf = re.sub(r"[^A-Za-z0-9_\-]+", "", suf)
        if suf:
            candidate = f"{clean}_{suf}"
            candidate = re.sub(r"_+", "_", candidate)
            candidate = re.sub(r"[^A-Za-z]+$", "", candidate)  # asegurar termina en letra
            clean = candidate if candidate else clean

    # asegurar que no termine en '_' ni '-'
    clean = clean.rstrip("_-")
    if not clean:
        clean = "converted"

    newname = f"{clean}{ext}"
    return (out_dir / newname).resolve()

# -------------------------- Worker --------------------------

class ConvertTask(QThread):
    progress = Signal(int)
    message = Signal(str)
    finished_ok = Signal(int, int)

    def __init__(self, files: List[Path], target_bpm: int, out_dir: Path,
                 overwrite: bool, flat_names: bool, suffix_label: str):
        super().__init__()
        self.files = files
        self.target_bpm = target_bpm
        self.out_dir = out_dir
        self.overwrite = overwrite
        self.flat_names = flat_names
        self.suffix_label = f"_{suffix_label}" if suffix_label else ""

    def run(self):
        processed = 0
        skipped = 0
        total = len(self.files)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        for i, f in enumerate(self.files, start=1):
            try:
                bpm = guess_bpm_from_name(f.name)
                if not bpm:
                    skipped += 1
                    self.message.emit(f"⏭️ {f.name}: no se detectó BPM en el nombre.")
                else:
                    ratio = self.target_bpm / bpm
                    outpath = safe_out_path(f, self.out_dir, self.target_bpm, self.flat_names, self.suffix_label)
                    if outpath.exists() and not self.overwrite:
                        skipped += 1
                        self.message.emit(f"⏭️ {f.name}: salida ya existe.")
                    else:
                        self.message.emit(f"🎚️ {f.name}: {bpm}→{self.target_bpm} (ratio {ratio:.6f})")
                        ok, msg = convert_with_ffmpeg(f, outpath, ratio)
                        if ok:
                            processed += 1
                            self.message.emit(f"✅ Guardado: {outpath.name}")
                        else:
                            skipped += 1
                            self.message.emit(f"❌ Error en {f.name}: {msg}")
            except Exception as e:
                skipped += 1
                self.message.emit(f"❌ Error en {f.name}: {e}")
                self.message.emit(traceback.format_exc())
            self.progress.emit(int(i * 100 / max(total, 1)))

        self.finished_ok.emit(processed, skipped)

# -------------------------- GUI --------------------------

class DropList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e: QDropEvent):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls()]
            self.parent().add_paths(paths)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(820, 640)

        # Widgets
        self.listw = DropList(self)
        self.btn_add_files = QPushButton("Agregar archivos")
        self.btn_add_folder = QPushButton("Agregar carpeta")
        self.btn_clear = QPushButton("Limpiar lista")
        self.btn_start = QPushButton("Convertir")
        self.btn_remove_sel = QPushButton("Eliminar selección")

        self.spin_bpm = QSpinBox()
        self.spin_bpm.setRange(BPM_RANGE[0], BPM_RANGE[1])
        self.spin_bpm.setValue(100)

        self.chk_overwrite = QCheckBox("Overwrite")
        self.chk_flat_names = QCheckBox("Usar nombre plano (ON) / Mantener árbol (OFF)")
        self.chk_flat_names.setChecked(True)

        self.lbl_outdir = QLabel("Carpeta de salida:")
        self.outdir_btn = QPushButton("Elegir…")
        self.outdir_label = QLabel(str(Path("output").resolve()))
        self.outdir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.lbl_suffix = QLabel("Etiqueta extra (opcional):")
        self.suffix_edit = QTextEdit()
        self.suffix_edit.setFixedHeight(30)

        self.progress = QProgressBar()
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        # Layout superior
        top = QHBoxLayout()
        top.addWidget(QLabel("BPM objetivo:"))
        top.addWidget(self.spin_bpm)
        top.addSpacing(20)
        top.addWidget(self.chk_overwrite)
        top.addSpacing(20)
        top.addWidget(self.chk_flat_names)
        top.addStretch()

        outrow = QHBoxLayout()
        outrow.addWidget(self.lbl_outdir)
        outrow.addWidget(self.outdir_label, 1)
        outrow.addWidget(self.outdir_btn)

        suffixrow = QHBoxLayout()
        suffixrow.addWidget(self.lbl_suffix)
        suffixrow.addWidget(self.suffix_edit, 1)

        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_add_files)
        buttons.addWidget(self.btn_add_folder)
        buttons.addWidget(self.btn_remove_sel)
        buttons.addStretch()
        buttons.addWidget(self.btn_clear)
        buttons.addWidget(self.btn_start)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(outrow)
        layout.addLayout(suffixrow)
        layout.addWidget(QLabel("Arrastra archivos o carpetas aquí:"))
        layout.addWidget(self.listw, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log, 2)

        footer = QLabel("© 2025 Gabriel Golker")
        footer.setAlignment(Qt.AlignCenter)
        footer.setStyleSheet("margin-top:8px; opacity:0.7;")
        layout.addWidget(footer)

        # Conexiones
        self.btn_add_files.clicked.connect(self.pick_files)
        self.btn_add_folder.clicked.connect(self.pick_folder)
        self.btn_clear.clicked.connect(self.listw.clear)
        self.btn_remove_sel.clicked.connect(self.remove_selected)
        self.btn_start.clicked.connect(self.start)
        self.outdir_btn.clicked.connect(self.choose_outdir)

        self.worker: Optional[ConvertTask] = None
        self.load_config()

        save_act = QAction("Guardar configuración", self)
        save_act.triggered.connect(self.save_config)
        self.addAction(save_act)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)

    # ---------- Config ----------
    def load_config(self):
        try:
            if Path(CONFIG_FILE).exists():
                data = json.load(open(CONFIG_FILE, "r", encoding="utf-8"))
                self.spin_bpm.setValue(int(data.get("target_bpm", 100)))
                self.chk_overwrite.setChecked(bool(data.get("overwrite", False)))
                self.chk_flat_names.setChecked(bool(data.get("flat_names", True)))
                self.outdir_label.setText(data.get("out_dir", str(Path("output").resolve())))
                self.suffix_edit.setPlainText(data.get("suffix", ""))
        except Exception:
            pass

    def save_config(self):
        data = {
            "target_bpm": self.spin_bpm.value(),
            "overwrite": self.chk_overwrite.isChecked(),
            "flat_names": self.chk_flat_names.isChecked(),
            "out_dir": self.outdir_label.text(),
            "suffix": self.suffix_edit.toPlainText().strip(),
        }
        json.dump(data, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
        self.log.append("💾 Configuración guardada.")

    # ---------- Files ----------
    def add_paths(self, paths: List[str]):
        files = list_audio_files(paths)
        for f in files:
            self.listw.addItem(f.as_posix())
        self.log.append(f"➕ Añadidos {len(files)} archivo(s).")

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Selecciona audios", "",
                                                "Audio files (*.wav *.flac *.aiff *.aif *.ogg *.mp3 *.m4a)")
        if files:
            self.add_paths(files)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Selecciona carpeta")
        if d:
            self.add_paths([d])

    def choose_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "Selecciona carpeta de salida")
        if d:
            self.outdir_label.setText(Path(d).resolve().as_posix())

    def remove_selected(self):
        for item in self.listw.selectedItems():
            self.listw.takeItem(self.listw.row(item))

    # ---------- Run ----------
    def start(self):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Ya hay una conversión en curso.")
            return

        files = [Path(self.listw.item(i).text()) for i in range(self.listw.count())]
        if not files:
            QMessageBox.information(self, APP_NAME, "Agrega algunos archivos o carpetas primero.")
            return

        target_bpm = self.spin_bpm.value()
        out_dir = Path(self.outdir_label.text())
        overwrite = self.chk_overwrite.isChecked()
        flat_names = self.chk_flat_names.isChecked()
        suffix = self.suffix_edit.toPlainText().strip()

        self.progress.setValue(0)
        self.log.append(f"🚀 Iniciando… Objetivo: {target_bpm} BPM. Archivos: {len(files)}")
        self.save_config()

        self.worker = ConvertTask(files, target_bpm, out_dir, overwrite, flat_names, suffix)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.message.connect(self.log.append)
        self.worker.finished_ok.connect(self.on_finished)
        self.worker.start()

    def on_finished(self, processed: int, skipped: int):
        self.log.append(f"🏁 Listo. Convertidos: {processed}, Omitidos: {skipped}")

def apply_dark_theme(app: QApplication):
    try:
        import qdarkstyle
        app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api='pyside6'))
    except Exception:
        pass

def main():
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    w = App()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()


