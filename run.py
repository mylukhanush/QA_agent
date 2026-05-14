"""
QA Automation Tool — Application Entry Point.

Run with:
    python run.py          → starts Flask web server
    python run.py --help   → shows CLI commands
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Avoid protobuf C-extension crashes on newer Python runtimes.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("FLASK_PORT", 5000)),
        debug=os.getenv("FLASK_ENV", "development") == "development",
    )
