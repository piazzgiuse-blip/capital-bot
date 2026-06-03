from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import requests
import threading
import time
from datetime import datetime
from typing import Dict, Tuple, Optional
import json
import os
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
EMAIL      = os.getenv("EMAIL", "giuseppepiazzolla43@gmail.com")
PASSWORD   = os.getenv("PASSWORD", "Peppe2013$")
API_KEY    = os.getenv("API_KEY", "adefwHN077PxxrR3")
BASE_URL   = "https://api-capital.backend-capital.com/api/v1"

EURUSD_EPIC = "CS.D.EURUSD.CFD.IP"
J225_EPIC   = "CS.D.JPXNKY.CFD.IP"

EURUSD_PROFIT_TARGET = 0.01
J225_LOSS_LIMIT      = -0.18
J225_PROFIT_TARGET   = 0.01

# Balance threshold per switchare a J225 only
BALANCE_THRESHOLD_FOR_J225_ONLY = 500.0  # Quando balance >= 500, solo J225

POLL_INTERVAL = 2
RETRY_WAIT    = 5

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# ── BOT STATE ─────────────────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.cst = None
        self.token = None
        self.session_time = None
        self.trades_history = []
        self.running = False
        self.eurusd_position = None
        self.j225_positions = []  # Lista di max 2 posizioni J225
        self.stats = {
            "totalTrades": 0,
            "winningTrades": 0,
            "totalProfit": 0.0,
            "avgProfit": 0.0
        }
        self.balance = 0.0
        self.mode = "EURUSD"  # "EURUSD" o "J225_ONLY"
        self.lock = threading.Lock()

state = BotState()

# ── AUTH ──────────────────────────────────────────────────────────────────────
def login():
    """Autentica con email/password"""
    try:
        resp = requests.post(
            f"{BASE_URL}/session",
            headers={
                "Content-Type": "application/json",
                "X-CAP-API-KEY": API_KEY
            },
            json={
                "identifier": EMAIL,
                "password": PASSWORD
            },
            timeout=10
        )
        resp.raise_for_status()
        
        cst = resp.headers.get("CST")
        token = resp.headers.get("X-SECURITY-TOKEN")
        
        if not cst or not token:
            raise ValueError("Missing CST or X-SECURITY-TOKEN")
        
        print(f"[{ts()}] ✅ Login successful")
        return cst, token
    except Exception as e:
        print(f"[{ts()}] ❌ Login error: {e}")
        raise

def headers(cst, token):
    return {
        "Content-Type": "application/json",
        "X-CAP-API-KEY": API_KEY,
        "CST": cst,
        "X-SECURITY-TOKEN": token
    }

def ensure_session():
    """Rinnova sessione se scaduta"""
    try:
        if state.cst is None or time.time() - state.session_time > 28800:
            state.cst, state.token = login()
            state.session_time = time.time()
    except Exception as e:
        print(f"[{ts()}] Session error: {e}")

def get_balance() -> float:
    """Ritorna il balance dell'account"""
    ensure_session()
    try:
        resp = requests.get(
            f"{BASE_URL}/accounts",
            headers=headers(state.cst, state.token),
            timeout=10
        )
        resp.raise_for_status()
        accounts = resp.json().get("accounts", [])
        if accounts:
            balance = float(accounts[0].get("balance", 0))
            with state.lock:
                state.balance = balance
                # Controlla se switchare modalità
                if balance >= BALANCE_THRESHOLD_FOR_J225_ONLY and state.mode == "EURUSD":
                    state.mode = "J225_ONLY"
                    print(f"\n[{ts()}] 🚀 MODALITÀ SWITCHED: EUR/USD → J225 ONLY (balance: {balance})")
            return balance
        return 0.0
    except Exception as e:
        print(f"[{ts()}] Balance error: {e}")
        return 0.0

