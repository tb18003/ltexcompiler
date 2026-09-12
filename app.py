import os
import shutil
import tempfile
import zipfile
import subprocess
import time
import uuid
import re
import unicodedata
import logging
from pathlib import Path
from flask import Flask, request, render_template, send_from_directory, flash, redirect, url_for, Response

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
)
logger = logging.getLogger('ltexcompiler')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'super_secret_key_for_flash_messages')

# Allow uploads up to 100 MB
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# Default compilation timeout: 180 seconds (3 minutes)
COMPILE_TIMEOUT_SECONDS = int(os.environ.get('COMPILE_TIMEOUT', '180'))

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

@app.errorhandler(413)
def request_entity_too_large(error):
    logger.warning("Upload rejected: File exceeded MAX_CONTENT_LENGTH (100 MB)")
    flash('El archivo es demasiado grande. El límite máximo permitido es de 100 MB.')
    return redirect(url_for('index')), 413

@app.errorhandler(500)
def internal_server_error(error):
    logger.error("Internal server error: %s", error, exc_info=True)
    flash('Ocurrió un error interno en el servidor al procesar la solicitud.')
    return redirect(url_for('index')), 500

def cleanup_old_outputs(max_age_seconds=600):
    """Delete files in OUTPUT_DIR older than max_age_seconds (default 10 mins)."""
    current_time = time.time()
    try:
        for filename in os.listdir(OUTPUT_DIR):
            file_path = os.path.join(OUTPUT_DIR, filename)
            if os.path.isfile(file_path):
                file_age = current_time - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    try:
                        os.remove(file_path)
                        logger.info("Cleaned up expired output: %s", filename)
                    except Exception as e:
                        logger.warning("Failed to delete %s: %s", file_path, e)
    except Exception as e:
        logger.warning("Error during outputs cleanup: %s", e)

def normalize_extracted_filenames(directory):
    """Normalize filenames to NFC Unicode in the extracted directory to avoid LaTeX NFD/NFC mismatches."""
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in files:
            nfc_name = unicodedata.normalize('NFC', name)
            if name != nfc_name:
                old_path = os.path.join(root, name)
                new_path = os.path.join(root, nfc_name)
                try:
                    if not os.path.exists(new_path):
                        os.rename(old_path, new_path)
                except Exception as e:
                    logger.debug("Could not normalize filename %s: %s", old_path, e)

