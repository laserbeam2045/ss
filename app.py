# app.py

import os
import random
import time
from flask import Flask, render_template, request, redirect, url_for, make_response, flash
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_session import Session
from werkzeug.utils import secure_filename
from threading import Lock

# Flaskアプリケーションの設定
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_default_secret_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL') or 'sqlite:////tmp/your_database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_TYPE'] = 'filesystem'
app.config['UPLOAD_FOLDER'] = 'static/images'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

# 必要なディレクトリが存在しない場合は作成
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('/tmp', exist_ok=True)  # SQLiteファイルを/tmpに配置

# 拡張機能の初期化
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")
Session(app)

# データベースモデルの定義
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=True)
    score = db.Column(db.Integer, default=0)

class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    status = db.Column(db.String(20), default='waiting')  # 'waiting' または 'playing'

    users = db.relationship(
        'User',
        backref='room',
        lazy=True,
        foreign_keys='User.room_id'
    )

    creator = db.relationship(
        'User',
        backref='created_rooms',
        foreign_keys=[creator_id]
    )

    cards = db.relationship('Card', backref='room', lazy=True)

class Card(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image = db.Column(db.String(120), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey('room.id'), nullable=False)
    is_matched = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, nullable=False)  # カードの位置を管理

# ゲーム状態を保持するための辞書
game_states = {}
game_states_lock = Lock()

# ユーザーIDをSocket.IOのSIDに関連付けるための辞書
user_sid_map = {}
sid_user_map = {}

# データベースの初期化
with app.app_context():
    db.create_all()

# ファイルアップロードのセキュリティチェック関数
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ゲーム開始ルート
@app.route('/start_game/<int:room_id>', methods=['POST'])
def start_game(room_id):
    room = Room.query.get_or_404(room_id)
    username = request.cookies.get('username')
    print(f"Start Game: Retrieved username from cookies: {username}")

    user = User.query.filter_by(name=username).first()
    if not user:
        flash('ユーザーが見つかりません。再度ログインしてください。')
        print(f"Start Game Error: User with name '{username}' not found.")
        return redirect(url_for('set_username'))

    print(f"Start Game: User '{username}' found with ID {user.id} and room_id {user.room_id}")

    # ルームとユーザーの関連性を確認
    if user.room_id != room_id:
        flash('ルームに参加していません。')
        print(f"Start Game Error: User '{username}' is not in room '{room_id}'.")
        return redirect(url_for('room_detail', room_id=room_id))

    if room.status != 'waiting':
        flash('既にゲームが開始されています。')
        print(f"Start Game Error: Room '{room_id}' status is not 'waiting'.")
        return redirect(url_for('room_detail', room_id=room_id))

    if room.creator_id != user.id:
        flash('ゲームを開始する権限がありません。')
        print(f"Start Game Error: User '{username}' is not the creator of room '{room_id}'.")
        return redirect(url_for('room_detail', room_id=room_id))

    if len(room.users) < 2:
        flash('ゲームを開始するには最低2人の参加が必要です。')
        print(f"Start Game Error: Not enough users in room '{room_id}'. Current count: {len(room.users)}")
        return redirect(url_for('room_detail', room_id=room_id))

    # ルームの状態を「ゲーム中」に変更
    room.status = 'playing'
    db.session.commit()
    print(f"Start Game: Room '{room_id}' status set to 'playing'")

    # ゲーム状態を初期化
    with game_states_lock:
        cards = Card.query.filter_by(room_id=room_id).all()
        shuffled_cards = random.sample(cards, len(cards))  # シャッフル

        # カードの位置を再設定
        for index, card in enumerate(shuffled_cards, start=1):
            card.position = index
        db.session.commit()

        game_states[room_id] = {
            'cards': {card.id: {'name': card.name, 'is_flipped': False, 'is_matched': False, 'position': card.position} for card in shuffled_cards},
            'current_turn': user.name,  # 最初のターンはルーム作成者の名前
            'players': [u.id for u in room.users],
            'scores': {u.id: 0 for u in room.users},
            'flipped_cards': []
        }
        print(f"Start Game: Game state initialized for room '{room_id}'")

    # SocketIOでゲーム開始を通知
    socketio.emit('game_started', {'room_id': room_id}, room=room_id)
    print(f"Start Game: 'game_started' event emitted for room '{room_id}'")

    # ゲームページにリダイレクト
    return redirect(url_for('game', room_id=room_id))

# ゲームオーバー時の背景タスク
def delayed_game_over(room_id):
    with app.app_context():
        game_state = game_states.get(room_id)
        if not game_state:
            print(f"Game state for room {room_id} not found.")
            return

        # ランキングを作成
        ranking = sorted(game_state['scores'].items(), key=lambda x: x[1], reverse=True)
        ranking_data = []
        for user_id, score in ranking:
            user_obj = User.query.get(user_id)
            if user_obj:
                ranking_data.append({'username': user_obj.name, 'score': score})

        # ゲーム終了を通知
        socketio.emit('game_over', {'ranking': ranking_data}, room=room_id)
        print(f"ゲーム終了: Room ID: {room_id} のランキングが送信されました。")

        # ルームの状態をリセット
        room_obj = Room.query.get(room_id)
        if room_obj:
            room_obj.status = 'waiting'
            db.session.commit()

        # ゲーム状態を削除
        with game_states_lock:
            if room_id in game_states:
                del game_states[room_id]

