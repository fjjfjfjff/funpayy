"""
Админ-панель для бота-гаранта (telebot).
Подключается через: import admin_panel  (в bot.py, строка 1815)
Функции по образцу референса: сделки, юзеры, баланс, рассылка, настройки, воркеры.
"""

import bot as b
import sqlite3
import threading
import time
from datetime import datetime

_bot   = b.bot
_lock  = b.db_lock
_conn  = b.conn
_cur   = b.cursor

# ──────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────

def _admin_check(user_id: int) -> bool:
    return b.is_admin(user_id)

def _send_log(title: str, body: str = ""):
    """Отправить лог в LOG_GROUP_ID (если задан)."""
    try:
        chat = b.LOG_GROUP_ID
        if not chat:
            return
        text = f"🛠 <b>{title}</b>"
        if body:
            text += f"\n{body}"
        _bot.send_message(chat, text, parse_mode="HTML")
    except Exception as e:
        print(f"[ADMIN LOG ERROR] {e}")

def _get_active_deals():
    with _lock:
        _cur.execute(
            "SELECT deal_id, seller_username, buyer_id, amount, deal_type, status, created_at "
            "FROM deals WHERE status NOT IN ('completed','closed','cancelled') ORDER BY created_at DESC"
        )
        rows = _cur.fetchall()
    result = []
    for r in rows:
        result.append({
            'deal_id': r[0], 'seller_username': r[1], 'buyer_id': r[2],
            'amount': r[3], 'deal_type': r[4], 'status': r[5], 'created_at': r[6]
        })
    return result

def _get_all_deals_count():
    with _lock:
        _cur.execute("SELECT COUNT(*) FROM deals")
        return _cur.fetchone()[0]

def _get_completed_deals_count():
    with _lock:
        _cur.execute("SELECT COUNT(*) FROM deals WHERE status='completed'")
        return _cur.fetchone()[0]

def _format_deal_status(status: str) -> str:
    return {
        'open':      '🟡 Открыта',
        'paid':      '💳 Оплачена',
        'completed': '✅ Завершена',
        'closed':    '❌ Закрыта',
        'cancelled': '🚫 Отменена',
    }.get(status, status)

def _format_bal(bal: dict) -> str:
    lines = [f"  {k.upper()}: <code>{v}</code>" for k, v in bal.items() if v and float(v) > 0]
    return "\n".join(lines) if lines else "  <i>пусто</i>"

def _set_deal_status(deal_id: str, status: str):
    clean = deal_id.replace('#', '').strip()
    with _lock:
        _cur.execute("UPDATE deals SET status=? WHERE deal_id=?", (status, clean))
        _conn.commit()

def _add_balance_admin(user_id, currency: str, amount: float):
    """Начислить баланс пользователю."""
    col = b.BALANCE_COLUMNS.get(currency.lower())
    if not col:
        return False
    with _lock:
        _cur.execute(f"SELECT user_id FROM balances WHERE user_id=?", (user_id,))
        if not _cur.fetchone():
            _cur.execute(
                "INSERT INTO balances (user_id,ton_balance,rub_balance,star_balance,"
                "usd_balance,eur_balance,kzt_balance,uah_balance,byn_balance,uzs_balance) "
                "VALUES (?,0,0,0,0,0,0,0,0,0)", (user_id,)
            )
        _cur.execute(f"UPDATE balances SET {col}={col}+? WHERE user_id=?", (amount, user_id))
        _conn.commit()
    return True

def _set_balance_admin(user_id, currency: str, new_val: float):
    col = b.BALANCE_COLUMNS.get(currency.lower())
    if not col:
        return False
    with _lock:
        _cur.execute(f"SELECT user_id FROM balances WHERE user_id=?", (user_id,))
        if not _cur.fetchone():
            _cur.execute(
                "INSERT INTO balances (user_id,ton_balance,rub_balance,star_balance,"
                "usd_balance,eur_balance,kzt_balance,uah_balance,byn_balance,uzs_balance) "
                "VALUES (?,0,0,0,0,0,0,0,0,0)", (user_id,)
            )
        _cur.execute(f"UPDATE balances SET {col}=? WHERE user_id=?", (new_val, user_id))
        _conn.commit()
    return True