def find_main_tex(directory):
    """Find the main .tex file in a directory."""
    dir_path = Path(directory)
    
    # 1. Check for standard main filenames at root that contain \documentclass
    standard_names = ['main.tex', 'document.tex', 'master.tex', 'thesis.tex', 'proyecto.tex']
    for standard_name in standard_names:
        for f in dir_path.glob('*.tex'):
            if f.name.lower() == standard_name:
                try:
                    with open(f, 'r', encoding='utf-8', errors='ignore') as cf:
                        if '\\documentclass' in cf.read():
                            return f
                except Exception:
                    pass

    # 2. Look for any .tex file with \documentclass
    candidates = []
    for tex_file in dir_path.rglob('*.tex'):
        try:
            with open(tex_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if '\\documentclass' in content:
                    depth = len(tex_file.relative_to(dir_path).parts)
                    candidates.append((depth, tex_file))
        except Exception as e:
            logger.debug("Error reading %s: %s", tex_file, e)
            
    if candidates:
        # Prioritize files closest to root
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
            
    return None

def parse_latex_logs(log_text):
    """Parse latexmk and pdflatex stdout/stderr to categorize messages."""
    errors = []
    warnings = []
    infos = []
    
    lines = log_text.split('\n')
    current_category = 'info'
    current_message = []
    
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
            
        # Check for error indicators
        if line_stripped.startswith('! ') or 'Error:' in line or 'Fatal error' in line or 'gave return code' in line:
            if current_message:
                if current_category == 'error': errors.append('\n'.join(current_message))
                elif current_category == 'warning': warnings.append('\n'.join(current_message))
                else: infos.append('\n'.join(current_message))
                current_message = []
            current_category = 'error'
            current_message.append(line_stripped)
            
        # Check for warning indicators
        elif 'Warning:' in line or 'Warning ' in line or 'LaTeX Warning:' in line or 'Package warning:' in line.lower() or 'failed to resolve' in line.lower() or 'undefined refs' in line.lower() or 'undefined on input line' in line.lower():
            if current_message:
                if current_category == 'error': errors.append('\n'.join(current_message))
                elif current_category == 'warning': warnings.append('\n'.join(current_message))
                else: infos.append('\n'.join(current_message))
                current_message = []
            current_category = 'warning'
            current_message.append(line_stripped)
            
        # Check for info indicators from latexmk
        elif line_stripped.startswith('Latexmk:'):
            if current_message:
                if current_category == 'error': errors.append('\n'.join(current_message))
                elif current_category == 'warning': warnings.append('\n'.join(current_message))
                else: infos.append('\n'.join(current_message))
                current_message = []
            
            # Sub-categorize some latexmk lines
            if 'error' in line.lower() or 'missing input file' in line.lower():
                current_category = 'error'
            elif 'warning' in line.lower():
                current_category = 'warning'
            else:
                current_category = 'info'
            current_message.append(line_stripped)
            
        else:
            # Append to current message if it looks like a continuation, otherwise it's just info
            if current_message and current_category != 'info':
                current_message.append(line_stripped)
            else:
                if current_message:
                    if current_category == 'error': errors.append('\n'.join(current_message))
                    elif current_category == 'warning': warnings.append('\n'.join(current_message))
                    else: infos.append('\n'.join(current_message))
                    current_message = []
                current_category = 'info'
                current_message.append(line_stripped)
                
    # Flush last message
    if current_message:
        if current_category == 'error': errors.append('\n'.join(current_message))
        elif current_category == 'warning': warnings.append('\n'.join(current_message))
        else: infos.append('\n'.join(current_message))
        
    return {
        'errors': errors,
        'warnings': warnings,
        'infos': infos
    }

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/compile', methods=['POST'])
def compile_latex():
    # Cleanup old outputs before processing new one
    cleanup_old_outputs(600) # 10 minutes
    
    if 'file' not in request.files:
        flash('No file part')
        return redirect(request.url)
        
    file = request.files['file']
    if file.filename == '':
        flash('No selected file')
        return redirect(url_for('index'))
        
    if not file.filename.lower().endswith('.zip'):
        flash('Uploaded file must be a .zip')
        return redirect(url_for('index'))

    temp_dir = tempfile.mkdtemp(prefix='latex_compile_')
    job_id = str(uuid.uuid4())
    logger.info("Starting compilation job %s for file: %s", job_id, file.filename)
    
    try:
        zip_path = os.path.join(temp_dir, 'project.zip')
        file.save(zip_path)
        file_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        logger.info("Saved upload to %s (size: %.2f MB)", zip_path, file_size_mb)
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Extract files avoiding path traversal
                for member in zip_ref.infolist():
                    member_path = os.path.normpath(member.filename)
                    if member_path.startswith('..') or os.path.isabs(member_path):
                        continue
                    zip_ref.extract(member, temp_dir)
            logger.info("Successfully extracted zip contents into %s", temp_dir)
        except zipfile.BadZipFile:
            logger.error("Uploaded file is not a valid ZIP: %s", file.filename)
            flash('El archivo subido no es un archivo ZIP válido o está dañado.')
            return redirect(url_for('index'))

        normalize_extracted_filenames(temp_dir)
            
        main_tex = find_main_tex(temp_dir)
        
        if not main_tex:
            logger.warning("Could not find main .tex file in extracted contents of %s", file.filename)
            flash('Could not find a main .tex file (like main.tex) with \\documentclass.')
            return redirect(url_for('index'))

        logger.info("Found main .tex file: %s", main_tex)
        work_dir = main_tex.parent
        cmd = ['latexmk', '-pdf', '-f', '-interaction=nonstopmode', '-shell-escape', main_tex.name]
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
            elapsed = time.time() - start_time
            logger.info("Compiler process finished in %.2f seconds (returncode: %d)", elapsed, process.returncode)
            
            raw_log = (process.stdout or '') + '\n' + (process.stderr or '')
            parsed_logs = parse_latex_logs(raw_log)
            
            pdf_path = work_dir / (main_tex.stem + '.pdf')
            has_pdf = pdf_path.exists()
            
            if has_pdf:
                # Move PDF to persistent outputs directory
                dest_pdf = os.path.join(OUTPUT_DIR, f"{job_id}.pdf")
                shutil.move(str(pdf_path), dest_pdf)
                pdf_size_kb = os.path.getsize(dest_pdf) / 1024
                logger.info("PDF generated successfully: %s (%.2f KB)", dest_pdf, pdf_size_kb)
            else:
                logger.warning("Compiler finished but no output PDF was found at %s", pdf_path)
                
            return render_template('result.html', 
                                   job_id=job_id, 
                                   has_pdf=has_pdf, 
                                   success=(process.returncode == 0 and has_pdf),
                                   errors=parsed_logs['errors'],
                                   warnings=parsed_logs['warnings'],
                                   infos=parsed_logs['infos'],
                                   raw_log=raw_log)
                
        except subprocess.TimeoutExpired as te:
            elapsed = time.time() - start_time
            logger.error("Compilation timed out after %.2f seconds for job %s", elapsed, job_id)
            raw_stdout = te.stdout if isinstance(te.stdout, str) else (te.stdout.decode('utf-8', errors='replace') if te.stdout else '')
            raw_stderr = te.stderr if isinstance(te.stderr, str) else (te.stderr.decode('utf-8', errors='replace') if te.stderr else '')
            raw_log = f"ERROR: Compilation timed out after {COMPILE_TIMEOUT_SECONDS} seconds.\n\n" + raw_stdout + '\n' + raw_stderr
            parsed_logs = parse_latex_logs(raw_log)
            parsed_logs['errors'].insert(0, f"Compilation exceeded the maximum time limit of {COMPILE_TIMEOUT_SECONDS} seconds. There may be missing input files causing prompts, an infinite loop, or heavy external tools.")
            
            return render_template('result.html',
                                   job_id=job_id,
                                   has_pdf=False,
                                   success=False,
                                   errors=parsed_logs['errors'],
                                   warnings=parsed_logs['warnings'],
                                   infos=parsed_logs['infos'],
                                   raw_log=raw_log)

        except FileNotFoundError:
            logger.error("LaTeX compiler (latexmk) is not installed on this system.")
            flash('Error: LaTeX compiler (latexmk) is not installed on the server.')
            return redirect(url_for('index'))

    except Exception as e:
        logger.exception("Unexpected error processing LaTeX job %s: %s", job_id, e)
        flash(f'Error al procesar el archivo: {str(e)}')
        return redirect(url_for('index'))
            
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/download/<job_id>')
def download_pdf(job_id):
    # Ensure job_id is just a uuid to prevent directory traversal
    if not re.match(r'^[a-f0-9\-]+$', job_id):
        return "Invalid Job ID", 400
        
    return send_from_directory(OUTPUT_DIR, f"{job_id}.pdf", as_attachment=True, download_name='document.pdf')

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
