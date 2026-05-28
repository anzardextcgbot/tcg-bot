import sqlite3
import requests
import json
import matplotlib.pyplot as plt
import re
from urllib.parse import urljoin

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
   Application,
   CommandHandler,
   ContextTypes,
   MessageHandler,
   filters,
   CallbackQueryHandler,
   JobQueue
)

import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
last_search_results = {}

def load_set_aliases():
    try:
        with open("set_aliases.json", "r", encoding="utf-8") as file:
            return json.load(file)
    except:
        return {}

SET_ALIASES = load_set_aliases()

conn = sqlite3.connect("tcg.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS favorite_sets (
    user_id TEXT,
    set_name TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS price_history (
    card_name TEXT,
    price REAL,
    checked_at TEXT
)
""")

conn.commit()
cursor.execute("""
CREATE TABLE IF NOT EXISTS tracked_cards (
    user_id TEXT,
    card_name TEXT
)
""")

conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT,
    alert_threshold REAL,
    only_drops TEXT
)
""")

conn.commit()
cursor.execute("""
CREATE TABLE IF NOT EXISTS tracked_urls (
    user_id TEXT,
    url TEXT
)
""")

conn.commit()
cursor.execute("""
CREATE TABLE IF NOT EXISTS restock_status (
    url TEXT,
    last_status TEXT
)
""")

conn.commit()
cursor.execute("""
CREATE TABLE IF NOT EXISTS sent_price_alerts (
    card_name TEXT,
    last_price REAL
)
""")

conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS tracked_products (
    user_id TEXT,
    product_query TEXT
)
""")

conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS tracked_shop_urls (
    user_id TEXT,
    product_name TEXT,
    shop_url TEXT
)
""")

conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS global_shop_products (
    product_name TEXT,
    shop_name TEXT,
    shop_url TEXT
)
""")

conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS sent_restock_alerts (
    product_name TEXT,
    shop_name TEXT,
    status TEXT
)
""")

conn.commit()

def search_pokemon_card(card_name, set_name=None):
    url = "https://api.pokemontcg.io/v2/cards"

    query = f'name:"{card_name}"'

    if set_name:
        if set_name == "scarlet & violet 151":
            query += ' set.id:sv3pt5'
        else:
            query += f' set.name:"{set_name}"'

    params = {
        "q": query,
        "pageSize": 50
    }

    response = requests.get(url, params=params)

    if response.status_code != 200:
        return []

    data = response.json()

    return data.get("data", [])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["🃏 Karte suchen", "📦 Sets"],
        ["⭐ Favoriten", "🔔 Alerts"],
        ["📈 Preise"]
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(
        "🃏 Willkommen bei AnzarDex TCG Bot\n\n"
        "Dein Pokémon Preis-Tracker 🔥",
        reply_markup=reply_markup
    )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔍 Karten suchen":
        await update.message.reply_text(
            "🔍 Sende einfach einen Kartennamen.\n\n"
            "Beispiele:\n"
            "charizard 151\n"
            "umbreon vmax evolving skies"
        )

    elif text == "⭐ Tracking":
        await update.message.reply_text(
            "⭐ Tracking-Menü\n\n"
            "/mycards\n"
            "/untrackcards"
        )

    elif text == "📈 Preise":
        await update.message.reply_text(
            "📈 Preis-Menü\n\n"
            "/preishistory Charizard"
        )

    else:
        text_lower = text.lower()

        product_keywords = [
            "etb",
            "display",
            "booster",
            "bundle",
            "mini tin",
            "upc",
            "case",
            "collection"
        ]

        is_product = False

        for keyword in product_keywords:
            if keyword in text_lower:
                is_product = True
                break

        context.args = update.message.text.split()

        if is_product:
            await product_search(update, context)
        else:
            await preis(update, context)

async def preis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)

    if not query:
        await update.message.reply_text("Benutze: /preis pikachu")
        return

    search_words = query.lower().split()

    query_lower = query.lower()
    CARD_SEARCH_COUNT[query_lower] = CARD_SEARCH_COUNT.get(query_lower, 0) + 1
    matched_set = None
    best_match = ""

    for set_name in ALL_SETS.keys():
        if set_name in query_lower and len(set_name) > len(best_match):
            best_match = set_name

    if best_match:
        matched_set = best_match

    if "151" in query_lower:
        matched_set = "scarlet & violet 151"

    card_name_words = []

    for word in search_words:
        if matched_set and word in matched_set:
            break

        if word == "151":
            break

        card_name_words.append(word)

    card_name = " ".join(card_name_words)

    cards = search_pokemon_card(card_name, matched_set)

    filtered_cards = []

    for card in cards:
        card_text = (
            f"{card.get('name', '')} "
            f"{card.get('set', {}).get('name', '')} "
            f"{card.get('number', '')}"
        ).lower()

        score = 0

        if card.get("name", "").lower() == card_name.lower():
            score += 10

        for word in search_words:
            if word in card_text:
                score += 1

                set_name = card.get("set", {}).get("name", "").lower()

                if matched_set and matched_set in set_name:
                    score += 10

                card_number = card.get("number", "").lower()

                if word in set_name:
                    score += 3

                if word == card_number:
                    score += 5

        filtered_cards.append((score, card))

    filtered_cards.sort(reverse=True, key=lambda x: x[0])

    cards = [card for score, card in filtered_cards[:5]]
    context.user_data["last_cards"] = cards

    user_id = str(update.message.from_user.id)
    last_search_results[user_id] = cards

    if not cards:
        await update.message.reply_text("Keine Karte gefunden.")
        return

    if len(cards) == 1:
        await send_card_details(update.message, cards[0])
        return

    keyboard = []

    for index, card in enumerate(cards, start=1):
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{index}. {card.get('name')} | {card.get('set', {}).get('name', 'Unbekannt')} | #{card.get('number', '?')} | {card.get('cardmarket', {}).get('prices', {}).get('trendPrice', '?')}€",
                    callback_data=f"select_{index}"
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🔍 Ergebnisse für: {query}",
        reply_markup=reply_markup
    )

async def select_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id not in last_search_results:
        await update.message.reply_text("Bitte suche zuerst eine Karte mit /preis pikachu")
        return

    if not context.args:
        await update.message.reply_text("Benutze: /select 1")
        return

    try:
        choice = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Bitte gib eine Zahl ein, z.B. /select 1")
        return

    await send_selected_card(update, user_id, choice)


async def button_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cards = context.user_data.get("last_cards")

    if not cards:
        await query.message.reply_text("Bitte suche zuerst eine Karte.")
        return

    try:
        choice = int(query.data.replace("select_", ""))
    except:
        await query.message.reply_text("Ungültige Auswahl.")
        return

    if choice < 1 or choice > len(cards):
        await query.message.reply_text("Diese Karte gibt es nicht.")
        return

    card = cards[choice - 1]

    await send_card_details(query.message, card)


async def send_card_details(message, card):
    name = card.get("name")
    set_name = card.get("set", {}).get("name")
    number = card.get("number", "?")
    rarity = card.get("rarity", "Unbekannt")

    image_url = card.get("images", {}).get("large")

    prices = card.get("cardmarket", {}).get("prices", {})

    trend_price = prices.get("trendPrice", "Keine Daten")
    low_price = prices.get("lowPrice", "Keine Daten")
    average_sell_price = prices.get("averageSellPrice", "Keine Daten")

    text = (
        f"🃏 <b>{name}</b>\n\n"
        f"📦 <b>Set:</b> {set_name}\n"
        f"#️⃣ <b>Nummer:</b> {number}\n"
        f"✨ <b>Seltenheit:</b> {rarity}\n\n"
        f"💰 <b>Low Price:</b> {low_price} €\n"
        f"📉 <b>Trend Price:</b> {trend_price} €\n"
        f"📊 <b>Durchschnitt:</b> {average_sell_price} €"
    )

    cardmarket_url = card.get("cardmarket", {}).get("url")

    keyboard = [
        [
            InlineKeyboardButton(
                "⭐ Track / Untrack",
                callback_data=f"track_{name}"
            ),
            InlineKeyboardButton(
                "📈 Verlauf",
                callback_data=f"history_{name}"
            )
        ]
    ]

    if cardmarket_url:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🛒 Cardmarket",
                    url=cardmarket_url
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    if image_url:
        await message.reply_photo(
            photo=image_url,
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    else:
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup
        )

