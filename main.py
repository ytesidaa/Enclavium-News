import json
import os
import random
import re
import sys
import tempfile
import time
from urllib.parse import quote, unquote

import feedparser
import requests

import traceback

from card import make_card, missing_assets, ASSETS

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# photo_source:
#   "article" -> use the photo from the news article itself (design stays ours)
#   "search"  -> ignore the article photo; the LLM proposes image search queries
#                for the story topic and we search Wikimedia Commons (free licence)
FEEDS = [
    {"name": "CryptoSlate", "url": "https://cryptoslate.com/feed/", "photo_source": "search"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "photo_source": "article"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "photo_source": "search"},
]

STATE_FILE = "last_seen.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")   # optional: free stock photos
GEMINI_MODEL = "gemini-3.6-flash"
DRY_RUN = os.environ.get("DRY_RUN") == "1"       # save cards locally, do not post

MAX_PER_RUN = 20            # articles analysed per run (rest wait for the next run)
SLEEP_BETWEEN = 4           # seconds between Gemini calls (free-tier friendly)
EMBED_COLOR = 0xB11226
UA = {"User-Agent": "EnclaviumNewsBot/1.0 (https://github.com/ytesidaa/cryptoslate-discord-bot)"}

NOTABLE_ENTITIES = [
    # People
    "Donald Trump", "Changpeng Zhao", "CZ", "Jack Dorsey", "Jack Mallers",
    "Elon Musk", "Michael Saylor", "Vitalik Buterin", "Brian Armstrong",
    "Sam Altman", "Gary Gensler", "Paul Atkins",
    # Founders / protocol figures
    "Anatoly Yakovenko", "Paolo Ardoino", "Jed McCaleb",
    # VCs
    "Paradigm", "Polychain Capital", "a16z", "Andreessen Horowitz",
    "Sequoia Capital", "Pantera Capital",
    # Exchanges / platforms
    "Coinbase", "Binance", "Bybit", "Kraken", "OKX", "Robinhood",
    # Protocols / assets often tied to major narratives
    "Solana", "Tether", "Sui", "Hyperliquid", "BNB Chain", "Ethereum",
    "Bitcoin",
]


# ---------------------------------------------------------------------------
# STATE HELPERS
# ---------------------------------------------------------------------------

def load_seen():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_seen(seen_ids):
    with open(STATE_FILE, "w") as f:
        json.dump(seen_ids[-300:], f, indent=2)


def clean_html(raw_html):
    return re.sub("<[^<]+?>", "", raw_html or "").strip()


# ---------------------------------------------------------------------------
# GEMINI: FILTER + COPYWRITING
# ---------------------------------------------------------------------------

def clean_title(raw):
    """Headline hygiene: no commas (except decimals), no !/?, single spaces."""
    t = str(raw or "")
    t = re.sub(r"(?<!\d),|,(?!\d)", " ", t)
    t = re.sub(r"[!?]+", "", t)
    t = re.sub(r"\s+", " ", t).strip(" .;:-")
    return t


