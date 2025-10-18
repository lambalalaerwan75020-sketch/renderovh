from flask import Flask, request, jsonify, render_template_string, redirect
import os
import json
import requests
import csv
import io
import time
import re
from datetime import datetime
from werkzeug.utils import secure_filename
from urllib.parse import urlencode
from functools import wraps
import logging
import threading

# ===================================================================
# CONFIGURATION ET LOGGING
# ===================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'webhook-ovh-render-secure-v1'

# Configuration centralisée - Render.com
class Config:
    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
    CHAT_ID = os.environ.get('CHAT_ID', '-4928923400')
    OVH_LINE_NUMBER = os.environ.get('OVH_LINE_NUMBER', '0033185093039')
    ABSTRACT_API_KEY = os.environ.get('ABSTRACT_API_KEY')
    RENDER = os.environ.get('RENDER', False)  # Variable auto Render

def check_required_config():
    missing_vars = []
    if not Config.TELEGRAM_TOKEN:
        missing_vars.append('TELEGRAM_TOKEN')
    if not Config.CHAT_ID:
        missing_vars.append('CHAT_ID')
    
    if missing_vars:
        logger.error(f"❌ Variables manquantes: {', '.join(missing_vars)}")
        return False, missing_vars
    
    if Config.TELEGRAM_TOKEN and ':' not in Config.TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN invalide")
        return False, ['TELEGRAM_TOKEN (format invalide)']
    
    logger.info("✅ Configuration OK")
    logger.info(f"📱 Chat ID: {Config.CHAT_ID}")
    logger.info(f"🌐 Plateforme: Render.com")
    
    return True, []

app.config.from_object(Config)

# ===================================================================
# KEEP-ALIVE POUR RENDER (éviter sleep après 15min)
# ===================================================================

def keep_alive_ping():
    """Ping interne toutes les 10 minutes pour éviter le sleep Render"""
    while True:
        try:
            time.sleep(600)  # 10 minutes
            # Auto-ping pour rester actif
            logger.info("🔄 Keep-alive ping")
        except Exception as e:
            logger.error(f"Erreur keep-alive: {str(e)}")

# Démarrer le thread keep-alive si sur Render
if Config.RENDER or os.environ.get('RENDER_SERVICE_NAME'):
    logger.info("🚀 Mode Render détecté - Activation keep-alive")
    keep_alive_thread = threading.Thread(target=keep_alive_ping, daemon=True)
    keep_alive_thread.start()

# ===================================================================
# CACHE
# ===================================================================

class SimpleCache:
    def __init__(self):
        self.cache = {}
        self.timestamps = {}
    
    def get(self, key, ttl=3600):
        if key in self.cache:
            if time.time() - self.timestamps.get(key, 0) < ttl:
                return self.cache[key]
            else:
                del self.cache[key]
                if key in self.timestamps:
                    del self.timestamps[key]
        return None
    
    def set(self, key, value):
        self.cache[key] = value
        self.timestamps[key] = time.time()
    
    def clear(self):
        self.cache.clear()
        self.timestamps.clear()

cache = SimpleCache()

def rate_limit(calls_per_minute=30):
    def decorator(func):
        calls = []
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            now = time.time()
            calls[:] = [call_time for call_time in calls if now - call_time < 60]
            
            if len(calls) >= calls_per_minute:
                logger.warning("Rate limit exceeded")
                raise Exception("Rate limit exceeded")
            
            calls.append(now)
            return func(*args, **kwargs)
        return wrapper
    return decorator

# ===================================================================
# SERVICE DÉTECTION IBAN
# ===================================================================

