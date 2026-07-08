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
import boto3
from botocore.exceptions import ClientError

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

# ---------- Amazon SES Configuration ----------
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
SES_SENDER_EMAIL = os.getenv('SES_SENDER_EMAIL', 'noreply@aura.com')

# Initialize SES client
ses_client = boto3.client(
    'ses',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ---------- Extensions ----------
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
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
    password_hash = db.Column(db.String(128), nullable=False)
    plan = db.Column(db.String(20), default='free')  # free, elite, pro
    revenuecat_user_id = db.Column(db.String(100), nullable=True)
    subscription_product_id = db.Column(db.String(100), nullable=True)
    referral_code = db.Column(db.String(20), unique=True, nullable=False)
    bonus_messages = db.Column(db.Integer, default=0)
    referred_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    monthly_messages_used = db.Column(db.Integer, default=0)
    month_start = db.Column(db.Date, default=date.today)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # ---------- NEW: Email Verification & Password Reset ----------
    email_verified = db.Column(db.Boolean, default=False)
    verification_token = db.Column(db.String(100), nullable=True)
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
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
    # INCREASED to 30 to accommodate longer personality names
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

# Proactive Messaging Models
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

def generate_verification_token():
    return secrets.token_urlsafe(32)

def send_email(to, subject, body):
    """Send email using Amazon SES."""
    try:
        response = ses_client.send_email(
            Source=SES_SENDER_EMAIL,
            Destination={
                'ToAddresses': [to]
            },
            Message={
                'Subject': {'Data': subject},
                'Body': {
                    'Text': {'Data': body}  # Plain text; you can add HTML if needed
                }
            }
        )
        app.logger.info(f"Email sent to {to}, MessageId: {response['MessageId']}")
        return True
    except ClientError as e:
        app.logger.error(f"SES email error: {e.response['Error']['Message']}")
        return False
    except Exception as e:
        app.logger.error(f"Unexpected email error: {e}")
        return False

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
    # ---------- EXPANDED PERSONALITIES (12 types) ----------
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

# ---------- Proactive Scheduler ----------
def generate_event_hash(user_id, companion_id, event_datetime, description):
    raw = f"{user_id}-{companion_id}-{event_datetime.isoformat()}-{description}"
    return hashlib.sha256(raw.encode()).hexdigest()

def extract_events_from_history(messages, companion_id, user_id):
    if not messages:
        return []
    history = "\n".join([f"{m.role}: {m.content}" for m in messages])
    prompt = f"""
You are an event extractor. Read the following conversation and extract any mention of a future or past event (job interview, appointment, meeting, test, travel, deadline, etc.).
Return a JSON list of objects with fields: "type" (string), "description" (string), "datetime" (ISO format like "2026-07-10 14:30" or "2026-07-15"), "is_future" (boolean).
If no event is found, return an empty list.

Conversation:
{history}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300
        )
        content = response.choices[0].message.content.strip()
        start = content.find('[')
        end = content.rfind(']') + 1
        if start != -1 and end != -1:
            json_str = content[start:end]
            events = json.loads(json_str)
            return events
        else:
            return []
    except Exception as e:
        app.logger.error(f"Event extraction error: {e}")
        return []

def generate_proactive_message(companion, user, event):
    prompt = f"""
You are {companion.name}, a supportive AI companion.
The user has an {event['type']}: {event['description']} at {event.get('datetime', 'soon')}.
Based on your past conversations, reach out to them with a warm, encouraging message.
If they mentioned this event earlier, reference it. If not, just offer support and ask how they're feeling about it.
Keep it short (2-3 sentences) and natural.

Companion personality: {companion.personality}, tone: {companion.tone}.
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=80
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        app.logger.error(f"Proactive message generation error: {e}")
        return None

def run_proactive_scheduler():
    with app.app_context():
        app.logger.info("Running proactive scheduler...")
        users = User.query.all()
        for user in users:
            recent_msgs = ChatMessage.query.join(ChatSession).filter(
                ChatSession.user_id == user.id
            ).order_by(ChatMessage.created_at.desc()).limit(20).all()
            if not recent_msgs:
                continue
            recent_msgs = list(reversed(recent_msgs))
            companion_ids = [msg.session.companion_id for msg in recent_msgs]
            if not companion_ids:
                continue
            from collections import Counter
            comp_id_counter = Counter(companion_ids)
            most_common_comp_id = comp_id_counter.most_common(1)[0][0]
            companion = Companion.query.get(most_common_comp_id)
            if not companion:
                continue
            events = extract_events_from_history(recent_msgs, most_common_comp_id, user.id)
            for event in events:
                event_datetime = None
                if 'datetime' in event and event['datetime']:
                    try:
                        dt_str = event['datetime']
                        if len(dt_str) == 10:
                            dt_str += " 09:00"
                        event_datetime = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                    except:
                        event_datetime = datetime.now() + timedelta(days=1)
                        event_datetime = event_datetime.replace(hour=9, minute=0, second=0)
                else:
                    event_datetime = datetime.now() + timedelta(days=1)
                    event_datetime = event_datetime.replace(hour=9, minute=0, second=0)
                event_hash = generate_event_hash(user.id, most_common_comp_id, event_datetime, event.get('description', ''))
                existing = ExtractedEvent.query.filter_by(hash=event_hash).first()
                if existing:
                    existing_msg = ProactiveMessage.query.filter_by(event_id=existing.id).first()
                    if existing_msg:
                        continue
                extracted = ExtractedEvent(
                    user_id=user.id,
                    companion_id=most_common_comp_id,
                    event_type=event.get('type', 'event'),
                    description=event.get('description', ''),
                    event_datetime=event_datetime,
                    is_future=event.get('is_future', True),
                    hash=event_hash
                )
                db.session.add(extracted)
                db.session.commit()
                msg_content = generate_proactive_message(companion, user, event)
                if msg_content:
                    proactive = ProactiveMessage(
                        user_id=user.id,
                        companion_id=most_common_comp_id,
                        content=msg_content,
                        event_id=extracted.id
                    )
                    db.session.add(proactive)
                    db.session.commit()
                    app.logger.info(f"Proactive message sent to user {user.id} for event {event.get('description')}")

scheduler = BackgroundScheduler()
scheduler.add_job(func=run_proactive_scheduler, trigger='interval', hours=2)
scheduler.start()

# ---------- Auth Routes (JWT) ----------
@app.route('/auth/register', methods=['POST'])
def register():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    referral_code = data.get('referral_code', '').strip().upper()

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 400

    user = User(email=email)
    user.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    user.referral_code = generate_referral_code()
    # ---------- NEW: Generate verification token ----------
    user.verification_token = generate_verification_token()
    user.email_verified = False

    if referral_code:
        referrer = User.query.filter_by(referral_code=referral_code).first()
        if referrer:
            user.referred_by = referrer.id
            referrer.bonus_messages += 100
            user.bonus_messages += 100
            db.session.add(referrer)

    db.session.add(user)
    db.session.commit()

    # ---------- SEED 12 COMPANIONS WITH NEW PERSONALITIES ----------
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

    # ---------- NEW: Send verification email via SES ----------
    verify_link = f"{request.host_url}api/verify-email?token={user.verification_token}"
    body = f"""Welcome to Aura!

Please verify your email address by clicking the link below:
{verify_link}

If you did not create an account, please ignore this email.

— The Aura Team"""
    send_email(user.email, "Verify your Aura account", body)

    access_token = create_access_token(identity=user.id)
    return jsonify({'access_token': access_token, 'user': {'email': user.email, 'id': user.id}}), 201

@app.route('/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
    user = User.query.filter_by(email=email).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, password):
        return jsonify({'error': 'Invalid credentials'}), 401
    # Note: we allow login even if not verified; client can show a warning
    access_token = create_access_token(identity=user.id)
    return jsonify({
        'access_token': access_token,
        'user': {
            'email': user.email,
            'id': user.id,
            'email_verified': user.email_verified
        }
    }), 200

# ---------- NEW: Email Verification ----------
@app.route('/api/verify-email', methods=['GET'])
def verify_email():
    token = request.args.get('token')
    if not token:
        return jsonify({'error': 'Missing token'}), 400
    user = User.query.filter_by(verification_token=token).first()
    if not user:
        return jsonify({'error': 'Invalid token'}), 400
    user.email_verified = True
    user.verification_token = None
    db.session.commit()
    # Return a simple success page (or JSON if called from app)
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Email Verified</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #f4f6fb; }
            .container { max-width: 500px; margin: 0 auto; background: white; padding: 40px; border-radius: 24px; box-shadow: 0 20px 60px rgba(0,0,0,0.08); }
            h1 { color: #4a6cf7; }
            p { color: #5a5a7a; }
            .btn { display: inline-block; padding: 10px 24px; background: #4a6cf7; color: white; border-radius: 40px; text-decoration: none; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✅ Email Verified!</h1>
            <p>Your email has been successfully verified. You can now close this window and return to the app.</p>
            <a href="/" class="btn">Go to Aura</a>
        </div>
    </body>
    </html>
    """)

# ---------- NEW: Password Reset ----------
@app.route('/api/request-reset', methods=['POST'])
def request_reset():
    data = request.get_json()
    email = data.get('email')
    if not email:
        return jsonify({'error': 'Email required'}), 400
    user = User.query.filter_by(email=email).first()
    # For security, don't reveal if email exists
    if not user:
        return jsonify({'success': True, 'message': 'If that email exists, a reset link was sent'}), 200
    # Generate reset token
    user.reset_token = secrets.token_urlsafe(32)
    user.reset_token_expiry = datetime.utcnow() + timedelta(hours=1)
    db.session.commit()
    reset_link = f"{request.host_url}reset-password?token={user.reset_token}"
    body = f"""Hello,

You requested to reset your password for your Aura account.

Click the link below to reset your password:
{reset_link}

This link will expire in 1 hour.

If you did not request this, please ignore this email.

— The Aura Team"""
    send_email(user.email, "Reset your Aura password", body)
    return jsonify({'success': True, 'message': 'Reset link sent'}), 200

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json()
    token = data.get('token')
    new_password = data.get('new_password')
    if not token or not new_password:
        return jsonify({'error': 'Token and new password required'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    user = User.query.filter_by(reset_token=token).first()
    if not user:
        return jsonify({'error': 'Invalid token'}), 400
    if user.reset_token_expiry < datetime.utcnow():
        return jsonify({'error': 'Token expired'}), 400
    user.password_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
    user.reset_token = None
    user.reset_token_expiry = None
    db.session.commit()
    return jsonify({'success': True, 'message': 'Password reset successfully'}), 200

# ---------- Protected Routes (JWT) ----------
@app.route('/api/me', methods=['GET'])
@jwt_required()
def get_user():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    return jsonify({
        'id': user.id,
        'email': user.email,
        'plan': user.plan,
        'email_verified': user.email_verified
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

# ---------- NEW API ENDPOINTS (JWT) for Flutter & Account Management ----------

@app.route('/api/update-plan', methods=['POST'])
@jwt_required()
def update_plan():
    """Sync user plan from RevenueCat entitlement changes (called from Flutter)."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json()
    new_plan = data.get('plan')
    if new_plan not in ('free', 'elite', 'pro'):
        return jsonify({'error': 'Invalid plan'}), 400
    user.plan = new_plan
    # Reset monthly usage when plan changes (optional)
    user.monthly_messages_used = 0
    user.month_start = date.today()
    db.session.commit()
    return jsonify({'success': True, 'plan': user.plan})

@app.route('/api/change-password', methods=['POST'])
@jwt_required()
def api_change_password():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.get_json()
    current = data.get('current_password')
    new_password = data.get('new_password')
    if not current or not new_password:
        return jsonify({'error': 'Current and new password required'}), 400
    if not bcrypt.check_password_hash(user.password_hash, current):
        return jsonify({'error': 'Current password is incorrect'}), 401
    if len(new_password) < 6:
        return jsonify({'error': 'New password must be at least 6 characters'}), 400
    user.password_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/delete-account', methods=['POST'])
@jwt_required()
def api_delete_account():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    # Delete all related data (cascade will handle companions, sessions, messages, etc.)
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
    # If using RevenueCat, you might call their API to cancel. For now, just downgrade.
    user.plan = 'free'
    user.subscription_product_id = None
    user.monthly_messages_used = 0
    user.month_start = date.today()
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/sessions', methods=['GET'])
@jwt_required()
def get_sessions():
    """List sessions for a companion."""
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

# ---------- SESSION-BASED WEB AUTH (for HTML views) ----------

@app.route('/login', methods=['GET', 'POST'])
def login_web():
    if request.method == 'GET':
        # If user is already logged in, redirect to home
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        return render_template('login.html')  # optional, but we can redirect to index with login modal
    # POST
    email = request.form.get('email')
    password = request.form.get('password')
    if not email or not password:
        flash('Email and password required', 'error')
        return redirect(url_for('index'))
    user = User.query.filter_by(email=email).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, password):
        flash('Invalid credentials', 'error')
        return redirect(url_for('index'))
    login_user(user, remember=True)
    flash('Logged in successfully', 'success')
    return redirect(url_for('index'))

@app.route('/signup', methods=['POST'])
def signup_web():
    email = request.form.get('email')
    password = request.form.get('password')
    referral_code = request.form.get('referral_code', '').strip().upper()
    if not email or not password:
        flash('Email and password required', 'error')
        return redirect(url_for('index'))
    if User.query.filter_by(email=email).first():
        flash('Email already registered', 'error')
        return redirect(url_for('index'))
    user = User(email=email)
    user.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
    user.referral_code = generate_referral_code()

    if referral_code:
        referrer = User.query.filter_by(referral_code=referral_code).first()
        if referrer:
            user.referred_by = referrer.id
            referrer.bonus_messages += 100
            user.bonus_messages += 100
            db.session.add(referrer)

    db.session.add(user)
    db.session.commit()

    # ---------- SEED 12 COMPANIONS WITH NEW PERSONALITIES ----------
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

    login_user(user, remember=True)
    flash('Account created!', 'success')
    return redirect(url_for('index'))

@app.route('/logout')
@login_required
def logout_web():
    logout_user()
    flash('Logged out', 'info')
    return redirect(url_for('index'))

# ---------- WEB ACCOUNT MANAGEMENT (session-based) ----------

@app.route('/change-password', methods=['POST'])
@login_required
def change_password_web():
    current = request.form.get('current_password')
    new_password = request.form.get('new_password')
    if not current or not new_password:
        flash('Both current and new password required', 'error')
        return redirect(url_for('dashboard'))
    if not bcrypt.check_password_hash(current_user.password_hash, current):
        flash('Current password is incorrect', 'error')
        return redirect(url_for('dashboard'))
    if len(new_password) < 6:
        flash('New password must be at least 6 characters', 'error')
        return redirect(url_for('dashboard'))
    current_user.password_hash = bcrypt.generate_password_hash(new_password).decode('utf-8')
    db.session.commit()
    flash('Password updated successfully', 'success')
    return redirect(url_for('dashboard'))

@app.route('/delete-account', methods=['POST'])
@login_required
def delete_account_web():
    user = current_user
    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash('Account deleted', 'info')
    return redirect(url_for('index'))

@app.route('/cancel-subscription', methods=['POST'])
@login_required
def cancel_subscription_web():
    user = current_user
    user.plan = 'free'
    user.subscription_product_id = None
    user.monthly_messages_used = 0
    user.month_start = date.today()
    db.session.commit()
    flash('Subscription cancelled. You are now on the Free plan.', 'success')
    return redirect(url_for('dashboard'))

# ---------- CHECKOUT (for web pricing) ----------
# This is a stub – replace with actual Lemon Squeezy / Stripe integration
@app.route('/create-checkout', methods=['POST'])
@login_required
def create_checkout():
    data = request.get_json()
    plan = data.get('plan')
    if plan not in ('elite', 'pro'):
        return jsonify({'error': 'Invalid plan'}), 400
    return jsonify({
        'checkout_url': f'https://your-payment-provider.com/checkout?plan={plan}',
        'message': f'Checkout for {plan} plan. (Replace with actual integration.)'
    })

# ---------- SHARED SESSION VIEW (public, read-only) ----------
@app.route('/s/<session_id>')
def shared_session(session_id):
    session_obj = ChatSession.query.filter_by(session_id=session_id).first()
    if not session_obj:
        return "Session not found", 404
    messages = session_obj.messages.order_by(ChatMessage.created_at).all()
    msg_list = [{'role': m.role, 'content': m.content, 'created_at': m.created_at.isoformat()} for m in messages]
    companion = Companion.query.get(session_obj.companion_id)
    companion_name = companion.name if companion else "Unknown"
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shared Session - {companion_name}</title>
        <link href="https://fonts.googleapis.com/css2?family=Inter:opsz,wght@14..32,300;14..32,400;14..32,500;14..32,600;14..32,700&display=swap" rel="stylesheet" />
        <style>
            * {{ margin:0; padding:0; box-sizing:border-box; }}
            body {{
                font-family: 'Inter', sans-serif;
                background: #f4f6fb;
                color: #1a1a2e;
                padding: 20px;
                display: flex;
                flex-direction: column;
                align-items: center;
                min-height: 100vh;
            }}
            .container {{
                max-width: 800px;
                width: 100%;
                background: #ffffff;
                border-radius: 24px;
                padding: 24px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.08);
            }}
            h1 {{
                font-size: 24px;
                margin-bottom: 4px;
                background: linear-gradient(135deg, #4a6cf7, #a855f7);
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
            }}
            .sub {{
                color: #5a5a7a;
                margin-bottom: 20px;
                border-left: 3px solid #4a6cf7;
                padding-left: 12px;
            }}
            .message {{
                padding: 12px 16px;
                border-radius: 12px;
                margin-bottom: 8px;
                max-width: 80%;
            }}
            .message.user {{
                background: #4a6cf7;
                color: #fff;
                align-self: flex-end;
                margin-left: auto;
            }}
            .message.assistant {{
                background: #f1f3f8;
                color: #1a1a2e;
                align-self: flex-start;
            }}
            .message .time {{
                font-size: 10px;
                opacity: 0.6;
                display: block;
                margin-top: 4px;
            }}
            .messages {{
                display: flex;
                flex-direction: column;
            }}
            .readonly-note {{
                margin-top: 20px;
                padding: 12px;
                background: #f1f3f8;
                border-radius: 12px;
                text-align: center;
                color: #5a5a7a;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🔗 Shared Session</h1>
            <div class="sub">with {companion_name} • {len(messages)} messages</div>
            <div class="messages">
    """
    for msg in msg_list:
        role = msg['role']
        content = msg['content']
        time_str = msg['created_at'][:16].replace('T', ' ')
        html += f"""
                <div class="message {role}">
                    {content}
                    <span class="time">{time_str}</span>
                </div>
        """
    html += """
            </div>
            <div class="readonly-note">📌 This is a read‑only view of a shared conversation.</div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

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
