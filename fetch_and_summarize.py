"""
Haberleri çeker, Claude Haiku 5.5 ile kısa özetler, news.json'a yazar.
GitHub Actions tarafından çalıştırılır (sunucu tarafında, proxy gerekmez).
"""
import json, os, re, sys, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = "openai/gpt-oss-120b"

TR = {"hl": "tr", "gl": "TR", "ceid": "TR:tr"}
EN = {"hl": "en-US", "gl": "US", "ceid": "US:en"}

CATEGORIES = [
    {"id": "teknoloji", "label": "Teknoloji", "query": "teknoloji", "edition": TR},
    {"id": "bilim", "label": "Bilim", "query": "bilim", "edition": TR},
    {"id": "sanat", "label": "Sanat", "query": "sanat", "edition": TR},
    {"id": "oyun", "label": "Ücretsiz Oyun", "query": "ücretsiz oyun dağıtıyor", "edition": TR},
    {"id": "doga", "label": "Doğa", "query": "doğa çevre", "edition": TR},
    {"id": "dunya", "label": "Dünya", "query": None, "edition": EN},
    {"id": "gundem", "label": "Gündem", "query": None, "edition": TR},
    {"id": "gundem", "label": "Gündem", "query": "Çorlu Tekirdağ", "edition": TR},  # Çorlu haberleri, ayrı sekme açmadan Gündem'e karışıyor
]

MAX_AGE_DAYS = 6 / 24  # sadece son 6 saatlik haberler; kategori tamamen boş kalmasın diye MIN_ITEMS_PER_CAT yedeği devrede
MIN_ITEMS_PER_CAT = 3
MAX_ITEMS_PER_CAT = 12


def build_url(cat):
    ed = cat["edition"]
    if cat["query"]:
        return f"https://news.google.com/rss/search?q={quote(cat['query'])}&hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}"
    return f"https://news.google.com/rss?hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}"


def age_days(pub_date):
    try:
        d = parsedate_to_datetime(pub_date)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400
    except Exception:
        return 999


MEDIA_CONTENT = "{http://search.yahoo.com/mrss/}content"
MEDIA_THUMBNAIL = "{http://search.yahoo.com/mrss/}thumbnail"


def extract_image(it):
    media = it.find(MEDIA_CONTENT)
    if media is None:
        media = it.find(MEDIA_THUMBNAIL)
    if media is not None and media.get("url"):
        return media.get("url")
    enclosure = it.find("enclosure")
    if enclosure is not None and enclosure.get("url"):
        return enclosure.get("url")
    desc = it.findtext("description") or ""
    m = re.search(r'<img[^>]+src="([^"]+)"', desc)
    if m:
        return m.group(1)
    return None


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
        if not link:
            continue
        items.append({
            "cat": cat["label"], "catId": cat["id"],
            "title": title, "source": source,
            "link": link, "pubDate": pub_date,
            "image": extract_image(it),
        })

    seen, deduped = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        deduped.append(it)

    deduped.sort(key=lambda i: age_days(i["pubDate"]))
    fresh = [i for i in deduped if age_days(i["pubDate"]) <= MAX_AGE_DAYS]
    final = fresh if len(fresh) >= MIN_ITEMS_PER_CAT else deduped[:max(MIN_ITEMS_PER_CAT, len(fresh))]
    return final[:MAX_ITEMS_PER_CAT]


WEATHER_DESC = {
    0: "Açık", 1: "Az bulutlu", 2: "Parçalı bulutlu", 3: "Kapalı",
    45: "Sisli", 48: "Kırağı sisi",
    51: "Hafif çisenti", 53: "Çisenti", 55: "Yoğun çisenti",
    61: "Hafif yağmur", 63: "Yağmur", 65: "Şiddetli yağmur",
    71: "Hafif kar", 73: "Kar", 75: "Yoğun kar",
    80: "Sağanak", 81: "Sağanak", 82: "Şiddetli sağanak",
    95: "Gök gürültülü sağanak", 96: "Dolu", 99: "Şiddetli dolu",
}

