"""
RADAR - Haber Motoru, Piyasa Takipçisi ve AI Özetleyici
"""
import json, os, re, sys, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET
import concurrent.futures

import requests
import trafilatura

# --- YAPILANDIRMA ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    print("HATA: GROQ_API_KEY bulunamadı!", file=sys.stderr)
    sys.exit(1)

GROQ_MODEL = "openai/gpt-oss-120b"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

TR = {"hl": "tr", "gl": "TR", "ceid": "TR:tr"}
EN = {"hl": "en-US", "gl": "US", "ceid": "US:en"}

CATEGORIES = [
    {"id": "teknoloji", "label": "Teknoloji", "query": "teknoloji yapay zeka", "edition": TR},
    {"id": "bilim", "label": "Bilim", "query": "bilim uzay keşif", "edition": TR},
    {"id": "sanat", "label": "Sanat", "query": "sanat sinema müzik", "edition": TR},
    {"id": "oyun", "label": "Ücretsiz Oyun", "query": "ücretsiz oyun dağıtıyor", "edition": TR},
    {"id": "doga", "label": "Doğa", "query": "doğa çevre hayvanlar", "edition": TR},
    {"id": "dunya", "label": "Dünya", "query": None, "edition": EN},
    {"id": "gundem", "label": "Gündem", "query": "türkiye gündem", "edition": TR},
    {"id": "corlu", "label": "Çorlu", "query": "Çorlu Tekirdağ", "edition": TR},
]

MAX_AGE_DAYS = 6 / 24
MIN_ITEMS_PER_CAT = 3
MAX_ITEMS_PER_CAT = 12
MAX_TEXT_LENGTH = 3500

# --- HABER ÇEKME ---
def build_url(cat):
    ed = cat["edition"]
    if cat["query"]:
        return f"https://news.google.com/rss/search?q={quote(cat['query'])}&hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}"
    return f"https://news.google.com/rss?hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}"

def age_days(pub_date):
    try:
        d = parsedate_to_datetime(pub_date)
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400
    except: return 999

def extract_image(it):
    media = it.find("{http://search.yahoo.com/mrss/}content")
    if media is None: media = it.find("{http://search.yahoo.com/mrss/}thumbnail")
    if media is not None and media.get("url"): return media.get("url")
    enclosure = it.find("enclosure")
    if enclosure is not None and enclosure.get("url"): return enclosure.get("url")
    desc = it.findtext("description") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc, re.I)
    return m.group(1) if m else None

def fetch_category(cat):
    url = build_url(cat)
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        res.raise_for_status()
        root = ET.fromstring(res.content)
    except Exception as e:
        print(f"  ! {cat['label']} alınamadı: {e}", file=sys.stderr)
        return []

    items = []
    for it in root.findall(".//item"):
        title_raw = (it.findtext("title") or "").strip()
        source_el = it.find("source")
        source = source_el.text.strip() if source_el is not None and source_el.text else ""
        title = title_raw.replace(f" - {source}", "") if source else title_raw
        link = (it.findtext("link") or "").strip()
        pub_date = (it.findtext("pubDate") or "").strip()
        if not link: continue
        items.append({
            "cat": cat["label"], "catId": cat["id"],
            "title": title, "source": source,
            "link": link, "pubDate": pub_date,
            "image": extract_image(it),
        })

    seen, deduped = set(), []
    for it in items:
        if it["link"] in seen: continue
        seen.add(it["link"])
        deduped.append(it)

    deduped.sort(key=lambda i: age_days(i["pubDate"]))
    fresh = [i for i in deduped if age_days(i["pubDate"]) <= MAX_AGE_DAYS]
    final = fresh if len(fresh) >= MIN_ITEMS_PER_CAT else deduped[:max(MIN_ITEMS_PER_CAT, len(fresh))]
    return final[:MAX_ITEMS_PER_CAT]

# --- HAVA DURUMU, PİYASA VE KRİPTO ---
WEATHER_DESC = {0:"Açık",1:"Az bulutlu",2:"Parçalı bulutlu",3:"Kapalı",45:"Sisli",48:"Kırağı sisi",51:"Hafif çisenti",53:"Çisenti",55:"Yoğun çisenti",61:"Hafif yağmur",63:"Yağmur",65:"Şiddetli yağmur",71:"Hafif kar",73:"Kar",75:"Yoğun kar",80:"Sağanak",81:"Sağanak",82:"Şiddetli sağanak",95:"Gök gürültülü sağanak",96:"Dolu",99:"Şiddetli dolu"}
WEATHER_LAT, WEATHER_LON = 41.1601, 27.8055

