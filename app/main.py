"""FastAPI 应用：节目 API + 网页播放器 + 播客订阅源 + 定时任务。"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, pipeline, summarizer, weather
from .auth import hash_password, new_token, random_password, verify_password
from .config import (AUDIO_DIR, BASE_DIR, CST, DATA_DIR, WEB_DIR, load_config, now_local,
                     resolve_hhmm_window)
from .pipeline import GenOptions
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("tnews")

VERSION = __version__  # 版本号只改 app/__init__.py；部署后打开 /health 就能确认跑的是不是新版
cfg = load_config()
app = FastAPI(title=cfg.app_name, version=VERSION)


@app.middleware("http")
async def _no_store_api(request: Request, call_next):
    """所有 /api 响应都按当前账号计算：禁止浏览器或反代缓存，换账号后不会读到上一位的旧结果。"""
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Vary"] = "Authorization"
    return resp

store = Store(Path(os.environ.get("TNEWS_DB", os.environ.get("NEWSRADIO_DB", BASE_DIR / "data" / "tnews.db"))))
scheduler = AsyncIOScheduler(timezone=CST)

STATE: dict[str, dict] = {}  # program_id -> {status, error, episode_id, started_at}



def _check_account(name: str) -> str:
    """账号规则：2~30 个字符，不含空格（兼容老用户的邮箱格式账号）。"""
    name = name.strip().lower()
    if len(name) < 2 or len(name) > 30:
        raise HTTPException(400, "账号需为 2~30 个字符")
    if re.search(r"\s", name):
        raise HTTPException(400, "账号不能包含空格")
    return name


# ---------------------------- 账号与鉴权 ----------------------------
def _token_of(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-token") or "").strip()


def _sub_token_of(request: Request) -> str:
    """订阅令牌：?sub=xxx 或请求头 X-TNews-Sub（订阅页面里前端会自动带上）。"""
    return (request.query_params.get("sub") or request.headers.get("x-tnews-sub") or "").strip()


def _allow_subscribe() -> bool:
    """外部订阅总开关（管理员在「设置 → 账号与数据」里控制）。"""
    return bool(store.get_setting("allow_subscribe", False))


def _allow_podcast() -> bool:
    """播客订阅总开关（与网页订阅分开，管理员单独控制）。"""
    return bool(store.get_setting("allow_podcast", False))


def _sub_user(token: str) -> dict | None:
    """校验订阅令牌：总开关 → 令牌有效 → 账号未禁用 → 该账号自己开着订阅。"""
    if not _allow_subscribe():
        return None
    u = store.user_by_sub_token(token)
    if not u or u.get("disabled"):
        return None
    if not store.get_user_setting(int(u["id"]), "sub_enabled", False):
        return None
    u = dict(u)
    u["_sub"] = True
    return u


def current_user(request: Request) -> dict | None:
    """请求方身份：None = 访客；否则 {id, email, role, ...}。

    订阅链接（/s/<token> 页面里所有请求会带 ?sub=<token>）解析成一个「只读身份」：
    返回该账号的资料但打上 _sub 标记，写操作在 require_user 里统一拒绝，
    读接口因此自动按该账号的节目范围工作，效果与访客听管理员节目一致。

    订阅身份优先于登录令牌：已经登录的 A 打开别人（B）的订阅地址时，
    即使浏览器顺手带上了 A 的令牌，也按 B 的只读身份处理——所有写操作都无权，
    否则会出现「拿别人的订阅链接当自己的登录态用」这种越权。
    """
    sub = _sub_token_of(request)
    if sub:
        u = _sub_user(sub)
        if u:
            return u
        # 订阅令牌存在但无效（被关/被重置）：不给登录态兜底，一律按访客处理
        if _token_of(request):
            return None
    token = _token_of(request)
    if token:
        return store.user_for_token(token)
    return None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "请先登录后再操作")
    if user.get("_sub"):
        raise HTTPException(403, "订阅链接只能收听，不能生成或修改设置")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "该操作仅管理员可用")
    return user


def admin_id() -> int | None:
    """主管理员（role=admin 的第一个账号，即播种的 admin）。"""
    for u in store.list_users():
        if u["role"] == "admin":
            return int(u["id"])
    return None


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "uid": u.get("uid") or "", "email": u["email"],
            "role": u["role"], "created_at": u.get("created_at")}


# 访客收听被关闭时用一个不存在的 user_id：所有按归属过滤的查询都查不到节目
_GUEST_NONE = -1
# 「设置 → 全局管理 → 首页访客指向」存哪个账号（存 6 位用户 ID；空 = 主管理员）
GUEST_ACCOUNT_KEY = "guest_account"


def _as_bool(v, default: bool = True) -> bool:
    """配置里写 true/false 也行，写 "true"/"false"/"on"/"off"/1/0 也行。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是", "开")


def _user_by_key(key: str) -> dict | None:
    """按「登录名 / 邮箱 / 6 位用户 ID / 数字 id」找账号。"""
    k = (key or "").strip()
    if not k:
        return None
    u = _account_by_name(k)
    if u:
        return u
    if k.isdigit():
        return store.get_user_by_uid(k) or store.get_user(int(k))
    return None


def _guest_home() -> dict:
    """首页访客（未登录、没有任何令牌）能看到谁的内容。

    指向的账号按这个顺序决定：
      ① 网页「设置 → 全局管理 → 首页访客指向」里选的账号（存 settings.guest_account，默认 = 主管理员）
      ② config.yaml 的 guest.account（登录名 / 邮箱 / 6 位用户 ID）
      ③ 主管理员（role=admin 的第一个账号）

    该账号必须在「我的账号 → 速听分享」里开启（sub_enabled），否则同样按「无内容」处理。
    config.yaml 里 guest.enabled=false 则访客一律看不到内容。
    老配置既没写 guest 段、网页里也没选过：保持旧行为，访客始终能听管理员的节目。
    """
    raw = cfg.guest if isinstance(cfg.guest, dict) else {}
    saved = str(store.get_setting(GUEST_ACCOUNT_KEY, "") or "").strip()
    key = ""
    if saved:
        u = _user_by_key(saved)
        key = (u or {}).get("email") or saved
    else:
        key = str(raw.get("account") or "").strip()
        u = _user_by_key(key) if key else None

    # 既没在网页里选过、config 也没写 guest 段 = 老配置：不做分享开关校验
    if not saved and not isinstance(cfg.guest, dict):
        return {"allowed": True, "user_id": admin_id(), "account": "", "reason": "", "message": ""}

    if not saved and not key:
        aid = admin_id()
        u = store.get_user(aid) if aid else None
        key = (u or {}).get("email") or ""

    if not _as_bool(raw.get("enabled"), True):
        return {"allowed": False, "user_id": None, "account": (u or {}).get("email") or key,
                "reason": "off",
                "message": "首页访客收听已关闭（config.yaml 的 guest.enabled = false），请登录后收听"}
    if not u:
        return {"allowed": False, "user_id": None, "account": key, "reason": "no-account",
                "message": "首页访客指向的账号不存在，请在「设置 → 全局管理 → 首页访客指向」里重新选择"}
    if u.get("disabled"):
        return {"allowed": False, "user_id": None, "account": u.get("email") or "", "reason": "disabled",
                "message": f"账号 {u.get('email')} 已被禁用，访客暂时没有可听的内容，请登录后收听"}
    if not store.get_user_setting(int(u["id"]), "sub_enabled", False):
        return {"allowed": False, "user_id": None, "account": u.get("email") or "", "reason": "share-off",
                "message": (f"账号 {u.get('email')} 还没在「我的账号 → 速听分享」里开启，"
                            "访客暂时听不到内容，请登录后收听")}
    return {"allowed": True, "user_id": int(u["id"]), "account": u.get("email") or "",
            "reason": "", "message": ""}


def _visible_filter(user: dict | None):
    """节目可见范围：访客看「分享速听」那个账号（默认管理员）；普通用户看自己的。"""
    if user is None:
        g = _guest_home()
        uid = int(g["user_id"]) if (g["allowed"] and g["user_id"]) else _GUEST_NONE
        return {"user_id": uid, "all_users": False, "blocked": not g["allowed"]}
    return {"user_id": int(user["id"]), "all_users": False, "blocked": False}


class AuthReq(BaseModel):
    # 字段名沿用 email（前端与老接口都这么发），实际接受任意登录名/邮箱
    email: str
    password: str


class GenerateReq(BaseModel):
    program_id: str
    categories: list[str] | None = None
    feeds: list[str] | None = None  # 只抓这些源；留空 = 用分类下全部源
    window_start: str | None = None  # "08:00"
    window_end: str | None = None  # "18:00"
    lookback_hours: float | None = None  # 最近 N 小时（优先于上面两个）
    rate: str | None = None  # "+10%"
    max_items: int | None = None
    target_minutes: float | None = None  # 目标时长（分钟），按它估算条数
    sort: str | None = None  # category=按分类 / date=按时间 / source=按平台
    voice: str | None = None  # 本期音色（Edge 神经语音名）
    shares: dict[str, int] | None = None  # 源 url → 占比%（1~100）；空/缺 = 不参考
    run_km: float | None = None       # 跑步模式：距离（公里）
    run_minutes: float | None = None  # 跑步模式：预计用时（分钟）；两者都给则按用时生成


# 页面上可选的音色（男 / 女声）
VOICES: dict[str, str] = {
    "zh-CN-YunxiNeural": "云希 · 男声",
    "zh-CN-YunjianNeural": "云健 · 男声",
    "zh-CN-XiaoxiaoNeural": "晓晓 · 女声",
    "zh-CN-XiaoyiNeural": "晓伊 · 女声",
}