# ── MARKET DATA ───────────────────────────────────────────────────────────────
def get_price(epic: str) -> Tuple[Optional[float], Optional[float]]:
    """Ritorna (bid, ask)"""
    ensure_session()
    try:
        resp = requests.get(
            f"{BASE_URL}/markets/{epic}",
            headers=headers(state.cst, state.token),
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        bid = float(data["snapshot"]["bid"])
        ask = float(data["snapshot"]["offer"])
        return bid, ask
    except Exception as e:
        print(f"[{ts()}] Price error for {epic}: {e}")
        return None, None

def get_trend(epic: str) -> str:
    """Determina trend"""
    ensure_session()
    try:
        resp = requests.get(
            f"{BASE_URL}/prices/{epic}",
            headers=headers(state.cst, state.token),
            params={"resolution": "HOUR", "max": 20},
            timeout=10
        )
        resp.raise_for_status()
        prices = resp.json().get("prices", [])
        
        if len(prices) < 2:
            return "BUY"
        
        closes = [p["closePrice"]["bid"] for p in prices]
        return "BUY" if closes[-1] > closes[0] else "SELL"
    except Exception as e:
        print(f"[{ts()}] Trend error: {e}")
        return "BUY"

def search_knockout_markets(market_name: str) -> list:
    """Cerca knockout disponibili"""
    ensure_session()
    try:
        resp = requests.get(
            f"{BASE_URL}/markets",
            headers=headers(state.cst, state.token),
            params={"searchTerm": market_name, "pageSize": 100},
            timeout=10
        )
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
        knockouts = [m for m in markets if m.get("instrumentType") == "KNOCKOUT" or "KO" in m.get("epic", "")]
        return knockouts
    except Exception as e:
        print(f"[{ts()}] Search error: {e}")
        return []

def get_nearest_knockout(market_name: str, direction: str) -> Optional[str]:
    """Trova knockout più vicino"""
    knockouts = search_knockout_markets(market_name)
    
    if not knockouts:
        return None
    
    try:
        epic_ref = EURUSD_EPIC if "EUR" in market_name.upper() else J225_EPIC
        bid, ask = get_price(epic_ref)
        if bid is None:
            return None
        
        ref_price = ask if direction == "BUY" else bid
        
        candidates = []
        for ko in knockouts:
            epic = ko.get("epic")
            ko_level = ko.get("knockoutLevel")
            
            if ko_level is None:
                continue
            
            distance = abs(float(ko_level) - ref_price)
            candidates.append((distance, epic, ko_level))
        
        if not candidates:
            return None
        
        candidates.sort(key=lambda x: x[0])
        _, selected_epic, ko_level = candidates[0]
        print(f"[{ts()}] Selected knockout: {selected_epic} | Level: {ko_level}")
        return selected_epic
    except Exception as e:
        print(f"[{ts()}] Knockout error: {e}")
        return None

# ── ORDERS ────────────────────────────────────────────────────────────────────
def open_position(epic: str, direction: str, size: int = 1) -> Optional[str]:
    """Apre posizione"""
    ensure_session()
    try:
        body = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET"
        }
        
        resp = requests.post(
            f"{BASE_URL}/positions",
            headers=headers(state.cst, state.token),
            json=body,
            timeout=10
        )
        resp.raise_for_status()
        
        deal = resp.json()
        deal_id = deal.get("dealId")
        
        print(f"[{ts()}] Position opened: {epic} | dir={direction} | id={deal_id}")
        return deal_id
    except Exception as e:
        print(f"[{ts()}] Open position error: {e}")
        return None

def get_open_positions() -> list:
    """Ritorna posizioni aperte"""
    ensure_session()
    try:
        resp = requests.get(
            f"{BASE_URL}/positions",
            headers=headers(state.cst, state.token),
            timeout=10
        )
        resp.raise_for_status()
        return resp.json().get("positions", [])
    except Exception as e:
        print(f"[{ts()}] Get positions error: {e}")
        return []

def get_position(deal_id: str) -> Optional[Dict]:
    """Ritorna dettagli posizione"""
    positions = get_open_positions()
    for pos_data in positions:
        if pos_data.get("position", {}).get("dealId") == deal_id:
            return pos_data
    return None

def close_position(deal_id: str, size: int = 1) -> bool:
    """Chiude posizione"""
    ensure_session()
    try:
        resp = requests.delete(
            f"{BASE_URL}/positions/{deal_id}",
            headers=headers(state.cst, state.token),
            json={"size": size},
            timeout=10
        )
        resp.raise_for_status()
        print(f"[{ts()}] Position closed: {deal_id}")
        return True
    except Exception as e:
        print(f"[{ts()}] Close position error: {e}")
        return False

# ── HELPERS ───────────────────────────────────────────────────────────────────
def ts():
    return datetime.now().strftime("%H:%M:%S")

