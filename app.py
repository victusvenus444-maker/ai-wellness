import os
import secrets
import hashlib
import hmac
import logging
import json
from datetime import datetime, date, timedelta
from flask import Flask, request, render_template, session, redirect, url_for, flash, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from openai import OpenAI
from dotenv import load_dotenv
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

load_dotenv()

app = Flask(__name__)

# ---------- Configuration ----------
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///wellness.db')
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DEBUG'] = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

# JWT
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', secrets.token_hex(32))
jwt = JWTManager(app)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ---------- Extensions ----------
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)  # kept for compatibility, but not used for auth
login_manager = LoginManager(app)
login_manager.login_view = 'login_web'
CORS(app)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["60 per minute", "1000 per hour"],
    storage_uri="memory://",
)

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ---------- RevenueCat ----------
REVENUECAT_WEBHOOK_SECRET = os.getenv('REVENUECAT_WEBHOOK_SECRET')

# ---------- Models ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    google_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=True)
    picture = db.Column(db.String(200), nullable=True)
    plan = db.Column(db.String(20), default='free')
    revenuecat_user_id = db.Column(db.String(100), nullable=True)
    subscription_product_id = db.Column(db.String(100), nullable=True)
    referral_code = db.Column(db.String(20), unique=True, nullable=False)
    bonus_messages = db.Column(db.Integer, default=0)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    monthly_messages_used = db.Column(db.Integer, default=0)
    month_start = db.Column(db.Date, default=date.today)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    companions = db.relationship('Companion', backref='user', lazy=True, cascade='all, delete-orphan')
    referrer = db.relationship('User', remote_side=[id], backref='referees')

    @property
    def is_pro(self):
        return self.plan != 'free'

    @property
    def monthly_limit(self):
        if self.plan == 'free':
            return None
        elif self.plan == 'elite':
            return 1000
        elif self.plan == 'pro':
            return 10000
        return None

class Companion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    avatar = db.Column(db.Text, nullable=True)
    personality = db.Column(db.String(30), default='empathetic')
    tone = db.Column(db.String(20), default='warm')
    description = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sessions = db.relationship('ChatSession', backref='companion', lazy=True, cascade='all, delete-orphan')

class ChatSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    companion_id = db.Column(db.Integer, db.ForeignKey('companion.id'), nullable=False)
    session_id = db.Column(db.String(100), unique=True, nullable=False)
    title = db.Column(db.String(100), default='New Conversation')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    messages = db.relationship('ChatMessage', backref='session', lazy=True, cascade='all, delete-orphan')

