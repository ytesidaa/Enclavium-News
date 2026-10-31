import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import quote, unquote

import feedparser
import requests

from card import make_card

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# photo_source:
#   "article" -> use the photo from the news article itself (design stays ours)
#   "wiki"    -> ignore the article photo; use a Wikimedia Commons photo of the
#                main person (if any), otherwise a text-only card
FEEDS = [
    {"name": "CryptoSlate", "url": "https://cryptoslate.com/feed/", "photo_source": "wiki"},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "photo_source": "article"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss", "photo_source": "wiki"},
]

STATE_FILE = "last_seen.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
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

def analyze_with_gemini(title, summary):
    """
    Returns dict:
      relevant, id_title, highlight, id_summary, person (English Wikipedia title or None)
    """
    import google.generativeai as genai

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config={"response_mime_type": "application/json", "temperature": 0.3},
    )
    entity_list_str = ", ".join(NOTABLE_ENTITIES)

    prompt = f"""Kamu adalah asisten kurasi berita untuk media crypto Instagram bernama Enclavium.

Tugas kamu:
1. Tentukan apakah artikel ini menyebut tokoh besar, founder proyek besar, VC ternama, atau exchange besar di dunia crypto/AI/finance. Contoh nama yang WAJIB dianggap relevan: {entity_list_str}. Selain daftar ini, kamu boleh memakai pengetahuanmu sendiri untuk mengenali tokoh/entitas besar lain yang relevan di dunia crypto, AI, atau makroekonomi global.
2. Jika relevan, tulis dalam Bahasa Indonesia dengan gaya jurnalistik yang formal, profesional, dan mudah dipahami:
   - "id_title": judul lugas dan faktual, MAKSIMAL 70 karakter (sekitar 9 kata), tanpa tanda seru, tanpa huruf kapital berlebihan, tanpa kata hiperbolik (misalnya melayang, bakal tembus, meledak). Judul ini dicetak besar di gambar.
   - "highlight": 1 sampai 3 kata berurutan dari id_title yang paling penting (angka, nama, atau inti peristiwa), ditulis PERSIS sama seperti di id_title.
   - "id_summary": 2-3 kalimat pendek yang hanya memuat fakta dari artikel (siapa, apa, angka, kapan), tanpa opini, tanpa prediksi, dan tanpa menambah informasi yang tidak ada di artikel. Gunakan istilah crypto umum apa adanya dan jelaskan singkat bila perlu.
   - "person": nama halaman Wikipedia bahasa Inggris (contoh: "Elon Musk") untuk SATU tokoh manusia yang menjadi fokus utama berita. Isi null jika fokusnya perusahaan, aset, regulasi, atau tidak ada tokoh yang jelas.

Artikel:
Judul: {title}
Ringkasan asli: {summary}

Jawab HANYA dengan JSON persis seperti ini:
{{"relevant": true, "id_title": "...", "highlight": "...", "id_summary": "...", "person": "..." atau null}}
Jika tidak relevan: {{"relevant": false, "id_title": "", "highlight": "", "id_summary": "", "person": null}}
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

    id_title = str(data.get("id_title") or "").strip()
    highlight = str(data.get("highlight") or "").strip()
    if id_title and highlight.lower() not in id_title.lower():
        words = id_title.split()
        highlight = " ".join(words[-2:]) if len(words) > 2 else ""   # safe fallback

    person = data.get("person")
    if not isinstance(person, str) or not person.strip() or len(person) > 60:
        person = None

    return {
        "relevant": bool(data.get("relevant", False)) and bool(id_title),
        "id_title": id_title,
        "highlight": highlight,
        "id_summary": str(data.get("id_summary") or "").strip(),
        "person": person.strip() if person else None,
    }


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


def wiki_photo(person):
    """
    Look up the lead photo of an English Wikipedia page, but only if it is hosted
    on Wikimedia Commons (free licence). Returns (path, credit_text) or None.
    """
    try:
        title = quote(person.replace(" ", "_"), safe="")
        r = requests.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
                         headers=UA, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("type") == "disambiguation":
            return None
        src = (data.get("originalimage") or {}).get("source")
        if not src or "/wikipedia/commons/" not in src:
            return None                      # not a free Commons file
        filename = unquote(src.rsplit("/", 1)[-1])

        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "titles": f"File:{filename}", "prop": "imageinfo",
                    "iiprop": "extmetadata", "format": "json"},
            headers=UA, timeout=15)
        pages = r.json().get("query", {}).get("pages", {})
        info = next(iter(pages.values())).get("imageinfo", [{}])[0].get("extmetadata", {})
        lic = (info.get("LicenseShortName") or {}).get("value", "").strip()
        if not lic:
            return None
        artist = clean_html((info.get("Artist") or {}).get("value", ""))
        artist = re.sub(r"\s+", " ", artist)[:60] or "Wikimedia Commons"

        path = download_image(
            f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}?width=1600")
        if not path:
            return None
        return path, f"Foto: {artist} / Wikimedia Commons ({lic})"
    except Exception as e:
        print(f"  wikimedia lookup failed: {e}")
        return None


# ---------------------------------------------------------------------------
# CARD + DISCORD
# ---------------------------------------------------------------------------

def build_card(entry, source_cfg, analysis):
    """Returns (card_path or None, credit_text or None)."""
    photo_path, credit = None, None

    if source_cfg["photo_source"] == "article":
        url = get_entry_image_url(entry)
        if url:
            photo_path = download_image(url)
            if photo_path:
                credit = f"Foto: {source_cfg['name']}"
    elif analysis["person"]:
        found = wiki_photo(analysis["person"])
        if found:
            photo_path, credit = found

    out = tempfile.mktemp(suffix=".png")
    try:
        make_card(analysis["id_title"], analysis["highlight"], out, photo=photo_path)
        return out, credit
    except Exception as e:
        print(f"  card generation failed ({e}); retrying without photo")
        try:
            make_card(analysis["id_title"], analysis["highlight"], out, photo=None)
            return out, None
        except Exception as e2:
            print(f"  text-only card failed too: {e2}")
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