def calculate_pnl(position_data: Dict, current_bid: float, current_ask: float) -> float:
    """Calcola P&L"""
    pos = position_data.get("position", {})
    direction = pos.get("direction")
    open_level = float(pos.get("openLevel", 0))
    size = pos.get("size", 1)
    
    current_level = current_ask if direction == "BUY" else current_bid
    
    if direction == "BUY":
        pnl = (current_level - open_level) * size
    else:
        pnl = (open_level - current_level) * size
    
    return pnl

def broadcast_update():
    """Invia aggiornamento ai client WebSocket"""
    socketio.emit('update', {
        'eurusdPosition': state.eurusd_position,
        'j225Positions': state.j225_positions,
        'stats': state.stats,
        'tradesHistory': state.trades_history[-20:],
        'running': state.running,
        'balance': state.balance,
        'mode': state.mode
    }, broadcast=True)

# ── TRADING LOOPS ─────────────────────────────────────────────────────────────
def trade_eurusd():
    """EUR/USD: chiude a +0.01 - SOLO quando mode è EURUSD"""
    print(f"\n[{ts()}] 🚀 EUR/USD bot started")
    
    while state.running:
        try:
            # Controlla balance continuamente
            get_balance()
            
            # Se siamo in modalità J225_ONLY, salta EUR/USD
            if state.mode == "J225_ONLY":
                time.sleep(5)
                continue
            
            direction = get_trend(EURUSD_EPIC)
            print(f"[{ts()}] EUR/USD Trend: {direction}")
            
            ko_epic = get_nearest_knockout("EUR/USD", direction)
            if not ko_epic:
                time.sleep(RETRY_WAIT)
                continue
            
            deal_id = open_position(ko_epic, direction, size=1)
            if not deal_id:
                time.sleep(RETRY_WAIT)
                continue
            
            time.sleep(1)
            
            target_reached = False
            while state.running and not target_reached and state.mode == "EURUSD":
                pos_data = get_position(deal_id)
                
                if not pos_data:
                    print(f"[{ts()}] EUR/USD position not found")
                    break
                
                bid, ask = get_price(ko_epic)
                if bid is None:
                    time.sleep(POLL_INTERVAL)
                    continue
                
                pnl = calculate_pnl(pos_data, bid, ask)
                
                with state.lock:
                    state.eurusd_position = {
                        'market': 'EUR/USD',
                        'direction': direction,
                        'pnl': round(pnl, 4),
                        'openLevel': float(pos_data['position'].get('openLevel', 0)),
                        'currentLevel': ask if direction == 'BUY' else bid,
                        'duration': int(time.time())
                    }
                
                broadcast_update()
                print(f"[{ts()}] EUR/USD P&L: {pnl:.4f}")
                
                if pnl >= EURUSD_PROFIT_TARGET:
                    close_position(deal_id, size=1)
                    
                    with state.lock:
                        state.stats['totalTrades'] += 1
                        state.stats['winningTrades'] += 1
                        state.stats['totalProfit'] += pnl
                        state.stats['avgProfit'] = state.stats['totalProfit'] / state.stats['totalTrades']
                        state.trades_history.insert(0, {
                            'market': 'EUR/USD',
                            'direction': direction,
                            'pnl': round(pnl, 4),
                            'timestamp': ts()
                        })
                        state.eurusd_position = None
                    
                    broadcast_update()
                    print(f"[{ts()}] ✅ EUR/USD target: +{pnl:.4f}")
                    target_reached = True
                
                time.sleep(POLL_INTERVAL)
        
        except Exception as e:
            print(f"[{ts()}] EUR/USD error: {e}")
            time.sleep(RETRY_WAIT)

