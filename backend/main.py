import sys
import os
import subprocess
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MagiTrader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

collector_process = None

@app.get("/api/data/status")
def get_status():
    global collector_process
    is_active = collector_process is not None and collector_process.poll() is None
    return {"active": is_active}

@app.post("/api/data/start")
def start_collection():
    global collector_process
    if collector_process is None or collector_process.poll() is not None:
        script_path = os.path.join(os.path.dirname(__file__), "services", "data_collector.py")
        collector_process = subprocess.Popen([sys.executable, script_path])
    return {"active": True}

@app.post("/api/data/stop")
def stop_collection():
    global collector_process
    if collector_process and collector_process.poll() is None:
        collector_process.terminate()
        collector_process.wait()
        collector_process = None
    return {"active": False}
