"""Abre la TUI de OpenCode pegada a una sesión del servidor del orquestador.

Lee la contraseña del servidor desde .env, para que no aparezca en la línea de comandos ni en el chat.
Uso: python oc_attach.py --dir <carpeta> --session <session_id>
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True)
ap.add_argument("--session", required=True)
args = ap.parse_args()

cli = os.getenv("OPENCODE_CLI_PATH", "opencode")
url = os.getenv("OPENCODE_SERVER_URL", "http://127.0.0.1:4097")
env = {**os.environ, "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1"}
sys.exit(subprocess.call([cli, "attach", url, "--dir", args.dir, "--session", args.session], env=env))
