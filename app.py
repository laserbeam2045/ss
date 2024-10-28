# app.py

import os
import random
import uuid
import time
from flask import Flask, render_template, request, redirect, url_for, make_response, flash
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_session import Session
from werkzeug.utils import secure_filename
from threading import Lock

# Flaskアプリケーションの設定
app = Flask(__name__)

# app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_default_secret_key')  # 環境変数からSECRET_KEYを取得
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/your_database.db'

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///your_database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_TYPE'] = 'filesystem'
app.config['UPLOAD_FOLDER'] = 'static/images'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MBまでのアップロードを許可

# 必要なディレクトリが存在しない場合は作成
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('instance', exist_ok=True)

# 拡張機能の初期化
db = SQLAlchemy(app)
# socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")  # async_mode を 'eventlet' に設定
socketio = SocketIO(app, ping_timeout=60, ping_interval=25)
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

    # users リレーションシップを明確化
    users = db.relationship(
        'User',
        backref='room',
        lazy=True,
        foreign_keys='User.room_id'
    )

    # creator リレーションシップを追加
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

# ユーザー識別と名前の設定
@app.before_request
def get_or_set_username():
    if request.endpoint in ['static', 'set_username']:
        return
    username = request.cookies.get('username')
    if not username and request.endpoint != 'set_username':
        return redirect(url_for('set_username'))

@app.route('/set_username', methods=['GET', 'POST'])
def set_username():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        if username:
            existing_user = User.query.filter_by(name=username).first()
            if existing_user:
                flash('既に存在する名前です。別の名前を選んでください。')
                return redirect(url_for('set_username'))
            # データベースにユーザーを追加
            user = User(name=username)
            db.session.add(user)
            db.session.commit()
            # クッキーにユーザー名を設定
            resp = make_response(redirect(url_for('index')))
            resp.set_cookie('username', username)
            print(f"新規ユーザー登録: {username}")
            return resp
        else:
            flash('名前を入力してください。')
            return redirect(url_for('set_username'))
    return render_template('set_username.html')

# ルーム一覧ページ
@app.route('/')
def index():
    rooms = Room.query.all()
    username = request.cookies.get('username')
    return render_template('index.html', rooms=rooms, username=username)

# ルーム作成ページ
@app.route('/create_room', methods=['GET', 'POST'])
def create_room():
    if request.method == 'POST':
        room_name = request.form.get('room_name').strip()
        card_images = request.files.getlist('card_image')
        card_names = request.form.getlist('card_name')
        username = request.cookies.get('username')

        # バリデーション
        if not room_name:
            flash('ルーム名を入力してください。')
            return redirect(url_for('create_room'))
        if len(card_images) < 2 or len(card_names) < 2:
            flash('最低2セットのカードを追加してください。')
            return redirect(url_for('create_room'))
        if len(card_images) != len(card_names):
            flash('画像とカード名の数が一致しません。')
            return redirect(url_for('create_room'))

        # ルーム名の重複チェック
        existing_room = Room.query.filter_by(name=room_name).first()
        if existing_room:
            flash('既に存在するルーム名です。別の名前を選んでください。')
            return redirect(url_for('create_room'))

        # ルーム作成者の取得
        user = User.query.filter_by(name=username).first()
        if not user:
            flash('ユーザーが見つかりません。再度ログインしてください。')
            return redirect(url_for('set_username'))

        # ルームの作成
        room = Room(name=room_name, creator_id=user.id)
        db.session.add(room)
        db.session.commit()
        print(f"ルーム作成: {room_name} (ID: {room.id}) by {username}")

        # ルーム主をルームに参加させる
        user.room_id = room.id
        db.session.commit()
        print(f"{username} がルーム {room.name} に参加しました。")

        # SocketIOでルームに参加している全員に通知
        socketio.emit('user_joined', {'username': username}, room=room.id)

        # カードの登録（各セットを2枚ずつ）
        position = 1
        for img, name in zip(card_images, card_names):
            if img and name:
                filename = secure_filename(img.filename)
                if filename == '':
                    flash('無効なファイル名です。')
                    return redirect(url_for('create_room'))
                # ファイルアップロードのセキュリティチェック
                if not allowed_file(filename):
                    flash('許可されていないファイルタイプです。')
                    return redirect(url_for('create_room'))
                # ファイル名の一意性を確保
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                img_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                img.save(img_path)
                print(f"カード保存: {unique_filename} (Name: {name})")

                # 各カードを2枚ずつ登録
                for _ in range(2):
                    card = Card(image=unique_filename, name=name, room_id=room.id, position=position)
                    db.session.add(card)
                    position += 1
        db.session.commit()
        print(f"ルーム {room.name} にカードが追加されました。")

        flash('ルームが作成され、ルーム主として参加しました。')
        return redirect(url_for('index'))
    return render_template('create_room.html')

