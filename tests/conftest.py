"""Make panel/server.py importable under pytest.

server.py reads PANEL_CONFIG at import time, so point it at a throwaway config
in a temp directory before any test imports it.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "panel"))

_tmp = tempfile.mkdtemp(prefix="panel-test-")
ROOT = os.path.join(_tmp, "downloads")
os.makedirs(ROOT, exist_ok=True)

_cfg = os.path.join(_tmp, "panel.json")
with open(_cfg, "w") as f:
    json.dump({
        "download_dir": ROOT,
        "rclone_config": os.path.join(_tmp, "rclone.conf"),
        "state_file": os.path.join(_tmp, "state.json"),
        "data_dir": _tmp,
        "session_key": "test-session-key",
        "rpc_secret": "test-secret",
        "users": {},
    }, f)
os.environ.setdefault("PANEL_CONFIG", _cfg)
