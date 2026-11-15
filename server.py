# -*- coding: utf-8 -*-
"""
t-chat1.3 聊天室服务器端
==================================================================
制作：汤at帅
B站主页：https://space.bilibili.com/3546392266017069?spm_id_from=333.1007.0.0
==================================================================

功能：
- 用户注册 / 登录（密码加盐哈希存储）、头像 / 用户名 / 密码修改（需验证原密码）
- 好友申请：必须对方同意才能成为好友（互相申请自动通过）
- 公共聊天室 + 私聊 + 群聊（至少 3 人才能建群）
- 多文件上传（图片 / 视频 / 音频 / 文档 / 压缩包，可一次选多个、带进度）
- 深色模式（设置里切换，跟随账号保存）
- 管理员后台：统计 / 用户 / 群聊 / 聊天记录 / 导出 / 修改管理员密码
- 管理员拥有一个聊天身份，可进入公共聊天室发言，并与所有用户互为好友
- PICO PARK 风格联机小游戏（4 位房间号，最多 8 人，合作闯关）

运行：
    python3 server.py
默认监听 0.0.0.0:5000（可用环境变量 HOST / PORT 修改）

环境变量：
    CHAT_SECRET          Flask 会话密钥（生产环境务必修改）
    ADMIN_PASSWORD       首次初始化时的管理员密码（默认 abc12345）
    TCHAT_ADMIN_FORCE    设为 1 时，用 ADMIN_PASSWORD 强制重置管理员密码
    TCHAT_MAX_MB         上传大小上限，单位 MB（默认 500）
    TCHAT_DB             数据库文件路径（默认 ./chatroom.db）
    HOST / PORT          监听地址与端口
"""
import csv
import inspect
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from functools import wraps
from io import StringIO

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from flask_socketio import SocketIO, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash

# ================================================================ 基础配置
VERSION = "1.3.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TCHAT_DATA_DIR") or os.path.join(BASE_DIR, "data")
USERS_DIR = os.path.join(DATA_DIR, "users")
MESSAGES_DIR = os.path.join(DATA_DIR, "messages")
FILES_DIR = os.path.join(DATA_DIR, "files")
USERS_DB = os.path.join(USERS_DIR, "users.db")
MESSAGES_DB = os.path.join(MESSAGES_DIR, "messages.db")
AVATAR_DIR = os.path.join(USERS_DIR, "avatars")
BACKGROUND_DIR = os.path.join(USERS_DIR, "backgrounds")
TUTORIAL_DIR = os.path.join(FILES_DIR, "tutorial")
# 兼容旧版本路径（1.2 及以前）：优先 data/chatroom.db，其次程序目录/chatroom.db
def _legacy_path(name):
    for base in (DATA_DIR, BASE_DIR):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return os.path.join(DATA_DIR, name)


LEGACY_DB = os.environ.get("TCHAT_DB") or _legacy_path("chatroom.db")
LEGACY_UPLOAD_DIR = _legacy_path("uploads")


def _detect_server_ips():
    ips = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1", "::ffff:127.0.0.1"}
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    host = os.environ.get("HOST", "")
    if host and host not in ("0.0.0.0", "::"):
        ips.add(host)
    return ips


# 服务器本机 IP（含局域网地址）不受注册上限/管理员锁定限制
SERVER_IPS = _detect_server_ips()
# 1.3 开始：用户 / 聊天消息 / 文件 三个独立文件夹
UPLOAD_DIR = FILES_DIR
DB_PATH = USERS_DB
for _d in (DATA_DIR, USERS_DIR, MESSAGES_DIR, FILES_DIR, AVATAR_DIR,
           BACKGROUND_DIR, TUTORIAL_DIR):
    os.makedirs(_d, exist_ok=True)

IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "ico", "avif"}
VIDEO_EXT = {"mp4", "webm", "ogg", "ogv", "mov", "mkv", "avi", "m4v"}
AUDIO_EXT = {"mp3", "wav", "flac", "aac", "m4a", "opus", "wma", "amr"}
# 1.3 起支持任意扩展名（未知类型当普通文件下载）
MAX_UPLOAD_MB = int(os.environ.get("TCHAT_MAX_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_AVATAR_BYTES = 8 * 1024 * 1024
MAX_BACKGROUND_BYTES = 16 * 1024 * 1024
MAX_TUTORIAL_BYTES = 1024 * 1024 * 1024

DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "abc12345")
FORCE_ADMIN_PASSWORD = os.environ.get("TCHAT_ADMIN_FORCE") == "1"

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get(
    "CHAT_SECRET", "t-chat-1.3-change-me-in-production-please")
app.config["MAX_CONTENT_LENGTH"] = max(MAX_UPLOAD_BYTES, MAX_TUTORIAL_BYTES)
app.config["JSON_AS_ASCII"] = False

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    max_http_buffer_size=MAX_UPLOAD_BYTES)

# 在线用户: {user_id: set(sid)}  一个用户可同时开多个标签页
user_sessions = {}
# 公共聊天室发言冷却: {user_id: last_ts}
public_cooldown = {}
# 管理员登录失败: {ip: {"count": n, "locked_until": ts}}
admin_attempts = {}

# 小游戏房间: {code: {...}}
GAME_MAX_PLAYERS = 8
GAME_COLORS = [
    "#ff6b6b", "#4dabf7", "#51cf66", "#ffd43b",
    "#cc5de8", "#ff922b", "#20c997", "#f783ac",
]
game_rooms = {}
game_lock = threading.Lock()


# ================================================================ 工具函数
def get_db():
    """用户库：用户、好友、群、设置、封禁、注册申请"""
    conn = sqlite3.connect(USERS_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_mdb():
    """消息库：所有聊天消息（与用户库分离，便于单独删除/替换）"""
    conn = sqlite3.connect(MESSAGES_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def now_ts():
    return time.time()


def fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "-"


def client_ip():
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return row


def is_admin_uid(uid):
    if not uid:
        return False
    conn = get_db()
    row = conn.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return bool(row and row["is_admin"])


def banned_user_ids():
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM bans_users").fetchall()
    conn.close()
    return {r["user_id"] for r in rows}


def is_user_banned(uid):
    if not uid:
        return False
    conn = get_db()
    row = conn.execute("SELECT 1 FROM bans_users WHERE user_id=?", (uid,)).fetchone()
    conn.close()
    return row is not None


def is_ip_banned(ip):
    if not ip:
        return False
    conn = get_db()
    row = conn.execute("SELECT 1 FROM bans_ips WHERE ip=?", (ip,)).fetchone()
    conn.close()
    return row is not None


def banned_words_list():
    conn = get_db()
    rows = conn.execute("SELECT word FROM banned_words").fetchall()
    conn.close()
    return [r["word"] for r in rows]


def contains_banned_word(text):
    if not text:
        return None
    low = str(text).casefold()
    for word in banned_words_list():
        if word and word.casefold() in low:
            return word
    return None


def online_uids():
    return {uid for uid, sids in user_sessions.items() if sids}


def emit_to_user(uid, event, payload):
    """给某个用户的所有在线标签页发事件"""
    for sid in list(user_sessions.get(uid, ())):
        try:
            socketio.emit(event, payload, to=sid)
        except Exception:
            pass


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    # 不用 UPSERT / ON CONFLICT 语法，兼容老系统自带的旧版 SQLite
    # （Python 3.6/3.7 所在的老麒麟、CentOS 可能仍是 SQLite < 3.24）
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (key, value))
    conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))


def ip_limit_for(ip):
    """某个 IP 的注册账号上限（优先专用限制，其次全局设置）"""
    conn = get_db()
    row = conn.execute(
        "SELECT max_accounts FROM ip_limits WHERE ip=?", (ip,)).fetchone()
    if row:
        limit = int(row["max_accounts"])
    else:
        raw = get_setting(conn, "ip_register_limit", "2")
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = 2
    conn.close()
    return max(1, limit)


def count_accounts_ip(ip):
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE reg_ip=?", (ip,)).fetchone()
    conn.close()
    return row["c"]


def game_enabled():
    conn = get_db()
    v = get_setting(conn, "game_enabled", "1")
    conn.close()
    return str(v).strip() == "1"


def friend_refresh_seconds():
    conn = get_db()
    raw = get_setting(conn, "friend_refresh_seconds", "10")
    conn.close()
    try:
        return max(3, min(600, int(raw)))
    except (TypeError, ValueError):
        return 10


def ensure_columns(conn, table, columns):
    """轻量迁移：旧版(1.1)数据库补字段"""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


def classify_ext(ext):
    """根据扩展名判断消息类型；任意扩展名都允许，未知类型当普通文件"""
    ext = (ext or "").lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "file"


def file_content(url, name, size, ext):
    return json.dumps({"url": url, "name": name, "size": size, "ext": ext},
                      ensure_ascii=False)


def parse_file_content(content):
    """把消息内容还原成文件信息，兼容 1.1 的纯 URL"""
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("url"):
            return data
    except (ValueError, TypeError):
        pass
    return {"url": content, "name": os.path.basename(str(content)), "size": 0, "ext": ""}


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "-"


def require_json():
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


# ================================================================ 数据库初始化
DEFAULT_SETTINGS = {
    "ip_register_limit": "2",
    "friend_refresh_seconds": "10",
    "game_enabled": "1",
    "tutorial_video": "",
    "tutorial_title": "t-chat1.3 使用教程",
}


def is_local_ip(ip):
    return (ip or "") in SERVER_IPS