class IBANDetector:
    def __init__(self):
        self.local_banks = {
            '10907': 'BNP Paribas', '30004': 'BNP Paribas',
            '30003': 'Société Générale', '30002': 'Crédit Agricole',
            '20041': 'La Banque Postale', '30056': 'BRED',
            '10278': 'Crédit Mutuel', '10906': 'CIC',
            '16798': 'ING Direct', '12548': 'Boursorama',
            '30027': 'Crédit Coopératif', '17515': 'Monabanq', '18206': 'N26'
        }
    
    def clean_iban(self, iban):
        if not iban:
            return ""
        return iban.replace(' ', '').replace('-', '').upper()
    
    def detect_local(self, iban_clean):
        if not iban_clean.startswith('FR'):
            return "Banque étrangère"
        if len(iban_clean) < 14:
            return "IBAN invalide"
        try:
            code_banque = iban_clean[4:9]
            return self.local_banks.get(code_banque, f"Banque française ({code_banque})")
        except:
            return "IBAN invalide"
    
    def detect_with_api(self, iban_clean):
        cache_key = f"iban:{iban_clean}"
        cached_result = cache.get(cache_key, ttl=86400)
        if cached_result:
            return cached_result
        
        try:
            response = requests.get(
                f"https://openiban.com/validate/{iban_clean}?getBIC=true",
                timeout=3
            )
            if response.status_code == 200:
                data = response.json()
                if data.get('valid'):
                    bank_name = data.get('bankData', {}).get('name', '')
                    if bank_name:
                        result = f"🌐 {bank_name}"
                        cache.set(cache_key, result)
                        return result
        except:
            pass
        
        return None
    
    def detect_bank(self, iban):
        if not iban:
            return "N/A"
        iban_clean = self.clean_iban(iban)
        if not iban_clean:
            return "N/A"
        api_result = self.detect_with_api(iban_clean)
        if api_result:
            return api_result
        return f"📍 {self.detect_local(iban_clean)}"

iban_detector = IBANDetector()

# ===================================================================
# SERVICE TELEGRAM
# ===================================================================

class TelegramService:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
    
    @rate_limit(calls_per_minute=30)
    def send_message(self, message):
        if not self.token or not self.chat_id:
            logger.error("❌ Token ou Chat ID manquant")
            return None
            
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            response = requests.post(url, data=data, timeout=10)
            
            if response.status_code == 200:
                logger.info("✅ Message Telegram envoyé")
                return response.json()
            else:
                logger.error(f"❌ Erreur Telegram: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Erreur Telegram: {str(e)}")
            return None
    
    def format_client_message(self, client_info, context="appel"):
        emoji = "📞" if client_info['statut'] != "Non référencé" else "❓"
        
        return f"""
{emoji} <b>{'APPEL ENTRANT' if context == 'appel' else 'RECHERCHE'}</b>
📞 Numéro: <code>{client_info['telephone']}</code>
🏢 Ligne: <code>{Config.OVH_LINE_NUMBER}</code>
🕐 {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}

👤 <b>IDENTITÉ</b>
▪️ Nom: <b>{client_info['nom']}</b>
▪️ Prénom: <b>{client_info['prenom']}</b>
🎂 Naissance: {client_info.get('date_naissance', 'N/A')}

🏢 <b>CONTACT</b>
📧 Email: {client_info['email']}
🏠 Adresse: {client_info['adresse']}
🏘️ Ville: {client_info['ville']} ({client_info['code_postal']})

🏦 <b>BANQUE</b>
▪️ Banque: {client_info.get('banque', 'N/A')}
▪️ SWIFT: <code>{client_info.get('swift', 'N/A')}</code>
▪️ IBAN: <code>{client_info.get('iban', 'N/A')}</code>

📊 <b>STATUT</b>
▪️ {client_info['statut']} | Appels: {client_info['nb_appels']}
        """

telegram_service = None
config_valid = False

def initialize_telegram_service():
    global telegram_service, config_valid
    is_valid, missing_vars = check_required_config()
    config_valid = is_valid
    
    if is_valid:
        telegram_service = TelegramService(Config.TELEGRAM_TOKEN, Config.CHAT_ID)
        logger.info("✅ Service Telegram initialisé")
    else:
        logger.error(f"❌ Variables manquantes: {missing_vars}")
        telegram_service = None