async def set_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)

    if not set_name:
        await update.message.reply_text("Benutze: /set 151")
        return

    url = "https://api.pokemontcg.io/v2/cards"

    params = {
        "q": f'set.name:"{set_name}"',
        "pageSize": 20
    }

    response = requests.get(url, params=params)
    data = response.json()

    cards = data.get("data", [])

    if not cards:
        await update.message.reply_text("Kein Set gefunden.")
        return

    text = f"📦 Karten aus {set_name}\n\n"

    for index, card in enumerate(cards, start=1):
        name = card.get("name")
        number = card.get("number")

        text += f"{index}. {name} #{number}\n"

    await update.message.reply_text(text)


async def favset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)

    if not set_name:
        await update.message.reply_text("Benutze: /favset 151")
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT * FROM favorite_sets WHERE user_id = ? AND set_name = ?",
        (user_id, set_name)
    )

    existing = cursor.fetchone()

    if existing:
        await update.message.reply_text("⭐ Dieses Set ist bereits gespeichert.")
        return

    cursor.execute(
        "INSERT INTO favorite_sets (user_id, set_name) VALUES (?, ?)",
        (user_id, set_name)
    )

    conn.commit()

    await update.message.reply_text(f"⭐ Set gespeichert: {set_name}")


async def meinesets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT set_name FROM favorite_sets WHERE user_id = ?",
        (user_id,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text("Du hast noch keine Favoriten.")
        return

    text = "⭐ Deine Lieblingssets\n\n"

    for index, row in enumerate(results, start=1):
        text += f"{index}. {row[0]}\n"

    await update.message.reply_text(text)


async def unfavset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)

    if not set_name:
        await update.message.reply_text("Benutze: /unfavset 151")
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        "DELETE FROM favorite_sets WHERE user_id = ? AND set_name = ?",
        (user_id, set_name)
    )

    conn.commit()

    await update.message.reply_text(f"❌ Set entfernt: {set_name}")