# ルーム参加ルート
@app.route('/join_room/<int:room_id>', methods=['POST'])
def join_room_route(room_id):
    room = Room.query.get_or_404(room_id)
    username = request.cookies.get('username')
    user = User.query.filter_by(name=username).first()
    if user and user.room_id != room_id:
        user.room_id = room_id
        db.session.commit()
        print(f"{username} がルーム {room.name} に参加しました。")
        flash(f'{room.name} に参加しました。')
        # SocketIOでルームに参加している全員に通知
        socketio.emit('user_joined', {'username': username}, room=room_id)
    return redirect(url_for('room_detail', room_id=room_id))

# ルーム詳細ページ
@app.route('/room/<int:room_id>', methods=['GET', 'POST'])
def room_detail(room_id):
    room = Room.query.get_or_404(room_id)
    users = User.query.filter_by(room_id=room_id).all()
    username = request.cookies.get('username')
    user = User.query.filter_by(name=username).first()

    # デバッグ用のログ出力
    print(f"Room ID: {room_id} Room Name: {room.name} User: {user.name} (ID: {user.id}) Users in Room: {[u.name for u in users]} Is Creator: {room.creator_id == user.id} Can Start: {room.creator_id == user.id and len(users) >= 2 and room.status == 'waiting'}")

    if request.method == 'POST':
        # 参加ボタンが押された場合（未参加ユーザーが参加）
        if user.room_id != room_id:
            user.room_id = room_id
            db.session.commit()
            print(f"{username} がルーム {room.name} に参加しました。")
            flash(f'{room.name} に参加しました。')
            # SocketIOでルームに参加している全員に通知
            socketio.emit('user_joined', {'username': username}, room=room_id)
            return redirect(url_for('room_detail', room_id=room_id))

    is_creator = (room.creator_id == user.id)
    can_start = is_creator and len(users) >= 2 and room.status == 'waiting'

    return render_template(
        'room_detail.html',
        room=room,
        users=users,
        can_start=can_start,
        is_creator=is_creator,
        username=username,
        user=user  # ここで 'user' をテンプレートに渡す
    )

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
            'current_turn': room.creator_id,  # 最初のターンはルーム作成者
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

# ゲームページ
@app.route('/game/<int:room_id>')
def game(room_id):
    room = Room.query.get_or_404(room_id)
    if room.status != 'playing':
        flash('ゲームが開始されていません。')
        return redirect(url_for('room_detail', room_id=room_id))

    # カードをシャッフルして配置（サーバー側でシャッフル済み）
    cards = Card.query.filter_by(room_id=room_id).all()
    sorted_cards = sorted(cards, key=lambda c: c.position)
    username = request.cookies.get('username')
    user = User.query.filter_by(name=username).first()

    if not user:
        flash('ユーザーが見つかりません。再度ログインしてください。')
        return redirect(url_for('set_username'))

    # ゲーム状態から現在のターンを取得
    with game_states_lock:
        game_state = game_states.get(room_id)
        if game_state:
            current_turn_user_id = game_state.get('current_turn')
            current_turn_user = User.query.get(current_turn_user_id)
            current_turn_name = current_turn_user.name if current_turn_user else "不明"
        else:
            current_turn_name = "不明"

    return render_template('game.html', room=room, cards=sorted_cards, username=username, user=user, current_turn=current_turn_name)