initialize_telegram_service()

# ===================================================================
# GESTION CLIENTS - FORMAT PIPE SÉPARATEUR
# ===================================================================

clients_database = {}
upload_stats = {"total_clients": 0, "last_upload": None, "filename": None}

def normalize_phone(phone):
    if not phone:
        return None
    cleaned = re.sub(r'[^\d+]', '', str(phone))
    
    patterns = [
        (r'^0033(\d{9})$', lambda m: '0' + m.group(1)),
        (r'^\+33(\d{9})$', lambda m: '0' + m.group(1)),
        (r'^33(\d{9})$', lambda m: '0' + m.group(1)),
        (r'^0(\d{9})$', lambda m: '0' + m.group(1)),
        (r'^(\d{9})$', lambda m: '0' + m.group(1)),
    ]
    
    for pattern, transform in patterns:
        match = re.match(pattern, cleaned)
        if match:
            result = transform(match)
            if result and len(result) == 10 and result.startswith('0'):
                return result
    return None

def get_client_info(phone_number):
    if not phone_number:
        return create_unknown_client(phone_number)
    
    normalized = normalize_phone(phone_number)
    
    if normalized and normalized in clients_database:
        client = clients_database[normalized].copy()
        clients_database[normalized]["nb_appels"] += 1
        clients_database[normalized]["dernier_appel"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        return client
    
    return create_unknown_client(phone_number)

def load_clients_from_pipe_file(file_content):
    """Charge les clients depuis le format pipe (|) du fichier fc.txt"""
    global clients_database, upload_stats
    clients_database = {}
    
    try:
        lines = file_content.strip().split('\n')
        loaded_count = 0
        
        for line_num, line in enumerate(lines, 1):
            try:
                if not line.strip():
                    continue
                
                # Format: telephone|nom|date_naissance|email|adresse|ville|iban|swift
                parts = line.split('|')
                
                if len(parts) < 7:
                    logger.warning(f"Ligne {line_num} ignorée (format incomplet)")
                    continue
                
                # Extraction des données
                telephone_raw = parts[0].strip()
                nom_complet = parts[1].strip() if len(parts) > 1 else ''
                date_naissance = parts[2].strip() if len(parts) > 2 else ''
                email = parts[3].strip() if len(parts) > 3 else ''
                adresse = parts[4].strip() if len(parts) > 4 else ''
                ville_code = parts[5].strip() if len(parts) > 5 else ''
                iban = parts[6].strip() if len(parts) > 6 else ''
                swift = parts[7].strip() if len(parts) > 7 else ''
                
                # Normalisation téléphone
                telephone = normalize_phone(telephone_raw)
                if not telephone:
                    logger.warning(f"Ligne {line_num}: Numéro invalide {telephone_raw}")
                    continue
                
                # Extraction nom/prénom
                nom_parts = nom_complet.split(' ', 1)
                if len(nom_parts) == 2:
                    nom = nom_parts[0]
                    prenom = nom_parts[1]
                else:
                    nom = nom_complet
                    prenom = ''
                
                # Extraction ville et code postal
                ville_match = re.match(r'(.+?)\s*\((\d{5})\)', ville_code)
                if ville_match:
                    ville = ville_match.group(1).strip()
                    code_postal = ville_match.group(2)
                else:
                    ville = ville_code
                    code_postal = ''
                
                # Détection banque
                banque = iban_detector.detect_bank(iban) if iban else 'N/A'
                
                clients_database[telephone] = {
                    "nom": nom,
                    "prenom": prenom,
                    "email": email,
                    "entreprise": "N/A",
                    "telephone": telephone,
                    "adresse": adresse,
                    "ville": ville,
                    "code_postal": code_postal,
                    "banque": banque,
                    "swift": swift,
                    "iban": iban,
                    "sexe": "N/A",
                    "date_naissance": date_naissance,
                    "lieu_naissance": "N/A",
                    "profession": "N/A",
                    "statut": "Prospect",
                    "date_upload": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    "nb_appels": 0,
                    "dernier_appel": None,
                    "notes": ""
                }
                loaded_count += 1
                
            except Exception as e:
                logger.error(f"Erreur ligne {line_num}: {str(e)}")
                continue
        
        upload_stats["total_clients"] = len(clients_database)
        upload_stats["last_upload"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        
        logger.info(f"✅ {loaded_count} clients chargés sur {len(lines)} lignes")
        return loaded_count
        
    except Exception as e:
        logger.error(f"Erreur chargement: {str(e)}")
        raise ValueError(f"Erreur: {str(e)}")

def create_unknown_client(phone_number):
    return {
        "nom": "INCONNU", "prenom": "CLIENT", "email": "N/A",
        "entreprise": "N/A", "adresse": "N/A", "ville": "N/A",
        "code_postal": "N/A", "telephone": phone_number,
        "banque": "N/A", "swift": "N/A", "iban": "N/A",
        "sexe": "N/A", "date_naissance": "N/A", "lieu_naissance": "N/A",
        "profession": "N/A", "statut": "Non référencé",
        "date_upload": "N/A", "nb_appels": 0, "dernier_appel": None, "notes": ""
    }

def process_telegram_command(message_text, chat_id):
    if not telegram_service:
        return {"error": "Service non configuré"}
    
    try:
        if message_text.startswith('/numero '):
            phone = message_text.replace('/numero ', '').strip()
            client = get_client_info(phone)
            msg = telegram_service.format_client_message(client, "recherche")
            telegram_service.send_message(msg)
            return {"status": "ok", "command": "numero"}
            
        elif message_text.startswith('/iban '):
            iban = message_text.replace('/iban ', '').strip()
            bank = iban_detector.detect_bank(iban)
            msg = f"🏦 <b>ANALYSE IBAN</b>\n\n💳 {iban}\n🏛️ {bank}"
            telegram_service.send_message(msg)
            return {"status": "ok", "command": "iban"}
            
        elif message_text.startswith('/stats'):
            msg = f"""📊 <b>STATS</b>
👥 Clients: {upload_stats['total_clients']}
📁 Upload: {upload_stats['last_upload'] or 'Aucun'}
📞 Ligne: {Config.OVH_LINE_NUMBER}
🌐 Plateforme: Render.com"""
            telegram_service.send_message(msg)
            return {"status": "ok", "command": "stats"}
        
        return {"status": "unknown"}
        
    except Exception as e:
        return {"error": str(e)}

# ===================================================================
# ROUTES
# ===================================================================

@app.route('/webhook/ovh', methods=['POST', 'GET'])
def ovh_webhook():
    try:
        if request.method == 'GET':
            caller = request.args.get('caller', 'Inconnu')
            event = request.args.get('type', 'unknown')
        else:
            data = request.get_json() or {}
            caller = data.get('callerIdNumber', 'Inconnu')
            event = 'incoming'
        
        client = get_client_info(caller)
        
        if telegram_service:
            msg = telegram_service.format_client_message(client)
            telegram_service.send_message(msg)
        
        return jsonify({
            "status": "success",
            "caller": caller,
            "client": f"{client['prenom']} {client['nom']}",
            "platform": "Render.com"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/webhook/telegram', methods=['POST'])
def telegram_webhook():
    if not config_valid:
        return jsonify({"error": "Config manquante"}), 400
    
    try:
        data = request.get_json()
        if 'message' in data and 'text' in data['message']:
            text = data['message']['text']
            chat_id = data['message']['chat']['id']
            result = process_telegram_command(text, chat_id)
            return jsonify({"status": "success", "result": result})
        return jsonify({"status": "no_text"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ping')
def ping():
    """Endpoint pour le keep-alive"""
    return jsonify({
        "status": "alive",
        "timestamp": datetime.now().isoformat(),
        "platform": "Render.com"
    })

@app.route('/')
def home():
    auto_detected = len([c for c in clients_database.values() 
                        if c['banque'] not in ['N/A', ''] and c['iban']])
    
    return render_template_string("""
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🌐 Webhook Render</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        .header {
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            color: white;
            padding: 40px;
            text-align: center;
            border-radius: 15px 15px 0 0;
        }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; }
        .badge {
            display: inline-block;
            background: rgba(255,255,255,0.2);
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.9em;
            margin-top: 10px;
        }
        .content { padding: 40px; }
        .alert {
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
            border-left: 5px solid;
        }
        .alert-success { background: #d4edda; border-color: #28a745; color: #155724; }
        .alert-error { background: #f8d7da; border-color: #dc3545; color: #721c24; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }
        .stat-card {
            background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
            color: white;
            padding: 25px;
            border-radius: 12px;
            text-align: center;
            box-shadow: 0 5px 15px rgba(99,102,241,0.3);
        }
        .stat-card h3 { font-size: 1em; margin-bottom: 15px; opacity: 0.9; }
        .stat-card .value { font-size: 2.5em; font-weight: bold; }
        .btn {
            display: inline-block;
            padding: 14px 24px;
            border-radius: 8px;
            text-decoration: none;
            margin: 5px;
            font-weight: 600;
            transition: all 0.3s;
            color: white;
        }
        .btn-primary { background: #6366f1; }
        .btn-success { background: #28a745; }
        .btn-danger { background: #dc3545; }
        .btn:hover { transform: translateY(-2px); opacity: 0.9; }
        .config-box {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin: 20px 0;
            font-family: monospace;
        }
        .config-box code {
            background: white;
            padding: 10px;
            display: block;
            border-radius: 5px;
            margin: 10px 0;
            word-break: break-all;
        }
        .upload-section {
            background: #f8f9fa;
            padding: 30px;
            border-radius: 12px;
            margin: 20px 0;
        }
        input[type="file"] { margin: 15px 0; }
        .format-info {
            background: #e9ecef;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌐 Webhook Render.com</h1>
            <div class="badge">Chat ID: {{ chat_id }}</div>
            <div class="badge">✅ Keep-Alive Actif</div>
        </div>
        
        <div class="content">
            {% if config_valid %}
            <div class="alert alert-success">
                <strong>✅ Configuration active</strong><br>
                Plateforme: Render.com<br>
                Chat ID: {{ chat_id }}<br>
                Ligne OVH: {{ ovh_line }}<br>
                🔄 Système anti-sleep: Actif
            </div>
            {% else %}
            <div class="alert alert-error">
                <strong>❌ Configuration requise</strong><br>
                Ajoutez TELEGRAM_TOKEN dans Render → Environment
            </div>
            {% endif %}
            
            <div class="stats-grid">
                <div class="stat-card">
                    <h3>👥 Clients chargés</h3>
                    <div class="value">{{ total_clients }}</div>
                </div>
                <div class="stat-card">
                    <h3>🏦 Banques détectées</h3>
                    <div class="value">{{ auto_detected }}</div>
                </div>
                <div class="stat-card">
                    <h3>📁 Dernier upload</h3>
                    <div class="value" style="font-size:1.2em;">{{ last_upload or 'Aucun' }}</div>
                </div>
            </div>
            
            <div class="upload-section">
                <h2>📂 Upload fichier clients</h2>
                <form action="/upload" method="post" enctype="multipart/form-data">
                    <div class="format-info">
                        <strong>📋 Format:</strong> Fichier texte (.txt) avec pipe (|)<br><br>
                        <strong>Structure:</strong><br>
                        <code>tel|nom prenom|date|email|adresse|ville (code)|iban|swift</code><br><br>
                        <strong>Exemple:</strong><br>
                        <code>0669290606|Islam Soussi|01/09/1976|email@gmail.com|2 Avenue|Paris (75001)|FR76...|AGRIFRPP839</code>
                    </div>
                    <input type="file" name="file" accept=".txt" required>
                    <br>
                    <button type="submit" class="btn btn-success">📁 Charger fichier</button>
                </form>
            </div>
            
            <h3>🔧 Actions</h3>
            <div style="margin: 20px 0;">
                <a href="/clients" class="btn btn-primary">👥 Clients</a>
                <a href="/test-telegram" class="btn btn-success">📧 Test</a>
                <a href="/health" class="btn btn-primary">🔍 Status</a>
                <a href="/fix-webhook" class="btn btn-success">🔧 Webhook</a>
                <a href="/ping" class="btn btn-primary">🔄 Ping</a>
            </div>
            
            <div class="config-box">
                <h3>🔗 URL Webhook OVH</h3>
                <code>{{ webhook_url }}/webhook/ovh?caller=*CALLING*&callee=*CALLED*&type=*EVENT*</code>
            </div>
            
            <div class="config-box">
                <h3>📱 Commandes Telegram</h3>
                <code>/numero 0669290606</code> - Fiche client<br>
                <code>/iban FR76...</code> - Détection banque<br>
                <code>/stats</code> - Statistiques
            </div>
        </div>
    </div>
</body>
</html>
    """,
    config_valid=config_valid,
    total_clients=upload_stats["total_clients"],
    auto_detected=auto_detected,
    last_upload=upload_stats.get("last_upload"),
    chat_id=Config.CHAT_ID,
    ovh_line=Config.OVH_LINE_NUMBER,
    webhook_url=request.url_root.rstrip('/')
    )

@app.route('/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "Aucun fichier"}), 400
        
        file = request.files['file']
        if not file.filename:
            return jsonify({"error": "Aucun fichier"}), 400
        
        filename = secure_filename(file.filename)
        if not filename.endswith('.txt'):
            return jsonify({"error": "Fichier .txt uniquement"}), 400
        
        content = file.read().decode('utf-8-sig')
        nb = load_clients_from_pipe_file(content)
        
        return jsonify({"status": "success", "clients": nb})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/clients')
def clients():
    return jsonify({
        "total": len(clients_database),
        "clients": list(clients_database.values())[:20]
    })

@app.route('/test-telegram')
def test_telegram():
    if not telegram_service:
        return jsonify({"error": "Non configuré"}), 400
    
    msg = f"🌐 Test Render.com - {datetime.now().strftime('%H:%M:%S')}"
    result = telegram_service.send_message(msg)
    return jsonify({"status": "success" if result else "error"})

@app.route('/fix-webhook')
def fix_webhook():
    if not Config.TELEGRAM_TOKEN:
        return jsonify({"error": "Token manquant"}), 400
    
    try:
        webhook_url = request.url_root + "webhook/telegram"
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/setWebhook"
        data = {"url": webhook_url, "drop_pending_updates": True}
        response = requests.post(url, data=data, timeout=10)
        
        if response.status_code == 200:
            return jsonify({
                "status": "success",
                "webhook_url": webhook_url,
                "message": "Webhook configuré sur Render"
            })
        return jsonify({"error": response.text}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "platform": "Render.com",
        "chat_id": Config.CHAT_ID,
        "config_valid": config_valid,
        "clients": upload_stats["total_clients"],
        "keep_alive": "active",
        "timestamp": datetime.now().isoformat()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))  # Port par défaut Render
    
    logger.info("🌐 Démarrage Render.com")
    logger.info(f"📱 Chat ID: {Config.CHAT_ID}")
    logger.info(f"🔄 Keep-alive: Actif")
    
    is_valid, missing = check_required_config()
    if is_valid:
        logger.info("✅ Configuration OK")
    else:
        logger.warning(f"⚠️ Manquant: {missing}")
    
    app.run(host='0.0.0.0', port=port, debug=False)
