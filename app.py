from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
import requests
import json
from datetime import datetime
import threading
import time
import os
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///wallets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'your-secret-key'  # Required for Flask-SocketIO
db = SQLAlchemy(app)

# Initialize SocketIO
socketio = SocketIO(app)

# Use Vibestation RPC endpoint with fallbacks
RPC_ENDPOINTS = [
    'http://basic.swqos.solanavibestation.com/?api_key=a25cf1b7c66c7795925ed2486645a57f',
    'https://api.mainnet-beta.solana.com',
    'https://rpc.ankr.com/solana'
]

# Cache for balances with shorter timeout
balance_cache = {}
balance_cache_timeout = 0.5  # Reduced to 500ms for faster updates

# Configure requests session with optimized settings
session = requests.Session()
session.mount('http://', requests.adapters.HTTPAdapter(
    max_retries=0,  # No retries for faster response
    pool_connections=100,  # Increased for better performance
    pool_maxsize=100
))

# Thread pool for parallel processing
executor = ThreadPoolExecutor(max_workers=20)

class TrackedWallet(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    address = db.Column(db.String(44), unique=True, nullable=False)
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_balance = db.Column(db.Float)
    last_updated = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)
    notifications_enabled = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'address': self.address,
            'name': self.name or self.address[:8] + '...',
            'created_at': self.created_at.isoformat(),
            'last_balance': self.last_balance,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'is_active': self.is_active,
            'notifications_enabled': self.notifications_enabled
        }

def init_db():
    with app.app_context():
        # Drop all tables if they exist
        db.drop_all()
        # Create all tables
        db.create_all()
        print("Database initialized successfully")

def validate_solana_address(address):
    """Validate if a string is a valid Solana address."""
    try:
        # Check length (Solana addresses are 32-44 characters)
        if not address or len(address) < 32 or len(address) > 44:
            return False
            
        # Check if it contains only base58 characters
        valid_chars = set('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz')
        return all(c in valid_chars for c in address)
    except:
        return False

def get_wallet_balance(wallet_address):
    try:
        # Check cache first
        current_time = time.time()
        if wallet_address in balance_cache:
            cached_balance, cache_time = balance_cache[wallet_address]
            if current_time - cache_time < balance_cache_timeout:
                return cached_balance

        # Validate wallet address format
        if not validate_solana_address(wallet_address):
            raise Exception('Invalid Solana wallet address format')

        # Try each RPC endpoint until one works
        for endpoint in RPC_ENDPOINTS:
            try:
                response = session.post(
                    endpoint,
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'getBalance',
                        'params': [wallet_address]
                    },
                    headers={
                        'Content-Type': 'application/json',
                    },
                    timeout=1  # Reduced timeout for faster response
                )
                
                if response.ok:
                    response_data = response.json()
                    if 'result' in response_data:
                        balance = response_data['result']['value'] / 1e9
                        balance_cache[wallet_address] = (balance, current_time)
                        return balance
            except:
                continue

        # If all endpoints fail, return cached balance if available
        if wallet_address in balance_cache:
            return balance_cache[wallet_address][0]
        raise Exception('Failed to fetch balance from all RPC endpoints')
                
    except Exception as e:
        print(f"Error fetching balance: {str(e)}")
        if wallet_address in balance_cache:
            return balance_cache[wallet_address][0]
        raise

def get_wallet_transactions(wallet_address):
    try:
        # Get recent signatures with increased limit
        response = session.post(
            RPC_ENDPOINTS[0],
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'getSignaturesForAddress',
                'params': [
                    wallet_address,
                    {'limit': 50}  # Increased from 10 to 50 transactions
                ]
            },
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            timeout=10
        )
        
        if not response.ok:
            return []

        response_data = response.json()
        if 'error' in response_data or not response_data.get('result'):
            return []
            
        signatures = [tx['signature'] for tx in response_data['result']]
        
        # Get transaction details for each signature
        transactions = []
        for i, signature in enumerate(signatures):
            try:
                # Reduced delay between requests
                if i > 0:
                    time.sleep(0.1)  # Reduced delay to 100ms for faster loading
                
                tx_response = session.post(
                    RPC_ENDPOINTS[0],
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'getTransaction',
                        'params': [
                            signature,
                            {
                                'encoding': 'jsonParsed',
                                'maxSupportedTransactionVersion': 0
                            }
                        ]
                    },
                    headers={
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    timeout=10
                )
                
                if not tx_response.ok:
                    continue
                    
                tx_data = tx_response.json()
                if 'error' in tx_data or not tx_data.get('result'):
                    continue
                    
                tx = tx_data['result']
                if not tx.get('meta') or not tx.get('transaction'):
                    continue
                    
                pre_balances = tx['meta']['preBalances']
                post_balances = tx['meta']['postBalances']
                
                account_keys = tx['transaction']['message']['accountKeys']
                account_index = next(
                    (i for i, key in enumerate(account_keys)
                     if key['pubkey'] == wallet_address),
                    -1
                )
                
                if account_index == -1:
                    continue
                    
                balance_change = (post_balances[account_index] - pre_balances[account_index]) / 1e9
                
                if balance_change > 0:
                    type = 'incoming'
                    amount = balance_change
                    sender = account_keys[1]['pubkey'] if len(account_keys) > 1 else 'Unknown'
                    recipient = wallet_address
                else:
                    type = 'outgoing'
                    amount = abs(balance_change)
                    sender = wallet_address
                    recipient = account_keys[1]['pubkey'] if len(account_keys) > 1 else 'Unknown'
                
                if amount == 0:
                    continue
                
                transactions.append({
                    'signature': signature,
                    'type': type,
                    'amount': amount,
                    'sender': sender,
                    'recipient': recipient,
                    'timestamp': tx.get('blockTime', 0) * 1000
                })
                
            except Exception as e:
                continue
        
        return transactions
        
    except Exception as e:
        return []