def _set_user_deals(user_id, count: int):
    with _lock:
        _cur.execute("UPDATE users SET successful_deals=? WHERE user_id=?", (count, user_id))
        _conn.commit()

def _get_all_workers():
    """Список воркеров (ADMIN_IDS)."""
    return list(b.ADMIN_IDS)

# ──────────────────────────────────────────────────────────────
# FSM-состояния (через словарь в памяти)
# ──────────────────────────────────────────────────────────────
_state: dict = {}   # {user_id: {'state': str, 'data': dict}}

def _set_state(uid, state_name, **data):
    _state[uid] = {'state': state_name, 'data': data}

def _get_state(uid):
    return _state.get(uid, {}).get('state')

def _get_data(uid):
    return _state.get(uid, {}).get('data', {})

def _clear_state(uid):
    _state.pop(uid, None)

# ──────────────────────────────────────────────────────────────
# Клавиатуры
# ──────────────────────────────────────────────────────────────

def _kb(*rows):
    kb = b.types.InlineKeyboardMarkup()
    for row in rows:
        if isinstance(row, list):
            kb.row(*row)
        else:
            kb.add(row)
    return kb

def _btn(text, cb):
    return b.types.InlineKeyboardButton(text, callback_data=cb)

def _main_panel_kb():
    return _kb(
        [_btn("📋 Активные сделки", "adm_deals"), _btn("👥 Пользователи", "adm_users")],
        [_btn("🔍 Поиск юзера", "adm_search_user"), _btn("📣 Рассылка", "adm_broadcast")],
        [_btn("📊 Статистика", "adm_stats"), _btn("⚙️ Настройки", "adm_settings")],
        [_btn("❌ Закрыть", "adm_close")],
    )

