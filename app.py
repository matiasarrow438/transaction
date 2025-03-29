from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import requests
import json
from datetime import datetime
import threading
import time
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///wallets.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Use Vibestation RPC endpoint
VIBESTATION_RPC_URL = 'http://basic.swqos.solanavibestation.com/?api_key=a25cf1b7c66c7795925ed2486645a57f'
# Backup RPC URLs if needed
# VIBESTATION_RPC_URL = 'https://api.mainnet-beta.solana.com'
# VIBESTATION_RPC_URL = 'https://rpc.ankr.com/solana'

# Cache for balances
balance_cache = {}
balance_cache_timeout = 5  # Reduced from 10 to 5 seconds for faster updates

# Configure requests session with retries
session = requests.Session()
session.mount('http://', requests.adapters.HTTPAdapter(
    max_retries=2,  # Reduced retries for faster response
    pool_connections=20,  # Increased for better performance
    pool_maxsize=20
))

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

        response = session.post(
            VIBESTATION_RPC_URL,
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'getBalance',
                'params': [wallet_address]
            },
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            timeout=5  # Reduced timeout for faster response
        )
        
        if not response.ok:
            raise Exception(f'RPC API error: {response.status_code}')

        response_data = response.json()
        if 'error' in response_data:
            error_msg = response_data['error'].get('message', 'Unknown error')
            if 'Invalid account' in error_msg:
                raise Exception('Invalid Solana wallet address')
            raise Exception(f'RPC API error: {error_msg}')
            
        if not response_data.get('result'):
            raise Exception('Invalid response from RPC API')
            
        balance = response_data['result']['value'] / 1e9  # Convert lamports to SOL
        
        # Update cache
        balance_cache[wallet_address] = (balance, current_time)
        
        return balance
                
    except Exception as e:
        print(f"Error fetching balance: {str(e)}")
        raise

def get_wallet_transactions(wallet_address):
    try:
        # Get recent signatures with increased limit
        response = session.post(
            VIBESTATION_RPC_URL,
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
                    VIBESTATION_RPC_URL,
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

def update_wallet(wallet):
    try:
        print(f"Updating wallet {wallet.address}...")
        balance = get_wallet_balance(wallet.address)
        print(f"Got balance: {balance} SOL")
        wallet.last_balance = balance
        wallet.last_updated = datetime.utcnow()
        db.session.commit()
        print(f"Successfully updated wallet {wallet.address}: {balance} SOL")
    except Exception as e:
        print(f"Error updating wallet {wallet.address}: {str(e)}")
        # Don't raise the exception, just log it

def background_update_task():
    print("Background update task started")
    while True:
        try:
            with app.app_context():
                active_wallets = TrackedWallet.query.filter_by(is_active=True).all()
                print(f"Found {len(active_wallets)} active wallets to update")
                for wallet in active_wallets:
                    update_wallet(wallet)
                    time.sleep(0.5)  # Reduced delay between wallet updates
            time.sleep(15)  # Update every 15 seconds instead of 30
        except Exception as e:
            print(f"Error in background task: {str(e)}")
            time.sleep(2)  # Reduced error retry delay

# Initialize database and start background task
init_db()
update_thread = threading.Thread(target=background_update_task, daemon=True)
update_thread.start()

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/wallet/<wallet_address>', methods=['GET', 'POST'])
def get_wallet_info(wallet_address):
    try:
        # Validate wallet address format
        if not validate_solana_address(wallet_address):
            return jsonify({'error': 'Invalid Solana wallet address format. Please enter a valid Solana address.'}), 400

        if request.method == 'POST':
            data = request.get_json()
            wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
            if wallet:
                return jsonify({'error': 'Wallet already exists'}), 400
                
            # Try to get initial balance to validate the wallet exists
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
        
        # Update the wallet's balance in the database
        wallet.last_balance = balance
        wallet.last_updated = datetime.utcnow()
        db.session.commit()
        print(f"Updated wallet {wallet_address} balance to {balance} SOL")
        
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
        wallet.is_active = False
        db.session.commit()
        return jsonify({'message': 'Wallet deleted successfully'})
    return jsonify({'error': 'Wallet not found'}), 404

@app.route('/api/wallet/<wallet_address>/toggle', methods=['POST'])
def toggle_wallet(wallet_address):
    wallet = TrackedWallet.query.filter_by(address=wallet_address).first()
    if wallet:
        wallet.is_active = not wallet.is_active
        db.session.commit()
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
        
        return jsonify({
            'message': 'Wallet renamed successfully',
            'wallet': wallet.to_dict()
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    with app.app_context():
        init_db()
    # Get port from environment variable or use 5000 as default
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