# Çorlu, Tekirdağ koordinatları — başka bir şehir istersen burayı değiştir
WEATHER_LAT, WEATHER_LON = 41.1601, 27.8055


def fetch_weather():
    url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&current_weather=true"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        cw = r.json().get("current_weather", {})
        code = cw.get("weathercode")
        return {
            "temp": cw.get("temperature"),
            "desc": WEATHER_DESC.get(code, ""),
        }
    except Exception as e:
        print(f"  ! hava durumu alınamadı: {e}", file=sys.stderr)
        return None


def fetch_market():
    usdtry = eurtry = gold_usd_oz = None
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        usdtry = rates.get("TRY")
        usd_eur = rates.get("EUR")
        if usdtry and usd_eur:
            eurtry = usdtry / usd_eur
    except Exception as e:
        print(f"  ! döviz verisi alınamadı: {e}", file=sys.stderr)

    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=15)
        r.raise_for_status()
        gold_usd_oz = r.json().get("price")
    except Exception as e:
        print(f"  ! altın verisi alınamadı: {e}", file=sys.stderr)

    gold_gram_try = round(gold_usd_oz / 31.1034768 * usdtry, 2) if (usdtry and gold_usd_oz) else None
    return {
        "usdtry": round(usdtry, 2) if usdtry else None,
        "eurtry": round(eurtry, 2) if eurtry else None,
        "gold_gram_try": gold_gram_try,
    }


STOCKS = [
    ("XU100.IS", "BIST 100"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
    ("THYAO.IS", "THY"),
    ("ASELS.IS", "Aselsan"),
    ("GARAN.IS", "Garanti BBVA"),
    ("BIMAS.IS", "BİM"),
    ("AAPL", "Apple"),
    ("MSFT", "Microsoft"),
    ("NVDA", "Nvidia"),
]


def fetch_stocks():
    results = []
    for symbol, label in STOCKS:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            )
            r.raise_for_status()
            result = r.json()["chart"]["result"]
            if not result:
                raise ValueError("sonuç boş")
            meta = result[0]["meta"]
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            # Piyasa kapalıyken regularMarketPrice boş gelebilir; o zaman son kapanışı göster
            price = meta.get("regularMarketPrice") or prev
            if price is None:
                raise ValueError("fiyat yok")
            change_pct = round((price - prev) / prev * 100, 2) if (prev and price != prev) else (0.0 if prev else None)
            results.append({"label": label, "price": round(price, 2), "change_pct": change_pct})
        except Exception as e:
            print(f"  ! {label} alınamadı: {e}", file=sys.stderr)
    return results


