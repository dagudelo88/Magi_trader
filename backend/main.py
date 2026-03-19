import sys
import os
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import ccxt

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
# Repo-root .env (same layout as data_collector); backend/.env overrides if both exist.
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)

app = FastAPI(title="MagiTrader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

collector_process = None

def get_exchange():
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET") or os.getenv("BINANCE_SECRET")

    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Binance API keys not configured in environment")
    
    return ccxt.binance({
        'apiKey': api_key,
        'secret': api_secret,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot'
        }
    })

@app.get("/api/wallet/balances")
def get_wallet_balances():
    try:
        exchange = get_exchange()
        balance = exchange.fetch_balance()
        
        non_zero_balances = []
        if 'total' in balance:
            for asset, amount in balance['total'].items():
                if amount > 0:
                    free_amt = balance.get('free', {}).get(asset, 0)
                    used_amt = balance.get('used', {}).get(asset, 0)
                    non_zero_balances.append({
                        "asset": asset,
                        "free": free_amt,
                        "used": used_amt,
                        "total": amount
                    })
                    
        non_zero_balances.sort(key=lambda x: x['total'], reverse=True)
        return {"balances": non_zero_balances}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
