import os, re, json, math, time
from datetime import datetime
from dateutil import tz

import requests
from bs4 import BeautifulSoup

# Optional integrations
USE_SHEETS = os.getenv("USE_SHEETS", "1") == "1"

# --- Helpers (normalize / similarity) ---
STOPWORDS = set("""
de da do das dos e a o os as para por com sem em no na nos nas um uma umas uns
kit jogo conjunto original novo nova promoção oferta relâmpago frete grátis
""".split())

def normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def top_keywords(title: str, k: int = 6) -> str:
    words = [w for w in normalize_text(title).split() if w not in STOPWORDS and len(w) > 2]
    words = words[:k]
    return "-".join(words) if words else normalize_text(title)[:40]

def similarity_key(category: str, title: str) -> str:
    return f"{category}:{top_keywords(title)}"

# --- Shopee (best-effort) search scraper ---
# NOTE: Shopee HTML can change; this is a pragmatic beta approach.
def shopee_search(query: str, limit: int = 60):
    """
    Returns list of dict with:
    title, url, price, sold, rating, reviews
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }

    # Shopee search page
    url = f"https://shopee.com.br/search?keyword={requests.utils.quote(query)}"
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    # Best-effort extraction: tries to find product cards and parse visible text.
    # If this yields too few items, you can switch to an affiliate feed later (recommended for production).
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/product/" in href and "shopee.com.br" not in href:
            full = "https://shopee.com.br" + href.split("?")[0]
            text = normalize_text(a.get_text(" ", strip=True))
            if len(text) < 20:
                continue
            items.append({"url": full, "raw": text})

    # Deduplicate urls and then fetch details per url (limited)
    seen = set()
    urls = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        urls.append(it["url"])
        if len(urls) >= limit:
            break

    out = []
    for u in urls:
        try:
            out.append(shopee_product_details(u, headers=headers))
        except Exception:
            continue
        time.sleep(0.6)  # gentle
    return [x for x in out if x]

def parse_int_like(s: str):
    # handles "10 mil", "78,7 mil", "1,2k" etc (best effort)
    s = s.lower().replace(".", "").replace(" ", "")
    s = s.replace(",", ".")
    mult = 1
    if "mil" in s:
        mult = 1000
        s = s.replace("mil", "")
    if "k" in s:
        mult = 1000
        s = s.replace("k", "")
    try:
        return int(float(s) * mult)
    except:
        return None

def shopee_product_details(url: str, headers: dict):
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    title = soup.find("title").get_text(strip=True) if soup.find("title") else ""
    title = re.sub(r"\s*\|\s*Shopee.*$", "", title).strip()

    page_text = soup.get_text(" ", strip=True).lower()

    # Best-effort sold detection (Portuguese patterns)
    sold = None
    for m in re.finditer(r"(\d+[.,]?\d*)\s*(mil|k)?\s*vendid", page_text):
        sold = parse_int_like(m.group(0).replace("vendidos", "").replace("vendido", ""))
        if sold:
            break

    # Best-effort rating detection
    rating = None
    m = re.search(r"(\d\.\d)\s*de\s*5", page_text)
    if m:
        try:
            rating = float(m.group(1))
        except:
            rating = None

    # Best-effort reviews detection
    reviews = None
    for m in re.finditer(r"(\d+[.,]?\d*)\s*(mil|k)?\s*avalia", page_text):
        reviews = parse_int_like(m.group(0).replace("avaliações", "").replace("avaliação", ""))
        if reviews:
            break

    # Best-effort price detection (R$)
    price = None
    m = re.search(r"r\$\s*([\d\.]+[,]\d{2})", page_text)
    if m:
        price = "R$ " + m.group(1).replace(".", "")

    return {
        "title": title or "Produto Shopee",
        "url": url,
        "price": price or "R$ --",
        "sold": sold,
        "rating": rating,
        "reviews": reviews
    }

# --- AI Copy (OpenAI) ---
def generate_copy(product: dict):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    title = product["title"]
    price = product.get("price") or "R$ --"
    sold = product.get("sold")
    rating = product.get("rating")
    reviews = product.get("reviews")

    sold_str = f"{sold} vendidos" if sold is not None else "muitos vendidos"
    rating_str = f"{rating}" if rating is not None else "alta"
    reviews_str = f"{reviews}" if reviews is not None else ""

    sys = (
        "Você é um redator especialista em conteúdo de achadinhos da Shopee. "
        "Você NÃO inventa números. Se algum dado não existir, você omite ou fala de forma neutra."
    )

    user = f"""
Crie conteúdo em PT-BR, no formato exato abaixo, para UM produto.

DADOS REAIS:
- Produto: {title}
- Preço: {price}
- Vendidos: {sold_str}
- Avaliação: {rating_str}
- Avaliações (qtd): {reviews_str}
- Link: {product["url"]}

FORMATO (obrigatório):

TÍTULO - <título em CAIXA ALTA e curto>

CTA BOTÃO STORY - “<CTA 1>” ou “<CTA 2>”