def _migrate_legacy(conn):
    """把 1.2 的 chatroom.db / uploads 自动迁移到 1.3 的三个数据文件夹"""
    if get_setting(conn, "legacy_migrated") == "1":
        return
    if not os.path.exists(LEGACY_DB):
        # 不写标记：这样用户之后再把 1.2 的 chatroom.db 放进 data/ 也能自动导入
        return
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0:
        set_setting(conn, "legacy_migrated", "1")
        return
    old = None
    try:
        old = sqlite3.connect(LEGACY_DB)
        old.row_factory = sqlite3.Row
        tables = {r["name"] for r in old.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "users" not in tables:
            set_setting(conn, "legacy_migrated", "1")
            return
        for r in old.execute(
                "SELECT id, username, password, avatar, theme, is_admin, last_ip, created_at "
                "FROM users ORDER BY id"):
            conn.execute(
                "INSERT OR IGNORE INTO users(id, username, password, avatar, theme, "
                "is_admin, last_ip, reg_ip, tutorial_seen, chat_bg, created_by_admin, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,'',0,?)",
                (r["id"], r["username"], r["password"], r["avatar"] or "",
                 r["theme"] or "light", r["is_admin"] or 0, r["last_ip"] or "",
                 r["last_ip"] or "", r["created_at"]))
        if "friends" in tables:
            for r in old.execute("SELECT user_id, friend_id, created_at FROM friends"):
                conn.execute("INSERT OR IGNORE INTO friends VALUES (?,?,?)",
                             (r["user_id"], r["friend_id"], r["created_at"]))
        if "friend_requests" in tables:
            for r in old.execute(
                    "SELECT from_id, to_id, message, status, created_at, updated_at "
                    "FROM friend_requests"):
                conn.execute(
                    "INSERT OR IGNORE INTO friend_requests(from_id,to_id,message,status,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (r["from_id"], r["to_id"], r["message"] or "", r["status"],
                     r["created_at"], r["updated_at"]))
        if "groups" in tables:
            for r in old.execute("SELECT id, name, owner_id, created_at FROM groups"):
                conn.execute("INSERT OR IGNORE INTO groups VALUES (?,?,?,?)",
                             (r["id"], r["name"], r["owner_id"], r["created_at"]))
        if "group_members" in tables:
            for r in old.execute(
                    "SELECT group_id, user_id, role, joined_at FROM group_members"):
                conn.execute("INSERT OR IGNORE INTO group_members VALUES (?,?,?,?)",
                             (r["group_id"], r["user_id"], r["role"], r["joined_at"]))
        if "settings" in tables:
            for r in old.execute("SELECT key, value FROM settings"):
                if r["key"] in ("admin_password", "admin_user_id"):
                    set_setting(conn, r["key"], r["value"])
        if "messages" in tables:
            mconn = get_mdb()
            try:
                rows = old.execute(
                    "SELECT m.*, u.username, u.avatar, u.is_admin FROM messages m "
                    "LEFT JOIN users u ON u.id=m.sender_id ORDER BY m.id").fetchall()
                for r in rows:
                    content = (r["content"] or "").replace("/uploads/", "/files/")
                    mconn.execute(
                        "INSERT OR IGNORE INTO messages(id, room, sender_id, sender_name, "
                        "sender_avatar, sender_is_admin, msg_type, content, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["room"], r["sender_id"],
                         r["username"] or "已注销", r["avatar"] or "",
                         r["is_admin"] or 0, r["msg_type"], content, r["created_at"]))
                mconn.commit()
            finally:
                mconn.close()
        # 文件搬到 data/files，头像搬到 data/users/avatars
        old_files = LEGACY_UPLOAD_DIR
        if os.path.isdir(old_files):
            for name in os.listdir(old_files):
                src = os.path.join(old_files, name)
                if os.path.isfile(src):
                    dst = os.path.join(FILES_DIR, name)
                    if not os.path.exists(dst):
                        shutil.copy2(src, dst)
            old_avatars = os.path.join(old_files, "avatars")
            if os.path.isdir(old_avatars):
                for name in os.listdir(old_avatars):
                    src = os.path.join(old_avatars, name)
                    dst = os.path.join(AVATAR_DIR, name)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        shutil.copy2(src, dst)
        set_setting(conn, "legacy_migrated", "1")
        conn.commit()
        print("已从 1.2 数据迁移：%s" % LEGACY_DB)
    except Exception as exc:
        print("旧数据迁移失败（可忽略，继续使用新库）：%s" % exc)
    finally:
        if old is not None:
            old.close()


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            avatar     TEXT DEFAULT '',
            theme      TEXT DEFAULT 'light',
            is_admin   INTEGER DEFAULT 0,
            last_ip    TEXT,
            reg_ip     TEXT DEFAULT '',
            tutorial_seen INTEGER DEFAULT 1,
            chat_bg    TEXT DEFAULT '',
            accent_color TEXT DEFAULT '',
            created_by_admin INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS friends (
            user_id    INTEGER NOT NULL,
            friend_id  INTEGER NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (user_id, friend_id),
            FOREIGN KEY (user_id)   REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (friend_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS friend_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id    INTEGER NOT NULL,
            to_id      INTEGER NOT NULL,
            message    TEXT DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL,
            UNIQUE (from_id, to_id),
            FOREIGN KEY (from_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (to_id)   REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS groups (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            owner_id   INTEGER NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            role       TEXT DEFAULT 'member',
            joined_at  REAL NOT NULL,
            PRIMARY KEY (group_id, user_id),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id)  REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS group_join_requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            invitee_id INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            updated_at REAL,
            UNIQUE (group_id, invitee_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT
        );
        CREATE TABLE IF NOT EXISTS bans_users (
            user_id    INTEGER PRIMARY KEY,
            reason     TEXT DEFAULT '',
            banned_ip  TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bans_ips (
            ip         TEXT PRIMARY KEY,
            reason     TEXT DEFAULT '',
            source_user_id INTEGER,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS banned_words (
            word       TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ip_limits (
            ip           TEXT PRIMARY KEY,
            max_accounts INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ip_applications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ip         TEXT NOT NULL,
            username   TEXT NOT NULL,
            password   TEXT NOT NULL,
            message    TEXT DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            handled_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_friend_req_to ON friend_requests(to_id, status);
        CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_group_join_status ON group_join_requests(group_id, status);
        CREATE INDEX IF NOT EXISTS idx_ip_app_status ON ip_applications(status);
        """
    )
    ensure_columns(conn, "users", {
        "avatar": "TEXT DEFAULT ''",
        "theme": "TEXT DEFAULT 'light'",
        "is_admin": "INTEGER DEFAULT 0",
        "reg_ip": "TEXT DEFAULT ''",
        "tutorial_seen": "INTEGER DEFAULT 1",
        "chat_bg": "TEXT DEFAULT ''",
        "accent_color": "TEXT DEFAULT ''",
        "created_by_admin": "INTEGER DEFAULT 0",
    })
    conn.commit()

    # 消息库独立
    mconn = get_mdb()
    mconn.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            room       TEXT NOT NULL,
            sender_id  INTEGER NOT NULL,
            sender_name TEXT NOT NULL DEFAULT '',
            sender_avatar TEXT DEFAULT '',
            sender_is_admin INTEGER DEFAULT 0,
            msg_type   TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room, id);
        """
    )
    mconn.commit()
    mconn.close()

    _migrate_legacy(conn)
    for key, value in DEFAULT_SETTINGS.items():
        if get_setting(conn, key) is None:
            set_setting(conn, key, value)
    conn.commit()
    _ensure_admin(conn)
    conn.close()



def _ensure_admin(conn):
    """保证存在一个管理员聊天账号，并保存管理员密码哈希"""
    stored = get_setting(conn, "admin_password")
    if not stored:
        stored = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
        set_setting(conn, "admin_password", stored)
    elif FORCE_ADMIN_PASSWORD and os.environ.get("ADMIN_PASSWORD"):
        stored = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
        set_setting(conn, "admin_password", stored)
        conn.execute("UPDATE users SET password=? WHERE is_admin=1", (stored,))

    admin = conn.execute("SELECT * FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    if admin is None:
        base = "管理员"
        username = base
        suffix = 2
        while conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            username = "%s_%d" % (base, suffix)
            suffix += 1
        cur = conn.execute(
            "INSERT INTO users(username, password, avatar, theme, is_admin, last_ip, created_at)"
            " VALUES (?,?,'','light',1,?,?)",
            (username, stored, "127.0.0.1", now_ts()))
        admin_id = cur.lastrowid
    else:
        admin_id = admin["id"]
        conn.execute("UPDATE users SET is_admin=1, password=? WHERE id=?", (stored, admin_id))
    set_setting(conn, "admin_user_id", str(admin_id))
    conn.commit()


def admin_user_id():
    conn = get_db()
    val = get_setting(conn, "admin_user_id")
    if not val:
        row = conn.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        val = str(row["id"]) if row else None
    conn.close()
    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None


# ================================================================ 好友 / 群聊辅助
def are_friends(uid1, uid2):
    uid1, uid2 = int(uid1), int(uid2)
    # 管理员拥有所有人的好友关系
    if is_admin_uid(uid1) or is_admin_uid(uid2):
        return True
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (uid1, uid2)
    ).fetchone()
    conn.close()
    return row is not None


def make_friends(conn, a, b):
    ts = now_ts()
    conn.execute("INSERT OR IGNORE INTO friends VALUES (?,?,?)", (a, b, ts))
    conn.execute("INSERT OR IGNORE INTO friends VALUES (?,?,?)", (b, a, ts))


def private_room(a, b):
    return "priv:" + ":".join(str(x) for x in sorted([int(a), int(b)]))


def group_room(gid):
    return "group:%d" % int(gid)


def group_room_of(room_key):
    m = re.match(r"^group:(\d+)$", room_key or "")
    return int(m.group(1)) if m else None


def is_group_member(gid, uid):
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)
    ).fetchone()
    conn.close()
    return row is not None


def group_owner_id(gid):
    conn = get_db()
    row = conn.execute("SELECT owner_id FROM groups WHERE id=?", (gid,)).fetchone()
    conn.close()
    return row["owner_id"] if row else None


def group_member_ids(gid):
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=?", (gid,)
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def my_group_ids(uid):
    conn = get_db()
    rows = conn.execute(
        "SELECT group_id FROM group_members WHERE user_id=?", (uid,)
    ).fetchall()
    conn.close()
    return [r["group_id"] for r in rows]


def notify_group_members(gid):
    """群名/人数变化后，让所有在线成员的侧边栏刷新"""
    for uid in group_member_ids(gid):
        emit_to_user(uid, "groups_updated", {"group_id": gid})


def enter_user_room(uid, room):
    """把某用户所有在线连接加入房间（建群/拉人后用）"""
    for sid in list(user_sessions.get(uid, ())):
        try:
            socketio.server.enter_room(sid, room)
        except Exception:
            pass


def leave_user_room(uid, room):
    for sid in list(user_sessions.get(uid, ())):
        try:
            socketio.server.leave_room(sid, room)
        except Exception:
            pass


def allow_room(room_key, uid):
    """校验用户是否可访问某聊天房间"""
    if room_key == "public":
        return True
    if room_key.startswith("priv:"):
        try:
            parts = [int(x) for x in room_key.split(":")[1:] if x]
        except ValueError:
            return False
        return uid in parts
    gid = group_room_of(room_key)
    if gid is not None:
        return is_group_member(gid, uid)
    return False


def add_system_message(gid, actor_id, text):
    mconn = get_mdb()
    cur = mconn.execute(
        "INSERT INTO messages(room, sender_id, sender_name, sender_avatar, "
        "sender_is_admin, msg_type, content, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (group_room(gid), actor_id, "", "", 0, "system", text, now_ts()))
    mconn.commit()
    mid = cur.lastrowid
    mconn.close()
    payload = {
        "id": mid, "room": group_room(gid), "sender_id": actor_id,
        "username": "", "avatar": "", "is_admin": 0, "banned": False,
        "msg_type": "system", "content": text, "created_at": now_ts(),
    }
    socketio.emit("new_message", payload, to=group_room(gid))
    return payload


def login_required_api(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify(ok=False, error="未登录"), 401
        return f(*args, **kwargs)
    return wrapper


# ================================================================ 页面路由
@app.before_request
def enforce_bans():
    path = request.path
    if (path.startswith("/static/") or path.startswith("/socket.io/") or
            path in ("/favicon.ico", "/banned", "/api/logout", "/admin", "/admin/login")):
        return None
    if session.get("is_admin"):
        return None
    ip = client_ip()
    uid = session.get("user_id")
    banned = is_ip_banned(ip) or (uid and is_user_banned(uid))
    if not banned:
        return None
    if path.startswith("/api/") or path.startswith("/admin/api/"):
        return jsonify(ok=False, banned=True, error="您已被封禁"), 403
    return redirect(url_for("banned_page"))


@app.route("/")
def index():
    if current_user():
        return redirect(url_for("chat_page"))
    return redirect(url_for("login_page"))


@app.route("/login")
def login_page():
    if current_user():
        return redirect(url_for("chat_page"))
    return render_template("login.html", version=VERSION)


@app.route("/chat")
def chat_page():
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    if not user["is_admin"] and not user["tutorial_seen"]:
        return redirect(url_for("tutorial_page"))
    return render_template("chat.html", username=user["username"],
                           avatar=user["avatar"] or "",
                           theme=user["theme"] or "light",
                           chat_bg=user["chat_bg"] or "",
                           accent_color=user["accent_color"] or "",
                           is_admin=user["is_admin"], version=VERSION,
                           max_mb=MAX_UPLOAD_MB,
                           game_enabled=game_enabled(),
                           friend_refresh=friend_refresh_seconds())


@app.route("/game")
def game_page():
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    if not game_enabled():
        return redirect(url_for("chat_page"))
    return render_template("game.html", username=user["username"],
                           avatar=user["avatar"] or "",
                           theme=user["theme"] or "light",
                           is_admin=user["is_admin"], version=VERSION)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "icon.png")


@app.route("/files/<path:filename>")
def uploaded_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    inline = ext in IMAGE_EXT or ext in VIDEO_EXT or ext in AUDIO_EXT
    return send_from_directory(FILES_DIR, filename, as_attachment=not inline)


@app.route("/uploads/<path:filename>")
def legacy_uploaded_file(filename):
    """兼容 1.2 旧消息里的 /uploads/ 链接"""
    return uploaded_file(filename)


@app.route("/avatars/<path:filename>")
def avatar_file(filename):
    return send_from_directory(AVATAR_DIR, filename)


@app.route("/backgrounds/<path:filename>")
def background_file(filename):
    return send_from_directory(BACKGROUND_DIR, filename)


@app.route("/banned")
def banned_page():
    ip = client_ip()
    uid = session.get("user_id")
    reason = ""
    kind = ""
    conn = get_db()
    row = None
    if uid:
        row = conn.execute(
            "SELECT reason FROM bans_users WHERE user_id=?", (uid,)).fetchone()
        if row:
            reason = row["reason"] or ""
            kind = "user"
    if not row:
        row = conn.execute(
            "SELECT reason FROM bans_ips WHERE ip=?", (ip,)).fetchone()
        if row:
            reason = row["reason"] or ""
            kind = "ip"
    conn.close()
    return render_template("banned.html", reason=reason, kind=kind,
                           version=VERSION, ip=ip)


@app.route("/tutorial")
def tutorial_page():
    user = current_user()
    if not user:
        return redirect(url_for("login_page"))
    conn = get_db()
    video = get_setting(conn, "tutorial_video", "") or ""
    title = get_setting(conn, "tutorial_title", "t-chat1.3 使用教程") or ""
    conn.close()
    return render_template("tutorial.html", username=user["username"],
                           video=video, title=title, version=VERSION)


@app.route("/api/tutorial/seen", methods=["POST"])
@login_required_api
def api_tutorial_seen():
    me = session["user_id"]
    conn = get_db()
    conn.execute("UPDATE users SET tutorial_seen=1 WHERE id=?", (me,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


# ================================================================ 账号接口
@app.route("/api/register", methods=["POST"])
def api_register():
    data = require_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not (2 <= len(username) <= 20):
        return jsonify(ok=False, error="用户名长度需在 2~20 之间")
    if len(password) < 4:
        return jsonify(ok=False, error="密码至少 4 位")
    if any(ch in username for ch in "\n\r\t<>\"'\\/"):
        return jsonify(ok=False, error="用户名包含非法字符")

    ip = client_ip()
    if not is_local_ip(ip) and not session.get("is_admin"):
        limit = ip_limit_for(ip)
        used = count_accounts_ip(ip)
        if used >= limit:
            return jsonify(
                ok=False, ip_limited=True, limit=limit, used=used,
                error="该 IP 已注册 %d 个账号（上限 %d 个），多开请向管理员申请" % (used, limit)
            ), 403

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users(username, password, avatar, theme, is_admin, last_ip, "
            "reg_ip, tutorial_seen, chat_bg, created_by_admin, created_at) "
            "VALUES (?,?,'','light',0,?,?,0,'',0,?)",
            (username, generate_password_hash(password), ip, ip, now_ts()),
        )
        conn.commit()
        uid = cur.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify(ok=False, error="用户名已被占用")
    conn.close()

    session.clear()
    session["user_id"] = uid
    session["username"] = username
    return jsonify(ok=True, message="注册成功", username=username, tutorial=True)


@app.route("/api/register/apply", methods=["POST"])
def api_register_apply():
    """IP 注册上限时，提交多开申请给管理员"""
    data = require_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    message = (data.get("message") or "").strip()[:200]
    if not (2 <= len(username) <= 20):
        return jsonify(ok=False, error="用户名长度需在 2~20 之间")
    if len(password) < 4:
        return jsonify(ok=False, error="密码至少 4 位")
    conn = get_db()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        conn.close()
        return jsonify(ok=False, error="用户名已被占用")
    pending = conn.execute(
        "SELECT 1 FROM ip_applications WHERE username=? AND status='pending'",
        (username,)).fetchone()
    if pending:
        conn.close()
        return jsonify(ok=False, error="该用户名已有待处理申请")
    conn.execute(
        "INSERT INTO ip_applications(ip, username, password, message, status, created_at) "
        "VALUES (?,?,?,?,'pending',?)",
        (client_ip(), username, generate_password_hash(password), message, now_ts()))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="申请已提交，请等待管理员审批")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = require_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password"], password):
        conn.close()
        return jsonify(ok=False, error="用户名或密码错误")
    if is_user_banned(row["id"]):
        conn.close()
        return jsonify(ok=False, banned=True, error="您已被封禁"), 403

    ip = client_ip()
    conn.execute("UPDATE users SET last_ip=? WHERE id=?", (ip, row["id"]))
    conn.commit()
    conn.close()

    session["user_id"] = row["id"]
    session["username"] = row["username"]
    if row["is_admin"]:
        session["is_admin"] = True
    else:
        session.pop("is_admin", None)
    need_tutorial = bool(not row["is_admin"] and not row["tutorial_seen"])
    return jsonify(ok=True, username=row["username"], is_admin=bool(row["is_admin"]),
                   avatar=row["avatar"] or "", theme=row["theme"] or "light",
                   chat_bg=row["chat_bg"] or "", accent_color=row["accent_color"] or "",
                   need_tutorial=need_tutorial)


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/api/me")
@login_required_api
def api_me():
    user = current_user()
    if not user:
        session.clear()
        return jsonify(ok=False, error="未登录"), 401
    return jsonify(
        ok=True, id=user["id"], username=user["username"],
        avatar=user["avatar"] or "", theme=user["theme"] or "light",
        chat_bg=user["chat_bg"] or "", accent_color=user["accent_color"] or "",
        is_admin=bool(user["is_admin"]),
        ip=user["last_ip"], reg_ip=user["reg_ip"] or "",
        need_tutorial=bool(not user["is_admin"] and not user["tutorial_seen"]),
        version=VERSION,
        config={
            "friend_refresh_seconds": friend_refresh_seconds(),
            "game_enabled": game_enabled(),
        },
    )


# ---------------- 个人资料 ----------------
@app.route("/api/profile/username", methods=["POST"])
@login_required_api
def api_change_username():
    me = session["user_id"]
    data = require_json()
    new_name = (data.get("new_username") or "").strip()
    password = data.get("password") or ""

    if not (2 <= len(new_name) <= 20):
        return jsonify(ok=False, error="用户名长度需在 2~20 之间")
    if any(ch in new_name for ch in "\n\r\t<>\"'\\/"):
        return jsonify(ok=False, error="用户名包含非法字符")

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (me,)).fetchone()
    if not row or not check_password_hash(row["password"], password):
        conn.close()
        return jsonify(ok=False, error="原密码不正确")
    try:
        conn.execute("UPDATE users SET username=? WHERE id=?", (new_name, me))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify(ok=False, error="用户名已被占用")
    conn.close()

    session["username"] = new_name
    socketio.emit("profile_updated", {"user_id": me, "username": new_name,
                                      "avatar": row["avatar"] or ""})
    return jsonify(ok=True, username=new_name)


@app.route("/api/profile/password", methods=["POST"])
@login_required_api
def api_change_password():
    me = session["user_id"]
    data = require_json()
    old_pwd = data.get("old_password") or ""
    new_pwd = data.get("new_password") or ""
    confirm = data.get("confirm_password", new_pwd)

    if len(new_pwd) < 4:
        return jsonify(ok=False, error="新密码至少 4 位")
    if new_pwd != confirm:
        return jsonify(ok=False, error="两次输入的新密码不一致")

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id=?", (me,)).fetchone()
    if not row or not check_password_hash(row["password"], old_pwd):
        conn.close()
        return jsonify(ok=False, error="原密码不正确")
    if old_pwd == new_pwd:
        conn.close()
        return jsonify(ok=False, error="新密码不能与原密码相同")

    hashed = generate_password_hash(new_pwd)
    conn.execute("UPDATE users SET password=? WHERE id=?", (hashed, me))
    if row["is_admin"]:
        set_setting(conn, "admin_password", hashed)
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="密码修改成功")


@app.route("/api/profile/avatar", methods=["POST"])
@login_required_api
def api_change_avatar():
    me = session["user_id"]
    if "file" not in request.files:
        return jsonify(ok=False, error="请选择图片")
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify(ok=False, error="请选择图片")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in {"png", "jpg", "jpeg", "gif", "bmp", "webp"}:
        return jsonify(ok=False, error="头像仅支持 png/jpg/gif/bmp/webp")
    fname = "avatar_%d_%s.%s" % (int(me), uuid.uuid4().hex[:10], ext)
    path = os.path.join(AVATAR_DIR, fname)
    f.save(path)
    if os.path.getsize(path) > MAX_AVATAR_BYTES:
        os.remove(path)
        return jsonify(ok=False, error="头像不能超过 8MB")
    url = "/avatars/" + fname
    conn = get_db()
    conn.execute("UPDATE users SET avatar=? WHERE id=?", (url, me))
    conn.commit()
    conn.close()
    socketio.emit("profile_updated", {"user_id": me,
                                      "username": session.get("username", ""),
                                      "avatar": url})
    return jsonify(ok=True, avatar=url)


@app.route("/api/profile/background", methods=["POST"])
@login_required_api
def api_change_background():
    me = session["user_id"]
    if "file" not in request.files:
        return jsonify(ok=False, error="请选择背景图片")
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify(ok=False, error="请选择背景图片")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in {"png", "jpg", "jpeg", "gif", "bmp", "webp"}:
        return jsonify(ok=False, error="背景仅支持 png/jpg/gif/bmp/webp")
    fname = "bg_%d_%s.%s" % (int(me), uuid.uuid4().hex[:10], ext)
    path = os.path.join(BACKGROUND_DIR, fname)
    f.save(path)
    if os.path.getsize(path) > MAX_BACKGROUND_BYTES:
        os.remove(path)
        return jsonify(ok=False, error="背景图片不能超过 16MB")
    url = "/backgrounds/" + fname
    conn = get_db()
    conn.execute("UPDATE users SET chat_bg=? WHERE id=?", (url, me))
    conn.commit()
    conn.close()
    return jsonify(ok=True, chat_bg=url)


@app.route("/api/profile/background/remove", methods=["POST"])
@login_required_api
def api_remove_background():
    me = session["user_id"]
    conn = get_db()
    conn.execute("UPDATE users SET chat_bg='' WHERE id=?", (me,))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@app.route("/api/profile/theme", methods=["POST"])
@login_required_api
def api_change_theme():
    me = session["user_id"]
    data = require_json()
    theme = (data.get("theme") or "light").lower()
    if theme not in ("light", "dark"):
        return jsonify(ok=False, error="主题只能是 light 或 dark")
    conn = get_db()
    conn.execute("UPDATE users SET theme=? WHERE id=?", (theme, me))
    conn.commit()
    conn.close()
    return jsonify(ok=True, theme=theme)


@app.route("/api/profile/accent", methods=["POST"])
@login_required_api
def api_change_accent():
    """保存主页主色；空字符串表示恢复默认黑白"""
    me = session["user_id"]
    data = require_json()
    raw = str(data.get("color") or "").strip()
    if raw:
        if not raw.startswith("#"):
            raw = "#" + raw
        if re.fullmatch(r"#[0-9a-fA-F]{3}", raw):
            raw = "#" + "".join(ch * 2 for ch in raw[1:])
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
            return jsonify(ok=False, error="颜色格式应为 #RRGGBB")
        raw = raw.lower()
    conn = get_db()
    conn.execute("UPDATE users SET accent_color=? WHERE id=?", (raw, me))
    conn.commit()
    conn.close()
    return jsonify(ok=True, accent_color=raw)


# ================================================================ 用户 / 好友接口
@app.route("/api/users")
@login_required_api
def api_users():
    me = session["user_id"]
    admin_me = is_admin_uid(me)
    q = (request.args.get("q") or "").strip()

    conn = get_db()
    sql = ("SELECT id, username, avatar, is_admin, created_at FROM users "
           "WHERE id != ?")
    params = [me]
    if q:
        sql += " AND username LIKE ?"
        params.append("%" + q + "%")
    sql += " ORDER BY username LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    friend_ids = {r["friend_id"] for r in conn.execute(
        "SELECT friend_id FROM friends WHERE user_id=?", (me,)).fetchall()}
    out_req = {r["to_id"]: r["id"] for r in conn.execute(
        "SELECT id, to_id FROM friend_requests WHERE from_id=? AND status='pending'",
        (me,)).fetchall()}
    in_req = {r["from_id"]: r["id"] for r in conn.execute(
        "SELECT id, from_id FROM friend_requests WHERE to_id=? AND status='pending'",
        (me,)).fetchall()}
    banned = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM bans_users").fetchall()}
    conn.close()

    online = online_uids()
    users = []
    for r in rows:
        uid = r["id"]
        is_friend = admin_me or r["is_admin"] or uid in friend_ids
        users.append({
            "id": uid,
            "username": r["username"],
            "avatar": r["avatar"] or "",
            "is_admin": bool(r["is_admin"]),
            "is_friend": is_friend,
            "online": uid in online,
            "banned": uid in banned,
            "request_id": out_req.get(uid),
            "incoming_request_id": in_req.get(uid),
        })
    return jsonify(ok=True, users=users, is_admin=admin_me)


@app.route("/api/friends")
@login_required_api
def api_friends():
    me = session["user_id"]
    admin_me = is_admin_uid(me)
    conn = get_db()
    if admin_me:
        rows = conn.execute(
            "SELECT id, username, avatar, is_admin FROM users WHERE id != ? ORDER BY username",
            (me,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT u.id, u.username, u.avatar, u.is_admin FROM friends f "
            "JOIN users u ON u.id = f.friend_id WHERE f.user_id=? ORDER BY u.username",
            (me,)).fetchall()
        # 管理员是所有用户的好友（反向也成立），让侧边栏能看到管理员
        aid = admin_user_id()
        if aid and aid != me and all(r["id"] != aid for r in rows):
            admin_row = conn.execute(
                "SELECT id, username, avatar, is_admin FROM users WHERE id=?", (aid,)
            ).fetchone()
            if admin_row:
                rows = list(rows) + [admin_row]
    banned = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM bans_users").fetchall()}
    conn.close()
    online = online_uids()
    friends = [{
        "id": r["id"], "username": r["username"], "avatar": r["avatar"] or "",
        "is_admin": bool(r["is_admin"]), "online": r["id"] in online,
        "banned": r["id"] in banned,
    } for r in rows]
    return jsonify(ok=True, friends=friends)


@app.route("/api/friends/requests")
@login_required_api
def api_friend_requests():
    me = session["user_id"]
    conn = get_db()
    incoming = conn.execute(
        "SELECT r.id, r.from_id, r.message, r.created_at, u.username, u.avatar "
        "FROM friend_requests r JOIN users u ON u.id=r.from_id "
        "WHERE r.to_id=? AND r.status='pending' ORDER BY r.id DESC", (me,)).fetchall()
    outgoing = conn.execute(
        "SELECT r.id, r.to_id, r.message, r.created_at, u.username, u.avatar "
        "FROM friend_requests r JOIN users u ON u.id=r.to_id "
        "WHERE r.from_id=? AND r.status='pending' ORDER BY r.id DESC", (me,)).fetchall()
    conn.close()

    def pack(rows, key):
        return [{
            "request_id": r["id"], "user_id": r[key], "username": r["username"],
            "avatar": r["avatar"] or "", "message": r["message"] or "",
            "created_at": fmt_ts(r["created_at"]),
        } for r in rows]

    return jsonify(ok=True, incoming=pack(incoming, "from_id"),
                   outgoing=pack(outgoing, "to_id"))


@app.route("/api/friends/request", methods=["POST"])
@login_required_api
def api_friend_request():
    me = session["user_id"]
    data = require_json()
    try:
        fid = int(data.get("friend_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    if fid == me:
        return jsonify(ok=False, error="不能添加自己")
    message = (data.get("message") or "").strip()[:80]

    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (fid,)).fetchone()
    if not target:
        conn.close()
        return jsonify(ok=False, error="用户不存在")

    if conn.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?",
                    (me, fid)).fetchone():
        conn.close()
        return jsonify(ok=False, error="你们已经是好友了")

    # 管理员：直接成为好友（管理员本来就和所有人是好友）
    if target["is_admin"]:
        make_friends(conn, me, fid)
        conn.commit()
        conn.close()
        emit_to_user(fid, "friend_added", {"friend_id": me,
                                           "friend_name": session.get("username", "")})
        return jsonify(ok=True, status="accepted",
                       message="已添加管理员为好友，可以直接私聊")

    # 对方已经申请过 -> 互相申请，自动成为好友
    reverse = conn.execute(
        "SELECT * FROM friend_requests WHERE from_id=? AND to_id=? AND status='pending'",
        (fid, me)).fetchone()
    if reverse:
        make_friends(conn, me, fid)
        conn.execute("UPDATE friend_requests SET status='accepted', updated_at=? WHERE id=?",
                     (now_ts(), reverse["id"]))
        conn.commit()
        conn.close()
        emit_to_user(fid, "friend_request_accepted",
                     {"friend_id": me, "friend_name": session.get("username", "")})
        return jsonify(ok=True, status="accepted", message="对方也申请过，已自动成为好友")

    existing = conn.execute(
        "SELECT * FROM friend_requests WHERE from_id=? AND to_id=?", (me, fid)).fetchone()
    if existing and existing["status"] == "pending":
        conn.close()
        return jsonify(ok=False, error="已发送过申请，等待对方同意")
    if existing:
        conn.execute(
            "UPDATE friend_requests SET status='pending', message=?, created_at=?, updated_at=? "
            "WHERE id=?", (message, now_ts(), now_ts(), existing["id"]))
    else:
        conn.execute(
            "INSERT INTO friend_requests(from_id, to_id, message, status, created_at)"
            " VALUES (?,?,?,'pending',?)", (me, fid, message, now_ts()))
    conn.commit()
    conn.close()

    emit_to_user(fid, "friend_request",
                 {"from_id": me, "from_name": session.get("username", "")})
    return jsonify(ok=True, status="pending", message="好友申请已发送，等待对方同意")


@app.route("/api/friends/request/respond", methods=["POST"])
@login_required_api
def api_friend_request_respond():
    me = session["user_id"]
    data = require_json()
    try:
        rid = int(data.get("request_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    action = (data.get("action") or "").lower()
    if action not in ("accept", "reject"):
        return jsonify(ok=False, error="操作无效")

    conn = get_db()
    req = conn.execute("SELECT * FROM friend_requests WHERE id=?", (rid,)).fetchone()
    if not req or req["to_id"] != me:
        conn.close()
        return jsonify(ok=False, error="申请不存在")
    if req["status"] != "pending":
        conn.close()
        return jsonify(ok=False, error="该申请已处理")

    if action == "accept":
        make_friends(conn, me, req["from_id"])
        status = "accepted"
    else:
        status = "rejected"
    conn.execute("UPDATE friend_requests SET status=?, updated_at=? WHERE id=?",
                 (status, now_ts(), rid))
    conn.commit()
    conn.close()

    if action == "accept":
        emit_to_user(req["from_id"], "friend_request_accepted",
                     {"friend_id": me, "friend_name": session.get("username", "")})
        return jsonify(ok=True, status="accepted", message="已添加为好友")
    return jsonify(ok=True, status="rejected", message="已拒绝该申请")


@app.route("/api/friends/request/cancel", methods=["POST"])
@login_required_api
def api_friend_request_cancel():
    me = session["user_id"]
    data = require_json()
    try:
        rid = int(data.get("request_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    conn = get_db()
    req = conn.execute("SELECT * FROM friend_requests WHERE id=?", (rid,)).fetchone()
    if not req or req["from_id"] != me:
        conn.close()
        return jsonify(ok=False, error="申请不存在")
    conn.execute("UPDATE friend_requests SET status='cancelled', updated_at=? WHERE id=?",
                 (now_ts(), rid))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已撤回申请")


@app.route("/api/friends/remove", methods=["POST"])
@login_required_api
def api_remove_friend():
    me = session["user_id"]
    data = require_json()
    try:
        fid = int(data.get("friend_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")

    conn = get_db()
    conn.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?", (me, fid))
    conn.execute("DELETE FROM friends WHERE user_id=? AND friend_id=?", (fid, me))
    conn.execute("DELETE FROM friend_requests WHERE (from_id=? AND to_id=?) "
                 "OR (from_id=? AND to_id=?)", (me, fid, fid, me))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已删除好友")


# ================================================================ 群聊接口
def _group_last_preview(conn, gid):
    mconn = get_mdb()
    row = mconn.execute(
        "SELECT msg_type, content, created_at, sender_name FROM messages "
        "WHERE room=? ORDER BY id DESC LIMIT 1", (group_room(gid),)).fetchone()
    mconn.close()
    if not row:
        return ""
    t = row["msg_type"]
    if t == "text":
        text = row["content"]
    elif t == "image":
        text = "[图片]"
    elif t == "video":
        text = "[视频]"
    elif t == "audio":
        text = "[语音]"
    elif t == "system":
        return row["content"][:40]
    elif t == "game_invite":
        text = "[小游戏邀请]"
    else:
        text = "[文件] " + parse_file_content(row["content"]).get("name", "")
    return ("%s: %s" % (row["sender_name"] or "已注销", text))[:60]


@app.route("/api/groups")
@login_required_api
def api_groups():
    me = session["user_id"]
    conn = get_db()
    rows = conn.execute(
        "SELECT g.id, g.name, g.owner_id, g.created_at, gm.role, gm.joined_at "
        "FROM group_members gm JOIN groups g ON g.id = gm.group_id "
        "WHERE gm.user_id=? ORDER BY g.id DESC", (me,)).fetchall()
    groups = []
    for r in rows:
        gid = r["id"]
        count = conn.execute("SELECT COUNT(*) c FROM group_members WHERE group_id=?",
                             (gid,)).fetchone()["c"]
        pending = 0
        if r["owner_id"] == me:
            pending = conn.execute(
                "SELECT COUNT(*) c FROM group_join_requests "
                "WHERE group_id=? AND status='pending'", (gid,)).fetchone()["c"]
        groups.append({
            "id": gid, "name": r["name"], "owner_id": r["owner_id"],
            "role": r["role"] or "member", "member_count": count,
            "pending_join": pending,
            "created_at": fmt_ts(r["created_at"]),
            "last_message": _group_last_preview(conn, gid),
            "room": group_room(gid),
        })
    conn.close()
    return jsonify(ok=True, groups=groups)


@app.route("/api/groups/create", methods=["POST"])
@login_required_api
def api_group_create():
    me = session["user_id"]
    data = require_json()
    name = (data.get("name") or "").strip()
    raw = data.get("member_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [x for x in raw.split(",") if x.strip()]
    if not name:
        return jsonify(ok=False, error="请输入群聊名称")
    if len(name) > 20:
        return jsonify(ok=False, error="群聊名称不能超过 20 个字")

    member_ids = []
    for x in raw:
        try:
            uid = int(x)
        except (TypeError, ValueError):
            continue
        if uid != me and uid not in member_ids:
            member_ids.append(uid)
    if len(member_ids) + 1 < 3:
        return jsonify(ok=False, error="群聊至少需要 3 个人（你 + 2 位好友）")

    conn = get_db()
    valid = [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE id IN (%s)" % ",".join("?" * len(member_ids)),
        member_ids).fetchall()] if member_ids else []
    if len(valid) + 1 < 3:
        conn.close()
        return jsonify(ok=False, error="所选用户不存在，群聊至少需要 3 个人")

    cur = conn.execute("INSERT INTO groups(name, owner_id, created_at) VALUES (?,?,?)",
                       (name, me, now_ts()))
    gid = cur.lastrowid
    ts = now_ts()
    conn.execute("INSERT INTO group_members VALUES (?,?,?,?)", (gid, me, "owner", ts))
    for uid in valid:
        conn.execute("INSERT INTO group_members VALUES (?,?,?,?)",
                     (gid, uid, "member", ts))
    conn.commit()
    conn.close()

    room = group_room(gid)
    for uid in [me] + valid:
        enter_user_room(uid, room)
    add_system_message(gid, me, "%s 创建了群聊「%s」" % (session.get("username", ""), name))
    notify_group_members(gid)
    return jsonify(ok=True, group_id=gid, room=room, name=name,
                   message="群聊创建成功")


@app.route("/api/groups/<int:gid>/members")
@login_required_api
def api_group_members(gid):
    me = session["user_id"]
    if not is_group_member(gid, me):
        return jsonify(ok=False, error="你不在该群聊中"), 403
    conn = get_db()
    rows = conn.execute(
        "SELECT u.id, u.username, u.avatar, u.is_admin, gm.role, gm.joined_at "
        "FROM group_members gm JOIN users u ON u.id=gm.user_id "
        "WHERE gm.group_id=? ORDER BY (gm.role='owner') DESC, gm.joined_at", (gid,)).fetchall()
    banned = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM bans_users").fetchall()}
    conn.close()
    members = [{
        "id": r["id"], "username": r["username"], "avatar": r["avatar"] or "",
        "is_admin": bool(r["is_admin"]), "role": r["role"] or "member",
        "banned": r["id"] in banned,
    } for r in rows]
    return jsonify(ok=True, members=members, owner_id=members[0]["id"] if members else None)


@app.route("/api/groups/<int:gid>/add", methods=["POST"])
@login_required_api
def api_group_add(gid):
    me = session["user_id"]
    data = require_json()
    raw = data.get("user_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [x for x in raw.split(",") if x.strip()]
    if not is_group_member(gid, me):
        return jsonify(ok=False, error="你不在该群聊中"), 403
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not group:
        conn.close()
        return jsonify(ok=False, error="群聊不存在")
    if group["owner_id"] != me:
        conn.close()
        return jsonify(ok=False, error="只有群主可以添加成员")

    existing = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=?", (gid,)).fetchall()}
    added = []
    for x in raw:
        try:
            uid = int(x)
        except (TypeError, ValueError):
            continue
        if uid in existing:
            continue
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
            continue
        conn.execute("INSERT INTO group_members VALUES (?,?,?,?)",
                     (gid, uid, "member", now_ts()))
        added.append(uid)
        existing.add(uid)
    conn.commit()
    conn.close()

    if not added:
        return jsonify(ok=False, error="没有可添加的新成员")
    room = group_room(gid)
    for uid in added:
        enter_user_room(uid, room)
    names = _usernames(added)
    add_system_message(gid, me, "%s 邀请 %s 加入了群聊" %
                       (session.get("username", ""), "、".join(names)))
    notify_group_members(gid)
    return jsonify(ok=True, added=added, message="已添加 %d 位成员" % len(added))


def _usernames(uids):
    if not uids:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT username FROM users WHERE id IN (%s)" % ",".join("?" * len(uids)),
        list(uids)).fetchall()
    conn.close()
    return [r["username"] for r in rows]


@app.route("/api/groups/<int:gid>/remove", methods=["POST"])
@login_required_api
def api_group_remove(gid):
    me = session["user_id"]
    data = require_json()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not group:
        conn.close()
        return jsonify(ok=False, error="群聊不存在")
    if group["owner_id"] != me:
        conn.close()
        return jsonify(ok=False, error="只有群主可以移除成员")
    if uid == me:
        conn.close()
        return jsonify(ok=False, error="不能移除自己，请使用退出群聊")
    conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, uid))
    conn.commit()
    conn.close()

    leave_user_room(uid, group_room(gid))
    emit_to_user(uid, "kicked_from_group", {"group_id": gid})
    add_system_message(gid, me, "%s 将 %s 移出了群聊" %
                       (session.get("username", ""), "、".join(_usernames([uid]))))
    notify_group_members(gid)
    return jsonify(ok=True, message="已移除该成员")


@app.route("/api/groups/<int:gid>/leave", methods=["POST"])
@login_required_api
def api_group_leave(gid):
    me = session["user_id"]
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not group:
        conn.close()
        return jsonify(ok=False, error="群聊不存在")
    if not conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?",
                        (gid, me)).fetchone():
        conn.close()
        return jsonify(ok=False, error="你不在该群聊中")
    conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, me))
    remaining = conn.execute(
        "SELECT user_id FROM group_members WHERE group_id=? ORDER BY joined_at", (gid,)
    ).fetchall()
    if not remaining:
        conn.execute("DELETE FROM groups WHERE id=?", (gid,))
        conn.execute("DELETE FROM group_join_requests WHERE group_id=?", (gid,))
        conn.commit()
        conn.close()
        mconn = get_mdb()
        mconn.execute("DELETE FROM messages WHERE room=?", (group_room(gid),))
        mconn.commit()
        mconn.close()
        return jsonify(ok=True, message="已退出并解散该群聊")

    if group["owner_id"] == me:
        new_owner = remaining[0]["user_id"]
        conn.execute("UPDATE groups SET owner_id=? WHERE id=?", (new_owner, gid))
        conn.execute("UPDATE group_members SET role='owner' WHERE group_id=? AND user_id=?",
                     (gid, new_owner))
    else:
        new_owner = None
    conn.commit()
    conn.close()

    leave_user_room(me, group_room(gid))
    add_system_message(gid, me, "%s 退出了群聊" % session.get("username", ""))
    if new_owner:
        add_system_message(gid, me, "%s 成为新的群主" %
                           "、".join(_usernames([new_owner])))
    notify_group_members(gid)
    return jsonify(ok=True, message="已退出群聊")


@app.route("/api/groups/<int:gid>/rename", methods=["POST"])
@login_required_api
def api_group_rename(gid):
    me = session["user_id"]
    data = require_json()
    name = (data.get("name") or "").strip()
    if not name or len(name) > 20:
        return jsonify(ok=False, error="群名需要 1~20 个字")
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    if not group:
        conn.close()
        return jsonify(ok=False, error="群聊不存在")
    if group["owner_id"] != me:
        conn.close()
        return jsonify(ok=False, error="只有群主可以修改群名")
    conn.execute("UPDATE groups SET name=? WHERE id=?", (name, gid))
    conn.commit()
    conn.close()
    add_system_message(gid, me, "群名已修改为「%s」" % name)
    notify_group_members(gid)
    return jsonify(ok=True, name=name, message="群名已修改")


@app.route("/api/groups/<int:gid>/invite", methods=["POST"])
@login_required_api
def api_group_invite(gid):
    me = session["user_id"]
    if not is_group_member(gid, me):
        return jsonify(ok=False, error="你不在该群聊中"), 403
    data = require_json()
    try:
        target = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    if target == me:
        return jsonify(ok=False, error="不能邀请自己")
    conn = get_db()
    group = conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    target_user = conn.execute("SELECT * FROM users WHERE id=?", (target,)).fetchone()
    if not group or not target_user:
        conn.close()
        return jsonify(ok=False, error="用户或群聊不存在")
    if is_user_banned(target):
        conn.close()
        return jsonify(ok=False, error="该用户已被封禁")
    if conn.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?",
                    (gid, target)).fetchone():
        conn.close()
        return jsonify(ok=False, error="对方已经在群里")
    existing = conn.execute(
        "SELECT * FROM group_join_requests WHERE group_id=? AND invitee_id=?",
        (gid, target)).fetchone()
    if existing and existing["status"] == "pending":
        conn.close()
        return jsonify(ok=False, error="已经发过申请，等待群主同意")
    if existing:
        conn.execute(
            "UPDATE group_join_requests SET inviter_id=?, status='pending', "
            "created_at=?, updated_at=NULL WHERE id=?",
            (me, now_ts(), existing["id"]))
    else:
        conn.execute(
            "INSERT INTO group_join_requests(group_id, inviter_id, invitee_id, "
            "status, created_at) VALUES (?,?,?,'pending',?)",
            (gid, me, target, now_ts()))
    conn.commit()
    owner_id = group["owner_id"]
    conn.close()
    emit_to_user(owner_id, "group_join_request", {
        "group_id": gid, "group_name": group["name"],
        "inviter": session.get("username", ""),
        "invitee": target_user["username"],
    })
    return jsonify(ok=True, message="已发送加入申请，等待群主同意")


@app.route("/api/groups/<int:gid>/requests")
@login_required_api
def api_group_requests(gid):
    me = session["user_id"]
    if group_owner_id(gid) != me:
        return jsonify(ok=False, error="只有群主可以查看入群申请"), 403
    conn = get_db()
    rows = conn.execute(
        "SELECT r.id, r.inviter_id, r.invitee_id, r.created_at, "
        "i.username AS inviter, e.username AS invitee, e.avatar AS invitee_avatar "
        "FROM group_join_requests r "
        "JOIN users i ON i.id=r.inviter_id "
        "JOIN users e ON e.id=r.invitee_id "
        "WHERE r.group_id=? AND r.status='pending' ORDER BY r.id DESC",
        (gid,)).fetchall()
    conn.close()
    requests = [{
        "id": r["id"], "inviter": r["inviter"], "invitee": r["invitee"],
        "invitee_id": r["invitee_id"],
        "invitee_avatar": r["invitee_avatar"] or "",
        "created_at": fmt_ts(r["created_at"]),
    } for r in rows]
    return jsonify(ok=True, requests=requests)


@app.route("/api/groups/<int:gid>/requests/<int:rid>/respond", methods=["POST"])
@login_required_api
def api_group_request_respond(gid, rid):
    me = session["user_id"]
    if group_owner_id(gid) != me:
        return jsonify(ok=False, error="只有群主可以处理入群申请"), 403
    data = require_json()
    action = (data.get("action") or "").lower()
    if action not in ("accept", "reject"):
        return jsonify(ok=False, error="操作无效")
    conn = get_db()
    request = conn.execute(
        "SELECT * FROM group_join_requests WHERE id=? AND group_id=?",
        (rid, gid)).fetchone()
    if not request or request["status"] != "pending":
        conn.close()
        return jsonify(ok=False, error="申请不存在或已处理")
    if action == "accept":
        exists = conn.execute(
            "SELECT 1 FROM group_members WHERE group_id=? AND user_id=?",
            (gid, request["invitee_id"])).fetchone()
        if not exists:
            conn.execute("INSERT INTO group_members VALUES (?,?,?,?)",
                         (gid, request["invitee_id"], "member", now_ts()))
        conn.execute(
            "UPDATE group_join_requests SET status='accepted', updated_at=? WHERE id=?",
            (now_ts(), rid))
        conn.commit()
        conn.close()
        enter_user_room(request["invitee_id"], group_room(gid))
        _notify_group_join_result(gid, request, True)
        return jsonify(ok=True, message="已同意入群")
    conn.execute(
        "UPDATE group_join_requests SET status='rejected', updated_at=? WHERE id=?",
        (now_ts(), rid))
    conn.commit()
    conn.close()
    _notify_group_join_result(gid, request, False)
    return jsonify(ok=True, message="已拒绝入群")


def _notify_group_join_result(gid, request, accepted):
    conn = get_db()
    group = conn.execute("SELECT name FROM groups WHERE id=?", (gid,)).fetchone()
    names = {r["id"]: r["username"] for r in conn.execute(
        "SELECT id, username FROM users WHERE id IN (?,?)",
        (request["inviter_id"], request["invitee_id"])).fetchall()}
    conn.close()
    group_name = group["name"] if group else "群聊"
    invitee = names.get(request["invitee_id"], "用户")
    if accepted:
        add_system_message(gid, request["invitee_id"],
                           "%s 加入了群聊" % invitee)
        emit_to_user(request["invitee_id"], "groups_updated", {"group_id": gid})
        notify_group_members(gid)
    emit_to_user(request["inviter_id"], "group_join_result", {
        "group_id": gid, "group_name": group_name,
        "invitee": invitee, "accepted": bool(accepted),
    })
    emit_to_user(request["invitee_id"], "group_join_result", {
        "group_id": gid, "group_name": group_name,
        "invitee": invitee, "accepted": bool(accepted),
    })


def _enrich_messages(msgs):
    """消息里保存了发送者快照；这里用当前用户资料覆盖，并补上封禁标签"""
    ids = {m["sender_id"] for m in msgs if m.get("sender_id")}
    if not ids:
        return msgs
    conn = get_db()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT id, username, avatar, is_admin FROM users WHERE id IN (%s)" % placeholders,
        list(ids)).fetchall()
    banned = {r["user_id"] for r in conn.execute("SELECT user_id FROM bans_users").fetchall()}
    conn.close()
    info = {r["id"]: r for r in rows}
    for m in msgs:
        u = info.get(m["sender_id"])
        if u:
            m["username"] = u["username"]
            m["avatar"] = u["avatar"] or ""
            m["is_admin"] = bool(u["is_admin"])
        m["banned"] = m["sender_id"] in banned
    return msgs


# ================================================================ 消息接口
@app.route("/api/messages/<room_key>", methods=["GET"])
@login_required_api
def api_messages(room_key):
    me = session["user_id"]
    try:
        limit = min(int(request.args.get("limit", 80)), 300)
    except ValueError:
        limit = 80
    if not allow_room(room_key, me):
        return jsonify(ok=False, error="无权访问该会话"), 403

    mconn = get_mdb()
    rows = mconn.execute(
        "SELECT * FROM messages WHERE room=? ORDER BY id DESC LIMIT ?",
        (room_key, limit)).fetchall()
    mconn.close()

    msgs = [{
        "id": r["id"], "sender_id": r["sender_id"],
        "username": r["sender_name"] or "已注销",
        "avatar": r["sender_avatar"] or "",
        "is_admin": bool(r["sender_is_admin"]),
        "msg_type": r["msg_type"], "content": r["content"],
        "created_at": r["created_at"],
    } for r in reversed(rows)]
    return jsonify(ok=True, messages=_enrich_messages(msgs))


# ================================================================ 文件上传
@app.route("/api/upload", methods=["POST"])
@login_required_api
def api_upload():
    files = request.files.getlist("file") + request.files.getlist("files")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify(ok=False, error="没有文件")

    out = []
    for f in files:
        original = os.path.basename(f.filename.replace("\\", "/"))
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        msg_type = classify_ext(ext)
        fname = "%s.%s" % (uuid.uuid4().hex, ext) if ext else uuid.uuid4().hex
        path = os.path.join(UPLOAD_DIR, fname)
        f.save(path)
        size = os.path.getsize(path)
        url = "/files/" + fname
        if msg_type in ("image", "video"):
            content = url
        else:
            content = file_content(url, original, size, ext)
        out.append({
            "url": url, "name": original, "size": size,
            "size_text": human_size(size), "ext": ext,
            "msg_type": msg_type, "content": content,
        })

    if not out:
        return jsonify(ok=False, error="没有有效文件")
    return jsonify(ok=True, files=out,
                   url=out[0]["url"], msg_type=out[0]["msg_type"],
                   max_mb=MAX_UPLOAD_MB)


@app.errorhandler(413)
def handle_413(e):
    return jsonify(ok=False,
                   error="文件过大，超过 %dMB 限制" % MAX_UPLOAD_MB), 413


# ================================================================ Socket.IO 聊天
def _message_payload(mid, room_key, sender_id, username, avatar, is_admin,
                     msg_type, content, created_at, target_id=None):
    payload = {
        "id": mid, "room": room_key, "sender_id": sender_id,
        "username": username, "avatar": avatar or "", "is_admin": bool(is_admin),
        "banned": False, "msg_type": msg_type, "content": content,
        "created_at": created_at,
    }
    if target_id is not None:
        payload["target_id"] = target_id
    return payload


def _store_message(room_key, sender_id, msg_type, content):
    uconn = get_db()
    u = uconn.execute("SELECT username, avatar, is_admin FROM users WHERE id=?",
                      (sender_id,)).fetchone()
    uconn.close()
    name = u["username"] if u else "已注销"
    avatar = (u["avatar"] if u else "") or ""
    is_admin = (u["is_admin"] if u else 0) or 0
    mconn = get_mdb()
    cur = mconn.execute(
        "INSERT INTO messages(room, sender_id, sender_name, sender_avatar, "
        "sender_is_admin, msg_type, content, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (room_key, sender_id, name, avatar, is_admin, msg_type, content, now_ts()))
    mconn.commit()
    mid = cur.lastrowid
    mconn.close()
    return mid


@socketio.on("connect")
def on_connect():
    uid = session.get("user_id")
    if not uid:
        return False
    user = current_user()
    if not user:
        return False
    if is_user_banned(uid) or is_ip_banned(getattr(request, "remote_addr", "")):
        return False
    user_sessions.setdefault(uid, set()).add(request.sid)
    join_room("public")
    for gid in my_group_ids(uid):
        join_room(group_room(gid))
    socketio.emit("online_update", {"user_id": uid, "online": True})


@socketio.on("disconnect")
def on_disconnect(*args):
    uid = session.get("user_id")
    sid = request.sid
    if not uid:
        return
    sids = user_sessions.get(uid)
    if sids:
        sids.discard(sid)
        if not sids:
            user_sessions.pop(uid, None)
            socketio.emit("online_update", {"user_id": uid, "online": False})


@socketio.on("send_message")
def on_send_message(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    if not user:
        return
    if is_user_banned(uid):
        socketio.emit("banned", {"reason": "您已被封禁"}, to=request.sid)
        return

    msg_type = data.get("msg_type", "text")
    if msg_type not in ("text", "image", "video", "audio", "file"):
        return
    content = (data.get("content") or "").strip()
    if not content:
        return
    if msg_type == "text" and len(content) > 5000:
        socketio.emit("error_msg", {"error": "消息太长（最多 5000 字）"}, to=request.sid)
        return

    group_id = data.get("group_id")
    direct_to = data.get("to")

    if group_id:
        try:
            gid = int(group_id)
        except (TypeError, ValueError):
            return
        if not is_group_member(gid, uid):
            socketio.emit("error_msg", {"error": "你不在该群聊中"}, to=request.sid)
            return
        room_key = group_room(gid)
    elif direct_to not in (None, "", "null"):
        try:
            target = int(direct_to)
        except (TypeError, ValueError):
            return
        if not are_friends(uid, target):
            socketio.emit("error_msg", {"error": "对方不是你的好友，无法私聊"},
                          to=request.sid)
            return
        room_key = private_room(uid, target)
    else:
        target = None
        room_key = "public"

    if room_key == "public":
        now = time.time()
        last = public_cooldown.get(uid, 0)
        if now - last < 5:
            remain = int(5 - (now - last)) + 1
            socketio.emit("error_msg",
                          {"error": "公共聊天室发言间隔为 5 秒，还需 %d 秒" % remain},
                          to=request.sid)
            return
        check_text = content
        if msg_type in ("file", "audio"):
            check_text = parse_file_content(content).get("name", "")
        if msg_type in ("text", "file", "audio"):
            bad = contains_banned_word(check_text)
            if bad:
                socketio.emit("error_msg",
                              {"error": "消息包含违禁词「%s」，禁止发送" % bad},
                              to=request.sid)
                return

    mid = _store_message(room_key, uid, msg_type, content)
    payload = _message_payload(mid, room_key, uid, user["username"],
                               user["avatar"], user["is_admin"], msg_type,
                               content, now_ts(),
                               target if room_key.startswith("priv:") else None)
    if room_key == "public":
        public_cooldown[uid] = time.time()

    if room_key == "public":
        socketio.emit("new_message", payload, to="public")
    elif room_key.startswith("priv:"):
        emit_to_user(target, "new_message", payload)
        emit_to_user(uid, "new_message", payload)
    else:
        socketio.emit("new_message", payload, to=room_key)


# ================================================================ 管理员后台
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login_page"))
        return f(*args, **kwargs)
    return wrapper


def admin_api_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify(ok=False, error="管理员未登录"), 403
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin", methods=["GET"])
def admin_login_page():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html", version=VERSION)


@app.route("/admin/login", methods=["POST"])
def admin_login_api():
    data = require_json()
    pwd = data.get("password") or ""
    ip = client_ip()

    if not is_local_ip(ip):
        record = admin_attempts.get(ip)
        if record and record.get("locked_until", 0) > time.time():
            remain = int(record["locked_until"] - time.time())
            return jsonify(
                ok=False, locked=True, retry_after=remain,
                error="密码错误次数过多，请 %d 分钟后再试" % max(1, remain // 60)
            ), 429

    conn = get_db()
    stored = get_setting(conn, "admin_password")
    admin_id = get_setting(conn, "admin_user_id")
    if not admin_id:
        row = conn.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
        admin_id = row["id"] if row else None
    conn.close()

    if not stored or not admin_id or not check_password_hash(stored, pwd):
        if not is_local_ip(ip):
            record = admin_attempts.get(ip) or {"count": 0, "locked_until": 0}
            record["count"] += 1
            if record["count"] >= 3:
                record["locked_until"] = time.time() + 1800
                record["count"] = 0
            admin_attempts[ip] = record
        return jsonify(ok=False, error="管理员密码错误"), 403

    admin_attempts.pop(ip, None)
    session["is_admin"] = True
    session["user_id"] = int(admin_id)
    session["username"] = "管理员"
    return jsonify(ok=True)


@app.route("/admin/logout", methods=["POST"])
def admin_logout_api():
    session.clear()
    return jsonify(ok=True)


@app.route("/admin/enter-chat")
def admin_enter_chat():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login_page"))
    return redirect(url_for("chat_page"))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin.html", version=VERSION)


@app.route("/admin/api/password", methods=["POST"])
@admin_api_required
def admin_change_password():
    data = require_json()
    old_pwd = data.get("old_password") or ""
    new_pwd = data.get("new_password") or ""
    confirm = data.get("confirm_password", new_pwd)

    if len(new_pwd) < 6:
        return jsonify(ok=False, error="管理员密码至少 6 位")
    if new_pwd != confirm:
        return jsonify(ok=False, error="两次输入的新密码不一致")

    conn = get_db()
    stored = get_setting(conn, "admin_password")
    if not stored or not check_password_hash(stored, old_pwd):
        conn.close()
        return jsonify(ok=False, error="原管理员密码不正确")
    if old_pwd == new_pwd:
        conn.close()
        return jsonify(ok=False, error="新密码不能与原密码相同")

    hashed = generate_password_hash(new_pwd)
    set_setting(conn, "admin_password", hashed)
    conn.execute("UPDATE users SET password=? WHERE is_admin=1", (hashed,))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="管理员密码已修改，下次登录请使用新密码")


# ---------------- 管理员设置 / 封禁 / 申请 / 教程 ----------------
@app.route("/admin/api/settings", methods=["GET"])
@admin_api_required
def admin_get_settings():
    conn = get_db()
    data = {
        "ip_register_limit": get_setting(conn, "ip_register_limit", "2"),
        "friend_refresh_seconds": get_setting(conn, "friend_refresh_seconds", "10"),
        "game_enabled": get_setting(conn, "game_enabled", "1"),
        "tutorial_video": get_setting(conn, "tutorial_video", "") or "",
        "tutorial_title": get_setting(conn, "tutorial_title", "t-chat1.3 使用教程"),
        "banned_words_count": conn.execute(
            "SELECT COUNT(*) c FROM banned_words").fetchone()["c"],
    }
    conn.close()
    if data["tutorial_video"]:
        data["tutorial_url"] = "/files/" + data["tutorial_video"]
    else:
        data["tutorial_url"] = ""
    return jsonify(ok=True, settings=data)


@app.route("/admin/api/settings", methods=["POST"])
@admin_api_required
def admin_update_settings():
    data = require_json()
    conn = get_db()
    if "ip_register_limit" in data:
        try:
            limit = int(data["ip_register_limit"])
        except (TypeError, ValueError):
            conn.close()
            return jsonify(ok=False, error="IP 注册上限必须是数字")
        if not (1 <= limit <= 100):
            conn.close()
            return jsonify(ok=False, error="IP 注册上限需在 1~100 之间")
        set_setting(conn, "ip_register_limit", str(limit))
    if "friend_refresh_seconds" in data:
        try:
            seconds = int(data["friend_refresh_seconds"])
        except (TypeError, ValueError):
            conn.close()
            return jsonify(ok=False, error="刷新间隔必须是数字")
        if not (3 <= seconds <= 600):
            conn.close()
            return jsonify(ok=False, error="刷新间隔需在 3~600 秒之间")
        set_setting(conn, "friend_refresh_seconds", str(seconds))
    if "game_enabled" in data:
        enabled = 1 if str(data["game_enabled"]).lower() in ("1", "true", "on", "yes") else 0
        set_setting(conn, "game_enabled", str(enabled))
    if "tutorial_title" in data:
        title = (data.get("tutorial_title") or "").strip()[:50]
        set_setting(conn, "tutorial_title", title or "t-chat1.3 使用教程")
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="设置已保存")


@app.route("/admin/api/banned_words")
@admin_api_required
def admin_banned_words():
    conn = get_db()
    rows = conn.execute(
        "SELECT word, created_at FROM banned_words ORDER BY word").fetchall()
    conn.close()
    return jsonify(ok=True, words=[
        {"word": r["word"], "created_at": fmt_ts(r["created_at"])} for r in rows])


@app.route("/admin/api/banned_words/add", methods=["POST"])
@admin_api_required
def admin_add_banned_word():
    data = require_json()
    word = (data.get("word") or "").strip()
    if not word or len(word) > 50:
        return jsonify(ok=False, error="违禁词长度需在 1~50 之间")
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO banned_words(word, created_at) VALUES (?,?)",
                 (word, now_ts()))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已添加违禁词")


@app.route("/admin/api/banned_words/remove", methods=["POST"])
@admin_api_required
def admin_remove_banned_word():
    data = require_json()
    word = (data.get("word") or "").strip()
    conn = get_db()
    conn.execute("DELETE FROM banned_words WHERE word=?", (word,))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已删除违禁词")


@app.route("/admin/api/bans")
@admin_api_required
def admin_bans():
    conn = get_db()
    rows = conn.execute(
        "SELECT u.id, u.username, u.last_ip, u.reg_ip, u.is_admin, "
        "b.reason, b.banned_ip, b.created_at AS banned_at "
        "FROM users u LEFT JOIN bans_users b ON b.user_id=u.id ORDER BY u.id").fetchall()
    users = [{
        "id": r["id"], "username": r["username"],
        "last_ip": r["last_ip"] or "-", "reg_ip": r["reg_ip"] or "-",
        "is_admin": bool(r["is_admin"]), "banned": r["banned_at"] is not None,
        "reason": r["reason"] or "", "banned_ip": r["banned_ip"] or "",
        "banned_at": fmt_ts(r["banned_at"]) if r["banned_at"] else "",
    } for r in rows]
    ips = [{
        "ip": r["ip"], "reason": r["reason"] or "",
        "source_user_id": r["source_user_id"],
        "created_at": fmt_ts(r["created_at"]),
    } for r in conn.execute(
        "SELECT ip, reason, source_user_id, created_at FROM bans_ips "
        "ORDER BY created_at DESC").fetchall()]
    conn.close()
    return jsonify(ok=True, users=users, ips=ips)


@app.route("/admin/api/ban/user", methods=["POST"])
@admin_api_required
def admin_ban_user():
    data = require_json()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    reason = (data.get("reason") or "违反聊天规则").strip()[:100]
    ban_ip = bool(data.get("ban_ip", True))
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify(ok=False, error="用户不存在")
    if user["is_admin"]:
        conn.close()
        return jsonify(ok=False, error="管理员账号不能被封禁")
    ip = user["last_ip"] or user["reg_ip"] or ""
    if ip and is_local_ip(ip):
        ban_ip = False
    conn.execute(
        "INSERT OR REPLACE INTO bans_users(user_id, reason, banned_ip, created_at) "
        "VALUES (?,?,?,?)", (uid, reason, ip if ban_ip else "", now_ts()))
    if ban_ip and ip:
        conn.execute(
            "INSERT OR REPLACE INTO bans_ips(ip, reason, source_user_id, created_at) "
            "VALUES (?,?,?,?)", (ip, reason, uid, now_ts()))
    conn.commit()
    conn.close()
    emit_to_user(uid, "banned", {"reason": reason, "ip": ip if ban_ip else ""})
    socketio.emit("ban_updated", {"user_id": uid, "banned": True})
    return jsonify(ok=True, message="已封禁用户%s" % ("及其 IP" if ban_ip else ""))


@app.route("/admin/api/unban/user", methods=["POST"])
@admin_api_required
def admin_unban_user():
    data = require_json()
    try:
        uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="参数错误")
    unban_ip = bool(data.get("unban_ip", True))
    conn = get_db()
    conn.execute("DELETE FROM bans_users WHERE user_id=?", (uid,))
    if unban_ip:
        conn.execute("DELETE FROM bans_ips WHERE source_user_id=?", (uid,))
    conn.commit()
    conn.close()
    emit_to_user(uid, "unbanned", {})
    socketio.emit("ban_updated", {"user_id": uid, "banned": False})
    return jsonify(ok=True, message="已解除用户封禁")


