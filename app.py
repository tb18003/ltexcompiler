import os
import shutil
import tempfile
import zipfile
import subprocess
from pathlib import Path
from flask import Flask, request, render_template, send_file, flash, redirect, url_for, Response

app = Flask(__name__)
app.secret_key = 'super_secret_key_for_flash_messages'

def find_main_tex(directory):
    """Find the main .tex file in a directory.
    Prioritizes 'main.tex', otherwise looks for any .tex file containing '\documentclass'."""
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
            pass # Ignore files that can't be read as utf-8
            
    return None

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/compile', methods=['POST'])
def compile_latex():
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

    # Create a temporary directory
    temp_dir = tempfile.mkdtemp(prefix='latex_compile_')
    
    try:
        # Save and extract ZIP
        zip_path = os.path.join(temp_dir, 'project.zip')
        file.save(zip_path)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
            
        # Find main.tex
        main_tex = find_main_tex(temp_dir)
        
        if not main_tex:
            flash('Could not find a main .tex file (like main.tex) with \documentclass.')
            return redirect(url_for('index'))

        # Run latexmk
        work_dir = main_tex.parent
        cmd = ['latexmk', '-pdf', '-interaction=nonstopmode', main_tex.name]
        try:
            process = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, errors='replace')
            
            if process.returncode != 0:
                error_log = process.stdout + '\n' + process.stderr
                response = Response(error_log, mimetype='text/plain')
                response.headers['Content-Disposition'] = 'attachment; filename=compilation_error.log'
                return response
        except FileNotFoundError:
            flash('Error: LaTeX compiler (latexmk) is not installed on the server.')
            return redirect(url_for('index'))
            
        # If success, find the pdf
        pdf_path = work_dir / (main_tex.stem + '.pdf')
        
        if pdf_path.exists():
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
            
            response = Response(pdf_data, mimetype='application/pdf')
            response.headers['Content-Disposition'] = f'attachment; filename={main_tex.stem}.pdf'
            return response
        else:
            flash('Compilation succeeded but PDF was not found.')
            return redirect(url_for('index'))
            
    finally:
        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
