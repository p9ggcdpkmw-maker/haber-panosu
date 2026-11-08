"""
Haberleri çeker, Claude Haiku 5.5 ile kısa özetler, news.json'a yazar.
GitHub Actions tarafından çalıştırılır (sunucu tarafında, proxy gerekmez).
"""
import json, os, re, sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = "llama-3.3-70b-versatile"

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
]

MAX_AGE_DAYS = 5
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


def summarize_batch(items):
    if not items:
        return []
    lines = [f"{i+1}. {it['title']} (Kaynak: {it['source']})" for i, it in enumerate(items)]
    prompt = (
        "Aşağıda haber başlıkları listelendi. Her biri için, haberin ne hakkında olduğunu "
        "açıklayan, başlığı tekrar ETMEYEN, Türkçe, TEK CÜMLELİK kısa bir özet yaz. "
        "Haberin içeriğini bilmiyorsan başlıktan makul bir çıkarım yap, uydurma ayrıntı ekleme. "
        "SADECE bir JSON dizisi döndür, başka hiçbir açıklama yazma. "
        "Dizinin uzunluğu tam olarak liste uzunluğuyla eşleşmeli.\n\n"
        + "\n".join(lines)
    )
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        match = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(match.group(0)) if match else []
    except Exception as e:
        print(f"  ! özetleme hatası: {e}", file=sys.stderr)
        arr = []
    arr = (arr + [""] * len(items))[: len(items)]
    return arr


def main():
    all_items = []
    for cat in CATEGORIES:
        print(f"Çekiliyor: {cat['label']}")
        all_items.extend(fetch_category(cat))

    print(f"Toplam {len(all_items)} haber, özetleniyor...")
    CHUNK = 20
    for i in range(0, len(all_items), CHUNK):
        chunk = all_items[i : i + CHUNK]
        summaries = summarize_batch(chunk)
        for it, s in zip(chunk, summaries):
            it["summary"] = s

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "items": all_items,
    }
    with open("news.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("news.json yazıldı.")


if __name__ == "__main__":
    main()
