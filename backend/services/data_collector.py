import asyncio
import websockets
import time
import json
import os
import sys
from collections import deque
from dotenv import load_dotenv

# Load environment variables
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

# Ensure backend folder is in path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from database import get_db_connection
from tracked_markets import (
    TRACKED_USDT_STREAM_IDS,
    BTC_STREAM_ID,
    alt_stream_ids,
    stream_id_to_ccxt,
)

# Initialize state dictionary (eight USDT pairs + BTC; see tracked_markets.py)
state = {}
for sym in TRACKED_USDT_STREAM_IDS:
    state[sym] = {
        'price': deque(maxlen=60), 
        'volume': 0.0, 
        'latest_price': 0.0,
        'bid': 0.0,
        'ask': 0.0,
        'spread_bps': 0.0
    }

def get_roc(price_history, seconds):
    if len(price_history) < seconds + 1:
        return 0.0
    current_price = price_history[-1]
    past_price = price_history[-(seconds + 1)]
    if past_price == 0:
        return 0.0
    return (current_price - past_price) / past_price

async def binance_ws_listener():
    # Build the combined stream URL
    streams = []
    for sym in TRACKED_USDT_STREAM_IDS:
        streams.append(f"{sym}@ticker")
        streams.append(f"{sym}@bookTicker")
        
    stream_path = "/".join(streams)
    
    # Determine endpoint based on Testnet toggle
    use_testnet = os.getenv("TESTNET", "True").lower() == "true"
    base_url = (
        "wss://stream.testnet.binance.vision"
        if use_testnet
        else "wss://stream.binance.com:9443"
    )
    ws_url = f"{base_url}/stream?streams={stream_path}"
    
    print(f"Connecting to Binance WS: {base_url} (Testnet: {use_testnet})")
    
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                print("Successfully connected to Binance Streams!")
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if 'data' not in data or 'stream' not in data:
                        continue
                        
                    stream_name = data['stream']
                    payload = data['data']
                    
                    # Ensure lowercase matching
                    symbol = payload.get('s', '').lower()
                    
                    if not symbol or symbol not in state:
                        continue
                        
                    # Handle @ticker stream (Price & Volume updates)
                    if stream_name.endswith('@ticker'):
                        state[symbol]['latest_price'] = float(payload.get('c', 0)) # Last price
                        state[symbol]['volume'] = float(payload.get('v', 0))     # 24h Base Asset Volume
                    
                    # Handle @bookTicker stream (Real-time Best Bid/Ask spread)
                    elif stream_name.endswith('@bookTicker'):
                        bid = float(payload.get('b', 0))
                        ask = float(payload.get('a', 0))
                        state[symbol]['bid'] = bid
                        state[symbol]['ask'] = ask
                        
                        # Calculate Spread in Basis Points (BPS)
                        if ask > 0 and bid > 0:
                            # (Ask - Bid) / MidPrice * 10000
                            mid_price = (ask + bid) / 2
                            spread = ((ask - bid) / mid_price) * 10000
                            state[symbol]['spread_bps'] = round(spread, 2)
                            
        except Exception as e:
            print(f"WebSocket connection error: {e}")
            print("Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

async def data_logger():
    print("Waiting for initial data streams to populate...")
    await asyncio.sleep(5)
    
    conn = get_db_connection()
    
    while True:
        try:
            timestamp = int(time.time() * 1000)
            
            # Snapshot current prices into deque (1 per second)
            for sym in state:
                latest = state[sym].get('latest_price', 0)
                state[sym]['price'].append(latest)
            
            # Calculate BTC base features
            btc_roc_1s = get_roc(state[BTC_STREAM_ID]['price'], 1)
            btc_roc_5s = get_roc(state[BTC_STREAM_ID]['price'], 5)
            btc_price = state[BTC_STREAM_ID]['price'][-1] if len(state[BTC_STREAM_ID]['price']) > 0 else 0
            btc_vol = state[BTC_STREAM_ID]['volume']
            
            cursor = conn.cursor()
            
            for asset in alt_stream_ids():
                asset_price = state[asset]['price'][-1] if len(state[asset]['price']) > 0 else 0
                if asset_price == 0 or btc_price == 0:
                    continue
                    
                asset_roc_1s = get_roc(state[asset]['price'], 1)
                asset_roc_5s = get_roc(state[asset]['price'], 5)
                asset_vol = state[asset]['volume']
                spread_bps = state[asset]['spread_bps']
                
                # Expandable features payload for ML
                features = {
                    'bid': state[asset]['bid'],
                    'ask': state[asset]['ask'],
                    'btc_bid': state[BTC_STREAM_ID]['bid'],
                    'btc_ask': state[BTC_STREAM_ID]['ask'],
                    'btc_spread_bps': state[BTC_STREAM_ID]['spread_bps']
                }
                
                db_asset_name = stream_id_to_ccxt(asset)
                
                cursor.execute("""
                    INSERT INTO market_ticks 
                    (timestamp, target_asset, target_price, btc_price, btc_roc_1s, btc_roc_5s, target_roc_1s, target_roc_5s, btc_volume_delta, target_volume_delta, spread_bps, features_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    timestamp, db_asset_name, asset_price, btc_price, 
                    btc_roc_1s, btc_roc_5s, asset_roc_1s, asset_roc_5s, 
                    btc_vol, asset_vol, spread_bps, json.dumps(features)
                ))
                
            conn.commit()
            
        except Exception as e:
            print(f"Error in data logger: {e}")
            
        # Sleep exactly 1 second to maintain 1s grid
        await asyncio.sleep(1)

async def main():
    print("Starting Native Binance WebSocket streams for Lead-Lag Arbitrage ML...")
    print(f"Tracking {len(TRACKED_USDT_STREAM_IDS)} pairs: {', '.join(TRACKED_USDT_STREAM_IDS)}")
    
    # Run the WS listener and DB logger concurrently
    await asyncio.gather(
        binance_ws_listener(),
        data_logger()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Data collection stopped.")