class AutoReq(BaseModel):
    enabled: bool
    interval_hours: float | None = None
    start_hhmm: str | None = None  # 间隔起始锚点，如 "12:15"
    program_id: str | None = None
    categories: list[str] | None = None
    feeds: list[str] | None = None
    rate: str | None = None
    target_minutes: float | None = None  # 每期目标时长（分钟）
    sort: str | None = None  # 播报顺序
    voice: str | None = None  # 每期音色
    shares: dict[str, int] | None = None  # 源占比
    reschedule: bool = True  # False = 只更新参数，不重置下次执行时间（手动生成后同步用）


async def _notify_episode(ep: dict) -> None:
    """生成完成后可选的 webhook 推送（Home Assistant 等外部系统用）。

    在 config.yaml 的 home_assistant.webhook_url 填上地址即开启，失败只记日志、不影响生成。
    """
    url = str((cfg.home_assistant or {}).get("webhook_url") or "").strip()
    if not url:
        return
    # webhook 里带的音频地址要能被 HA 直接播放 → 优先用 home_assistant.base_url，其次 public_base_url
    base = str((cfg.home_assistant or {}).get("base_url") or "").strip().rstrip("/") or cfg.public_base_url
    audio = f"/audio/{ep.get('audio_file')}"
    payload = {
        "event": "episode_ready",
        "id": ep.get("id"),
        "title": ep.get("title"),
        "program_id": ep.get("program_id"),
        "created_at": ep.get("created_at"),
        "duration": ep.get("duration"),
        "item_count": ep.get("item_count"),
        "audio_file": ep.get("audio_file"),
        "url": f"{base}{audio}" if base else audio,
        "latest_mp3": f"{base}/latest.mp3" if base else "/latest.mp3",
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(url, json=payload)
        log.info("已推送 webhook：HTTP %s（%s）", resp.status_code, url)
    except Exception as e:   # 外部系统不可用时不影响本期节目
        log.warning("webhook 推送失败（%s）：%s", url, e)


async def _run_generation(program_id: str, opts: GenOptions | None = None):
    program = cfg.get_program(program_id)
    if not program:
        return
    STATE[program_id] = {"status": "running", "started_at": now_local().isoformat(timespec="seconds")}
    try:
        ep = await pipeline.generate_episode(program, cfg, store, opts=opts)
        STATE[program_id] = {
            "status": "done",
            "episode_id": ep.get("id"),
            "finished_at": now_local().isoformat(timespec="seconds"),
        }
        await _notify_episode(ep)
    except Exception as e:
        log.exception("生成失败: %s", program_id)
        STATE[program_id] = {
            "status": "error",
            "error": str(e),
            "finished_at": now_local().isoformat(timespec="seconds"),
        }


def _admin_gen_shares(program_id: str) -> dict[str, int] | None:
    """读管理员账号云同步的生成设置（nr_gen_<节目>），取「源占比」给定点定时用。

    前端每次改设置都会把 nr_gen_* 快照同步到 u<uid>:prefs（/api/me/prefs），
    所以定时生成不用管理员重新开启自动更新也能拿到最新的占比。
    """
    admin = next(
        (u for u in store.list_users()
         if u.get("role") == "admin" and not u.get("disabled")),
        None,
    )
    if not admin:
        return None
    prefs = store.get_setting(f"u{int(admin['id'])}:prefs") or {}
    prefs = prefs.get("prefs") if isinstance(prefs, dict) and "prefs" in prefs else prefs
    raw = (prefs or {}).get(f"nr_gen_{program_id}")
    try:
        saved = json.loads(raw) if isinstance(raw, str) else None
    except (ValueError, TypeError):
        saved = None
    if not isinstance(saved, dict) or not isinstance(saved.get("shares"), dict):
        return None
    clean: dict[str, int] = {}
    for u, p in (saved.get("shares") or {}).items():
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if 0 < p <= 100:
            clean[str(u)] = p
    return clean or None


def _scheduled_job(program_id: str):
    async def job():
        # 定点定时也遵循管理员保存的「源占比」：否则定时出来的期永远不按占比，
        # 用户在界面上设了占比会觉得「没生效」
        await _run_generation(program_id, GenOptions(shares=_admin_gen_shares(program_id)))

    return job


# ---------------------------- 自动更新（每个用户各自的每 N 小时任务） ----------------------------
AUTO_INTERVALS = [1, 2, 3, 6, 12, 24]
AUTO_DEFAULTS: dict = {
    "enabled": False,
    "interval_hours": 6.0,
    "start_hhmm": "00:00",  # 间隔的起始锚点：如 12:15 + 每 2 小时 → 14:15 / 16:15 …
    "program_id": "",
    "categories": None,
    "feeds": None,
    "shares": None,
    "rate": None,
    "target_minutes": None,
    "sort": None,
    "voice": None,
}


def auto_setting_key(uid: int) -> str:
    return f"u{int(uid)}:auto_update"


def auto_job_id(uid: int) -> str:
    return f"auto-u{int(uid)}"


def _get_auto(uid: int) -> dict:
    """合并三处来源：代码默认值 < config.yaml < 该用户保存到数据库的值。"""
    data = dict(AUTO_DEFAULTS)
    for k, v in (cfg.auto_update or {}).items():
        if k in AUTO_DEFAULTS and v is not None:
            data[k] = v
    saved = store.get_setting(auto_setting_key(uid))
    if isinstance(saved, dict):
        data.update({k: v for k, v in saved.items() if k in AUTO_DEFAULTS})

    data["enabled"] = bool(data.get("enabled"))
    try:
        data["interval_hours"] = float(data.get("interval_hours") or 6)
    except (TypeError, ValueError):
        data["interval_hours"] = 6.0
    data["interval_hours"] = min(168.0, max(0.5, data["interval_hours"]))
    hhmm = str(data.get("start_hhmm") or "00:00")
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        hhmm = "00:00"
    else:
        hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
    data["start_hhmm"] = hhmm
    if not data.get("program_id") or not cfg.get_program(str(data["program_id"])):
        data["program_id"] = cfg.programs[0].id if cfg.programs else ""
    return data


def _auto_state(uid: int) -> dict:
    data = _get_auto(uid)
    job = scheduler.get_job(auto_job_id(uid))
    next_run = getattr(job, "next_run_time", None) if job else None
    data["next_run"] = next_run.isoformat(timespec="seconds") if next_run else None
    data["intervals"] = AUTO_INTERVALS
    return data


async def _run_auto(uid: int) -> None:
    """某个用户自己的自动更新任务：用该用户保存的参数生成，节目归该用户。"""
    u = store.get_user(uid)
    if not u or u.get("disabled"):
        if scheduler.get_job(auto_job_id(uid)):
            scheduler.remove_job(auto_job_id(uid))
        return
    data = _get_auto(uid)
    if not data["enabled"]:
        return
    program = cfg.get_program(str(data["program_id"]))
    if not program:
        return
    if STATE.get(program.id, {}).get("status") == "running":
        log.info("自动更新跳过（%s）：%s 还在生成中", u["email"], program.name)
        return

    hours = float(data["interval_hours"])
    opts = GenOptions(
        categories=data.get("categories") or None,
        feeds=data.get("feeds") or None,
        shares=data.get("shares") or None,
        rate=data.get("rate") or None,
        target_minutes=data.get("target_minutes") or None,
        sort=data.get("sort") or None,
        voice=data.get("voice") or None,
        user_id=uid,  # 各用户的自动更新生成的节目归各自账号（管理员的那份访客可见）
        lookback_hours=hours,  # 按间隔回溯，既覆盖刚过去的这段时间，又不会和上一期大量重叠
    )
    log.info("自动更新触发（%s）：每 %.1f 小时一期，抓取最近 %.1f 小时", u["email"], hours, hours)
    await _run_generation(program.id, opts)


def _apply_auto_job(uid: int) -> None:
    """按某用户当前设置重建其间隔任务；关闭时只移除任务，不影响 config.yaml 里的定点定时。"""
    jid = auto_job_id(uid)
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
    data = _get_auto(uid)
    if not data["enabled"] or not data["program_id"]:
        log.info("自动更新（u%s）：未开启", uid)
        return
    hours = float(data["interval_hours"])
    # 锚定到 start_hhmm：12:15 起每 2 小时 → 14:15 / 16:15 / …，而不是「从现在起算」
    hh, mm = (int(x) for x in str(data.get("start_hhmm") or "00:00").split(":"))
    now = now_local()
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    step_min = max(30, int(round(hours * 60)))
    delta_min = (now - base).total_seconds() / 60
    if delta_min < 0:
        next_dt = base
    else:
        k = int(delta_min // step_min) + 1
        next_dt = base + timedelta(minutes=k * step_min)
    scheduler.add_job(
        _run_auto,
        IntervalTrigger(hours=hours, start_date=next_dt.replace(tzinfo=CST), timezone=CST),
        args=[uid],
        id=jid,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    log.info(
        "自动更新（u%s）：已开启，每 %.1f 小时一期，锚点 %s，下次 %s",
        uid, hours, data.get("start_hhmm"), next_dt.strftime("%H:%M"),
    )


def _remove_auto_job(uid: int) -> None:
    jid = auto_job_id(uid)
    if scheduler.get_job(jid):
        scheduler.remove_job(jid)
        log.info("自动更新（u%s）：任务已移除", uid)


def _sync_auto_jobs() -> None:
    """启动时为所有开启了自动更新的用户排程。"""
    for u in store.list_users():
        try:
            if u.get("disabled"):
                continue
            if _get_auto(int(u["id"]))["enabled"]:
                _apply_auto_job(int(u["id"]))
        except Exception as e:  # 单个用户出错不影响其他用户
            log.warning("排程自动更新（u%s）失败：%s", u.get("id"), e)


def _migrate_global_auto() -> None:
    """老版本把自动更新存成全局设置，这里一次性迁到管理员名下（幂等）。"""
    aid = admin_id()
    if not aid:
        return
    saved = store.get_setting("auto_update")
    if isinstance(saved, dict) and store.get_setting(auto_setting_key(aid)) is None:
        store.set_setting(auto_setting_key(aid), saved)
        log.info("已把旧的全局自动更新设置迁移到管理员账号名下")


@app.on_event("startup")
async def startup():
    # 首次启动播种管理员账号；老数据库里没有用户概念的旧节目也归到管理员名下
    if store.count_users() == 0:
        # 密码来源：环境变量 > 随机生成（不写死默认密码，避免装完忘了改）
        env_pw = os.environ.get("TNEWS_ADMIN_PASSWORD") or os.environ.get("NEWSRADIO_ADMIN_PASSWORD")
        pw = env_pw or random_password()
        store.create_user("admin", hash_password(pw), role="admin")
        log.warning("=" * 60)
        log.warning("已创建管理员账号：admin / %s", pw)
        if not env_pw:
            log.warning("（密码是随机生成的，只在本次首启打印一次）")
            pw_file = DATA_DIR / "INITIAL_ADMIN_PASSWORD.txt"
            try:
                pw_file.write_text(
                    "TNews 初始管理员密码\n账号：admin\n密码：%s\n\n"
                    "登录后请到「设置 → 用户管理」里改掉，然后删掉本文件。\n" % pw,
                    encoding="utf-8",
                )
                log.warning("已同时写入 %s", pw_file)
            except OSError as exc:  # 只读数据目录时不至于启动失败
                log.warning("初始密码写入文件失败（%s），请直接记下上面的密码", exc)
        log.warning("请尽快登录，在「设置 → 用户管理」里修改密码")
        log.warning("=" * 60)
    aid = admin_id()
    if aid:
        moved = store.assign_legacy_episodes(aid)
        if moved:
            log.info("已把 %d 期升级前的旧节目归属到管理员名下", moved)

    # 节目合并成一个通称：配置里已不存在的 program_id（例如旧的 evening/热点快报）
    # 把往期并入第一档节目，期数按时间顺序重排，网页里不再出现第二个节目名
    if cfg.programs:
        first = cfg.programs[0]
        merged = store.reassign_program([p.id for p in cfg.programs], first.id, first.name)
        if merged:
            log.info("已把 %d 期旧节目的往期并入「%s」", merged, first.name)
        store.rename_program_name(first.id, first.name)

    # 节目改名：把往期记录里的旧叫法同步过来（幂等，改名后第一次启动生效）
    for old, new in (("早间通勤", "要闻速听"), ("晚间通勤", "要闻速听"),
                     ("要闻速览", "要闻速听"), ("热点快报", "要闻速听")):
        if store.rename_program(old, new):
            log.info("往期节目名已更新：%s → %s", old, new)

    for program in cfg.programs:
        for cron in program.cron_list:
            scheduler.add_job(
                _scheduled_job(program.id),
                CronTrigger.from_crontab(cron, timezone=CST),
                id=f"gen-{program.id}-{cron}",
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
            log.info("已排程 %s (%s)", program.name, cron)

    scheduler.add_job(
        lambda: pipeline.cleanup(cfg, store),
        CronTrigger(hour=4, minute=10, timezone=CST),
        id="cleanup",
        replace_existing=True,
    )
    _migrate_global_auto()
    _sync_auto_jobs()
    scheduler.start()


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------- 页面 ----------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/s/{token}", response_class=HTMLResponse)
async def subscribe_page(token: str):
    """外部订阅页：和首页同一份前端，只是前端会把 ?sub=<token> 带到每个请求上。

    令牌无效时也照样返回页面，由前端调 /api/meta 拿到错误后给出提示
    （直接把 404 甩给访客，会看到一行冷冰冰的 JSON）。
    """
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/manifest.webmanifest")
async def manifest():
    return {
        "name": cfg.app_name,
        "short_name": cfg.app_name,
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f1115",
        "theme_color": "#0f1115",
        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }


# ---------------------------- API ----------------------------
@app.post("/api/auth/register")
async def auth_register(req: AuthReq):
    account = _check_account(req.email)
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if len(req.password) > 72:
        raise HTTPException(400, "密码太长（72 位以内）")
    if store.get_user_by_email(account):
        raise HTTPException(400, "这个账号已经注册过了")
    user = store.create_user(account, hash_password(req.password))
    token = new_token()
    store.create_token(token, int(user["id"]))
    log.info("新用户注册：%s", account)
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/login")
async def auth_login(req: AuthReq):
    account = req.email.strip().lower()
    user = store.get_user_by_email(account)
    if user is None and account == "admin":
        user = store.get_user_by_email("admin")  # 管理员账号本身就叫 admin
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "账号或密码不正确")
    if user["disabled"]:
        raise HTTPException(403, "该账号已被禁用，请联系管理员")
    token = new_token()
    store.create_token(token, int(user["id"]))
    return {"token": token, "user": _public_user(user)}


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = _token_of(request)
    if token:
        store.delete_token(token)
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = current_user(request)
    return {"user": _public_user(user) if user else None}


def _custom_feeds(user: dict | None) -> list[dict]:
    if user is None:
        return store.custom_feeds(admin_id())
    return store.custom_feeds(int(user["id"]))


# ---------------- 版本更新检查（管理员） ----------------
_VER_CACHE: dict = {"at": 0.0, "payload": None}
_VER_TTL = 600      # 成功结果缓存 10 分钟：设置面板来回打开不会反复打 GitHub
_VER_TTL_ERR = 120  # 失败结果缓存 2 分钟：网络不好时也别把 GitHub 打爆


def _repo_slug() -> str | None:
    """从 repo_url 里取 owner/repo。不是 GitHub 地址就返回 None（不检查）。"""
    m = re.match(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/#?\s]+)", cfg.repo_url or "", re.I)
    if not m:
        return None
    return "%s/%s" % (m.group(1), m.group(2).removesuffix(".git"))


def _ver_tuple(v: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    return tuple((nums + [0, 0, 0])[:3])  # type: ignore[return-value]


async def _github_latest(slug: str) -> tuple[str, str]:
    """取仓库最新版本号与页面地址。没发过 Release 就退回看 tag。"""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"TNews/{VERSION}"}
    token = os.environ.get("TNEWS_GITHUB_TOKEN")
    if token:  # 私有仓库要额外给一个只读 token，公开仓库留空即可
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as cli:
        r = await cli.get(f"https://api.github.com/repos/{slug}/releases/latest", headers=headers)
        if r.status_code == 200:
            d = r.json()
            return str(d.get("tag_name") or d.get("name") or ""), str(d.get("html_url") or "")
        if r.status_code in (403, 429):
            raise RuntimeError("GitHub 接口限流，稍后再试")
        if r.status_code != 404:
            raise RuntimeError(f"GitHub 返回 {r.status_code}")
        r2 = await cli.get(f"https://api.github.com/repos/{slug}/tags?per_page=1", headers=headers)
        if r2.status_code == 200 and r2.json():
            return str(r2.json()[0].get("name") or ""), f"https://github.com/{slug}/releases"
    raise RuntimeError("仓库不可访问：地址有误，或是私有仓库（私有仓库需设置 TNEWS_GITHUB_TOKEN）")


@app.get("/api/version")
async def version_info(request: Request, force: bool = False):
    """当前版本 vs 仓库最新版本。仅管理员；force=1 跳过缓存。"""
    require_admin(request)
    now_ts = time.time()
    cached = _VER_CACHE["payload"]
    if cached and not force:
        ttl = _VER_TTL_ERR if cached.get("error") else _VER_TTL
        if now_ts - _VER_CACHE["at"] < ttl:
            return cached

    data = {
        "current": VERSION,
        "latest": None,
        "has_update": False,
        "url": cfg.repo_url or "",
        "checked_at": now_local().isoformat(timespec="seconds"),
    }
    slug = _repo_slug()
    if not slug:
        data["error"] = "未配置项目地址（config.yaml 的 repo_url），无法检查更新"
    else:
        try:
            tag, url = await _github_latest(slug)
            latest = tag.lstrip("vV")
            data["latest"] = latest or None
            data["url"] = url or cfg.repo_url
            data["has_update"] = bool(latest) and _ver_tuple(latest) > _ver_tuple(VERSION)
        except Exception as exc:  # 网络不通/限流/私有仓库：只影响提示，不影响页面
            data["error"] = str(exc)
    _VER_CACHE.update(at=now_ts, payload=data)
    return data


@app.get("/api/meta")
async def meta(request: Request):
    now = now_local()
    user = current_user(request)
    is_sub = bool(user and user.get("_sub"))
    raw_sub = _sub_token_of(request)
    if is_sub:
        sub_info = {"enabled": True, "account": user.get("email"), "uid": user.get("uid")}
    elif raw_sub:
        # 带了订阅令牌却认不出身份：把原因说清楚，前端好给出可读提示
        sub_info = {
            "enabled": False,
            "error": ("订阅功能未开启：请让管理员在「设置 → 账号与数据」里打开「允许外部订阅」"
                      if not _allow_subscribe()
                      else "订阅地址无效：该账号已关闭订阅，或链接已被重置"),
        }
    else:
        sub_info = None
    customs = _custom_feeds(user)
    return {
        "app_name": cfg.app_name,
        "categories": [
            {
                "key": k,
                "label": v.label,
                "desc": v.desc,
                "feeds": (
                    [{"name": f.name, "url": f.url, "share": f.share or ""} for f in v.feeds]
                    + [c for c in customs if c.get("category") == k]
                ),
            }
            for k, v in cfg.categories.items()
        ],
        "programs": [
            {
                "id": p.id,
                "name": p.name,
                "cron": p.cron_list[0],
                "crons": p.cron_list,
                "categories": p.categories,
                "window": p.window,
                "lookback_hours": p.lookback_hours,
                "resolved_window": {
                    "start": p.resolve_window(now)[0].isoformat(),
                    "end": p.resolve_window(now)[1].isoformat(),
                },
            }
            for p in cfg.programs
        ],
        "voice": cfg.tts.get("voice"),
        "voices": [{"value": k, "label": v} for k, v in VOICES.items()],
        "sorts": [{"value": k, "label": v} for k, v in summarizer.SORTS.items()],
        "rate": cfg.tts.get("rate", "+0%"),
        "max_items": int(cfg.digest.get("max_items", 14)),
        "auto": _auto_state(int(user["id"])) if (user and not is_sub) else {"enabled": False, "guest": True, "intervals": AUTO_INTERVALS},
        # 订阅访问不是登录：user 给 None（前端按访客界面渲染），另给 sub 说明「在听谁的」
        "user": _public_user(user) if (user and not is_sub) else None,
        "sub": sub_info,
        # 未登录访客：能不能听、听谁的、不能听的原因（前端据此提示「需登录」）
        "guest": _guest_home() if (user is None and not is_sub) else None,
        "version": VERSION,
        "repo_url": cfg.repo_url,
        "server_time": now.isoformat(timespec="seconds"),
    }


@app.get("/api/episodes")
async def list_episodes(request: Request, program_id: str | None = None, limit: int = 30):
    flt = _visible_filter(current_user(request))
    if flt.get("blocked"):
        return []      # 访客收听已关闭：不返回任何节目
    return store.list(limit=limit, program_id=program_id,
                      user_id=flt["user_id"], all_users=flt["all_users"])


@app.get("/api/episodes/latest")
async def latest_episode(request: Request, program_id: str | None = None):
    flt = _visible_filter(current_user(request))
    if flt.get("blocked"):
        raise HTTPException(404, _guest_home()["message"] or "访客收听已关闭，请登录后收听")
    ep = store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"])
    if not ep:
        raise HTTPException(404, "还没有节目")
    ep["seq"] = store.seq_of(int(ep["id"]), ep.get("program_id"),
                             user_id=flt["user_id"], all_users=flt["all_users"])
    return ep


@app.get("/api/episodes/{episode_id}")
async def get_episode(request: Request, episode_id: int):
    flt = _visible_filter(current_user(request))
    if flt.get("blocked"):
        raise HTTPException(403, "访客收听已关闭，请登录后收听")
    ep = store.get(episode_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    ep["seq"] = store.seq_of(episode_id, ep.get("program_id"),
                             user_id=flt["user_id"], all_users=flt["all_users"])
    return ep


@app.delete("/api/episodes/{episode_id}")
async def delete_episode(episode_id: int, user: dict = Depends(require_user)):
    ep = store.get(episode_id)
    if not ep:
        raise HTTPException(404, "节目不存在")
    if user["role"] != "admin" and ep.get("user_id") not in (None, int(user["id"])):
        raise HTTPException(403, "只能删除自己的节目")
    name = store.delete(episode_id)
    path = AUDIO_DIR / (name or "")
    if name and path.exists():
        path.unlink()
    return {"ok": True}


@app.post("/api/ensure")
async def ensure(background: BackgroundTasks, request: Request, program_id: str | None = None):
    """打开页面时调用：最新一期太旧就自动补录一期（访客触发的也算管理员生成的）。"""
    if not cfg.server.get("auto_generate_on_open", True):
        return {"generating": False, "reason": "disabled"}

    user = current_user(request)
    if user and user.get("_sub"):
        # 订阅者是只读的：不替账号触发自动补录（否则等于外人能让你的账号跑生成）
        return {"generating": False, "reason": "readonly"}
    if user is None and not _guest_home()["allowed"]:
        # 访客收听已关闭：不替管理员跑生成
        return {"generating": False, "reason": "guest-off"}
    flt = _visible_filter(user)
    uid = int(user["id"]) if user else admin_id()

    program = cfg.get_program(program_id) if program_id else (cfg.programs[0] if cfg.programs else None)
    if not program:
        return {"generating": False, "reason": "no-program"}

    if STATE.get(program.id, {}).get("status") == "running":
        return {"generating": True, "program_id": program.id}

    latest = store.latest(program.id, user_id=flt["user_id"], all_users=flt["all_users"])
    min_age = float(cfg.server.get("auto_generate_min_age_hours", 6))
    if latest and latest.get("created_at"):
        try:
            age = (now_local() - datetime.fromisoformat(latest["created_at"])).total_seconds() / 3600
            if age < min_age:
                return {"generating": False, "age_hours": round(age, 1), "episode_id": latest["id"]}
        except ValueError:
            pass

    background.add_task(_run_generation, program.id, GenOptions(user_id=uid))
    return {"generating": True, "program_id": program.id}


@app.post("/api/generate")
async def generate(req: GenerateReq, background: BackgroundTasks, user: dict = Depends(require_user)):
    if not cfg.get_program(req.program_id):
        raise HTTPException(404, "节目配置不存在")
    if STATE.get(req.program_id, {}).get("status") == "running":
        return {"status": "running", "message": "正在生成中"}

    opts = GenOptions(
        categories=req.categories,
        feeds=req.feeds,
        shares=req.shares,
        rate=req.rate,
        max_items=req.max_items,
        target_minutes=req.target_minutes,
        sort=req.sort,
        voice=req.voice,
        lookback_hours=req.lookback_hours,
        user_id=int(user["id"]),
    )
    if req.run_km is not None or req.run_minutes is not None:
        # 跑步模式：距离 + 用时 → 按用时生成，并在节目里记下配速供播放端显示
        if not req.run_km or not req.run_minutes:
            raise HTTPException(400, "跑步模式需同时提供距离与用时")
        if not (0.5 <= req.run_km <= 200):
            raise HTTPException(400, "距离需在 0.5 ~ 200 公里之间")
        if not (3 <= req.run_minutes <= 360):
            raise HTTPException(400, "用时需在 3 ~ 360 分钟之间")
        opts.run_km = float(req.run_km)
        opts.target_minutes = float(req.run_minutes)

    if req.lookback_hours:
        if not (0.5 <= req.lookback_hours <= 168):
            raise HTTPException(400, "回溯小时数需在 0.5 ~ 168 之间")
    elif req.window_start and req.window_end:
        try:
            start, end = resolve_hhmm_window(req.window_start, req.window_end, now_local())
        except ValueError:
            raise HTTPException(400, "时间格式应为 HH:MM")
        if (end - start).total_seconds() < 600:
            raise HTTPException(400, "时间窗口太短，至少需要 10 分钟")
        if (end - start).total_seconds() > 7 * 86400:
            raise HTTPException(400, "时间窗口太长，最多 7 天")
        opts.window_start, opts.window_end = start, end

    background.add_task(_run_generation, req.program_id, opts)
    return {"status": "started"}


@app.get("/api/status")
async def status():
    return STATE


# ---------------------------- 自定义新闻源 ----------------------------
class CustomFeedReq(BaseModel):
    name: str
    url: str
    category: str


async def _validate_feed(url: str) -> tuple[bool, str]:
    """检验地址是否是标准 RSS/Atom：能取回 XML 且含 channel/entry 结构。"""
    import httpx

    if not url.lower().startswith(("http://", "https://")):
        return False, "地址必须以 http:// 或 https:// 开头"
    try:
        async with httpx.AsyncClient(
            timeout=12, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (tnews feed check)"},
        ) as client:
            resp = await client.get(url)
        body = resp.text[:20000]
        lower = body.lower()
        if resp.status_code >= 400:
            return False, f"该地址返回 HTTP {resp.status_code}"
        if "<rss" in lower or "<feed" in lower or "<rdf:rdf" in lower:
            return True, "标准 RSS/Atom 源，校验通过"
        return False, "能访问，但内容不是标准 RSS/Atom 格式（需要 XML 里的 <rss> 或 <feed> 结构）"
    except httpx.HTTPError as e:
        return False, f"访问失败：{type(e).__name__}"


@app.get("/api/custom-feeds")
async def list_custom_feeds(request: Request):
    return _custom_feeds(current_user(request))


@app.post("/api/custom-feeds")
async def add_custom_feed(req: CustomFeedReq, user: dict = Depends(require_user)):
    if req.category not in cfg.categories:
        raise HTTPException(400, "分类无效")
    url = req.url.strip()
    name = req.name.strip() or "自定义源"
    if len(name) > 40:
        raise HTTPException(400, "名称太长（40 字以内）")
    ok, msg = await _validate_feed(url)
    if not ok:
        raise HTTPException(400, msg)
    uid = int(user["id"])
    customs = store.custom_feeds(uid)
    if any(f["url"] == url for f in customs):
        raise HTTPException(400, "这个地址已经添加过了")
    builtin = {f.url for c in cfg.categories.values() for f in c.feeds}
    if url in builtin:
        raise HTTPException(400, "这个地址是内置源，不用重复添加")
    customs.append({"name": name, "url": url, "category": req.category})
    store.set_custom_feeds(uid, customs)
    log.info("新增自定义源（%s）：%s → %s (%s)", user["email"], name, url, req.category)
    return {"ok": True, "feeds": customs, "message": msg}


@app.delete("/api/custom-feeds")
async def delete_custom_feed(url: str, user: dict = Depends(require_user)):
    uid = int(user["id"])
    customs = [f for f in store.custom_feeds(uid) if f.get("url") != url]
    store.set_custom_feeds(uid, customs)
    return {"ok": True, "feeds": customs}


# ---------------------------- 今日天气 ----------------------------
class WeatherReq(BaseModel):
    enabled: bool
    place: str | None = None  # 地区名（与 lat/lon 二选一）
    lat: float | None = None  # 浏览器定位 / 访问端 IP 定位坐标
    lon: float | None = None
    name: str | None = None   # 坐标对应的城市名（访问端 IP 定位自带，反解失败时兜底）


def _wx_key(user: dict) -> str:
    return f"u{user['id']}:weather"


@app.get("/api/weather")
async def weather_get(request: Request):
    user = current_user(request)
    if not user:
        return {"enabled": False, "guest": True}
    return store.get_setting(_wx_key(user)) or {"enabled": False}


@app.get("/api/weather/search")
async def weather_search(q: str, user: dict = Depends(require_user)):
    return {"results": await weather.geocode(q)}


@app.get("/api/weather/audio")
async def weather_audio(request: Request):
    """今日天气独立音频（一天一条）。

    当天已有音频直接返回；没有但用户开启了天气 → 现场补合成一条（约几秒），
    因为最新一期可能是昨天生成的、生成时顺带合成的那条已过期。
    前端当日首次播放最新一期时前置播放它；往期节目、未开启天气返回 exists=False。
    """
    user = current_user(request)
    if not user:
        return {"exists": False}
    today = now_local().strftime("%Y-%m-%d")
    info = store.get_setting(f"u{user['id']}:weather_audio") or {}
    f = str(info.get("file") or "")
    if info.get("date") != today or not f or not (AUDIO_DIR / f).exists():
        wcfg = store.get_setting(_wx_key(user)) or {}
        if not (wcfg.get("enabled") and wcfg.get("lat") is not None):
            return {"exists": False}
        info = await pipeline.synthesize_weather(user["id"], wcfg, cfg, store)
        if not info:
            return {"exists": False}
        f = str(info.get("file") or "")
        if not f or not (AUDIO_DIR / f).exists():
            return {"exists": False}
    return {
        "exists": True,
        "url": f"/audio/{f}",
        "text": info.get("text", ""),
        "place": info.get("place", ""),
        "duration": info.get("duration", 0),
    }


_WX_NOW_CACHE: dict[int, tuple[int, dict]] = {}  # user_id -> (10分钟桶, payload)


@app.get("/api/weather/now")
async def weather_now(request: Request):
    """首页常驻天气条：开启了天气就返回实时天气（10 分钟缓存，避免每次刷新都打外部接口）。"""
    user = current_user(request)
    if not user:
        return {"enabled": False}
    wcfg = store.get_setting(_wx_key(user)) or {}
    if not (wcfg.get("enabled") and wcfg.get("lat") is not None):
        return {"enabled": False}
    import time as _time
    stamp = int(_time.time() // 600)
    hit = _WX_NOW_CACHE.get(user["id"])
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        info = await weather.report(
            float(wcfg["lat"]), float(wcfg["lon"]), str(wcfg.get("place") or "")
        )
    except Exception as e:
        log.warning("首页天气条获取失败：%s", e)
        return {"enabled": True}
    payload = {
        "enabled": True, "place": info.get("place", ""),
        "temp": info.get("temp"), "desc": info.get("desc", ""),
        "tmax": info.get("tmax"), "tmin": info.get("tmin"),
    }
    _WX_NOW_CACHE[user["id"]] = (stamp, payload)
    return payload


@app.post("/api/weather")
async def weather_set(req: WeatherReq, request: Request):
    user = require_user(request)
    data: dict = {"enabled": req.enabled}
    if req.enabled:
        if req.lat is not None and req.lon is not None:
            # 浏览器定位 / 访问端 IP 定位：坐标直接可用，城市名尽力反解（IP 定位自带城市名兜底）
            name = await weather.reverse(req.lat, req.lon) or (req.name or "").strip() or "当前位置"
            data.update(place=name, lat=req.lat, lon=req.lon)
        elif req.place:
            res = await weather.geocode(req.place)
            if not res:
                raise HTTPException(400, f"找不到地区「{req.place}」，试试加上省市名")
            data.update(place=res[0]["label"], lat=res[0]["lat"], lon=res[0]["lon"])
        else:
            # 只切开关：沿用上次保存的坐标
            old = store.get_setting(_wx_key(user)) or {}
            if old.get("lat") is None:
                raise HTTPException(400, "请先填写地区或使用当前位置")
            data.update(place=old.get("place"), lat=old["lat"], lon=old["lon"])
    store.set_setting(_wx_key(user), data)
    log.info("天气设置更新（%s）：%s", user["email"], data)
    return data


@app.get("/api/auto")
async def get_auto(user: dict = Depends(require_user)):
    """间隔自动更新的当前状态：每个登录用户各自一份设置、各自的任务。"""
    return _auto_state(int(user["id"]))


@app.post("/api/auto")
async def set_auto(req: AutoReq, user: dict = Depends(require_user)):
    """开启/关闭「每 N 小时自动更新」，并记住当时的时间范围、分类、源、语速。

    每个登录用户各自设置、互不影响：参数快照存在该用户名下，
    自动生成出的节目也归该用户（管理员的那份访客可见）。"""
    uid = int(user["id"])
    data = _get_auto(uid)
    data["enabled"] = bool(req.enabled)

    if req.interval_hours is not None:
        hours = float(req.interval_hours)
        if not (0.5 <= hours <= 168):
            raise HTTPException(400, "自动更新间隔需在 0.5 ~ 168 小时之间")
        data["interval_hours"] = hours

    if req.start_hhmm is not None:
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(req.start_hhmm).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise HTTPException(400, "开始时间格式应为 HH:MM")
        data["start_hhmm"] = f"{int(m.group(1)):02d}:{m.group(2)}"

    if req.program_id:
        if not cfg.get_program(req.program_id):
            raise HTTPException(400, "节目配置不存在")
        data["program_id"] = req.program_id

    if req.categories:
        valid = [c for c in req.categories if c in cfg.categories]
        if not valid:
            raise HTTPException(400, "分类无效")
        data["categories"] = valid
    if req.feeds:
        data["feeds"] = req.feeds
    if req.shares is not None:
        # 只保留 1~100 的合法占比，空值/非法值视为不参考
        data["shares"] = {u: int(p) for u, p in req.shares.items() if 0 < int(p) <= 100} or None
    if req.rate:
        data["rate"] = req.rate
    if req.target_minutes is not None:
        if not (2 <= req.target_minutes <= 120):
            raise HTTPException(400, "目标时长需在 2 ~ 120 分钟之间")
        data["target_minutes"] = float(req.target_minutes)
    if req.sort is not None:
        if req.sort not in summarizer.SORTS:
            raise HTTPException(400, "排序方式无效")
        data["sort"] = req.sort
    if req.voice is not None:
        if req.voice and req.voice not in VOICES:
            raise HTTPException(400, "不支持的音色")
        data["voice"] = req.voice or None

    store.set_setting(auto_setting_key(uid), data)
    if req.reschedule or (data["enabled"] and scheduler.get_job(auto_job_id(uid)) is None):
        _apply_auto_job(uid)
    log.info(
        "自动更新设置已保存（%s）：%s，每 %.1f 小时",
        user["email"],
        "开启" if data["enabled"] else "关闭",
        float(data["interval_hours"]),
    )
    return _auto_state(uid)


# ---------------------------- 往期保留与清除（管理员） ----------------------------
RETENTION_CHOICES = [7, 14, 30, 60, 90, 0]  # 0 = 永久保留


def _retention_days() -> int:
    val = store.get_setting("retention_days")
    try:
        return int(val) if val is not None else int(cfg.retention_days)
    except (TypeError, ValueError):
        return int(cfg.retention_days)


@app.get("/api/retention")
async def get_retention(user: dict = Depends(require_admin)):
    return {"days": _retention_days(), "choices": RETENTION_CHOICES, "config_default": cfg.retention_days}


@app.post("/api/retention")
async def set_retention(req: dict, user: dict = Depends(require_admin)):
    try:
        days = int(req.get("days"))
    except (TypeError, ValueError):
        raise HTTPException(400, "保留天数无效")
    if days != 0 and not (1 <= days <= 365):
        raise HTTPException(400, "保留天数需在 1 ~ 365 之间，0 表示永久保留")
    store.set_setting("retention_days", days)
    log.info("往期节目保留天数改为 %s", "永久" if days == 0 else f"{days} 天")
    return {"days": days, "choices": RETENTION_CHOICES}


@app.post("/api/episodes/clear")
async def clear_episodes(user: dict = Depends(require_admin)):
    """手动清除全部往期节目（含音频文件），不可恢复。"""
    files = store.purge_all()
    removed = 0
    for name in files:
        path = AUDIO_DIR / name
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass
    log.info("手动清除往期节目：%d 期、音频文件 %d 个", len(files), removed)
    return {"ok": True, "removed": len(files)}


@app.post("/api/episodes/clear/mine")
async def clear_my_episodes(user: dict = Depends(require_user)):
    """用户清除自己账号下的全部往期节目（含音频文件），不可恢复。"""
    files = store.purge_user(user["id"])
    removed = 0
    for name in files:
        path = AUDIO_DIR / name
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass
    log.info("用户 %s 清除自己的往期节目：%d 期、音频文件 %d 个", user["email"], len(files), removed)
    return {"ok": True, "removed": len(files)}


# ---------------------------- 用户管理（管理员） ----------------------------
class AdminUserReq(BaseModel):
    email: str | None = None
    password: str | None = None
    disabled: bool | None = None
    external: bool | None = None   # 是否允许外部按账号调用该用户的最新一期（?user=）
    subscribe: bool | None = None  # 是否开启该账号的外部订阅地址（/s/<token>）
    podcast: bool | None = None    # 是否开启该账号的播客订阅地址（/feed/<token>.xml）


@app.get("/api/admin/users")
async def admin_list_users(user: dict = Depends(require_admin)):
    users = []
    for u in store.list_users():
        users.append({
            "id": u["id"], "uid": u.get("uid") or "", "email": u["email"], "role": u["role"],
            "disabled": bool(u["disabled"]), "created_at": u.get("created_at"),
            "episodes": u.get("episodes", 0),
            "external": bool(store.get_user_setting(u["id"], "allow_external", False)),
            "subscribe": bool(store.get_user_setting(u["id"], "sub_enabled", False)),
            "podcast": bool(store.get_user_setting(u["id"], "feed_enabled", False)),
        })
    return users


@app.post("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, req: AdminUserReq, user: dict = Depends(require_admin)):
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target["id"] == user["id"] and req.disabled:
        raise HTTPException(400, "不能禁用自己的账号")

    fields: dict = {}
    if req.email is not None:
        account = _check_account(req.email)
        exists = store.get_user_by_email(account)
        if exists and int(exists["id"]) != user_id:
            raise HTTPException(400, "这个账号已被其他用户使用")
        fields["email"] = account
    if req.password is not None:
        if len(req.password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        fields["password_hash"] = hash_password(req.password)
    if req.disabled is not None:
        fields["disabled"] = 1 if req.disabled else 0

    store.update_user(user_id, **fields)
    if req.external is not None:
        # 分开关存在用户级设置里，不动 users 表结构
        store.set_user_setting(user_id, "allow_external", req.external)
    if req.subscribe is not None:
        # 管理员代开关；开启时若还没有令牌就顺手生成，用户那边打开即可看到地址
        if req.subscribe:
            store.ensure_sub_token(user_id)
        store.set_user_setting(user_id, "sub_enabled", req.subscribe)
    if req.podcast is not None:
        # 播客订阅是另一套开关与令牌（/feed/<token>.xml），互不影响
        if req.podcast:
            store.ensure_feed_token(user_id)
        store.set_user_setting(user_id, "feed_enabled", req.podcast)
    # 禁用后踢掉全部会话，并停掉该用户自己的自动更新任务
    if fields.get("disabled") == 1:
        with store._conn() as conn:
            conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        _remove_auto_job(user_id)
    log.info("管理员更新用户 #%s：%s", user_id, ", ".join(fields.keys()) or "无改动")
    return {"ok": True, "user": _public_user(store.get_user(user_id) or {})}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, user: dict = Depends(require_admin)):
    if user_id == user["id"]:
        raise HTTPException(400, "不能删除自己的账号")
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    _remove_auto_job(user_id)
    audio = store.delete_user(user_id)
    removed = 0
    for name in audio:
        path = AUDIO_DIR / name
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError:
            pass
    log.info("管理员删除用户 %s（#%s），清理节目音频 %d 个", target["email"], user_id, removed)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "time": now_local().isoformat(timespec="seconds")}


# ---------------------------- 音频与播客 ----------------------------
@app.get("/audio/{name}")
async def audio(name: str, inline: bool = False):
    """音频文件。默认以附件下载；加 ?inline=1 改为流式内联，
    个别音箱/播放器遇到 attachment 头会反复拉流导致「循环播放」，用 inline 更稳。"""
    if "/" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    path = AUDIO_DIR / name
    if not path.exists():
        raise HTTPException(404, "音频不存在")
    if inline:
        return FileResponse(
            path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )
    return FileResponse(path, media_type="audio/mpeg", filename=name)


def _base_url(request: Request) -> str:
    if cfg.public_base_url:
        return cfg.public_base_url
    return str(request.base_url).rstrip("/")


def _sub_url(request: Request, token: str) -> str:
    """该账号的订阅地址：/s/<token>，单独打开的页面，不需要登录。"""
    tok = str(token or "").strip()
    if not tok:
        return ""
    return f"{_base_url(request).rstrip('/')}/s/{tok}"


def _sub_feed_url(request: Request, token: str) -> str:
    """该账号的播客订阅地址：/feed/<播客令牌>.xml（只装这个账号的节目）。"""
    tok = str(token or "").strip()
    if not tok:
        return ""
    return f"{_base_url(request).rstrip('/')}/feed/{tok}.xml"


def _latest_for_visitor(program_id: str | None):
    """访客视角的最新一期（外部系统如 Home Assistant 直接调，不用登录）。"""
    g = _guest_home()
    if not g["allowed"]:
        return None            # 访客收听已关闭：外部匿名调用也拿不到内容
    flt = _visible_filter(None)   # 访客 = 「分享速听」那个账号（默认管理员）的节目
    return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"])


def _resolve_program(raw: str | None):
    """把 program_id 归一化：支持节目 id 或节目名，大小写不敏感。

    返回 (规范化后的 id, Program)；raw 为空时返回 (None, None)，表示不限定节目。
    """
    key = (raw or "").strip()
    if not key:
        return None, None
    low = key.lower()
    for p in cfg.programs:
        if low == (p.id or "").lower() or key == p.name:
            return p.id, p
    return key, None


def _available_programs_text() -> str:
    return "、".join(f"{p.id}（{p.name}）" for p in cfg.programs) or "（config.yaml 里还没配置节目）"


def _allow_external_latest() -> bool:
    return bool(store.get_setting("allow_external_latest", False))


def _account_by_name(name: str | None) -> dict | None:
    """按账号名找用户：完整邮箱，或 @ 前面那一段（ha → ha@local）。"""
    key = (name or "").strip().lower()
    if not key:
        return None
    u = store.get_user_by_email(key)
    if u:
        return u
    for row in store.list_users():
        email = str(row.get("email") or "").strip().lower()
        if email.split("@")[0] == key:
            return row
    return None


def _latest_for_caller(request: Request, program_id: str | None,
                       account: str | None, token: str | None, uid: str | None = None,
                       ext: str | None = None):
    """决定「最新一期」属于谁：token（身份）> ?ext=（外部调用令牌）> ?sub=（订阅）> ?user=（账号名）> 访客。

    匿名按账号调用（?user=）必须同时带上该账号的 6 位用户 ID（?id=123456），
    相当于「账号名 + ID」两把钥匙，防止只靠猜账号名就能调到节目。

    返回 (最新一期, 命中的账号或 None, 可见范围)。
    """
    tok = (token or "").strip() or _token_of(request)
    if tok:
        u = store.user_for_token(tok)
        if not u:
            raise HTTPException(401, "token 无效或已失效，请在网页重新登录后获取")
        flt = _visible_filter(u)
        return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"]), u, flt

    # 外部调用令牌（?ext=）：与外部订阅 / 播客订阅同一套令牌思路 —— 地址即钥匙，
    # 仍受两层开关约束：管理员总开关 + 该账号自己的「外部」分开关
    ext_tok = (ext or "").strip()
    if ext_tok:
        if not _allow_external_latest():
            raise HTTPException(
                403,
                "外部按账号调用未开启（总开关）：请让管理员在网页「设置 → 账号与数据」里打开"
                "「允许外部按账号调用最新一期」",
            )
        u = store.user_by_ext_token(ext_tok)
        if not u:
            raise HTTPException(
                403,
                "外部调用令牌无效：请在网页「我的账号 → 外部调用」重新复制地址",
            )
        if u.get("disabled"):
            raise HTTPException(403, f"账号 {u.get('email')} 已被禁用")
        if not store.get_user_setting(int(u["id"]), "allow_external", False):
            raise HTTPException(
                403,
                f"账号 {u.get('email')} 已关闭外部调用：请在网页「我的账号 → 外部调用」打开",
            )
        flt = _visible_filter(u)
        return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"]), u, flt

    # 外部订阅（?sub=<订阅令牌>）：和访客听管理员一样，只是对象换成这个账号
    sub = _sub_token_of(request)
    if sub:
        if not _allow_subscribe():
            raise HTTPException(
                403,
                "外部订阅未开启（总开关）：请让管理员在网页「设置 → 账号与数据」里打开「允许外部订阅」",
            )
        u = store.user_by_sub_token(sub)
        if not u:
            raise HTTPException(403, "订阅地址无效：令牌不存在或已被重置")
        if u.get("disabled"):
            raise HTTPException(403, f"账号 {u.get('email')} 已被禁用")
        if not store.get_user_setting(int(u["id"]), "sub_enabled", False):
            raise HTTPException(403, f"账号 {u.get('email')} 已关闭外部订阅")
        flt = _visible_filter(u)
        return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"]), u, flt

    if account:
        # ?user= 是把某个账号的节目暴露给匿名调用，需要三层校验都通过：
        # ① 管理员的总开关（设置 → 账号与数据） ② 该账号自己的分开关（用户管理里逐个开）
        # ③ 6 位用户 ID 匹配（?id=123456，在「我的账户」或「用户管理」里可查）
        if not _allow_external_latest():
            raise HTTPException(
                403,
                "外部按账号调用未开启（总开关）：请让管理员在网页「设置 → 账号与数据」里打开"
                "「允许外部按账号调用最新一期」，或改用该账号的 token",
            )
        u = _account_by_name(account)
        if not u:
            raise HTTPException(
                404,
                f"没有这个账号：{account}。请填账号的登录名或邮箱（管理员在「用户管理」里可以查到）",
            )
        if u.get("disabled"):
            raise HTTPException(403, f"账号 {u.get('email')} 已被禁用")
        if not store.get_user_setting(int(u["id"]), "allow_external", False):
            raise HTTPException(
                403,
                f"账号 {u.get('email')} 未开启外部调用（分开关）："
                "请让管理员在「设置 → 账号与数据 → 用户管理」里打开该账号的「外部」，或改用 token",
            )
        got = (uid or "").strip()
        want = str(u.get("uid") or "")
        if not got:
            # 安全：不回显正确 ID，否则等于把钥匙贴在锁上
            raise HTTPException(
                403,
                "缺少用户 ID：按账号匿名调用需要同时带上该账号的 6 位用户 ID"
                "（在网页「我的账户」或管理员「用户管理」里可查，附到 &id= 参数）",
            )
        if got != want:
            raise HTTPException(403, "账号或用户 ID 不正确：请核对后重试")
        flt = _visible_filter(u)
        return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"]), u, flt

    g = _guest_home()
    if not g["allowed"]:
        raise HTTPException(
            403,
            g["message"] or "访客收听已关闭，请登录后收听；外部系统请改用账号的 ext / token 参数",
        )
    flt = _visible_filter(None)
    return store.latest(program_id, user_id=flt["user_id"], all_users=flt["all_users"]), None, flt


def _latest_strict(request: Request, program_id: str | None = None,
                   account: str | None = None, token: str | None = None,
                   uid: str | None = None, ext: str | None = None) -> dict:
    """取最新一期；节目/账号写错时给出明确提示，而不是笼统的「还没有节目」。"""
    pid, prog = _resolve_program(program_id)
    if program_id and prog is None:
        raise HTTPException(
            404,
            f"没有这个节目：{program_id}。可用的 program_id：{_available_programs_text()}；"
            "不传 program_id 则返回所有节目里最新的一期",
        )
    ep, acct, flt = _latest_for_caller(request, pid, account, token, uid, ext)
    if not ep:
        if prog is not None:
            raise HTTPException(404, f"节目「{prog.name}」（{prog.id}）还没有生成过任何一期")
        if acct is not None:
            raise HTTPException(404, f"账号 {acct.get('email')} 还没有生成过任何一期")
        raise HTTPException(404, "还没有节目")
    ep["seq"] = store.seq_of(int(ep["id"]), ep.get("program_id"),
                             user_id=flt["user_id"], all_users=flt["all_users"])
    return ep


@app.get("/api/external-latest")
async def get_external_latest(user: dict = Depends(require_admin)):
    """「允许外部按账号调用最新一期」开关的当前状态。"""
    return {"enabled": _allow_external_latest()}


@app.post("/api/external-latest")
async def set_external_latest(req: dict, user: dict = Depends(require_admin)):
    enabled = bool(req.get("enabled"))
    store.set_setting("allow_external_latest", enabled)
    log.info("外部按账号调用最新一期（?user=）：%s", "开启" if enabled else "关闭")
    return {"enabled": enabled}


@app.get("/api/me/prefs")
async def get_my_prefs(user: dict = Depends(require_user)):
    """账号级界面设置（生成分类/时长/音色/源占比、背景音乐、睡眠、播放顺序…）。

    存在浏览器本地时换域名（192.168.x.x / nas.local / 域名）就丢了，所以这里按账号存一份，
    登录后前端先拉这个把本机缺的补齐。只收 nr_ 开头的简单键值，值统一按字符串存。
    """
    data = store.get_setting(f"u{int(user['id'])}:prefs") or {}
    prefs = data.get("prefs") if isinstance(data, dict) and "prefs" in data else data
    return {
        "prefs": prefs if isinstance(prefs, dict) else {},
        "ts": int(data.get("ts") or 0) if isinstance(data, dict) else 0,
    }


@app.put("/api/me/prefs")
async def set_my_prefs(req: dict, user: dict = Depends(require_user)):
    """整体覆盖保存（前端每次改动 800ms 防抖后推一次全量快照）。"""
    raw = req.get("prefs")
    if not isinstance(raw, dict):
        raise HTTPException(400, "prefs 必须是对象")
    clean: dict[str, str] = {}
    for k, v in list(raw.items())[:300]:
        if not isinstance(k, str) or not k.startswith("nr_") or len(k) > 80:
            continue
        if k in ("nr_token", "nr_uid"):
            continue
        s = v if isinstance(v, str) else str(v)
        if len(s) > 20000:   # 生成设置里可能带整份源列表，给足余量
            continue
        clean[k] = s
    try:
        ts = int(req.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    store.set_setting(f"u{int(user['id'])}:prefs", {"prefs": clean, "ts": ts})
    return {"ok": True, "count": len(clean), "ts": ts}


@app.get("/api/me/external")
async def get_my_external(request: Request, user: dict = Depends(require_user)):
    """我这个账号的「外部」分开关状态 + 外部调用令牌地址（总开关由管理员控制）。

    令牌与外部订阅/播客订阅同一套体系：关闭开关不清令牌，重开地址不变。
    """
    uid = int(user["id"])
    tok = store.ensure_ext_token(uid)
    base = _base_url(request).rstrip("/")
    q = f"ext={tok}"
    return {
        "enabled": bool(store.get_user_setting(uid, "allow_external", False)),
        "global_enabled": _allow_external_latest(),
        "uid": user.get("uid"),
        "account": user.get("email"),
        "ext_token": tok,
        "urls": {
            "latest": f"{base}/api/latest?{q}",
            "content": f"{base}/api/latest/content?{q}",
            "mp3": f"{base}/latest.mp3?{q}",
        },
    }


@app.post("/api/me/external")
async def set_my_external(req: dict, user: dict = Depends(require_user)):
    """用户在自己的「我的账号」里开关自己的外部调用权限。

    总开关（管理员）没开时，即使分开关开着也调不通，所以这里照实返回总开关状态给前端提示。
    """
    enabled = bool(req.get("enabled"))
    store.set_user_setting(int(user["id"]), "allow_external", enabled)
    log.info("用户 %s 自己的外部调用开关：%s", user.get("email"), "开启" if enabled else "关闭")
    return {
        "enabled": enabled,
        "global_enabled": _allow_external_latest(),
        "uid": user.get("uid"),
        "account": user.get("email"),
    }


@app.get("/api/me/subscribe")
async def get_my_subscribe(request: Request, user: dict = Depends(require_user)):
    """我的订阅开关 + 订阅地址（外部订阅总开关由管理员控制）。"""
    uid = int(user["id"])
    tok = str(store.get_user_setting(uid, "sub_token") or "")
    return {
        "enabled": bool(store.get_user_setting(uid, "sub_enabled", False)),
        "global_enabled": _allow_subscribe(),
        "token": tok,
        "url": _sub_url(request, tok) if tok else "",
        "account": user.get("email"),
    }


@app.post("/api/me/subscribe")
async def set_my_subscribe(request: Request, req: dict, user: dict = Depends(require_user)):
    """开启/关闭自己的外部订阅；开启时首次生成一个只属于自己的订阅地址。"""
    enabled = bool(req.get("enabled"))
    uid = int(user["id"])
    tok = store.ensure_sub_token(uid) if enabled else str(store.get_user_setting(uid, "sub_token") or "")
    store.set_user_setting(uid, "sub_enabled", enabled)
    log.info("用户 %s 的外部订阅：%s", user.get("email"), "开启" if enabled else "关闭")
    return {
        "enabled": enabled,
        "global_enabled": _allow_subscribe(),
        "token": tok,
        "url": _sub_url(request, tok) if tok else "",
        "account": user.get("email"),
    }


@app.get("/api/me/podcast")
async def get_my_podcast(request: Request, user: dict = Depends(require_user)):
    """我的播客订阅开关 + 专属 RSS 地址（与网页订阅各用一把钥匙、各一个开关）。"""
    uid = int(user["id"])
    tok = str(store.get_user_setting(uid, "feed_token") or "")
    return {
        "enabled": bool(store.get_user_setting(uid, "feed_enabled", False)),
        "global_enabled": _allow_podcast(),
        "token": tok,
        "feed_url": _sub_feed_url(request, tok) if tok else "",
        "account": user.get("email"),
    }


@app.post("/api/me/podcast")
async def set_my_podcast(request: Request, req: dict, user: dict = Depends(require_user)):
    """开启/关闭自己的播客订阅；开启时首次生成专属 RSS 地址。"""
    enabled = bool(req.get("enabled"))
    uid = int(user["id"])
    tok = store.ensure_feed_token(uid) if enabled else str(store.get_user_setting(uid, "feed_token") or "")
    store.set_user_setting(uid, "feed_enabled", enabled)
    log.info("用户 %s 的播客订阅：%s", user.get("email"), "开启" if enabled else "关闭")
    return {
        "enabled": enabled,
        "global_enabled": _allow_podcast(),
        "token": tok,
        "feed_url": _sub_feed_url(request, tok) if tok else "",
        "account": user.get("email"),
    }


@app.get("/api/podcast")
async def get_podcast(user: dict = Depends(require_admin)):
    """「允许播客订阅」总开关状态（管理员）。"""
    return {"enabled": _allow_podcast()}


@app.post("/api/podcast")
async def set_podcast(req: dict, user: dict = Depends(require_admin)):
    enabled = bool(req.get("enabled"))
    store.set_setting("allow_podcast", enabled)
    log.info("播客订阅（/feed/<token>.xml）：%s", "开启" if enabled else "关闭")
    return {"enabled": enabled}


@app.get("/api/subscribe")
async def get_subscribe(user: dict = Depends(require_admin)):
    """「允许外部订阅」总开关状态（管理员）。"""
    return {"enabled": _allow_subscribe()}


@app.post("/api/subscribe")
async def set_subscribe(req: dict, user: dict = Depends(require_admin)):
    """总开关：允许账号在「我的账号」里开启 /s/<token> 只读收听地址。"""
    enabled = bool(req.get("enabled"))
    store.set_setting("allow_subscribe", enabled)
    log.info("外部订阅（/s/<token>）：%s", "开启" if enabled else "关闭")
    return {"enabled": enabled}


@app.get("/api/guest")
async def get_guest(user: dict = Depends(require_admin)):
    """「设置 → 全局管理 → 首页访客指向」：可选账号 + 当前指向 + 实际能否收听。"""
    accounts = []
    for u in store.list_users():
        accounts.append({
            "email": u.get("email"),
            "uid": u.get("uid") or "",
            "role": u.get("role"),
            "disabled": bool(u.get("disabled")),
            # 该账号的「速听分享」是否开着：没开的话访客会被要求登录
            "sub_enabled": bool(store.get_user_setting(int(u["id"]), "sub_enabled", False)),
        })
    g = _guest_home()
    raw = cfg.guest if isinstance(cfg.guest, dict) else {}
    return {
        "account": g.get("account") or "",      # 当前实际生效的账号
        "saved_uid": str(store.get_setting(GUEST_ACCOUNT_KEY, "") or ""),
        "config_account": str(raw.get("account") or ""),
        "config_enabled": _as_bool(raw.get("enabled"), True) if isinstance(cfg.guest, dict) else None,
        "allowed": g["allowed"],
        "reason": g["reason"],
        "message": g["message"],
        "accounts": accounts,
    }


@app.post("/api/guest")
async def set_guest(req: dict, user: dict = Depends(require_admin)):
    """改「首页访客指向」：传 account（登录名/邮箱/6 位用户 ID），空 = 主管理员自己。"""
    key = str(req.get("account") or "").strip()
    if not key:
        store.set_setting(GUEST_ACCOUNT_KEY, "")
        log.info("首页访客指向：恢复为主管理员")
    else:
        u = _user_by_key(key)
        if not u:
            raise HTTPException(404, f"没有这个账号：{key}")
        store.set_setting(GUEST_ACCOUNT_KEY, str(u.get("uid") or u.get("email") or ""))
        log.info("首页访客指向：%s", u.get("email"))
    return await get_guest(user)


@app.get("/api/programs")
async def programs_info():
    """节目清单 + 各自最新一期，方便 HA 做下拉选择，也方便确认 program_id 该写什么。"""
    out = []
    for p in cfg.programs:
        ep = _latest_for_visitor(p.id) or {}
        out.append({
            "id": p.id,
            "name": p.name,
            "lookback_hours": p.lookback_hours,
            "latest_id": ep.get("id"),
            "latest_title": ep.get("title"),
            "latest_created_at": ep.get("created_at"),
            "latest_duration": ep.get("duration"),
        })
    return {"programs": out}


@app.get("/latest.mp3")
async def latest_audio(request: Request, program_id: str | None = None,
                       user: str | None = None, token: str | None = None,
                       id: str | None = None, uid: str | None = None,
                       ext: str | None = None):
    """固定地址：永远指向最新一期音频（302 跳转到绝对地址）。

    Home Assistant / 小爱 / 其它音箱直接播放这个地址即可，
    例如 media_content_id: "http://192.168.1.100:18282/latest.mp3"。

    可选参数：
    - program_id：morning / evening，也可以直接写节目名
    - ext：外部调用令牌（推荐，网页「我的账号 → 外部调用」里复制），
      等价于以该账号身份请求；总开关与该账号的「外部」分开关需开启
    - user：按账号取（如 ha，或完整邮箱）；不给则用访客视角（= 管理员的节目）。
      按账号调用必须同时带该账号的 6 位用户 ID（id=123456，也接受 uid=）
    - token：正式鉴权，等价于以该账号身份请求（推荐外网时用）
    """
    ep = _latest_strict(request, program_id, user, token, id or uid, ext)
    if not ep.get("audio_file"):
        raise HTTPException(404, "这一期没有音频文件")
    return RedirectResponse(
        url=f"{_base_url(request)}/audio/{ep['audio_file']}",
        status_code=302,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/latest")
async def latest_info(request: Request, program_id: str | None = None,
                      user: str | None = None, token: str | None = None,
                      id: str | None = None, uid: str | None = None,
                      ext: str | None = None):
    """最新一期的最小信息集（不含文稿），方便 Home Assistant 的 REST sensor 用。

    参数同 /latest.mp3：program_id 选节目，ext= 外部调用令牌（推荐），
    user 选账号（需同时带 6 位用户 ID），token 走正式鉴权。
    """
    ep = _latest_strict(request, program_id, user, token, id or uid, ext)
    program = cfg.get_program(ep.get("program_id") or "")
    base = _base_url(request)
    return {
        "id": ep.get("id"),
        "title": ep.get("title"),
        "program_id": ep.get("program_id"),
        "program_name": program.name if program else "",
        "seq": ep.get("seq"),
        "user_id": ep.get("user_id"),
        "created_at": ep.get("created_at"),
        "duration": ep.get("duration"),
        "item_count": ep.get("item_count"),
        "url": f"{base}/audio/{ep['audio_file']}",
        "latest_mp3": f"{base}/latest.mp3",
        "feed": f"{base}/feed.xml",
    }


@app.get("/api/latest/content")
async def latest_content(request: Request, program_id: str | None = None,
                         user: str | None = None, token: str | None = None,
                         id: str | None = None, uid: str | None = None,
                         ext: str | None = None):
    """最新一期的**内容明细**：分类、标题、正文、来源、原文链接、播报时间点。

    鉴权/参数与 /api/latest 完全一致（ext= 外部调用令牌 / token 或 ?user=账号&id=6位ID 或访客）。
    给 Home Assistant、自动化脚本、第三方 App 取「这一期都有什么新闻」用。
    """
    ep = _latest_strict(request, program_id, user, token, id or uid, ext)
    program = cfg.get_program(ep.get("program_id") or "")
    script = ep.get("script") or {}
    segs = script.get("segments") or []
    chapters = {c.get("index"): c for c in (script.get("chapters") or [])}
    items: list[dict] = []
    weather = None
    for i, seg in enumerate(segs):
        ch = chapters.get(i) or {}
        if seg.get("type") == "weather":
            weather = {
                "text": seg.get("text"),
                "start": ch.get("start"),
            }
        elif seg.get("type") == "item":
            cat = seg.get("category") or ""
            # 口播里的「第N条，」编号只对耳朵有意义，文本展示是多余的 → 输出给外部系统时剥掉
            body = _strip_item_no(seg.get("text") or "")
            items.append({
                "n": len(items) + 1,
                "category": cat,
                "category_label": cfg.category_label(cat),
                "title": seg.get("title") or "",
                "text": body,
                "source": seg.get("source") or "",
                "sources": seg.get("sources") or [],
                "link": seg.get("link") or "",
                "published": seg.get("published"),
                "start": ch.get("start"),
            })
    base = _base_url(request)
    return {
        "id": ep.get("id"),
        "title": ep.get("title"),
        "program_id": ep.get("program_id"),
        "program_name": program.name if program else "",
        "seq": ep.get("seq"),
        "created_at": ep.get("created_at"),
        "duration": ep.get("duration"),
        "item_count": len(items),
        "weather": weather,
        "items": items,
        # 简洁清单（每条一行「- 标题」，类似热榜卡片样式，给 HA 通知/卡片直接用；
        # 条与条之间空一行——Markdown 里单换行不换行，会挤成一坨）
        "list_md": "\n\n".join(f"- {it['title']}" for it in items if it["title"]),
        "url": f"{base}/audio/{ep['audio_file']}" if ep.get("audio_file") else "",
        "latest_mp3": f"{base}/latest.mp3",
    }


def _strip_item_no(text: str) -> str:
    """去掉口播稿开头的「第N条，」编号（中文数字/阿拉伯数字都兼容）。"""
    return re.sub(r"^第[〇零一二三四五六七八九十百\d]+条[，,、：:\s]*", "", text or "").strip()


def _podcast_xml(base: str, episodes: list[dict], title: str, descr: str) -> str:
    """把节目列表拼成标准播客 RSS（/feed.xml 与每个账号的 /feed/<令牌>.xml 共用）。"""
    items = []
    for e in episodes:
        ep = store.get(e["id"]) or {}
        script = ep.get("script") or {}
        desc_segs = [_strip_item_no(s.get("text") or "") for s in script.get("segments", []) if s.get("type") == "item"]
        description = "\n".join(f"· {t}" for t in desc_segs[:20] if t) or "本期暂无内容"
        pub = e.get("created_at") or ""
        try:
            dt = datetime.fromisoformat(pub).replace(tzinfo=CST)
            pub_rfc = format_datetime(dt)
        except Exception:
            pub_rfc = format_datetime(datetime.now(CST))
        url = f"{base}/audio/{e['audio_file']}"
        items.append(
            f"""    <item>
      <title>{escape(e['title'] or '')}</title>
      <description>{escape(description)}</description>
      <pubDate>{pub_rfc}</pubDate>
      <guid isPermaLink="false">tnews-{e['id']}</guid>
      <enclosure url="{escape(url)}" type="audio/mpeg" length="{os.path.getsize(AUDIO_DIR / e['audio_file']) if (AUDIO_DIR / e['audio_file']).exists() else 0}"/>
      <itunes:duration>{int(e.get('duration') or 0)}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>"""
        )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(title)}</title>
    <link>{escape(base)}</link>
    <language>zh-cn</language>
    <description>{escape(descr)}</description>
    <itunes:author>TNews</itunes:author>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>"""


@app.get("/feed.xml")
async def podcast_feed(request: Request):
    """标准播客 RSS：可直接在小宇宙 / Apple Podcasts / 通用播客 App 里订阅。

    这是「站点合集」旧地址（所有账号的节目），为了不打断已经订上的 App 一直保留。
    每个账号自己的地址是 /feed/<订阅令牌>.xml，只装这个账号的节目。
    """
    base = _base_url(request)
    if not _guest_home()["allowed"]:
        # 访客收听已关闭：站点合集源一并收起（各账号自己的 /feed/<令牌>.xml 不受影响）
        raise HTTPException(403, "访客收听已关闭：请使用账号自己的订阅地址 /feed/<令牌>.xml")
    xml = _podcast_xml(base, store.list(limit=50), cfg.app_name,
                       "每天上下班路上的国内国际热点新闻简讯")
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


@app.get("/feed/{name}")
async def podcast_feed_by_account(request: Request, name: str):
    """每个账号自己的播客源：/feed/<播客令牌>.xml。

    播客订阅有自己的一套开关：管理员总开关（allow_podcast）→ 令牌有效 →
    账号未禁用 → 该账号自己开着播客订阅。任意一层关掉，地址立即失效。
    只收这个账号自己生成的节目。
    """
    tok = name[:-4] if name.lower().endswith(".xml") else name
    if not _allow_podcast():
        raise HTTPException(403, "管理员已关闭播客订阅（设置 → 账号与数据 → 允许播客订阅）")
    u = store.user_by_feed_token(tok.strip())
    if not u:
        raise HTTPException(403, "播客地址无效：令牌不存在或已被重置")
    if u.get("disabled"):
        raise HTTPException(403, f"账号 {u.get('email')} 已被禁用")
    if not store.get_user_setting(int(u["id"]), "feed_enabled", False):
        raise HTTPException(403, f"账号 {u.get('email')} 已关闭播客订阅")
    base = _base_url(request)
    xml = _podcast_xml(base, store.list(limit=50, user_id=int(u["id"])),
                       f"{cfg.app_name} · {u.get('email')}",
                       f"{u.get('email')} 的 TNews 节目")
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
