import os
import io
import zipfile
from app import app

def create_sample_zip():
    tex_content = """\\documentclass{article}
\\begin{document}
Hello World! This is a test LaTeX document.
\\end{document}
"""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr('main.tex', tex_content)
    zip_buffer.seek(0)
    return zip_buffer

def test_compile():
    app.testing = True
    client = app.test_client()
    
    zip_buffer = create_sample_zip()
    
    data = {
        'file': (zip_buffer, 'project.zip')
    }
    
    response = client.post('/compile', data=data, content_type='multipart/form-data')
    
    print(f"Status Code: {response.status_code}")
    print(f"Content Type: {response.content_type}")
    
    if response.status_code == 200 and response.content_type == 'application/pdf':
        print("Success! PDF received.")
        with open('test_output.pdf', 'wb') as f:
            f.write(response.data)
    else:
        print("Error:")
        print(response.data.decode('utf-8'))

if __name__ == '__main__':
    test_compile()
