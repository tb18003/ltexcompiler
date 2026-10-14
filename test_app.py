import os
import io
import tempfile
import zipfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess
from app import app, extract_zip_safely, find_main_tex, parse_latex_logs

class LatexCompilerTestCase(unittest.TestCase):
    def setUp(self):
        app.testing = True
        app.config['SECRET_KEY'] = 'test_secret_key'
        self.client = app.test_client()

        # Keep generated PDFs out of the real outputs/ directory
        self._outputs_tmp = tempfile.TemporaryDirectory()
        self.output_dir = Path(self._outputs_tmp.name)
        patcher = patch('app.OUTPUT_DIR', self.output_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._outputs_tmp.cleanup)

    def create_output_pdf(self, content=b'%PDF-1.4 fake content'):
        job_id = str(uuid.uuid4())
        (self.output_dir / f'{job_id}.pdf').write_bytes(content)
        return job_id

    def create_sample_zip(self, main_tex_content=None, filename='main.tex'):
        if main_tex_content is None:
            main_tex_content = """\\documentclass{article}
\\begin{document}
Hello World! This is a test LaTeX document.
\\end{document}
"""
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr(filename, main_tex_content)
        zip_buffer.seek(0)
        return zip_buffer

    def test_index_page(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'LaTeX Compiler', response.data)
        self.assertIn(b'100 MB', response.data)

    def test_upload_no_file(self):
        response = self.client.post('/compile', data={}, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)

    def test_upload_non_zip(self):
        data = {
            'file': (io.BytesIO(b"not a zip"), 'test.txt')
        }
        response = self.client.post('/compile', data=data, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Uploaded file must be a .zip', response.data)

    def test_upload_corrupt_zip(self):
        data = {
            'file': (io.BytesIO(b"corrupt zip content that is not pkzip"), 'test.zip')
        }
        response = self.client.post('/compile', data=data, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        # Should gracefully flash message instead of 500 error
        self.assertIn(b'El archivo subido no es un archivo ZIP', response.data)

    def test_upload_zip_without_documentclass(self):
        zip_buffer = self.create_sample_zip(main_tex_content="Plain text without documentclass")
        data = {
            'file': (zip_buffer, 'test.zip')
        }
        response = self.client.post('/compile', data=data, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Could not find a main .tex file', response.data)

    @patch('subprocess.run')
    def test_compilation_success(self, mock_run):
        # Mock subprocess.run returning success
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = 'Latexmk: All targets () are up-to-date'
        mock_process.stderr = ''
        mock_run.return_value = mock_process

        # We also need a fake PDF to be created in the temp directory so shutil.move succeeds
        def side_effect(*args, **kwargs):
            cwd = kwargs.get('cwd')
            fake_pdf = os.path.join(cwd, 'main.pdf')
            with open(fake_pdf, 'wb') as f:
                f.write(b'%PDF-1.4 fake content')
            return mock_process

        mock_run.side_effect = side_effect

        zip_buffer = self.create_sample_zip()
        data = {'file': (zip_buffer, 'project.zip')}
        response = self.client.post('/compile', data=data, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Compilation Successful', response.data)
        self.assertIn(b'Download PDF', response.data)
        self.assertIn(b'Preview PDF', response.data)
        self.assertEqual(len(list(self.output_dir.glob('*.pdf'))), 1)

    @patch('subprocess.run')
    def test_compilation_timeout(self, mock_run):
        # Mock subprocess.TimeoutExpired
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=['latexmk'],
            timeout=180,
            output='Partial LaTeX output before hanging...',
            stderr=''
        )

        zip_buffer = self.create_sample_zip()
        data = {'file': (zip_buffer, 'project.zip')}
        response = self.client.post('/compile', data=data, content_type='multipart/form-data')
        
        # Must not return 500, but render result page with timeout error details
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Compilation Failed', response.data)
        self.assertIn(b'Compilation exceeded the maximum time limit', response.data)

    def test_parse_latex_logs(self):
        log_sample = """
! LaTeX Error: File 'test.sty' not found.
Type X to quit or <RETURN> to proceed.

LaTeX Warning: Reference `sec:intro' on page 1 undefined on input line 15.

Latexmk: Found input bbl file 'main.bbl'
"""
        parsed = parse_latex_logs(log_sample)
        self.assertTrue(len(parsed['errors']) >= 1)
        self.assertTrue(len(parsed['warnings']) >= 1)
        self.assertIn("! LaTeX Error: File 'test.sty' not found.", parsed['errors'][0])

    def test_parse_latex_logs_continuation_lines(self):
        parsed = parse_latex_logs("! Undefined control sequence.\nl.12 \\foo\nLatexmk: done\nplain info")
        self.assertEqual(parsed['errors'], ["! Undefined control sequence.\nl.12 \\foo"])
        self.assertEqual(parsed['infos'], ['Latexmk: done', 'plain info'])
        self.assertEqual(parsed['warnings'], [])

    def test_view_pdf_inline(self):
        job_id = self.create_output_pdf()
        response = self.client.get(f'/view/{job_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        self.assertTrue(response.headers['Content-Disposition'].startswith('inline'))
        self.assertEqual(response.data, b'%PDF-1.4 fake content')
        response.close()

    def test_download_pdf_attachment(self):
        job_id = self.create_output_pdf()
        response = self.client.get(f'/download/{job_id}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Disposition'].startswith('attachment'))
        self.assertIn('document.pdf', response.headers['Content-Disposition'])
        response.close()

    def test_pdf_routes_reject_invalid_job_id(self):
        for route in ('/view/', '/download/'):
            self.assertEqual(self.client.get(route + 'not-a-uuid').status_code, 400)
            self.assertEqual(self.client.get(route + '..%2Fapp').status_code, 404)

    def test_pdf_routes_missing_file(self):
        missing = str(uuid.uuid4())
        self.assertEqual(self.client.get(f'/view/{missing}').status_code, 404)
        self.assertEqual(self.client.get(f'/download/{missing}').status_code, 404)

    def test_extract_zip_skips_unsafe_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, 'p.zip')
            dest = os.path.join(tmp, 'out')
            os.mkdir(dest)
            with zipfile.ZipFile(zip_path, 'w') as zf:
                zf.writestr('../evil.tex', 'x')
                zf.writestr('/abs.tex', 'x')
                zf.writestr('sub/ok.tex', 'x')
            extract_zip_safely(zip_path, dest)
            self.assertTrue(os.path.exists(os.path.join(dest, 'sub', 'ok.tex')))
            self.assertFalse(os.path.exists(os.path.join(tmp, 'evil.tex')))
            self.assertFalse(os.path.exists(os.path.join(dest, 'abs.tex')))

    def test_find_main_tex_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'chapters').mkdir()
            (root / 'chapters' / 'deep.tex').write_text('\\documentclass{article}')
            (root / 'other.tex').write_text('\\documentclass{article}')
            self.assertEqual(find_main_tex(tmp).name, 'other.tex')
            (root / 'main.tex').write_text('\\documentclass{article}')
            self.assertEqual(find_main_tex(tmp).name, 'main.tex')

if __name__ == '__main__':
    unittest.main()
