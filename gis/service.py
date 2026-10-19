"""Minimal Python service for the React status page and direct file consumers."""
import json
import os
import subprocess
import sys
import threading
from flask import Flask, jsonify, send_from_directory
from config import ROOT, DATA
from acquire import is_running

app = Flask(__name__)
launch_lock = threading.Lock()
child = None


def snapshot():
    manifest = DATA / "manifest.json"
    files = []
    for p in sorted(DATA.glob("*")):
        try:
            if p.is_file() and p.suffix != ".tmp":
                files.append({"name": p.name, "size": p.stat().st_size, "url": "/data/" + p.name})
        except FileNotFoundError:
            pass  # A rerun is replacing this output.
    log_files = [DATA / "log.txt"] + sorted(DATA.glob("log.part*.txt"))
    def read(path):
        try:
            return path.read_text()
        except FileNotFoundError:
            return ""
    return dict(running=is_running(),
                manifest=read(manifest),
                log="".join(read(p) for p in log_files), files=files)


@app.get("/api/healthz")
def health():
    return jsonify(status="ok")


@app.get("/api/status")
def status():
    return jsonify(snapshot())


@app.route("/run", methods=["GET", "POST"])
@app.post("/api/run")
def rerun():
    global child
    with launch_lock:
        if not is_running() and (child is None or child.poll() is not None):
            child = subprocess.Popen([sys.executable, str(ROOT / "gis/acquire.py")], cwd=ROOT)
        result = snapshot()
        result["running"] = True
        return jsonify(result), 202


@app.get("/data/<path:name>")
def data_file(name):
    return send_from_directory(DATA, name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True,
            debug=False, use_reloader=False)
