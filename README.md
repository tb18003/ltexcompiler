# LaTeX Compiler Web App

A Python web application that allows you to upload a ZIP file containing a LaTeX project (e.g., from Overleaf) and returns the compiled PDF document.

## Prerequisites

- Python 3
- A LaTeX distribution installed on the machine running this server:
  - **macOS**: [MacTeX](https://tug.org/mactex/)
  - **Linux**: `texlive-full` (e.g., `sudo apt install texlive-full`)
  - **Windows**: [MiKTeX](https://miktex.org/)
- Ensure `latexmk` is accessible in your system's PATH (it comes by default with most distributions).

## Installation

1. Create a virtual environment and activate it:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
2. Install the required packages:
   ```bash
   pip install -r requirements.txt
   ```

## Running the App

1. Run the Flask application:
   ```bash
   python app.py
   ```
2. Open your browser and navigate to: `http://localhost:5000`
3. Upload your `.zip` LaTeX project!

## How it works
1. The app receives the ZIP upload and extracts it to an isolated temporary directory.
2. It intelligently finds the main `.tex` file.
3. It runs `latexmk -pdf` to compile the document (handling bibliographies, cross-references, and multiple passes automatically).
4. Returns the generated PDF directly to your browser for download.
5. Cleans up all temporary files immediately.