def _deals_kb(page=0, active_deals=None):
    if active_deals is None:
        active_deals = _get_active_deals()
    per = 8
    total_pages = max(1, (len(active_deals) + per - 1) // per)
    page = max(0, min(page, total_pages - 1))
    chunk = active_deals[page*per:(page+1)*per]
    kb = b.types.InlineKeyboardMarkup(row_width=2)
    for d in chunk:
        status_emoji = {'open':'🟡','paid':'💳','completed':'✅'}.get(d['status'],'•')
        cur = b.CURRENCY_DISPLAY.get(d['deal_type'], d['deal_type'].upper())
        label = f"{status_emoji} #{d['deal_id'][:8]} {d['amount']} {cur}"
        kb.add(_btn(label, f"adm_deal_{d['deal_id']}"))
    nav = []
    if page > 0:
        nav.append(_btn("◀️", f"adm_deals_page_{page-1}"))
    nav.append(_btn(f"{page+1}/{total_pages}", "adm_noop"))
    if page < total_pages - 1:
        nav.append(_btn("▶️", f"adm_deals_page_{page+1}"))
    kb.row(*nav)
    kb.row(_btn("🔍 Поиск по ID", "adm_deals_search"), _btn("◀️ В панель", "adm_panel"))
    return kb

def _deal_actions_kb(deal_id, status):
    kb = b.types.InlineKeyboardMarkup(row_width=1)
    if status == 'open':
        kb.add(_btn("💳 Подтвердить оплату", f"adm_pay_{deal_id}"))
    if status in ('open', 'paid'):
        kb.add(_btn("✅ Завершить сделку", f"adm_complete_{deal_id}"))
    if status not in ('completed', 'closed', 'cancelled'):
        kb.add(_btn("❌ Отменить сделку", f"adm_cancel_{deal_id}"))
    kb.add(_btn("◀️ К списку сделок", "adm_deals"))
    return kb

def _cancel_confirm_kb(deal_id):
    return _kb(
        [_btn("✅ Подтвердить отмену", f"adm_cancel_ok_{deal_id}"),
         _btn("◀️ Назад", f"adm_deal_{deal_id}")]
    )

def _users_kb(page=0):
    users = b.get_all_users_for_admin()
    per = 10
    total_pages = max(1, (len(users) + per - 1) // per)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page*per:(page+1)*per]
    kb = b.types.InlineKeyboardMarkup(row_width=1)
    for u in chunk:
        icon = "🚫" if u['is_banned'] else "✅"
        name = f"@{u['username']}" if u['username'] else str(u['tg_id'])
        kb.add(_btn(f"{icon} {name} | сделок: {u['successful_deals']}", f"adm_user_{u['tg_id']}"))
    nav = []
    if page > 0: nav.append(_btn("◀️", f"adm_users_page_{page-1}"))
    nav.append(_btn(f"{page+1}/{total_pages}", "adm_noop"))
    if page < total_pages - 1: nav.append(_btn("▶️", f"adm_users_page_{page+1}"))
    kb.row(*nav)
    kb.row(_btn("◀️ В панель", "adm_panel"))
    return kb

def _user_actions_kb(tg_id, is_banned):
    ban_label = "🔓 Разбанить" if is_banned else "🚫 Забанить"
    ban_cb    = f"adm_unban_{tg_id}" if is_banned else f"adm_ban_{tg_id}"
    return _kb(
        [_btn("💰 Изменить баланс", f"adm_edit_bal_{tg_id}"), _btn("🤝 Кол-во сделок", f"adm_edit_deals_{tg_id}")],
        [_btn("✉️ Написать юзеру", f"adm_msg_{tg_id}"), _btn(ban_label, ban_cb)],
        [_btn("◀️ К списку", "adm_users")],
    )

def _balance_currency_kb(tg_id):
    kb = b.types.InlineKeyboardMarkup(row_width=2)
    currencies = [("TON","ton"),("RUB","rub"),("STAR","star"),
                  ("USD","usd"),("EUR","eur"),("KZT","kzt"),
                  ("UAH","uah"),("BYN","byn"),("UZS","uzs")]
    for label, cur in currencies:
        kb.add(_btn(label, f"adm_bal_cur_{tg_id}_{cur}"))
    kb.add(_btn("❌ Отмена", f"adm_user_{tg_id}"))
    return kb

def _settings_kb():
    return _kb(
        [_btn("💳 Карта", "adm_set_card"), _btn("👛 TON-кошелёк", "adm_set_ton")],
        [_btn("👤 Имя получателя", "adm_set_cardname"), _btn("🏦 Банк", "adm_set_cardbank")],
        [_btn("🔔 Канал уведомлений", "adm_set_channel"), _btn("🔢 Мин. сделок вывода", "adm_set_min_deals")],
        [_btn("👨‍💼 Менеджер (username)", "adm_set_manager")],
        [_btn("◀️ В панель", "adm_panel")],
    )

def _back_panel_kb():
    return _kb([_btn("◀️ В панель", "adm_panel")])

def _back_kb(cb):
    return _kb([_btn("◀️ Назад", cb)])

# ──────────────────────────────────────────────────────────────
# Форматирование текстов
# ──────────────────────────────────────────────────────────────

def _panel_text():
    active = _get_active_deals()
    total  = _get_all_deals_count()
    done   = _get_completed_deals_count()
    users  = b.get_all_users_for_admin()
    return (
        f"🛠 <b>Панель управления</b>\n\n"
        f"<blockquote>"
        f"👥 <b>Пользователей:</b> <code>{len(users)}</code>\n"
        f"📋 <b>Активных сделок:</b> <code>{len(active)}</code>\n"
        f"✅ <b>Завершённых:</b> <code>{done}</code> из <code>{total}</code>"
        f"</blockquote>"
    )

def _user_card_text(u: dict, bal: dict) -> str:
    banned = "🚫 Заблокирован" if u['is_banned'] else "✅ Активен"
    bal_str = _format_bal(bal)
    return (
        f"👤 <b>Пользователь</b>\n\n"
        f"<blockquote>"
        f"🆔 <b>ID:</b> <code>{u['tg_id']}</code>\n"
        f"📌 <b>Username:</b> @{u['username'] or '—'}\n"
        f"🤝 <b>Сделок:</b> <code>{u['successful_deals']}</code>\n"
        f"📊 <b>Всего сделок:</b> <code>{u['total_deals']}</code>\n"
        f"🔄 <b>Оборот:</b> <code>{u['turnover']}</code>\n"
        f"Статус: {banned}"
        f"</blockquote>\n\n"
        f"💰 <b>Баланс:</b>\n<blockquote>{bal_str}</blockquote>"
    )

def _deal_text(deal: dict) -> str:
    cur = b.CURRENCY_DISPLAY.get(deal.get('deal_type',''), deal.get('deal_type','').upper())
    created = datetime.fromtimestamp(deal.get('created_at', 0)).strftime('%d.%m.%Y %H:%M') if deal.get('created_at') else '—'
    return (
        f"🔗 <b>Сделка</b> <code>#{deal['deal_id']}</code>\n\n"
        f"<blockquote>"
        f"📌 <b>Статус:</b> {_format_deal_status(deal.get('status',''))}\n"
        f"💰 <b>Сумма:</b> <code>{deal.get('amount','?')} {cur}</code>\n"
        f"👤 <b>Продавец:</b> @{deal.get('seller_username','—')}\n"
        f"👥 <b>Покупатель ID:</b> <code>{deal.get('buyer_id') or '—'}</code>\n"
        f"📦 <b>Товар:</b> {str(deal.get('offer','—'))[:200]}\n"
        f"🕐 <b>Создана:</b> {created}"
        f"</blockquote>"
    )

def _settings_text():
    s = b.get_all_settings()
    return (
        f"⚙️ <b>Настройки платформы</b>\n\n"
        f"<blockquote>"
        f"💳 <b>Карта:</b> <code>{s.get('card_number','—')}</code>\n"
        f"👤 <b>Получатель:</b> {s.get('card_name','—')}\n"
        f"🏦 <b>Банк:</b> {s.get('card_bank','—')}\n"
        f"👛 <b>TON:</b> <code>{s.get('ton_wallet','—')}</code>\n"
        f"👨‍💼 <b>Менеджер:</b> @{s.get('manager_username','—')}\n"
        f"🔔 <b>Канал:</b> <code>{s.get('notification_channel','—')}</code>\n"
        f"🔢 <b>Мин. сделок вывода:</b> <code>{s.get('min_deals_withdraw','—')}</code>"
        f"</blockquote>"
    )

# ──────────────────────────────────────────────────────────────
# Отправка / редактирование
# ──────────────────────────────────────────────────────────────

def _send(chat_id, text, kb=None, **kw):
    try:
        _bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, **kw)
    except Exception as e:
        print(f"[ADMIN SEND ERROR] {e}")

def _edit(call, text, kb=None):
    try:
        _bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        _send(call.message.chat.id, text, kb)

# ──────────────────────────────────────────────────────────────
# /admin — команда
# ──────────────────────────────────────────────────────────────

@_bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not _admin_check(message.from_user.id):
        return
    _clear_state(message.from_user.id)
    _send(message.chat.id, _panel_text(), _main_panel_kb())

# ──────────────────────────────────────────────────────────────
# Callback-хендлер (все adm_*)
# ──────────────────────────────────────────────────────────────

@_bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("adm_"))
def admin_callback(call):
    uid  = call.from_user.id
    data = call.data

    if not _admin_check(uid):
        _bot.answer_callback_query(call.id, "⛔ Нет доступа", show_alert=True)
        return

    _bot.answer_callback_query(call.id)

    # ── Главное меню ──
    if data == "adm_panel":
        _clear_state(uid)
        _edit(call, _panel_text(), _main_panel_kb())

    elif data == "adm_close":
        try:
            _bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass

    elif data == "adm_noop":
        pass

    # ── Статистика ──
    elif data == "adm_stats":
        active = _get_active_deals()
        total  = _get_all_deals_count()
        done   = _get_completed_deals_count()
        users  = b.get_all_users_for_admin()
        # Объём по валютам из завершённых сделок
        with _lock:
            _cur.execute("SELECT deal_type, SUM(amount) FROM deals WHERE status='completed' GROUP BY deal_type")
            vol_rows = _cur.fetchall()
        vol_lines = [f"  {b.CURRENCY_DISPLAY.get(r[0],r[0].upper())}: <code>{round(r[1],4)}</code>" for r in vol_rows] or ["  —"]
        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"<blockquote>"
            f"👥 <b>Пользователей:</b> <code>{len(users)}</code>\n"
            f"📋 <b>Активных сделок:</b> <code>{len(active)}</code>\n"
            f"✅ <b>Завершённых:</b> <code>{done}</code> из <code>{total}</code>"
            f"</blockquote>\n\n"
            f"💰 <b>Объём завершённых:</b>\n<blockquote>{'  '.join(vol_lines)}</blockquote>"
        )
        _edit(call, text, _back_panel_kb())

    # ── Список сделок ──
    elif data == "adm_deals" or data.startswith("adm_deals_page_"):
        _clear_state(uid)
        page = 0
        if data.startswith("adm_deals_page_"):
            page = int(data.split("_")[-1])
        active = _get_active_deals()
        text = f"📋 <b>Активные сделки</b>\n<i>Всего: {len(active)}</i>"
        _edit(call, text, _deals_kb(page, active))

    # ── Поиск сделки ──
    elif data == "adm_deals_search":
        _set_state(uid, "adm_deals_search")
        _edit(call, "🔍 <b>Введите ID сделки (или часть ID):</b>",
              _back_kb("adm_deals"))

    # ── Карточка сделки ──
    elif data.startswith("adm_deal_") and not data.startswith("adm_deal_page"):
        deal_id = data[len("adm_deal_"):]
        deal = b.get_deal(deal_id)
        if not deal:
            _bot.answer_callback_query(call.id, "❌ Сделка не найдена", show_alert=True)
            return
        _edit(call, _deal_text(deal), _deal_actions_kb(deal_id, deal.get('status','')))

    # ── Подтвердить оплату ──
    elif data.startswith("adm_pay_"):
        deal_id = data[len("adm_pay_"):]
        deal = b.get_deal(deal_id)
        if not deal:
            return
        _set_deal_status(deal_id, 'paid')
        _send_log("💳 Оплата подтверждена администратором",
                  f"Сделка #{deal_id}\nПродавец: @{deal.get('seller_username','?')}\n"
                  f"Сумма: {deal.get('amount')} {b.CURRENCY_DISPLAY.get(deal.get('deal_type',''),'')}")
        deal = b.get_deal(deal_id)
        _edit(call, _deal_text(deal), _deal_actions_kb(deal_id, deal.get('status','')))
        # Уведомление продавцу
        try:
            _bot.send_message(deal['seller_id'],
                              f"💳 Оплата по сделке <code>#{deal_id}</code> подтверждена администратором.",
                              parse_mode="HTML")
        except Exception: pass

    # ── Завершить сделку ──
    elif data.startswith("adm_complete_"):
        deal_id = data[len("adm_complete_"):]
        deal = b.get_deal(deal_id)
        if not deal:
            return
        b.mark_deal_successful(deal_id)
        _send_log("✅ Сделка завершена администратором",
                  f"Сделка #{deal_id}\nПродавец: @{deal.get('seller_username','?')}\n"
                  f"Сумма: {deal.get('amount')} {b.CURRENCY_DISPLAY.get(deal.get('deal_type',''),'')}")
        try:
            _bot.send_message(deal['seller_id'],
                              f"✅ Сделка <code>#{deal_id}</code> завершена администратором.",
                              parse_mode="HTML")
        except Exception: pass
        if deal.get('buyer_id'):
            try:
                _bot.send_message(deal['buyer_id'],
                                  f"✅ Сделка <code>#{deal_id}</code> завершена администратором.",
                                  parse_mode="HTML")
            except Exception: pass
        deal = b.get_deal(deal_id)
        _edit(call, _deal_text(deal), _deal_actions_kb(deal_id, deal.get('status','')))

    # ── Отменить сделку — подтверждение ──
    elif data.startswith("adm_cancel_") and not data.startswith("adm_cancel_ok_"):
        deal_id = data[len("adm_cancel_"):]
        deal = b.get_deal(deal_id)
        if not deal:
            return
        cur = b.CURRENCY_DISPLAY.get(deal.get('deal_type',''), '')
        _edit(call,
              f"⚠️ <b>Отмена сделки <code>#{deal_id}</code></b>\n\n"
              f"<blockquote>💰 {deal.get('amount')} {cur}\n"
              f"📦 {str(deal.get('offer',''))[:100]}</blockquote>\n\n"
              f"<i>Действие необратимо. Участники будут уведомлены.</i>",
              _cancel_confirm_kb(deal_id))

    # ── Отменить сделку — подтверждено ──
    elif data.startswith("adm_cancel_ok_"):
        deal_id = data[len("adm_cancel_ok_"):]
        deal = b.get_deal(deal_id)
        if not deal:
            return
        _set_deal_status(deal_id, 'cancelled')
        _send_log("❌ Сделка отменена администратором", f"Сделка #{deal_id}")
        for pid in [deal.get('seller_id'), deal.get('buyer_id')]:
            if pid:
                try:
                    _bot.send_message(pid,
                                      f"❌ Сделка <code>#{deal_id}</code> отменена администратором.",
                                      parse_mode="HTML")
                except Exception: pass
        deal = b.get_deal(deal_id)
        _edit(call, _deal_text(deal), _deal_actions_kb(deal_id, deal.get('status','')))

    # ── Список пользователей ──
    elif data == "adm_users" or data.startswith("adm_users_page_"):
        _clear_state(uid)
        page = 0
        if data.startswith("adm_users_page_"):
            page = int(data.split("_")[-1])
        users = b.get_all_users_for_admin()
        text = f"👥 <b>Пользователи</b>\n<i>Всего: {len(users)}</i>"
        _edit(call, text, _users_kb(page))

    # ── Карточка пользователя ──
    elif data.startswith("adm_user_") and not data.startswith("adm_users"):
        tg_id = int(data[len("adm_user_"):])
        u = b.get_user_for_admin(tg_id)
        if not u:
            _bot.answer_callback_query(call.id, "❌ Не найден", show_alert=True)
            return
        bal = b.get_user_balance(tg_id)
        _edit(call, _user_card_text(u, bal), _user_actions_kb(tg_id, u['is_banned']))

    # ── Бан / разбан ──
    elif data.startswith("adm_ban_") or data.startswith("adm_unban_"):
        is_ban = data.startswith("adm_ban_")
        tg_id  = int(data.split("_")[-1])
        b.ensure_user_exists(tg_id)
        b.ban_user(tg_id, is_ban)
        action = "заблокирован 🚫" if is_ban else "разблокирован ✅"
        _bot.answer_callback_query(call.id, f"Пользователь {action}")
        _send_log(f"{'🚫 Бан' if is_ban else '✅ Разбан'}", f"ID: {tg_id}")
        u = b.get_user_for_admin(tg_id)
        if u:
            bal = b.get_user_balance(tg_id)
            _edit(call, _user_card_text(u, bal), _user_actions_kb(tg_id, u['is_banned']))

    # ── Поиск пользователя ──
    elif data == "adm_search_user":
        _set_state(uid, "adm_search_user")
        _edit(call, "🔍 <b>Введите Telegram ID или @username:</b>", _back_kb("adm_panel"))

    # ── Изменить баланс — выбор валюты ──
    elif data.startswith("adm_edit_bal_"):
        tg_id = int(data[len("adm_edit_bal_"):])
        _set_state(uid, "adm_edit_bal", target_id=tg_id)
        bal = b.get_user_balance(tg_id)
        _edit(call,
              f"💰 <b>Изменение баланса</b>\n"
              f"<blockquote>Пользователь: <code>{tg_id}</code>\n"
              f"Текущий баланс:\n{_format_bal(bal)}</blockquote>\n\n"
              f"Выберите валюту:",
              _balance_currency_kb(tg_id))

    # ── Изменить баланс — выбрана валюта ──
    elif data.startswith("adm_bal_cur_"):
        parts    = data.split("_")
        currency = parts[-1]
        tg_id    = int(parts[-2])
        cur_bal  = b.get_user_balance(tg_id).get(currency.lower(), 0)
        _set_state(uid, "adm_set_bal_amount", target_id=tg_id, currency=currency)
        _edit(call,
              f"💰 <b>Баланс {currency}</b>\n"
              f"<blockquote>Пользователь: <code>{tg_id}</code>\n"
              f"Текущий: <code>{cur_bal}</code> {currency}</blockquote>\n\n"
              f"Введите новое значение или <code>+5</code> / <code>-3</code>:",
              _back_kb(f"adm_user_{tg_id}"))

    # ── Редактировать кол-во сделок ──
    elif data.startswith("adm_edit_deals_"):
        tg_id   = int(data[len("adm_edit_deals_"):])
        u       = b.get_user_for_admin(tg_id)
        current = u['successful_deals'] if u else 0
        _set_state(uid, "adm_edit_deals", target_id=tg_id)
        _edit(call,
              f"🤝 <b>Количество сделок</b>\n"
              f"<blockquote>Пользователь: <code>{tg_id}</code>\n"
              f"Текущее: <code>{current}</code></blockquote>\n\n"
              f"Введите новое значение или <code>+5</code> / <code>-1</code>:",
              _back_kb(f"adm_user_{tg_id}"))

    # ── Написать юзеру ──
    elif data.startswith("adm_msg_"):
        tg_id = int(data[len("adm_msg_"):])
        _set_state(uid, "adm_msg_user", target_id=tg_id)
        _edit(call,
              f"✉️ <b>Сообщение пользователю</b>\n"
              f"<blockquote>ID: <code>{tg_id}</code></blockquote>\n\n"
              f"Отправьте любое сообщение (текст, фото, видео):",
              _back_kb(f"adm_user_{tg_id}"))

    # ── Рассылка ──
    elif data == "adm_broadcast":
        _set_state(uid, "adm_broadcast")
        users = b.get_all_users_for_admin()
        _edit(call,
              f"📣 <b>Рассылка</b>\n\n"
              f"<blockquote>Сообщение получат все <b>{len(users)}</b> пользователей.</blockquote>\n\n"
              f"Отправьте любое сообщение (текст, фото, видео, GIF):",
              _back_kb("adm_panel"))

    # ── Настройки ──
    elif data == "adm_settings":
        _edit(call, _settings_text(), _settings_kb())

    elif data in ("adm_set_card", "adm_set_ton", "adm_set_cardname",
                  "adm_set_cardbank", "adm_set_channel", "adm_set_min_deals", "adm_set_manager"):
        prompts = {
            "adm_set_card":     ("card_number",           "💳 Введите номер карты платформы:"),
            "adm_set_ton":      ("ton_wallet",            "👛 Введите TON-адрес платформы:"),
            "adm_set_cardname": ("card_name",             "👤 Введите имя получателя:"),
            "adm_set_cardbank": ("card_bank",             "🏦 Введите название банка:"),
            "adm_set_channel":  ("notification_channel",  "🔔 Введите ID канала уведомлений:"),
            "adm_set_min_deals":("min_deals_withdraw",    "🔢 Введите минимальное кол-во сделок для вывода:"),
            "adm_set_manager":  ("manager_username",      "👨‍💼 Введите username менеджера (без @):"),
        }
        key, prompt = prompts[data]
        _set_state(uid, "adm_save_setting", setting_key=key)
        _edit(call, prompt, _back_kb("adm_settings"))