# Socket.IO event handlers
@socketio.on('connect')
def handle_connect():
    print('Client connected')
    # Send current wallets to the new client
    try:
        with app.app_context():
            wallets = TrackedWallet.query.filter_by(is_active=True).all()
            for wallet in wallets:
                try:
                    balance = get_wallet_balance(wallet.address)
                    wallet_data = wallet.to_dict()
                    wallet_data.update({
                        'balance': balance,
                        'type': 'initial_load'
                    })
                    emit('wallet_update', wallet_data)
                except Exception as e:
                    print(f"Error sending wallet {wallet.address} to new client: {str(e)}")
    except Exception as e:
        print(f"Error in handle_connect: {str(e)}")

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

def broadcast_wallet_update(wallet_data):
    """Broadcast wallet updates to all connected clients"""
    try:
        print(f"Broadcasting update: {wallet_data}")
        # Add timestamp to track update order
        wallet_data['timestamp'] = int(time.time() * 1000)
        
        # Force a type if none is provided
        if 'type' not in wallet_data:
            wallet_data['type'] = 'balance_update'
            
        # Broadcast to all clients including sender
        socketio.emit('wallet_update', wallet_data, broadcast=True)
        print(f"Broadcast complete for {wallet_data.get('address')}")
            
    except Exception as e:
        print(f"Error broadcasting update: {str(e)}")

def update_wallet(wallet):
    try:
        balance = get_wallet_balance(wallet.address)
        if balance != wallet.last_balance:
            wallet.last_balance = balance
            wallet.last_updated = datetime.utcnow()
            db.session.commit()
            # Only broadcast if balance changed
            broadcast_wallet_update(wallet.to_dict())
    except Exception as e:
        print(f"Error updating wallet {wallet.address}: {str(e)}")

def update_wallet_balances():
    """Update all active wallet balances in parallel"""
    while True:
        try:
            with app.app_context():
                active_wallets = TrackedWallet.query.filter_by(is_active=True).all()
                if not active_wallets:
                    time.sleep(1)
                    continue

                def update_wallet(wallet):
                    try:
                        balance = get_wallet_balance(wallet.address)
                        if balance != wallet.last_balance:
                            wallet.last_balance = balance
                            wallet.last_updated = datetime.utcnow()
                            db.session.commit()
                            
                            # Broadcast the update
                            wallet_data = wallet.to_dict()
                            wallet_data.update({
                                'balance': balance,
                                'type': 'balance_update'
                            })
                            broadcast_wallet_update(wallet_data)
                            print(f"Updated and broadcast balance for {wallet.address}: {balance} SOL")
                    except Exception as e:
                        print(f"Error updating wallet {wallet.address}: {str(e)}")

                # Update all wallets in parallel
                list(executor.map(update_wallet, active_wallets))
                
        except Exception as e:
            print(f"Error in update thread: {str(e)}")
        
        time.sleep(0.5)  # Check for updates every 500ms