async def preishistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_name = " ".join(context.args)

    if not card_name:
        await update.message.reply_text("Benutze: /preishistory pikachu")
        return

    cursor.execute(
        """
        SELECT price, checked_at
        FROM price_history
        WHERE card_name = ?
        ORDER BY checked_at DESC
        LIMIT 5
        """,
        (card_name,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text("Noch keine Preise gespeichert.")
        return

    text = f"📈 Preisverlauf für {card_name}\n\n"

    for price, checked_at in results:
        text += f"💰 {price} € — {checked_at}\n"

    await update.message.reply_text(text)


async def checkprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_name = " ".join(context.args)

    if not card_name:
        await update.message.reply_text("Benutze: /checkprice pikachu")
        return

    cards = search_pokemon_card(card_name)

    if not cards:
        await update.message.reply_text("Keine Karte gefunden.")
        return

    card = cards[0]
    name = card.get("name")

    prices = card.get("cardmarket", {}).get("prices", {})
    new_price = prices.get("trendPrice")

    if not new_price:
        await update.message.reply_text("Für diese Karte gibt es keine Preisdaten.")
        return

    cursor.execute(
        """
        SELECT price
        FROM price_history
        WHERE card_name = ?
        ORDER BY checked_at DESC
        LIMIT 1
        """,
        (name,)
    )

    result = cursor.fetchone()

    save_price(name, new_price)

    if not result:
        await update.message.reply_text(
            f"📈 Erster Preis gespeichert für {name}: {new_price} €"
        )
        return

    old_price = result[0]
    difference = round(new_price - old_price, 2)

    if difference > 0:
        status = f"📈 Gestiegen um {difference} €"
    elif difference < 0:
        status = f"📉 Gefallen um {abs(difference)} €"
    else:
        status = "➖ Keine Änderung"

    await update.message.reply_text(
        f"🃏 {name}\n\n"
        f"Alter Preis: {old_price} €\n"
        f"Neuer Preis: {new_price} €\n\n"
        f"{status}"
    )

async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_name = " ".join(context.args)

    if not card_name:
        await update.message.reply_text(
            "Benutze: /track charizard"
        )
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT * FROM tracked_cards WHERE user_id = ? AND card_name = ?",
        (user_id, card_name)
    )

    existing = cursor.fetchone()

    if existing:
        await update.message.reply_text(
            "🃏 Diese Karte wird bereits beobachtet."
        )
        return

    cursor.execute(
        "INSERT INTO tracked_cards (user_id, card_name) VALUES (?, ?)",
        (user_id, card_name)
    )

    conn.commit()

    await update.message.reply_text(
        f"✅ Karte wird beobachtet: {card_name}"
    )


async def meinekarten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT card_name FROM tracked_cards WHERE user_id = ?",
        (user_id,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text(
            "Du beobachtest noch keine Karten."
        )
        return

    text = "🃏 Deine beobachteten Karten\n\n"

    for index, row in enumerate(results, start=1):
        text += f"{index}. {row[0]}\n"

    await update.message.reply_text(text)
async def checktracked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT card_name FROM tracked_cards WHERE user_id = ?",
        (user_id,)
    )

    tracked = cursor.fetchall()

    if not tracked:
        await update.message.reply_text(
            "Du beobachtest keine Karten."
        )
        return

    text = "🔔 Preisprüfung\n\n"

    for row in tracked:
        card_name = row[0]

        cards = search_pokemon_card(card_name)

        if not cards:
            continue

        card = cards[0]

        name = card.get("name")

        prices = card.get("cardmarket", {}).get("prices", {})
        new_price = prices.get("trendPrice")

        if not new_price:
            continue

        cursor.execute(
            """
            SELECT price
            FROM price_history
            WHERE card_name = ?
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (name,)
        )

        result = cursor.fetchone()

        save_price(name, new_price)

        if result:
            old_price = result[0]
            difference = round(new_price - old_price, 2)

            if difference > 0:
                status = f"📈 +{difference} €"
            elif difference < 0:
                status = f"📉 -{abs(difference)} €"
            else:
                status = "➖ Keine Änderung"

            text += (
                f"🃏 {name}\n"
                f"💰 {new_price} €\n"
                f"{status}\n\n"
            )

        else:
            text += (
                f"🃏 {name}\n"
                f"💰 {new_price} €\n\n"
            )

    await update.message.reply_text(text)
async def watchsets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT set_name FROM favorite_sets WHERE user_id = ?",
        (user_id,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text("Du hast keine beobachteten Sets.")
        return

    text = "👀 Beobachtete Sets\n\n"

    for row in results:
        set_name = row[0]
        text += f"📦 {set_name}\n"

    text += "\n🔔 Automatische Preisalarme kommen als Nächstes."

    await update.message.reply_text(text)


async def alertcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT set_name FROM favorite_sets WHERE user_id = ?",
        (user_id,)
    )

    sets = cursor.fetchall()

    if not sets:
        await update.message.reply_text("Du hast keine Favoriten.")
        return

    text = "🔔 Preis-Check deiner Favoriten\n\n"

    for row in sets:
        set_name = row[0]

        url = "https://api.pokemontcg.io/v2/cards"

        params = {
            "q": f'set.name:"{set_name}"',
            "pageSize": 5
        }

        response = requests.get(url, params=params)
        data = response.json()

        cards = data.get("data", [])

        text += f"📦 {set_name}\n"

        if not cards:
            text += "Keine Karten gefunden.\n\n"
            continue

        for card in cards:
            name = card.get("name")

            prices = card.get("cardmarket", {}).get("prices", {})
            trend_price = prices.get("trendPrice")

            if trend_price:
                cursor.execute(
                    """
                    SELECT price
                    FROM price_history
                    WHERE card_name = ?
                    ORDER BY checked_at DESC
                    LIMIT 1
                    """,
                    (name,)
                )

                old = cursor.fetchone()

                save_price(name, trend_price)

                if old:
                    old_price = old[0]
                    difference = round(trend_price - old_price, 2)

                    if difference > 0:
                        status = f"📈 +{difference} €"
                    elif difference < 0:
                        status = f"📉 {difference} €"
                    else:
                        status = "➖ Keine Änderung"

                    text += f"🃏 {name}: {trend_price} € {status}\n"

                else:
                    text += f"🃏 {name}: {trend_price} €\n"

        text += "\n"

    await update.message.reply_text(text)
async def alerttest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔔 Preisalarm\n\n"
        "📦 Set: Pokémon 151\n"
        "🃏 Karte: Charizard ex\n\n"
        "Alter Preis: 89.99 €\n"
        "Neuer Preis: 79.99 €\n\n"
        "📉 Gefallen um 10.00 €"
    )
async def auto_price_check(context: ContextTypes.DEFAULT_TYPE):
    print("📉 Automatische Preisprüfung läuft...")

    cursor.execute(
        "SELECT user_id, card_name FROM tracked_cards"
    )

    tracked = cursor.fetchall()

    for row in tracked:
        user_id = row[0]
        card_name = row[1]

        cards = search_pokemon_card(card_name)

        if not cards:
            continue

        card = cards[0]

        name = card.get("name")

        prices = card.get("cardmarket", {}).get("prices", {})
        new_price = prices.get("trendPrice")

        if not new_price:
            continue

        cursor.execute(
            """
            SELECT price
            FROM price_history
            WHERE card_name = ?
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (name,)
        )

        result = cursor.fetchone()

        save_price(name, new_price)

        if not result:
            continue

        old_price = result[0]

        difference = round(new_price - old_price, 2)

        cursor.execute(
            """
            SELECT alert_threshold, only_drops
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,)
        )

        settings = cursor.fetchone()

        threshold = 2
        only_drops = "no"

        if settings:
            if settings[0]:
                threshold = settings[0]

            if settings[1]:
                only_drops = settings[1]

        if abs(difference) < threshold:
            continue

        if only_drops == "yes" and difference > 0:
            continue

        cursor.execute(
            """
            SELECT last_price
            FROM sent_price_alerts
            WHERE card_name = ?
            """,
            (card_name,)
        )

        existing = cursor.fetchone()

        old_alert_price = None

        if existing:
            old_alert_price = existing[0]

        if old_alert_price == new_price:
            continue

        cursor.execute(
            """
            DELETE FROM sent_price_alerts
            WHERE card_name = ?
            """,
            (card_name,)
        )

        cursor.execute(
            """
            INSERT INTO sent_price_alerts (card_name, last_price)
            VALUES (?, ?)
            """,
            (card_name, new_price)
        )

        conn.commit()

        if difference > 0:
            status = f"📈 Gestiegen um {difference} €"
        else:
            status = f"📉 Gefallen um {abs(difference)} €"

        text = (
            f"🔔 Preisalarm\n\n"
            f"🃏 {name}\n"
            f"💰 Neuer Preis: {new_price} €\n\n"
            f"{status}"
        )

        await context.bot.send_message(
            chat_id=user_id,
            text=text
        )
async def setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Benutze: /setalert 5"
        )
        return

    try:
        threshold = float(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "Bitte eine Zahl eingeben."
        )
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT * FROM user_settings WHERE user_id = ?",
        (user_id,)
    )

    existing = cursor.fetchone()

    if existing:
        cursor.execute(
            """
            UPDATE user_settings
            SET alert_threshold = ?
            WHERE user_id = ?
            """,
            (threshold, user_id)
        )
    else:
        cursor.execute(
            """
            INSERT INTO user_settings (user_id, alert_threshold)
            VALUES (?, ?)
            """,
            (user_id, threshold)
        )

    conn.commit()

    await update.message.reply_text(
        f"✅ Alert-Grenze gesetzt auf {threshold} €"
    )
async def setdrops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Benutze: /setdrops on oder /setdrops off"
        )
        return

    value = context.args[0].lower()

    if value not in ["on", "off"]:
        await update.message.reply_text(
            "Bitte nutze: /setdrops on oder /setdrops off"
        )
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        "SELECT * FROM user_settings WHERE user_id = ?",
        (user_id,)
    )

    existing = cursor.fetchone()

    only_drops = "yes" if value == "on" else "no"

    if existing:
        cursor.execute(
            """
            UPDATE user_settings
            SET only_drops = ?
            WHERE user_id = ?
            """,
            (only_drops, user_id)
        )
    else:
        cursor.execute(
            """
            INSERT INTO user_settings (user_id, alert_threshold, only_drops)
            VALUES (?, ?, ?)
            """,
            (user_id, 2, only_drops)
        )

    conn.commit()

    await update.message.reply_text(
        f"✅ Nur Preis-Drops: {'aktiviert' if only_drops == 'yes' else 'deaktiviert'}"
    )
async def trackurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Benutze: /trackurl LINK"
        )
        return

    url = context.args[0]

    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        INSERT INTO tracked_urls (user_id, url)
        VALUES (?, ?)
        """,
        (user_id, url)
    )

    conn.commit()

    await update.message.reply_text(
        "✅ URL wird jetzt überwacht."
    )
async def myurls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        SELECT url
        FROM tracked_urls
        WHERE user_id = ?
        """,
        (user_id,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text(
            "Du beobachtest noch keine URLs."
        )
        return

    text = "🔗 Deine überwachten URLs\n\n"

    for index, row in enumerate(results, start=1):
        text += f"{index}. {row[0]}\n\n"

    await update.message.reply_text(text)

def extract_shop_price(url):

    try:
        response = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        html = response.text

        price_patterns = [
            r"\d+,\d{2}\s?€",
            r"€\s?\d+,\d{2}",
            r"\d+\.\d{2}\s?€"
        ]

        for pattern in price_patterns:
            match = re.search(pattern, html)

            if match:
                price = match.group(0)

                if "0,00" in price or "0.00" in price:
                    continue

                return price

        return "Preis nicht gefunden"

    except Exception:
        return "Preis nicht gefunden"


def check_restock(url):

    try:
        response = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        html = response.text.lower()

        sold_out_words = [
            "out of stock",
            "sold out",
            "nicht verfügbar",
            "ausverkauft",
            "derzeit nicht verfügbar",
            "momentan nicht verfügbar"
        ]

        available_words = [
            "in den warenkorb",
            "add to cart",
            "buy now",
            "kaufen",
            "verfügbar",
            "auf lager"
        ]

        for word in sold_out_words:
            if word in html:
                return False

        for word in available_words:
            if word in html:
                return True

        return None

    except Exception:
        return None
async def checkurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Benutze: /checkurl LINK"
        )
        return

    url = context.args[0]

    result = check_restock(url)

    if result is True:
        await update.message.reply_text(
            "✅ Produkt vermutlich verfügbar!"
        )

    elif result is False:
        await update.message.reply_text(
            "❌ Produkt aktuell ausverkauft."
        )

    else:
        await update.message.reply_text(
            "⚠️ Seite konnte nicht geprüft werden."
        )
async def check_my_urls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        SELECT url
        FROM tracked_urls
        WHERE user_id = ?
        """,
        (user_id,)
    )

    urls = cursor.fetchall()

    if not urls:
        await update.message.reply_text("Du hast keine URLs gespeichert.")
        return

    text = "🔍 URL-Check\n\n"

    for row in urls:
        url = row[0]
        result = check_restock(url)

        if result is True:
            text += f"✅ Verfügbar:\n{url}\n\n"
        elif result is False:
            text += f"❌ Ausverkauft:\n{url}\n\n"
        else:
            text += f"⚠️ Konnte nicht prüfen:\n{url}\n\n"

    await update.message.reply_text(text)

