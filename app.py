"""Web app that compiles a zipped LaTeX project (e.g. an Overleaf export) into a PDF."""
import contextlib
import gc
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for

# Ensure logs flush immediately to stdout/stderr without buffering (critical for Google Cloud Run / Docker)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(line_buffering=True)

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger('ltexcompiler')

# --- Configuration ---------------------------------------------------------

MAX_UPLOAD_MB = 100
# Default compilation timeout: 180 seconds (3 minutes)
COMPILE_TIMEOUT_SECONDS = int(os.environ.get('COMPILE_TIMEOUT', '180'))
# Generated PDFs are kept this long so they can be previewed/downloaded (10 minutes)
OUTPUT_RETENTION_SECONDS = 600
OUTPUT_DIR = Path(__file__).resolve().parent / 'outputs'
OUTPUT_DIR.mkdir(exist_ok=True)

PDF_DOWNLOAD_NAME = 'document.pdf'
MAIN_TEX_NAMES = ('main.tex', 'document.tex', 'master.tex', 'thesis.tex', 'proyecto.tex')
LATEXMK_ARGS = ('latexmk', '-pdf', '-f', '-interaction=nonstopmode', '-shell-escape')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_for_flash_messages')
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

if 'SECRET_KEY' not in os.environ:
    logger.warning("SECRET_KEY is not set; using an insecure default key for session cookies.")


# --- Error handlers --------------------------------------------------------

@app.errorhandler(413)
def request_entity_too_large(error):
    logger.warning("Upload rejected: File exceeded MAX_CONTENT_LENGTH (%d MB)", MAX_UPLOAD_MB)
    flash(f'El archivo es demasiado grande. El límite máximo permitido es de {MAX_UPLOAD_MB} MB.')
    return redirect(url_for('index'))


@app.errorhandler(500)
def internal_server_error(error):
    logger.error("Internal server error: %s", error, exc_info=True)
    flash('Ocurrió un error interno en el servidor al procesar la solicitud.')
    return redirect(url_for('index'))


# --- File helpers ----------------------------------------------------------

def cleanup_old_outputs(max_age_seconds=OUTPUT_RETENTION_SECONDS):
    """Delete files in OUTPUT_DIR older than max_age_seconds."""
    current_time = time.time()
    try:
        for file_path in OUTPUT_DIR.iterdir():
            if not file_path.is_file():
                continue
            if current_time - file_path.stat().st_mtime > max_age_seconds:
                try:
                    file_path.unlink()
                    logger.info("Cleaned up expired output: %s", file_path.name)
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", file_path, e)
    except OSError as e:
        logger.warning("Error during outputs cleanup: %s", e)


def extract_zip_safely(zip_path, dest_dir):
    """Extract a ZIP archive, skipping entries that would land outside dest_dir."""
    dest = Path(dest_dir).resolve()
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        for member in zip_ref.infolist():
            target = (dest / member.filename).resolve()
            if target != dest and dest not in target.parents:
                logger.warning("Skipping unsafe zip entry: %s", member.filename)
                continue
            zip_ref.extract(member, dest)


def normalize_extracted_filenames(directory):
    """Normalize filenames to NFC Unicode in the extracted directory to avoid LaTeX NFD/NFC mismatches."""
    for root, _dirs, files in os.walk(directory, topdown=False):
        for name in files:
            nfc_name = unicodedata.normalize('NFC', name)
            if name == nfc_name:
                continue
            old_path = os.path.join(root, name)
            new_path = os.path.join(root, nfc_name)
            try:
                if not os.path.exists(new_path):
                    os.rename(old_path, new_path)
            except OSError as e:
                logger.debug("Could not normalize filename %s: %s", old_path, e)


def _contains_documentclass(tex_file):
    try:
        return '\\documentclass' in tex_file.read_text(encoding='utf-8', errors='ignore')
    except OSError as e:
        logger.debug("Error reading %s: %s", tex_file, e)
        return False