def trade_j225_single():
    """J225 singolo: chiude tra -0.18 e +infinito - SOLO quando mode è EURUSD"""
    # Questo non viene più usato in modalità EURUSD se la idea è fare EUR/USD prima
    # Ma lo teniamo per sicurezza
    print(f"\n[{ts()}] 🚀 J225 single bot started")
    
    while state.running:
        try:
            if state.mode == "J225_ONLY":
                time.sleep(5)
                continue
            
            direction = get_trend(J225_EPIC)
            print(f"[{ts()}] J225 Trend: {direction}")
            
            ko_epic = get_nearest_knockout("Nikkei 225", direction)
            if not ko_epic:
                time.sleep(RETRY_WAIT)
                continue
            
            deal_id = open_position(ko_epic, direction, size=1)
            if not deal_id:
                time.sleep(RETRY_WAIT)
                continue
            
            time.sleep(1)
            
            closed = False
            while state.running and not closed and state.mode == "EURUSD":
                pos_data = get_position(deal_id)
                
                if not pos_data:
                    print(f"[{ts()}] J225 position not found")
                    break
                
                bid, ask = get_price(J225_EPIC)
                if bid is None:
                    time.sleep(POLL_INTERVAL)
                    continue
                
                pnl = calculate_pnl(pos_data, bid, ask)
                
                # Aggiorna lista posizioni (max 1 in single mode)
                with state.lock:
                    if state.j225_positions:
                        state.j225_positions[0] = {
                            'market': 'J225',
                            'direction': direction,
                            'pnl': round(pnl, 4),
                            'openLevel': float(pos_data['position'].get('openLevel', 0)),
                            'currentLevel': ask if direction == 'BUY' else bid,
                            'duration': int(time.time()),
                            'dealId': deal_id
                        }
                
                broadcast_update()
                print(f"[{ts()}] J225 P&L: {pnl:.4f}")
                
                if pnl >= J225_LOSS_LIMIT:
                    close_position(deal_id, size=1)
                    
                    with state.lock:
                        state.stats['totalTrades'] += 1
                        if pnl > 0:
                            state.stats['winningTrades'] += 1
                        state.stats['totalProfit'] += pnl
                        state.stats['avgProfit'] = state.stats['totalProfit'] / state.stats['totalTrades']
                        state.trades_history.insert(0, {
                            'market': 'J225',
                            'direction': direction,
                            'pnl': round(pnl, 4),
                            'timestamp': ts()
                        })
                        state.j225_positions = []
                    
                    broadcast_update()
                    print(f"[{ts()}] ✅ J225 closed: {pnl:.4f}")
                    closed = True
                
                time.sleep(POLL_INTERVAL)
        
        except Exception as e:
            print(f"[{ts()}] J225 single error: {e}")
            time.sleep(RETRY_WAIT)