async def auto_restock_check(context: ContextTypes.DEFAULT_TYPE):
    print("🔄 Restock-Check läuft...")

    cursor.execute(
        """
        SELECT user_id, url
        FROM tracked_urls
        """
    )

    rows = cursor.fetchall()

    for row in rows:
        user_id = row[0]
        url = row[1]

        result = check_restock(url)

        cursor.execute(
            """
            SELECT last_status
            FROM restock_status
            WHERE url = ?
            """,
            (url,)
        )

        existing = cursor.fetchone()

        old_status = None

        if existing:
            old_status = existing[0]

        new_status = "available" if result else "soldout"

        if old_status != new_status:
            cursor.execute(
                """
                DELETE FROM restock_status
                WHERE url = ?
                """,
                (url,)
            )

            cursor.execute(
                """
                INSERT INTO restock_status (url, last_status)
                VALUES (?, ?)
                """,
                (url, new_status)
            )

            conn.commit()

            if new_status == "available":
                text = (
                    "🚨 RESTOCK ERKANNT!\n\n"
                    f"{url}"
                )

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text
                )
async def untrackurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Benutze: /untrackurl NUMMER"
        )
        return

    user_id = str(update.effective_user.id)

    try:
        index = int(context.args[0]) - 1
    except:
        await update.message.reply_text(
            "Bitte eine gültige Nummer angeben."
        )
        return

    cursor.execute(
        """
        SELECT url
        FROM tracked_urls
        WHERE user_id = ?
        """,
        (user_id,)
    )

    urls = cursor.fetchall()

    if index < 0 or index >= len(urls):
        await update.message.reply_text(
            "Ungültige Nummer."
        )
        return

    url = urls[index][0]

    cursor.execute(
        """
        DELETE FROM tracked_urls
        WHERE user_id = ? AND url = ?
        """,
        (user_id, url)
    )

    conn.commit()

    await update.message.reply_text(
        "🗑 URL entfernt."
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = str(query.from_user.id)

    if data == "back_main":
        keyboard = [
            [
                InlineKeyboardButton("🔍 Karten", callback_data="menu_cards"),
                InlineKeyboardButton("📦 Produkte", callback_data="menu_products")
            ],
            [
                InlineKeyboardButton("🔔 Restocks", callback_data="menu_restocks"),
                InlineKeyboardButton("📈 Trends", callback_data="menu_trends")
            ],
            [
                InlineKeyboardButton("⭐ Watchlist", callback_data="menu_watchlist")
            ]
        ]

        await query.edit_message_text(
            "🔥 Hauptmenü",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "menu_cards":
        await query.edit_message_text(
            "🔍 Sende einfach einen Kartennamen.\n\n"
            "Beispiel:\n"
            "Giratina V Lost Origin",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="back_main")]
            ])
        )
        return

    if data == "menu_products":
        keyboard = [
            [
                InlineKeyboardButton("🔎 Produkt suchen", callback_data="product_search_help"),
                InlineKeyboardButton("🔥 Trending", callback_data="product_trending")
            ],
            [
                InlineKeyboardButton("🇯🇵 JP Produkte", callback_data="product_jp"),
                InlineKeyboardButton("🆕 Neue Sets", callback_data="product_new")
            ],
            [
                InlineKeyboardButton("🔙 Zurück", callback_data="back_main")
            ]
        ]

        await query.edit_message_text(
            "📦 Produktmenü",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "product_search_help":
        await query.edit_message_text(
            "🔎 Sende einfach ein Produkt.\n\n"
            "Beispiele:\n"
            "151 ETB\n"
            "Lost Origin Booster Box\n"
            "JP 151 Display",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_products")]
            ])
        )
        return

    if data == "product_trending":
        await query.edit_message_text(
            "🔥 Trending Produkte folgen bald.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_products")]
            ])
        )
        return

    if data == "product_jp":
        await query.edit_message_text(
            "🇯🇵 JP Produkte\n\n"
            "Beispiele:\n"
            "151 JP\n"
            "VSTAR Universe\n"
            "Battle Partners",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_products")]
            ])
        )
        return

    if data == "product_new":
        await query.edit_message_text(
            "🆕 Neue Pokémon Sets folgen bald.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_products")]
            ])
        )
        return

    if data == "menu_restocks":
        keyboard = [
            [
                InlineKeyboardButton("🔔 Meine Produkte", callback_data="restock_myproducts"),
                InlineKeyboardButton("🔍 Shop-Check", callback_data="restock_check")
            ],
            [
                InlineKeyboardButton("🌍 Shop-Produkte", callback_data="restock_shopproducts")
            ],
            [
                InlineKeyboardButton("🔙 Zurück", callback_data="back_main")
            ]
        ]

        await query.edit_message_text(
            "🔔 Restock-Menü",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "restock_myproducts":
        cursor.execute(
            """
            SELECT product_query
            FROM tracked_products
            WHERE user_id = ?
            """,
            (user_id,)
        )

        products = cursor.fetchall()

        text = "🔔 Beobachtete Produkte\n\n"

        if not products:
            text += "Noch keine Produkte."
        else:
            for product in products:
                text += f"• {product[0]}\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_restocks")]
            ])
        )
        return

    if data == "restock_check":
        await query.edit_message_text(
            "🔍 Shop-Checks laufen automatisch alle 5 Minuten.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_restocks")]
            ])
        )
        return

    if data == "restock_shopproducts":
        cursor.execute(
            """
            SELECT product_name, shop_name
            FROM global_shop_products
            """
        )

        products = cursor.fetchall()

        text = "🌍 Globale Shop-Produkte\n\n"

        if not products:
            text += "Keine Produkte gespeichert."
        else:
            for product_name, shop_name in products:
                text += f"📦 {product_name} — {shop_name}\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_restocks")]
            ])
        )
        return

    if data == "menu_trends":
        keyboard = [
            [
                InlineKeyboardButton("📦 Produkt-Trends", callback_data="trend_products"),
                InlineKeyboardButton("🃏 Karten-Trends", callback_data="trend_cards")
            ],
            [
                InlineKeyboardButton("🔙 Zurück", callback_data="back_main")
            ]
        ]

        await query.edit_message_text(
            "📈 Trend-Menü",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "trend_products":

        text = "📦 Produkt-Trends\n\n"

        if not PRODUCT_TRENDS:
            text += "Noch keine Produkt-Trends vorhanden."

        else:
            sorted_products = sorted(
                PRODUCT_TRENDS.items(),
                key=lambda x: x[1],
                reverse=True
            )

            for name, count in sorted_products[:10]:
                text += f"🔥 {name} ({count}x)\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_trends")]
            ])
        )

        return

    if data == "trend_cards":

        text = "🃏 Karten-Trends\n\n"

        if not CARD_SEARCH_COUNT:
            text += "Noch keine Karten-Trends vorhanden."

        else:
            sorted_cards = sorted(
                CARD_SEARCH_COUNT.items(),
                key=lambda x: x[1],
                reverse=True
            )

            for name, count in sorted_cards[:10]:
                text += f"🔥 {name} ({count}x)\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_trends")]
            ])
        )

        return

    if data == "menu_watchlist":
        keyboard = [
            [
                InlineKeyboardButton("🃏 Meine Karten", callback_data="watch_cards"),
                InlineKeyboardButton("📦 Meine Produkte", callback_data="watch_products")
            ],
            [
                InlineKeyboardButton("🔙 Zurück", callback_data="back_main")
            ]
        ]

        await query.edit_message_text(
            "⭐ Watchlist",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if data == "watch_cards":
        cursor.execute(
            """
            SELECT card_name
            FROM tracked_cards
            WHERE user_id = ?
            """,
            (user_id,)
        )

        cards = cursor.fetchall()

        text = "🃏 Meine Karten\n\n"

        if not cards:
            text += "Keine Karten gespeichert."
        else:
            for card in cards:
                text += f"• {card[0]}\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_watchlist")]
            ])
        )
        return

    if data == "watch_products":
        cursor.execute(
            """
            SELECT product_query
            FROM tracked_products
            WHERE user_id = ?
            """,
            (user_id,)
        )

        products = cursor.fetchall()

        text = "📦 Meine Produkte\n\n"

        if not products:
            text += "Keine Produkte gespeichert."
        else:
            for product in products:
                text += f"• {product[0]}\n"

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Zurück", callback_data="menu_watchlist")]
            ])
        )
        return