# カードをめくるイベントの修正例
@socketio.on('flip_card')
def handle_flip_card(data):
    room_id = data.get('room')
    card_id = data.get('card_id')
    username = data.get('username')
    user = User.query.filter_by(name=username).first()

    if not user or user.room_id != room_id:
        emit('error', {'message': 'カードをめくる権限がありません。'})
        print(f"エラー: ユーザー {username} がルーム {room_id} に所属していません。")
        return

    print(f"{username} がルーム {room_id} のカード {card_id} をめくりました。")

    with game_states_lock:
        if room_id not in game_states:
            emit('error', {'message': 'ゲーム状態が見つかりません。'})
            print(f"エラー: Room {room_id} のゲーム状態が見つかりません。")
            return

        game_state = game_states[room_id]

        # 現在のターンのプレイヤーか確認
        if game_state['current_turn'] != username:
            emit('error', {'message': '現在のターンではありません。'})
            print(f"エラー: {username} は現在のターンではありません。")
            return

        try:
            card_id_int = int(card_id)
        except ValueError:
            emit('error', {'message': '無効なカードIDです。'})
            print(f"エラー: 無効なカードID '{card_id}' が送信されました。")
            return

        card = game_state['cards'].get(card_id_int)
        if not card:
            emit('error', {'message': 'カードが見つかりません。'})
            print(f"エラー: Room {room_id} にカード {card_id} が存在しません。")
            return
        if card['is_flipped'] or card['is_matched']:
            emit('error', {'message': '既にめくられたカードです。'})
            print(f"エラー: Room {room_id} のカード {card_id} は既にめくられています。")
            return

        # カードをめくる
        card['is_flipped'] = True
        game_state['flipped_cards'].append(card_id_int)
        print(f"カード {card_id} がめくられました。")

        # デバッグ用にカード情報をログ出力
        print(f"カード情報: {game_state['cards'][card_id_int]}")

        # 全プレイヤーにカードがめくられたことを通知
        emit('card_flipped', {'card_id': card_id, 'position': card['position'], 'username': username}, room=room_id)
        print(f"カード {card_id} のめくりが Room {room_id} の全プレイヤーに通知されました。")

        if len(game_state['flipped_cards']) == 2:
            card1_id, card2_id = game_state['flipped_cards']
            card1 = game_state['cards'][card1_id]
            card2 = game_state['cards'][card2_id]

            if card1['name'] == card2['name']:
                # マッチ成功
                card1['is_matched'] = True
                card2['is_matched'] = True
                game_state['scores'][user.id] += 1
                emit('match_result', {
                    'card1_id': card1_id,
                    'card2_id': card2_id,
                    'matched': True,
                    'scores': {str(k): v for k, v in game_state['scores'].items()}
                }, room=room_id)
                print(f"マッチ成功: カード {card1_id} と {card2_id} が一致しました。 Room ID: {room_id}")

                game_state['flipped_cards'] = []

                # 全てのカードがマッチしたか確認
                all_matched = all(c['is_matched'] for c in game_state['cards'].values())
                if all_matched:
                    # 遅延後にランキングを作成して表示
                    socketio.start_background_task(delayed_game_over, room_id)
            else:
                # マッチ失敗: 一定時間後にカードを裏返す
                def reset_cards():
                    time.sleep(1)  # 1秒待機
                    card1['is_flipped'] = False
                    card2['is_flipped'] = False
                    # 背景スレッドからemitを使用する際はsocketio.emitを使用
                    socketio.emit('cards_reset', {'card1_id': card1_id, 'card2_id': card2_id}, room=room_id)
                    print(f"カード {card1_id} と {card2_id} が Room {room_id} で裏返されました。")

                    # ターンを次のプレイヤーに変更
                    players = game_state['players']
                    current_index = players.index(user.id)
                    next_player_id = players[(current_index + 1) % len(players)]
                    next_player = User.query.get(next_player_id)
                    if next_player:
                        game_state['current_turn'] = next_player.name
                        socketio.emit('turn_changed', {'current_turn': next_player.name}, room=room_id)
                        print(f"ターンが {next_player.name} に変更されました。 Room ID: {room_id}")

                    game_state['flipped_cards'] = []

                # 背景タスクでreset_cardsを実行
                socketio.start_background_task(reset_cards)
                print(f"マッチ失敗: カード {card1_id} と {card2_id} が一致しませんでした。 Room ID: {room_id}")

# デプロイ後にデータベースを初期化するためのCLIコマンドを追加
@app.cli.command("init-db")
def init_db():
    """データベースを初期化します。"""
    with app.app_context():
        db.create_all()
        print("データベースが初期化されました。")

# アプリケーションの起動
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