# ルームが空になった場合に状態をリセットする関数
def reset_room_if_empty(room_id):
    room_obj = Room.query.get(room_id)
    if room_obj and len(room_obj.users) == 0:
        room_obj.status = 'waiting'
        db.session.commit()
        with game_states_lock:
            if room_id in game_states:
                del game_states[room_id]
        print(f"Room {room_id} is now empty. Resetting to 'waiting' state.")

# SocketIO イベントハンドリング
@socketio.on('join_game')
def handle_join_game(data):
    room_id = data.get('room')
    username = data.get('username')
    user = User.query.filter_by(name=username).first()

    if not user:
        emit('error', {'message': 'ユーザーが見つかりません。'})
        print(f"Error: User '{username}' not found.")
        return

    if user.room_id != room_id:
        emit('error', {'message': 'ルームに参加していません。'})
        print(f"Error: User '{username}' is not in room '{room_id}'.")
        return

    join_room(room_id)
    user_sid_map[user.id] = request.sid
    sid_user_map[request.sid] = user.id
    print(f"{username} joined SocketIO room {room_id} with SID {request.sid}")

    with game_states_lock:
        if room_id in game_states:
            game_state = game_states[room_id]
            emit('game_state', game_state, room=request.sid)
            print(f"Game state sent to user '{username}' in room '{room_id}'")

    # 現在の参加者リストを送信
    current_players = [u.name for u in Room.query.get(room_id).users]
    emit('update_player_list', {'players': current_players}, room=room_id)
    print(f"Updated player list sent to room '{room_id}': {current_players}")

@socketio.on('leave_game')
def handle_leave_game(data):
    room_id = data.get('room')
    username = data.get('username')
    user = User.query.filter_by(name=username).first()

    if not user or user.room_id != room_id:
        emit('error', {'message': 'ルームから離脱できません。'})
        print(f"Error: User '{username}' cannot leave room '{room_id}'.")
        return

    leave_room(room_id)
    user.room_id = None
    db.session.commit()
    print(f"{username} がルーム {room_id} から離脱しました。")

    # 他のプレイヤーにユーザーが離脱したことを通知
    emit('user_left', {'username': username}, room=room_id)

    # 参加者リストを更新
    current_players = [u.name for u in Room.query.get(room_id).users if u.id != user.id]
    emit('update_player_list', {'players': current_players}, room=room_id)
    print(f"参加者リストが更新されました。Room ID: {room_id}: {current_players}")

    # マップから削除
    if user.id in user_sid_map:
        sid = user_sid_map[user.id]
        del sid_user_map[sid]
        del user_sid_map[user.id]

    emit('left_room', {'message': 'ルームから離脱しました。'}, room=request.sid)

    # ルームが空になった場合にリセット
    reset_room_if_empty(room_id)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    user_id = sid_user_map.get(sid)
    if user_id:
        user = User.query.get(user_id)
        if user:
            room_id = user.room_id
            print(f"{user.name} (ID: {user.id}) がルーム {room_id} から切断しました。 SID: {sid}")

            # SocketIOのルームからユーザーを離脱
            leave_room(room_id)

            # 他のプレイヤーにユーザーが離脱したことを通知
            emit('user_left', {'username': user.name}, room=room_id)

            # 参加者リストを更新
            current_players = [u.name for u in Room.query.get(room_id).users if u.id != user.id]
            emit('update_player_list', {'players': current_players}, room=room_id)
            print(f"参加者リストが更新されました。Room ID: {room_id}: {current_players}")

        # マップから削除
        del user_sid_map[user_id]
        del sid_user_map[sid]

        # ルームが空になった場合にリセット
        reset_room_if_empty(room_id)
    else:
        print(f"未登録のSID: {sid} が切断しました。")