async def action_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    data = query.data

    if data.startswith("track_"):
        card_name = data.replace("track_", "")

        cursor.execute(
            "SELECT * FROM tracked_cards WHERE user_id = ? AND card_name = ?",
            (user_id, card_name)
        )

        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """
                DELETE FROM tracked_cards
                WHERE user_id = ? AND card_name = ?
                """,
                (user_id, card_name)
            )

            conn.commit()

            await query.message.reply_text(f"❌ Tracking entfernt: {card_name}")
            return

        cursor.execute(
            "INSERT INTO tracked_cards (user_id, card_name) VALUES (?, ?)",
            (user_id, card_name)
        )

        conn.commit()

        await query.message.reply_text(f"✅ Karte wird beobachtet: {card_name}")

    elif data.startswith("history_"):
        card_name = data.replace("history_", "")

        cursor.execute(
            """
            SELECT price, checked_at
            FROM price_history
            WHERE card_name = ?
            ORDER BY checked_at DESC
            LIMIT 5
            """,
            (card_name,)
        )

        results = cursor.fetchall()

        if not results:
            await query.message.reply_text("Noch keine Preise gespeichert.")
            return

        text = f"📈 Preisverlauf für {card_name}\n\n"

        for price, checked_at in results:
            text += f"💰 {price} € — {checked_at}\n"

        await query.message.reply_text(text)


def load_all_sets():
    url = "https://api.pokemontcg.io/v2/sets"

    response = requests.get(url)

    if response.status_code != 200:
        return {}

    data = response.json()

    sets = {}

    for s in data.get("data", []):
        name = s.get("name", "").lower()

        sets[name] = s.get("id")

    return sets

ALL_SETS = load_all_sets()

async def mycards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        SELECT card_name
        FROM tracked_cards
        WHERE user_id = ?
        """,
        (user_id,)
    )

    results = cursor.fetchall()

    if not results:
        await update.message.reply_text("Du trackst noch keine Karten.")
        return

    text = "⭐ Deine getrackten Karten:\n\n"

    for row in results:
        text += f"🃏 {row[0]}\n"

    await update.message.reply_text(text)

async def untrackcards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        DELETE FROM tracked_cards
        WHERE user_id = ?
        """,
        (user_id,)
    )

    conn.commit()

    await update.message.reply_text(
        "❌ Alle getrackten Karten wurden entfernt."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Pokémon TCG Bot Hilfe\n\n"

        "🔍 Karten suchen:\n"
        "charizard 151\n"
        "/preis pikachu\n\n"

        "⭐ Tracking:\n"
        "⭐ Button unter Karten drücken\n"
        "/mycards\n"
        "/untrackcards\n\n"

        "📈 Preise:\n"
        "/preishistory Charizard\n\n"

        "🛒 Cardmarket:\n"
        "Direkt unter jeder Karte verfügbar"
    )

    await update.message.reply_text(text)

PRODUCT_TYPES = {
    "etb": "Top-Trainer-Box",
    "display": "Display",
    "booster bundle": "Booster Bundle",
    "mini tin": "Mini Tin",
    "tin": "Tin",
    "case": "Case",
    "upc": "Ultra Premium Collection",
    "collection": "Kollektion"
}

SET_ALIASES = {
    "base set": "basis-set",
    "jungle": "dschungel",
    "fossil": "fossil",
    "team rocket": "team rocket",
    "gym heroes": "arena der helden",
    "gym challenge": "arena der champions",
    "neo genesis": "neo genesis",
    "neo discovery": "neo entdeckung",
    "neo revelation": "neo revelation",
    "neo destiny": "neo destiny",
    "expedition": "expedition",
    "aquapolis": "aquapolis",
    "skyridge": "skyridge",

    "ruby & sapphire": "rubin & saphir",
    "sandstorm": "sandsturm",
    "dragon": "dragon",
    "team magma vs team aqua": "team magma vs team aqua",
    "hidden legends": "verborgene legenden",
    "fire red & leaf green": "feuerrot & blattgrün",
    "team rocket returns": "team rocket returns",
    "deoxys": "deoxys",
    "emerald": "smaragd",
    "unseen forces": "verborgene mächte",
    "delta species": "delta species",
    "legend maker": "legend maker",
    "holon phantoms": "holon phantoms",
    "crystal guardians": "crystal guardians",
    "dragon frontiers": "dragon frontiers",
    "power keepers": "power keepers",

    "diamond & pearl": "diamant & perl",
    "mysterious treasures": "geheimnisvolle schätze",
    "secret wonders": "geheimnisvolle wunder",
    "great encounters": "große begegnungen",
    "majestic dawn": "majestätischer morgen",
    "legends awakened": "erwachte legenden",
    "stormfront": "sturmfront",

    "platinum": "platin",
    "rising rivals": "ultimative sieger",
    "supreme victors": "ultimative sieger",
    "arceus": "arceus",

    "heartgold soulsilver": "heartgold soulsilver",
    "unleashed": "entfesselt",
    "undaunted": "unerschrocken",
    "triumphant": "triumpf",

    "black & white": "schwarz & weiß",
    "emerging powers": "aufstrebende mächte",
    "noble victories": "noble victories",
    "next destinies": "nächste schicksale",
    "dark explorers": "finstere erkunder",
    "dragons exalted": "drachenleuchten",
    "boundaries crossed": "grenzen überschritten",
    "plasma storm": "plasmasturm",
    "plasma freeze": "plasmafrost",
    "plasma blast": "plasmaorkan",
    "legendary treasures": "legendäre schätze",

    "xy": "xy",
    "flashfire": "flammenmeer",
    "furious fists": "faustschlag",
    "phantom forces": "phantomkräfte",
    "primal clash": "protoschock",
    "roaring skies": "sturmtief",
    "ancient origins": "ewige anfänge",
    "breakthrough": "durchbruch",
    "breakpoint": "turbo start",
    "fates collide": "schicksalsschmiede",
    "steam siege": "dampfkessel",
    "evolutions": "evolution",

    "sun & moon": "sonne & mond",
    "guardians rising": "stunde der wächter",
    "burning shadows": "nacht in flammen",
    "crimson invasion": "ultra prisma",
    "ultra prism": "ultra prisma",
    "forbidden light": "ultra prism",
    "celestial storm": "sturm am firmament",
    "dragon majesty": "majestät der drachen",
    "lost thunder": "donnernde entfesselung",
    "team up": "teams sind trumpf",
    "unbroken bonds": "teams sind trumpf",
    "unified minds": "ewiger bund",
    "cosmic eclipse": "kosmische finsternis",

    "sword & shield": "schwert & schild",
    "rebel clash": "clash der rebellen",
    "darkness ablaze": "flammen der finsternis",
    "vivid voltage": "farbenschock",
    "battle styles": "kampfstile",
    "chilling reign": "schaurige herrschaft",
    "evolving skies": "drachenwandel",
    "fusion strike": "fusionsangriff",
    "brilliant stars": "strahlende sterne",
    "astral radiance": "astralglanz",
    "pokemon go": "pokemon go",
    "lost origin": "verlorener ursprung",
    "silver tempest": "silberne sturmwinde",
    "crown zenith": "zenit der könige",

    "scarlet & violet": "karmesin & purpur",
    "paldea evolved": "entwicklungen in paldea",
    "obsidian flames": "obsidianflammen",
    "pokemon 151": "151",
    "paradox rift": "paradoxrift",
    "paldean fates": "paldeas schicksale",
    "temporal forces": "zeitliche mächte",
    "twilight masquerade": "maskerade im zwielicht",
    "shrouded fable": "verborgene fabel",
    "stellar crown": "stellarkrone",
    "surging sparks": "stürmische funken",
    "journey together": "reisegefährten",
    "destined rivals": "ewige rivalen",

    "mega evolution": "mega-entwicklung",
    "ascended heroes": "erhabene helden",
    "phantasmal flames": "fatale flammen",
    "perfect order": "optimale ordnung",
    "rising chaos": "wachsendes chaos"
}