LEGENDA POST - <texto completo no estilo achadinho, com:
- 1 frase de abertura contextual
- 1 parágrafo curto explicando uso
- lista "✨ Destaques do produto:" com 5 a 7 bullets com emojis
- linha do preço (💰)
- linha de avaliação (⭐) incluindo vendidos quando disponível
- 1 linha de frete/cupom SOMENTE se houver dado (se não houver, não invente)
- fechamento humano + "📲 Link nos stories ou no grupo do WhatsApp!">

REGRAS:
- Não use palavras tipo "imperdível" sem contexto.
- Não invente frete grátis/cupom/oferta relâmpago se não houver dado.
- Seja direto, prático e vendedor sem exagero.
""".strip()

    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role":"system","content":sys},{"role":"user","content":user}],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()

# --- Google Sheets writer ---
def append_to_sheet(rows):
    import gspread
    from google.oauth2.service_account import Credentials

    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_info = json.loads(creds_json)

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)

    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    sh = gc.open_by_key(sheet_id)
    ws = sh.sheet1

    # Ensure header exists
    header = ["Dia", "Produto", "Link", "Preço", "Vendidos", "Avaliação", "CTAs", "Conteúdo Completo", "Categoria", "SimilarityKey", "CriadoEm"]
    if ws.row_count < 1 or ws.acell("A1").value != "Dia":
        ws.insert_row(header, 1)

    for r in rows:
        ws.append_row(r, value_input_option="RAW")

def load_state(path="state.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state, path="state.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def main():
    # Config
    tz_br = tz.gettz("America/Sao_Paulo")
    now = datetime.now(tz_br)
    created_at = now.strftime("%Y-%m-%d %H:%M:%S")

    total_days = int(os.getenv("TOTAL_DAYS", "30"))
    per_day = int(os.getenv("PER_DAY", "3"))

    categories = os.getenv("CATEGORIES", "Ofertas gerais,Moda,Beleza,Eletrônicos,Casa e Decoração,Pets").split(",")
    sold_min = int(os.getenv("SOLD_MIN", "100"))
    rating_min = float(os.getenv("RATING_MIN", "4.5"))

    state = load_state()
    day_index = int(state.get("day_index", 0)) + 1

    if day_index > total_days:
        print("✅ Projeto finalizado. Já completou os 30 dias.")
        return

    used_ids = set(state.get("used_product_ids", []))
    used_keys = set(state.get("used_similarity_keys", []))

    picks = []
    attempts = 0

    # Search queries per category (simple)
    # You can refine queries later (e.g. add "kit", "utilidades", etc.)
    for cat in categories:
        if len(picks) >= per_day:
            break
        q = f"{cat} shopee"
        results = shopee_search(q, limit=50)

        # Filter hard + make candidates
        candidates = []
        for p in results:
            if not p.get("sold") or p["sold"] < sold_min:
                continue
            if p.get("rating") is not None and p["rating"] < rating_min:
                continue
            pid = p["url"]  # using url as id in beta
            if pid in used_ids:
                continue
            sk = similarity_key(cat, p["title"])
            # global anti-repeat by similarity
            if sk in used_keys:
                continue
            p["category"] = cat
            p["product_id"] = pid
            p["similarity_key"] = sk
            candidates.append(p)

        # Sort by sold desc (master rule)
        candidates.sort(key=lambda x: x.get("sold", 0), reverse=True)

        for cand in candidates:
            if len(picks) >= per_day:
                break
            # ensure not too similar within the same day
            if any(c["similarity_key"] == cand["similarity_key"] for c in picks):
                continue
            picks.append(cand)

    if len(picks) < per_day:
        raise RuntimeError(f"Não consegui achar {per_day} produtos válidos hoje. Achou {len(picks)}. Ajuste categorias ou filtros.")

    rows_to_save = []
    outputs = []

    for idx, p in enumerate(picks, start=1):
        content = generate_copy(p)
        outputs.append((p, content))

        # Extract CTA line if present (optional)
        ctas = ""
        m = re.search(r"CTA BOTÃO STORY\s*-\s*([^\n]+)", content, re.IGNORECASE)
        if m:
            ctas = m.group(1).strip()

        rows_to_save.append([
            day_index,
            p["title"],
            p["url"],
            p.get("price",""),
            p.get("sold",""),
            p.get("rating",""),
            ctas,
            content,
            p["category"],
            p["similarity_key"],
            created_at
        ])

        # Update state (global non-repeat)
        used_ids.add(p["product_id"])
        used_keys.add(p["similarity_key"])

    # Save to Sheets or CSV fallback
    if USE_SHEETS:
        append_to_sheet(rows_to_save)
        print("✅ Enviado para Google Sheets.")
    else:
        os.makedirs("output", exist_ok=True)
        outpath = f"output/dia_{day_index:02d}.txt"
        with open(outpath, "w", encoding="utf-8") as f:
            for p, content in outputs:
                f.write(content + "\n\n" + p["url"] + "\n\n" + ("-"*40) + "\n\n")
        print(f"✅ Salvo em {outpath}")

    # Persist state
    state["day_index"] = day_index
    state["used_product_ids"] = list(used_ids)
    state["used_similarity_keys"] = list(used_keys)
    save_state(state)

if __name__ == "__main__":
    main()
