"""SQLite 存储：节目、文稿、用户、令牌与各类设置（单文件数据库，路径由 TNEWS_DB 决定）。"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id    TEXT,
    program_name  TEXT,
    title         TEXT,
    created_at    TEXT,
    window_start  TEXT,
    window_end    TEXT,
    item_count    INTEGER,
    duration      REAL,
    audio_file    TEXT,
    script        TEXT,
    user_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at DESC);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,            -- 实际是登录名（历史列名，账号或邮箱都行）
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',   -- admin / user
    disabled      INTEGER NOT NULL DEFAULT 0,
    uid           TEXT,                           -- 6 位数字用户 ID（外部调用用）
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
"""


# 老版本数据库缺少的列。升级镜像时旧 data/tnews.db 会被直接沿用，
# CREATE TABLE IF NOT EXISTS 不会补列，所以启动时按需 ALTER TABLE。
MIGRATIONS = {
    "episodes": {
        "program_name": "TEXT",
        "window_start": "TEXT",
        "window_end": "TEXT",
        "item_count": "INTEGER",
        "duration": "REAL",
        "user_id": "INTEGER",
    },
    "users": {
        "uid": "TEXT",
    },
}


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """补齐老库里缺失的列，让新版本可以直接跑在旧数据上。"""
        for table, columns in MIGRATIONS.items():
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # 老用户没有 6 位 ID 的，按 用户名+注册时间 补齐
        rows = conn.execute(
            "SELECT id, email, created_at FROM users WHERE uid IS NULL OR uid = ''"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE users SET uid = ? WHERE id = ?",
                (self._new_uid(str(r["email"]), str(r["created_at"] or ""), conn), r["id"]),
            )
        self._resync_titles(conn)
        self._migrate_feed_settings(conn)

    def _migrate_feed_settings(self, conn: sqlite3.Connection) -> None:
        """播客订阅有独立的开关与令牌（feed_enabled / feed_token）。

        升级时把老的外部订阅（sub_enabled / sub_token）复制一份过去，
        这样老版本已经发出去的 /feed/<令牌>.xml 地址不会因为升级而失效；
        之后两者各关各的，互不影响。
        """
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'u%:sub_token' OR key LIKE 'u%:sub_enabled'"
        ).fetchall()
        for r in rows:
            key = str(r["key"])
            uid_part, _, name = key.partition(":")
            new_key = f"{uid_part}:{'feed_token' if name == 'sub_token' else 'feed_enabled'}"
            if conn.execute("SELECT 1 FROM settings WHERE key = ?", (new_key,)).fetchone():
                continue
            conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (new_key, r["value"]))
        # 总开关同理：升级时沿用「允许外部订阅」的现状，管理员之后可以单独关掉播客
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'allow_podcast'").fetchone():
            row = conn.execute("SELECT value FROM settings WHERE key = 'allow_subscribe'").fetchone()
            if row:
                conn.execute("INSERT INTO settings(key, value) VALUES('allow_podcast', ?)", (row["value"],))

    def _resync_titles(self, conn: sqlite3.Connection) -> None:
        """期数口径统一：标题里的「第 N 期」按「同档节目 + 同一归属账号」重写，
        各账号独立计数，修复旧版按当天人数算期数导致角标与标题对不上的历史数据。"""
        seq_re = re.compile(r"\s*[·・]\s*第\s*\d+\s*期\s*$")
        rows = conn.execute(
            "SELECT id, program_id, user_id, title, script FROM episodes WHERE program_id IS NOT NULL ORDER BY id"
        ).fetchall()
        seqs: dict[tuple, int] = {}
        for r in rows:
            key = (str(r["program_id"]), r["user_id"])
            seqs[key] = seqs.get(key, 0) + 1
            seq = seqs[key]
            old_title = str(r["title"] or "")
            base = seq_re.sub("", old_title).rstrip()
            new_title = f"{base} · 第 {seq} 期" if base else old_title
            if new_title == old_title:
                continue
            conn.execute("UPDATE episodes SET title = ? WHERE id = ?", (new_title, r["id"]))
            try:
                script = json.loads(r["script"]) if r["script"] else {}
                if script.get("title"):
                    script["title"] = new_title
                    conn.execute(
                        "UPDATE episodes SET script = ? WHERE id = ?",
                        (json.dumps(script, ensure_ascii=False), r["id"]),
                    )
            except Exception:
                pass

    def resync_titles(self) -> None:
        """外部再触发一次标题期数重算（节目合并后需要，因为 seq 的键变了）。"""
        with self._conn() as conn:
            self._resync_titles(conn)

    def reassign_program(self, valid_ids: list[str], target_id: str, target_name: str) -> int:
        """把 program_id 已不在配置里的往期归并到 target_id（节目合并成一个通称时用）。

        返回被归并的期数；期数由 resync_titles 之后统一重排。
        """
        if not target_id:
            return 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, program_id, program_name, title, script FROM episodes WHERE program_id IS NOT NULL"
            ).fetchall()
            moved = 0
            for r in rows:
                if str(r["program_id"]) in valid_ids:
                    continue
                old_name = str(r["program_name"] or "")
                title = str(r["title"] or "")
                new_title = title
                # 标题开头如果是旧节目名，一并换成新的通称
                if old_name and title.startswith(old_name):
                    new_title = target_name + title[len(old_name):]
                conn.execute(
                    "UPDATE episodes SET program_id = ?, program_name = ?, title = ? WHERE id = ?",
                    (target_id, target_name, new_title, r["id"]),
                )
                if new_title != title:
                    try:
                        script = json.loads(r["script"]) if r["script"] else {}
                        if script.get("title"):
                            script["title"] = new_title
                            script["program_name"] = target_name
                            conn.execute(
                                "UPDATE episodes SET script = ? WHERE id = ?",
                                (json.dumps(script, ensure_ascii=False), r["id"]),
                            )
                    except Exception:
                        pass
                moved += 1
            if moved:
                self._resync_titles(conn)
        return moved

    def rename_program_name(self, target_id: str, name: str) -> int:
        """把某档节目的往期记录里的节目名同步成配置里的新名字（标题前缀一起换）。

        合并节目后，旧节目的往期标题开头还写着旧名字（如「热点快报 · …」），
        这里按 script 里留存的原始节目名把前缀改掉。
        """
        changed = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, program_name, title, script FROM episodes WHERE program_id = ?",
                (target_id,),
            ).fetchall()
            for r in rows:
                try:
                    script = json.loads(r["script"]) if r["script"] else {}
                except Exception:
                    script = {}
                old_name = str(script.get("program_name") or r["program_name"] or "")
                title = str(r["title"] or "")
                new_title = title
                if old_name and old_name != name and title.startswith(old_name):
                    new_title = name + title[len(old_name):]
                script_dirty = bool(script) and (
                    script.get("program_name") != name or script.get("program_id") != target_id
                    or (new_title != title and script.get("title") != new_title)
                )
                if old_name == name and new_title == title and not script_dirty:
                    continue
                conn.execute(
                    "UPDATE episodes SET program_name = ?, title = ? WHERE id = ?",
                    (name, new_title, r["id"]),
                )
                if script_dirty:
                    if new_title != title and script.get("title"):
                        script["title"] = new_title
                    script["program_name"] = name
                    script["program_id"] = target_id
                    conn.execute(
                        "UPDATE episodes SET script = ? WHERE id = ?",
                        (json.dumps(script, ensure_ascii=False), r["id"]),
                    )
                changed += 1
        return changed

    @staticmethod
    def _new_uid(email: str, created_at: str, conn: sqlite3.Connection) -> str:
        """6 位数字用户 ID：由 用户名+注册时间戳+随机 数生成，保证全局不重复。"""
        taken = {r["uid"] for r in conn.execute(
            "SELECT uid FROM users WHERE uid IS NOT NULL AND uid != ''"
        )}
        for _ in range(64):
            seed = f"{email}|{created_at}|{random.getrandbits(64)}".encode("utf-8")
            digest = hashlib.sha256(seed).hexdigest()
            uid = str(int(digest[:12], 16) % 1_000_000).zfill(6)
            if uid not in taken:
                return uid
        return "".join(random.choices("0123456789", k=6))  # 理论上到不了这里

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, script: dict, audio_file: str, duration: float, chapters: list[dict],
             user_id: int | None = None) -> int:
        payload = dict(script)
        payload["chapters"] = chapters
        payload["duration"] = duration
        payload["audio_file"] = audio_file
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO episodes
                   (program_id, program_name, title, created_at, window_start, window_end,
                    item_count, duration, audio_file, script, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    script.get("program_id"),
                    script.get("program_name"),
                    script.get("title"),
                    script.get("created_at"),
                    script.get("window_start"),
                    script.get("window_end"),
                    script.get("item_count", 0),
                    duration,
                    audio_file,
                    json.dumps(payload, ensure_ascii=False),
                    user_id,
                ),
            )
            return int(cur.lastrowid)

    def list(self, limit: int = 30, program_id: str | None = None,
             user_id: int | None = None, all_users: bool = False) -> list[dict]:
        """user_id=None 且 all_users=False：老语义（全部）；指定 user_id 只看该用户。

        seq = 该期在「同档节目 + 同一归属账号」里的期数（第 1 期 = 1），
        各账号独立计数，与标题里的「第 N 期」一致。
        """
        # is_run：这期是否由「跑步模式」生成（script 里有 run 段）。列表里据此显示 🏃 标签。
        # 用 instr 做纯字符串判断（不依赖 SQLite 的 JSON1 扩展，老环境也安全）；
        # run_km 只对确实是跑步的那几期回表取 script 解析，普通期不多读一字节。
        sql = ("SELECT id, program_id, program_name, title, created_at, item_count, duration, audio_file, seq, "
               "is_run "
               "FROM (SELECT id, program_id, program_name, title, created_at, item_count, duration, audio_file, "
               "user_id, CASE WHEN instr(COALESCE(script, ''), '\"run\":') > 0 THEN 1 ELSE 0 END AS is_run, "
               "ROW_NUMBER() OVER (PARTITION BY program_id, user_id ORDER BY id) AS seq FROM episodes) e")
        conds, params = [], []
        if program_id:
            conds.append("program_id = ?")
            params.append(program_id)
        if not all_users and user_id is not None:
            conds.append("user_id = ?")
            params.append(user_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = [dict(r) for r in rows]
        run_ids = [r["id"] for r in out if r.get("is_run")]
        if run_ids:
            q = "SELECT id, script FROM episodes WHERE id IN (%s)" % ",".join("?" * len(run_ids))
            km_of = {}
            with self._conn() as conn:
                for r in conn.execute(q, run_ids).fetchall():
                    try:
                        km = (json.loads(r["script"] or "{}").get("run") or {}).get("km")
                    except Exception:
                        km = None
                    km_of[r["id"]] = km
            for r in out:
                r["run_km"] = km_of.get(r["id"])
        return out

    def get(self, episode_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["script"] = json.loads(data["script"]) if data["script"] else {}
        return data

    def latest(self, program_id: str | None = None,
               user_id: int | None = None, all_users: bool = False) -> dict | None:
        items = self.list(limit=1, program_id=program_id, user_id=user_id, all_users=all_users)
        return self.get(items[0]["id"]) if items else None

    def seq_of(self, episode_id: int, program_id: str | None,
               user_id: int | None = None, all_users: bool = False) -> int | None:
        """某一期的期数：按「同档节目 + 同一归属账号」计数，第 1 期 = 1。

        按节目归属者（user_id）算，不按观看者算，所以所有视角看到的数字一样。
        user_id / all_users 参数保留只为兼容旧调用。
        """
        if not episode_id or not program_id:
            return None
        sql = ("SELECT COUNT(*) AS n FROM episodes e "
               "WHERE e.program_id = ? AND e.id <= ? "
               "AND e.user_id IS (SELECT user_id FROM episodes WHERE id = ?)")
        with self._conn() as conn:
            row = conn.execute(sql, (program_id, episode_id, episode_id)).fetchone()
        return int(row["n"]) if row else None

    def count_program_episodes(self, program_id: str, user_id: int | None = None) -> int:
        """这档节目已有的总期数；给 user_id 时只数该账号自己的（生成标题期数用）。"""
        sql = "SELECT COUNT(*) AS n FROM episodes WHERE program_id = ?"
        params: list = [program_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def delete(self, episode_id: int) -> str | None:
        ep = self.get(episode_id)
        if not ep:
            return None
        with self._conn() as conn:
            conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        return ep.get("audio_file")

    def purge_older_than(self, cutoff_iso: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, audio_file FROM episodes WHERE created_at < ?", (cutoff_iso,)
            ).fetchall()
            conn.execute("DELETE FROM episodes WHERE created_at < ?", (cutoff_iso,))
        return [r["audio_file"] for r in rows if r["audio_file"]]

    def purge_all(self) -> list[str]:
        """清空全部往期节目，返回音频文件名列表。"""
        with self._conn() as conn:
            rows = conn.execute("SELECT audio_file FROM episodes WHERE audio_file IS NOT NULL").fetchall()
            conn.execute("DELETE FROM episodes")
        return [r["audio_file"] for r in rows]

    def purge_user(self, user_id: int) -> list[str]:
        """删除某个用户的全部节目（注销用户时用）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT audio_file FROM episodes WHERE user_id = ? AND audio_file IS NOT NULL",
                (user_id,),
            ).fetchall()
            conn.execute("DELETE FROM episodes WHERE user_id = ?", (user_id,))
        return [r["audio_file"] for r in rows]

    # ------------------------ 设置（键值） ------------------------
    def get_setting(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_setting(self, key: str, value) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, payload),
            )

    def rename_program(self, old_name: str, new_name: str) -> int:
        """历史节目改名（节目重命名后，往期标题也跟着改，避免新旧叫法混在一起）。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE episodes SET program_name = ?, title = REPLACE(title, ?, ?) "
                "WHERE program_name = ? OR title LIKE ?",
                (new_name, old_name, new_name, old_name, f"%{old_name}%"),
            )
            changed = cur.rowcount or 0
            # 文稿 JSON 里也存了节目名，一并替换
            rows = conn.execute(
                "SELECT id, script FROM episodes WHERE script LIKE ?", (f"%{old_name}%",)
            ).fetchall()
            for r in rows:
                conn.execute(
                    "UPDATE episodes SET script = ? WHERE id = ?",
                    (r["script"].replace(old_name, new_name), r["id"]),
                )
        return changed or len(rows)

    # ------------------------ 用户与令牌 ------------------------
    def create_user(self, email: str, password_hash: str, role: str = "user") -> dict:
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._conn() as conn:
            uid = self._new_uid(email, created_at, conn)
            cur = conn.execute(
                "INSERT INTO users(email, password_hash, role, disabled, uid, created_at) "
                "VALUES(?,?,?,0,?,?)",
                (email, password_hash, role, uid, created_at),
            )
        return self.get_user(int(cur.lastrowid))  # type: ignore[arg-type]

    def get_user(self, user_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None

    def get_user_by_uid(self, uid: str) -> dict | None:
        uid = str(uid or "").strip()
        if not uid:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE uid = ?", (uid,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT u.*, (SELECT COUNT(*) FROM episodes e WHERE e.user_id = u.id) AS episodes "
                "FROM users u ORDER BY u.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_user(self, user_id: int, **fields) -> None:
        if not fields:
            return
        sets, params = [], []
        for k, v in fields.items():
            if k in ("email", "password_hash", "role", "disabled"):
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:          # 只传了不认识的字段时不要拼出空 SET（会报 SQL 语法错）
            return
        params.append(user_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)

    def delete_user(self, user_id: int) -> list[str]:
        """删除用户及其节目、令牌，返回要清理的音频文件。"""
        audio = self.purge_user(user_id)
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return audio

    def count_users(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    def create_token(self, token: str, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tokens(token, user_id, created_at) VALUES(?,?,?)",
                (token, user_id, datetime.now().astimezone().isoformat(timespec="seconds")),
            )

    def delete_token(self, token: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE token = ?", (token,))

    def user_for_token(self, token: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token = ? AND u.disabled = 0",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def assign_legacy_episodes(self, user_id: int) -> int:
        """升级前没有用户概念的旧节目，全部归属管理员。"""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE episodes SET user_id = ? WHERE user_id IS NULL", (user_id,)
            )
            return cur.rowcount or 0

    # ------------------------ 用户级设置（自定义源等） ------------------------
    @staticmethod
    def _ukey(user_id: int | None, key: str) -> str:
        return f"u{user_id if user_id is not None else 0}:{key}"

    def get_user_setting(self, user_id: int | None, key: str, default=None):
        return self.get_setting(self._ukey(user_id, key), default)

    def set_user_setting(self, user_id: int | None, key: str, value) -> None:
        self.set_setting(self._ukey(user_id, key), value)

    # ------------------------ 外部订阅 / 播客订阅令牌 ------------------------
    def ensure_token(self, user_id: int, key: str) -> str:
        """取该账号某个用途的令牌，没有就生成一个（sub_token → /s/，feed_token → /feed/）。

        关闭开关不清令牌：重新打开时地址不变，已经发出去的链接还能用。
        """
        tok = str(self.get_user_setting(user_id, key) or "").strip()
        if not tok:
            tok = secrets.token_urlsafe(18)
            self.set_user_setting(user_id, key, tok)
        return tok

    def ensure_sub_token(self, user_id: int) -> str:
        """外部订阅（网页只读收听）令牌，地址形如 /s/<token>。"""
        return self.ensure_token(user_id, "sub_token")

    def ensure_feed_token(self, user_id: int) -> str:
        """播客订阅（RSS）令牌，地址形如 /feed/<token>.xml。与网页订阅各用一把钥匙。"""
        return self.ensure_token(user_id, "feed_token")

    def ensure_ext_token(self, user_id: int) -> str:
        """外部调用（HA / Node-RED 拉最新一期）令牌，?ext=<token>。

        与外部订阅、播客订阅同一套思路：地址即钥匙，不再需要「账号名 + 6 位 ID」。
        """
        return self.ensure_token(user_id, "ext_token")

    def _user_by_setting_token(self, key: str, token: str) -> dict | None:
        t = str(token or "").strip()
        if not t or len(t) > 80:
            return None
        for u in self.list_users():
            if str(self.get_user_setting(int(u["id"]), key) or "") == t:
                return u
        return None

    def user_by_sub_token(self, token: str) -> dict | None:
        """按订阅令牌反查账号（订阅链接只读访问时用）。"""
        return self._user_by_setting_token("sub_token", token)

    def user_by_feed_token(self, token: str) -> dict | None:
        """按播客 subscribe 令牌反查账号（/feed/<token>.xml 用）。"""
        return self._user_by_setting_token("feed_token", token)

    def user_by_ext_token(self, token: str) -> dict | None:
        """按外部调用令牌反查账号（/api/latest?ext= 等外部系统调用用）。"""
        return self._user_by_setting_token("ext_token", token)

    def custom_feeds(self, user_id: int | None) -> list[dict]:
        """该用户添加的自定义源；管理员兼容读取升级前的全局 custom_feeds。"""
        val = self.get_user_setting(user_id, "custom_feeds")
        if val is None and (user_id is None or user_id == 1):
            # 1.3.x 及之前存在全局键里，管理员沿用
            val = self.get_setting("custom_feeds")
        return [f for f in (val or []) if isinstance(f, dict) and f.get("url")]

    def set_custom_feeds(self, user_id: int | None, feeds: list[dict]) -> None:
        self.set_user_setting(user_id, "custom_feeds", feeds)
