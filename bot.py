import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("not-rewards-hub")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()
AD_SMARTLINK_URL = os.getenv("AD_SMARTLINK_URL", "").strip()
AD_POSTBACK_SECRET = os.getenv("AD_POSTBACK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "8080"))

MINING_RATE = Decimal(os.getenv("MINING_RATE_NOT_PER_HOUR", "0.09"))
MINING_HOURS = int(os.getenv("MINING_DURATION_HOURS", "5"))
AD_REWARD = Decimal(os.getenv("AD_REWARD_NOT", "0.20"))
REF_REWARD = Decimal(os.getenv("REFERRAL_REWARD_NOT", "10.00"))
FEE_PERCENT = Decimal(os.getenv("WITHDRAWAL_FEE_PERCENT", "2.00"))
FIRST_ADS = int(os.getenv("FIRST_WITHDRAWAL_ADS", "10"))
LATER_ADS = int(os.getenv("LATER_WITHDRAWAL_ADS", "30"))
MIN_WITHDRAWAL = Decimal(os.getenv("MIN_WITHDRAWAL_NOT", "1.00"))
PAYOUT_ENABLED = os.getenv("PAYOUT_ENABLED", "false").lower() == "true"
PAYOUT_DRY_RUN = os.getenv("PAYOUT_DRY_RUN", "true").lower() == "true"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are missing")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
http: Optional[httpx.AsyncClient] = None
DB_BASE = f"{SUPABASE_URL}/rest/v1"
locks: dict[int, asyncio.Lock] = {}


def q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def get_lock(uid: int) -> asyncio.Lock:
    locks.setdefault(uid, asyncio.Lock())
    return locks[uid]


def headers(prefer: str | None = None) -> dict[str, str]:
    h = {"apikey": SUPABASE_SECRET_KEY, "Authorization": f"Bearer {SUPABASE_SECRET_KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    return h


async def db_request(method: str, table: str, *, params: dict[str, str] | None = None, json: Any = None, prefer: str | None = None):
    assert http is not None
    url = f"{DB_BASE}/{table}"
    r = await http.request(method, url, params=params, json=json, headers=headers(prefer))
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase {method} {table} {r.status_code}: {r.text[:500]}")
    if not r.text:
        return []
    try:
        return r.json()
    except Exception:
        return []


async def get_user(uid: int):
    rows = await db_request("GET", "users", params={"telegram_id": f"eq.{uid}", "limit": "1"})
    return rows[0] if rows else None


async def ensure_user(message: Message, referrer: int | None = None):
    uid = message.from_user.id
    u = await get_user(uid)
    if u:
        return u
    data = {
        "telegram_id": uid,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "referrer_id": referrer if referrer and referrer != uid else None,
    }
    rows = await db_request("POST", "users", json=data, prefer="return=representation")
    u = rows[0]
    if referrer and referrer != uid:
        try:
            await db_request("POST", "referrals", json={"referrer_id": referrer, "referred_id": uid, "reward_not": str(REF_REWARD)}, prefer="return=minimal")
        except Exception as e:
            log.warning("Referral insert skipped: %s", e)
    return u


async def update_user(uid: int, changes: dict[str, Any]):
    return await db_request("PATCH", "users", params={"telegram_id": f"eq.{uid}"}, json=changes, prefer="return=representation")


async def credit(uid: int, amount: Decimal, tx_type: str, description: str, reference_type: str | None = None, reference_id: int | None = None):
    async with get_lock(uid):
        u = await get_user(uid)
        if not u:
            raise RuntimeError("User not found")
        before = Decimal(str(u["balance_not"]))
        after = q(before + amount)
        changes = {"balance_not": str(after)}
        if tx_type == "mining":
            changes["total_mined_not"] = str(q(Decimal(str(u["total_mined_not"])) + amount))
        elif tx_type == "ad":
            changes["total_ad_reward_not"] = str(q(Decimal(str(u["total_ad_reward_not"])) + amount))
        elif tx_type == "referral":
            changes["total_referral_reward_not"] = str(q(Decimal(str(u["total_referral_reward_not"])) + amount))
        await update_user(uid, changes)
        await db_request("POST", "transactions", json={
            "telegram_id": uid, "type": tx_type, "amount_not": str(amount),
            "balance_before_not": str(before), "balance_after_not": str(after),
            "reference_type": reference_type, "reference_id": reference_id, "description": description
        }, prefer="return=minimal")
        return after


async def active_session(uid: int):
    rows = await db_request("GET", "mining_sessions", params={
        "telegram_id": f"eq.{uid}", "status": "eq.active", "order": "started_at.desc", "limit": "1"
    })
    return rows[0] if rows else None


async def qualify_referral(uid: int):
    u = await get_user(uid)
    if not u or not u.get("referrer_id") or u.get("referral_rewarded"):
        return
    # Qualifying action: first successful mining session start.
    ref = int(u["referrer_id"])
    async with get_lock(uid):
        u2 = await get_user(uid)
        if not u2 or u2.get("referral_rewarded"):
            return
        rows = await db_request("GET", "referrals", params={"referred_id": f"eq.{uid}", "limit": "1"})
        if not rows:
            return
        rr = rows[0]
        if rr.get("status") == "rewarded":
            return
        try:
            await credit(ref, REF_REWARD, "referral", "Qualified referral reward", "referral", int(rr["id"]))
            await db_request("PATCH", "referrals", params={"id": f"eq.{rr['id']}"}, json={"status": "rewarded", "qualified_at": iso(now()), "rewarded_at": iso(now())}, prefer="return=minimal")
            await update_user(uid, {"referral_rewarded": True})
            try:
                await bot.send_message(ref, f"🎉 <b>Referral reward</b>\nYou earned <b>{REF_REWARD} NOT</b> from a qualified referral.")
            except Exception:
                pass
        except Exception as e:
            log.error("Referral reward failed: %s", e)


async def start_mining(uid: int):
    async with get_lock(uid):
        if await active_session(uid):
            return False, await active_session(uid)
        start = now()
        end = start + timedelta(hours=MINING_HOURS)
        rows = await db_request("POST", "mining_sessions", json={
            "telegram_id": uid, "started_at": iso(start), "ends_at": iso(end),
            "last_reward_at": iso(start), "reward_rate_not": str(MINING_RATE), "total_reward_not": "0", "status": "active"
        }, prefer="return=representation")
        await qualify_referral(uid)
        return True, rows[0]


async def process_session(s: dict):
    uid = int(s["telegram_id"])
    async with get_lock(uid):
        current = await active_session(uid)
        if not current or int(current["id"]) != int(s["id"]):
            return
        start = datetime.fromisoformat(current["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(current["ends_at"].replace("Z", "+00:00"))
        elapsed = min(now(), end) - start
        hours = max(0, min(MINING_HOURS, int(elapsed.total_seconds() // 3600)))
        already = Decimal(str(current["total_reward_not"])) / MINING_RATE if MINING_RATE else Decimal(0)
        due = max(0, hours - int(already))
        if due > 0:
            amount = q(MINING_RATE * due)
            new_total = q(Decimal(str(current["total_reward_not"])) + amount)
            await credit(uid, amount, "mining", f"Mining reward for {due} completed hour(s)", "mining_session", int(current["id"]))
            await db_request("PATCH", "mining_sessions", params={"id": f"eq.{current['id']}"}, json={"total_reward_not": str(new_total), "last_reward_at": iso(start + timedelta(hours=hours))}, prefer="return=minimal")
            try:
                await bot.send_message(uid, f"⛏️ <b>Mining update</b>\nYou received <b>+{amount} NOT</b>.\nTotal this session: <b>{new_total} NOT</b>.")
            except Exception:
                pass
        if now() >= end:
            await db_request("PATCH", "mining_sessions", params={"id": f"eq.{current['id']}"}, json={"status": "completed", "completed_at": iso(now()), "total_reward_not": str(q(MINING_RATE * MINING_HOURS))}, prefer="return=minimal")
            try:
                await bot.send_message(uid, "✅ <b>Mining session complete.</b>\nStart a new 5-hour session whenever you are ready.")
            except Exception:
                pass


async def mining_worker():
    while True:
        try:
            sessions = await db_request("GET", "mining_sessions", params={"status": "eq.active", "limit": "100"})
            for s in sessions:
                try:
                    await process_session(s)
                except Exception:
                    log.exception("Mining session processing failed")
        except Exception:
            log.exception("Mining worker failed")
        await asyncio.sleep(60)


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛏️ Mining", callback_data="mine"), InlineKeyboardButton(text="💰 Balance", callback_data="balance")],
        [InlineKeyboardButton(text="📺 Sponsored", callback_data="ads"), InlineKeyboardButton(text="🎁 Invite", callback_data="invite")],
        [InlineKeyboardButton(text="📊 Activity", callback_data="stats"), InlineKeyboardButton(text="💸 Withdraw", callback_data="withdraw")],
        [InlineKeyboardButton(text="ℹ️ Guide", callback_data="guide")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Home", callback_data="home")]])


def mining_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Start 5-hour session", callback_data="start_mining")], [InlineKeyboardButton(text="⬅️ Home", callback_data="home")]])


def ad_keyboard():
    rows = []
    if AD_SMARTLINK_URL:
        rows.append([InlineKeyboardButton(text="🌐 Open sponsor", url=AD_SMARTLINK_URL)])
    rows.append([InlineKeyboardButton(text="⬅️ Home", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def wallet_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Home", callback_data="home")]])


def valid_ton_wallet(addr: str) -> bool:
    # Basic safety check only; final address validity is verified before payout.
    return bool(re.fullmatch(r"(?:EQ|UQ)[A-Za-z0-9_-]{46}", addr.strip()))


async def show_home(message: Message):
    u = await get_user(message.from_user.id)
    if not u:
        u = await ensure_user(message)
    await message.answer(
        f"👋 <b>NOT Rewards Hub</b>\n\n💰 Balance: <b>{Decimal(str(u['balance_not'])):.8f} NOT</b>\n⛏️ Mining: <b>{MINING_RATE} NOT/hour</b>\n⏱️ Session: <b>{MINING_HOURS} hours</b>\n\nChoose an option below.",
        reply_markup=main_keyboard()
    )


@dp.message(CommandStart())
async def start_cmd(message: Message):
    ref = None
    args = (message.text or "").split(maxsplit=1)
    if len(args) == 2 and args[1].startswith("ref_"):
        try: ref = int(args[1][4:])
        except ValueError: pass
    await ensure_user(message, ref)
    await show_home(message)


@dp.message(Command("menu"))
async def menu_cmd(message: Message):
    await show_home(message)


@dp.callback_query(F.data == "home")
async def home_cb(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text("<b>NOT Rewards Hub</b>\nChoose an option:", reply_markup=main_keyboard())


@dp.callback_query(F.data == "balance")
async def balance_cb(c: CallbackQuery):
    await c.answer()
    u = await get_user(c.from_user.id)
    await c.message.edit_text(f"💰 <b>Your balance</b>\n\nAvailable: <b>{Decimal(str(u['balance_not'])):.8f} NOT</b>\nMined: {Decimal(str(u['total_mined_not'])):.8f} NOT\nAd rewards: {Decimal(str(u['total_ad_reward_not'])):.8f} NOT\nReferral rewards: {Decimal(str(u['total_referral_reward_not'])):.8f} NOT", reply_markup=back_keyboard())


@dp.callback_query(F.data == "mine")
async def mine_cb(c: CallbackQuery):
    await c.answer()
    s = await active_session(c.from_user.id)
    if s:
        end = datetime.fromisoformat(s["ends_at"].replace("Z", "+00:00"))
        remaining = max(0, int((end - now()).total_seconds()))
        h, rem = divmod(remaining, 3600); m, sec = divmod(rem, 60)
        await c.message.edit_text(f"⛏️ <b>Mining is active</b>\n\nRate: <b>{MINING_RATE} NOT/hour</b>\nRemaining: <b>{h}h {m}m</b>\nSession earned: <b>{Decimal(str(s['total_reward_not'])):.8f} NOT</b>", reply_markup=back_keyboard())
    else:
        await c.message.edit_text(f"⛏️ <b>Mining</b>\n\nEarn <b>{MINING_RATE} NOT/hour</b> for a <b>{MINING_HOURS}-hour</b> session.\nMaximum session reward: <b>{q(MINING_RATE*MINING_HOURS)} NOT</b>.", reply_markup=mining_keyboard())


@dp.callback_query(F.data == "start_mining")
async def start_mine_cb(c: CallbackQuery):
    await c.answer()
    ok, s = await start_mining(c.from_user.id)
    if not ok:
        await c.message.edit_text("⛏️ You already have an active mining session.", reply_markup=back_keyboard())
        return
    await c.message.edit_text(f"🚀 <b>Mining started</b>\n\nRate: <b>{MINING_RATE} NOT/hour</b>\nDuration: <b>{MINING_HOURS} hours</b>\nSession maximum: <b>{q(MINING_RATE*MINING_HOURS)} NOT</b>\n\nThe server tracks the session. You will receive an update after each completed hour.", reply_markup=back_keyboard())


@dp.callback_query(F.data == "ads")
async def ads_cb(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text(f"📺 <b>Sponsored</b>\n\nOpen the sponsor page using the button below.\n\nReward: <b>{AD_REWARD} NOT</b> per <b>verified</b> ad event.\n\n⚠️ A click alone does <b>not</b> create a reward. Credits are added only after an allowed provider verification/postback or admin verification.", reply_markup=ad_keyboard())


@dp.callback_query(F.data == "invite")
async def invite_cb(c: CallbackQuery):
    await c.answer()
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{c.from_user.id}"
    await c.message.edit_text(f"🎁 <b>Invite</b>\n\nShare your personal link:\n<code>{link}</code>\n\nReward: <b>{REF_REWARD} NOT</b> for a qualified referral.\nA referral qualifies only after the invited user starts their first mining session and passes the bot's checks.", reply_markup=back_keyboard())


@dp.callback_query(F.data == "stats")
async def stats_cb(c: CallbackQuery):
    await c.answer()
    u = await get_user(c.from_user.id)
    await c.message.edit_text(f"📊 <b>Activity</b>\n\nMining earned: {Decimal(str(u['total_mined_not'])):.8f} NOT\nAd rewards: {Decimal(str(u['total_ad_reward_not'])):.8f} NOT\nReferral rewards: {Decimal(str(u['total_referral_reward_not'])):.8f} NOT\nVerified ads: <b>{u['verified_ad_count']}</b>\nWithdrawals: <b>{u['withdrawal_count']}</b>", reply_markup=back_keyboard())


@dp.callback_query(F.data == "guide")
async def guide_cb(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text("ℹ️ <b>Guide</b>\n\n1. Start a 5-hour mining session.\n2. Mining rewards are tracked on the server.\n3. Sponsored rewards require a verified ad event.\n4. Invite rewards require a qualified referral.\n5. Withdrawals are reviewed by the admin.\n6. Never send anyone your wallet seed phrase or private key.", reply_markup=back_keyboard())


@dp.callback_query(F.data == "withdraw")
async def withdraw_cb(c: CallbackQuery):
    await c.answer()
    u = await get_user(c.from_user.id)
    count = int(u["withdrawal_count"])
    required = FIRST_ADS if count == 0 else LATER_ADS
    await c.message.edit_text(f"💸 <b>Withdraw</b>\n\nVerified ads required: <b>{required}</b>\nYour verified ads: <b>{u['verified_ad_count']}</b>\nFee: <b>{FEE_PERCENT}%</b>\n\nSend your public TON wallet address in chat to request a withdrawal.\nMinimum withdrawal: <b>{MIN_WITHDRAWAL} NOT</b>.\n\n⚠️ Never send a seed phrase or private key.", reply_markup=back_keyboard())
    await c.message.answer("Reply with your public TON wallet address when you are ready.")


@dp.message(F.text)
async def text_handler(message: Message):
    text = (message.text or "").strip()
    uid = message.from_user.id
    if valid_ton_wallet(text):
        u = await get_user(uid)
        if not u:
            await ensure_user(message); u = await get_user(uid)
        required = FIRST_ADS if int(u["withdrawal_count"]) == 0 else LATER_ADS
        if int(u["verified_ad_count"]) < required:
            await message.answer(f"❌ You need {required} verified ad events. You currently have {u['verified_ad_count']}.")
            return
        balance = Decimal(str(u["balance_not"]))
        if balance < MIN_WITHDRAWAL:
            await message.answer(f"❌ Minimum withdrawal is {MIN_WITHDRAWAL} NOT.")
            return
        fee = q(balance * FEE_PERCENT / Decimal(100))
        net = q(balance - fee)
        rows = await db_request("POST", "withdrawals", json={
            "telegram_id": uid, "withdrawal_number": int(u["withdrawal_count"])+1,
            "gross_amount_not": str(balance), "fee_percent": str(FEE_PERCENT), "fee_amount_not": str(fee),
            "net_amount_not": str(net), "wallet_address": text, "required_verified_ads": required,
            "verified_ads_at_request": int(u["verified_ad_count"]), "status": "pending"
        }, prefer="return=representation")
        wid = int(rows[0]["id"])
        await update_user(uid, {"balance_not": "0", "withdrawal_count": int(u["withdrawal_count"])+1})
        await db_request("POST", "transactions", json={"telegram_id": uid, "type": "withdrawal", "amount_not": str(-balance), "balance_before_not": str(balance), "balance_after_not": "0", "reference_type": "withdrawal", "reference_id": wid, "description": f"Withdrawal request; fee {fee} NOT; net {net} NOT"}, prefer="return=minimal")
        await message.answer(f"✅ <b>Withdrawal request submitted.</b>\nGross: {balance} NOT\nFee: {fee} NOT\nNet: {net} NOT\nRequest ID: <b>{wid}</b>\n\nAdmin approval is required.")
        for aid in ADMIN_IDS:
            try: await bot.send_message(aid, f"💸 <b>New withdrawal #{wid}</b>\nUser: <code>{uid}</code>\nGross: {balance} NOT\nNet: {net} NOT\nWallet: <code>{text}</code>\n\nApprove: /approve {wid}\nReject: /reject {wid}")
            except Exception: pass
    else:
        await message.answer("Use the buttons below.", reply_markup=main_keyboard())


def admin_only(uid: int) -> bool:
    return uid in ADMIN_IDS


@dp.message(Command("withdrawals"))
async def withdrawals_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    rows = await db_request("GET", "withdrawals", params={"status": "eq.pending", "order": "created_at.desc", "limit": "20"})
    if not rows:
        await message.answer("No pending withdrawals."); return
    lines = [f"#{r['id']} | user {r['telegram_id']} | {r['net_amount_not']} NOT | {r['wallet_address']}" for r in rows]
    await message.answer("<b>Pending withdrawals</b>\n" + "\n".join(lines))


@dp.message(Command("approve"))
async def approve_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit(): await message.answer("Usage: /approve ID"); return
    wid = int(parts[1])
    rows = await db_request("GET", "withdrawals", params={"id": f"eq.{wid}", "limit": "1"})
    if not rows: await message.answer("Withdrawal not found."); return
    r = rows[0]
    if r["status"] != "pending": await message.answer("Already processed."); return
    await db_request("PATCH", "withdrawals", params={"id": f"eq.{wid}"}, json={"status": "approved", "approved_at": iso(now())}, prefer="return=minimal")
    await message.answer(f"Approved #{wid}. Payout remains controlled by payout settings.")
    try: await bot.send_message(int(r["telegram_id"]), f"✅ Withdrawal #{wid} approved. Net amount: {r['net_amount_not']} NOT.")
    except Exception: pass


@dp.message(Command("reject"))
async def reject_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit(): await message.answer("Usage: /reject ID"); return
    wid = int(parts[1])
    rows = await db_request("GET", "withdrawals", params={"id": f"eq.{wid}", "limit": "1"})
    if not rows: await message.answer("Withdrawal not found."); return
    r = rows[0]
    if r["status"] != "pending": await message.answer("Already processed."); return
    uid = int(r["telegram_id"])
    gross = Decimal(str(r["gross_amount_not"]))
    u = await get_user(uid)
    before = Decimal(str(u["balance_not"]))
    await update_user(uid, {"balance_not": str(q(before + gross))})
    await db_request("POST", "transactions", json={"telegram_id": uid, "type": "withdrawal_refund", "amount_not": str(gross), "balance_before_not": str(before), "balance_after_not": str(q(before+gross)), "reference_type": "withdrawal", "reference_id": wid, "description": "Rejected withdrawal refund"}, prefer="return=minimal")
    await db_request("PATCH", "withdrawals", params={"id": f"eq.{wid}"}, json={"status": "rejected", "rejected_at": iso(now())}, prefer="return=minimal")
    await message.answer(f"Rejected #{wid} and refunded {gross} NOT.")
    try: await bot.send_message(uid, f"❌ Withdrawal #{wid} was rejected. {gross} NOT was returned to your balance.")
    except Exception: pass


@dp.message(Command("verifyad"))
async def verifyad_cmd(message: Message):
    if not admin_only(message.from_user.id): return
    parts = (message.text or "").split()
    if len(parts) not in (2,3) or not parts[1].isdigit(): await message.answer("Usage: /verifyad USER_ID [EVENT_ID]"); return
    uid = int(parts[1]); event_id = parts[2] if len(parts) == 3 else f"manual-{uid}-{int(now().timestamp())}"
    try:
        await db_request("POST", "ad_events", json={"telegram_id": uid, "provider": "admin", "provider_event_id": event_id, "reward_not": str(AD_REWARD), "status": "verified"}, prefer="return=minimal")
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower(): await message.answer("This event was already credited."); return
        raise
    u = await get_user(uid)
    await credit(uid, AD_REWARD, "ad", "Verified sponsored event reward", "ad_event")
    await update_user(uid, {"verified_ad_count": int(u["verified_ad_count"])+1})
    await message.answer(f"Verified ad credited: {AD_REWARD} NOT to {uid}.")
    try: await bot.send_message(uid, f"📺 <b>Verified ad reward</b>\n+{AD_REWARD} NOT added to your balance.")
    except Exception: pass


async def postback(request: web.Request):
    if AD_POSTBACK_SECRET and request.headers.get("X-Postback-Secret") != AD_POSTBACK_SECRET:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    data = await request.json()
    uid = int(data.get("telegram_id", 0)); event_id = str(data.get("event_id", ""))
    if not uid or not event_id:
        return web.json_response({"ok": False, "error": "telegram_id and event_id required"}, status=400)
    try:
        await db_request("POST", "ad_events", json={"telegram_id": uid, "provider": str(data.get("provider", "monetag")), "provider_event_id": event_id, "reward_not": str(AD_REWARD), "status": "verified", "metadata": data}, prefer="return=minimal")
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            return web.json_response({"ok": True, "duplicate": True})
        raise
    u = await get_user(uid)
    if not u: return web.json_response({"ok": False, "error": "user not found"}, status=404)
    await credit(uid, AD_REWARD, "ad", "Verified provider postback reward", "ad_event")
    await update_user(uid, {"verified_ad_count": int(u["verified_ad_count"])+1})
    try: await bot.send_message(uid, f"📺 <b>Verified ad reward</b>\n+{AD_REWARD} NOT added to your balance.")
    except Exception: pass
    return web.json_response({"ok": True, "reward": str(AD_REWARD)})


async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "not-rewards-hub"})


async def start_http():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/ad/postback", postback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on %s", PORT)
    return runner


async def main():
    global http
    http = httpx.AsyncClient(timeout=20)
    runner = await start_http()
    worker = asyncio.create_task(mining_worker())
    try:
        log.info("NOT Rewards Hub starting")
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await runner.cleanup()
        await http.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