# Initialize database and start background task
init_db()
update_thread = threading.Thread(target=update_wallet_balances, daemon=True)
update_thread.start()

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/wallet/<wallet_address>', methods=['GET', 'POST'])
def get_wallet_info(wallet_address):
    try:
        if not validate_solana_address(wallet_address):
            return jsonify({'error': 'Invalid Solana wallet address format. Please enter a valid Solana address.'}), 400

        if request.method == 'POST':
            data = request.get_json()
            wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
            if wallet:
                return jsonify({'error': 'Wallet already exists'}), 400
                
            try:
                initial_balance = get_wallet_balance(wallet_address)
                print(f"Initial balance for {wallet_address}: {initial_balance} SOL")
            except Exception as e:
                return jsonify({'error': f'Invalid wallet address: {str(e)}'}), 400
                
            wallet = TrackedWallet(
                address=wallet_address,
                name=data.get('name'),
                is_active=True,
                notifications_enabled=data.get('notifications_enabled', False),
                last_balance=initial_balance,
                last_updated=datetime.utcnow()
            )
            db.session.add(wallet)
            db.session.commit()
            print(f"Added new wallet {wallet_address} with balance {initial_balance} SOL")
            
            # Get transactions for the new wallet
            transactions = get_wallet_transactions(wallet_address)
            
            # Broadcast the new wallet with full data to all connected clients
            wallet_data = wallet.to_dict()
            wallet_data.update({
                'balance': initial_balance,
                'transactions': transactions,
                'type': 'new_wallet'  # Indicate this is a new wallet
            })
            broadcast_wallet_update(wallet_data)
            
            return jsonify({
                'balance': initial_balance,
                'transactions': transactions,
                'wallet': wallet.to_dict()
            })

        # GET request handling
        try:
            balance = get_wallet_balance(wallet_address)
            transactions = get_wallet_transactions(wallet_address)
            print(f"Fetched balance for {wallet_address}: {balance} SOL")
        except Exception as e:
            print(f"Error fetching wallet data: {str(e)}")
            return jsonify({'error': f'Failed to fetch wallet data: {str(e)}'}), 500
        
        wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
        if not wallet:
            return jsonify({'error': 'Wallet not found'}), 404
        
        if balance != wallet.last_balance:
            wallet.last_balance = balance
            wallet.last_updated = datetime.utcnow()
            db.session.commit()
            print(f"Updated wallet {wallet_address} balance to {balance} SOL")
            
            # Broadcast balance update
            wallet_data = wallet.to_dict()
            wallet_data.update({
                'balance': balance,
                'type': 'balance_update'
            })
            broadcast_wallet_update(wallet_data)
        
        return jsonify({
            'balance': balance,
            'transactions': transactions,
            'wallet': wallet.to_dict()
        })
    except Exception as e:
        print(f"Error in get_wallet_info: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/wallets')
def get_tracked_wallets():
    wallets = TrackedWallet.query.order_by(TrackedWallet.last_updated.desc()).all()
    return jsonify([wallet.to_dict() for wallet in wallets])

@app.route('/api/wallet/<wallet_address>', methods=['DELETE'])
def delete_wallet(wallet_address):
    wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
    if wallet:
        wallet_data = wallet.to_dict()
        wallet_data['type'] = 'delete'
        db.session.delete(wallet)
        db.session.commit()
        # Broadcast the deletion immediately
        broadcast_wallet_update(wallet_data)
        return jsonify({'message': 'Wallet deleted successfully'})
    return jsonify({'error': 'Wallet not found'}), 404

@app.route('/api/wallet/<wallet_address>/toggle', methods=['POST'])
def toggle_wallet(wallet_address):
    wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
    if wallet:
        wallet.is_active = not wallet.is_active
        db.session.commit()
        # Broadcast the toggle update
        wallet_data = wallet.to_dict()
        wallet_data['type'] = 'toggle'
        broadcast_wallet_update(wallet_data)
        return jsonify({'message': 'Wallet status updated successfully', 'is_active': wallet.is_active})
    return jsonify({'error': 'Wallet not found'}), 404

@app.route('/api/wallet/<wallet_address>/notifications', methods=['POST'])
def toggle_notifications(wallet_address):
    try:
        wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
        if not wallet:
            return jsonify({'error': 'Wallet not found'}), 404
            
        data = request.get_json()
        wallet.notifications_enabled = data.get('notifications_enabled', False)
        db.session.commit()
        
        # Broadcast the notifications update
        wallet_data = wallet.to_dict()
        wallet_data['type'] = 'notifications'
        broadcast_wallet_update(wallet_data)
        
        return jsonify({
            'message': 'Notifications updated successfully',
            'notifications_enabled': wallet.notifications_enabled
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wallet/<wallet_address>/rename', methods=['POST'])
def rename_wallet(wallet_address):
    try:
        wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
        if not wallet:
            return jsonify({'error': 'Wallet not found'}), 404
            
        data = request.get_json()
        new_name = data.get('name')
        
        if not new_name or new_name.strip() == '':
            return jsonify({'error': 'Invalid wallet name'}), 400
            
        wallet.name = new_name.strip()
        db.session.commit()
        
        # Broadcast the rename update
        wallet_data = wallet.to_dict()
        wallet_data['type'] = 'rename'
        broadcast_wallet_update(wallet_data)
        
        return jsonify({
            'message': 'Wallet renamed successfully',
            'wallet': wallet.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("Server initialized for threading.")
    # Initialize database
    with app.app_context():
        init_db()
    
    # Start the background update thread
    update_thread = threading.Thread(target=update_wallet_balances, daemon=True)
    update_thread.start()
    
    # Run the Socket.IO server with minimal configuration
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