@app.route("/admin/api/ban/ip", methods=["POST"])
@admin_api_required
def admin_ban_ip():
    data = require_json()
    ip = (data.get("ip") or "").strip()
    reason = (data.get("reason") or "违反聊天规则").strip()[:100]
    if not ip:
        return jsonify(ok=False, error="请输入 IP")
    if is_local_ip(ip):
        return jsonify(ok=False, error="不能封禁服务器本机 IP")
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO bans_ips(ip, reason, source_user_id, created_at) "
        "VALUES (?,?,NULL,?)", (ip, reason, now_ts()))
    uids = [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE last_ip=? OR reg_ip=?", (ip, ip)).fetchall()]
    conn.commit()
    conn.close()
    for uid in uids:
        emit_to_user(uid, "banned", {"reason": reason, "ip": ip})
    return jsonify(ok=True, message="已封禁 IP")


@app.route("/admin/api/unban/ip", methods=["POST"])
@admin_api_required
def admin_unban_ip():
    data = require_json()
    ip = (data.get("ip") or "").strip()
    conn = get_db()
    conn.execute("DELETE FROM bans_ips WHERE ip=?", (ip,))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已解除 IP 封禁")


@app.route("/admin/api/ip_applications")
@admin_api_required
def admin_ip_applications():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM ip_applications ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, "
        "id DESC LIMIT 200").fetchall()
    conn.close()
    data = [{
        "id": r["id"], "ip": r["ip"], "username": r["username"],
        "message": r["message"] or "", "status": r["status"],
        "created_at": fmt_ts(r["created_at"]),
    } for r in rows]
    return jsonify(ok=True, applications=data)