def find_main_tex(directory):
    """Find the main .tex file (one containing \\documentclass) in a directory."""
    dir_path = Path(directory)

    # 1. Prefer standard main filenames at the root
    root_tex_files = list(dir_path.glob('*.tex'))
    for standard_name in MAIN_TEX_NAMES:
        for tex_file in root_tex_files:
            if tex_file.name.lower() == standard_name and _contains_documentclass(tex_file):
                return tex_file

    # 2. Otherwise, any .tex file with \documentclass, prioritizing files closest to the root
    candidates = [f for f in dir_path.rglob('*.tex') if _contains_documentclass(f)]
    if not candidates:
        return None
    return min(candidates, key=lambda f: len(f.relative_to(dir_path).parts))


# --- Log parsing -----------------------------------------------------------

_ERROR_MARKERS = ('Error:', 'Fatal error', 'gave return code')
_WARNING_MARKERS = ('Warning:', 'Warning ')
_WARNING_MARKERS_LOWER = ('package warning:', 'failed to resolve', 'undefined refs', 'undefined on input line')


def _classify_log_line(line, stripped):
    """Return the category of a line that starts a new message, or None for a plain line."""
    lower = line.lower()
    if stripped.startswith('! ') or any(m in line for m in _ERROR_MARKERS):
        return 'error'
    if any(m in line for m in _WARNING_MARKERS) or any(m in lower for m in _WARNING_MARKERS_LOWER):
        return 'warning'
    if stripped.startswith('Latexmk:'):
        if 'error' in lower or 'missing input file' in lower:
            return 'error'
        if 'warning' in lower:
            return 'warning'
        return 'info'
    return None


def parse_latex_logs(log_text):
    """Parse latexmk and pdflatex stdout/stderr to categorize messages."""
    buckets = {'error': [], 'warning': [], 'info': []}
    category = 'info'
    message = []

    def flush():
        if message:
            buckets[category].append('\n'.join(message))
            message.clear()

    for line in log_text.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue

        new_category = _classify_log_line(line, stripped)
        # Plain lines following an error/warning are treated as its continuation
        if new_category is None and message and category != 'info':
            message.append(stripped)
            continue

        flush()
        category = new_category or 'info'
        message.append(stripped)

    flush()
    return {
        'errors': buckets['error'],
        'warnings': buckets['warning'],
        'infos': buckets['info'],
    }


# --- Compilation -----------------------------------------------------------

def _decode_output(output):
    if output is None:
        return ''
    if isinstance(output, bytes):
        return output.decode('utf-8', errors='replace')
    return output


def run_latexmk(main_tex):
    """Compile main_tex with latexmk.

    Returns (returncode, raw_log, timed_out). Raises FileNotFoundError if latexmk is not installed.
    """
    cmd = [*LATEXMK_ARGS, main_tex.name]
    work_dir = main_tex.parent
    logger.info("Running compiler: %s in %s (timeout: %ds)", ' '.join(cmd), work_dir, COMPILE_TIMEOUT_SECONDS)
    start_time = time.time()

    try:
        process = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            errors='replace',
            stdin=subprocess.DEVNULL,
            timeout=COMPILE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as te:
        logger.error("Compilation timed out after %.2f seconds", time.time() - start_time)
        raw_log = (f"ERROR: Compilation timed out after {COMPILE_TIMEOUT_SECONDS} seconds.\n\n"
                   + _decode_output(te.stdout) + '\n' + _decode_output(te.stderr))
        return None, raw_log, True

    logger.info("Compiler process finished in %.2f seconds (returncode: %d)",
                time.time() - start_time, process.returncode)
    raw_log = (process.stdout or '') + '\n' + (process.stderr or '')
    return process.returncode, raw_log, False


def _store_pdf(main_tex, job_id):
    """Move the generated PDF to OUTPUT_DIR. Returns True if a PDF was produced."""
    pdf_path = main_tex.with_suffix('.pdf')
    if not pdf_path.exists():
        logger.warning("Compiler finished but no output PDF was found at %s", pdf_path)
        return False

    dest_pdf = OUTPUT_DIR / f"{job_id}.pdf"
    shutil.move(str(pdf_path), str(dest_pdf))
    logger.info("PDF generated successfully: %s (%.2f KB)", dest_pdf, dest_pdf.stat().st_size / 1024)
    return True