def fetch_weather():
    url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&current_weather=true"
    try:
        r = requests.get(url, timeout=15); r.raise_for_status()
        cw = r.json().get("current_weather", {})
        return {"temp": cw.get("temperature"), "desc": WEATHER_DESC.get(cw.get("weathercode"), "")}
    except: return None

def fetch_market():
    usdtry = eurtry = gold_usd_oz = None
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        rates = r.json().get("rates", {})
        usdtry = rates.get("TRY")
        if usdtry and rates.get("EUR"): eurtry = usdtry / rates.get("EUR")
    except: pass
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
        gold_usd_oz = r.json().get("price")
    except: pass
    
    gold_gram_try = round(gold_usd_oz / 31.1034768 * usdtry, 2) if (usdtry and gold_usd_oz) else None
    ceyrek_altin = round(gold_gram_try * 1.75, 2) if gold_gram_try else None
    
    return {
        "usdtry": round(usdtry, 2) if usdtry else None, 
        "eurtry": round(eurtry, 2) if eurtry else None, 
        "gold_gram_try": gold_gram_try,
        "ceyrek_altin_try": ceyrek_altin
    }

def fetch_crypto():
    cryptos = []
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=try&include_24hr_change=true", timeout=15)
        r.raise_for_status()
        data = r.json()
        if "bitcoin" in data:
            cryptos.append({"label": "Bitcoin", "price": data["bitcoin"]["try"], "change_pct": data["bitcoin"].get("try_24h_change", 0)})
        if "ethereum" in data:
            cryptos.append({"label": "Ethereum", "price": data["ethereum"]["try"], "change_pct": data["ethereum"].get("try_24h_change", 0)})
    except Exception as e:
        print(f"  ! Kripto alınamadı: {e}", file=sys.stderr)
    return cryptos

STOCKS = [
    ("XU100.IS", "BIST 100"), ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"),
    ("THYAO.IS", "THY"), ("ASELS.IS", "Aselsan"), ("GARAN.IS", "Garanti BBVA"),
    ("BIMAS.IS", "BİM"), ("EREGL.IS", "Ereğli Demir Çelik"), ("SISE.IS", "Şişecam"),
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "Nvidia"),
    ("TSLA", "Tesla"), ("META", "Meta"), ("GOOGL", "Alphabet")
]

def fetch_stocks():
    results = []
    for symbol, label in STOCKS:
        try:
            r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            meta = r.json()["chart"]["result"][0]["meta"]
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            price = meta.get("regularMarketPrice") or prev
            change_pct = round((price - prev) / prev * 100, 2) if (prev and price != prev) else (0.0 if prev else None)
            results.append({"label": label, "price": round(price, 2), "change_pct": change_pct})
        except: pass
    return results

# --- METİN ÇEKME VE GROQ (HABER ÖZETLEME) ---
def fetch_article_text(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded: return ""
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return text[:MAX_TEXT_LENGTH] if text else ""
    except: return ""

def summarize_batch(items):
    if not items: return []
    lines = []
    for i, it in enumerate(items):
        content = it.get("text") if it.get("text") else it['title']
        lines.append(f"--- HABER {i+1} ---\nBaşlık: {it['title']}\nKaynak: {it['source']}\nİçerik: {content}\n")
    
    prompt = (
        "Aşağıdaki haberleri Türkçe, 2-3 cümlelik, tarafsız ve anlaşılır şekilde özetle. "
        "Eğer 'İçerik' kısmı sadece başlıktan oluşuyorsa, 'Tam metne ulaşılamadı' uyarısı ekleyerek başlıktan çıkarım yap. "
        "Haberlerde olmayan bilgileri KESİNLİKLE uydurma. Önemli isim, tarih ve rakamları koru. "
        "Çıktıyı tam olarak şu JSON formatında ver: "
        '{"summaries": ["özet 1", "özet 2", ...]} '
        "Başka hiçbir şey yazma.\n\n" + "\n".join(lines)
    )

    for attempt in range(3):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "max_tokens": 3000, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}]},
                timeout=60
            )
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", 10 * (attempt + 1))), 30)
                time.sleep(wait); continue
            resp.raise_for_status()
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
            arr = data.get("summaries", [])
            if len(arr) == len(items): return arr
            else: return (arr + ["Özet oluşturulamadı."] * len(items))[:len(items)]
        except Exception as e:
            print(f"  ! Groq hatası: {e}", file=sys.stderr)
            break
    return [""] * len(items)