def analyze_with_gemini(title, summary):
    """
    Returns dict:
      relevant, id_title, highlight, id_summary, image_queries (list of English search terms)
    """
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config={"response_mime_type": "application/json", "temperature": 0.3},
    )
    entity_list_str = ", ".join(NOTABLE_ENTITIES)
    keyword_list = ", ".join(sorted(k for k in LIBRARY if k != "default")) or "-"

    prompt = f"""Kamu adalah asisten kurasi berita untuk media crypto Instagram bernama Enclavium.

Tugas kamu:
1. Tentukan apakah artikel ini menyebut tokoh besar, founder proyek besar, VC ternama, atau exchange besar di dunia crypto/AI/finance. Contoh nama yang WAJIB dianggap relevan: {entity_list_str}. Selain daftar ini, kamu boleh memakai pengetahuanmu sendiri untuk mengenali tokoh/entitas besar lain yang relevan di dunia crypto, AI, atau makroekonomi global.
2. Jika relevan, tulis dalam Bahasa Indonesia dengan gaya jurnalistik formal, profesional, dan mudah dipahami (seperti kantor berita nasional). Jangan menerjemahkan judul asli secara harfiah:
   - "id_title": judul yang menonjolkan sudut pandang paling penting bagi pembaca (dampak ke harga, pasar, regulasi, keamanan dana, atau keputusan besar). MAKSIMAL 70 karakter (sekitar 9 kata). Aturan penulisan: kalimat aktif dengan urutan Subjek + Kata kerja + Inti berita; subjek jelas (siapa yang bertindak); kata kerja berita yang tepat (misalnya menetapkan, mengungkap, menambah, menolak, memperketat, menyetujui); TANPA tanda koma (kecuali koma desimal pada angka), tanpa tanda seru, tanpa tanda tanya, tanpa huruf kapital berlebihan, tanpa kata hiperbolik (misalnya melayang, bakal tembus, meledak, gila). Tulis angka ringkas dengan format Indonesia (contoh: US$6,3 miliar, 12%). Judul ini dicetak besar di gambar.
   - "highlight": 2 sampai 4 kata berurutan dari id_title yang memuat inti berita atau angka yang paling penting bagi pembaca (apa yang terjadi atau seberapa besar dampaknya), tanpa tanda baca di awal atau akhir, ditulis PERSIS sama seperti di id_title. Idealnya berada di bagian akhir judul.
   - Contoh format yang baik (hanya contoh, jangan disalin): judul "Bursa X Ungkap Peretasan Senilai US$6,3 Miliar" dengan highlight "US$6,3 Miliar"; judul "Regulator Setujui ETF Solana Pertama di AS" dengan highlight "ETF Solana Pertama". Contoh yang BURUK: judul yang memakai koma, judul yang menggantung, atau highlight yang hanya berisi kata sambung.
   - "id_summary": 2-3 kalimat pendek yang hanya memuat fakta dari artikel (siapa, apa, angka, kapan), tanpa opini, tanpa prediksi, dan tanpa menambah informasi yang tidak ada di artikel. Kalimat pertama menjelaskan peristiwa utama dan alasan pentingnya bagi pembaca. Gunakan istilah crypto umum apa adanya dan jelaskan singkat bila perlu.
   - "person": nama lengkap SATU tokoh manusia (bahasa Inggris, contoh: "Elon Musk") jika berita berpusat pada orang tersebut, selain itu null.
   - "keywords": 1 sampai 2 kata kunci yang PALING relevan dengan topik berita, dipilih HANYA dari daftar ini: {keyword_list}. Urutkan dari yang paling penting. Pakai [] jika tidak ada yang cocok.
   - "image_queries": 1 sampai 3 kata kunci pencarian FOTO dalam Bahasa Inggris untuk Wikimedia Commons, urut dari paling spesifik ke paling umum, sesuai topik utama berita. Pakai subjek nyata yang mungkin punya foto: nama tokoh (contoh: "Elon Musk"), perusahaan atau produk (contoh: "Binance", "Ledger hardware wallet"), gedung atau lokasi (contoh: "Federal Reserve building"), atau objek umum (contoh: "Bitcoin coin"). Jangan pakai kalimat panjang atau konsep abstrak. Pakai [] jika tidak ada subjek visual yang jelas.
   - "stock_queries": 1 sampai 3 kata kunci pencarian foto stok dalam Bahasa Inggris untuk topik umum berita, berupa BENDA atau SUASANA tanpa orang dan tanpa wajah (contoh: "bitcoin coin", "stock market screen", "server room", "government building", "gold bars", "digital network"). Hindari menampilkan orang pada berita kejahatan, peretasan, atau kerugian.

Artikel:
Judul: {title}
Ringkasan asli: {summary}

Jawab HANYA dengan JSON persis seperti ini:
{{"relevant": true, "id_title": "...", "highlight": "...", "id_summary": "...", "person": "..." atau null, "keywords": ["..."], "image_queries": ["...", "..."], "stock_queries": ["...", "..."]}}
Jika tidak relevan: {{"relevant": false, "id_title": "", "highlight": "", "id_summary": "", "person": null, "keywords": [], "image_queries": [], "stock_queries": []}}
"""

    for attempt in range(2):
        try:
            response = model.generate_content(prompt)
            break
        except Exception as e:
            if attempt == 0 and ("429" in str(e) or "quota" in str(e).lower()):
                print("Rate limit hit, waiting 25s and retrying once...")
                time.sleep(25)
                continue
            raise

    text = re.sub(r"^```json\s*|\s*```$", "", response.text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)

    id_title = clean_title(data.get("id_title"))
    highlight = str(data.get("highlight") or "").strip(" ,.;:!?\"'")
    if id_title:
        hw = highlight.split()
        if not hw or len(hw) > 4 or highlight.lower() not in id_title.lower():
            words = id_title.split()                      # safe fallback: last 2 words
            highlight = " ".join(words[-2:]) if len(words) > 3 else ""

    person = data.get("person")
    person = person.strip() if isinstance(person, str) and 2 <= len(person.strip()) <= 60 else None

    def _queries(key):
        out = []
        raw = data.get(key)
        if isinstance(raw, list):
            for q in raw:
                if isinstance(q, str) and 2 <= len(q.strip()) <= 60:
                    out.append(q.strip())
        return out[:3]

    return {
        "relevant": bool(data.get("relevant", False)) and bool(id_title),
        "id_title": id_title,
        "highlight": highlight,
        "id_summary": str(data.get("id_summary") or "").strip(),
        "person": person,
        "keywords": [normalize_keyword(k) for k in _queries("keywords")],
        "image_queries": _queries("image_queries"),
        "stock_queries": _queries("stock_queries"),
    }