def _compile_project(file, temp_dir, job_id):
    zip_path = temp_dir / 'project.zip'
    file.save(str(zip_path))
    logger.info("Saved upload to %s (size: %.2f MB)", zip_path, zip_path.stat().st_size / (1024 * 1024))

    try:
        extract_zip_safely(zip_path, temp_dir)
    except zipfile.BadZipFile:
        logger.error("Uploaded file is not a valid ZIP: %s", file.filename)
        flash('El archivo subido no es un archivo ZIP válido o está dañado.')
        return redirect(url_for('index'))
    logger.info("Successfully extracted zip contents into %s", temp_dir)

    # Remove uploaded zip immediately to save RAM in Cloud Run (tmpfs)
    with contextlib.suppress(OSError):
        zip_path.unlink()

    normalize_extracted_filenames(temp_dir)

    main_tex = find_main_tex(temp_dir)
    if not main_tex:
        logger.warning("Could not find main .tex file in extracted contents of %s", file.filename)
        flash('Could not find a main .tex file (like main.tex) with \\documentclass.')
        return redirect(url_for('index'))
    logger.info("Found main .tex file: %s", main_tex)

    try:
        returncode, raw_log, timed_out = run_latexmk(main_tex)
    except FileNotFoundError:
        logger.error("LaTeX compiler (latexmk) is not installed on this system.")
        flash('Error: LaTeX compiler (latexmk) is not installed on the server.')
        return redirect(url_for('index'))

    parsed_logs = parse_latex_logs(raw_log)
    if timed_out:
        logger.error("Compilation timed out for job %s", job_id)
        parsed_logs['errors'].insert(0, (
            f"Compilation exceeded the maximum time limit of {COMPILE_TIMEOUT_SECONDS} seconds. "
            "There may be missing input files causing prompts, an infinite loop, or heavy external tools."
        ))
        has_pdf = False
    else:
        has_pdf = _store_pdf(main_tex, job_id)

    return render_template(
        'result.html',
        job_id=job_id,
        has_pdf=has_pdf,
        success=(returncode == 0 and has_pdf),
        raw_log=raw_log,
        retention_minutes=OUTPUT_RETENTION_SECONDS // 60,
        **parsed_logs
    )


# --- Routes ----------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


@app.route('/compile', methods=['POST'])
def compile_latex():
    cleanup_old_outputs()

    file = request.files.get('file')
    if file is None:
        flash('No file part')
        return redirect(url_for('index'))
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('index'))
    if not file.filename.lower().endswith('.zip'):
        flash('Uploaded file must be a .zip')
        return redirect(url_for('index'))

    job_id = str(uuid.uuid4())
    temp_dir = Path(tempfile.mkdtemp(prefix='latex_compile_'))
    logger.info("Starting compilation job %s for file: %s", job_id, file.filename)

    try:
        return _compile_project(file, temp_dir, job_id)
    except Exception as e:
        logger.exception("Unexpected error processing LaTeX job %s: %s", job_id, e)
        flash(f'Error al procesar el archivo: {e}')
        return redirect(url_for('index'))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        gc.collect()


def _is_valid_job_id(job_id):
    """Accept only canonical UUIDs, which also prevents directory traversal."""
    try:
        return str(uuid.UUID(job_id)) == job_id
    except ValueError:
        return False


def _send_pdf(job_id, as_attachment):
    if not _is_valid_job_id(job_id):
        return "Invalid Job ID", 400
    return send_from_directory(
        OUTPUT_DIR,
        f"{job_id}.pdf",
        mimetype='application/pdf',
        as_attachment=as_attachment,
        download_name=PDF_DOWNLOAD_NAME
    )


@app.route('/download/<job_id>')
def download_pdf(job_id):
    return _send_pdf(job_id, as_attachment=True)


@app.route('/view/<job_id>')
def view_pdf(job_id):
    """Serve the PDF inline so the browser's built-in viewer opens it instead of downloading."""
    return _send_pdf(job_id, as_attachment=False)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    host = os.environ.get('HOST', '0.0.0.0')
    try:
        from waitress import serve
        logger.info("Iniciando servidor de producción WSGI (Waitress) en http://%s:%d", host, port)
        serve(app, host=host, port=port, channel_timeout=300)
    except ImportError:
        logger.warning("Waitress no está instalado. Iniciando servidor en modo producción en http://%s:%d", host, port)
        app.run(debug=False, port=port, host=host, threaded=True)