@app.route("/admin/api/ip_applications/<int:aid>/respond", methods=["POST"])
@admin_api_required
def admin_ip_application_respond(aid):
    data = require_json()
    action = (data.get("action") or "").lower()
    if action not in ("accept", "reject"):
        return jsonify(ok=False, error="操作无效")
    conn = get_db()
    app_row = conn.execute("SELECT * FROM ip_applications WHERE id=?", (aid,)).fetchone()
    if not app_row or app_row["status"] != "pending":
        conn.close()
        return jsonify(ok=False, error="申请不存在或已处理")
    if action == "reject":
        conn.execute("UPDATE ip_applications SET status='rejected', handled_at=? WHERE id=?",
                     (now_ts(), aid))
        conn.commit()
        conn.close()
        return jsonify(ok=True, message="已拒绝申请")
    if conn.execute("SELECT 1 FROM users WHERE username=?",
                    (app_row["username"],)).fetchone():
        conn.close()
        return jsonify(ok=False, error="用户名已被占用，无法创建")
    cur = conn.execute(
        "INSERT INTO users(username, password, avatar, theme, is_admin, last_ip, "
        "reg_ip, tutorial_seen, chat_bg, created_by_admin, created_at) "
        "VALUES (?,?,'','light',0,?,?,0,'',0,?)",
        (app_row["username"], app_row["password"], app_row["ip"], app_row["ip"], now_ts()))
    new_uid = cur.lastrowid
    # 给这个 IP 放宽 1 个名额，避免创建完立刻又被限制
    used = conn.execute("SELECT COUNT(*) c FROM users WHERE reg_ip=?",
                        (app_row["ip"],)).fetchone()["c"]
    current = conn.execute("SELECT max_accounts FROM ip_limits WHERE ip=?",
                           (app_row["ip"],)).fetchone()
    new_limit = max(used + 1, (current["max_accounts"] if current else 0) + 1)
    conn.execute("INSERT OR REPLACE INTO ip_limits(ip, max_accounts) VALUES (?,?)",
                 (app_row["ip"], new_limit))
    conn.execute("UPDATE ip_applications SET status='accepted', handled_at=? WHERE id=?",
                 (now_ts(), aid))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已批准并创建账号", user_id=new_uid)