IMAGE_META_PATTERNS = [
    r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image(?::secure_url)?["\']',
    r'<meta[^>]+(?:property|name)=["\']twitter:image(?::src)?["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']twitter:image(?::src)?["\']',
]


def fetch_og_image(url):
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10, allow_redirects=True,
        )
        if not r.ok:
            print(f"  ! resim alınamadı ({url[:70]}): status {r.status_code}", file=sys.stderr)
            return None
        html = r.text[:400000]  # sayfanın tamamını işlemeye gerek yok, <head> zaten baştadır
        for pattern in IMAGE_META_PATTERNS:
            m = re.search(pattern, html, re.I)
            if m:
                return m.group(1)
        print(f"  ! resim bulunamadı ({url[:70]})", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  ! resim alınamadı ({url[:70]}): {e}", file=sys.stderr)
        return None


def summarize_batch(items):
    if not items:
        return []
    lines = [f"{i+1}. {it['title']} (Kaynak: {it['source']})" for i, it in enumerate(items)]
    prompt = (
        "Aşağıda haber başlıkları listelendi. Her biri için Türkçe, 2-3 CÜMLELİK bir özet yaz: "
        "ilk cümle(ler) haberin ne hakkında olduğunu başlığı tekrar ETMEDEN açıklasın, "
        "son cümle neden önemli olabileceğine veya daha geniş bağlamına dair kısa bir "
        "değerlendirme/yorum katsın (aşırı iddialı olma, spekülasyonu 'olabilir', 'muhtemelen' "
        "gibi ifadelerle yumuşat). Haberin tam içeriğini bilmiyorsan başlıktan makul bir çıkarım "
        "yap, somut sayı/isim/olay uydurma. "
        "SADECE bir JSON dizisi döndür, başka hiçbir açıklama yazma. "
        "Dizinin uzunluğu tam olarak liste uzunluğuyla eşleşmeli.\n\n"
        + "\n".join(lines)
    )
    arr = []
    for attempt in range(4):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            if resp.status_code == 429:
                wait = min(int(resp.headers.get("Retry-After", 10 * (attempt + 1))), 30)
                print(f"  ! Groq 429 (çok fazla istek), {wait} sn bekleyip tekrar denenecek...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            match = re.search(r"\[.*\]", text, re.S)
            arr = json.loads(match.group(0)) if match else []
            break
        except Exception as e:
            print(f"  ! özetleme hatası: {e}", file=sys.stderr)
            break
    arr = (arr + [""] * len(items))[: len(items)]
    return arr


def load_cache():
    """Önceki news.json'dan link -> {summary, image} belleği yükler, böylece aynı haber tekrar tekrar
    özetlenmez / resmi tekrar tekrar aranmaz. Dosya yoksa (ilk çalıştırma) boş döner."""
    cache = {}
    try:
        with open("news.json", "r", encoding="utf-8") as f:
            old = json.load(f)
        for it in old.get("items", []):
            if it.get("link"):
                cache[it["link"]] = {"summary": it.get("summary", ""), "image": it.get("image")}
    except Exception:
        pass
    return cache


NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()


def send_notification(new_count):
    if not NTFY_TOPIC or new_count <= 0:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=f"{new_count} yeni haber eklendi".encode("utf-8"),
            headers={"Title": "Haber Panosu", "Tags": "newspaper"},
            timeout=10,
        )
    except Exception as e:
        print(f"  ! bildirim gönderilemedi: {e}", file=sys.stderr)


def main():
    cache = load_cache()

    all_items = []
    for cat in CATEGORIES:
        print(f"Çekiliyor: {cat['label']}" + (f" ({cat['query']})" if cat['query'] else ""))
        all_items.extend(fetch_category(cat))

    seen_links = set()
    deduped_all = []
    for it in all_items:
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        deduped_all.append(it)
    all_items = deduped_all

    # Özet ve resim ayrı ayrı önbellekten dolduruluyor; biri daha önce işlendi diye diğeri atlanmıyor
    need_summary = []
    need_image = []
    for it in all_items:
        cached = cache.get(it["link"])
        if cached and cached.get("summary"):
            it["summary"] = cached["summary"]
        else:
            need_summary.append(it)

        if not it.get("image"):
            if cached and cached.get("image"):
                it["image"] = cached["image"]
            else:
                need_image.append(it)

    print(f"Toplam {len(all_items)} haber — {len(need_summary)} tanesi yeni özetlenecek, {len(need_image)} tanesi için resim aranacak.")

    print("Resim aranıyor...")
    for it in need_image:
        it["image"] = fetch_og_image(it["link"])

    print("Özetleniyor...")
    CHUNK = 20
    for i in range(0, len(need_summary), CHUNK):
        chunk = need_summary[i : i + CHUNK]
        summaries = summarize_batch(chunk)
        for it, s in zip(chunk, summaries):
            it["summary"] = s
        if i + CHUNK < len(need_summary):
            time.sleep(3)

    print("Hava durumu ve piyasa verisi çekiliyor...")
    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "items": all_items,
        "weather": fetch_weather(),
        "market": fetch_market(),
        "stocks": fetch_stocks(),
    }
    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("news.json yazıldı.")
    send_notification(len(need_summary))


if __name__ == "__main__":
    main()
