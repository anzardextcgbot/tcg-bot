import sqlite3
import requests
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
def search_pokemon_card(card_name):
    url = "https://api.pokemontcg.io/v2/cards"

    params = {
        "q": f'name:"{card_name}"',
        "pageSize": 10
    }

    response = requests.get(url, params=params)
    data = response.json()

    return data.get("data", [])


def save_price(card_name, price):
    from datetime import datetime

    cursor.execute(
        "INSERT INTO price_history (card_name, price, checked_at) VALUES (?, ?, ?)",
        (
            card_name,
            price,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )

    conn.commit()


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

    if text == "🃏 Karte suchen":
        await update.message.reply_text("🔍 Schreibe:\n/preis pikachu")

    elif text == "📦 Sets":
        await update.message.reply_text("📦 Schreibe:\n/set 151")

    elif text == "⭐ Favoriten":
        await update.message.reply_text(
            "⭐ Favoriten-Menü\n\n"
            "/favset 151\n"
            "/meinesets\n"
            "/unfavset 151"
        )

    elif text == "🔔 Alerts":
        await update.message.reply_text(
            "🔔 Alert-Menü\n\n"
            "/watchsets\n"
            "/alertcheck"
        )

    elif text == "📈 Preise":
        await update.message.reply_text(
            "📈 Preis-Menü\n\n"
            "/checkprice pikachu\n"
            "/preishistory Pikachu"
        )
    else:
        context.args = update.message.text.split()

        await preis(update, context)

async def preis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)

    if not query:
        await update.message.reply_text("Benutze: /preis pikachu")
        return

    search_words = query.lower().split()

    card_name = search_words[0]

    cards = search_pokemon_card(card_name)

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
                card_number = card.get("number", "").lower()

                if word in set_name:
                    score += 3

                if word == card_number:
                    score += 5

        filtered_cards.append((score, card))

    filtered_cards.sort(reverse=True, key=lambda x: x[0])

    cards = [card for score, card in filtered_cards[:5]]

    user_id = str(update.effective_user.id)

    last_search_results[user_id] = cards

    if not cards:
        await update.message.reply_text("Keine Karte gefunden.")
        return

    if len(cards) == 1:
        card = cards[0]

        image = card.get("images", {}).get("large")

        name = card.get("name")
        set_name = card.get("set", {}).get("name")
        number = card.get("number")

        price = (
            card.get("cardmarket", {})
            .get("prices", {})
            .get("trendPrice", "?")
        )

        text = (
            f"🃏 {name}\n"
            f"📦 Set: {set_name}\n"
            f"#️⃣ Nummer: {number}\n"
            f"💰 Preis: {price} €"
        )

        await update.message.reply_photo(
            photo=image,
            caption=text
        )

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

    user_id = str(query.from_user.id)

    choice = int(query.data.replace("select_", ""))

    cards = last_search_results.get(user_id)

    if not cards:
        await query.message.reply_text("Bitte suche zuerst eine Karte mit /preis pikachu")
        return

    if choice < 1 or choice > len(cards):
        await query.message.reply_text("Diese Karte gibt es nicht.")
        return

    card = cards[choice - 1]

    await send_card_details(query.message, card)


async def send_selected_card(update: Update, user_id, choice):
    cards = last_search_results[user_id]

    if choice < 1 or choice > len(cards):
        await update.message.reply_text("Diese Nummer gibt es nicht.")
        return

    card = cards[choice - 1]

    await send_card_details(update.message, card)


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

    keyboard = [
        [
            InlineKeyboardButton("⭐ Tracken", callback_data=f"track_{name}"),
            InlineKeyboardButton("📈 Verlauf", callback_data=f"history_{name}")
        ]
    ]

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
def check_restock(url):
    try:
        response = requests.get(url, timeout=10)

        html = response.text.lower()

        sold_out_words = [
            "out of stock",
            "sold out",
            "nicht verfügbar",
            "ausverkauft"
        ]

        for word in sold_out_words:
            if word in html:
                return False

        return True

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
            await query.message.reply_text("🃏 Diese Karte wird bereits beobachtet.")
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

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)
    )

    print("Bot läuft...")

    app.run_polling()


if __name__ == "__main__":
    main()