@app.route("/admin/api/users/create", methods=["POST"])
@admin_api_required
def admin_create_user():
    """管理员创建小号：不受 IP 注册上限限制"""
    data = require_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not (2 <= len(username) <= 20):
        return jsonify(ok=False, error="用户名长度需在 2~20 之间")
    if len(password) < 4:
        return jsonify(ok=False, error="密码至少 4 位")
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users(username, password, avatar, theme, is_admin, last_ip, "
            "reg_ip, tutorial_seen, chat_bg, created_by_admin, created_at) "
            "VALUES (?,?,'','light',0,?,?,1,'',1,?)",
            (username, generate_password_hash(password), client_ip(), client_ip(), now_ts()))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify(ok=False, error="用户名已被占用")
    conn.close()
    return jsonify(ok=True, message="小号创建成功")


@app.route("/admin/api/tutorial/upload", methods=["POST"])
@admin_api_required
def admin_upload_tutorial():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(ok=False, error="请选择视频文件")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ("mp4", "webm", "ogg", "ogv", "mov", "m4v", "mkv"):
        return jsonify(ok=False, error="教程视频仅支持 mp4/webm/ogg/mov/mkv")
    fname = "tut_%s.%s" % (uuid.uuid4().hex, ext)
    path = os.path.join(TUTORIAL_DIR, fname)
    f.save(path)
    if os.path.getsize(path) > MAX_TUTORIAL_BYTES:
        os.remove(path)
        return jsonify(ok=False, error="视频不能超过 1GB")
    conn = get_db()
    old = get_setting(conn, "tutorial_video", "") or ""
    set_setting(conn, "tutorial_video", "tutorial/" + fname)
    conn.commit()
    conn.close()
    if old:
        old_path = os.path.join(FILES_DIR, old)
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
    return jsonify(ok=True, message="教程视频已上传",
                   url="/files/tutorial/" + fname,
                   path="tutorial/" + fname)