JP_SET_ALIASES = {

    "151 jp": "pokemon card 151",
    "pokemon 151 jp": "pokemon card 151",
    "jp 151": "pokemon card 151",

    "vstar universe": "vstar universe",

    "shiny treasure": "shiny treasure ex",
    "shiny treasure ex": "shiny treasure ex",

    "terastal festival": "terastal festival ex",
    "terastal festival ex": "terastal festival ex",

    "battle partners": "battle partners",

    "night wanderer": "night wanderer",

    "ruler of the black flame": "ruler of the black flame",

    "super electric breaker": "super electric breaker",

    "crimson haze": "crimson haze",

    "mask of change": "mask of change",

    "paradise dragona": "paradise dragona"
}

def normalize_product_query(query):

    q = query.lower()

    for english_name, german_name in SET_ALIASES.items():
        q = q.replace(
            english_name,
            german_name
        )

    for alias, real_name in JP_SET_ALIASES.items():
        q = q.replace(
            alias,
            real_name
        )

    replacements = {
        "etb": "top trainer box",
        "display": "display",
        "case": "6 display-karton",
        "upc": "ultra premium collection",
        "booster bundle": "booster bundle",
        "mini tin": "mini tin",
        "collection": "collection box"
    }

    for short, full in replacements.items():
        q = q.replace(
            short,
            full
        )

    return q

def get_product_price(query):
    q = query.lower()

    for product_name, price in PRODUCT_PRICES.items():
        if product_name in q:

            old_price = LAST_PRODUCT_PRICES.get(product_name)

            LAST_PRODUCT_PRICES[product_name] = price

            if product_name not in PRODUCT_HISTORY:
                PRODUCT_HISTORY[product_name] = []

            PRODUCT_HISTORY[product_name].append(price)

            if old_price and old_price != price:
                return f"{price} 📈 geändert"

            return price

    return "Noch keine Live-Daten"

PRODUCT_PRICES = {
    "verlorener ursprung top trainer box": "ca. 45–60 €",
    "ewige rivalen top trainer box": "ca. 50–70 €",
    "151 top trainer box": "ca. 80–120 €"
}

LAST_PRODUCT_PRICES = {}
PRODUCT_HISTORY = {}
PRODUCT_TRENDS = {}
CARD_SEARCH_COUNT = {}
LAST_RESTOCK_ALERTS = {}
SHOP_SEARCH_PATTERNS = {

    "Gate to the Games":
        "https://www.gate-to-the-games.de/search?sSearch={query}",

    "Cardbuddys":
        "https://cardbuddys.de/search?search={query}",

    "Games Island":
        "https://games-island.eu/search?sSearch={query}",

    "Trader Online":
        "https://www.trader-online.de/search?sSearch={query}",

    "Amazon":
        "https://www.amazon.de/s?k={query}",

    "eBay":
        "https://www.ebay.de/sch/i.html?_nkw={query}",

    "Smyths":
        "https://www.smythstoys.com/de/de-de/search/?text={query}",

    "Müller":
        "https://www.mueller.de/search/?query={query}",

    "GameStop":
        "https://www.gamestop.de/SearchResult/QuickSearch?q={query}",

    "Chaos Cards":
        "https://www.chaoscards.co.uk/search/{query}",

    "OTTO":
        "https://www.otto.de/suche/{query}/",

    "Kaufland":
        "https://www.kaufland.de/s/?search_value={query}",

    "MediaMarkt":
        "https://www.mediamarkt.de/de/search.html?query={query}",

    "Saturn":
        "https://www.saturn.de/de/search.html?query={query}",

    "Thalia":
        "https://www.thalia.de/suche?sq={query}",

    "Rossmann":
        "https://www.rossmann.de/de/search?text={query}",

    "dm":
        "https://www.dm.de/search?query={query}",

    "StockX":
        "https://stockx.com/search?s={query}",

    "Plaza Japan":
        "https://www.plazajapan.com/search-results/?q={query}",

    "Meccha Japan":
        "https://meccha-japan.com/en/search?controller=search&s={query}",

    "Japan2UK":
        "https://www.japan2uk.com/search?q={query}"

}
SHOPS = {
    "Gate to the Games": "https://www.gate-to-the-games.de",
    "Cardbuddys": "https://cardbuddys.de",
    "Pokeviert": "https://pokeviert.de",
    "Smyths": "https://www.smythstoys.com/de/de-de",
    "Müller": "https://www.mueller.de",
    "Rossmann": "https://www.rossmann.de",
    "dm": "https://www.dm.de",
    "GameStop": "https://www.gamestop.de",
    "Games Island": "https://games-island.eu",
    "Trader Online": "https://www.trader-online.de",
    "TCG-Corner": "https://www.tcg-corner.de",
    "Poke-Corner": "https://www.poke-corner.de",
    "Cardicuno": "https://cardicuno.de",
    "Collect-It": "https://collect-it.de",
    "Mythic Games": "https://mythicgames.de",
    "Lucky Card Shop": "https://luckycardshop.de",
    "Kofuku": "https://kofuku.de",

    "Amazon": "https://www.amazon.de",
    "eBay": "https://www.ebay.de",
    "OTTO": "https://www.otto.de",
    "Kaufland": "https://www.kaufland.de",
    "MediaMarkt": "https://www.mediamarkt.de",
    "Saturn": "https://www.saturn.de",
    "Thalia": "https://www.thalia.de",
    "Müller Online": "https://www.mueller.de",

    "PokeNinJapan": "https://pokeninjapan.store",
    "Plaza Japan": "https://www.plazajapan.com",
    "Japan2UK": "https://www.japan2uk.com",
    "Chaos Cards": "https://www.chaoscards.co.uk",
    "Meccha Japan": "https://meccha-japan.com"
}

def get_product_trend(query):
    q = query.lower()

    for product_name, history in PRODUCT_HISTORY.items():
        if product_name in q:

            if len(history) < 2:
                return "➖ stabil"

            latest = history[-1]
            previous = history[-2]

            if latest > previous:
                return "📈 steigend"

            elif latest < previous:
                return "📉 fallend"

            else:
                return "➖ stabil"

    return "➖ unbekannt"

def get_product_history(query):
    q = query.lower()

    for product_name, history in PRODUCT_HISTORY.items():
        if product_name in q:
            return history

    return []


async def product_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    query_lower = query.lower()
    PRODUCT_TRENDS[query_lower] = PRODUCT_TRENDS.get(query_lower, 0) + 1
    product_type = "Produkt"

    for keyword, display_name in PRODUCT_TYPES.items():
        if keyword in query.lower():
            product_type = display_name
            break

    search_query = normalize_product_query(query)
    product_price = get_product_price(search_query)

    product_history = get_product_history(search_query)
    product_trend = get_product_trend(search_query)

    history_text = ""

    if product_history:
        history_text = "\n".join(product_history[-5:])

    cardmarket_search_url = (
        "https://www.cardmarket.com/de/Pokemon/Products/Search?searchString="
        + search_query.replace(" ", "+")
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "🛒 Auf Cardmarket suchen",
                url=cardmarket_search_url
            )
        ],
        [
            InlineKeyboardButton(
                "🔔 Restock-Link hinzufügen",
                callback_data="restock_help"
            )
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"📦 <b>Produktsuche</b>\n\n"
        f"🔍 <b>Gesucht:</b> {query}\n"
        f"📦 <b>Produkttyp:</b> {product_type}\n"
        f"💰 <b>Preis:</b> {product_price}\n"
        f"📈 <b>Trend:</b> {product_trend}\n\n"
        f"📊 <b>Verlauf:</b>\n{history_text}\n\n"
        f"🛒 Öffne Cardmarket über den Button.\n"
        f"🔔 Für Restock kannst du später Produktlinks speichern."
)

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=reply_markup
    )

async def trackproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)

    if not query:
        await update.message.reply_text(
            "Benutze: /trackproduct lost origin etb"
        )
        return

    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        INSERT INTO tracked_products (user_id, product_query)
        VALUES (?, ?)
        """,
        (user_id, query)
    )

    conn.commit()

    await update.message.reply_text(
        f"🔔 Produkt wird beobachtet:\n{query}"
    )

async def myproducts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        SELECT product_query
        FROM tracked_products
        WHERE user_id = ?
        """,
        (user_id,)
    )

    products = cursor.fetchall()

    if not products:
        await update.message.reply_text(
            "Du beobachtest noch keine Produkte."
        )
        return

    text = "🔔 <b>Deine beobachteten Produkte:</b>\n\n"

    for product in products:
        text += f"• {product[0]}\n"

    await update.message.reply_text(
        text,
        parse_mode="HTML"
    )