class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('chat_session.id'), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ProactiveMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    companion_id = db.Column(db.Integer, db.ForeignKey('companion.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    read = db.Column(db.Boolean, default=False)
    event_id = db.Column(db.Integer, nullable=True)

class ExtractedEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    companion_id = db.Column(db.Integer, db.ForeignKey('companion.id'), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    event_datetime = db.Column(db.DateTime, nullable=False)
    is_future = db.Column(db.Boolean, default=True)
    hash = db.Column(db.String(64), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------- Helpers ----------
def generate_referral_code():
    while True:
        code = secrets.token_urlsafe(4).upper()
        if not User.query.filter_by(referral_code=code).first():
            return code

# ---------- Google OAuth ----------
def verify_google_token(token):
    try:
        CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
        if not CLIENT_ID:
            raise ValueError("GOOGLE_CLIENT_ID not set")
        info = id_token.verify_oauth2_token(token, google_requests.Request(), CLIENT_ID)
        return info
    except Exception as e:
        app.logger.error(f"Google token verification failed: {e}")
        return None

# ---------- Auth Routes ----------
@app.route('/auth/google', methods=['POST'])
def google_login():
    data = request.get_json()
    token = data.get('id_token')
    if not token:
        return jsonify({'error': 'Missing id_token'}), 400

    user_info = verify_google_token(token)
    if not user_info:
        return jsonify({'error': 'Invalid token'}), 401

    google_id = user_info['sub']
    email = user_info.get('email')
    name = user_info.get('name')
    picture = user_info.get('picture')

    if not email:
        return jsonify({'error': 'Email not provided by Google'}), 400

    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User(
            email=email,
            google_id=google_id,
            name=name,
            picture=picture,
            referral_code=generate_referral_code()
        )
        db.session.add(user)
        db.session.commit()

        default_companions = [
            {'name': 'Alex', 'avatar': None, 'personality': 'empathetic', 'tone': 'warm', 'description': 'A calm listener who helps you reflect.'},
            {'name': 'Jordan', 'avatar': None, 'personality': 'logical', 'tone': 'formal', 'description': 'Clear‑headed and solution‑focused.'},
            {'name': 'Taylor', 'avatar': None, 'personality': 'playful', 'tone': 'casual', 'description': 'Cheerful, witty, and uplifting.'},
            {'name': 'Morgan', 'avatar': None, 'personality': 'wise', 'tone': 'warm', 'description': 'Thoughtful, patient, and insightful.'},
            {'name': 'Riley', 'avatar': None, 'personality': 'creative', 'tone': 'casual', 'description': 'Imaginative and inspiring.'},
            {'name': 'Casey', 'avatar': None, 'personality': 'analytical', 'tone': 'warm', 'description': 'Detail-oriented and objective.'},
            {'name': 'Quinn', 'avatar': None, 'personality': 'supportive', 'tone': 'warm', 'description': 'Encouraging and confidence-building.'},
            {'name': 'Avery', 'avatar': None, 'personality': 'motivational', 'tone': 'casual', 'description': 'Energetic goal‑pusher.'},
            {'name': 'Drew', 'avatar': None, 'personality': 'intuitive', 'tone': 'warm', 'description': 'Perceptive and trusting of instinct.'},
            {'name': 'Sage', 'avatar': None, 'personality': 'gentle', 'tone': 'warm', 'description': 'Soft and calming presence.'},
            {'name': 'Blake', 'avatar': None, 'personality': 'adventurous', 'tone': 'casual', 'description': 'Bold and encouraging exploration.'},
            {'name': 'Parker', 'avatar': None, 'personality': 'witty', 'tone': 'casual', 'description': 'Smart humor and cleverness.'}
        ]
        for comp_data in default_companions:
            comp = Companion(
                user_id=user.id,
                name=comp_data['name'],
                avatar=comp_data['avatar'],
                personality=comp_data['personality'],
                tone=comp_data['tone'],
                description=comp_data['description']
            )
            db.session.add(comp)
        db.session.commit()

    if user.name != name or user.picture != picture:
        user.name = name
        user.picture = picture
        db.session.commit()

    access_token = create_access_token(identity=user.id)
    return jsonify({
        'access_token': access_token,
        'user': {
            'id': user.id,
            'email': user.email,
            'name': user.name,
            'picture': user.picture,
            'plan': user.plan
        }
    }), 200

# ---------- Protected Routes (JWT) ----------
@app.route('/api/me', methods=['GET'])
@jwt_required()
def get_user():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    return jsonify({
        'id': user.id,
        'email': user.email,
        'name': user.name,
        'picture': user.picture,
        'plan': user.plan
    })

# ---------- API Routes ----------
@app.route('/api/referral', methods=['GET'])
@jwt_required()
def get_referral_info():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({
        'referral_code': user.referral_code,
        'bonus_messages': user.bonus_messages,
        'referral_link': request.host_url + '?ref=' + user.referral_code
    })

@app.route('/api/companions', methods=['GET'])
@jwt_required()
def get_companions():
    user_id = get_jwt_identity()
    companions = Companion.query.filter_by(user_id=user_id).all()
    return jsonify([{
        'id': c.id,
        'name': c.name,
        'avatar': c.avatar,
        'personality': c.personality,
        'tone': c.tone,
        'description': c.description
    } for c in companions])

@app.route('/api/companion', methods=['POST'])
@jwt_required()
@limiter.limit("10 per minute")
def create_companion():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    data = request.get_json()
    if user.plan == 'free':
        count = Companion.query.filter_by(user_id=user_id).count()
        if count >= 2:
            return jsonify({'error': 'Free plan limited to 2 companions. Upgrade to Elite or Pro.'}), 403
    comp = Companion(
        user_id=user_id,
        name=data.get('name', 'Companion'),
        avatar=data.get('avatar'),
        personality=data.get('personality', 'empathetic'),
        tone=data.get('tone', 'warm'),
        description=data.get('description', '')
    )
    db.session.add(comp)
    db.session.commit()
    return jsonify({'id': comp.id, 'name': comp.name, 'avatar': comp.avatar})

@app.route('/api/companion/<int:comp_id>', methods=['PUT'])
@jwt_required()
def update_companion(comp_id):
    user_id = get_jwt_identity()
    comp = Companion.query.filter_by(id=comp_id, user_id=user_id).first()
    if not comp:
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json()
    comp.name = data.get('name', comp.name)
    comp.avatar = data.get('avatar')
    comp.personality = data.get('personality', comp.personality)
    comp.tone = data.get('tone', comp.tone)
    comp.description = data.get('description', comp.description)
    db.session.commit()
    return jsonify({'id': comp.id, 'name': comp.name, 'avatar': comp.avatar})

@app.route('/api/companion/<int:comp_id>', methods=['DELETE'])
@jwt_required()
def delete_companion(comp_id):
    user_id = get_jwt_identity()
    comp = Companion.query.filter_by(id=comp_id, user_id=user_id).first()
    if not comp:
        return jsonify({'error': 'Not found'}), 404
    db.session.delete(comp)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/session', methods=['POST'])
@jwt_required()
def new_session():
    user_id = get_jwt_identity()
    data = request.get_json()
    comp_id = data.get('companion_id')
    if not comp_id:
        return jsonify({'error': 'companion_id required'}), 400
    comp = Companion.query.filter_by(id=comp_id, user_id=user_id).first()
    if not comp:
        return jsonify({'error': 'Companion not found'}), 404
    share_id = secrets.token_urlsafe(12)
    session_obj = ChatSession(
        user_id=user_id,
        companion_id=comp_id,
        session_id=share_id,
        title=data.get('title', 'New Conversation')
    )
    db.session.add(session_obj)
    db.session.commit()
    return jsonify({'session_id': share_id, 'url': f"/s/{share_id}"})

@app.route('/api/chat', methods=['POST'])
@jwt_required()
@limiter.limit("20 per minute")
def chat():
    user_id = get_jwt_identity()
    data = request.get_json()
    user_message = data.get('message', '').strip()
    session_id = data.get('session_id')
    if not user_message or not session_id:
        return jsonify({'error': 'Message and session_id required'}), 400

    session_obj = ChatSession.query.filter_by(session_id=session_id).first()
    if not session_obj:
        return jsonify({'error': 'Session not found'}), 404

    if session_obj.user_id != user_id:
        return jsonify({'error': 'Unauthorized'}), 403

    user = User.query.get(user_id)
    if user.bonus_messages > 0:
        user.bonus_messages -= 1
        db.session.commit()
    else:
        check_and_reset_monthly_usage(user)
        plan = user.plan
        if plan == 'free':
            used_today = get_daily_message_count(user)
            if used_today >= 10:
                return jsonify({'error': 'Daily free limit reached. Upgrade to Elite or Pro.'}), 403
        else:
            limit = get_plan_limit(plan)
            if user.monthly_messages_used >= limit:
                return jsonify({'error': f'Monthly message limit for {plan.capitalize()} reached ({limit} messages).'}), 403

    user_msg = ChatMessage(session_id=session_obj.id, role='user', content=user_message)
    db.session.add(user_msg)
    db.session.commit()
    if user.bonus_messages == 0 and user.plan != 'free':
        user.monthly_messages_used += 1
        db.session.commit()

    history = ChatMessage.query.filter_by(session_id=session_obj.id).order_by(ChatMessage.created_at).limit(10).all()
    messages = []
    comp = Companion.query.get(session_obj.companion_id)
    if comp:
        system_prompt = build_system_prompt(comp)
        messages.append({'role': 'system', 'content': system_prompt})
    else:
        messages.append({'role': 'system', 'content': "You are a compassionate wellness companion."})

    for h in history:
        messages.append({'role': h.role, 'content': h.content})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
            max_tokens=150
        )
        reply = response.choices[0].message.content
    except Exception as e:
        app.logger.error(f"OpenAI error: {e}")
        return jsonify({'error': str(e)}), 500

    assistant_msg = ChatMessage(session_id=session_obj.id, role='assistant', content=reply)
    db.session.add(assistant_msg)
    db.session.commit()

    return jsonify({'reply': reply, 'session_id': session_obj.session_id})

# ---------- usage, proactive messages, etc. ----------
def get_plan_limit(plan):
    if plan == 'free':
        return 10
    elif plan == 'elite':
        return 1000
    elif plan == 'pro':
        return 10000
    return 10

def check_and_reset_monthly_usage(user):
    today = date.today()
    if user.month_start.month != today.month or user.month_start.year != today.year:
        user.monthly_messages_used = 0
        user.month_start = today
        db.session.commit()
        return True
    return False

def get_daily_message_count(user):
    today = datetime.utcnow().date()
    return ChatMessage.query.join(ChatSession).filter(
        ChatSession.user_id == user.id,
        db.func.date(ChatMessage.created_at) == today,
        ChatMessage.role == 'user'
    ).count()

def build_system_prompt(companion):
    personality_prompts = {
        'empathetic': "You are deeply empathetic and compassionate. You listen carefully and validate feelings.",
        'logical': "You are analytical and logical. You help users think through problems with clear reasoning.",
        'playful': "You are warm, funny, and playful. You use humor to lighten the mood.",
        'wise': "You are wise and philosophical. You offer deep insights and ask reflective questions.",
        'creative': "You are imaginative and creative. You inspire users with new ideas and perspectives.",
        'analytical': "You are detail-oriented and data-driven. You help users analyze situations objectively.",
        'supportive': "You are encouraging and supportive. You build users' confidence and self-belief.",
        'motivational': "You are energetic and motivational. You push users to achieve their goals.",
        'intuitive': "You are intuitive and perceptive. You help users trust their instincts.",
        'adventurous': "You are bold and adventurous. You encourage users to explore new possibilities.",
        'gentle': "You are gentle and kind. You create a safe, calming environment for users.",
        'witty': "You are clever and witty. You bring lightheartedness with smart humor."
    }
    tone_prompts = {
        'warm': "You speak warmly and gently.",
        'formal': "You speak formally and respectfully.",
        'casual': "You speak casually and informally, like a close friend."
    }
    base = f"You are {companion.name}, a wellness companion. {personality_prompts.get(companion.personality, personality_prompts['empathetic'])} {tone_prompts.get(companion.tone, tone_prompts['warm'])} You never diagnose or prescribe. Always remind users you're not a replacement for professional help. Keep responses warm and concise (2-3 sentences)."
    if companion.description:
        base += f" Your backstory: {companion.description}"
    return base

@app.route('/api/usage', methods=['GET'])
@jwt_required()
def get_usage():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    plan = user.plan
    daily_used = get_daily_message_count(user)
    check_and_reset_monthly_usage(user)
    monthly_limit = get_plan_limit(plan) if plan != 'free' else None
    monthly_used = user.monthly_messages_used if plan != 'free' else None
    companion_count = Companion.query.filter_by(user_id=user_id).count()
    max_companions = 2 if plan == 'free' else 999999

    return jsonify({
        'plan': plan,
        'daily_used': daily_used,
        'daily_limit': 10 if plan == 'free' else None,
        'monthly_used': monthly_used,
        'monthly_limit': monthly_limit,
        'bonus_messages': user.bonus_messages,
        'companion_count': companion_count,
        'max_companions': max_companions,
        'is_pro': user.is_pro
    })

@app.route('/api/proactive-messages', methods=['GET'])
@jwt_required()
def get_proactive_messages():
    user_id = get_jwt_identity()
    messages = ProactiveMessage.query.filter_by(
        user_id=user_id,
        read=False
    ).order_by(ProactiveMessage.sent_at).all()
    return jsonify([{
        'id': m.id,
        'companion_id': m.companion_id,
        'content': m.content,
        'sent_at': m.sent_at.isoformat(),
        'companion_name': Companion.query.get(m.companion_id).name
    } for m in messages])

@app.route('/api/proactive-messages/<int:msg_id>/read', methods=['POST'])
@jwt_required()
def mark_proactive_read(msg_id):
    user_id = get_jwt_identity()
    msg = ProactiveMessage.query.filter_by(id=msg_id, user_id=user_id).first()
    if not msg:
        return jsonify({'error': 'Message not found'}), 404
    msg.read = True
    db.session.commit()
    return jsonify({'success': True})

# ---------- ACCOUNT MANAGEMENT ----------
@app.route('/api/update-plan', methods=['POST'])
@jwt_required()
def update_plan():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json()
    new_plan = data.get('plan')
    if new_plan not in ('free', 'elite', 'pro'):
        return jsonify({'error': 'Invalid plan'}), 400
    user.plan = new_plan
    user.monthly_messages_used = 0
    user.month_start = date.today()
    db.session.commit()
    return jsonify({'success': True, 'plan': user.plan})

@app.route('/api/delete-account', methods=['POST'])
@jwt_required()
def api_delete_account():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    db.session.delete(user)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/cancel-subscription', methods=['POST'])
@jwt_required()
def api_cancel_subscription():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    user.plan = 'free'
    user.subscription_product_id = None
    user.monthly_messages_used = 0
    user.month_start = date.today()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/sessions', methods=['GET'])
@jwt_required()
def get_sessions():
    user_id = get_jwt_identity()
    companion_id = request.args.get('companion_id', type=int)
    if not companion_id:
        return jsonify({'error': 'companion_id required'}), 400
    sessions = ChatSession.query.filter_by(user_id=user_id, companion_id=companion_id).order_by(ChatSession.created_at.desc()).all()
    return jsonify([{
        'id': s.id,
        'session_id': s.session_id,
        'title': s.title,
        'created_at': s.created_at.isoformat(),
        'message_count': len(s.messages)
    } for s in sessions])

# ---------- REVENUECAT WEBHOOK ----------
@app.route('/webhook/revenuecat', methods=['POST'])
def revenuecat_webhook():
    # Verify HMAC signature (RevenueCat sends X-RevenueCat-Webhook-Signature)
    signature_header = request.headers.get('X-RevenueCat-Webhook-Signature')
    if not signature_header or not REVENUECAT_WEBHOOK_SECRET:
        app.logger.warning("Missing signature or secret")
        return 'Missing signature', 400

    # Parse the header: format "t=<timestamp>,v1=<hmac>"
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        timestamp = parts.get("t")
        expected_sig = parts.get("v1")
        if not timestamp or not expected_sig:
            raise ValueError("Invalid signature header format")
    except Exception as e:
        app.logger.warning(f"Could not parse signature header: {e}")
        return 'Invalid signature header', 400

    # Get the raw request body (bytes)
    payload = request.get_data()

    # Compute HMAC
    signed_payload = f"{timestamp}.".encode() + payload
    computed = hmac.new(
        REVENUECAT_WEBHOOK_SECRET.encode(),
        signed_payload,
        hashlib.sha256
    ).hexdigest()

    # Constant-time compare
    if not hmac.compare_digest(computed, expected_sig):
        app.logger.warning("Invalid webhook signature")
        return 'Invalid signature', 401

    # Optionally check timestamp freshness (e.g., within 5 minutes)
    try:
        if abs(datetime.utcnow().timestamp() - int(timestamp)) > 300:
            app.logger.warning("Webhook timestamp too old")
            return 'Timestamp too old', 400
    except:
        pass

    # Process the webhook event
    data = request.json
    event = data.get('event')

    if event in ('INITIAL_PURCHASE', 'RENEWAL', 'NON_RENEWING_PURCHASE'):
        app_user_id = data.get('app_user_id')
        if not app_user_id:
            return jsonify({'error': 'Missing app_user_id'}), 400

        try:
            user_id = int(app_user_id)
        except ValueError:
            return jsonify({'error': 'Invalid app_user_id'}), 400

        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        # Extract product_id
        product_id = None
        if 'purchases' in data and data['purchases']:
            first_purchase = data['purchases'][0]
            product_id = first_purchase.get('product_id')
        elif 'product' in data:
            product_id = data.get('product')

        if product_id:
            if product_id.startswith('aura_elite'):
                user.plan = 'elite'
            elif product_id.startswith('aura_pro'):
                user.plan = 'pro'
            else:
                user.plan = 'pro'
            user.subscription_product_id = product_id
        else:
            # Fallback: assume pro if entitlement is active
            entitlement = data.get('entitlements', {}).get('pro', {})
            if entitlement.get('is_active'):
                user.plan = 'pro'
            else:
                # If no entitlement, keep current plan? But we should be safe.
                pass

        user.monthly_messages_used = 0
        user.month_start = date.today()
        db.session.commit()
        app.logger.info(f"Webhook: user {user.id} plan updated to {user.plan} (product: {product_id})")
        return jsonify({'status': 'ok'}), 200

    return jsonify({'status': 'ignored'}), 200

# ---------- Web Views (for web version) ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user)

# ---------- Create tables ----------
with app.app_context():
    db.create_all()

# Shutdown scheduler
import atexit
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