@app.route("/admin/api/tutorial/remove", methods=["POST"])
@admin_api_required
def admin_remove_tutorial():
    conn = get_db()
    old = get_setting(conn, "tutorial_video", "") or ""
    set_setting(conn, "tutorial_video", "")
    conn.commit()
    conn.close()
    if old:
        p = os.path.join(FILES_DIR, old)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    return jsonify(ok=True, message="已删除教程视频")


@app.route("/admin/api/ip_limits", methods=["GET"])
@admin_api_required
def admin_ip_limits():
    conn = get_db()
    rows = conn.execute("SELECT ip, max_accounts FROM ip_limits ORDER BY ip").fetchall()
    conn.close()
    return jsonify(ok=True, limits=[
        {"ip": r["ip"], "max_accounts": r["max_accounts"]} for r in rows])


@app.route("/admin/api/ip_limits", methods=["POST"])
@admin_api_required
def admin_set_ip_limit():
    data = require_json()
    ip = (data.get("ip") or "").strip()
    action = (data.get("action") or "set").lower()
    if not ip:
        return jsonify(ok=False, error="请输入 IP")
    conn = get_db()
    if action == "delete":
        conn.execute("DELETE FROM ip_limits WHERE ip=?", (ip,))
        conn.commit()
        conn.close()
        return jsonify(ok=True, message="已删除 IP 专用上限")
    try:
        limit = int(data.get("max_accounts"))
    except (TypeError, ValueError):
        conn.close()
        return jsonify(ok=False, error="上限必须是数字")
    if not (1 <= limit <= 100):
        conn.close()
        return jsonify(ok=False, error="上限需在 1~100 之间")
    conn.execute("INSERT OR REPLACE INTO ip_limits(ip, max_accounts) VALUES (?,?)",
                 (ip, limit))
    conn.commit()
    conn.close()
    return jsonify(ok=True, message="已保存 IP 专用上限")


# ---------------- 管理员数据接口 ----------------
@app.route("/admin/api/stats")
@admin_api_required
def admin_stats():
    conn = get_db()
    users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    friends = conn.execute("SELECT COUNT(*) c FROM friends").fetchone()["c"] // 2
    groups = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
    reqs = conn.execute(
        "SELECT COUNT(*) c FROM friend_requests WHERE status='pending'").fetchone()["c"]
    conn.close()
    mconn = get_mdb()
    msgs = mconn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    mconn.close()
    return jsonify(ok=True, users=users, messages=msgs, friends=friends,
                   groups=groups, pending_requests=reqs,
                   online=len(online_uids()))


@app.route("/admin/api/users")
@admin_api_required
def admin_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, avatar, theme, is_admin, last_ip, reg_ip, created_at "
        "FROM users ORDER BY id").fetchall()
    grp_counts = {r["user_id"]: r["c"] for r in conn.execute(
        "SELECT user_id, COUNT(*) c FROM group_members GROUP BY user_id").fetchall()}
    banned = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM bans_users").fetchall()}
    conn.close()
    mconn = get_mdb()
    msg_counts = {r["sender_id"]: r["c"] for r in mconn.execute(
        "SELECT sender_id, COUNT(*) c FROM messages GROUP BY sender_id").fetchall()}
    mconn.close()
    online = online_uids()
    data = [{
        "id": r["id"], "username": r["username"], "avatar": r["avatar"] or "",
        "theme": r["theme"] or "light", "is_admin": bool(r["is_admin"]),
        "last_ip": r["last_ip"] or "-", "reg_ip": r["reg_ip"] or "-",
        "created_at": fmt_ts(r["created_at"]),
        "online": r["id"] in online, "msg_count": msg_counts.get(r["id"], 0),
        "group_count": grp_counts.get(r["id"], 0),
        "banned": r["id"] in banned,
    } for r in rows]
    return jsonify(ok=True, users=data)


@app.route("/admin/api/groups")
@admin_api_required
def admin_groups():
    conn = get_db()
    rows = conn.execute(
        "SELECT g.id, g.name, g.owner_id, g.created_at, u.username AS owner "
        "FROM groups g LEFT JOIN users u ON u.id=g.owner_id ORDER BY g.id DESC").fetchall()
    data = []
    for r in rows:
        count = conn.execute("SELECT COUNT(*) c FROM group_members WHERE group_id=?",
                             (r["id"],)).fetchone()["c"]
        data.append({
            "id": r["id"], "name": r["name"], "owner": r["owner"] or "-",
            "member_count": count, "created_at": fmt_ts(r["created_at"]),
        })
    conn.close()
    return jsonify(ok=True, groups=data)