# ──────────────────────────────────────────────────────────────
# Message handler для FSM-состояний
# ──────────────────────────────────────────────────────────────

@_bot.message_handler(
    func=lambda m: _get_state(m.from_user.id) is not None and _admin_check(m.from_user.id),
    content_types=['text', 'photo', 'video', 'animation', 'document']
)
def admin_fsm_handler(message):
    uid   = message.from_user.id
    state = _get_state(uid)
    data  = _get_data(uid)

    # ── Сохранить настройку ──
    if state == "adm_save_setting":
        key = data.get("setting_key")
        val = (message.text or "").strip()
        b.set_setting(key, val)
        _clear_state(uid)
        _send(uid,
              f"✅ Сохранено: <code>{key}</code> = <code>{val}</code>",
              _settings_kb())

    # ── Поиск пользователя ──
    elif state == "adm_search_user":
        _clear_state(uid)
        identifier = (message.text or "").strip()
        u_data = b.get_user_by_id_or_username(identifier)
        if not u_data:
            _send(uid, "❌ Пользователь не найден.", _back_panel_kb())
            return
        tg_id = u_data['user_id'] if isinstance(u_data, dict) else u_data[0]
        u = b.get_user_for_admin(tg_id)
        if not u:
            _send(uid, "❌ Не удалось загрузить данные.", _back_panel_kb())
            return
        bal = b.get_user_balance(tg_id)
        _send(uid, _user_card_text(u, bal), _user_actions_kb(tg_id, u['is_banned']))

    # ── Поиск сделки ──
    elif state == "adm_deals_search":
        _clear_state(uid)
        search = (message.text or "").strip().lower()
        active = _get_active_deals()
        found  = [d for d in active if search in d['deal_id'].lower()]
        if not found:
            _send(uid, f"❌ Сделки с ID <code>{search}</code> не найдены.", _back_kb("adm_deals"))
            return
        text = f"🔍 <b>Результат поиска:</b> <code>{search}</code>"
        _send(uid, text, _deals_kb(0, found))

    # ── Установить баланс ──
    elif state == "adm_set_bal_amount":
        tg_id    = data.get("target_id")
        currency = data.get("currency")
        raw      = (message.text or "").strip()
        cur_bal  = b.get_user_balance(tg_id).get(currency.lower(), 0)
        try:
            if raw.startswith("+"):
                new_val = round(float(cur_bal) + float(raw[1:]), 8)
            elif raw.startswith("-"):
                new_val = round(float(cur_bal) - float(raw[1:]), 8)
            else:
                new_val = round(float(raw), 8)
            if new_val < 0:
                raise ValueError("negative")
        except (ValueError, TypeError):
            _send(uid, "❌ Неверный формат. Введите число, +N или -N.")
            return
        _clear_state(uid)
        _set_balance_admin(tg_id, currency, new_val)
        _send_log("💰 Баланс изменён администратором",
                  f"Пользователь: {tg_id}\n{currency}: {cur_bal} → {new_val}")
        _send(uid,
              f"✅ <b>Баланс обновлён!</b>\n"
              f"<blockquote>Пользователь: <code>{tg_id}</code>\n"
              f"{currency}: <code>{cur_bal}</code> → <code>{new_val}</code></blockquote>",
              _user_actions_kb(tg_id, b.get_is_banned(tg_id)))

    # ── Установить кол-во сделок ──
    elif state == "adm_edit_deals":
        tg_id   = data.get("target_id")
        raw     = (message.text or "").strip()
        u       = b.get_user_for_admin(tg_id)
        current = u['successful_deals'] if u else 0
        try:
            if raw.startswith("+"):
                new_val = current + int(raw[1:])
            elif raw.startswith("-"):
                new_val = current - int(raw[1:])
            else:
                new_val = int(raw)
            if new_val < 0:
                raise ValueError
        except (ValueError, TypeError):
            _send(uid, "❌ Введите целое неотрицательное число.")
            return
        _clear_state(uid)
        _set_user_deals(tg_id, new_val)
        _send_log("🤝 Кол-во сделок изменено", f"Пользователь: {tg_id}\n{current} → {new_val}")
        _send(uid,
              f"✅ <b>Сделки обновлены!</b>\n"
              f"<blockquote>Пользователь: <code>{tg_id}</code>\n"
              f"Было: <code>{current}</code> → Стало: <code>{new_val}</code></blockquote>",
              _user_actions_kb(tg_id, b.get_is_banned(tg_id)))

    # ── Написать юзеру ──
    elif state == "adm_msg_user":
        tg_id = data.get("target_id")
        _clear_state(uid)
        try:
            _bot.copy_message(tg_id, message.chat.id, message.message_id)
            _send(uid, f"✅ Сообщение отправлено пользователю <code>{tg_id}</code>.",
                  _user_actions_kb(tg_id, b.get_is_banned(tg_id)))
        except Exception as e:
            if "Forbidden" in str(e):
                _send(uid, f"❌ Пользователь <code>{tg_id}</code> заблокировал бота.")
            else:
                _send(uid, f"❌ Ошибка: {e}")

    # ── Рассылка ──
    elif state == "adm_broadcast":
        _clear_state(uid)
        users = b.get_all_users_for_admin()
        _send(uid, f"📣 Начинаю рассылку {len(users)} пользователям...")
        ok = fail = 0
        for u in users:
            try:
                _bot.copy_message(u['tg_id'], message.chat.id, message.message_id)
                ok += 1
            except Exception:
                fail += 1
        _send(uid,
              f"📊 <b>Рассылка завершена!</b>\n"
              f"<blockquote>✅ Успешно: <code>{ok}</code>\n"
              f"❌ Ошибок: <code>{fail}</code></blockquote>",
              _back_panel_kb())

