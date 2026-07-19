import os
import shutil
import tempfile
import zipfile
import subprocess
import time
import uuid
import re
from pathlib import Path
from flask import Flask, request, render_template, send_from_directory, flash, redirect, url_for, Response

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_flash_messages'

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

def cleanup_old_outputs(max_age_seconds=600):
    """Delete files in OUTPUT_DIR older than max_age_seconds (default 10 mins)."""
    current_time = time.time()
    for filename in os.listdir(OUTPUT_DIR):
        file_path = os.path.join(OUTPUT_DIR, filename)
        if os.path.isfile(file_path):
            file_age = current_time - os.path.getmtime(file_path)
            if file_age > max_age_seconds:
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")

def find_main_tex(directory):
    """Find the main .tex file in a directory."""
    dir_path = Path(directory)
    
    # 1. Check for main.tex
    main_tex = dir_path / 'main.tex'
    if main_tex.exists():
        return main_tex
        
    # 2. Look for any .tex file with \documentclass
    for tex_file in dir_path.rglob('*.tex'):
        try:
            with open(tex_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if '\\documentclass' in content:
                    return tex_file
        except UnicodeDecodeError:
            pass
            
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
        
    if not file.filename.endswith('.zip'):
        flash('Uploaded file must be a .zip')
        return redirect(url_for('index'))

    temp_dir = tempfile.mkdtemp(prefix='latex_compile_')
    job_id = str(uuid.uuid4())
    
    try:
        zip_path = os.path.join(temp_dir, 'project.zip')
        file.save(zip_path)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        main_tex = find_main_tex(temp_dir)
        
        if not main_tex:
            flash('Could not find a main .tex file (like main.tex) with \documentclass.')
            return redirect(url_for('index'))

        work_dir = main_tex.parent
        cmd = ['latexmk', '-pdf', '-f', '-interaction=nonstopmode', main_tex.name]
        try:
            process = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, errors='replace')
            
            raw_log = process.stdout + '\n' + process.stderr
            parsed_logs = parse_latex_logs(raw_log)
            
            pdf_path = work_dir / (main_tex.stem + '.pdf')
            has_pdf = pdf_path.exists()
            
            if has_pdf:
                # Move PDF to persistent outputs directory
                dest_pdf = os.path.join(OUTPUT_DIR, f"{job_id}.pdf")
                shutil.move(str(pdf_path), dest_pdf)
                
            return render_template('result.html', 
                                   job_id=job_id, 
                                   has_pdf=has_pdf, 
                                   success=(process.returncode == 0),
                                   errors=parsed_logs['errors'],
                                   warnings=parsed_logs['warnings'],
                                   infos=parsed_logs['infos'],
                                   raw_log=raw_log)
                
        except FileNotFoundError:
            flash('Error: LaTeX compiler (latexmk) is not installed on the server.')
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