@app.route("/admin/api/messages")
@admin_api_required
def admin_messages():
    room = (request.args.get("room") or "").strip()
    try:
        limit = min(int(request.args.get("limit", 300)), 3000)
    except ValueError:
        limit = 300
    conn = get_db()
    group_names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM groups").fetchall()}
    conn.close()
    mconn = get_mdb()
    sql = """SELECT id, room, msg_type, content, created_at, sender_name AS sender
             FROM messages"""
    params = []
    if room:
        sql += " WHERE room = ?"
        params.append(room)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = mconn.execute(sql, params).fetchall()
    mconn.close()

    data = []
    for r in reversed(rows):
        raw = r["room"]
        if raw == "public":
            label = "公共聊天室"
        elif raw.startswith("priv:"):
            label = "私聊(" + "-".join(raw.split(":")[1:]) + ")"
        else:
            gid = group_room_of(raw)
            label = "群聊 · " + group_names.get(gid, "已解散") if gid else raw
        content = r["content"]
        if r["msg_type"] in ("file", "audio"):
            content = parse_file_content(content)
        data.append({
            "id": r["id"], "room": label, "raw_room": raw, "sender": r["sender"],
            "msg_type": r["msg_type"], "content": content,
            "created_at": fmt_ts(r["created_at"]),
        })
    return jsonify(ok=True, messages=data)


@app.route("/admin/export/users.csv")
@admin_api_required
def admin_export_users():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, avatar, theme, is_admin, last_ip, created_at "
        "FROM users ORDER BY id").fetchall()
    conn.close()
    buf = StringIO()
    buf.write("\ufeff")  # BOM，Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(["用户ID", "用户名", "是否管理员", "头像", "主题", "最近IP", "注册时间"])
    for r in rows:
        w.writerow([r["id"], r["username"], "是" if r["is_admin"] else "否",
                    r["avatar"] or "-", r["theme"] or "light",
                    r["last_ip"] or "-", fmt_ts(r["created_at"])])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=users_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
    return resp


@app.route("/admin/export/messages.csv")
@admin_api_required
def admin_export_messages():
    conn = get_db()
    group_names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM groups").fetchall()}
    conn.close()
    mconn = get_mdb()
    rows = mconn.execute(
        """SELECT id, room, msg_type, content, created_at, sender_name AS sender
           FROM messages ORDER BY id""").fetchall()
    mconn.close()
    buf = StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["消息ID", "房间", "发送者", "类型", "内容/文件", "时间"])
    for r in rows:
        room = r["room"]
        if room == "public":
            room = "公共聊天室"
        elif room.startswith("priv:"):
            room = "私聊(" + "-".join(room.split(":")[1:]) + ")"
        else:
            gid = group_room_of(room)
            if gid:
                room = "群聊 · " + group_names.get(gid, "已解散")
        content = r["content"]
        if r["msg_type"] in ("file", "audio"):
            info = parse_file_content(content)
            content = "%s (%s)" % (info.get("name", ""), info.get("url", ""))
        w.writerow([r["id"], room, r["sender"], r["msg_type"], content,
                    fmt_ts(r["created_at"])])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=messages_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
    return resp


@app.route("/admin/export/all.json")
@admin_api_required
def admin_export_all():
    conn = get_db()
    users = [dict(r) for r in conn.execute(
        "SELECT id, username, avatar, theme, is_admin, last_ip, reg_ip, "
        "tutorial_seen, chat_bg, created_at FROM users ORDER BY id").fetchall()]
    friends = [dict(r) for r in conn.execute(
        "SELECT user_id, friend_id, created_at FROM friends").fetchall()]
    requests = [dict(r) for r in conn.execute(
        "SELECT id, from_id, to_id, status, message, created_at "
        "FROM friend_requests").fetchall()]
    groups = [dict(r) for r in conn.execute(
        "SELECT id, name, owner_id, created_at FROM groups").fetchall()]
    members = [dict(r) for r in conn.execute(
        "SELECT group_id, user_id, role, joined_at FROM group_members").fetchall()]
    bans = [dict(r) for r in conn.execute("SELECT * FROM bans_users").fetchall()]
    ban_ips = [dict(r) for r in conn.execute("SELECT * FROM bans_ips").fetchall()]
    conn.close()
    mconn = get_mdb()
    messages = [dict(r) for r in mconn.execute(
        "SELECT id, room, sender_id, sender_name, sender_avatar, sender_is_admin, "
        "msg_type, content, created_at FROM messages ORDER BY id").fetchall()]
    mconn.close()
    payload = {
        "app": "t-chat", "version": VERSION,
        "exported_at": fmt_ts(now_ts()),
        "users": users, "friends": friends, "friend_requests": requests,
        "groups": groups, "group_members": members, "messages": messages,
        "bans_users": bans, "bans_ips": ban_ips,
    }
    resp = Response(json.dumps(payload, ensure_ascii=False, indent=2),
                    mimetype="application/json")
    resp.headers["Content-Disposition"] = \
        "attachment; filename=chat_backup_%s.json" % time.strftime("%Y%m%d_%H%M%S")
    return resp


# ================================================================ 小游戏：PICO PARK 风格合作闯关
# 关卡数据同时给前端渲染 / 物理碰撞，服务端只用 door / keys 判定通关
GAME_LEVELS = [
    {
        "name": "第一关 · 初次合作",
        "hint": "收集所有钥匙，然后所有玩家一起站进门里！",
        "width": 2000, "height": 900,
        "spawn": {"x": 120, "y": 740},
        "bg": ["#8ed6ff", "#d9f3ff"],
        "platforms": [
            [0, 800, 2000, 100, "ground"],
            [280, 700, 240, 24, "wood"],
            [600, 610, 200, 24, "wood"],
            [900, 700, 240, 24, "wood"],
            [1200, 610, 200, 24, "wood"],
            [1500, 700, 260, 24, "wood"],
            [1820, 600, 180, 24, "wood"],
        ],
        "spikes": [[820, 782, 80, 18]],
        "keys": [[400, 655], [700, 565], [1300, 565]],
        "door": [1840, 480, 150, 120],
    },
    {
        "name": "第二关 · 跨越深坑",
        "hint": "小心深坑。跳不上去时，可以踩在队友头上再跳！",
        "width": 2400, "height": 900,
        "spawn": {"x": 140, "y": 740},
        "bg": ["#ffd6a5", "#fff3d6"],
        "platforms": [
            [0, 800, 520, 100, "ground"],
            [760, 800, 540, 100, "ground"],
            [1500, 800, 900, 100, "ground"],
            [300, 690, 200, 22, "wood"],
            [520, 700, 240, 22, "wood"],
            [820, 690, 180, 22, "wood"],
            [1060, 600, 200, 22, "wood"],
            [1360, 700, 220, 22, "wood"],
            [1750, 690, 200, 22, "wood"],
            [2050, 600, 200, 22, "wood"],
        ],
        "spikes": [[520, 855, 240, 25], [1300, 855, 200, 25]],
        "keys": [[390, 645], [1120, 555], [2150, 555]],
        "door": [2100, 480, 150, 120],
    },
    {
        "name": "第三关 · 垂直攀登",
        "hint": "一层一层往上跳，跳不上去就踩队友的头。",
        "width": 1600, "height": 1100,
        "spawn": {"x": 120, "y": 930},
        "bg": ["#c3f0ca", "#eefbf0"],
        "platforms": [
            [0, 1000, 1600, 100, "ground"],
            [260, 890, 240, 22, "wood"],
            [560, 780, 240, 22, "wood"],
            [860, 680, 240, 22, "wood"],
            [1160, 580, 240, 22, "wood"],
            [900, 480, 240, 22, "wood"],
            [600, 380, 240, 22, "wood"],
            [300, 280, 260, 22, "wood"],
        ],
        "spikes": [[700, 982, 160, 18]],
        "keys": [[360, 845], [660, 735], [1020, 635]],
        "door": [330, 160, 150, 120],
    },
    {
        "name": "第四关 · 齐心协力",
        "hint": "最后一关！四把钥匙，一个都不能少。",
        "width": 2000, "height": 900,
        "spawn": {"x": 120, "y": 740},
        "bg": ["#d8c4ff", "#f3edff"],
        "platforms": [
            [0, 800, 700, 100, "ground"],
            [900, 800, 1100, 100, "ground"],
            [400, 690, 220, 22, "wood"],
            [700, 610, 200, 22, "wood"],
            [1000, 680, 220, 22, "wood"],
            [1300, 600, 200, 22, "wood"],
            [1600, 680, 180, 22, "wood"],
            [1810, 570, 190, 22, "wood"],
        ],
        "spikes": [[700, 855, 200, 22]],
        "keys": [[500, 645], [790, 565], [1110, 635], [1400, 555]],
        "door": [1830, 450, 150, 120],
    },
    {
        "name": "第五关 · 交错平台",
        "hint": "平台更窄了，注意跳跃节奏，跳不上去就踩队友的头。",
        "width": 2200, "height": 900,
        "spawn": {"x": 120, "y": 740},
        "bg": ["#ffd6a5", "#ffe8c2"],
        "platforms": [
            [0, 800, 700, 100, "ground"],
            [900, 800, 1300, 100, "ground"],
            [300, 700, 200, 22, "wood"],
            [560, 620, 180, 22, "wood"],
            [820, 700, 200, 22, "wood"],
            [1200, 600, 200, 22, "wood"],
            [1450, 500, 180, 22, "wood"],
            [1700, 600, 180, 22, "wood"],
            [1950, 500, 180, 22, "wood"],
        ],
        "spikes": [[700, 855, 200, 22]],
        "keys": [[400, 655], [1300, 555], [1540, 455], [2040, 455]],
        "door": [1980, 380, 150, 120],
    },
    {
        "name": "第六关 · 连锁高塔",
        "hint": "一路往上爬，最后要向左跳，别掉下去。",
        "width": 1800, "height": 1100,
        "spawn": {"x": 120, "y": 930},
        "bg": ["#c3f0ca", "#eefbf0"],
        "platforms": [
            [0, 1000, 1800, 100, "ground"],
            [200, 890, 200, 22, "wood"],
            [430, 780, 200, 22, "wood"],
            [660, 670, 200, 22, "wood"],
            [890, 560, 200, 22, "wood"],
            [1120, 450, 200, 22, "wood"],
            [850, 340, 200, 22, "wood"],
            [580, 230, 200, 22, "wood"],
        ],
        "spikes": [[800, 982, 200, 18]],
        "keys": [[300, 845], [530, 735], [960, 515], [680, 185]],
        "door": [620, 110, 150, 120],
    },
    {
        "name": "第七关 · 深坑窄桥",
        "hint": "下面是整片尖刺，只能踩着窄桥走，掉下去就回起点。",
        "width": 2600, "height": 900,
        "spawn": {"x": 120, "y": 740},
        "bg": ["#d8c4ff", "#f3edff"],
        "platforms": [
            [0, 800, 500, 100, "ground"],
            [600, 760, 140, 22, "wood"],
            [830, 680, 140, 22, "wood"],
            [1060, 600, 140, 22, "wood"],
            [1290, 680, 140, 22, "wood"],
            [1520, 760, 140, 22, "wood"],
            [1750, 680, 140, 22, "wood"],
            [1980, 600, 140, 22, "wood"],
            [2210, 680, 200, 22, "wood"],
        ],
        "spikes": [[500, 855, 2000, 25]],
        "keys": [[670, 715], [900, 635], [1130, 555], [1820, 635], [2050, 555]],
        "door": [2240, 560, 150, 120],
    },
    {
        "name": "第八关 · 最终合作",
        "hint": "最后一关！六把钥匙，平台很窄，记得互相搭人梯。",
        "width": 2800, "height": 1000,
        "spawn": {"x": 120, "y": 840},
        "bg": ["#8ed6ff", "#dff4ff"],
        "platforms": [
            [0, 900, 600, 100, "ground"],
            [800, 900, 600, 100, "ground"],
            [1600, 900, 1200, 100, "ground"],
            [300, 790, 200, 22, "wood"],
            [620, 700, 220, 22, "wood"],
            [880, 610, 200, 22, "wood"],
            [1150, 700, 220, 22, "wood"],
            [1380, 610, 200, 22, "wood"],
            [1620, 700, 220, 22, "wood"],
            [1900, 600, 200, 22, "wood"],
            [2150, 500, 200, 22, "wood"],
            [2400, 600, 200, 22, "wood"],
            [2650, 500, 180, 22, "wood"],
        ],
        "spikes": [[600, 955, 200, 20], [1400, 955, 200, 20]],
        "keys": [[400, 745], [700, 655], [1230, 655], [1460, 565],
                 [2240, 455], [2720, 455]],
        "door": [2680, 380, 150, 120],
    },
]


def _game_room_name(code):
    return "game:" + code


def _game_public_room_list():
    rooms = []
    for code, room in game_rooms.items():
        if not room["players"] or room.get("private"):
            continue
        rooms.append({
            "code": code,
            "count": len(room["players"]),
            "max": GAME_MAX_PLAYERS,
            "state": room["state"],
            "level": room["level"] + 1,
            "level_name": GAME_LEVELS[room["level"]]["name"] if room["level"] < len(GAME_LEVELS) else "",
            "host": room["host_name"],
            "private": False,
        })
    rooms.sort(key=lambda r: (-r["count"], r["code"]))
    return rooms[:20]


def _game_players_snapshot(room):
    players = []
    host = room.get("host")
    for uid, p in room["players"].items():
        players.append({
            "uid": uid, "name": p["name"], "avatar": p["avatar"],
            "color": p["color"], "x": round(p["x"], 1), "y": round(p["y"], 1),
            "vx": round(p.get("vx", 0), 1), "vy": round(p.get("vy", 0), 1),
            "facing": p.get("facing", 1), "anim": p.get("anim", "idle"),
            "host": uid == host, "spawn": p.get("spawn", 0),
        })
    return players