def trade_j225_dual():
    """J225 DUAL: apre 2 posizioni simultaneamente quando mode è J225_ONLY"""
    print(f"\n[{ts()}] 🚀 J225 DUAL bot started (2 positions)")
    
    while state.running:
        try:
            # Controlla balance
            get_balance()
            
            # SOLO quando mode è J225_ONLY
            if state.mode != "J225_ONLY":
                time.sleep(5)
                continue
            
            # Apri 2 posizioni J225
            direction = get_trend(J225_EPIC)
            print(f"[{ts()}] J225 Trend (DUAL): {direction}")
            
            ko_epic_1 = get_nearest_knockout("Nikkei 225", direction)
            if not ko_epic_1:
                time.sleep(RETRY_WAIT)
                continue
            
            # Aspetta un attimo per non aprire esattamente allo stesso tempo
            time.sleep(0.5)
            
            ko_epic_2 = get_nearest_knockout("Nikkei 225", direction)
            if not ko_epic_2:
                time.sleep(RETRY_WAIT)
                continue
            
            deal_id_1 = open_position(ko_epic_1, direction, size=1)
            if not deal_id_1:
                time.sleep(RETRY_WAIT)
                continue
            
            time.sleep(0.5)
            
            deal_id_2 = open_position(ko_epic_2, direction, size=1)
            if not deal_id_2:
                # Se fallisce la seconda, chiudi la prima
                close_position(deal_id_1, size=1)
                time.sleep(RETRY_WAIT)
                continue
            
            print(f"[{ts()}] ✅ DUAL J225 positions opened: {deal_id_1} & {deal_id_2}")
            time.sleep(1)
            
            closed_count = 0
            while state.running and closed_count < 2 and state.mode == "J225_ONLY":
                # Monitoraggio posizione 1
                pos_data_1 = get_position(deal_id_1)
                bid, ask = get_price(J225_EPIC)
                
                if pos_data_1 and bid is not None:
                    pnl_1 = calculate_pnl(pos_data_1, bid, ask)
                    
                    if pnl_1 >= J225_LOSS_LIMIT:
                        close_position(deal_id_1, size=1)
                        
                        with state.lock:
                            state.stats['totalTrades'] += 1
                            if pnl_1 > 0:
                                state.stats['winningTrades'] += 1
                            state.stats['totalProfit'] += pnl_1
                            state.stats['avgProfit'] = state.stats['totalProfit'] / state.stats['totalTrades']
                            state.trades_history.insert(0, {
                                'market': 'J225',
                                'direction': direction,
                                'pnl': round(pnl_1, 4),
                                'timestamp': ts()
                            })
                        
                        print(f"[{ts()}] ✅ J225 #1 closed: {pnl_1:.4f}")
                        closed_count += 1
                    else:
                        # Aggiorna UI
                        with state.lock:
                            if len(state.j225_positions) > 0:
                                state.j225_positions[0] = {
                                    'market': 'J225 #1',
                                    'direction': direction,
                                    'pnl': round(pnl_1, 4),
                                    'openLevel': float(pos_data_1['position'].get('openLevel', 0)),
                                    'currentLevel': ask if direction == 'BUY' else bid,
                                    'duration': int(time.time()),
                                    'dealId': deal_id_1
                                }
                
                # Monitoraggio posizione 2
                pos_data_2 = get_position(deal_id_2)
                bid, ask = get_price(J225_EPIC)
                
                if pos_data_2 and bid is not None:
                    pnl_2 = calculate_pnl(pos_data_2, bid, ask)
                    
                    if pnl_2 >= J225_LOSS_LIMIT:
                        close_position(deal_id_2, size=1)
                        
                        with state.lock:
                            state.stats['totalTrades'] += 1
                            if pnl_2 > 0:
                                state.stats['winningTrades'] += 1
                            state.stats['totalProfit'] += pnl_2
                            state.stats['avgProfit'] = state.stats['totalProfit'] / state.stats['totalTrades']
                            state.trades_history.insert(0, {
                                'market': 'J225',
                                'direction': direction,
                                'pnl': round(pnl_2, 4),
                                'timestamp': ts()
                            })
                        
                        print(f"[{ts()}] ✅ J225 #2 closed: {pnl_2:.4f}")
                        closed_count += 1
                    else:
                        # Aggiorna UI
                        with state.lock:
                            if len(state.j225_positions) > 1:
                                state.j225_positions[1] = {
                                    'market': 'J225 #2',
                                    'direction': direction,
                                    'pnl': round(pnl_2, 4),
                                    'openLevel': float(pos_data_2['position'].get('openLevel', 0)),
                                    'currentLevel': ask if direction == 'BUY' else bid,
                                    'duration': int(time.time()),
                                    'dealId': deal_id_2
                                }
                
                broadcast_update()
                print(f"[{ts()}] J225 DUAL P&L: #{1} (TBD) | #{2} (TBD)")
                
                time.sleep(POLL_INTERVAL)
        
        except Exception as e:
            print(f"[{ts()}] J225 DUAL error: {e}")
            time.sleep(RETRY_WAIT)

# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        'running': state.running,
        'eurusdPosition': state.eurusd_position,
        'j225Positions': state.j225_positions,
        'stats': state.stats,
        'tradesHistory': state.trades_history[-20:],
        'balance': state.balance,
        'mode': state.mode
    })

@app.route('/api/start', methods=['POST'])
def start_bot():
    if state.running:
        return jsonify({'error': 'Bot already running'}), 400
    
    state.running = True
    
    eurusd_thread = threading.Thread(target=trade_eurusd, daemon=True)
    j225_single_thread = threading.Thread(target=trade_j225_single, daemon=True)
    j225_dual_thread = threading.Thread(target=trade_j225_dual, daemon=True)
    
    eurusd_thread.start()
    j225_single_thread.start()
    j225_dual_thread.start()
    
    broadcast_update()
    return jsonify({'status': 'Bot started'})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    state.running = False
    broadcast_update()
    return jsonify({'status': 'Bot stopped'})

@app.route('/api/reset', methods=['POST'])
def reset_stats():
    with state.lock:
        state.stats = {
            "totalTrades": 0,
            "winningTrades": 0,
            "totalProfit": 0.0,
            "avgProfit": 0.0
        }
        state.trades_history = []
    
    broadcast_update()
    return jsonify({'status': 'Stats reset'})

# ── WEBSOCKET ─────────────────────────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    print(f"[{ts()}] Client connected")
    broadcast_update()

@socketio.on('disconnect')
def handle_disconnect():
    print(f"[{ts()}] Client disconnected")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