# --- YENİ: GÜNÜN ÖZETİ (RADAR AI BRIEFING) ---
def generate_daily_briefing(all_news):
    """En güncel 10 haberi alır ve Groq ile tek bir akıcı 'Günün Özeti' paragrafı yazar."""
    if not all_news: return "Bugün için özetlenecek önemli bir gelişme bulunamadı."
    
    # En son eklenen 10 haberi al
    top_news = all_news[:10]
    haber_metni = "\n".join([f"- {h['title']}: {h.get('summary', '')}" for h in top_news])
    
    prompt = (
        "Sen RADAR adında bir yapay zeka haber asistanısın. Aşağıdaki bugünün öne çıkan haberlerini okuyup, "
        "kullanıcıya 'Merhaba, bugünün öne çıkan gelişmeleri şunlar:' diyerek başlayan, 3-4 cümlelik akıcı, "
        "tarafsız ve profesyonel bir 'Günün Özeti' metni yaz. Önemli detayları (şirket isimleri, ülke, rakam) koru. "
        "Sadece özet metnini döndür, başka bir şey yazma.\n\nHaberler:\n" + haber_metni
    )
    
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]},
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  ! Briefing (Günün Özeti) hatası: {e}", file=sys.stderr)
        return "Günün özeti şu an oluşturulamıyor, lütfen aşağıdaki haberlere göz atın."

# --- CACHE VE BİLDİRİM ---
def load_cache():
    cache = {}
    try:
        with open("news.json", "r", encoding="utf-8") as f:
            old = json.load(f)
            for it in old.get("items", []):
                if it.get("link"): cache[it["link"]] = {"summary": it.get("summary", ""), "image": it.get("image")}
    except: pass
    return cache

def send_notification(new_count):
    if not NTFY_TOPIC or new_count <= 0: return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=f"RADAR: {new_count} yeni haber eklendi".encode("utf-8"), headers={"Title": "RADAR Güncel", "Tags": "newspaper"}, timeout=10)
    except: pass

# --- ANA AKIŞ ---
def main():
    cache = load_cache()
    all_items = []
    
    print("Kategoriler çekiliyor...")
    for cat in CATEGORIES:
        print(f"  -> {cat['label']}")
        all_items.extend(fetch_category(cat))

    seen_links = set()
    deduped_all = []
    for it in all_items:
        if it["link"] in seen_links: continue
        seen_links.add(it["link"])
        deduped_all.append(it)
    all_items = deduped_all

    need_summary = []
    for it in all_items:
        cached = cache.get(it["link"])
        if cached and cached.get("summary"):
            it["summary"] = cached["summary"]
        else:
            need_summary.append(it)
        if not it.get("image") and cached and cached.get("image"):
            it["image"] = cached["image"]

    print(f"Toplam {len(all_items)} haber. {len(need_summary)} yeni özetlenecek.")

    if need_summary:
        print("Tam metinler çekiliyor (Paralel)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            texts = list(executor.map(lambda i: fetch_article_text(i["link"]), need_summary))
            for it, txt in zip(need_summary, texts): it["text"] = txt

        print("Groq ile haberler özetleniyor...")
        CHUNK_SIZE = 3
        for i in range(0, len(need_summary), CHUNK_SIZE):
            chunk = need_summary[i : i + CHUNK_SIZE]
            summaries = summarize_batch(chunk)
            for it, s in zip(chunk, summaries): it["summary"] = s
            if i + CHUNK_SIZE < len(need_summary): time.sleep(2)

    print("Hava durumu, piyasa ve kripto çekiliyor...")
    print("Günün Özeti (RADAR AI) hazırlanıyor...")
    gunun_ozeti = generate_daily_briefing(all_items)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "briefing": gunun_ozeti, # YENİ: AI Özeti
        "items": all_items,
        "weather": fetch_weather(),
        "market": fetch_market(),
        "crypto": fetch_crypto(),
        "stocks": fetch_stocks(),
    }
    
    tmp_file = "news.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, "news.json")
    
    print("news.json başarıyla yazıldı.")
    send_notification(len(need_summary))

if __name__ == "__main__":
    main()