# ---------------------------------------------------------------------------
# KEYWORD IMAGE LIBRARY
# ---------------------------------------------------------------------------
# Files named lib_<keyword>.png|jpg (repo root) or library/<keyword>.png|jpg
# (also library/<keyword>/*.jpg for several images). Small images (logos) are shown
# as a round badge; large images (photos) are used full-bleed. lib_default.* is the
# last-resort image so every story has a picture.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
ALIASES = {
    "btc": "bitcoin", "eth": "ethereum", "ether": "ethereum", "sol": "solana",
    "bnb chain": "bnb", "binance coin": "bnb", "ripple": "xrp", "usdt": "tether",
    "pump.fun": "pumpfun", "pump fun": "pumpfun", "memecoin": "pumpfun",
    "nft": "opensea", "matic": "polygon", "avax": "avalanche", "ltc": "litecoin",
    "ada": "cardano", "trx": "tron", "pancake": "pancakeswap", "uni": "uniswap",
}


def library_index():
    idx = {}
    lib_dir = os.path.join(BASE_DIR, "library")
    if os.path.isdir(lib_dir):
        for name in sorted(os.listdir(lib_dir)):
            full = os.path.join(lib_dir, name)
            if os.path.isdir(full):
                for f in sorted(os.listdir(full)):
                    if f.lower().endswith(IMG_EXT):
                        idx.setdefault(name.lower(), []).append(os.path.join(full, f))
            elif name.lower().endswith(IMG_EXT):
                kw = re.sub(r"(_\d+)?\.\w+$", "", name).lower()
                idx.setdefault(kw, []).append(full)
    for name in sorted(os.listdir(BASE_DIR)):
        if name.startswith("lib_") and name.lower().endswith(IMG_EXT):
            kw = re.sub(r"(_\d+)?\.\w+$", "", name[4:]).lower()
            idx.setdefault(kw, []).append(os.path.join(BASE_DIR, name))
    return idx


LIBRARY = library_index()


def normalize_keyword(k):
    k = str(k).strip().lower()
    k = ALIASES.get(k, k)
    return k


