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
from difflib import SequenceMatcher

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
MONTHLY_PRICE    = 499   # Preis in Cent = 6,99€
CURRENCY         = "EUR"
ADMIN_ID         = os.getenv("ADMIN_ID", "")          # Deine Telegram-ID für Admin-Befehle
MONTHLY_PRICE    = 499                                 # Preis in Cent → 4,99 €

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
        CREATE TABLE IF NOT EXISTS price_targets (
            user_id    TEXT,
            card_name  TEXT,
            target_price REAL,
            created_at TEXT,
            PRIMARY KEY (user_id, card_name)
        );
        CREATE TABLE IF NOT EXISTS set_alerts (
            user_id    TEXT PRIMARY KEY,
            active     INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS deal_alerts (
            user_id    TEXT,
            card_name  TEXT,
            threshold_pct INTEGER DEFAULT 15,
            PRIMARY KEY (user_id, card_name)
        );
        CREATE TABLE IF NOT EXISTS portfolio (
            user_id    TEXT,
            card_name  TEXT,
            set_name   TEXT,
            quantity   INTEGER DEFAULT 1,
            buy_price  REAL,
            added_at   TEXT,
            PRIMARY KEY (user_id, card_name, set_name)
        );
        CREATE TABLE IF NOT EXISTS known_sets_notified (
            set_id TEXT PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS amazon_products (
            asin        TEXT PRIMARY KEY,
            product_name TEXT,
            amazon_url  TEXT,
            last_status TEXT DEFAULT 'unknown',
            added_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS amazon_invite_alerts (
            asin    TEXT,
            user_id TEXT,
            sent_at TEXT,
            PRIMARY KEY (asin, user_id)
        );
    """)
    conn.commit()

init_db()

# ─────────────────────────────────────────
# SUBSCRIPTION HELPERS
# ─────────────────────────────────────────
def is_subscribed(user_id: str) -> bool:
    # Admin hat immer vollen Zugang
    if str(user_id) == str(ADMIN_ID):
        return True
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
                "📦 AnzarDexBot Premium – 4,99 €/Monat\n"
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


# ─────────────────────────────────────────
# TCGDEX API – Bessere Kartensuche (EN/DE/JP)
# ─────────────────────────────────────────
TCGDEX_API = "https://api.tcgdex.net/v2"

TCGDEX_LANG_MAP = {
    "jp": "ja", "jpn": "ja", "japanese": "ja",
    "de": "de", "deutsch": "de", "german": "de",
    "en": "en", "english": "en",
}

TCGDEX_SET_ALIASES = {
    # DE → JP Set-Name für TCGDex
    "prismatische entwicklungen":    "terastal festival ex",
    "paldeas schicksale":            "shiny treasure ex",
    "drachenwandel":                 "evolving skies",
    "schaurige herrschaft":          "chilling reign",
    "zenit der könige":              "crown zenith",
    "stürmische funken":             "surging sparks",
    "ewige rivalen":                 "the glory of team rocket",
    "reisegefährten":                "journey together",
    "zeitliche mächte":              "temporal forces",
    "maskerade im zwielicht":        "twilight masquerade",
    "verborgene fabel":              "shrouded fable",
    "stellarkrone":                  "stellar crown",
    "paradoxrift":                   "paradox rift",
    "obsidianflammen":               "obsidian flames",
    "entwicklungen in paldea":       "paldea evolved",
    "verlorener ursprung":           "lost origin",
    "silberne sturmwinde":           "silver tempest",
    "strahlende sterne":             "brilliant stars",
    "fusionsangriff":                "fusion strike",
    "astralglanz":                   "astral radiance",
    "flammen der finsternis":        "darkness ablaze",
    "clash der rebellen":            "rebel clash",
    "farbenschock":                  "vivid voltage",
    "kampfstile":                    "battle styles",
    # JP-eigene Namen
    "terastal festival ex":          "terastal festival ex",
    "shiny treasure ex":             "shiny treasure ex",
    "vmax climax":                   "vmax climax",
    "blue sky stream":               "blue sky stream",
    "eevee heroes":                  "eevee heroes",
    "vstar universe":                "vstar universe",
    "lost abyss":                    "lost abyss",
    "ruler of the black flame":      "ruler of the black flame",
    "crimson haze":                  "crimson haze",
    "night wanderer":                "night wanderer",
    "battle partners":               "battle partners",
    "the glory of team rocket":      "the glory of team rocket",
    "paradise dragona":              "paradise dragona",
    "151":                           "151",
    "pokemon card 151":              "151",
}

def tcgdex_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def tcgdex_detect_language(text: str):
    parts = text.lower().split()
    for part in parts:
        if part in TCGDEX_LANG_MAP:
            return TCGDEX_LANG_MAP[part], part
    return "en", None

def tcgdex_detect_set(text: str):
    text_lower = text.lower()
    # Exakter Match zuerst
    for alias, canonical in TCGDEX_SET_ALIASES.items():
        if alias in text_lower:
            return canonical
    # Fuzzy Match
    best_set = None
    best_score = 0
    for alias, canonical in TCGDEX_SET_ALIASES.items():
        score = tcgdex_similarity(alias, text_lower)
        if score > best_score:
            best_set = canonical
            best_score = score
    return best_set if best_score >= 0.6 else None

def tcgdex_find_cards(pokemon_name: str, set_name: str = None, language: str = "en") -> list:
    """Sucht Karten über TCGDex API – unterstützt EN, DE, JP."""
    try:
        url = f"{TCGDEX_API}/{language}/cards"
        resp = requests.get(url, timeout=20, headers={"User-Agent": "AnzarDexBot/1.0"})
        all_cards = resp.json()
        if not isinstance(all_cards, list):
            return []

        results = []
        for card in all_cards:
            card_name = (card.get("name") or "").lower()
            card_set  = ((card.get("set") or {}).get("name") or "").lower()

            # Pokémon-Name muss vorkommen
            if pokemon_name.lower() not in card_name:
                continue

            # Set prüfen wenn angegeben
            if set_name:
                set_match = (
                    set_name.lower() in card_set or
                    tcgdex_similarity(set_name.lower(), card_set) >= 0.6
                )
                if not set_match:
                    continue

            # Vollständige Kartendetails laden
            card_id = card.get("id", "")
            try:
                detail_url = f"{TCGDEX_API}/{language}/cards/{card_id}"
                detail = requests.get(detail_url, timeout=10).json()
            except Exception:
                detail = card

            results.append({
                "source":  "tcgdex",
                "id":      card_id,
                "name":    detail.get("name", card.get("name", "?")),
                "set":     (detail.get("set") or {}).get("name", "?"),
                "number":  detail.get("localId", card.get("localId", "?")),
                "image":   detail.get("image", card.get("image", "")),
                "lang":    language,
            })
            if len(results) >= 8:
                break

        return results
    except Exception as e:
        print(f"⚠️ TCGDex Fehler: {e}")
        return []

# Amazon Pokémon Store
AMAZON_POKEMON_STORE = "https://www.amazon.de/stores/Pok%C3%A9mon-Sammelkartenspiel/page/EBE7C18D-29BC-41FA-9252-C03AD4C74D4B"

def get_amazon_search_url(query: str) -> str:
    """Direkter Amazon Produktlink für Pokémon TCG."""
    encoded = query.replace(" ", "+")
    return f"https://www.amazon.de/s?k={encoded}+pokemon+karten&rh=n%3A301128"

def get_amazon_product_url(asin: str) -> str:
    """Direkter Amazon Produkt-Link via ASIN."""
    return f"https://www.amazon.de/dp/{asin}"

def check_amazon_invite(asin: str) -> tuple:
    """
    Prüft ob Amazon-Einladung für ein Produkt verfügbar ist.
    Gibt (status, url) zurück:
    - ("invite", url)    → Einladungs-Button gefunden
    - ("available", url) → Normal kaufbar (In den Einkaufswagen)
    - ("soldout", url)   → Ausverkauft
    - ("unknown", url)   → Unbekannt
    """
    url = get_amazon_product_url(asin)
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "de-DE,de;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        html = resp.text.lower()

        # Einladungs-Erkennung
        invite_signals = [
            "zur einladung anmelden",
            "einladung anfordern",
            "request invitation",
            "nur auf einladung",
            "by invitation only",
            "join waitlist",
            "warteliste",
            "invite only",
        ]
        for signal in invite_signals:
            if signal in html:
                return ("invite", url)

        # Normal verfügbar
        available_signals = [
            "in den einkaufswagen",
            "add to cart",
            "jetzt kaufen",
            "buy now",
            "auf lager",
            "in stock",
        ]
        for signal in available_signals:
            if signal in html:
                return ("available", url)

        # Ausverkauft
        sold_out_signals = [
            "derzeit nicht verfügbar",
            "currently unavailable",
            "nicht auf lager",
            "out of stock",
            "ausverkauft",
        ]
        for signal in sold_out_signals:
            if signal in html:
                return ("soldout", url)

        return ("unknown", url)
    except Exception as e:
        print(f"⚠️ Amazon Check Fehler ({asin}): {e}")
        return ("unknown", url)

# Bekannte Pokémon TCG Produkte auf Amazon mit ASIN
# Diese werden automatisch überwacht
# Bekannte Pokémon TCG Produkte auf Amazon
# NUR verifizierte ASINs – neue werden automatisch vom Store-Scan gefunden
KNOWN_AMAZON_PRODUCTS = {}
# ASINs werden ausschließlich automatisch vom Amazon Store gescannt
# damit keine falschen Produkte gemeldet werden

def discover_amazon_products_from_store() -> dict:
    """
    Scannt den offiziellen Amazon Pokémon Store nach neuen Produkten.
    Gibt {asin: product_name} zurück.
    """
    discovered = {}
    store_pages = [
        "https://www.amazon.de/stores/Pok%C3%A9mon-Sammelkartenspiel/page/EBE7C18D-29BC-41FA-9252-C03AD4C74D4B",
        "https://www.amazon.de/s?k=pokemon+karten+elite+trainer+box&rh=n%3A301128&s=date-desc-rank",
        "https://www.amazon.de/s?k=pokemon+karten+display&rh=n%3A301128&s=date-desc-rank",
        "https://www.amazon.de/s?k=pokemon+karten+booster&rh=n%3A301128&s=date-desc-rank",
    ]
    for url in store_pages:
        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "de-DE,de;q=0.9",
            })
            html = resp.text
            # ASIN aus Links extrahieren
            asins = re.findall(r"/dp/([A-Z0-9]{10})", html)
            # Produktnamen aus Titeln
            titles = re.findall(r"Pokémon[^<]{5,80}(?:ETB|Display|Booster|Collection|Tin|Bundle|UPC)", html, re.IGNORECASE)
            for i, asin in enumerate(set(asins)):
                if asin not in KNOWN_AMAZON_PRODUCTS and asin not in discovered:
                    name = titles[i].strip() if i < len(titles) else f"Pokémon TCG Produkt ({asin})"
                    discovered[asin] = name[:60]
        except Exception as e:
            print(f"⚠️ Amazon Store Scan Fehler: {e}")
    return discovered


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
    "Pokémon Center DE":    "https://www.pokemoncenter.com/de-de/search?q={query}",
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
    "Ludkins (UK)", "Amazon DE", "Smyths", "GameStop DE", "MediaMarkt", "Pokémon Center DE",
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

def search_pokemon_card(card_name: str, set_name: str = None, language: str = "en") -> list:
    """Sucht Karten über PokémonTCG API."""
    try:
        url   = "https://api.pokemontcg.io/v2/cards"
        query = f'name:"{card_name}"'
        if set_name:
            if "151" in set_name.lower():
                query += " set.id:sv3pt5"
            elif set_name.lower() in ALL_SETS:
                query += f" set.id:{ALL_SETS[set_name.lower()]}"
            else:
                query += f' set.name:"{set_name}"'
        resp = requests.get(url, params={"q": query, "pageSize": 50}, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("data", [])
            if results:
                return results
    except Exception as e:
        print(f"⚠️ PokémonTCG API Fehler: {e}")

    # Fallback ohne Anführungszeichen
    try:
        url   = "https://api.pokemontcg.io/v2/cards"
        query = f"name:{card_name}"
        if set_name:
            query += f' set.name:"{set_name}"'
        resp = requests.get(url, params={"q": query, "pageSize": 50}, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", [])
    except Exception as e:
        print(f"⚠️ PokémonTCG Fallback Fehler: {e}")

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

    keyboard_start = [[InlineKeyboardButton("📖 Alle Funktionen anzeigen", callback_data="show_help")]]

    await update.message.reply_text(
        f"🃏 <b>AnzarDexBot</b>\n\n"
        f"Dein persönlicher Pokémon TCG Tracker 🔥\n\n"
        f"Status: {sub_text}\n\n"
        f"<b>Schnellstart:</b>\n"
        f"• Karte suchen: <code>charizard 151</code>\n"
        f"• Produkt suchen: <code>151 etb</code>\n"
        f"• JP-Karte: <code>charizard 151 jp</code>\n"
        f"• Alle Befehle: /hilfe\n\n"
        f"<i>Restock-Alerts, Preisziele, Portfolio & mehr mit Premium 🔒</i>",
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
            [InlineKeyboardButton("💳 Jetzt abonnieren – 4,99 €/Monat", callback_data="buy_sub")],
        ]
        await update.message.reply_text(
            "🔓 <b>AnzarDexBot Premium – 4,99 €/Monat</b>\n\n"
            "Jederzeit kündbar · Automatische Verlängerung\n\n"
            "🚨 <b>Restock-Alerts</b> – sofort benachrichtigt\n"
            "wenn dein Produkt wieder verfügbar ist\n\n"
            "🎯 <b>Preisziel-Alarm</b> – wir melden uns\n"
            "wenn dein Wunschpreis erreicht wird\n\n"
            "🔥 <b>Deal-Alert</b> – Meldung wenn eine Karte\n"
            "deutlich günstiger als der Marktpreis ist\n\n"
            "🆕 <b>Neue Set-Alerts</b> – als erster informiert\n"
            "wenn ein neues Pokémon TCG Set erscheint\n\n"
            "📊 <b>Portfolio-Tracker</b> – verfolge den Wert\n"
            "deiner gesamten Kartensammlung\n\n"
            "🌍 40+ Shops überwacht – DE, UK, JP\n\n"
            "<b>Zahlung:</b> Kreditkarte · Apple Pay · Google Pay · Klarna",
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
            "💳 Jetzt abonnieren – 4,99 €/Monat",
            url=full_link
        )]]
        await query.message.reply_text(
            "💳 <b>AnzarDexBot Premium – 4,99 €/Monat</b>\n\n"
            "Das bekommst du mit Premium:\n\n"
            "🚨 <b>Restock-Alerts</b>\n"
            "Sofort benachrichtigt wenn ein Produkt wieder verfügbar ist – mit direktem Shop-Link.\n\n"
            "🎯 <b>Preisziel-Alarm</b>\n"
            "Wir melden uns automatisch wenn dein Wunschpreis erreicht wird.\n\n"
            "🔥 <b>Deal-Alert</b>\n"
            "Automatische Meldung wenn eine Karte deutlich günstiger als der Marktpreis ist.\n\n"
            "🆕 <b>Neue Set-Alerts</b>\n"
            "Als erster informiert wenn ein neues Pokémon TCG Set erscheint.\n\n"
            "📊 <b>Portfolio-Tracker</b>\n"
            "Verfolge jederzeit den aktuellen Gesamtwert deiner Sammlung.\n\n"
            "🌍 <b>40+ Shops</b> – DE, UK & JP überwacht\n"
            "💰 Günstigster Cardmarket-Preis immer dabei\n\n"
            "─────────────────────\n"
            "✅ Kreditkarte · Apple Pay · Google Pay · Klarna\n"
            "✅ Automatische Freischaltung nach Zahlung\n"
            "✅ Jederzeit kündbar – kein Risiko\n\n"
            "👇 Tippe auf den Button und starte jetzt:",
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
        prices=[LabeledPrice("AnzarDexBot Premium – 1 Monat", MONTHLY_PRICE)],
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
        "🎉 <b>Zahlung erfolgreich! Willkommen bei AnzarDexBot Premium!</b>\n\n"
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
    # TCGDex oder PokémonTCG API Karte
    is_tcgdex = card.get("source") == "tcgdex"

    if is_tcgdex:
        # TCGDex Karte
        low = trend = avg = None
        img_base = card.get("image", "")
        image = f"{img_base}/high.png" if img_base and not img_base.endswith(".png") else img_base
        cm_url = get_cardmarket_card_url(name, set_nm, number if str(number).isdigit() else None)
    else:
        cm_data = card.get("cardmarket", {})
        prices  = cm_data.get("prices", {})
        trend = prices.get("trendPrice")
        low   = prices.get("lowPrice")
        avg   = prices.get("averageSellPrice")
        cm_url = cm_data.get("url", "")
        if cm_url:
            if not cm_url.startswith("http"):
                cm_url = "https://www.cardmarket.com" + cm_url
            if " " in cm_url or not cm_url.startswith("https://"):
                cm_url = ""
        if not cm_url:
            q = f"{name} {set_nm}".replace(" ", "%20")
            cm_url = f"https://www.cardmarket.com/de/Pokemon/Products/Singles/Search?searchString={q}"

    preis_zeilen = []
    if low:
        preis_zeilen.append(f"💰 <b>Günstigster Preis:</b> {low} €")
    if trend:
        preis_zeilen.append(f"📉 <b>Trend:</b> {trend} €")
    if avg:
        preis_zeilen.append(f"📊 <b>Ø Verkauf:</b> {avg} €")
    if not preis_zeilen:
        preis_zeilen.append("💰 Preis → siehe Cardmarket")

    text = (
        f"🃏 <b>{name}</b>\n"
        f"📦 {set_nm} · #{number} · {rarity}\n\n"
        + "\n".join(preis_zeilen)
    )

    # Karten-Key – eindeutig pro Karte (Name|Set|Nummer)
    safe_name = f"{name}|{set_nm}|{number}"[:55]

    keyboard = [
        # Zeile 1: Cardmarket
        [InlineKeyboardButton("🛒 Direkt auf Cardmarket", url=cm_url)],
        # Zeile 2: Preisziel + Deal-Alert
        [
            InlineKeyboardButton("🎯 Preisziel", callback_data=f"pz_{safe_name}"),
            InlineKeyboardButton("🔥 Deal-Alert", callback_data=f"da_{safe_name}"),
        ],
        # Zeile 3: Portfolio hinzufügen
        [InlineKeyboardButton("📊 Portfolio +", callback_data=f"pa_{safe_name}")],
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
    query_lower = query.lower().strip()

    # JP-Suche deaktiviert – immer normale EN/DE Suche
    is_jp = False
    # "jp" aus dem Query entfernen damit die Suche nicht gestört wird
    query_lower = query_lower.replace(" jp", "").strip()
    if is_jp:
        # JP-Set → EN Set-ID mappen, dann normal über PokémonTCG API suchen
        # (Die API hat EN-Karten der JP-Sets unter derselben set.id)
        jp_query = query_lower
        # DE Namen → EN
        for de_name, en_name in DE_TO_EN_POKEMON.items():
            if jp_query.startswith(de_name) or f" {de_name} " in f" {jp_query} ":
                jp_query = jp_query.replace(de_name, en_name, 1)
                break
        # JP-Set erkennen – "151 jp" → sv2a, "shiny treasure" → sv4a usw.
        matched_set_id = None

        # Set erkennen: erst DE-Set-Namen prüfen, dann JP-Set-Namen, dann "151"
        if "151" in jp_query and not any(s in jp_query for s in ["shiny", "treasure", "climax"]):
            matched_set_id = "sv2a"
            jp_query = jp_query.replace("151", "").strip()
        else:
            # Zuerst deutsche Set-Namen probieren
            matched_set_id = None
            for de_set, set_id in DE_SET_TO_JP.items():
                if de_set in jp_query:
                    matched_set_id = set_id
                    jp_query = jp_query.replace(de_set, "").strip()
                    break
            # Dann JP Set-Namen
            if not matched_set_id:
                for jp_name, set_id in JP_SET_IDS.items():
                    if jp_name in jp_query:
                        matched_set_id = set_id
                        jp_query = jp_query.replace(jp_name, "").strip()
                        break

        # " jp" suffix entfernen
        jp_query = jp_query.replace(" jp", "").strip()
        if not jp_query:
            await message.reply_text("❌ Bitte einen Kartennamen angeben.")
            return

        # Suche via TCGDex (unterstützt echte JP-Karten)
        cards = []
        # TCGDex Set-Namen ermitteln
        tcgdex_set = None
        if matched_set_id:
            # matched_set_id ist API-ID (sv2a etc.) → TCGDex Set-Name aus TCGDEX_SET_ALIASES
            for alias, canonical in TCGDEX_SET_ALIASES.items():
                if alias in jp_query or alias == "151" and "151" in jp_query:
                    tcgdex_set = canonical
                    break
            if not tcgdex_set:
                # DE-Set-Name direkt als TCGDex-Set nutzen
                for de_set, jp_set in DE_SET_TO_JP.items():
                    if de_set in query_lower:
                        tcgdex_set = TCGDEX_SET_ALIASES.get(de_set, de_set)
                        break

        # TCGDex JP-Suche
        cards = tcgdex_find_cards(jp_query, tcgdex_set, "ja")

        # Fallback: TCGDex EN
        if not cards:
            cards = tcgdex_find_cards(jp_query, tcgdex_set, "en")

        # Fallback: PokémonTCG API
        if not cards:
            try:
                url   = "https://api.pokemontcg.io/v2/cards"
                full_q = f'name:"{jp_query}"' + (f" set.id:{matched_set_id}" if matched_set_id else "")
                resp  = requests.get(url, params={"q": full_q, "pageSize": 30}, timeout=15)
                cards = resp.json().get("data", [])
            except Exception as e:
                print(f"⚠️ Fallback API Fehler: {e}")

        # Nur echte Dicts behalten
        cards = [c for c in cards if isinstance(c, dict)]

        if not cards:
            # JP nicht gefunden → still als normale EN-Suche weitermachen
            query_lower = jp_query
            is_jp = False
            # Kein return – fällt durch zu normaler Suche unten

        if is_jp and cards:
            user_id = str(message.from_user.id) if hasattr(message, "from_user") else "0"
            last_search_results[user_id] = cards[:10]
            try:
                now = datetime.now().isoformat()
                cursor.execute("DELETE FROM card_search_cache WHERE user_id=?", (user_id,))
                for pos, card in enumerate(cards[:10], 1):
                    cursor.execute(
                        "INSERT INTO card_search_cache (user_id, position, card_json, created_at) VALUES (?,?,?,?)",
                        (user_id, pos, json.dumps(card, ensure_ascii=False), now)
                    )
                conn.commit()
            except Exception:
                pass
            if len(cards) == 1:
                await send_card_details(message, cards[0])
                return
            keyboard = []
            for idx, card in enumerate(cards[:10], 1):
                prices = card.get("cardmarket", {}).get("prices", {}) if isinstance(card.get("cardmarket"), dict) else {}
                trend  = prices.get("trendPrice", "–")
                set_obj = card.get("set", {})
                set_nm = set_obj.get("name", "?") if isinstance(set_obj, dict) else str(set_obj)
                num    = card.get("number", card.get("localId", "?"))
                keyboard.append([InlineKeyboardButton(
                    f"{idx}. {card.get('name','?')} | {set_nm} | #{num}" + (f" | {trend}€" if trend != "–" else ""),
                    callback_data=f"sel_{user_id}_{idx}"
                )])
            await message.reply_text(
                f"🇯🇵 JP-Ergebnisse für: <b>{jp_query}</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        # JP nicht gefunden – normale Suche unten läuft weiter

    # DE Pokémon-Namen → EN übersetzen (z.B. "Glurak" → "Charizard")
    for de_name, en_name in DE_TO_EN_POKEMON.items():
        if query_lower.startswith(de_name) or f" {de_name} " in f" {query_lower} ":
            query_lower = query_lower.replace(de_name, en_name, 1)
            break

    # DE Set-Namen → EN alias
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

    # Scoring – richtiges Set bevorzugen, JP-Karten raus
    # Nur echte Dict-Karten verarbeiten
    cards = [c for c in cards if isinstance(c, dict)]
    scored = []
    for card in cards:
        set_obj  = card.get("set", {})
        set_name = set_obj.get("name", "").lower() if isinstance(set_obj, dict) else ""
        set_id   = set_obj.get("id", "").lower() if isinstance(set_obj, dict) else ""
        c_name   = card.get("name", "").lower()
        number   = card.get("number", "")
        score    = 0

        # Kartenname stimmt überein
        if c_name == card_name.lower():
            score += 20
        elif card_name.lower() in c_name:
            score += 10

        # Richtiges Set – starker Bonus
        if matched_set and matched_set.lower() in set_name:
            score += 50

        # JP-Karten komplett ausschließen (set_id endet auf pt/kor/jp etc.)
        lang = card.get("language", "").lower()
        is_jp = (
            lang == "ja" or
            any(set_id.startswith(p) for p in ["sm", "xy", "dp", "ex", "neo", "base"]) is False and
            any(j in set_id for j in ["-jp", "jp-", "kor", "pt-"])
        )
        if is_jp:
            continue  # JP-Karte komplett überspringen

        # Numerische Nummer = EN-Karte
        if number.isdigit():
            score += 3

        # Promo-Karten weniger bevorzugen wenn Set gefunden
        if matched_set and "promo" in set_name and matched_set not in set_name:
            score -= 20

        scored.append((score, card))

    scored.sort(reverse=True, key=lambda x: x[0])
    # Alle mit positivem Score, max 8
    cards = [c for _, c in scored[:10] if _ >= 0]
    if not cards:
        cards = [c for _, c in scored[:10]]

    user_id = str(message.from_user.id) if hasattr(message, "from_user") else "0"
    # RAM + DB Cache speichern – DB überlebt Bot-Neustarts
    last_search_results[user_id] = cards
    try:
        now = datetime.now().isoformat()
        cursor.execute("DELETE FROM card_search_cache WHERE user_id=?", (user_id,))
        for pos, card in enumerate(cards, 1):
            cursor.execute(
                "INSERT INTO card_search_cache (user_id, position, card_json, created_at) VALUES (?,?,?,?)",
                (user_id, pos, json.dumps(card, ensure_ascii=False), now)
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️ Cache-Fehler: {e}")

    if not cards:
        await message.reply_text("❌ Keine Karte gefunden.")
        return
    if len(cards) == 1:
        await send_card_details(message, cards[0])
        return

    keyboard = []
    for idx, card in enumerate(cards, 1):
        if not isinstance(card, dict):
            continue
        cm   = card.get("cardmarket", {})
        prices = cm.get("prices", {}) if isinstance(cm, dict) else {}
        trend  = prices.get("trendPrice", "–") if isinstance(prices, dict) else "–"
        set_obj = card.get("set", {})
        set_nm  = set_obj.get("name", "?") if isinstance(set_obj, dict) else str(set_obj)
        num     = card.get("number", card.get("localId", "?"))
        callback = f"sel_{user_id}_{idx}"
        label = f"{idx}. {card.get('name','?')} | {set_nm} | #{num}"
        if trend != "–":
            label += f" | {trend}€"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

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

    # Produkttyp erkennen
    product_type = "Produkt"
    for kw, pname in PRODUCT_TYPES.items():
        if kw in query_lower:
            product_type = pname
            break

    # Case-Erkennung direkt im Query – unabhängig vom Produkttyp-Loop
    is_case = "case" in query_lower or "karton" in query_lower

    search_query = normalize_product_query(query)

    # Cardmarket URL – direkt mit dem was der User geschrieben hat
    cm_url_main = get_cardmarket_de_url(query)

    # Bei "case": zweiten Link mit "karton" generieren und umgekehrt
    cm_url_karton = None
    if is_case:
        query_karton  = re.sub(r"\bcase\b", "karton", query_lower)
        if query_karton == query_lower:  # "karton" war schon drin
            query_karton = query_lower.replace("karton", "case")
        cm_url_karton = get_cardmarket_de_url(query_karton)

    text = (
        f"📦 <b>Produkt gefunden</b>\n\n"
        f"🔍 <b>Gesucht:</b> {query}\n"
        f"🏷 <b>Typ:</b> {product_type}\n\n"
        f"🛒 Cardmarket zeigt alle Varianten sortiert nach Preis.\n"
        f"🔔 Restock-Alert aktivieren – sofort benachrichtigt wenn wieder verfügbar,\n"
        f"<b>inklusive direktem Shop-Link.</b>"
    )

    keyboard = [
        [InlineKeyboardButton("🛒 Cardmarket – Alle Varianten", url=cm_url_main)],
    ]
    if cm_url_karton:
        keyboard.append([InlineKeyboardButton("🛒 Cardmarket – als Karton", url=cm_url_karton)])
    keyboard.append([InlineKeyboardButton("🔔 Restock-Alert aktivieren", callback_data=f"trackproduct_{search_query}")])

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

    # Warte auf Preis-Eingabe für Preisziel
    if context.user_data.get("awaiting_preisziel"):
        try:
            target    = float(text.replace(",", ".").replace("€", "").strip())
            card_info = context.user_data.pop("awaiting_preisziel")
            card_name = card_info["card_name"]
            set_name  = card_info["set_name"]
            user_id   = str(update.effective_user.id)
            db_key    = f"{card_name}|{set_name}" if set_name else card_name
            cursor.execute(
                "INSERT INTO price_targets (user_id, card_name, target_price, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id, card_name) DO UPDATE SET target_price=excluded.target_price",
                (user_id, db_key, target, datetime.now().isoformat())
            )
            conn.commit()
            await update.message.reply_text(
                f"\U0001f3af <b>Preisziel gesetzt!</b>\n\n"

                + (f"\U0001f0cf <b>{card_name}</b> \u00b7 {set_name}" if set_name else f"\U0001f0cf <b>{card_name}</b>") + "\n"

                + f"\U0001f4b0 Alert wenn Preis unter <b>{target} \u20ac</b> f\u00e4llt\n\n"

                + "\u2705 Ich pr\u00fcfe alle 5 Minuten!",
            )
            return
        except ValueError:
            context.user_data.pop("awaiting_preisziel", None)
            # Kein gültiger Preis – normal weitersuchen

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

    # Immer erst aus DB laden – überlebt Bot-Neustarts zuverlässig
    cursor.execute(
        "SELECT card_json FROM card_search_cache WHERE user_id=? ORDER BY position ASC",
        (user_id,)
    )
    rows  = cursor.fetchall()
    cards = [json.loads(r[0]) for r in rows] if rows else []

    # RAM als Backup
    if not cards:
        cards = last_search_results.get(user_id, [])
        if not cards:
            cards = last_search_results.get(cache_id, [])

    if not cards:
        await query.message.reply_text(
            "❌ Bitte such die Karte nochmal — tippe z.B. *umbreon evolving skies*",
            parse_mode="Markdown"
        )
        return

    if choice < 1 or choice > len(cards):
        await query.message.reply_text("❌ Ungültige Auswahl.")
        return

    await send_card_details(query.message, cards[choice - 1])

async def deal_alert_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User hat auf 🔥 Deal-Alert gedrückt."""
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not is_subscribed(user_id):
        keyboard = [[InlineKeyboardButton("🔓 Premium holen", callback_data="buy_sub")]]
        await query.message.reply_text(
            "🔒 Deal-Alerts sind nur für Premium-Abonnenten.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    card_key  = query.data.replace("da_", "")
    parts     = card_key.split("|")
    card_name = parts[0] if parts else card_key
    set_name  = parts[1] if len(parts) > 1 else ""

    # Mit 15% Standard-Schwelle speichern
    db_key = f"{card_name}|{set_name}" if set_name else card_name
    cursor.execute(
        "INSERT INTO deal_alerts (user_id, card_name, threshold_pct) VALUES (?,?,15) "
        "ON CONFLICT(user_id, card_name) DO UPDATE SET threshold_pct=15",
        (user_id, db_key)
    )
    conn.commit()

    await query.message.reply_text(
        f"🔥 <b>Deal-Alert aktiviert!</b>\n\n"
        f"🃏 <b>{card_name}</b>" + (f" · {set_name}" if set_name else "") + f"\n"
        f"📉 Alert wenn Preis <b>15% unter Trend-Preis</b> fällt\n\n"
        f"<i>Prüft alle 5 Minuten.</i>",
        parse_mode="HTML"
    )

async def portfolio_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User hat auf 📊 Portfolio + gedrückt."""
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not is_subscribed(user_id):
        keyboard = [[InlineKeyboardButton("🔓 Premium holen", callback_data="buy_sub")]]
        await query.message.reply_text(
            "🔒 Portfolio ist nur für Premium-Abonnenten.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    card_key  = query.data.replace("pa_", "")
    parts     = card_key.split("|")
    card_name = parts[0] if parts else card_key
    set_name  = parts[1] if len(parts) > 1 else ""
    number    = parts[2] if len(parts) > 2 else ""

    # Aktuellen Preis aus Cache holen
    cursor.execute(
        "SELECT card_json FROM card_search_cache WHERE user_id=? ORDER BY position ASC LIMIT 8",
        (user_id,)
    )
    rows = cursor.fetchall()
    # Preis aus Cache holen
    current_price = None
    cursor.execute(
        "SELECT card_json FROM card_search_cache WHERE user_id=? ORDER BY position ASC LIMIT 10",
        (user_id,)
    )
    for row in cursor.fetchall():
        try:
            c = json.loads(row[0])
            if c.get("name","").lower() == card_name.lower():
                prices = c.get("cardmarket",{}).get("prices",{}) or {}
                current_price = prices.get("lowPrice") or prices.get("trendPrice")
                break
        except Exception:
            pass

    price_hint = f" · {current_price} €" if current_price else ""

    now = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO portfolio (user_id, card_name, set_name, quantity, buy_price, added_at) "
        "VALUES (?,?,?,1,?,?) ON CONFLICT(user_id, card_name, set_name) DO UPDATE SET "
        "quantity=quantity+1, buy_price=excluded.buy_price",
        (user_id, card_name, set_name, current_price or 0.0, now)
    )
    conn.commit()

    await query.message.reply_text(
        f"📊 <b>Portfolio aktualisiert!</b>\n\n"
        f"🃏 <b>{card_name}</b>" + (f" · {set_name}" if set_name else "") + f"{price_hint}\n"
        f"➕ 1x hinzugefügt · Kaufpreis: {current_price or 0} €\n\n"
        f"/portfolio – Gesamtwert anzeigen",
        parse_mode="HTML"
    )

async def preisziel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User hat auf 🎯 Preisziel setzen gedrückt – fragt nach Wunschpreis."""
    query   = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)

    if not is_subscribed(user_id):
        keyboard = [[InlineKeyboardButton("🔓 Premium holen", callback_data="buy_sub")]]
        await query.message.reply_text(
            "🔒 Preisziele sind nur für Premium-Abonnenten.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    card_key  = query.data.replace("pz_", "")
    parts     = card_key.split("|")
    card_name = parts[0].strip() if parts else card_key
    set_name  = parts[1].strip() if len(parts) > 1 else ""
    number    = parts[2].strip() if len(parts) > 2 else ""

    # Aktuellen Preis aus Cache
    current_price = None
    cursor.execute(
        "SELECT card_json FROM card_search_cache WHERE user_id=? ORDER BY position ASC LIMIT 10",
        (user_id,)
    )
    for row in cursor.fetchall():
        try:
            c = json.loads(row[0])
            if c.get("name","").lower() == card_name.lower():
                prices = c.get("cardmarket",{}).get("prices",{}) or {}
                current_price = prices.get("lowPrice") or prices.get("trendPrice")
                break
        except Exception:
            pass

    price_hint = f"\n\U0001f4b0 Aktueller Preis: <b>{current_price} \u20ac</b>" if current_price else ""

    # Karte in user_data speichern – warten auf direkte Preis-Eingabe
    context.user_data["awaiting_preisziel"] = {
        "card_name": card_name,
        "set_name":  set_name,
        "number":    number,
    }

    await query.message.reply_text(
        f"\U0001f3af <b>Preisziel setzen</b>\n\n"
        f"\U0001f0cf <b>{card_name}</b> \u00b7 {set_name} \u00b7 #{number}{price_hint}\n\n"
        f"Schreib einfach deinen <b>Wunschpreis in \u20ac</b>:\n"
        f"<i>(z.B. 50 oder 49.99)</i>",
        parse_mode="HTML"
    )

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
        params={"q": f'set.name:"{set_name}"', "pageSize": 30},
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

# ─────────────────────────────────────────
# JP-SUCHE
# ─────────────────────────────────────────

# Deutsche & englische Pokémon-Namen → EN für API
DE_TO_EN_POKEMON = {
    # ── Gen 1 Kanto ──────────────────────────────────────
    "bisasam":"bulbasaur","bisaknosp":"ivysaur","bisaflor":"venusaur",
    "glumanda":"charmander","glutexo":"charmeleon","glurak":"charizard",
    "schiggy":"squirtle","shiggy":"squirtle","schillok":"wartortle","turtok":"blastoise",
    "raupi":"caterpie","safcon":"metapod","smettbo":"butterfree",
    "hornliu":"weedle","kokuna":"kakuna","bibor":"beedrill",
    "taubsi":"pidgey","tauboga":"pidgeotto","tauboss":"pidgeot",
    "rattfratz":"rattata","rattikarl":"raticate",
    "habitak":"spearow","ibitak":"fearow",
    "rettan":"ekans","arbok":"arbok",
    "pikachu":"pikachu","raichu":"raichu","pichu":"pichu",
    "sandan":"sandshrew","sandamer":"sandslash",
    "piepi":"clefairy","pixi":"clefable","pii":"cleffa",
    "pummeluff":"jigglypuff","knuddeluff":"wigglytuff","knirsp":"igglybuff",
    "zubat":"zubat","golbat":"golbat","iksbat":"crobat",
    "myrapla":"oddish","duflor":"gloom","giflor":"vileplume","blubella":"bellossom",
    "paras":"paras","parasek":"parasect",
    "bluzuk":"venonat","omot":"venomoth",
    "digda":"diglett","digdri":"dugtrio",
    "mauzi":"meowth","snobilikat":"persian",
    "enton":"psyduck","entoron":"golduck",
    "menki":"mankey","rasaff":"primeape",
    "fukano":"growlithe","arkani":"arcanine",
    "quapsel":"poliwag","quaputzi":"poliwhirl","quappo":"poliwrath","politoed":"politoed",
    "abra":"abra","kadabra":"kadabra","simsala":"alakazam",
    "machollo":"machop","maschock":"machoke","machomei":"machamp",
    "knofensa":"bellsprout","ultrigaria":"weepinbell","sarzenia":"victreebel",
    "tentacha":"tentacool","tentoxa":"tentacruel",
    "kleinstein":"geodude","georok":"graveler","geowaz":"golem",
    "ponyta":"ponyta","gallopa":"rapidash",
    "flegmon":"slowpoke","kamslower":"slowbro","laschoking":"slowking",
    "magnetilo":"magnemite","magneton":"magneton","magnezone":"magnezone",
    "dodu":"doduo","dodri":"dodrio",
    "jurob":"seel","jugong":"dewgong",
    "sleima":"grimer","sleimok":"muk",
    "muschas":"shellder","austos":"cloyster",
    "nebulak":"gastly","alpollo":"haunter","gengar":"gengar",
    "onix":"onix","steelix":"steelix",
    "schläfer":"drowzee","hypno":"hypno",
    "krabby":"krabby","kingler":"kingler",
    "voltobal":"voltorb","lektrobal":"electrode",
    "owei":"exeggcute","kokowei":"exeggutor",
    "tragosso":"cubone","knogga":"marowak",
    "kicklee":"hitmonlee","nockchan":"hitmonchan","kapoera":"hitmontop",
    "lippenbaby":"lickitung","schlurp":"lickilicky",
    "smogon":"koffing","smogmog":"weezing",
    "rihorn":"rhyhorn","rizeros":"rhydon","rihornior":"rhyperior",
    "tangela":"tangela","tangoloss":"tangrowth",
    "kangama":"kangaskhan",
    "seeper":"horsea","seemon":"seadra","kingdra":"kingdra",
    "goldini":"goldeen","golking":"seaking",
    "sterndu":"staryu","starmie":"starmie",
    "pantimos":"mr-mime",
    "sichlor":"scyther","scherox":"scizor",
    "jynx":"jynx",
    "elektek":"electabuzz","elevoltek":"electivire","elekid":"elekid",
    "magmar":"magmar","magbrant":"magmortar","magby":"magby",
    "pinsir":"pinsir","tauros":"tauros",
    "karpador":"magikarp","garados":"gyarados",
    "lapras":"lapras","ditto":"ditto",
    "evoli":"eevee","flamara":"flareon","aquana":"vaporeon",
    "blitza":"jolteon","psiana":"espeon","nachtara":"umbreon",
    "kryppuk":"glaceon","folipurba":"leafeon","feelinara":"sylveon",
    "porygon":"porygon","porygon2":"porygon2","porygonz":"porygon-z",
    "amonitas":"omanyte","amoroso":"omastar",
    "kabuto":"kabuto","kabutops":"kabutops",
    "aerodactyl":"aerodactyl","relaxo":"snorlax",
    "arktos":"articuno","zapdos":"zapdos","lavados":"moltres",
    "dratini":"dratini","dragonir":"dragonair","dragoran":"dragonite",
    "mewtu":"mewtwo","zerozone":"mew",
    # ── Gen 2 Johto ──────────────────────────────────────
    "endivie":"chikorita","lorblatt":"bayleef","meganie":"meganium",
    "feurigel":"cyndaquil","igelavar":"quilava","typhlosion":"typhlosion",
    "karnimani":"totodile","tyracroc":"croconaw","impergator":"feraligatr",
    "hoothoot":"hoothoot","noctuh":"noctowl",
    "ledyba":"ledyba","ledian":"ledian",
    "webarak":"spinarak","ariados":"ariados",
    "lampi":"chinchou","lanturn":"lanturn",
    "togepi":"togepi","togetic":"togetic","togekiss":"togekiss",
    "natu":"natu","xatu":"xatu",
    "voltilamm":"mareep","waaty":"flaaffy","ampharos":"ampharos",
    "mogelbaum":"sudowoodo",
    "snubbull":"snubbull","granbull":"granbull",
    "qwilfish":"qwilfish","heracross":"heracross",
    "schneppke":"sneasel","weavile":"weavile",
    "teddiursa":"teddiursa","ursaring":"ursaring",
    "schneckmag":"slugma","magcargo":"magcargo",
    "quiekel":"swinub","mamutel":"piloswine",
    "corasonn":"corsola","remoraid":"remoraid","octillery":"octillery",
    "delibird":"delibird","mantirps":"mantine",
    "panzaeron":"skarmory",
    "hunduster":"houndour","hundemon":"houndoom",
    "phanpy":"phanpy","donphan":"donphan",
    "stantler":"stantler","miltank":"miltank",
    "chansey":"chansey","blissey":"blissey","happiny":"happiny",
    "raikou":"raikou","entei":"entei","suicune":"suicune",
    "larvitar":"larvitar","pupitar":"pupitar","despotar":"tyranitar",
    "lugia":"lugia","ho-oh":"ho-oh","celebi":"celebi",
    # ── Gen 3 Hoenn ──────────────────────────────────────
    "geckarbor":"treecko","reptain":"grovyle","gewaldro":"sceptile",
    "flemmli":"torchic","jungglut":"combusken","lohgock":"blaziken",
    "hydropi":"mudkip","moorabbel":"marshtomp","sumpex":"swampert",
    "zigzachs":"zigzagoon","geradaks":"linoone",
    "roselia":"roselia","roseremy":"budew","roserade":"roserade",
    "makuhita":"makuhita","hariyama":"hariyama",
    "azurill":"azurill","marill":"marill","azumarill":"azumarill",
    "aron":"aron","lairon":"lairon","aggron":"aggron",
    "meditite":"meditite","medicham":"medicham",
    "wailmer":"wailmer","wailord":"wailord",
    "numel":"numel","camerupt":"camerupt","torkoal":"torkoal",
    "spoink":"spoink","groink":"grumpig",
    "hopplo":"trapinch","vibrava":"vibrava","libelldra":"flygon",
    "cacnea":"cacnea","cacturne":"cacturne",
    "solrock":"solrock","lunatone":"lunatone",
    "barschwa":"barboach","welsar":"whiscash",
    "liliep":"lileep","corasonn":"cradily",
    "anorith":"anorith","armaldo":"armaldo",
    "milotic":"milotic","castform":"castform","kecleon":"kecleon",
    "shuppet":"shuppet","banette":"banette",
    "zwirrlicht":"duskull","zwirrklop":"dusclops","dusknoir":"dusknoir",
    "tropius":"tropius","palimpalim":"chimecho","klingplim":"chingling",
    "absol":"absol",
    "snorunt":"snorunt","glalie":"glalie","froslass":"froslass",
    "spheal":"spheal","sealeo":"sealeo","walrein":"walrein",
    "relicanth":"relicanth","luvdisc":"luvdisc",
    "kindwurm":"bagon","stefelz":"shelgon","brutalanda":"salamence",
    "beldum":"beldum","metang":"metang","metagross":"metagross",
    "regirock":"regirock","regice":"regice","registeel":"registeel",
    "latias":"latias","latios":"latios",
    "kyogre":"kyogre","groudon":"groudon","rayquaza":"rayquaza",
    "jirachi":"jirachi","deoxys":"deoxys",
    # ── Gen 4 Sinnoh ─────────────────────────────────────
    "chelast":"turtwig","chelcarain":"grotle","chelterrar":"torterra",
    "panflam":"chimchar","panpyro":"monferno","panferno":"infernape",
    "plinfa":"piplup","pliprin":"prinplup","impoleon":"empoleon",
    "staralili":"starly","staravia":"staravia","staraptor":"staraptor",
    "bidiza":"bidoof","bidifas":"bibarel",
    "sheinux":"shinx","luxio":"luxio","luxtra":"luxray",
    "grantieras":"cranidos","rameidon":"rampardos",
    "schilterus":"shieldon","bastiodon":"bastiodon",
    "pachirisu":"pachirisu","ambidiffel":"ambipom","aipom":"aipom",
    "driftlon":"drifloon","drifzepeli":"drifblim",
    "haspiror":"buneary","lophauser":"lopunny",
    "hippopotas":"hippopotas","hippoterus":"hippowdon",
    "skorupi":"skorupi","drapion":"drapion",
    "pantimos":"croagunk","toxiquak":"toxicroak",
    "finneon":"finneon","lumineon":"lumineon",
    "riolu":"riolu","lucario":"lucario",
    "rotom":"rotom",
    "uxie":"uxie","mesprit":"mesprit","azelf":"azelf",
    "dialga":"dialga","palkia":"palkia","giratina":"giratina",
    "cresellia":"cresselia","darkrai":"darkrai",
    "shaymin":"shaymin","arceus":"arceus",
    "mamutel":"mamoswine",
    # ── Gen 5 Unova ──────────────────────────────────────
    "serpifeu":"snivy","serpiroyal":"serperior",
    "floink":"tepig","ferkelot":"pignite","flambirex":"emboar",
    "ottaro":"oshawott","dignitas":"dewott","admurai":"samurott",
    "zorua":"zorua","zoroark":"zoroark",
    "minccino":"minccino","cinccino":"cinccino",
    "emolga":"emolga","joltik":"joltik","galvantula":"galvantula",
    "litwick":"litwick","lampent":"lampent","chandelure":"chandelure",
    "axew":"axew","fraxure":"fraxure","haxorus":"haxorus",
    "cubchoo":"cubchoo","beartic":"beartic",
    "deino":"deino","zweilous":"zweilous","hydreigon":"hydreigon",
    "larvesta":"larvesta","volcarona":"volcarona",
    "cobalion":"cobalion","terrakion":"terrakion","virizion":"virizion",
    "reshiram":"reshiram","zekrom":"zekrom","kyurem":"kyurem",
    "keldeo":"keldeo","meloetta":"meloetta","genesect":"genesect",
    "tornadus":"tornadus","thundurus":"thundurus","landorus":"landorus",
    # ── Gen 6 Kalos ──────────────────────────────────────
    "igamaro":"chespin","igastarnish":"quilladin","brigaron":"chesnaught",
    "fynx":"fennekin","zweifusel":"braixen","fynara":"delphox",
    "froxy":"froakie","frewpie":"frogadier","quabbex":"greninja",
    "xerneas":"xerneas","yveltal":"yveltal","zygarde":"zygarde",
    "diancie":"diancie","hoopa":"hoopa","volcanion":"volcanion",
    "sylveon":"sylveon","hawlucha":"hawlucha","dedenne":"dedenne",
    "klefki":"klefki","mimikyu":"mimikyu",
    "goomy":"goomy","sliggoo":"sliggoo","goodra":"goodra",
    "noibat":"noibat","noivern":"noivern",
    # ── Gen 7 Alola ──────────────────────────────────────
    "flamiau":"litten","tignar":"torracat","fuegro":"incineroar",
    "molli":"brionne","primarina":"primarina",
    "rockruff":"rockruff","lycanroc":"lycanroc",
    "solgaleo":"solgaleo","lunala":"lunala",
    "nihilego":"nihilego","buzzwole":"buzzwole","pheromosa":"pheromosa",
    "necrozma":"necrozma","magearna":"magearna","marshadow":"marshadow",
    "zeraora":"zeraora","meltan":"meltan","melmetal":"melmetal",
    "tapu koko":"tapu-koko","tapu lele":"tapu-lele",
    "tapu bulu":"tapu-bulu","tapu fini":"tapu-fini",
    # ── Gen 8 Galar ──────────────────────────────────────
    "zacian":"zacian","zamazenta":"zamazenta","eternatus":"eternatus",
    "kubfu":"kubfu","urshifu":"urshifu","zarude":"zarude",
    "regieleki":"regieleki","regidrago":"regidrago",
    "calyrex":"calyrex","glastrier":"glastrier","spectrier":"spectrier",
    "hopplo":"scorbunny","raboot":"raboot","liberlo":"cinderace",
    "rillaboom":"rillaboom",
    "morpeko":"morpeko","cufant":"cufant","copperajah":"copperajah",
    # ── Gen 9 Paldea ─────────────────────────────────────
    "felori":"sprigatito","floragato":"floragato","meowscarada":"meowscarada",
    "krokel":"fuecoco","crocalor":"crocalor","skeledirge":"skeledirge",
    "kwaks":"quaxly","kwaxo":"quaxwell","quaquaval":"quaquaval",
    "lechonk":"lechonk","oinkologne":"oinkologne",
    "pawmi":"pawmi","pawmo":"pawmo","pawmot":"pawmot",
    "fidough":"fidough","dachsbun":"dachsbun",
    "charcadet":"charcadet","armarouge":"armarouge","ceruledge":"ceruledge",
    "bellibolt":"bellibolt","kilowattrel":"kilowattrel",
    "klawf":"klawf","tinkaton":"tinkaton","tinkatink":"tinkatink",
    "finizen":"finizen","palafin":"palafin",
    "gholdengo":"gholdengo","gimmighoul":"gimmighoul",
    "koraidon":"koraidon","miraidon":"miraidon",
    "wo-chien":"wo-chien","chien-pao":"chien-pao",
    "ting-lu":"ting-lu","chi-yu":"chi-yu",
    "terapagos":"terapagos","pecharunt":"pecharunt",
    "ogerpon":"ogerpon","archaludon":"archaludon","hydrapple":"hydrapple",
    "great tusk":"great-tusk","scream tail":"scream-tail",
    "brute bonnet":"brute-bonnet","flutter mane":"flutter-mane",
    "iron hands":"iron-hands","iron treads":"iron-treads",
    "iron bundle":"iron-bundle","iron valiant":"iron-valiant",
    "roaring moon":"roaring-moon","gouging fire":"gouging-fire",
    "raging bolt":"raging-bolt","iron crown":"iron-crown",
    "iron boulder":"iron-boulder",
}

# JP Set-IDs für die API
# JP Set-IDs – EN JP-Name → Pokémon TCG API Set-ID
JP_SET_IDS = {
    # ── Scarlet & Violet JP ────────────────────────────────
    "151 jp":                    "sv2a",   # Pokémon Card 151
    "pokemon card 151":          "sv2a",
    "triplet beat":              "sv1a",   # = Scarlet & Violet 151 Vorstufe
    "clay burst":                "sv2D",   # = Paldea Evolved Teil
    "snow hazard":               "sv2P",
    "ruler of the black flame":  "sv3a",   # = Obsidian Flames JP
    "ancient roar":              "sv4M",   # = Paradox Rift JP Teil
    "future flash":              "sv4K",
    "wild force":                "sv5M",   # = Temporal Forces JP
    "cyber judge":               "sv5R",
    "crimson haze":              "sv5a",   # = Twilight Masquerade JP
    "mask of change":            "sv6",    # = Shrouded Fable JP
    "night wanderer":            "sv6pt5",
    "terastal festival":         "sv6a",   # = Prismatische Entwicklungen JP
    "terastal festival ex":      "sv6a",
    "stellar miracle":           "sv7",    # = Stellar Crown JP
    "paradise dragona":          "sv7R",   # = Surging Sparks JP
    "super electric breaker":    "sv8",    # = Journey Together JP
    "battle partners":           "sv9",    # = Destined Rivals JP
    "shiny treasure":            "sv4a",   # = Paldean Fates JP
    "shiny treasure ex":         "sv4a",
    # ── Sword & Shield JP ──────────────────────────────────
    "vstar universe":            "swsh12pt5", # = Crown Zenith JP
    "incandescent arcana":       "swsh11",    # = Silver Tempest JP
    "lost abyss":                "swsh10a",   # = Lost Origin JP
    "dark phantasma":            "swsh11a",
    "vmax climax":               "swsh8a",    # = Brilliant Stars JP
    "blue sky stream":           "swsh7a",    # = Evolving Skies JP
    "eevee heroes":              "swsh6a",    # = Chilling Reign JP
    "peerless fighters":         "swsh5a",    # = Battle Styles JP
    "single strike master":      "swsh5S",
    "rapid strike master":       "swsh5R",
    "shiny star v":              "swsh4a",    # = Vivid Voltage JP
    "amazing volt tackle":       "swsh4",
    "legendary heartbeat":       "swsh3a",    # = Darkness Ablaze JP
    "infinity zone":             "swsh3",
    "rebel clash jp":            "swsh2",
    # ── Kurzformen & Alternativen ──────────────────────────
    "terastal":                  "sv6a",
    "prismatische entwicklungen":"sv6a",
    "prismatische":              "sv6a",
    "shiny":                     "sv4a",
    "vmax":                      "swsh8a",
    "climax":                    "swsh8a",
    "151":                       "sv2a",
}

# Deutsche Set-Namen → JP Set-ID
# So kann man "nachtara prismatische entwicklungen jp" schreiben
DE_SET_TO_JP = {
    # Scarlet & Violet
    "prismatische entwicklungen":  "sv6a",   # Terastal Festival ex
    "maskerade im zwielicht":      "sv5a",   # Crimson Haze
    "verborgene fabel":            "sv6",    # Mask of Change
    "stellarkrone":                "sv7",    # Stellar Miracle
    "stürmische funken":           "sv7R",   # Paradise Dragona
    "zeitliche mächte":            "sv5M",   # Wild Force / Cyber Judge
    "twilight masquerade jp":      "sv5a",
    "paldeas schicksale":          "sv4a",   # Shiny Treasure ex
    "paradoxrift":                 "sv4M",   # Ancient Roar / Future Flash
    "obsidianflammen":             "sv3a",   # Ruler of the Black Flame
    "entwicklungen in paldea":     "sv2D",   # Clay Burst / Snow Hazard
    "151 jp":                      "sv2a",
    "reisegefährten":              "sv8",    # Super Electric Breaker
    "ewige rivalen":               "sv9",    # Battle Partners
    # Sword & Shield
    "zenit der könige":            "swsh12pt5", # VSTAR Universe
    "silberne sturmwinde":         "swsh11",    # Incandescent Arcana
    "verlorener ursprung":         "swsh10a",   # Lost Abyss
    "strahlende sterne":           "swsh8a",    # VMAX Climax
    "drachenwandel":               "swsh7a",    # Blue Sky Stream
    "schaurige herrschaft":        "swsh6a",    # Eevee Heroes
    "kampfstile":                  "swsh5a",    # Peerless Fighters
    "farbenschock":                "swsh4",     # Amazing Volt Tackle
    "flammen der finsternis":      "swsh3a",    # Legendary Heartbeat
    "fusionsangriff":              "swsh8",     # Fusion Arts JP
    "astralglanz":                 "swsh10",    # Lost Origin JP Basis
}

async def jp_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """JP-Suche: /jp shiggy 151 jp"""
    if not context.args:
        await update.message.reply_text(
            "🇯🇵 <b>Japanische Karten suchen</b>\n\n"
            "Benutze: /jp KARTENNAME SET\n"
            "Oder einfach: KARTENNAME SET jp\n\n"
            "<b>Mit deutschen Set-Namen:</b>\n"
            "/jp nachtara prismatische entwicklungen\n"
            "/jp glurak 151 jp\n"
            "/jp pikachu schaurige herrschaft\n"
            "/jp umbreon drachenwandel\n\n"
            "<b>Mit JP-Set-Namen:</b>\n"
            "/jp charizard shiny treasure ex\n"
            "/jp umbreon vmax climax\n\n"
            "<b>DE → JP Set-Übersetzung:</b>\n"
            "• Prismatische Entwicklungen → Terastal Festival ex\n"
            "• Paldeas Schicksale → Shiny Treasure ex\n"
            "• Drachenwandel → Blue Sky Stream\n"
            "• Schaurige Herrschaft → Eevee Heroes\n"
            "• Zenit der Könige → VSTAR Universe\n"
            "• Stürmische Funken → Paradise Dragona\n"
            "• Ewige Rivalen → Battle Partners",
            parse_mode="HTML"
        )
        return

    query = " ".join(context.args).lower()

    # DE-Name → EN übersetzen
    pokemon_name = query
    for de, en in DE_TO_EN_POKEMON.items():
        if query.startswith(de):
            pokemon_name = query.replace(de, en, 1)
            break

    # JP-Set erkennen
    matched_set_id = None
    matched_set_name = None
    for jp_name, set_id in JP_SET_IDS.items():
        if jp_name in pokemon_name:
            matched_set_id   = set_id
            matched_set_name = jp_name
            # Setname aus Suchstring entfernen
            pokemon_name = pokemon_name.replace(jp_name, "").strip()
            break

    # Kartenname bereinigen
    card_name = pokemon_name.strip()
    if not card_name:
        await update.message.reply_text("❌ Bitte einen Kartennamen angeben.")
        return

    await update.message.reply_text(f"🔍 Suche JP: <b>{card_name}</b>" + (f" aus <b>{matched_set_name}</b>" if matched_set_name else ""), parse_mode="HTML")

    # API-Suche
    url = "https://api.pokemontcg.io/v2/cards"
    api_query = f'name:"{card_name}"'
    if matched_set_id:
        api_query += f" set.id:{matched_set_id}"

    try:
        resp = requests.get(url, params={"q": api_query, "pageSize": 30}, timeout=10)
        cards = resp.json().get("data", [])
    except Exception:
        await update.message.reply_text("❌ API nicht erreichbar, bitte erneut versuchen.")
        return

    if not cards:
        # Fallback: ohne Anführungszeichen suchen
        try:
            resp = requests.get(url, params={"q": f"name:{card_name}" + (f" set.id:{matched_set_id}" if matched_set_id else ""), "pageSize": 30}, timeout=10)
            cards = resp.json().get("data", [])
        except Exception:
            cards = []

    if not cards:
        await update.message.reply_text(
            f"❌ Keine JP-Karte gefunden für: <b>{card_name}</b>\n\n"
            f"Tipp: Benutze den englischen Namen.\n"
            f"Shiggy → squirtle, Glurak → charizard",
            parse_mode="HTML"
        )
        return

    user_id = str(update.effective_user.id)
    last_search_results[user_id] = cards[:10]

    # In DB cachen
    try:
        now = datetime.now().isoformat()
        cursor.execute("DELETE FROM card_search_cache WHERE user_id=?", (user_id,))
        for pos, card in enumerate(cards[:10], 1):
            cursor.execute(
                "INSERT INTO card_search_cache (user_id, position, card_json, created_at) VALUES (?,?,?,?)",
                (user_id, pos, json.dumps(card, ensure_ascii=False), now)
            )
        conn.commit()
    except Exception:
        pass

    if len(cards) == 1:
        await send_card_details(update.message, cards[0])
        return

    keyboard = []
    for idx, card in enumerate(cards[:10], 1):
        prices = card.get("cardmarket", {}).get("prices", {})
        trend  = prices.get("trendPrice", "?")
        set_nm = card.get("set", {}).get("name", "?")
        num    = card.get("number", "?")
        keyboard.append([InlineKeyboardButton(
            f"{idx}. {card.get('name')} | {set_nm} | #{num} | {trend}€",
            callback_data=f"sel_{user_id}_{idx}"
        )])

    await update.message.reply_text(
        f"🇯🇵 <b>JP-Ergebnisse für: {card_name}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>AnzarDexBot – Alle Funktionen</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 <b>KARTEN SUCHEN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Einfach eintippen – kein Befehl nötig:\n"
        "<code>charizard 151</code>\n"
        "<code>glurak 151</code> (deutsch funktioniert!)\n"
        "<code>umbreon vmax evolving skies</code>\n"
        "<code>pikachu ex surging sparks</code>\n\n"
        "🇯🇵 Für japanische Karten: <b>jp</b> ans Ende:\n"
        "<code>charizard 151 jp</code>\n"
        "<code>umbreon vmax climax jp</code>\n"
        "<code>nachtara drachenwandel jp</code>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 <b>PRODUKTE SUCHEN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Einfach eintippen:\n"
        "<code>151 etb</code>\n"
        "<code>destined rivals display</code>\n"
        "<code>mega evolution case</code>\n"
        "<code>surging sparks booster bundle</code>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚨 <b>RESTOCK-ALERTS</b> 🔒 Premium\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Bei Produktsuche auf 🔔 <b>Restock-Alert aktivieren</b> drücken.\n"
        "Du wirst sofort benachrichtigt wenn das Produkt\n"
        "wieder verfügbar ist – mit direktem Shop-Link.\n"
        "/myproducts – Deine aktiven Alerts anzeigen\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>PREISZIEL-ALARM</b> 🔒 Premium\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/preisziel Charizard ex 50</code>\n"
        "→ Alert wenn Karte unter 50€ fällt\n"
        "/meinepreisziele – Alle Preisziele anzeigen\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 <b>DEAL-ALERT</b> 🔒 Premium\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/deal Umbreon VMAX 20</code>\n"
        "→ Alert wenn Karte 20% unter Trend-Preis fällt\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 <b>NEUE SET-ALERTS</b> 🔒 Premium\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/setalert_sets</code> aktivieren\n"
        "→ Sofort benachrichtigt wenn neues Set erscheint\n"
        "<code>/setalert_sets_off</code> deaktivieren\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>PORTFOLIO-TRACKER</b> 🔒 Premium\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/portfolio_add Charizard ex 151 | 2 | 89.99</code>\n"
        "→ Karte mit Anzahl und Kaufpreis eintragen\n"
        "<code>/portfolio</code> – Gesamtwert + Gewinn/Verlust\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>PREISE & VERLAUF</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/preishistory Charizard</code> – Preisverlauf\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 <b>SETS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/allsets</code> – Alle bekannten Sets\n"
        "<code>/favset 151</code> – Set favorisieren\n"
        "<code>/meinesets</code> – Favoriten anzeigen\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "💳 <b>ABO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/abo</code> – Premium für 4,99€/Monat\n"
        "Kreditkarte · Apple Pay · Google Pay · Klarna\n"
        "Jederzeit kündbar – automatische Freischaltung\n\n"

        "<i>🔒 = Nur für Premium-Abonnenten</i>"
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
            # card_name kann "Charizard ex|151" Format haben
            parts     = card_name.split("|")
            cn        = parts[0].strip()
            sn        = parts[1].strip() if len(parts) > 1 else None
            cards     = search_pokemon_card(cn, sn)
            if not cards:
                continue
            # Richtige Karte mit passendem Set finden
            best = None
            for card in cards:
                if not isinstance(card, dict): continue
                s = card.get("set", {})
                sname = s.get("name","").lower() if isinstance(s, dict) else ""
                if not sn or sn.lower() in sname:
                    best = card
                    break
            card      = best or (cards[0] if isinstance(cards[0], dict) else None)
            if not card: continue
            name      = card_name  # Original mit Set-Info behalten
            prices    = card.get("cardmarket", {}).get("prices", {}) or {}
            new_price = prices.get("lowPrice") or prices.get("trendPrice")
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
                    f"🚨 <b>RESTOCK!</b> 🚨\n\n"
                    f"📦 <b>{product_query.upper()}</b>\n"
                    f"🏪 <b>{shop_name}</b>\n\n"
                    f"🛒 <a href='{product_url}'>Direkt zum Produkt</a>\n"
                    f"💳 <a href='{cm_url}'>Cardmarket DE</a>\n\n"
                    f"⚡ <i>Schnell sein!</i>"
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
    elif data.startswith("pz_"):
        await preisziel_callback(update, context)
    elif data.startswith("da_"):
        await deal_alert_callback(update, context)
    elif data.startswith("pa_"):
        await portfolio_add_callback(update, context)
    elif data.startswith("tc_") or data.startswith("utc_") or data.startswith("track_") or data.startswith("untrack_"):
        await action_button_handler(update, context)
    elif data.startswith("trackproduct_"):
        await product_button_handler(update, context)
    elif data.startswith("removeproduct_"):
        await remove_product_handler(update, context)
    elif data.startswith("removecard_"):
        await remove_card_handler(update, context)
    elif data == "clear_portfolio":
        await portfolio_clear(update, context)
    elif data == "clear_preisziele":
        await clear_preisziele_handler(update, context)
    else:
        await query.answer()


# ═════════════════════════════════════════════════════════
# FEATURE 1: PREISALARM – User setzt Wunschpreis
# ═════════════════════════════════════════════════════════
@require_sub
async def setpreisziel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Benutze: /preisziel KARTENNAME | SET | PREIS"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "🎯 <b>Preisziel setzen</b>\n\n"
            "Benutze: /preisziel KARTENNAME | SET | PREIS\n\n"
            "Beispiele:\n"
            "<code>/preisziel Charizard ex | 151 | 50</code>\n"
            "<code>/preisziel Umbreon VMAX | Evolving Skies | 30</code>\n\n"
            "💡 Tipp: Suche die Karte und drücke 🎯 – dann wird sie automatisch erkannt!",
            parse_mode="HTML"
        )
        return
    full  = " ".join(context.args)
    parts = [p.strip() for p in full.split("|")]
    set_name = ""
    if len(parts) >= 3:
        card_name = parts[0]
        set_name  = parts[1]
        try: target = float(parts[2].replace(',', '.'))
        except: await update.message.reply_text('❌ Preis ungültig. Beispiel: /preisziel Charizard ex | 151 | 50'); return
    elif len(parts) == 2:
        card_name = parts[0]
        try: target = float(parts[1].replace(',', '.'))
        except: await update.message.reply_text('❌ Bitte auch Preis angeben: /preisziel Charizard ex | 151 | 50'); return
    else:
        try:
            target    = float(context.args[-1].replace(',', '.'))
            card_name = " ".join(context.args[:-1])
        except ValueError:
            await update.message.reply_text('❌ Beispiel: /preisziel Charizard ex | 151 | 50')
            return

    user_id = str(update.effective_user.id)
    now = datetime.now().isoformat()
    # card_name kann "Charizard ex|151|6" Format haben (vom Button)
    # oder normaler Name vom Command
    cursor.execute(
        "INSERT INTO price_targets (user_id, card_name, target_price, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, card_name) DO UPDATE SET target_price=excluded.target_price",
        (user_id, card_name, target, now)
    )
    conn.commit()
    display_name = card_name.split("|")[0] if "|" in card_name else card_name
    set_hint     = f" ({card_name.split('|')[1]})" if "|" in card_name and len(card_name.split('|')) > 1 else ""
    await update.message.reply_text(
        f"🎯 <b>Preisziel gesetzt!</b>\n\n"
        f"🃏 {display_name}{set_hint}\n"
        f"💰 Alert wenn Preis unter <b>{target} €</b> fällt\n\n"
        f"✅ Ich prüfe alle 5 Minuten!",
        parse_mode="HTML"
    )

@require_sub
async def meinepreisziele(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute("SELECT card_name, target_price FROM price_targets WHERE user_id=?", (user_id,))
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("Du hast noch keine Preisziele gesetzt.\n/preisziel Charizard ex 50")
        return
    text = "🎯 <b>Deine Preisziele</b>\n\n"
    for card_name, target in rows:
        text += f"🃏 {card_name} → unter {target} €\n"
    keyboard = [[InlineKeyboardButton("❌ Alle löschen", callback_data="clear_preisziele")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ═════════════════════════════════════════════════════════
# FEATURE 2: NEUE SET-ALERTS
# ═════════════════════════════════════════════════════════
@require_sub
async def setalert_sets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute(
        "INSERT INTO set_alerts (user_id, active) VALUES (?,1) "
        "ON CONFLICT(user_id) DO UPDATE SET active=1",
        (user_id,)
    )
    conn.commit()
    await update.message.reply_text(
        "🆕 <b>Neue-Set-Alerts aktiviert!</b>\n\n"
        "Du wirst sofort benachrichtigt wenn ein neues Pokémon TCG Set angekündigt wird oder erscheint.\n\n"
        "/setalert_sets_off zum Deaktivieren",
        parse_mode="HTML"
    )

async def setalert_sets_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute("UPDATE set_alerts SET active=0 WHERE user_id=?", (user_id,))
    conn.commit()
    await update.message.reply_text("🔕 Neue-Set-Alerts deaktiviert.")

# ═════════════════════════════════════════════════════════
# FEATURE 4: DEAL-ALERT – Karte günstiger als normal
# ═════════════════════════════════════════════════════════
@require_sub
async def setdeal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Benutze: /deal Charizard ex 20  (alert wenn 20% günstiger als Trend)"""
    if len(context.args) < 1:
        await update.message.reply_text(
            "🔥 <b>Deal-Alert setzen</b>\n\n"
            "Benutze: /deal KARTENNAME PROZENT\n\n"
            "Beispiel:\n"
            "/deal Charizard ex 20\n\n"
            "Du wirst benachrichtigt wenn die Karte mehr als 20% unter dem Trend-Preis angeboten wird.\n"
            "Standard: 15% wenn kein Prozent angegeben.",
            parse_mode="HTML"
        )
        return
    full  = " ".join(context.args)
    parts = [p.strip() for p in full.split("|")]
    pct   = 15
    if len(parts) >= 3:
        card_name = parts[0]
        set_name  = parts[1]
        try: pct = int(parts[2])
        except: pct = 15
    elif len(parts) == 2:
        card_name = parts[0]
        try:
            pct      = int(parts[1])
            set_name = ""
        except:
            set_name  = parts[1]
    else:
        try:
            pct       = int(context.args[-1])
            card_name = " ".join(context.args[:-1])
            set_name  = ""
        except:
            card_name = full
            set_name  = ""

    user_id = str(update.effective_user.id)
    cursor.execute(
        "INSERT INTO deal_alerts (user_id, card_name, threshold_pct) VALUES (?,?,?) "
        "ON CONFLICT(user_id, card_name) DO UPDATE SET threshold_pct=excluded.threshold_pct",
        (user_id, card_name, pct)
    )
    conn.commit()
    await update.message.reply_text(
        f"🔥 <b>Deal-Alert aktiviert!</b>\n\n"
        f"🃏 {card_name}\n"
        f"📉 Alert wenn Preis <b>{pct}% unter Trend-Preis</b> fällt\n\n"
        f"Ich prüfe alle 5 Minuten!",
        parse_mode="HTML"
    )

# ═════════════════════════════════════════════════════════
# FEATURE 6: PORTFOLIO-TRACKER
# ═════════════════════════════════════════════════════════
@require_sub
async def portfolio_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Benutze: /portfolio_add Charizard ex 151 2 89.99"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "📊 <b>Portfolio – Karte hinzufügen</b>\n\n"
            "Benutze: /portfolio_add KARTENNAME | ANZAHL | KAUFPREIS\n\n"
            "Beispiele:\n"
            "/portfolio_add Charizard ex 151 | 1 | 89.99\n"
            "/portfolio_add Umbreon VMAX | 2 | 45.00\n\n"
            "Der Bot verfolgt den aktuellen Wert deiner Sammlung!",
            parse_mode="HTML"
        )
        return
    full = " ".join(args)
    parts = [p.strip() for p in full.split("|")]
    if len(parts) == 3:
        card_name, qty_str, price_str = parts
        set_name = ""
    elif len(parts) == 2:
        card_name, qty_str = parts
        price_str = "0"
        set_name = ""
    else:
        card_name = full
        qty_str = "1"
        price_str = "0"
        set_name = ""

    try:
        qty   = max(1, int(qty_str))
        price = float(price_str.replace(",", "."))
    except ValueError:
        qty   = 1
        price = 0.0

    user_id = str(update.effective_user.id)
    now     = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO portfolio (user_id, card_name, set_name, quantity, buy_price, added_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, card_name, set_name) DO UPDATE SET "
        "quantity=quantity+excluded.quantity, buy_price=excluded.buy_price",
        (user_id, card_name.strip(), set_name, qty, price, now)
    )
    conn.commit()
    total = qty * price
    await update.message.reply_text(
        f"📊 <b>Zum Portfolio hinzugefügt!</b>\n\n"
        f"🃏 {card_name.strip()}\n"
        f"📦 Anzahl: {qty}x\n"
        f"💰 Kaufpreis: {price} € pro Stück\n"
        f"💵 Gesamt investiert: {total:.2f} €\n\n"
        f"/portfolio zum Gesamtüberblick",
        parse_mode="HTML"
    )

@require_sub
async def portfolio_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cursor.execute(
        "SELECT card_name, set_name, quantity, buy_price FROM portfolio WHERE user_id=? ORDER BY card_name",
        (user_id,)
    )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text(
            "📊 Dein Portfolio ist leer.\n\n"
            "Füge Karten hinzu mit:\n"
            "/portfolio_add Charizard ex 151 | 1 | 89.99"
        )
        return

    await update.message.reply_text("📊 <b>Dein Portfolio wird berechnet...</b>", parse_mode="HTML")

    total_invested = 0.0
    total_current  = 0.0
    lines = []

    for card_name_raw, set_name_raw, qty, buy_price in rows:
        # card_name kann "Charizard ex|151" Format haben
        if "|" in card_name_raw and not set_name_raw:
            parts     = card_name_raw.split("|")
            card_name = parts[0].strip()
            set_name  = parts[1].strip() if len(parts) > 1 else ""
        else:
            card_name = card_name_raw
            set_name  = set_name_raw

        invested = qty * buy_price
        total_invested += invested

        # Aktuellen Preis von API holen – mit richtigem Set
        cards = search_pokemon_card(card_name, set_name if set_name else None)
        current_price = 0.0
        if cards:
            for card in cards:
                if not isinstance(card, dict):
                    continue
                s     = card.get("set", {})
                sname = s.get("name","").lower() if isinstance(s, dict) else ""
                if not set_name or set_name.lower() in sname:
                    prices = card.get("cardmarket", {}).get("prices", {}) or {}
                    p = float(prices.get("lowPrice") or prices.get("trendPrice") or 0)
                    if p > 0:
                        current_price = p
                        break
        current_total = qty * current_price
        total_current += current_total
        diff     = current_total - invested
        diff_pct = (diff / invested * 100) if invested > 0 else 0
        emoji    = "📈" if diff >= 0 else "📉"
        lines.append(
            f"{emoji} <b>{card_name}</b>\n"
            f"   {qty}x · Kauf: {buy_price:.2f}€ · Jetzt: {current_price:.2f}€ · "
            f"{'+'if diff>=0 else ''}{diff:.2f}€ ({diff_pct:+.1f}%)"
        )

    gesamtdiff     = total_current - total_invested
    gesamtdiff_pct = (gesamtdiff / total_invested * 100) if total_invested > 0 else 0
    gesamtemoji    = "📈" if gesamtdiff >= 0 else "📉"

    text = (
        f"📊 <b>Dein Portfolio</b>\n\n"
        + "\n".join(lines)
        + f"\n\n{'─'*25}\n"
        f"💰 Investiert: <b>{total_invested:.2f} €</b>\n"
        f"💵 Aktueller Wert: <b>{total_current:.2f} €</b>\n"
        f"{gesamtemoji} Gesamt: <b>{'+'if gesamtdiff>=0 else ''}{gesamtdiff:.2f} € ({gesamtdiff_pct:+.1f}%)</b>"
    )

    keyboard = [[InlineKeyboardButton("🗑 Portfolio leeren", callback_data="clear_portfolio")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def portfolio_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    cursor.execute("DELETE FROM portfolio WHERE user_id=?", (user_id,))
    conn.commit()
    await query.message.edit_text("🗑 Portfolio geleert.")

async def clear_preisziele_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    cursor.execute("DELETE FROM price_targets WHERE user_id=?", (user_id,))
    conn.commit()
    await query.message.edit_text("❌ Alle Preisziele gelöscht.")


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
                "🎉 <b>Willkommen bei AnzarDexBot Premium!</b>\n\n"
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
                        "🎉 <b>Zahlung bestätigt! Willkommen bei AnzarDexBot Premium!</b>\n\n"
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


# ═════════════════════════════════════════════════════════
# JOBS: Preisziele, Deal-Alerts, Neue Sets
# ═════════════════════════════════════════════════════════
async def job_price_targets(context: ContextTypes.DEFAULT_TYPE):
    """Prüft ob Preisziele erreicht wurden."""
    cursor.execute("SELECT user_id, card_name, target_price FROM price_targets")
    targets = cursor.fetchall()
    for user_id, card_name, target_price in targets:
        if not is_subscribed(user_id):
            continue
        try:
            cards = search_pokemon_card(card_name)
            if not cards:
                continue
            prices    = cards[0].get("cardmarket", {}).get("prices", {})
            low_price = prices.get("lowPrice") or prices.get("trendPrice")
            if not low_price:
                continue
            if float(low_price) <= float(target_price):
                cm_url = cards[0].get("cardmarket", {}).get("url", "")
                if cm_url and not cm_url.startswith("http"):
                    cm_url = "https://www.cardmarket.com" + cm_url
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "\U0001f3af <b>PREISZIEL ERREICHT!</b>\n\n"
                        + f"\U0001f0cf <b>{cn}</b>" + (f" \u00b7 {sn}" if sn else "") + "\n"
                        + f"\U0001f4b0 Aktueller Preis: <b>{low_price} \u20ac</b>\n"
                        + f"\U0001f3af Dein Ziel: {target_price} \u20ac\n\n"
                        + (f'<a href="{cm_url}">\U0001f6d2 Jetzt kaufen</a>' if cm_url else "\U0001f6d2 Jetzt auf Cardmarket!")
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
        except Exception as e:
            print(f"\u26a0\ufe0f Preisziel-Check Fehler: {e}")

async def job_deal_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Prüft ob Karten deutlich unter Trend-Preis sind."""
    cursor.execute("SELECT user_id, card_name, threshold_pct FROM deal_alerts")
    deals = cursor.fetchall()
    for user_id, card_name_raw, threshold_pct in deals:
        if not is_subscribed(user_id):
            continue
        try:
            # card_name kann "Charizard ex|151" Format haben
            parts     = card_name_raw.split("|")
            card_name = parts[0].strip()
            set_name  = parts[1].strip() if len(parts) > 1 else None

            cards = search_pokemon_card(card_name, set_name)
            if not cards:
                continue

            # Beste Karte finden (richtiges Set)
            best_card = None
            for card in cards:
                if isinstance(card, dict):
                    s = card.get("set", {})
                    sname = s.get("name","").lower() if isinstance(s, dict) else ""
                    if set_name and set_name.lower() in sname:
                        best_card = card
                        break
            if not best_card:
                best_card = cards[0] if isinstance(cards[0], dict) else None
            if not best_card:
                continue

            prices = best_card.get("cardmarket", {}).get("prices", {}) or {}
            trend  = prices.get("trendPrice")
            low    = prices.get("lowPrice")
            if not trend or not low:
                continue

            discount = ((float(trend) - float(low)) / float(trend)) * 100
            if discount < threshold_pct:
                continue

            cm_url = best_card.get("cardmarket", {}).get("url", "")
            if cm_url and not cm_url.startswith("http"):
                cm_url = "https://www.cardmarket.com" + cm_url
            if not cm_url:
                q = f"{card_name} {set_name or ''}".strip().replace(" ", "%20")
                cm_url = f"https://www.cardmarket.com/de/Pokemon/Products/Singles/Search?searchString={q}"

            display_name = card_name + (f" | {set_name}" if set_name else "")
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🔥 <b>DEAL GEFUNDEN!</b>\n\n"
                    f"🃏 <b>{display_name}</b>\n"
                    f"💰 Günstigster Preis: <b>{low} €</b>\n"
                    f"📉 Trend-Preis: {trend} €\n"
                    f"🔥 <b>{discount:.0f}% unter Trend!</b>\n\n"
                    f"<a href='{cm_url}'>🛒 Jetzt zuschlagen!</a>"
                ),
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"⚠️ Deal-Alert Fehler: {e}")

async def job_new_sets(context: ContextTypes.DEFAULT_TYPE):
    """Prüft ob neue Sets erschienen sind und benachrichtigt Abonnenten."""
    global ALL_SETS
    try:
        resp = requests.get(
            "https://api.pokemontcg.io/v2/sets",
            params={"pageSize": 10, "orderBy": "-releaseDate"},
            timeout=15
        )
        new_sets_data = resp.json().get("data", [])
        cursor.execute("SELECT user_id FROM set_alerts WHERE active=1")
        users = [r[0] for r in cursor.fetchall()]

        for s in new_sets_data:
            set_id   = s.get("id", "")
            set_name = s.get("name", "")
            release  = s.get("releaseDate", "")
            # Prüfen ob schon bekannt
            cursor.execute("SELECT 1 FROM known_sets_notified WHERE set_id=?", (set_id,))
            if cursor.fetchone():
                continue
            # Neu! Alle User benachrichtigen
            cursor.execute("INSERT OR IGNORE INTO known_sets_notified (set_id) VALUES (?)", (set_id,))
            conn.commit()
            # Sets-Cache aktualisieren
            ALL_SETS[set_name.lower()] = set_id
            for user_id in users:
                if not is_subscribed(user_id):
                    continue
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🆕 <b>NEUES SET ERSCHIENEN!</b>\n\n"
                            f"📦 <b>{set_name}</b>\n"
                            f"📅 Release: {release}\n\n"
                            f"Suche jetzt Karten aus diesem Set!\n"
                            f"Tippe einfach einen Kartennamen + <b>{set_name}</b>"
                        ),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ Neue-Sets-Check Fehler: {e}")



async def job_amazon_invite_check(context: ContextTypes.DEFAULT_TYPE):
    """
    1. Scannt Amazon Store nach neuen Produkten
    2. Prüft alle Produkte auf Verfügbarkeit / Einladung
    3. Benachrichtigt nur wenn verfügbar – kein Alert wenn ausverkauft
    """
    # Admin immer benachrichtigen, Plus alle aktiven Abonnenten
    cursor.execute(
        "SELECT user_id FROM subscriptions WHERE status='active' "
        "UNION SELECT ? WHERE ? != ''",
        (ADMIN_ID, ADMIN_ID)
    )
    all_users = list({r[0] for r in cursor.fetchall()})
    if not all_users:
        return

    # Neue Produkte vom Amazon Store entdecken
    try:
        new_products = discover_amazon_products_from_store()
        for asin, name in new_products.items():
            cursor.execute(
                "INSERT OR IGNORE INTO amazon_products (asin, product_name, amazon_url, last_status, added_at) "
                "VALUES (?,?,?,'unknown',?)",
                (asin, name, get_amazon_product_url(asin), datetime.now().isoformat())
            )
        conn.commit()
        if new_products:
            print(f"🆕 {len(new_products)} neue Amazon-Produkte entdeckt")
    except Exception as e:
        print(f"⚠️ Amazon Discovery Fehler: {e}")

    # Alle zu checkenden Produkte = bekannte + neu entdeckte
    all_products = dict(KNOWN_AMAZON_PRODUCTS)
    cursor.execute("SELECT asin, product_name FROM amazon_products")
    for asin, name in cursor.fetchall():
        if asin not in all_products:
            all_products[asin] = name

    for asin, product_name in all_products.items():
        try:
            status, url = check_amazon_invite(asin)

            # Letzten Status prüfen
            cursor.execute("SELECT last_status FROM amazon_products WHERE asin=?", (asin,))
            row = cursor.fetchone()
            old_status = row[0] if row else "unknown"

            # Status speichern
            cursor.execute(
                "INSERT INTO amazon_products (asin, product_name, amazon_url, last_status, added_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(asin) DO UPDATE SET "
                "last_status=excluded.last_status, amazon_url=excluded.amazon_url",
                (asin, product_name, url, status, datetime.now().isoformat())
            )
            conn.commit()

            # Nur bei Einladung oder neu verfügbar benachrichtigen
            if status not in ("invite", "available"):
                # Wenn wieder ausverkauft → gesendete Alerts löschen
                if status == "soldout":
                    cursor.execute("DELETE FROM amazon_invite_alerts WHERE asin=?", (asin,))
                    conn.commit()
                continue

            # Nur wenn Status sich geändert hat
            if old_status == status:
                continue

            # Nur bei Einladung oder Verfügbarkeit benachrichtigen
            AMAZON_INVITE_LINK = "https://amzn.to/4srY4S3"

            for user_id in all_users:
                cursor.execute(
                    "SELECT 1 FROM amazon_invite_alerts WHERE asin=? AND user_id=?",
                    (asin, user_id)
                )
                if cursor.fetchone():
                    continue

                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"🎟 <b>EINLADUNGEN SIND RAUS!</b>\n\n"
                            f"📦 <b>{product_name}</b>\n\n"
                            f"👉 <a href='{AMAZON_INVITE_LINK}'>Jetzt Einladung sichern!</a>\n\n"
                            f"⚡ <i>Schnell sein – Einladungen sind oft nur kurz verfügbar!</i>"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=False
                    )
                    cursor.execute(
                        "INSERT OR IGNORE INTO amazon_invite_alerts (asin, user_id, sent_at) VALUES (?,?,?)",
                        (asin, user_id, datetime.now().isoformat())
                    )
                    conn.commit()
                except Exception as e:
                    print(f"⚠️ Amazon Alert Fehler ({user_id}): {e}")

        except Exception as e:
            print(f"⚠️ Amazon Job Fehler ({asin}): {e}")



# ─────────────────────────────────────────
# POKÉMON CENTER DE – Restock-Überwachung
# ─────────────────────────────────────────
POKEMON_CENTER_DE_URL = (
    "https://www.pokemoncenter.com/de-de/category/trading-card-game"
    "?availability=true&category=tcg-cards"
)
POKEMON_CENTER_GB_URL = (
    "https://www.pokemoncenter.com/en-gb/category/trading-card-game"
    "?availability=true&category=tcg-cards"
)
POKEMON_CENTER_URLS = {
    "Pokémon Center DE": POKEMON_CENTER_DE_URL,
    "Pokémon Center UK": POKEMON_CENTER_GB_URL,
}

async def job_pokemon_center_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Überwacht den offiziellen Pokémon Center DE auf neue Produkte.
    Schickt Alert wenn neue Karten/Produkte verfügbar sind.
    """
    cursor.execute(
        "SELECT user_id FROM subscriptions WHERE status='active' "
        "UNION SELECT ? WHERE ? != ''",
        (ADMIN_ID, ADMIN_ID)
    )
    all_users = list({r[0] for r in cursor.fetchall()})
    if not all_users:
        return

    for pc_name, pc_url in POKEMON_CENTER_URLS.items():
      try:
        resp = requests.get(
            pc_url,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "de-DE,de;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        html = resp.text.lower()

        # Produkte aus der Seite extrahieren
        import re as _re
        product_titles = _re.findall(
            r'(?:class="[^"]*product[^"]*"[^>]*>|<h[123][^>]*>)([^<]{5,80})</',
            resp.text
        )
        # Pokémon TCG Produkte filtern
        tcg_keywords = [
            "booster", "display", "elite trainer", "etb", "collection",
            "tin", "bundle", "premium", "trainer box", "upc", "case",
        ]
        found_products = []
        for title in product_titles:
            title_lower = title.lower().strip()
            if any(kw in title_lower for kw in tcg_keywords):
                if "pokémon" in title_lower or "pokemon" in title_lower or len(title_lower) < 60:
                    found_products.append(title.strip()[:80])

        # Prüfen ob neue Produkte da sind
        cursor.execute(
            "SELECT last_status FROM restock_status WHERE url=?",
            (pc_url,)
        )
        row          = cursor.fetchone()
        old_count    = int(row[0]) if row and row[0] and row[0].isdigit() else 0
        new_count    = len(found_products)

        cursor.execute(
            "INSERT INTO restock_status (url, last_status) VALUES (?,?) "
            "ON CONFLICT(url) DO UPDATE SET last_status=excluded.last_status",
            (pc_url, str(new_count))
        )
        conn.commit()

        # Nur benachrichtigen wenn neue Produkte hinzugekommen sind
        if new_count <= old_count and old_count > 0:
            return

        # Produktliste für die Nachricht
        product_list = ""
        if found_products:
            shown = found_products[:10]
            product_list = "\n".join(f"• {p}" for p in shown)
            if len(found_products) > 10:
                product_list += f"\n• ... und {len(found_products)-10} weitere"
        else:
            product_list = "• Neue Produkte verfügbar"

        # Direkte Produkt-Links aus der Seite extrahieren
        import re as _re
        product_links = []
        raw_links = _re.findall(r'href="(/(?:de-de|en-gb)/product/[^"]+)"', resp.text)
        base_domain = "https://www.pokemoncenter.com"
        seen_links = set()
        for link in raw_links[:15]:
            full_link = base_domain + link
            if full_link not in seen_links:
                seen_links.add(full_link)
                # Produktnamen aus URL extrahieren
                slug = link.split("/product/")[-1].split("?")[0]
                name_from_url = slug.replace("-", " ").replace("/", " ").strip().title()
                product_links.append((name_from_url[:60], full_link))

        # Text mit direkten Links aufbauen
        if product_links:
            product_list_linked = "\n".join(
                f"• <a href='{lnk}'>{nm}</a>" for nm, lnk in product_links[:10]
            )
            if len(product_links) > 10:
                product_list_linked += f"\n• <a href='{pc_url}'>... und mehr anzeigen</a>"
        else:
            product_list_linked = f"• <a href='{pc_url}'>Alle verfügbaren Produkte anzeigen</a>"

        for user_id in all_users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🏪 <b>{pc_name.upper()} – RESTOCK!</b>\n\n"
                        f"Neue Produkte verfügbar – direkt zum Produkt:\n\n"
                        f"{product_list_linked}\n\n"
                        f"⚡ <i>Schnell sein – Produkte sind oft schnell ausverkauft!</i>"
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as e:
                print(f"⚠️ Pokémon Center Alert Fehler ({user_id}): {e}")

      except Exception as e:
        print(f"⚠️ {pc_name} Check Fehler: {e}")


def main():
    global telegram_app_ref
    app = Application.builder().token(BOT_TOKEN).build()
    telegram_app_ref = app
    jq  = app.job_queue
    jq  = app.job_queue

    # Jobs
    jq.run_repeating(job_price_check,      interval=300,   first=15)
    jq.run_repeating(job_restock_check,    interval=600,   first=30)
    jq.run_repeating(job_url_restock_check,interval=600,   first=45)
    jq.run_repeating(job_refresh_sets,     interval=86400, first=3600)
    jq.run_repeating(job_price_targets,    interval=300,   first=60)   # Preisziele
    jq.run_repeating(job_deal_alerts,      interval=300,   first=90)   # Deal-Alerts
    jq.run_repeating(job_new_sets,         interval=3600,  first=120)  # Neue Sets
    jq.run_repeating(job_amazon_invite_check,   interval=300, first=20)  # Amazon alle 5 Min
    jq.run_repeating(job_pokemon_center_check,  interval=600, first=10)  # Pokémon Center alle 10 Min

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
    app.add_handler(CommandHandler("hilfe",        help_command))
    app.add_handler(CommandHandler("admin",        admin_stats))
    app.add_handler(CommandHandler("adduser",      admin_adduser))
    app.add_handler(CommandHandler("removeuser",   admin_removeuser))
    app.add_handler(CommandHandler("preisziel",    setpreisziel))
    app.add_handler(CommandHandler("meinepreisziele", meinepreisziele))
    app.add_handler(CommandHandler("setalert_sets",setalert_sets))
    app.add_handler(CommandHandler("setalert_sets_off", setalert_sets_off))
    app.add_handler(CommandHandler("deal",         setdeal))
    app.add_handler(CommandHandler("portfolio_add",portfolio_add))
    app.add_handler(CommandHandler("portfolio",    portfolio_show))
    app.add_handler(CommandHandler("jp",           jp_search))

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