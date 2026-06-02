import sqlite3
import requests
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import re
import asyncio
import threading
import hmac
import hashlib
from datetime import datetime
from urllib.parse import urljoin
from flask import Flask, request as flask_request

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    JobQueue,
    PreCheckoutQueryHandler,
)

import os

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
STRIPE_TOKEN          = os.getenv("STRIPE_TOKEN", "")        # Telegram Payments Token von @BotFather
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")   # sk_live_... aus Stripe Dashboard
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "") # whsec_... aus Stripe Webhook
MONTHLY_PRICE    = 699   # Preis in Cent = 6,99€
CURRENCY         = "EUR"
ADMIN_ID         = os.getenv("ADMIN_ID", "")          # Deine Telegram-ID für Admin-Befehle
MONTHLY_PRICE    = 699                                 # Preis in Cent → 6,99 €

last_search_results = {}

# ─────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────
conn   = sqlite3.connect("tcg.db", check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS favorite_sets (
            user_id TEXT,
            set_name TEXT
        );
        CREATE TABLE IF NOT EXISTS price_history (
            card_name TEXT,
            price REAL,
            checked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tracked_cards (
            user_id TEXT,
            card_name TEXT
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id TEXT,
            alert_threshold REAL DEFAULT 2,
            only_drops TEXT DEFAULT 'no'
        );
        CREATE TABLE IF NOT EXISTS tracked_urls (
            user_id TEXT,
            url TEXT
        );
        CREATE TABLE IF NOT EXISTS restock_status (
            url TEXT PRIMARY KEY,
            last_status TEXT
        );
        CREATE TABLE IF NOT EXISTS sent_price_alerts (
            card_name TEXT PRIMARY KEY,
            last_price REAL
        );
        CREATE TABLE IF NOT EXISTS tracked_products (
            user_id TEXT,
            product_query TEXT,
            UNIQUE(user_id, product_query)
        );
        CREATE TABLE IF NOT EXISTS global_shop_products (
            product_name TEXT,
            shop_name TEXT,
            shop_url TEXT,
            last_checked TEXT,
            last_status TEXT DEFAULT 'unknown'
        );
        CREATE TABLE IF NOT EXISTS sent_restock_alerts (
            product_name TEXT,
            shop_name TEXT,
            status TEXT,
            sent_at TEXT,
            PRIMARY KEY(product_name, shop_name)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id     TEXT PRIMARY KEY,
            username    TEXT,
            status      TEXT DEFAULT 'inactive',
            plan        TEXT DEFAULT 'monthly',
            started_at  TEXT,
            expires_at  TEXT,
            telegram_payment_charge_id TEXT
        );
        CREATE TABLE IF NOT EXISTS known_sets (
            set_id   TEXT PRIMARY KEY,
            set_name TEXT,
            series   TEXT,
            language TEXT DEFAULT 'en',
            release_date TEXT
        );
        CREATE TABLE IF NOT EXISTS card_search_cache (
            user_id   TEXT,
            position  INTEGER,
            card_json TEXT,
            created_at TEXT,
            PRIMARY KEY (user_id, position)
        );
    """)
    conn.commit()

init_db()

# ─────────────────────────────────────────
# SUBSCRIPTION HELPERS
# ─────────────────────────────────────────
def is_subscribed(user_id: str) -> bool:
    cursor.execute(
        "SELECT status, expires_at FROM subscriptions WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    if not row:
        return False
    status, expires_at = row
    if status != "active":
        return False
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if datetime.now() > exp:
                cursor.execute(
                    "UPDATE subscriptions SET status='expired' WHERE user_id=?",
                    (user_id,)
                )
                conn.commit()
                return False
        except Exception:
            pass
    return True

def require_sub(func):
    """Decorator: Nur für Abonnenten."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if not is_subscribed(user_id):
            keyboard = [[InlineKeyboardButton("🔓 Jetzt abonnieren", callback_data="buy_sub")]]
            await update.message.reply_text(
                "🔒 Diese Funktion ist nur für Abonnenten verfügbar.\n\n"
                "📦 AnzarDex Premium – 4,99 €/Monat\n"
                "✅ Restock-Alerts für alle Produkte\n"
                "✅ Preisalarme für alle Karten\n"
                "✅ Alle Sets EN/DE/JP\n"
                "✅ Unbegrenzte Watchlist\n\n"
                "Tippe auf den Button um zu abonnieren 👇",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

# ─────────────────────────────────────────
# SET CACHE (aus PokémonTCG API)
# ─────────────────────────────────────────
ALL_SETS: dict = {}
SETS_LAST_LOADED: str = ""

# Hardcoded Fallback-Sets (alle wichtigen Sets)
FALLBACK_SETS = {
    # Scarlet & Violet
    "scarlet & violet": "sv1", "paldea evolved": "sv2", "obsidian flames": "sv3",
    "scarlet & violet 151": "sv3pt5", "paradox rift": "sv4", "paldean fates": "sv4pt5",
    "temporal forces": "sv5", "twilight masquerade": "sv6", "shrouded fable": "sv6pt5",
    "stellar crown": "sv7", "surging sparks": "sv8", "journey together": "sv9",
    "destined rivals": "sv9pt5",
    # Sword & Shield
    "sword & shield": "swsh1", "rebel clash": "swsh2", "darkness ablaze": "swsh3",
    "vivid voltage": "swsh4", "battle styles": "swsh5", "chilling reign": "swsh6",
    "evolving skies": "swsh7", "fusion strike": "swsh8", "brilliant stars": "swsh9",
    "astral radiance": "swsh10", "lost origin": "swsh11", "silver tempest": "swsh12",
    "crown zenith": "swsh12pt5",
    # Sun & Moon
    "sun & moon": "sm1", "guardians rising": "sm2", "burning shadows": "sm3",
    "ultra prism": "sm5", "forbidden light": "sm6", "celestial storm": "sm7",
    "lost thunder": "sm8", "team up": "sm9", "unbroken bonds": "sm10",
    "unified minds": "sm11", "cosmic eclipse": "sm12",
    # XY
    "xy": "xy1", "flashfire": "xy2", "furious fists": "xy3", "phantom forces": "xy4",
    "primal clash": "xy5", "roaring skies": "xy6", "ancient origins": "xy7",
    "breakthrough": "xy8", "breakpoint": "xy9", "fates collide": "xy10",
    "steam siege": "xy11", "evolutions": "xy12",
    # Black & White
    "black & white": "bw1", "emerging powers": "bw2", "noble victories": "bw3",
    "next destinies": "bw4", "dark explorers": "bw5", "dragons exalted": "bw6",
    "boundaries crossed": "bw7", "plasma storm": "bw8", "plasma freeze": "bw9",
    "plasma blast": "bw10", "legendary treasures": "bw11",
    # HeartGold SoulSilver
    "heartgold & soulsilver": "hgss1", "unleashed": "hgss2",
    "undaunted": "hgss3", "triumphant": "hgss4",
    # Platinum
    "platinum": "pl1", "rising rivals": "pl2", "supreme victors": "pl3", "arceus": "pl4",
    # Diamond & Pearl
    "diamond & pearl": "dp1", "mysterious treasures": "dp2", "secret wonders": "dp3",
    "great encounters": "dp4", "majestic dawn": "dp5", "legends awakened": "dp6",
    "stormfront": "dp7",
    # Base
    "base set": "base1", "jungle": "base2", "fossil": "base3",
    "team rocket": "base4", "gym heroes": "gym1", "gym challenge": "gym2",
    "neo genesis": "neo1", "neo discovery": "neo2", "neo revelation": "neo3",
    "neo destiny": "neo4",
}

def load_all_sets() -> dict:
    global SETS_LAST_LOADED
    # Erstmal Fallback laden
    sets = dict(FALLBACK_SETS)
    # Dann aus DB (falls schon gecacht)
    try:
        cursor.execute("SELECT set_name, set_id FROM known_sets")
        for row in cursor.fetchall():
            sets[row[0].lower()] = row[1]
    except Exception:
        pass
    # API versuchen
    for attempt in range(3):
        try:
            response = requests.get(
                "https://api.pokemontcg.io/v2/sets",
                params={"pageSize": 500},
                timeout=30,
                headers={"User-Agent": "AnzarDexBot/1.0"}
            )
            data = response.json()
            for s in data.get("data", []):
                name   = s.get("name", "").lower()
                set_id = s.get("id", "")
                sets[name] = set_id
                try:
                    cursor.execute(
                        "INSERT OR REPLACE INTO known_sets (set_id, set_name, release_date) VALUES (?,?,?)",
                        (set_id, s.get("name",""), s.get("releaseDate",""))
                    )
                except Exception:
                    pass
            conn.commit()
            SETS_LAST_LOADED = datetime.now().strftime("%Y-%m-%d %H:%M")
            print(f"✅ {len(sets)} Sets geladen ({SETS_LAST_LOADED})")
            return sets
        except Exception as e:
            print(f"⚠️ Sets Versuch {attempt+1}/3 fehlgeschlagen: {e}")
            import time; time.sleep(2)
    print(f"⚠️ Nutze Fallback mit {len(sets)} Sets")
    SETS_LAST_LOADED = "Fallback"
    return sets

ALL_SETS = load_all_sets()

# ─────────────────────────────────────────
# SET ALIASES (DE + JP)
# ─────────────────────────────────────────
SET_ALIASES = {
    # ── Scarlet & Violet (2023–heute) ──────────────────────────────
    "karmesin & purpur":          "scarlet & violet",
    "entwicklungen in paldea":    "paldea evolved",
    "obsidianflammen":            "obsidian flames",
    "paradoxrift":                "paradox rift",
    "paldeas schicksale":         "paldean fates",
    "zeitliche mächte":           "temporal forces",
    "maskerade im zwielicht":     "twilight masquerade",
    "verborgene fabel":           "shrouded fable",
    "stellarkrone":               "stellar crown",
    "stürmische funken":          "surging sparks",
    "reisegefährten":             "journey together",
    "ewige rivalen":              "destined rivals",

    # ── Sword & Shield (2020–2023) ─────────────────────────────────
    "schwert & schild":           "sword & shield",
    "clash der rebellen":         "rebel clash",
    "flammen der finsternis":     "darkness ablaze",
    "farbenschock":               "vivid voltage",
    "kampfstile":                 "battle styles",
    "schaurige herrschaft":       "chilling reign",
    "drachenwandel":              "evolving skies",
    "fusionsangriff":             "fusion strike",
    "strahlende sterne":          "brilliant stars",
    "astralglanz":                "astral radiance",
    "verlorener ursprung":        "lost origin",
    "silberne sturmwinde":        "silver tempest",
    "zenit der könige":           "crown zenith",

    # ── Sun & Moon (2017–2019) ─────────────────────────────────────
    "sonne & mond":               "sun & moon",
    "stunde der wächter":         "guardians rising",
    "nacht in flammen":           "burning shadows",
    "ultra prisma":               "ultra prism",
    "verbotenes licht":           "forbidden light",
    "sturm am firmament":         "celestial storm",
    "majestät der drachen":       "dragon majesty",
    "donnernde entfesselung":     "lost thunder",
    "teams sind trumpf":          "team up",
    "ewiger bund":                "unbroken bonds",
    "einheitliche geister":       "unified minds",
    "kosmische finsternis":       "cosmic eclipse",

    # ── XY (2014–2016) ─────────────────────────────────────────────
    "xy":                         "xy",
    "flammenmeer":                "flashfire",
    "faustschlag":                "furious fists",
    "phantomkräfte":              "phantom forces",
    "protoschock":                "primal clash",
    "sturmtief":                  "roaring skies",
    "ewige anfänge":              "ancient origins",
    "durchbruch":                 "breakthrough",
    "turbo start":                "breakpoint",
    "schicksalsschmiede":         "fates collide",
    "dampfkessel":                "steam siege",
    "evolution":                  "evolutions",

    # ── Black & White (2011–2013) ──────────────────────────────────
    "schwarz & weiß":             "black & white",
    "aufstrebende mächte":        "emerging powers",
    "nächste schicksale":         "next destinies",
    "finstere erkunder":          "dark explorers",
    "drachenleuchten":            "dragons exalted",
    "grenzen überschritten":      "boundaries crossed",
    "plasmasturm":                "plasma storm",
    "plasmafrost":                "plasma freeze",
    "plasmaorkan":                "plasma blast",
    "legendäre schätze":          "legendary treasures",

    # ── HeartGold & SoulSilver (2010–2011) ────────────────────────
    "heartgold soulsilver":       "heartgold & soulsilver",
    "entfesselt":                 "unleashed",
    "unerschrocken":              "undaunted",
    "triumph":                    "triumphant",

    # ── Platinum (2009–2010) ───────────────────────────────────────
    "platin":                     "platinum",
    "aufstrebende rivalen":       "rising rivals",
    "ultimative sieger":          "supreme victors",

    # ── Diamond & Pearl (2007–2009) ────────────────────────────────
    "diamant & perl":             "diamond & pearl",
    "geheimnisvolle schätze":     "mysterious treasures",
    "geheimnisvolle wunder":      "secret wonders",
    "große begegnungen":          "great encounters",
    "majestätischer morgen":      "majestic dawn",
    "erwachte legenden":          "legends awakened",
    "sturmfront":                 "stormfront",

    # ── EX-Ära (2003–2007) ─────────────────────────────────────────
    "rubin & saphir":             "ruby & sapphire",
    "sandsturm":                  "sandstorm",
    "team magma vs team aqua":    "team magma vs team aqua",
    "verborgene legenden":        "hidden legends",
    "feuerrot & blattgrün":       "firered & leafgreen",
    "smaragd":                    "emerald",
    "verborgene mächte":          "unseen forces",
    "legende maker":              "legend maker",

    # ── Neo (2000–2002) ────────────────────────────────────────────
    "neo genesis":                "neo genesis",
    "neo entdeckung":             "neo discovery",
    "neo offenbarung":            "neo revelation",
    "neo schicksal":              "neo destiny",

    # ── Basis-Ära (1999–2000) ──────────────────────────────────────
    "basis":                      "base set",
    "basis-set":                  "base set",
    "dschungel":                  "jungle",
    "fossil":                     "fossil",
    "team rocket":                "team rocket",
    "arena der helden":           "gym heroes",
    "arena der champions":        "gym challenge",

    # ── Japanische Sets (JP) ───────────────────────────────────────
    "151 jp":                     "scarlet & violet 151",
    "shiny treasure":             "scarlet & violet—shiny treasure ex",
    "shiny treasure ex":          "scarlet & violet—shiny treasure ex",
    "vstar universe":             "sword & shield—vstar universe",
    "terastal festival":          "scarlet & violet—terastal festival ex",
    "terastal festival ex":       "scarlet & violet—terastal festival ex",
    "battle partners":            "scarlet & violet—battle partners",
    "night wanderer":             "scarlet & violet—night wanderer",
    "ruler of the black flame":   "scarlet & violet—obsidian flames",
    "super electric breaker":     "scarlet & violet—surging sparks",
    "crimson haze":               "scarlet & violet—twilight masquerade",
    "mask of change":             "scarlet & violet—twilight masquerade",
    "wild force":                 "scarlet & violet—temporal forces",
    "cyber judge":                "scarlet & violet—temporal forces",
    "clay burst":                 "scarlet & violet—paldea evolved",
    "snow hazard":                "scarlet & violet—paldea evolved",

    # ── Zukünftige Sets (werden automatisch via API ergänzt) ───────
    "mega entwicklung":           "mega evolution",
    "ascended heroes":            "scarlet & violet",
    "phantasmal flames":          "scarlet & violet",
    "perfect order":              "scarlet & violet",
    "rising chaos":               "scarlet & violet",
}

JP_SET_ALIASES = {
    "151 jp", "shiny treasure", "shiny treasure ex",
    "vstar universe", "terastal festival", "terastal festival ex",
    "battle partners", "night wanderer", "ruler of the black flame",
    "super electric breaker", "crimson haze", "mask of change",
    "wild force", "cyber judge", "clay burst", "snow hazard",
}

PRODUCT_TYPES = {
    "etb": "Elite Trainer Box",
    "display": "Display (36 Booster)",
    "booster bundle": "Booster Bundle",
    "mini tin": "Mini Tin",
    "tin": "Tin",
    "case": "Case (6 Displays)",
    "upc": "Ultra Premium Collection",
    "collection": "Collection Box",
    "premium collection": "Premium Collection",
    "trainer box": "Elite Trainer Box",
    "ttb": "Top Trainer Box",
    "build and battle": "Build & Battle Box",
}

PRODUCT_KEYWORDS = list(PRODUCT_TYPES.keys()) + [
    "booster", "bundle", "box", "trainer", "premium", "build"
]

# ─────────────────────────────────────────
# SHOPS – Suchmuster + Direktlinks
# ─────────────────────────────────────────
# Shop-Suchmuster: {query} wird durch den Produktnamen ersetzt
SHOP_SEARCH_PATTERNS = {
    # ── Deutsche TCG-Shops ─────────────────────────────────────────
    "Gate to the Games":    "https://www.gate-to-the-games.de/search?sSearch={query}",
    "Cardbuddys":           "https://cardbuddys.de/search?search={query}",
    "Games Island":         "https://games-island.eu/search?sSearch={query}",
    "Trader Online":        "https://www.trader-online.de/search?sSearch={query}",
    "TCG-Corner":           "https://www.tcg-corner.de/search?q={query}",
    "Pokeviert":            "https://pokeviert.de/?s={query}",
    "Cardicuno":            "https://cardicuno.de/search?q={query}",
    "Collect-It":           "https://collect-it.de/search?q={query}",
    "Kofuku":               "https://kofuku.de/?s={query}",
    "Legendary Cards":      "https://legendary-cards.de/search?q={query}",
    "Helden der Freizeit":  "https://www.helden-der-freizeit.de/search?q={query}",
    "bigpanda":             "https://www.bigpanda.de/search?query={query}",
    "Fantasywelt":          "https://www.fantasywelt.de/search?sSearch={query}",
    "Spiele-Offensive":     "https://www.spiele-offensive.de/search?q={query}",
    "Cardgame Corner":      "https://cardgamecorner.de/search?type=product&q={query}",
    "Poke-Corner":          "https://www.poke-corner.de/search?q={query}",
    "Mythic Games":         "https://mythicgames.de/search?q={query}",
    "Lucky Card Shop":      "https://luckycardshop.de/search?q={query}",
    # ── Große DE Händler ───────────────────────────────────────────
    "Amazon DE":            "https://www.amazon.de/s?k={query}+pokemon",
    "eBay DE":              "https://www.ebay.de/sch/i.html?_nkw={query}+pokemon",
    "Smyths":               "https://www.smythstoys.com/de/de-de/search/?text={query}",
    "Müller":               "https://www.mueller.de/search/?query={query}",
    "GameStop DE":          "https://www.gamestop.de/SearchResult/QuickSearch?q={query}",
    "MediaMarkt":           "https://www.mediamarkt.de/de/search.html?query={query}",
    "Saturn":               "https://www.saturn.de/de/search.html?query={query}",
    "OTTO":                 "https://www.otto.de/suche/{query}/",
    "Kaufland":             "https://www.kaufland.de/s/?search_value={query}",
    "Thalia":               "https://www.thalia.de/suche?sq={query}",
    "Rossmann":             "https://www.rossmann.de/de/search?text={query}",
    "dm":                   "https://www.dm.de/search?query={query}",
    # ── UK / Internationale TCG-Shops ─────────────────────────────
    "Chaos Cards (UK)":     "https://www.chaoscards.co.uk/search?q={query}",
    "Pokémon Center UK":    "https://www.pokemoncenter.com/search?q={query}",
    "Total Cards (UK)":     "https://www.totalcards.net/search?q={query}",
    "Ludkins (UK)":         "https://www.ludkins.co.uk/search?type=product&q={query}",
    "Magic Madhouse (UK)":  "https://www.magicmadhouse.co.uk/search?q={query}",
    "Zatu Games (UK)":      "https://www.zatugames.com/search?q={query}",
    "Card Merchant (UK)":   "https://www.cardmerchant.co.uk/search?type=product&q={query}",
    # ── JP-Shops ───────────────────────────────────────────────────
    "Plaza Japan":          "https://www.plazajapan.com/search-results/?q={query}",
    "Meccha Japan":         "https://meccha-japan.com/en/search?controller=search&s={query}",
    "Japan2UK":             "https://www.japan2uk.com/search?q={query}",
    "AmiAmi":               "https://www.amiami.com/eng/search/list/?s_keywords={query}",
    "HLJ":                  "https://www.hlj.com/search/?q={query}",
}

# Welche Shops für den Restock-Check aktiv gecheckt werden (nicht alle wegen Rate-Limits)
RESTOCK_CHECK_SHOPS = [
    "Gate to the Games", "Cardbuddys", "Games Island", "Trader Online",
    "TCG-Corner", "Pokeviert", "Cardicuno", "Collect-It", "Kofuku",
    "Legendary Cards", "bigpanda", "Chaos Cards (UK)", "Total Cards (UK)",
    "Ludkins (UK)", "Amazon DE", "Smyths", "GameStop DE", "MediaMarkt",
    "Plaza Japan", "Meccha Japan",
]

PRODUCT_HISTORY: dict  = {}
PRODUCT_TRENDS: dict   = {}
CARD_SEARCH_COUNT: dict = {}

# ─────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────
def normalize_product_query(query: str) -> str:
    q = query.lower().strip()
    # Abkürzungen expandieren
    abbrevs = {
        "etb":   "elite trainer box",
        "ttb":   "top trainer box",
        "upc":   "ultra premium collection",
        "bab":   "build and battle box",
    }
    for short, full in abbrevs.items():
        # Nur als ganzes Wort ersetzen
        import re as _re
        q = _re.sub(rf"\b{short}\b", full, q)
    # DE Set-Namen → EN (für API-Suche)
    for de_name, en_name in SET_ALIASES.items():
        if de_name in q:
            q = q.replace(de_name, en_name)
    return q

def save_price(card_name: str, price: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cursor.execute(
        "INSERT INTO price_history (card_name, price, checked_at) VALUES (?,?,?)",
        (card_name, price, now)
    )
    conn.commit()

def search_pokemon_card(card_name: str, set_name: str = None) -> list:
    url    = "https://api.pokemontcg.io/v2/cards"
    query  = f'name:"{card_name}"'
    if set_name:
        if "151" in set_name.lower():
            query += " set.id:sv3pt5"
        else:
            query += f' set.name:"{set_name}"'
    params = {"q": query, "pageSize": 50}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception:
        pass
    return []

def check_restock(url: str):
    """Gibt True (verfügbar), False (ausverkauft) oder None (unbekannt) zurück."""
    try:
        resp = requests.get(url, timeout=12, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        html = resp.text.lower()

        sold_out_words = [
            "ausverkauft", "out of stock", "sold out",
            "nicht verfügbar", "derzeit nicht verfügbar",
            "momentan nicht verfügbar", "currently unavailable",
            "nicht auf lager", "vergriffen", "not available",
        ]
        for word in sold_out_words:
            if word in html:
                return False

        available_words = [
            "in den warenkorb", "add to cart", "buy now",
            "auf lager", "lieferbar", "sofort lieferbar",
            "in stock", "verfügbar", "jetzt kaufen",
            "zum warenkorb", "bestellen",
        ]
        hits = sum(1 for w in available_words if w in html)
        if hits >= 1:
            return True
        return None
    except Exception:
        return None

# EN → DE Übersetzung für Cardmarket-Suche (CM ist auf Deutsch)
EN_TO_DE_SETS = {
    "lost origin": "Verlorener Ursprung",
    "silver tempest": "Silberne Sturmwinde",
    "evolving skies": "Drachenwandel",
    "fusion strike": "Fusionsangriff",
    "brilliant stars": "Strahlende Sterne",
    "chilling reign": "Schaurige Herrschaft",
    "destined rivals": "Ewige Rivalen",
    "surging sparks": "Stürmische Funken",
    "stellar crown": "Stellarkrone",
    "shrouded fable": "Verborgene Fabel",
    "twilight masquerade": "Maskerade im Zwielicht",
    "temporal forces": "Zeitliche Mächte",
    "paldean fates": "Paldeas Schicksale",
    "paradox rift": "Paradoxrift",
    "scarlet & violet 151": "151",
    "obsidian flames": "Obsidianflammen",
    "paldea evolved": "Entwicklungen in Paldea",
    "scarlet & violet": "Karmesin & Purpur",
    "crown zenith": "Zenit der Könige",
    "astral radiance": "Astralglanz",
    "darkness ablaze": "Flammen der Finsternis",
    "rebel clash": "Clash der Rebellen",
    "sword & shield": "Schwert & Schild",
    "vivid voltage": "Farbenschock",
    "battle styles": "Kampfstile",
    "journey together": "Reisegefährten",
}

PRODUCT_EN_TO_DE = {
    "elite trainer box": "Top Trainer Box",
    "etb": "Top Trainer Box",
    "booster display": "Booster Display",
    "display": "Booster Display",
    "case": "Case",
    "ultra premium collection": "Ultra Premium Collection",
    "upc": "Ultra Premium Collection",
    "booster bundle": "Booster Bundle",
    "mini tin": "Mini Tin",
    "tin": "Tin",
    "collection box": "Kollektion",
    "build and battle box": "Kampf-Akademie",
}

def get_cardmarket_de_url(product_query: str) -> str:
    """Gibt Cardmarket Suchergebnisseite zurück – zeigt ALLE passenden Produkte."""
    q = product_query.lower().strip()
    # EN Set-Namen → DE übersetzen
    for en, de in EN_TO_DE_SETS.items():
        if en in q:
            q = q.replace(en, de)
    # Produkttypen NICHT übersetzen – Suchbegriff breit lassen
    # damit alle Varianten (18er, 36er, Case etc.) erscheinen
    # Nur ETB/UPC expandieren weil CM die kennt
    q = re.sub(r"\betb\b", "Elite Trainer Box", q)
    q = re.sub(r"\bupc\b", "Ultra Premium Collection", q)
    encoded = q.strip().replace(" ", "%20")
    return (
        f"https://www.cardmarket.com/de/Pokemon/Products/Search"
        f"?searchString={encoded}&sellerCountry=7&sortBy=price_asc"
    )

def get_cardmarket_card_url(card_name: str, set_name: str = None, number: str = None) -> str:
    """Gibt direkten Cardmarket Singles Link zurück – mit DE Set-Name + Kartennummer."""
    de_set = ""
    if set_name:
        # EN → DE übersetzen
        set_lower = set_name.lower()
        for en, de in EN_TO_DE_SETS.items():
            if en in set_lower:
                de_set = de
                break
        if not de_set:
            de_set = set_name  # Fallback: original behalten

    # Suchstring aufbauen: Kartenname + DE Set-Name + Nummer falls vorhanden
    parts = [card_name]
    if de_set:
        parts.append(de_set)
    if number and number.isdigit():
        parts.append(number)

    q       = " ".join(parts)
    encoded = q.replace(" ", "%20")
    return (
        f"https://www.cardmarket.com/de/Pokemon/Products/Singles/Search"
        f"?searchString={encoded}&minCondition=2&sortBy=price_asc"
    )

def find_product_link(search_url: str, query: str) -> str:
    """Extrahiert direkten Produktlink aus Shop-Suchergebnisseite."""
    SKIP = {
        "cart", "checkout", "account", "login", "register",
        "impressum", "datenschutz", "agb", "newsletter",
        "javascript", "mailto", "facebook", "instagram", "twitter",
        "cookie", "privacy", "legal", "wishlist", "blog",
    }
    PRODUCT_PATH_HINTS = {
        "gate-to-the-games.de":  ["/pokemon-", "/produkt", "/detail"],
        "cardbuddys.de":         ["/products/", "/pokemon"],
        "games-island.eu":       ["/pokemon", "/detail"],
        "trader-online.de":      ["/pokemon", "/detail"],
        "tcg-corner.de":         ["/products/"],
        "pokeviert.de":          ["/produkt", "/shop"],
        "cardicuno.de":          ["/products/"],
        "collect-it.de":         ["/products/"],
        "kofuku.de":             ["/produkt", "/shop"],
        "chaoscards.co.uk":      ["/product", "/pokemon"],
        "totalcards.net":        ["/product", "/pokemon"],
        "ludkins.co.uk":         ["/products/"],
        "magicmadhouse.co.uk":   ["/product"],
        "zatugames.com":         ["/product"],
        "plazajapan.com":        ["/product"],
        "meccha-japan.com":      ["/pokemon"],
        "legendary-cards.de":    ["/products/"],
        "bigpanda.de":           ["/detail", "/produkt"],
        "amazon.de":             ["/dp/", "/gp/product"],
        "smythstoys.com":        ["/product", "/pokemon"],
        "gamestop.de":           ["/products/"],
    }
    try:
        resp = requests.get(search_url, timeout=12, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        html        = resp.text
        domain_m    = re.search(r"https?://([^/]+)", search_url)
        domain      = domain_m.group(1) if domain_m else ""
        links = re.findall(r'href="([^"]+)"', html)
        query_words = [w for w in query.lower().split() if len(w) > 2]
        path_hints  = []
        for d, hints in PRODUCT_PATH_HINTS.items():
            if d in domain:
                path_hints = hints
                break

        best_link  = search_url
        best_score = 0

        for link in links:
            link_lower = link.lower()
            if any(s in link_lower for s in SKIP):
                continue
            if link.startswith("#") or link.startswith("javascript"):
                continue
            word_score  = sum(1 for w in query_words if w in link_lower)
            if word_score == 0:
                continue
            path_bonus  = 5 if path_hints and any(h in link_lower for h in path_hints) else 0
            score       = word_score + path_bonus
            if score > best_score:
                full_link = urljoin(search_url, link)
                if domain and domain not in full_link:
                    continue
                best_score = score
                best_link  = full_link

        return best_link if best_score >= 1 else search_url
    except Exception as e:
        print(f"⚠️ find_product_link ({search_url[:40]}): {e}")
        return search_url

# ─────────────────────────────────────────
# START / MENU
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = str(update.effective_user.id)
    subbed   = is_subscribed(user_id)
    sub_text = "✅ Premium aktiv" if subbed else "🔒 Kein Abo"

    keyboard = [
        ["🔍 Suchen", "📦 Meine Watchlist"],
        ["🔔 Restock-Alerts", "💳 Abo & Kündigung"],
        ["📈 Preise", "❓ Hilfe"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"🃏 <b>AnzarDex TCG Bot</b>\n\n"
        f"Dein persönlicher Pokémon TCG Tracker 🔥\n\n"
        f"Status: {sub_text}\n\n"
        f"Einfach einen Produktnamen oder Kartennamen eintippen!\n"
        f"<i>Beispiele: 151 etb · charizard 151 · destined rivals display</i>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )

# ─────────────────────────────────────────
# SUBSCRIPTION – Kauf-Flow
# ─────────────────────────────────────────
async def abo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    subbed  = is_subscribed(user_id)

    if subbed:
        keyboard = [
            [InlineKeyboardButton("❌ Abo kündigen", callback_data="cancel_sub")],
            [InlineKeyboardButton("ℹ️ Abo-Details", callback_data="sub_details")],
        ]
        await update.message.reply_text(
            "✅ <b>Dein Abo ist aktiv!</b>\n\n"
            "Du hast Zugriff auf alle Premium-Funktionen.\n\n"
            "Möchtest du dein Abo verwalten?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        keyboard = [
            [InlineKeyboardButton("💳 Jetzt abonnieren – 6,99 €/Monat", callback_data="buy_sub")],
        ]
        await update.message.reply_text(
            "🔓 <b>AnzarDex Premium</b>\n\n"
            "4,99 € pro Monat – jederzeit kündbar\n\n"
            "✅ Automatische Restock-Alerts\n"
            "✅ Preisalarme für alle Karten & Produkte\n"
            "✅ Alle Sets EN/DE/JP – immer aktuell\n"
            "✅ Günstigster Cardmarket-Preis (DE)\n"
            "✅ Unbegrenzte Watchlist\n"
            "✅ Shop-Links bei Verfügbarkeit\n\n"
            "<b>Zahlung:</b> Kreditkarte, Debitkarte, Apple Pay, Google Pay, PayPal\n"
            "(Kreditkarte, Debitkarte, Apple Pay, Google Pay)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

async def buy_sub_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id  = str(query.from_user.id)
    username = query.from_user.username or ""

    # Immer live aus Umgebungsvariablen lesen
    stripe_link   = os.getenv("STRIPE_PAYMENT_LINK", "").strip()
    stripe_secret = os.getenv("STRIPE_SECRET_KEY", "").strip()

    if stripe_link:
        # Stripe Payment Link mit User-ID – vollautomatische Freischaltung
        full_link = f"{stripe_link}?client_reference_id={user_id}"
        keyboard  = [[InlineKeyboardButton(
            "💳 Jetzt abonnieren – 6,99 €/Monat",
            url=full_link
        )]]
        await query.message.reply_text(
            "💳 <b>AnzarDex Premium – 6,99 €/Monat</b>\n\n"
            "✅ Kreditkarte, Debitkarte, Apple Pay, Google Pay\n"
            "✅ Automatische Freischaltung nach Zahlung\n"
            "✅ Jederzeit kündbar\n\n"
            "Tippe auf den Button und zahle sicher über Stripe 👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await query.message.reply_text(
            "⚠️ <b>Zahlung noch nicht eingerichtet.</b>\n\n"
            "Bitte kontaktiere den Bot-Betreiber.",
            parse_mode="HTML"
        )

async def send_invoice(message, user_id: str):
    if not STRIPE_TOKEN:
        await message.reply_text(
            "⚠️ Zahlungen noch nicht eingerichtet.\n"
            "Bitte STRIPE_TOKEN in Railway eintragen."
        )
        return
    await message.reply_invoice(
        title="AnzarDex TCG Premium",
        description=(
            "1 Monat Premium:\n"
            "• Restock-Alerts für alle Produkte\n"
            "• Preisalarme für alle Karten\n"
            "• Alle Sets EN/DE/JP\n"
            "• Günstigster Cardmarket-Preis DE"
        ),
        payload=f"sub_{user_id}",
        provider_token=STRIPE_TOKEN,
        currency=CURRENCY,
        prices=[LabeledPrice("AnzarDex Premium – 1 Monat", MONTHLY_PRICE)],
        need_name=False,
        need_email=False,
        is_flexible=False,
    )

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = str(update.effective_user.id)
    username  = update.effective_user.username or ""
    charge_id = update.message.successful_payment.telegram_payment_charge_id
    now       = datetime.now()
    if now.month == 12:
        expires = now.replace(year=now.year + 1, month=1)
    else:
        expires = now.replace(month=now.month + 1)

    cursor.execute(
        """
        INSERT INTO subscriptions
            (user_id, username, status, plan, started_at, expires_at, telegram_payment_charge_id)
        VALUES (?,?,'active','monthly',?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            status='active',
            started_at=excluded.started_at,
            expires_at=excluded.expires_at,
            telegram_payment_charge_id=excluded.telegram_payment_charge_id
        """,
        (user_id, username, now.isoformat(), expires.isoformat(), charge_id)
    )
    conn.commit()

    await update.message.reply_text(
        "🎉 <b>Zahlung erfolgreich! Willkommen bei AnzarDex Premium!</b>\n\n"
        "✅ Restock-Alerts aktiv\n"
        "✅ Preisalarme aktiv\n"
        "✅ Alle Sets EN/DE/JP\n\n"
        f"Gültig bis: <b>{expires.strftime('%d.%m.%Y')}</b>\n\n"
        "Tippe /start um loszulegen!",
        parse_mode="HTML",
    )

async def cancel_sub_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    keyboard = [
        [
            InlineKeyboardButton("✅ Ja, kündigen", callback_data="confirm_cancel_sub"),
            InlineKeyboardButton("❌ Abbrechen", callback_data="back_to_abo"),
        ]
    ]
    await query.message.edit_text(
        "⚠️ <b>Abo wirklich kündigen?</b>\n\n"
        "Dein Abo läuft bis zum Ende des bezahlten Zeitraums weiter.\n"
        "Danach hast du keinen Zugriff mehr auf Premium-Funktionen.\n\n"
        "Möchtest du wirklich kündigen?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def confirm_cancel_sub_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    cursor.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE user_id=?",
        (user_id,)
    )
    conn.commit()

    await query.message.edit_text(
        "✅ <b>Abo wurde gekündigt.</b>\n\n"
        "Du hast bis zum Ende deines Abrechnungszeitraums weiter Zugriff.\n"
        "Danach läuft das Abo automatisch aus.\n\n"
        "Wir hoffen dich bald wiederzusehen! 👋",
        parse_mode="HTML",
    )

async def sub_details_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    cursor.execute(
        "SELECT status, plan, started_at, expires_at FROM subscriptions WHERE user_id=?",
        (user_id,)
    )
    row = cursor.fetchone()
    if not row:
        await query.message.edit_text("Kein Abo gefunden.")
        return

    status, plan, started, expires = row
    status_emoji = "✅" if status == "active" else ("⚠️" if status == "cancelled" else "❌")

    await query.message.edit_text(
        f"📋 <b>Deine Abo-Details</b>\n\n"
        f"Status: {status_emoji} {status.capitalize()}\n"
        f"Plan: {plan.capitalize()}\n"
        f"Beginn: {started[:10] if started else '–'}\n"
        f"Gültig bis: {expires[:10] if expires else '–'}\n\n"
        f"Preis: 4,99 €/Monat",
        parse_mode="HTML",
    )

# ─────────────────────────────────────────
# KARTEN-SUCHE
# ─────────────────────────────────────────
async def send_card_details(message, card):
    name    = card.get("name", "?")
    set_obj = card.get("set", {})
    set_nm  = set_obj.get("name", "?")
    set_id  = set_obj.get("id", "")
    number  = card.get("number", "?")
    rarity  = card.get("rarity", "Unbekannt")
    image   = card.get("images", {}).get("large")
    cm_data = card.get("cardmarket", {})
    prices  = cm_data.get("prices", {})

    trend = prices.get("trendPrice")
    low   = prices.get("lowPrice")
    avg   = prices.get("averageSellPrice")

    # Direkter Cardmarket Link aus der API – geht DIREKT zur richtigen Karte
    cm_url = cm_data.get("url", "")
    if cm_url:
        if not cm_url.startswith("http"):
            cm_url = "https://www.cardmarket.com" + cm_url
    else:
        # Fallback: Suchanfrage ohne DE-Filter
        cm_url = get_cardmarket_card_url(name, set_nm, number if number.isdigit() else None)

    preis_zeilen = []
    if low:
        preis_zeilen.append(f"💰 <b>Günstigster Preis:</b> {low} €")
    if trend:
        preis_zeilen.append(f"📉 <b>Trend:</b> {trend} €")
    if avg:
        preis_zeilen.append(f"📊 <b>Ø Verkauf:</b> {avg} €")
    if not preis_zeilen:
        preis_zeilen.append("💰 Keine Preisdaten verfügbar")

    text = (
        f"🃏 <b>{name}</b>\n"
        f"📦 {set_nm} · #{number} · {rarity}\n\n"
        + "\n".join(preis_zeilen)
    )

    # callback_data max 64 Bytes – nur Kartenname kürzen
    safe_name   = name[:28].strip()
    track_cb    = f"tc_{safe_name}"    # max ~35 Bytes
    untrack_cb  = f"utc_{safe_name}"   # max ~35 Bytes

    keyboard = [
        [
            InlineKeyboardButton("⭐ Beobachten", callback_data=track_cb),
            InlineKeyboardButton("❌ Entfernen",  callback_data=untrack_cb),
        ],
        [InlineKeyboardButton("🛒 Direkt auf Cardmarket", url=cm_url)],
    ]

    if image:
        await message.reply_photo(
            photo=image, caption=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await message.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def preis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Benutze: /preis charizard 151")
        return
    await _search_card(update.message, query)

async def _search_card(message, query: str):
    CARD_SEARCH_COUNT[query.lower()] = CARD_SEARCH_COUNT.get(query.lower(), 0) + 1
    query_lower = query.lower()

    # DE → EN alias
    for de, en in SET_ALIASES.items():
        if de in query_lower:
            query_lower = query_lower.replace(de, en)

    # Set erkennen
    matched_set = None
    best_match  = ""
    for set_name in ALL_SETS.keys():
        if set_name in query_lower and len(set_name) > len(best_match):
            best_match = set_name

    if "151" in query_lower and "set" not in query_lower:
        matched_set = "scarlet & violet 151"
    elif best_match:
        matched_set = best_match

    # Kartenname extrahieren (Set-Wörter rausfiltern)
    set_words = set(matched_set.split()) if matched_set else set()
    card_name_words = []
    for word in query_lower.split():
        if word in set_words or word == "151":
            continue
        card_name_words.append(word)

    card_name = " ".join(card_name_words).strip()
    if not card_name:
        card_name = query_lower

    cards = search_pokemon_card(card_name, matched_set)

    # Scoring – JP-Karten nach unten, EN bevorzugen, richtiges Set stark bevorzugen
    scored = []
    for card in cards:
        set_obj  = card.get("set", {})
        set_name = set_obj.get("name", "").lower()
        set_id   = set_obj.get("id", "").lower()
        c_name   = card.get("name", "").lower()
        number   = card.get("number", "")
        score    = 0

        # Exakter Kartenname
        if c_name == card_name.lower():
            score += 20
        elif card_name.lower() in c_name:
            score += 10

        # Richtiges Set
        if matched_set and matched_set.lower() in set_name:
            score += 30

        # JP-Karten stark benachteiligen
        jp_indicators = ["jp", "japanese", "japan", "sv", "s12"]
        lang = card.get("language", "").lower()
        if lang == "ja" or any(j in set_id for j in ["jp", "jap"]):
            score -= 50

        # Nummer ist rein numerisch → EN-Karte wahrscheinlicher
        if number.isdigit():
            score += 5

        scored.append((score, card))

    scored.sort(reverse=True, key=lambda x: x[0])
    # Nur Karten mit positivem Score oder die besten 5
    cards = [c for _, c in scored[:5] if _ > -10]
    if not cards:
        cards = [c for _, c in scored[:5]]

    user_id = str(message.from_user.id) if hasattr(message, "from_user") else "0"
    # RAM Cache
    last_search_results[user_id] = cards
    # DB Cache – überlebt Bot-Neustarts
    now = datetime.now().isoformat()
    cursor.execute("DELETE FROM card_search_cache WHERE user_id=?", (user_id,))
    for pos, card in enumerate(cards, 1):
        cursor.execute(
            "INSERT INTO card_search_cache (user_id, position, card_json, created_at) VALUES (?,?,?,?)",
            (user_id, pos, json.dumps(card), now)
        )
    conn.commit()

    if not cards:
        await message.reply_text("❌ Keine Karte gefunden.")
        return
    if len(cards) == 1:
        await send_card_details(message, cards[0])
        return

    keyboard = []
    for idx, card in enumerate(cards, 1):
        prices = card.get("cardmarket", {}).get("prices", {})
        trend  = prices.get("trendPrice", "?")
        set_nm = card.get("set", {}).get("name", "?")
        num    = card.get("number", "?")
        # User-ID im callback_data damit button_select immer den richtigen Cache trifft
        callback = f"sel_{user_id}_{idx}"
        keyboard.append([InlineKeyboardButton(
            f"{idx}. {card.get('name')} | {set_nm} | #{num} | {trend}€",
            callback_data=callback
        )])

    await message.reply_text(
        f"🔍 Ergebnisse für: <b>{query}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─────────────────────────────────────────
# PRODUKT-SUCHE
# ─────────────────────────────────────────
async def product_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query       = " ".join(context.args) if context.args else update.message.text
    query_lower = query.lower()

    product_type = "Produkt"
    for kw, pname in PRODUCT_TYPES.items():
        if kw in query_lower:
            product_type = pname
            break

    search_query = normalize_product_query(query)
    # Für CM-Link: Original-Query nehmen damit alle Varianten erscheinen
    cm_url = get_cardmarket_de_url(query)

    text = (
        f"📦 <b>Produkt gefunden</b>\n\n"
        f"🔍 <b>Gesucht:</b> {query}\n"
        f"🏷 <b>Typ:</b> {product_type}\n\n"
        f"🛒 Cardmarket zeigt alle Varianten (18er, 36er, Case etc.) sortiert nach Preis.\n"
        f"🔔 Aktiviere den Restock-Alert – du wirst sofort benachrichtigt\n"
        f"wenn das Produkt wieder verfügbar ist, <b>inklusive direktem Shop-Link.</b>"
    )

    keyboard = [
        [InlineKeyboardButton("🛒 Cardmarket DE – Günstigster Preis", url=cm_url)],
        [InlineKeyboardButton("🔔 Restock-Alert aktivieren", callback_data=f"trackproduct_{search_query}")],
    ]

    await update.message.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

# ─────────────────────────────────────────
# MENU HANDLER (Text-Eingabe)
# ─────────────────────────────────────────
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text       = update.message.text
    text_lower = text.lower()

    # Menü-Buttons
    if text == "🔍 Suchen":
        await update.message.reply_text(
            "🔍 Gib einen Karten- oder Produktnamen ein:\n\n"
            "<i>Beispiele:\n"
            "· charizard 151\n"
            "· umbreon vmax evolving skies\n"
            "· destined rivals etb\n"
            "· 151 display\n"
            "· pikachu ex surging sparks</i>",
            parse_mode="HTML"
        )
        return
    if text == "📦 Meine Watchlist":
        await mytracking(update, context)
        return
    if text == "🔔 Restock-Alerts":
        context.args = []
        await myproducts(update, context)
        return
    if text == "💳 Abo & Kündigung":
        await abo_menu(update, context)
        return
    if text == "📈 Preise":
        await update.message.reply_text(
            "📈 Preis-Befehle:\n\n"
            "/preis charizard 151 – Preis suchen\n"
            "/preishistory charizard – Verlauf\n"
            "/setalert 5 – Alert ab 5€ Änderung\n"
            "/setdrops on – Nur Preisrückgänge"
        )
        return
    if text == "❓ Hilfe":
        context.args = []
        await help_command(update, context)
        return

    # Produkt-Erkennung (EN + DE Keywords)
    DE_PRODUCT_KEYWORDS = [
        "booster display", "top trainer box", "trainer box",
        "ultra premium", "kollektion", "display", "tin", "case",
        "elite trainer", "bundle", "booster"
    ]
    is_product = any(kw in text_lower for kw in PRODUCT_KEYWORDS + DE_PRODUCT_KEYWORDS)
    context.args = text.split()

    if is_product:
        await product_search(update, context)
    else:
        await _search_card(update.message, text)

# ─────────────────────────────────────────
# TRACKING
# ─────────────────────────────────────────
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_name = " ".join(context.args)
    if not card_name:
        await update.message.reply_text("Benutze: /track charizard")
        return
    user_id = str(update.effective_user.id)
    cursor.execute(
        "INSERT OR IGNORE INTO tracked_cards (user_id, card_name) VALUES (?,?)",
        (user_id, card_name)
    )
    conn.commit()
    await update.message.reply_text(f"✅ Karte wird beobachtet: {card_name}")

@require_sub
async def trackproduct_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Benutze: /trackproduct 151 etb")
        return
    user_id      = str(update.effective_user.id)
    search_query = normalize_product_query(query)
    cursor.execute(
        "INSERT OR IGNORE INTO tracked_products (user_id, product_query) VALUES (?,?)",
        (user_id, search_query)
    )
    conn.commit()
    await update.message.reply_text(f"🔔 Produkt wird beobachtet:\n📦 {search_query}")

async def product_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not is_subscribed(user_id):
        keyboard = [[InlineKeyboardButton("🔓 Jetzt abonnieren", callback_data="buy_sub")]]
        await query.message.reply_text(
            "🔒 Restock-Alerts sind nur für Premium-Abonnenten.\n\n"
            "Tippe auf den Button um zu abonnieren 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    product_name = query.data.replace("trackproduct_", "")
    cursor.execute(
        "INSERT OR IGNORE INTO tracked_products (user_id, product_query) VALUES (?,?)",
        (user_id, product_name)
    )
    conn.commit()

    keyboard = [[InlineKeyboardButton("❌ Nicht mehr beobachten", callback_data=f"removeproduct_{product_name}")]]
    await query.message.reply_text(
        f"🔔 Restock-Alert aktiviert!\n\n"
        f"📦 {product_name}\n\n"
        f"Du wirst automatisch benachrichtigt wenn das Produkt "
        f"wieder in einem der überwachten Shops verfügbar ist.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def myproducts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute(
        "SELECT product_query FROM tracked_products WHERE user_id=? ORDER BY product_query",
        (user_id,)
    )
    products = cursor.fetchall()
    if not products:
        await update.message.reply_text(
            "Du beobachtest noch keine Produkte.\n\n"
            "Suche nach einem Produkt und tippe auf 🔔 Restock-Alert aktivieren!"
        )
        return
    await update.message.reply_text("🔔 <b>Deine Restock-Alerts</b>", parse_mode="HTML")
    for (product_name,) in products:
        keyboard = [[InlineKeyboardButton("❌ Entfernen", callback_data=f"removeproduct_{product_name}")]]
        await update.message.reply_text(
            f"📦 {product_name}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def mytracking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute("SELECT product_query FROM tracked_products WHERE user_id=?", (user_id,))
    products = cursor.fetchall()
    cursor.execute("SELECT card_name FROM tracked_cards WHERE user_id=?", (user_id,))
    cards = cursor.fetchall()

    if not products and not cards:
        await update.message.reply_text("Du beobachtest aktuell nichts.")
        return

    if products:
        await update.message.reply_text("📦 <b>Beobachtete Produkte</b>", parse_mode="HTML")
        for (pname,) in products:
            keyboard = [[InlineKeyboardButton("❌", callback_data=f"removeproduct_{pname}")]]
            await update.message.reply_text(f"📦 {pname}", reply_markup=InlineKeyboardMarkup(keyboard))

    if cards:
        await update.message.reply_text("🃏 <b>Beobachtete Karten</b>", parse_mode="HTML")
        for (cname,) in cards:
            keyboard = [[InlineKeyboardButton("❌", callback_data=f"removecard_{cname}")]]
            await update.message.reply_text(f"🃏 {cname}", reply_markup=InlineKeyboardMarkup(keyboard))

async def remove_product_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    product_name = query.data.replace("removeproduct_", "")
    cursor.execute(
        "DELETE FROM tracked_products WHERE user_id=? AND product_query=?",
        (user_id, product_name)
    )
    conn.commit()
    await query.message.edit_text(f"❌ Entfernt: {product_name}")

async def remove_card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    card_name = query.data.replace("removecard_", "")
    cursor.execute(
        "DELETE FROM tracked_cards WHERE user_id=? AND card_name=?",
        (user_id, card_name)
    )
    conn.commit()
    await query.message.edit_text(f"❌ Entfernt: {card_name}")

async def button_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data    = query.data  # Format: "sel_USERID_IDX"

    try:
        # Neues Format: sel_USERID_IDX
        if data.startswith("sel_"):
            parts  = data.split("_")
            # parts = ["sel", user_id_part1, ..., idx]
            choice = int(parts[-1])
            cache_id = "_".join(parts[1:-1])
        else:
            # Altes Format: select_IDX
            choice   = int(data.replace("select_", ""))
            cache_id = user_id
    except Exception:
        await query.message.reply_text("❌ Fehler beim Laden der Karte.")
        return

    cards = last_search_results.get(cache_id, [])
    if not cards:
        cards = last_search_results.get(user_id, [])
    # Fallback: aus DB laden (überlebt Neustarts)
    if not cards:
        cursor.execute(
            "SELECT card_json FROM card_search_cache WHERE user_id=? ORDER BY position ASC",
            (user_id,)
        )
        rows = cursor.fetchall()
        if rows:
            cards = [json.loads(r[0]) for r in rows]
            last_search_results[user_id] = cards

    if not cards:
        await query.message.reply_text("❌ Bitte suche die Karte nochmal neu.")
        return

    if choice < 1 or choice > len(cards):
        await query.message.reply_text("❌ Ungültige Auswahl.")
        return

    await send_card_details(query.message, cards[choice - 1])

async def action_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = str(query.from_user.id)

    if data.startswith("utc_"):
        card_name = data[4:]  # "utc_" = 4 Zeichen
        cursor.execute(
            "DELETE FROM tracked_cards WHERE user_id=? AND card_name=?",
            (user_id, card_name)
        )
        conn.commit()
        await query.answer("❌ Nicht mehr beobachtet", show_alert=True)

    elif data.startswith("tc_"):
        card_name = data[3:]  # "tc_" = 3 Zeichen
        cursor.execute(
            "INSERT OR IGNORE INTO tracked_cards (user_id, card_name) VALUES (?,?)",
            (user_id, card_name)
        )
        conn.commit()
        await query.answer("⭐ Karte wird beobachtet!", show_alert=True)

    elif data.startswith("untrack_"):
        card_name = data.replace("untrack_", "").split("|")[0]
        cursor.execute("DELETE FROM tracked_cards WHERE user_id=? AND card_name=?", (user_id, card_name))
        conn.commit()
        await query.answer("❌ Nicht mehr beobachtet", show_alert=True)

    elif data.startswith("track_"):
        card_name = data.replace("track_", "").split("|")[0]
        cursor.execute("INSERT OR IGNORE INTO tracked_cards (user_id, card_name) VALUES (?,?)", (user_id, card_name))
        conn.commit()
        await query.answer("⭐ Karte wird beobachtet!", show_alert=True)

# ─────────────────────────────────────────
# PREISHISTORIE
# ─────────────────────────────────────────
async def preishistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card_name = " ".join(context.args)
    if not card_name:
        await update.message.reply_text("Benutze: /preishistory charizard")
        return
    cursor.execute(
        "SELECT price, checked_at FROM price_history WHERE card_name=? ORDER BY checked_at DESC LIMIT 10",
        (card_name,)
    )
    results = cursor.fetchall()
    if not results:
        await update.message.reply_text("Noch keine Preise gespeichert.")
        return
    text = f"📈 Preisverlauf: <b>{card_name}</b>\n\n"
    for price, ts in results:
        text += f"💰 {price} € — {ts}\n"
    await update.message.reply_text(text, parse_mode="HTML")

# ─────────────────────────────────────────
# SETS
# ─────────────────────────────────────────
async def set_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)
    if not set_name:
        await update.message.reply_text("Benutze: /set scarlet violet 151")
        return
    resp = requests.get(
        "https://api.pokemontcg.io/v2/cards",
        params={"q": f'set.name:"{set_name}"', "pageSize": 20},
        timeout=10
    )
    cards = resp.json().get("data", [])
    if not cards:
        await update.message.reply_text("Kein Set gefunden.")
        return
    text = f"📦 Karten aus <b>{set_name}</b>\n\n"
    for idx, card in enumerate(cards, 1):
        text += f"{idx}. {card.get('name')} #{card.get('number')}\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def allsets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt alle bekannten Sets."""
    if not ALL_SETS:
        await update.message.reply_text("Sets werden geladen...")
        return
    # Nur die letzten 30 Sets zeigen
    recent = list(ALL_SETS.keys())[-30:]
    text   = f"📦 <b>Bekannte Sets ({len(ALL_SETS)} total)</b>\n\n"
    text  += "\n".join(f"• {s.title()}" for s in recent)
    text  += f"\n\n<i>Zuletzt aktualisiert: {SETS_LAST_LOADED}</i>"
    await update.message.reply_text(text, parse_mode="HTML")

async def favset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)
    if not set_name:
        await update.message.reply_text("Benutze: /favset 151")
        return
    user_id = str(update.effective_user.id)
    cursor.execute(
        "INSERT OR IGNORE INTO favorite_sets (user_id, set_name) VALUES (?,?)",
        (user_id, set_name)
    )
    conn.commit()
    await update.message.reply_text(f"⭐ Set gespeichert: {set_name}")

async def meinesets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute("SELECT set_name FROM favorite_sets WHERE user_id=?", (user_id,))
    results = cursor.fetchall()
    if not results:
        await update.message.reply_text("Du hast noch keine Favoriten.")
        return
    text = "⭐ <b>Deine Lieblingssets</b>\n\n"
    for idx, (name,) in enumerate(results, 1):
        text += f"{idx}. {name}\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def unfavset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_name = " ".join(context.args)
    if not set_name:
        await update.message.reply_text("Benutze: /unfavset 151")
        return
    user_id = str(update.effective_user.id)
    cursor.execute("DELETE FROM favorite_sets WHERE user_id=? AND set_name=?", (user_id, set_name))
    conn.commit()
    await update.message.reply_text(f"❌ Set entfernt: {set_name}")

# ─────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────
async def setalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Benutze: /setalert 5")
        return
    try:
        threshold = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Bitte eine Zahl eingeben.")
        return
    user_id = str(update.effective_user.id)
    cursor.execute(
        "INSERT INTO user_settings (user_id, alert_threshold) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET alert_threshold=excluded.alert_threshold",
        (user_id, threshold)
    )
    conn.commit()
    await update.message.reply_text(f"✅ Alert-Grenze gesetzt auf {threshold} €")

async def setdrops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Benutze: /setdrops on | off")
        return
    value     = context.args[0].lower()
    only_drops = "yes" if value == "on" else "no"
    user_id   = str(update.effective_user.id)
    cursor.execute(
        "INSERT INTO user_settings (user_id, only_drops) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET only_drops=excluded.only_drops",
        (user_id, only_drops)
    )
    conn.commit()
    await update.message.reply_text(
        f"✅ Nur Preis-Drops: {'aktiviert' if only_drops=='yes' else 'deaktiviert'}"
    )

# ─────────────────────────────────────────
# SHOP URL TRACKING (manuell)
# ─────────────────────────────────────────
async def trackurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Benutze: /trackurl https://shop.de/produkt")
        return
    url     = context.args[0]
    user_id = str(update.effective_user.id)
    cursor.execute("INSERT INTO tracked_urls (user_id, url) VALUES (?,?)", (user_id, url))
    conn.commit()
    await update.message.reply_text(f"✅ URL wird überwacht:\n{url}")

async def myurls(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute("SELECT url FROM tracked_urls WHERE user_id=?", (user_id,))
    results = cursor.fetchall()
    if not results:
        await update.message.reply_text("Du beobachtest noch keine URLs.")
        return
    text = "🔗 <b>Deine überwachten URLs</b>\n\n"
    for idx, (url,) in enumerate(results, 1):
        text += f"{idx}. {url}\n\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def untrackurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Benutze: /untrackurl 1")
        return
    user_id = str(update.effective_user.id)
    try:
        idx = int(context.args[0]) - 1
    except Exception:
        await update.message.reply_text("Bitte eine Nummer eingeben.")
        return
    cursor.execute("SELECT url FROM tracked_urls WHERE user_id=?", (user_id,))
    urls = cursor.fetchall()
    if idx < 0 or idx >= len(urls):
        await update.message.reply_text("Ungültige Nummer.")
        return
    cursor.execute("DELETE FROM tracked_urls WHERE user_id=? AND url=?", (user_id, urls[idx][0]))
    conn.commit()
    await update.message.reply_text("🗑 URL entfernt.")

# ─────────────────────────────────────────
# HILFE
# ─────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>AnzarDex TCG Bot – Hilfe</b>\n\n"
        "🔍 <b>Suchen (einfach eintippen):</b>\n"
        "charizard 151\n"
        "pikachu ex surging sparks\n"
        "umbreon vmax evolving skies\n"
        "destined rivals etb\n"
        "151 display\n\n"
        "📦 <b>Produkt-Befehle:</b>\n"
        "/trackproduct 151 etb – Restock-Alert\n"
        "/myproducts – Deine Alerts\n"
        "/trackurl https://… – Shop-URL tracken\n\n"
        "🃏 <b>Karten-Befehle:</b>\n"
        "/preis pikachu\n"
        "/track charizard\n"
        "/preishistory charizard\n"
        "/setalert 5 – Alert bei ±5€\n"
        "/setdrops on – Nur Preis-Drops\n\n"
        "📦 <b>Sets:</b>\n"
        "/allsets – Alle bekannten Sets\n"
        "/favset 151 – Set favorisieren\n"
        "/meinesets\n\n"
        "💳 <b>Abo:</b>\n"
        "/abo – Abo verwalten\n\n"
        "<i>Für Restock-Alerts brauchst du ein Premium-Abo (4,99€/Monat)</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ─────────────────────────────────────────
# AUTOMATISCHE JOBS
# ─────────────────────────────────────────
async def job_refresh_sets(context: ContextTypes.DEFAULT_TYPE):
    """Lädt alle Sets neu (täglich)."""
    global ALL_SETS
    print("🔄 Sets werden aktualisiert...")
    ALL_SETS = load_all_sets()
    print(f"✅ {len(ALL_SETS)} Sets geladen.")

async def job_price_check(context: ContextTypes.DEFAULT_TYPE):
    """Prüft Preise aller getrackten Karten (alle 5 Min)."""
    cursor.execute("SELECT user_id, card_name FROM tracked_cards")
    tracked = cursor.fetchall()

    for user_id, card_name in tracked:
        try:
            cards = search_pokemon_card(card_name)
            if not cards:
                continue
            card      = cards[0]
            name      = card.get("name", card_name)
            prices    = card.get("cardmarket", {}).get("prices", {})
            new_price = prices.get("trendPrice")
            if not new_price:
                continue

            cursor.execute(
                "SELECT price FROM price_history WHERE card_name=? ORDER BY checked_at DESC LIMIT 1",
                (name,)
            )
            result = cursor.fetchone()
            save_price(name, new_price)

            if not result:
                continue

            old_price  = result[0]
            difference = round(new_price - old_price, 2)

            cursor.execute(
                "SELECT alert_threshold, only_drops FROM user_settings WHERE user_id=?",
                (user_id,)
            )
            settings   = cursor.fetchone()
            threshold  = float(settings[0]) if settings and settings[0] else 2.0
            only_drops = settings[1] if settings else "no"

            if abs(difference) < threshold:
                continue
            if only_drops == "yes" and difference > 0:
                continue

            # Gleichen Alert nicht zweimal senden
            cursor.execute(
                "SELECT last_price FROM sent_price_alerts WHERE card_name=?",
                (name,)
            )
            last = cursor.fetchone()
            if last and last[0] == new_price:
                continue

            cursor.execute(
                "INSERT INTO sent_price_alerts (card_name, last_price) VALUES (?,?) "
                "ON CONFLICT(card_name) DO UPDATE SET last_price=excluded.last_price",
                (name, new_price)
            )
            conn.commit()

            emoji = "📈" if difference > 0 else "📉"
            text  = (
                f"🔔 <b>Preisalarm</b>\n\n"
                f"🃏 {name}\n"
                f"💰 Neuer Preis: <b>{new_price} €</b>\n"
                f"{emoji} {'Gestiegen' if difference > 0 else 'Gefallen'} um {abs(difference)} €\n\n"
                f"<a href='{get_cardmarket_card_url(name)}'>Cardmarket (DE)</a>"
            )
            await context.bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
        except Exception as e:
            print(f"⚠️ Preis-Check Fehler ({card_name}): {e}")

async def job_restock_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Restock-Check für alle getrackten Produkte (alle 10 Min).
    Für jeden User + Produkt werden die Shop-Such-URLs live geprüft.
    """
    cursor.execute("SELECT DISTINCT user_id, product_query FROM tracked_products")
    tracked = cursor.fetchall()

    for user_id, product_query in tracked:
        if not is_subscribed(user_id):
            continue

        encoded = product_query.replace(" ", "+")

        # Nur aktive Restock-Shops durchsuchen
        active_shops = {k: v for k, v in SHOP_SEARCH_PATTERNS.items() if k in RESTOCK_CHECK_SHOPS}
        for shop_name, pattern in active_shops.items():
            try:
                search_url   = pattern.format(query=encoded)
                product_url  = find_product_link(search_url, product_query)
                status       = check_restock(product_url)

                if status is not True:
                    # Wenn nicht mehr verfügbar: Alert-Flag löschen
                    cursor.execute(
                        "DELETE FROM sent_restock_alerts WHERE product_name=? AND shop_name=?",
                        (product_query, shop_name)
                    )
                    conn.commit()
                    continue

                # Prüfen ob Alert für diese Kombi bereits gesendet
                cursor.execute(
                    "SELECT status FROM sent_restock_alerts WHERE product_name=? AND shop_name=?",
                    (product_query, shop_name)
                )
                already_sent = cursor.fetchone()
                if already_sent and already_sent[0] == "sent":
                    continue

                # Alert senden mit direktem Shop-Link + Alert deaktivieren Button
                cm_url = get_cardmarket_de_url(product_query)
                text   = (
                    f"🚨 <b>RESTOCK GEFUNDEN!</b> 🚨\n\n"
                    f"📦 <b>{product_query.upper()}</b>\n"
                    f"🏪 Shop: <b>{shop_name}</b>\n\n"
                    f"🛒 <b>Direkt zum Produkt:</b>\n{product_url}\n\n"
                    f"💳 <b>Cardmarket DE:</b>\n{cm_url}\n\n"
                    f"⚡ <i>Schnell sein – Restocks sind oft schnell ausverkauft!</i>"
                )
                alert_keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🔕 Alert deaktivieren",
                        callback_data=f"removeproduct_{product_query[:50]}"
                    )]
                ])
                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=alert_keyboard
                )

                # Flag speichern
                cursor.execute(
                    "INSERT INTO sent_restock_alerts (product_name, shop_name, status, sent_at) "
                    "VALUES (?,?,?,?) ON CONFLICT(product_name, shop_name) "
                    "DO UPDATE SET status='sent', sent_at=excluded.sent_at",
                    (product_query, shop_name, "sent", datetime.now().isoformat())
                )
                conn.commit()

            except Exception as e:
                print(f"⚠️ Restock-Check Fehler ({shop_name} / {product_query}): {e}")

async def job_url_restock_check(context: ContextTypes.DEFAULT_TYPE):
    """Prüft manuell hinzugefügte URLs auf Verfügbarkeit (alle 10 Min)."""
    cursor.execute("SELECT user_id, url FROM tracked_urls")
    rows = cursor.fetchall()

    for user_id, url in rows:
        try:
            result     = check_restock(url)
            new_status = "available" if result is True else "soldout"

            cursor.execute(
                "SELECT last_status FROM restock_status WHERE url=?", (url,)
            )
            existing   = cursor.fetchone()
            old_status = existing[0] if existing else None

            cursor.execute(
                "INSERT INTO restock_status (url, last_status) VALUES (?,?) "
                "ON CONFLICT(url) DO UPDATE SET last_status=excluded.last_status",
                (url, new_status)
            )
            conn.commit()

            if old_status != new_status and new_status == "available":
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🚨 <b>RESTOCK ERKANNT!</b>\n\n"
                        f"🛒 <a href='{url}'>{url}</a>"
                    ),
                    parse_mode="HTML"
                )
        except Exception as e:
            print(f"⚠️ URL-Check Fehler ({url}): {e}")

# ─────────────────────────────────────────
# CALLBACK HANDLER (Button-Dispatcher)
# ─────────────────────────────────────────
async def callback_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data.startswith("sel_") or data.startswith("select_"):
        await button_select(update, context)
    elif data == "buy_sub":
        await buy_sub_button(update, context)
    elif data == "cancel_sub":
        await cancel_sub_button(update, context)
    elif data == "confirm_cancel_sub":
        await confirm_cancel_sub_button(update, context)
    elif data == "sub_details":
        await sub_details_button(update, context)
    elif data == "back_to_abo":
        await query.answer()
        await query.message.edit_text(
            "💳 Tippe /abo um dein Abo zu verwalten."
        )
    elif data.startswith("select_"):
        await button_select(update, context)
    elif data.startswith("tc_") or data.startswith("utc_") or data.startswith("track_") or data.startswith("untrack_"):
        await action_button_handler(update, context)
    elif data.startswith("trackproduct_"):
        await product_button_handler(update, context)
    elif data.startswith("removeproduct_"):
        await remove_product_handler(update, context)
    elif data.startswith("removecard_"):
        await remove_card_handler(update, context)
    else:
        await query.answer()

# ─────────────────────────────────────────
# ADMIN
# ─────────────────────────────────────────
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ADMIN_ID:
        return

async def admin_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /adduser TELEGRAM_ID  →  schaltet User frei"""
    if str(update.effective_user.id) != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Benutze: /adduser 123456789")
        return
    target_id = context.args[0]
    now     = datetime.now()
    expires = now.replace(month=now.month % 12 + 1) if now.month < 12 else now.replace(year=now.year+1, month=1)
    cursor.execute(
        """
        INSERT INTO subscriptions (user_id, username, status, plan, started_at, expires_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            status='active', started_at=excluded.started_at, expires_at=excluded.expires_at
        """,
        (target_id, "", "active", "monthly", now.isoformat(), expires.isoformat())
    )
    conn.commit()
    await update.message.reply_text(f"✅ User {target_id} freigeschaltet bis {expires.strftime('%d.%m.%Y')}")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 <b>Willkommen bei AnzarDex Premium!</b>\n\n"
                "Dein Abo wurde aktiviert.\n"
                "✅ Restock-Alerts aktiv\n"
                "✅ Preisalarme aktiv\n"
                "✅ Alle Sets EN/DE/JP\n\n"
                f"Gültig bis: <b>{expires.strftime('%d.%m.%Y')}</b>\n\n"
                "Tippe /start um loszulegen!"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

async def admin_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /removeuser TELEGRAM_ID  →  deaktiviert User"""
    if str(update.effective_user.id) != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Benutze: /removeuser 123456789")
        return
    target_id = context.args[0]
    cursor.execute("UPDATE subscriptions SET status='cancelled' WHERE user_id=?", (target_id,))
    conn.commit()
    await update.message.reply_text(f"❌ User {target_id} deaktiviert")
    cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active'")
    active = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM subscriptions")
    total  = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tracked_products")
    prods  = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tracked_cards")
    cards  = cursor.fetchone()[0]

    await update.message.reply_text(
        f"📊 <b>Admin Stats</b>\n\n"
        f"👤 Abonnenten (aktiv): {active}\n"
        f"👥 Gesamt User: {total}\n"
        f"📦 Getrackte Produkte: {prods}\n"
        f"🃏 Getrackte Karten: {cards}\n"
        f"📚 Bekannte Sets: {len(ALL_SETS)}\n"
        f"🕐 Sets geladen: {SETS_LAST_LOADED}",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
# ─────────────────────────────────────────
# FLASK WEBHOOK – Stripe Zahlungen
# ─────────────────────────────────────────
flask_app = Flask(__name__)
telegram_app_ref = None  # wird in main() gesetzt

def activate_subscription(user_id: str, username: str = ""):
    """Abo in DB aktivieren und User benachrichtigen."""
    now = datetime.now()
    if now.month == 12:
        expires = now.replace(year=now.year + 1, month=1)
    else:
        expires = now.replace(month=now.month + 1)

    cursor.execute(
        """
        INSERT INTO subscriptions
            (user_id, username, status, plan, started_at, expires_at)
        VALUES (?,?,'active','monthly',?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            status='active',
            started_at=excluded.started_at,
            expires_at=excluded.expires_at
        """,
        (user_id, username, now.isoformat(), expires.isoformat())
    )
    conn.commit()
    print(f"✅ Abo aktiviert für User {user_id} bis {expires.strftime('%d.%m.%Y')}")

    # Telegram-Nachricht senden
    if telegram_app_ref:
        async def send_msg():
            try:
                await telegram_app_ref.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "🎉 <b>Zahlung bestätigt! Willkommen bei AnzarDex Premium!</b>\n\n"
                        "✅ Restock-Alerts aktiv\n"
                        "✅ Preisalarme aktiv\n"
                        "✅ Alle Sets EN/DE/JP\n\n"
                        f"Gültig bis: <b>{expires.strftime('%d.%m.%Y')}</b>\n\n"
                        "Tippe /start um loszulegen! 🚀"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                print(f"⚠️ Telegram-Nachricht fehlgeschlagen: {e}")
        asyncio.run_coroutine_threadsafe(send_msg(), telegram_app_ref.update_queue._loop)

def deactivate_subscription(user_id: str):
    """Abo deaktivieren (z.B. bei Kündigung oder fehlgeschlagener Zahlung)."""
    cursor.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    print(f"❌ Abo deaktiviert für User {user_id}")

@flask_app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload    = flask_request.get_data()
    sig_header = flask_request.headers.get("Stripe-Signature", "")

    # Webhook-Signatur prüfen
    if STRIPE_WEBHOOK_SECRET:
        try:
            # Stripe Signatur manuell verifizieren
            parts = {k: v for k, v in (p.split("=", 1) for p in sig_header.split(","))}
            timestamp = parts.get("t", "")
            signature = parts.get("v1", "")
            signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
            expected = hmac.new(
                STRIPE_WEBHOOK_SECRET.encode(),
                signed_payload.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                print("⚠️ Ungültige Webhook-Signatur")
                return "", 400
        except Exception as e:
            print(f"⚠️ Signatur-Fehler: {e}")
            return "", 400

    try:
        event = json.loads(payload)
    except Exception:
        return "", 400

    event_type = event.get("type", "")
    data_obj   = event.get("data", {}).get("object", {})

    print(f"📨 Stripe Event: {event_type}")

    # Erfolgreiche Zahlung – checkout.session.completed ist der wichtigste Event
    if event_type == "checkout.session.completed":
        # client_reference_id = telegram user_id (wird automatisch gesetzt)
        user_id  = data_obj.get("client_reference_id", "")
        metadata = data_obj.get("metadata", {})
        username = metadata.get("telegram_username", "")
        print(f"📨 Checkout completed: user_id={user_id}")
        if user_id:
            activate_subscription(str(user_id), username)
        else:
            print(f"⚠️ Kein client_reference_id im Checkout-Event!")

    elif event_type == "invoice.payment_succeeded":
        # Folge-Zahlung bei Abo-Verlängerung
        sub_id    = data_obj.get("subscription", "")
        user_id   = data_obj.get("metadata", {}).get("telegram_user_id", "")
        if not user_id:
            # Über Subscription die User-ID finden
            cursor.execute(
                "SELECT user_id FROM subscriptions WHERE telegram_payment_charge_id=?",
                (sub_id,)
            )
            row = cursor.fetchone()
            if row:
                user_id = row[0]
        if user_id:
            activate_subscription(str(user_id))
            print(f"✅ Abo verlängert für {user_id}")

    # Kündigung / fehlgeschlagene Zahlung
    elif event_type in ("customer.subscription.deleted", "invoice.payment_failed"):
        user_id = data_obj.get("metadata", {}).get("telegram_user_id", "")
        if user_id:
            deactivate_subscription(user_id)
            print(f"❌ Abo beendet für {user_id}")

    return "", 200

@flask_app.route("/health", methods=["GET"])
def health():
    return "AnzarDex Bot läuft ✅", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    print(f"🌐 Flask Webhook läuft auf Port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False)

def main():
    global telegram_app_ref
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    jq  = app.job_queue
    jq  = app.job_queue

    # Jobs
    jq.run_repeating(job_price_check,      interval=300,   first=15)   # alle 5 Min
    jq.run_repeating(job_restock_check,    interval=600,   first=30)   # alle 10 Min
    jq.run_repeating(job_url_restock_check,interval=600,   first=45)   # alle 10 Min
    jq.run_repeating(job_refresh_sets,     interval=86400, first=3600) # täglich

    # Commands
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("abo",          abo_menu))
    app.add_handler(CommandHandler("subscribe",    abo_menu))
    app.add_handler(CommandHandler("preis",        preis))
    app.add_handler(CommandHandler("track",        track))
    app.add_handler(CommandHandler("trackproduct", trackproduct_cmd))
    app.add_handler(CommandHandler("myproducts",   myproducts))
    app.add_handler(CommandHandler("mytracking",   mytracking))
    app.add_handler(CommandHandler("mycards",      mytracking))
    app.add_handler(CommandHandler("trackurl",     trackurl))
    app.add_handler(CommandHandler("myurls",       myurls))
    app.add_handler(CommandHandler("untrackurl",   untrackurl))
    app.add_handler(CommandHandler("preishistory", preishistory))
    app.add_handler(CommandHandler("setalert",     setalert))
    app.add_handler(CommandHandler("setdrops",     setdrops))
    app.add_handler(CommandHandler("set",          set_search))
    app.add_handler(CommandHandler("allsets",      allsets))
    app.add_handler(CommandHandler("favset",       favset))
    app.add_handler(CommandHandler("meinesets",    meinesets))
    app.add_handler(CommandHandler("unfavset",     unfavset))
    app.add_handler(CommandHandler("help",         help_command))
    app.add_handler(CommandHandler("admin",        admin_stats))
    app.add_handler(CommandHandler("adduser",      admin_adduser))
    app.add_handler(CommandHandler("removeuser",   admin_removeuser))

    # Payments

    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Callbacks (zentraler Dispatcher)
    app.add_handler(CallbackQueryHandler(callback_dispatcher))

    # Text-Eingabe
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))

    # Flask Webhook in separatem Thread starten
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print("🚀 AnzarDex TCG Bot läuft...")
    app.run_polling()


if __name__ == "__main__":
    main()