def library_pick(keywords):
    """Return (kind, path, None) for the first keyword that has an image, else None."""
    from PIL import Image
    for k in keywords:
        files = LIBRARY.get(normalize_keyword(k))
        if not files:
            continue
        path = random.choice(files)
        try:
            with Image.open(path) as im:
                kind = "emblem" if max(im.size) <= 800 else "photo"
        except Exception:
            continue
        print(f"  [IMAGE] library '{normalize_keyword(k)}' ({kind})")
        return kind, path, None
    return None


# ---------------------------------------------------------------------------
# PHOTOS
# ---------------------------------------------------------------------------

def get_entry_image_url(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    for l in entry.get("links", []) or []:
        if l.get("rel") == "enclosure" and str(l.get("type", "")).startswith("image"):
            return l.get("href")
    return None


def download_image(url, min_width=500):
    """Download an image to a temp file. Returns the path or None."""
    from PIL import Image
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        path = tempfile.mktemp(suffix=".img")
        with open(path, "wb") as f:
            f.write(r.content)
        with Image.open(path) as im:
            if im.width < min_width:
                print(f"  image too small ({im.width}px), skipping")
                return None
        return path
    except Exception as e:
        print(f"  image download failed: {e}")
        return None


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SKIP_RE = re.compile(r"\b(logo|icon|flag|map|chart|diagram|screenshot|signature|stamp|"
                     r"coat of arms|collage|montage|poster)s?\b")


def _commons_candidates(query):
    """Search Wikimedia Commons for photos. Returns a list of dicts, best first."""
    r = requests.get(
        COMMONS_API,
        params={"action": "query", "generator": "search", "gsrnamespace": 6,
                "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": 20,
                "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
                "iiurlwidth": 1920, "format": "json"},
        headers=UA, timeout=20)
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda x: x.get("index", 999)):
        info = (p.get("imageinfo") or [{}])[0]
        title = p.get("title", "")
        w, h = info.get("width", 0), info.get("height", 0)
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "").strip()
        if info.get("mime") != "image/jpeg":          # photos are almost always JPEG
            continue
        if w < 900 or h < 600 or not lic:
            continue
        if not (0.5 <= w / h <= 2.3):
            continue
        if SKIP_RE.search(title.lower()):
            continue
        artist = re.sub(r"\s+", " ", clean_html((meta.get("Artist") or {}).get("value", "")))[:60]
        out.append({
            "title": title, "w": w, "h": h, "license": lic,
            "artist": artist or "Wikimedia Commons",
            "url": info.get("thumburl") or info.get("url"),
        })
    return out


_used_commons = set()          # avoid the same Commons photo twice in one run


def find_commons_image(queries, vary=False):
    """Try each query in order. Returns (path, credit) or None.
    vary=True: pick randomly among the best results (used for generic queries)."""
    for q in queries:
        try:
            cands = _commons_candidates(q)
        except Exception as e:
            print(f"  [IMAGE] query '{q}' failed: {e}")
            continue
        if not cands:
            print(f"  [IMAGE] query '{q}' -> no usable photo")
            continue
        cands = [c for c in cands if c["title"] not in _used_commons]
        top = cands[:6] if vary else cands[:3]        # best few, first that downloads
        if vary:
            random.shuffle(top)
        for c in top:
            path = download_image(c["url"])
            if path:
                _used_commons.add(c["title"])
                print(f"  [IMAGE] query '{q}' -> {c['title']} ({c['w']}x{c['h']}, {c['license']})")
                return path, f"Foto: {c['artist']} / Wikimedia Commons ({c['license']})"
    return None


DEFAULT_STOCK = ["bitcoin coin", "cryptocurrency", "digital finance"]
_used_pexels = set()           # avoid the same stock photo twice in one run
_pexels_blocked = False
_pexels_warned = False
DEFAULT_COMMONS = ["Bitcoin physical coin", "Cryptocurrency", "Stock exchange building"]


