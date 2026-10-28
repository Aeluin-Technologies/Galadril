# /// script
# requires-python = "==3.13.*"
# dependencies = [
#   "cryptography==50.0.2", "PyJWT==2.15.1", "PyYAML==6.0.3",
#   "pydantic==2.14.1", "python-dotenv==1.2.4", "httpx==0.28.1", "rich==15.0.0",
#   "opentelemetry-api==1.45.1", "opentelemetry-sdk==1.45.1",
#   "opentelemetry-exporter-otlp-proto-http==1.45.1",
# ]
# ///
"""Standalone bootstrap avoids installing the machine-learning workspace to start Docker."""

import sys
from pathlib import Path

# Script execution needs the repository namespace; no application code is loaded here.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from infrastructure.dev.galadril_dev.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