# カードをめくるイベント
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
        if game_state['current_turn'] != user.name and game_state['current_turn'] != user.id:
            emit('error', {'message': '現在のターンではありません。'})
            print(f"エラー: {username} は現在のターンではありません。")
            print(game_state['current_turn'])
            print(user.name)
            # print(game_state)
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
                    'scores': game_state['scores']
                }, room=room_id)
                print(f"マッチ成功: カード {card1_id} と {card2_id} が一致しました。 Room ID: {room_id}")

                game_state['flipped_cards'] = []

                # 全てのカードがマッチしたか確認
                all_matched = all(c['is_matched'] for c in game_state['cards'].values())
                if all_matched:
                    # 遅延後にランキングを作成して表示
                    def delayed_game_over(room_id):
                        with app.app_context():
                            time.sleep(1)  # 1秒待機

                            # 現在のゲーム状態を取得
                            game_state = game_states.get(room_id)
                            
                            # ランキングを作成して全プレイヤーに送信
                            if game_state:
                                ranking = sorted(game_state['scores'].items(), key=lambda x: x[1], reverse=True)
                                ranking_data = []
                                for user_id, score in ranking:
                                    user_obj = User.query.get(user_id)
                                    ranking_data.append({'username': user_obj.name, 'score': score})
                                socketio.emit('game_over', {'ranking': ranking_data}, room=room_id)
                                print(f"ゲーム終了: Room ID: {room_id} のランキングが送信されました。")

                                # ルームの状態を待機中に戻す
                                room_obj = Room.query.get(room_id)
                                room_obj.status = 'waiting'
                                db.session.commit()

                                # ゲーム状態を削除して完全にリセット
                                with game_states_lock:
                                    if room_id in game_states:
                                        del game_states[room_id]  # ルームごとの状態をリセット
                                print(f"Room {room_id} のゲーム状態が完全にリセットされました。")

                    # 背景タスクでdelayed_game_overを実行
                    socketio.start_background_task(delayed_game_over, room_id)
            else:
                # マッチ失敗: 一定時間後にカードを裏返す
                def reset_cards(room_id, card1_id, card2_id, user_id, card1, card2):
                    with app.app_context():
                        game_state = game_states.get(room_id)
                        if not game_state:
                            print(f"Game state for room {room_id} not found.")
                            return

                        time.sleep(1)  # 1秒待機

                        # カードを裏返す
                        # card1 = Card.query.get(card1_id)
                        # card2 = Card.query.get(card2_id)
                        if card1 and card2:
                            card1['is_flipped'] = False
                            card2['is_flipped'] = False
                            db.session.commit()

                            # カードリセットを通知
                            socketio.emit('cards_reset', {'card1_id': card1_id, 'card2_id': card2_id}, room=room_id)
                            print(f"カード {card1_id} と {card2_id} が Room {room_id} で裏返されました。")

                        # ターンを次のプレイヤーに変更
                        players = game_state['players']
                        current_index = players.index(user_id)
                        next_player_id = players[(current_index + 1) % len(players)]
                        next_player_obj = User.query.get(next_player_id)
                        if next_player_obj:
                            game_state['current_turn'] = next_player_obj.name
                            db.session.commit()
                            socketio.emit('turn_changed', {'current_turn': next_player_obj.name}, room=room_id)
                            print(f"ターンが {next_player_obj.name} に変更されました。 Room ID: {room_id}")

                        # フリップされたカードをリセット
                        game_state['flipped_cards'] = []

                # 背景タスクでreset_cardsを実行
                socketio.start_background_task(reset_cards, room_id, card1_id, card2_id, user.id, card1, card2)
                print(f"マッチ失敗: カード {card1_id} と {card2_id} が一致しませんでした。 Room ID: {room_id}")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)