def find_pexels_image(queries):
    """Free stock photos (https://www.pexels.com/api/). Returns (path, credit) or None."""
    global _pexels_blocked, _pexels_warned
    if not PEXELS_API_KEY:
        if not _pexels_warned:
            print("  [IMAGE] PEXELS_API_KEY not set - Pexels skipped (using Commons only)")
            _pexels_warned = True
        return None
    if _pexels_blocked:
        return None
    for q in queries:
        try:
            r = requests.get("https://api.pexels.com/v1/search",
                             params={"query": q, "per_page": 15, "orientation": "landscape"},
                             headers={"Authorization": PEXELS_API_KEY, **UA}, timeout=20)
            if r.status_code == 429:
                print("  [IMAGE] Pexels rate limit reached - stock photos paused for this run")
                _pexels_blocked = True
                return None
            r.raise_for_status()
            photos = [p for p in r.json().get("photos", [])
                      if p.get("id") not in _used_pexels and p.get("width", 0) >= 1200
                      and (p.get("src") or {}).get("large2x")]
        except Exception as e:
            print(f"  [IMAGE] Pexels query '{q}' failed: {e}")
            continue
        top = photos[:6]
        random.shuffle(top)                       # vary the picks
        for p in top:
            path = download_image(p["src"]["large2x"], min_width=900)
            if path:
                _used_pexels.add(p["id"])
                print(f"  [IMAGE] Pexels '{q}' -> #{p['id']} by {p.get('photographer')}")
                return path, f"Foto: {p.get('photographer', 'Pexels')} / Pexels"
        print(f"  [IMAGE] Pexels '{q}' -> no usable photo")
    return None


def find_photo(entry, cfg, analysis):
    """Every story gets a picture. Returns (kind, path, credit), kind = 'photo' | 'emblem' | None.
    Order: article photo (sources set to 'article') -> portrait of the main person (Commons)
    -> keyword library (logos/photos you control) -> topical Commons photo -> Pexels (if key)
    -> generic Commons photo -> library default -> text-only card."""
    if cfg["photo_source"] == "article":
        url = get_entry_image_url(entry)
        path = download_image(url) if url else None
        if path:
            print("  [IMAGE] using the article's own photo")
            return "photo", path, f"Foto: {cfg['name']}"
        print("  [IMAGE] no usable article photo")

    if analysis["person"]:
        found = find_commons_image([analysis["person"]])
        if found:
            return "photo", found[0], found[1]

    lib = library_pick(analysis["keywords"])
    if lib:
        return lib

    if analysis["image_queries"]:
        found = find_commons_image(analysis["image_queries"])
        if found:
            return "photo", found[0], found[1]
    found = find_pexels_image(analysis["stock_queries"] + DEFAULT_STOCK)
    if found:
        return "photo", found[0], found[1]
    found = find_commons_image(analysis["stock_queries"] + DEFAULT_COMMONS, vary=True)
    if found:
        return "photo", found[0], found[1]

    lib = library_pick(["default"])
    if lib:
        return lib
    print("  [IMAGE] no photo found -> text-only card")
    return None, None, None


# ---------------------------------------------------------------------------
# CARD + DISCORD
# ---------------------------------------------------------------------------

def build_card(entry, source_cfg, analysis):
    """Returns (card_path or None, credit_text or None)."""
    kind, img_path, credit = find_photo(entry, source_cfg, analysis)
    photo_path = img_path if kind == "photo" else None
    emblem_path = img_path if kind == "emblem" else None

    out = tempfile.mktemp(suffix=".png")
    try:
        make_card(analysis["id_title"], analysis["highlight"], out,
                  photo=photo_path, emblem=emblem_path)
        return out, credit
    except Exception as e:
        print(f"  card generation failed ({e}); retrying without photo")
        traceback.print_exc()
        try:
            make_card(analysis["id_title"], analysis["highlight"], out, photo=None)
            return out, None
        except Exception as e2:
            print(f"  text-only card failed too: {e2}")
            traceback.print_exc()
            return None, None