async def restocktest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "🚨 RESTOCK ALARM!\n\n"
            "📦 Pokémon 151 ETB\n"
            "🛒 Produkt möglicherweise wieder verfügbar!"
        )
    )

async def checkproducts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        SELECT product_query
        FROM tracked_products
        WHERE user_id = ?
        """,
        (user_id,)
    )

    products = cursor.fetchall()

    if not products:
        await update.message.reply_text(
            "Du beobachtest noch keine Produkte."
        )
        return

    text = "🔍 Produkt-Check\n\n"

    for product in products:
        query = product[0]
        search_query = normalize_product_query(query)
        price = get_product_price(search_query)
        trend = get_product_trend(search_query)

        text += (
            f"📦 {query}\n"
            f"💰 Preis: {price}\n"
            f"📈 Trend: {trend}\n\n"
        )

    await update.message.reply_text(text)

async def trackshopurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "Benutze:\n/trackshopurl PRODUKTNAME URL"
        )
        return

    product_name = " ".join(args[:-1])
    shop_url = args[-1]

    user_id = str(update.effective_user.id)

    cursor.execute(
        """
        INSERT INTO tracked_shop_urls
        (user_id, product_name, shop_url)
        VALUES (?, ?, ?)
        """,
        (user_id, product_name, shop_url)
    )

    conn.commit()

    await update.message.reply_text(
        f"🔔 Shop-URL gespeichert:\n\n"
        f"📦 {product_name}\n"
        f"🛒 {shop_url}"
    )
async def addshopproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if len(args) < 3:
        await update.message.reply_text(
            "Benutze:\n/addshopproduct PRODUKT | SHOP | URL"
        )
        return

    full_text = " ".join(args)

    parts = full_text.split("|")

    if len(parts) != 3:
        await update.message.reply_text(
            "Format:\n/addshopproduct PRODUKT | SHOP | URL"
        )
        return

    product_name = parts[0].strip()
    shop_name = parts[1].strip()
    shop_url = parts[2].strip()

    cursor.execute(
        """
        INSERT INTO global_shop_products
        (product_name, shop_name, shop_url)
        VALUES (?, ?, ?)
        """,
        (product_name, shop_name, shop_url)
    )

    conn.commit()

    await update.message.reply_text(
        f"✅ Produkt gespeichert\n\n"
        f"📦 {product_name}\n"
        f"🏪 {shop_name}"
    )

async def listshopproducts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        """
        SELECT product_name, shop_name, shop_url
        FROM global_shop_products
        """
    )

    products = cursor.fetchall()

    if not products:
        await update.message.reply_text(
            "Noch keine globalen Shop-Produkte gespeichert."
        )
        return

    text = "🌍 Globale Shop-Produkte:\n\n"

    for product_name, shop_name, shop_url in products:
        text += (
            f"📦 {product_name}\n"
            f"🏪 {shop_name}\n"
            f"🛒 {shop_url}\n\n"
        )

    await update.message.reply_text(text)

async def checkshopproducts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor.execute(
        """
        SELECT product_name, shop_name, shop_url
        FROM global_shop_products
        """
    )

    products = cursor.fetchall()

    if not products:
        await update.message.reply_text(
            "Keine globalen Shop-Produkte gespeichert."
        )
        return

    text = "🔍 Shop-Produkt-Check\n\n"

    for product_name, shop_name, shop_url in products:

        status = check_restock(shop_url)

        price = extract_shop_price(shop_url)

        if status is True:
            status_text = "✅ möglicherweise verfügbar"

        elif status is False:
            status_text = "❌ wahrscheinlich ausverkauft"

        else:
            status_text = "⚠️ konnte nicht geprüft werden"

        text += (
            f"📦 {product_name}\n"
            f"🏪 {shop_name}\n"
            f"{status_text}\n"
            f"💰 Preis: {price}\n"
            f"🛒 {shop_url}\n\n"
        )

    await update.message.reply_text(text)

def product_matches(user_query, product_name):
    user_words = user_query.lower().split()
    product_text = product_name.lower()

    for word in user_words:
        if word not in product_text:
            return False

    return True

async def auto_shop_restock_check(app):

    while True:

        try:

            cursor.execute(
                """
                SELECT product_name, shop_name, shop_url
                FROM global_shop_products
                """
            )

            products = cursor.fetchall()

            for product_name, shop_name, shop_url in products:

                status = check_restock(shop_url)

                if status is not True:

                    cursor.execute(
                        """
                        DELETE FROM sent_restock_alerts
                        WHERE product_name = ? AND shop_name = ?
                        """,
                        (product_name, shop_name)
                    )

                    conn.commit()

                    continue

                cursor.execute(
                    """
                    SELECT status
                    FROM sent_restock_alerts
                    WHERE product_name = ? AND shop_name = ?
                    """,
                    (product_name, shop_name)
                )

                existing_alert = cursor.fetchone()

                if existing_alert and existing_alert[0] == "sent":
                    continue

                cursor.execute(
                    """
                    INSERT INTO sent_restock_alerts (
                        product_name,
                        shop_name,
                        status
                    )
                    VALUES (?, ?, ?)
                    """,
                    (product_name, shop_name, "sent")
                )

                conn.commit()

                cursor.execute(
                    """
                    SELECT user_id, product_query
                    FROM tracked_products
                    """
                )

                tracked = cursor.fetchall()

                for user_id, product_query in tracked:

                    if not product_matches(product_query, product_name):
                        continue

                    price = get_cardmarket_price(product_name)

                    text = (
    			"🚨 RESTOCK GEFUNDEN 🚨\n\n"
    	               f"📦 {product_name}\n"
    		       f"🏪 {shop_name}\n\n"
    		       f"🛒 Jetzt verfügbar:\n{shop_url}"
 		    )

                        await app.bot.send_message(
                            chat_id=user_id,
                            text=text
                        )

                    except Exception as e:
                        print(e)

            await asyncio.sleep(300)

        except Exception as e:
            print(e)
            await asyncio.sleep(30)


async def searchshops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)

    if not query:
        await update.message.reply_text(
            "Benutze: /searchshops 151 etb"
        )
        return

    search_query = normalize_product_query(query)
    encoded_query = search_query.replace(" ", "+")

    text = f"🔍 Shop-Suchlinks für:\n{search_query}\n\n"

    for shop_name, pattern in SHOP_SEARCH_PATTERNS.items():

        search_url = pattern.format(query=encoded_query)

        text += (
            f"🏪 {shop_name}\n"
            f"{search_url}\n\n"
        )

    await update.message.reply_text(text)

async def findproductpages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)

    if not query:
        await update.message.reply_text(
            "Benutze: /findproductpages 151 etb"
        )
        return

    search_query = normalize_product_query(query)
    encoded_query = search_query.replace(" ", "+")

    text = f"🔍 Mögliche Produktseiten für:\n{search_query}\n\n"

    for shop_name, pattern in SHOP_SEARCH_PATTERNS.items():
        search_url = pattern.format(query=encoded_query)

        text += (
            f"🏪 {shop_name}\n"
            f"🔗 {search_url}\n\n"
        )

    await update.message.reply_text(text)

async def savefoundproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Benutze:\n"
        "/addshopproduct PRODUKT | SHOP | URL\n\n"
        "Beispiel:\n"
        "/addshopproduct 151 ETB | Smyths | https://..."
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton("🔍 Karten", callback_data="menu_cards"),
            InlineKeyboardButton("📦 Produkte", callback_data="menu_products")
        ],
        [
            InlineKeyboardButton("🔔 Restocks", callback_data="menu_restocks"),
            InlineKeyboardButton("📈 Trends", callback_data="menu_trends")
        ],
        [
            InlineKeyboardButton("⭐ Watchlist", callback_data="menu_watchlist")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🔥 Hauptmenü",
        reply_markup=reply_markup
    )

def detect_product_price_change(query):
    q = query.lower()

    history = PRODUCT_HISTORY.get(q)

    if not history or len(history) < 2:
        return "Noch nicht genug Daten."

    old_price = history[-2]
    new_price = history[-1]

    if new_price < old_price:
        return f"📉 Preis gefallen: {old_price} → {new_price}"

    if new_price > old_price:
        return f"📈 Preis gestiegen: {old_price} → {new_price}"

    return "➖ Preis stabil."

def generate_price_chart(product_name, history):

    plt.figure(figsize=(6, 4))

    clean_history = []

    for item in history:

        number = (
            str(item)
            .replace("ca.", "")
            .replace("€", "")
            .replace(",", ".")
            .strip()
        )

        if "-" in number:
            number = number.split("-")[0].strip()

        try:
            clean_history.append(float(number))
        except:
            pass

    plt.plot(clean_history, marker="o")

    plt.title(product_name)
    plt.xlabel("Preischecks")
    plt.ylabel("Preis €")

    filename = f"{product_name}.png"

    plt.savefig(filename)

    plt.close()

    return filename

async def producthistory(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = " ".join(context.args)

    if not query:
        await update.message.reply_text(
            "Benutze:\n/producthistory 151 etb"
        )
        return

    query_lower = query.lower()

    history = PRODUCT_HISTORY.get(query_lower)
    chart_file = generate_price_chart(query, history)
    price_change = detect_product_price_change(query_lower)

    if not history:
        await update.message.reply_text(
            "Keine Preishistorie gefunden."
        )
        return

    text = f"📈 Preisverlauf für:\n{query}\n\n"
    text += f"{price_change}\n\n"

    for entry in history[-10:]:
        text += f"💰 {entry}\n"

    with open(chart_file, "rb") as photo:

        await update.message.reply_photo(
            photo=photo,
            caption=text
        )

def find_gate_product_link(search_url, query):

    try:

        response = requests.get(
            search_url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        html = response.text

        product_links = re.findall(
            r'href="(https://www\.gate-to-the-games\.de/[^"]+)"',
            html
        )

        query_words = query.lower().split()

        for link in product_links:

            link_lower = link.lower()

            matched = 0

            for word in query_words:
                if word in link_lower:
                    matched += 1

            if matched >= 2:
                return link

        return search_url

    except Exception:
        return search_url

def find_gate_product_link(search_url, query):

    try:

        response = requests.get(
            search_url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        html = response.text.lower()

        links = re.findall(r'href=["\'](.*?)["\']', html)

        query_words = query.lower().split()

        for link in links:

            link_lower = link.lower()

            if "/pokemon-" not in link_lower:
                continue

            if "display" in link_lower or "trainer" in link_lower:

                matched = 0

                for word in query_words:
                    if word in link_lower:
                        matched += 1

                if matched >= 2:
                    return urljoin(search_url, link)

        return search_url

    except Exception:
        return search_url

def find_product_link(search_url, query):

    try:

        if "gate-to-the-games.de" in search_url:
            return find_gate_product_link(
                search_url,
                query
            )

        response = requests.get(
            search_url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        html = response.text

        query_words = query.lower().split()

        links = re.findall(
            r'href=["\'](.*?)["\']',
            html
        )

        for link in links:

            link_lower = link.lower()

            if all(
                word in link_lower
                for word in query_words[:2]
            ):
                return urljoin(
                    search_url,
                    link
                )

        return search_url

    except Exception:
        return search_url

async def autoproduct(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = " ".join(context.args)

    if not query:
        await update.message.reply_text(
            "Benutze:\n/autoproduct 151 etb"
        )
        return

    search_query = normalize_product_query(query)
    encoded_query = search_query.replace(" ", "+")

    cursor.execute(
        """
        DELETE FROM global_shop_products
        WHERE product_name = ?
        """,
        (search_query,)
    )

    conn.commit()

    await update.message.reply_text(
        "🤖 Automatische Produktsuche gestartet\n\n"
        f"📦 Produkt: {search_query}\n\n"
        "Ich suche passende Shopseiten und bereite Restock-Überwachung vor."
    )

    await update.message.reply_text(
        f"DEBUG Shops gefunden: {len(SHOP_SEARCH_PATTERNS)}"
    )

    for shop_name, pattern in SHOP_SEARCH_PATTERNS.items():

        try:

            search_url = pattern.format(query=encoded_query)

            product_url = find_product_link(
                search_url,
                search_query
            )

            cursor.execute(
                """
                INSERT INTO global_shop_products
                (
                    product_name,
                    shop_name,
                    shop_url
                )
                VALUES (?, ?, ?)
                """,
                (
                    search_query,
                    shop_name,
                    product_url
                )
            )

            conn.commit()

        except Exception as e:

            await update.message.reply_text(
                f"FEHLER bei {shop_name}:\n{e}"
            )

    cursor.execute(
        "SELECT COUNT(*) FROM global_shop_products"
    )

    count = cursor.fetchone()[0]

    await update.message.reply_text(
        f"DEBUG gespeicherte Produkte: {count}"
    )

    await update.message.reply_text(
        "✅ Produkt wurde automatisch für alle bekannten Shops vorbereitet."
    )
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    job_queue = app.job_queue

    job_queue.run_repeating(
        auto_price_check,
        interval=300,
        first=10
    )

    job_queue.run_repeating(
        auto_restock_check,
        interval=300,
        first=20
    )

    job_queue.run_repeating(
        auto_shop_restock_check,
        interval=300,
        first=30
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("preis", preis))
    app.add_handler(CommandHandler("select", select_card))
    app.add_handler(CallbackQueryHandler(button_select, pattern="^select_"))
    app.add_handler(CommandHandler("set", set_search))
    app.add_handler(CommandHandler("favset", favset))
    app.add_handler(CommandHandler("meinesets", meinesets))
    app.add_handler(CommandHandler("unfavset", unfavset))
    app.add_handler(CommandHandler("preishistory", preishistory))
    app.add_handler(CommandHandler("checkprice", checkprice))
    app.add_handler(CommandHandler("watchsets", watchsets))
    app.add_handler(CommandHandler("alertcheck", alertcheck))
    app.add_handler(CommandHandler("alerttest", alerttest))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("meinekarten", meinekarten))
    app.add_handler(CommandHandler("checktracked", checktracked))
    app.add_handler(CommandHandler("setalert", setalert))
    app.add_handler(CommandHandler("setdrops", setdrops))
    app.add_handler(CommandHandler("trackurl", trackurl))
    app.add_handler(CommandHandler("myurls", myurls))
    app.add_handler(CommandHandler("checkurl", checkurl))
    app.add_handler(CommandHandler("checkmyurls", check_my_urls))
    app.add_handler(CommandHandler("untrackurl", untrackurl))
    app.add_handler(CallbackQueryHandler(action_button_handler, pattern="^(track_|history_)"))
    app.add_handler(CommandHandler("mycards", mycards))
    app.add_handler(CommandHandler("untrackcards", untrackcards))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    app.add_handler(CommandHandler("trackproduct", trackproduct))
    app.add_handler(CommandHandler("myproducts", myproducts))
    app.add_handler(CommandHandler("checkproducts", checkproducts))
    app.add_handler(CommandHandler("restocktest", restocktest))
    app.add_handler(CommandHandler("trackshopurl", trackshopurl))
    app.add_handler(CommandHandler("addshopproduct", addshopproduct))
    app.add_handler(CommandHandler("listshopproducts", listshopproducts))
    app.add_handler(CommandHandler("checkshopproducts", checkshopproducts))
    app.add_handler(CommandHandler("searchshops", searchshops))
    app.add_handler(CommandHandler("findproductpages", findproductpages))
    app.add_handler(CommandHandler("savefoundproduct", savefoundproduct))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(menu_|product_|back_)"))
    app.add_handler(CommandHandler("producthistory", producthistory))
    app.add_handler(CommandHandler("autoproduct", autoproduct))
    app.add_handler(

        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)
    )

    print("Bot läuft...")

    app.run_polling()


if __name__ == "__main__":
    main()