def _game_spawn_for(level, slot):
    sp = level["spawn"]
    return {"x": sp["x"] + (slot % 4) * 46, "y": sp["y"] - (slot // 4) * 54}


def _game_reset_positions(room):
    level = GAME_LEVELS[room["level"]]
    for slot, (uid, p) in enumerate(room["players"].items()):
        pos = _game_spawn_for(level, slot)
        p["x"], p["y"] = pos["x"], pos["y"]
        p["vx"], p["vy"] = 0, 0
        p["spawn"] = slot
        p["anim"], p["facing"] = "idle", 1
    room["keys"] = set()
    room["goal_since"] = None
    room["advancing"] = False


def _game_emit_state(code):
    room = game_rooms.get(code)
    if not room:
        return
    socketio.emit("game_players", {
        "code": code, "players": _game_players_snapshot(room),
        "level": room["level"], "state": room["state"],
        "keys": sorted(room["keys"]), "host": room.get("host"),
        "private": bool(room.get("private")),
        "started_at": room.get("started_at"),
    }, to=_game_room_name(code))


def _game_broadcast_room(code):
    room = game_rooms.get(code)
    if not room:
        return
    socketio.emit("game_room", {
        "code": code, "players": _game_players_snapshot(room),
        "host": room.get("host"), "state": room["state"],
        "level": room["level"], "keys": sorted(room["keys"]),
        "private": bool(room.get("private")),
        "max": GAME_MAX_PLAYERS,
    }, to=_game_room_name(code))


def _game_loop(code):
    """每个房间 15Hz 广播位置 + 判定全体到达终点"""
    while True:
        socketio.sleep(1.0 / 15)
        room = game_rooms.get(code)
        if not room:
            return
        with game_lock:
            players = list(room["players"].values())
            if not players:
                if room.get("empty_since") and time.time() - room["empty_since"] > 120:
                    game_rooms.pop(code, None)
                    return
                continue
            room["empty_since"] = None
            if room["state"] == "playing":
                level = GAME_LEVELS[room["level"]]
                if len(room["keys"]) >= len(level["keys"]):
                    d = level["door"]
                    # 所有玩家到门口即可；允许站在队友头上，8 人也不会因为门太小而卡住
                    inside = all(
                        d[0] - 10 <= p["x"] <= d[0] + d[2] + 10 and
                        d[1] - 220 <= p["y"] <= d[1] + d[3] + 10
                        for p in players)
                    if inside:
                        if not room.get("goal_since"):
                            room["goal_since"] = time.time()
                        elif (time.time() - room["goal_since"] > 0.9
                              and not room.get("advancing")):
                            room["advancing"] = True
                            room["state"] = "cleared"
                            socketio.emit("game_level_clear", {
                                "code": code, "level": room["level"],
                                "name": level["name"],
                            }, to=_game_room_name(code))
                            socketio.start_background_task(_game_next_level, code)
                    else:
                        room["goal_since"] = None
        _game_emit_state(code)


def _game_next_level(code):
    socketio.sleep(3.0)
    room = game_rooms.get(code)
    if not room:
        return
    with game_lock:
        if room["state"] != "cleared":
            return
        next_level = room["level"] + 1
        if next_level >= len(GAME_LEVELS):
            room["state"] = "won"
            socketio.emit("game_won", {
                "code": code, "total_time": round(time.time() - (room.get("started_at") or time.time()), 1),
            }, to=_game_room_name(code))
            _game_broadcast_room(code)
            return
        room["level"] = next_level
        room["state"] = "playing"
        _game_reset_positions(room)
        level = GAME_LEVELS[next_level]
        socketio.emit("game_level", {
            "code": code, "level": next_level, "state": "playing",
            "name": level["name"],
            "spawns": {uid: {"x": p["x"], "y": p["y"]} for uid, p in room["players"].items()},
        }, to=_game_room_name(code))
    _game_emit_state(code)


def _game_leave(uid, sid):
    """把用户从小游戏房间移除（换房间 / 断开时调用）"""
    touched = []
    for code, room in list(game_rooms.items()):
        if uid in room["players"]:
            with game_lock:
                room["players"].pop(uid, None)
                if room.get("host") == uid:
                    room["host"] = next(iter(room["players"]), None)
                    room["host_name"] = (room["players"][room["host"]]["name"]
                                         if room["host"] else "")
                if not room["players"]:
                    room["empty_since"] = time.time()
                touched.append(code)
    for code in touched:
        try:
            leave_room(_game_room_name(code), sid=sid)
        except Exception:
            pass
        _game_broadcast_room(code)
    return touched


@app.route("/api/game/rooms")
@login_required_api
def api_game_rooms():
    if not game_enabled():
        return jsonify(ok=False, error="小游戏功能已被管理员关闭"), 403
    return jsonify(ok=True, rooms=_game_public_room_list(),
                   max=GAME_MAX_PLAYERS, levels=len(GAME_LEVELS))


@app.route("/api/game/levels")
@login_required_api
def api_game_levels():
    if not game_enabled():
        return jsonify(ok=False, error="小游戏功能已被管理员关闭"), 403
    return jsonify(ok=True, levels=GAME_LEVELS, max=GAME_MAX_PLAYERS)


@socketio.on("game_join")
def on_game_join(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    if not game_enabled():
        socketio.emit("game_error", {"error": "小游戏功能已被管理员关闭"}, to=request.sid)
        return
    code = str(data.get("code") or "").strip()
    if not re.match(r"^\d{4}$", code):
        socketio.emit("game_error", {"error": "房间号必须是 4 位数字"}, to=request.sid)
        return

    _game_leave(uid, request.sid)
    want_private = bool(data.get("private"))

    with game_lock:
        room = game_rooms.get(code)
        is_new_room = room is None
        if room is None:
            room = {
                "players": {}, "host": None, "host_name": "",
                "state": "lobby", "level": 0, "keys": set(),
                "goal_since": None, "advancing": False,
                "created_at": time.time(), "empty_since": None,
                "started_at": None, "loop_started": False,
                "private": want_private, "invited": set(), "kicked": set(),
            }
            game_rooms[code] = room
        # 空房间：允许新玩家重新创建/转换公开或私人，避免残留的私人房把所有人挡在外面
        if not is_new_room and not room["players"]:
            room["private"] = want_private
            room["host"] = None
            room["host_name"] = ""
            room["invited"] = set()
            room["kicked"] = set()
            room["state"] = "lobby"
            room["level"] = 0
            room["keys"] = set()
            room["started_at"] = None
            room["goal_since"] = None
            room["advancing"] = False
            is_new_room = True
        if uid in room.get("kicked", set()):
            socketio.emit("game_error", {"error": "你已被房主移出该房间"}, to=request.sid)
            return
        if (not is_new_room and room.get("private") and uid != room.get("host")
                and uid not in room.get("invited", set())):
            socketio.emit("game_error",
                          {"error": "这是私人房间，需要房主邀请才能加入"}, to=request.sid)
            return
        if uid not in room["players"] and len(room["players"]) >= GAME_MAX_PLAYERS:
            socketio.emit("game_error",
                          {"error": "房间已满（最多 %d 人）" % GAME_MAX_PLAYERS},
                          to=request.sid)
            return
        user = current_user()
        if not user:
            return
        slot = len(room["players"])
        color = GAME_COLORS[slot % len(GAME_COLORS)]
        level = GAME_LEVELS[room["level"]]
        pos = _game_spawn_for(level, slot)
        room["players"][uid] = {
            "name": user["username"], "avatar": user["avatar"] or "",
            "color": color, "x": pos["x"], "y": pos["y"],
            "vx": 0, "vy": 0, "facing": 1, "anim": "idle", "spawn": slot,
        }
        if not room["host"] or room["host"] not in room["players"]:
            room["host"] = uid
            room["host_name"] = user["username"]

    join_room(_game_room_name(code))
    with game_lock:
        start_loop = not room.get("loop_started")
        if start_loop:
            room["loop_started"] = True
    socketio.emit("game_joined", {
        "code": code, "uid": uid, "host": room["host"],
        "state": room["state"], "level": room["level"],
        "keys": sorted(room["keys"]), "private": bool(room.get("private")),
        "players": _game_players_snapshot(room),
        "max": GAME_MAX_PLAYERS,
    }, to=request.sid)
    _game_broadcast_room(code)
    if start_loop:
        socketio.start_background_task(_game_loop, code)


@socketio.on("game_leave")
def on_game_leave(data):
    uid = session.get("user_id")
    if not uid:
        return
    _game_leave(uid, request.sid)
    socketio.emit("game_left", {}, to=request.sid)


@socketio.on("share_game")
def on_share_game(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room or uid not in room["players"]:
        socketio.emit("game_error", {"error": "你不在该游戏房间中"}, to=request.sid)
        return
    target_type = (data.get("target_type") or "").lower()
    try:
        target_id = int(data.get("target_id"))
    except (TypeError, ValueError):
        socketio.emit("game_error", {"error": "请选择分享对象"}, to=request.sid)
        return
    content = json.dumps({
        "code": code,
        "private": bool(room.get("private")),
        "host": room.get("host_name", ""),
    }, ensure_ascii=False)
    room.setdefault("invited", set())
    if target_type == "user":
        if target_id == uid:
            socketio.emit("game_error", {"error": "不能分享给自己"}, to=request.sid)
            return
        if not are_friends(uid, target_id):
            socketio.emit("game_error", {"error": "对方不是你的好友"}, to=request.sid)
            return
        room["invited"].add(target_id)
        mid = _store_message(private_room(uid, target_id), uid, "game_invite", content)
        payload = _message_payload(
            mid, private_room(uid, target_id), uid, session.get("username", ""),
            current_user()["avatar"] if current_user() else "",
            current_user()["is_admin"] if current_user() else 0,
            "game_invite", content, now_ts(), target_id)
        emit_to_user(target_id, "new_message", payload)
        emit_to_user(uid, "new_message", payload)
    elif target_type == "group":
        if not is_group_member(target_id, uid):
            socketio.emit("game_error", {"error": "你不在该群聊中"}, to=request.sid)
            return
        for member in group_member_ids(target_id):
            room["invited"].add(member)
        mid = _store_message(group_room(target_id), uid, "game_invite", content)
        payload = _message_payload(
            mid, group_room(target_id), uid, session.get("username", ""),
            current_user()["avatar"] if current_user() else "",
            current_user()["is_admin"] if current_user() else 0,
            "game_invite", content, now_ts())
        socketio.emit("new_message", payload, to=group_room(target_id))
    else:
        socketio.emit("game_error", {"error": "分享类型错误"}, to=request.sid)
        return
    socketio.emit("game_share_sent", {"target_type": target_type}, to=request.sid)


@socketio.on("game_kick")
def on_game_kick(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room:
        return
    if room.get("host") != uid:
        socketio.emit("game_error", {"error": "只有房主可以踢人"}, to=request.sid)
        return
    try:
        target = int(data.get("uid"))
    except (TypeError, ValueError):
        return
    if target == uid or target not in room["players"]:
        return
    with game_lock:
        room["players"].pop(target, None)
        room.setdefault("kicked", set()).add(target)
    leave_user_room(target, _game_room_name(code))
    emit_to_user(target, "game_kicked", {"code": code, "reason": "你已被房主移出房间"})
    _game_broadcast_room(code)


@socketio.on("game_start")
def on_game_start(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room:
        socketio.emit("game_error", {"error": "房间不存在"}, to=request.sid)
        return
    if room.get("host") != uid:
        socketio.emit("game_error", {"error": "只有房主可以开始游戏"}, to=request.sid)
        return
    if not room["players"]:
        return
    with game_lock:
        room["level"] = 0
        room["state"] = "playing"
        room["started_at"] = time.time()
        _game_reset_positions(room)
        spawns = {u: {"x": p["x"], "y": p["y"]} for u, p in room["players"].items()}
    socketio.emit("game_started", {
        "code": code, "level": 0, "state": "playing",
        "name": GAME_LEVELS[0]["name"], "spawns": spawns,
    }, to=_game_room_name(code))
    _game_emit_state(code)


@socketio.on("game_restart")
def on_game_restart(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room or room.get("host") != uid:
        return
    with game_lock:
        room["level"] = 0
        room["state"] = "playing"
        room["started_at"] = time.time()
        _game_reset_positions(room)
        spawns = {u: {"x": p["x"], "y": p["y"]} for u, p in room["players"].items()}
    socketio.emit("game_started", {
        "code": code, "level": 0, "state": "playing",
        "name": GAME_LEVELS[0]["name"], "spawns": spawns,
    }, to=_game_room_name(code))
    _game_emit_state(code)


@socketio.on("game_state")
def on_game_state(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room or uid not in room["players"]:
        return
    level = GAME_LEVELS[room["level"]]
    p = room["players"][uid]
    try:
        p["x"] = max(0, min(level["width"], float(data.get("x", p["x"]))))
        p["y"] = max(0, min(level["height"], float(data.get("y", p["y"]))))
        p["vx"] = float(data.get("vx", 0))
        p["vy"] = float(data.get("vy", 0))
        p["facing"] = 1 if float(data.get("facing", 1)) >= 0 else -1
        anim = str(data.get("anim", "idle"))
        p["anim"] = anim if anim in ("idle", "run", "jump", "fall") else "idle"
    except (TypeError, ValueError):
        return


@socketio.on("game_key")
def on_game_key(data):
    uid = session.get("user_id")
    if not uid or not isinstance(data, dict):
        return
    code = str(data.get("code") or "")
    room = game_rooms.get(code)
    if not room or uid not in room["players"] or room["state"] != "playing":
        return
    try:
        key = int(data.get("key"))
    except (TypeError, ValueError):
        return
    level = GAME_LEVELS[room["level"]]
    if not (0 <= key < len(level["keys"])):
        return
    if key in room["keys"]:
        return
    p = room["players"][uid]
    kx, ky = level["keys"][key]
    if abs(p["x"] - kx) > 140 or abs(p["y"] - ky) > 140:
        return
    room["keys"].add(key)
    socketio.emit("game_key_collected", {"key": key, "uid": uid,
                                         "keys": sorted(room["keys"])},
                  to=_game_room_name(code))
    if len(room["keys"]) >= len(level["keys"]):
        socketio.emit("game_keys_complete", {}, to=_game_room_name(code))


# ================================================================ 启动
# 注意：放在模块级，保证用 gunicorn / waitress 等 WSGI 服务器
# （server:app）启动时也会建库、建表和初始化管理员账号。
init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print("=" * 60)
    print("t-chat %s 已启动（制作：汤at帅）" % VERSION)
    print("B站主页: https://space.bilibili.com/3546392266017069")
    print("监听地址: http://%s:%d" % (host, port))
    print("管理后台: http://127.0.0.1:%d/admin" % port)
    print("上传上限: %d MB" % MAX_UPLOAD_MB)
    print("=" * 60)
    # Flask-SocketIO 5.3+ 才支持 allow_unsafe_werkzeug 参数；
    # 旧版本（如 1.1 使用的 5.1.1）不能传，否则会多启动一次，这里先检测。
    run_kwargs = {}
    try:
        if "allow_unsafe_werkzeug" in inspect.signature(socketio.run).parameters:
            run_kwargs["allow_unsafe_werkzeug"] = True
    except (TypeError, ValueError):
        pass
    socketio.run(app, host=host, port=port, **run_kwargs)