def send_to_discord(entry, source_name, analysis, card_path, credit):
    link = entry.get("link", "")
    footer = f"Sumber berita: {source_name}" + (f"  •  {credit}" if credit else "")
    embed = {
        "description": f"{analysis['id_summary']}\n\n[Baca selengkapnya di {source_name}]({link})",
        "color": EMBED_COLOR,
        "footer": {"text": footer[:2000]},
    }
    if not card_path:                        # last-resort fallback: text embed
        embed["title"] = analysis["id_title"]
        embed["url"] = link
        resp = requests.post(WEBHOOK_URL, json={"embeds": [embed]}, timeout=20)
    else:
        embed["image"] = {"url": "attachment://card.png"}
        with open(card_path, "rb") as f:
            resp = requests.post(
                WEBHOOK_URL,
                data={"payload_json": json.dumps({"embeds": [embed]})},
                files={"files[0]": ("card.png", f, "image/png")},
                timeout=40,
            )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not DRY_RUN and not WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL environment variable is not set.")
        sys.exit(1)
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    miss = missing_assets()
    if miss:
        print(f"WARNING: design files not found in '{ASSETS}': {', '.join(miss)}")
        print("         -> image cards cannot be made, posts fall back to plain text.")
    else:
        print(f"Design files OK ({ASSETS})")

    kws = sorted(k for k in LIBRARY if k != "default")
    print(f"Image library: {len(kws)} keywords ({', '.join(kws) if kws else 'none'}); "
          f"default image: {'yes' if 'default' in LIBRARY else 'NO'}")
    print(f"Pexels key: {'set' if PEXELS_API_KEY else 'not set'}")

    seen_ids = load_seen()
    seen_set = set(seen_ids)

    pending = []                              # (published_ts, entry, source_cfg)
    for cfg in FEEDS:
        feed = feedparser.parse(cfg["url"])
        if feed.bozo and not feed.entries:
            print(f"Failed to parse feed {cfg['name']}: {feed.bozo_exception}")
            continue
        for entry in feed.entries:
            entry_id = entry.get("id", entry.get("link"))
            if entry_id not in seen_set:
                ts = time.mktime(entry.published_parsed) if entry.get("published_parsed") else 0
                pending.append((ts, entry, cfg))

    if not pending:
        print("No new articles across all feeds.")
        return

    pending.sort(key=lambda x: x[0])          # oldest first
    batch = pending[:MAX_PER_RUN]
    print(f"{len(pending)} new article(s); processing {len(batch)} this run.")

    posted = skipped = 0
    consecutive_fail = 0

    for _, entry, cfg in batch:
        entry_id = entry.get("id", entry.get("link"))
        title = entry.get("title", "")
        summary = clean_html(entry.get("summary", ""))

        try:
            analysis = analyze_with_gemini(title, summary)
            consecutive_fail = 0
        except Exception as e:
            consecutive_fail += 1
            print(f"Gemini analysis failed for '{title}': {e}")
            if consecutive_fail >= 3:
                print("3 failures in a row - stopping this run (articles stay unprocessed).")
                break
            continue                          # NOT marked as seen -> retried next run

        if not analysis["relevant"]:
            print(f"[SKIPPED] ({cfg['name']}) {title}")
            skipped += 1
            seen_ids.append(entry_id)
            time.sleep(SLEEP_BETWEEN)
            continue

        card_path, credit = build_card(entry, cfg, analysis)

        if DRY_RUN:
            keep = os.path.join(os.getcwd(), f"dryrun_{posted + 1}.png")
            if card_path:
                os.replace(card_path, keep)
            print(f"[DRY-RUN] ({cfg['name']}) {analysis['id_title']} | credit={credit} | {keep}")
            posted += 1
        else:
            try:
                send_to_discord(entry, cfg["name"], analysis, card_path, credit)
                print(f"[POSTED] ({cfg['name']}) {analysis['id_title']}")
                posted += 1
            except Exception as e:
                print(f"Failed to post to Discord: {e}")
                time.sleep(SLEEP_BETWEEN)
                continue                      # retry next run
            seen_ids.append(entry_id)

        time.sleep(SLEEP_BETWEEN)

    if not DRY_RUN:
        save_seen(seen_ids)
    print(f"Done. Posted: {posted}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
