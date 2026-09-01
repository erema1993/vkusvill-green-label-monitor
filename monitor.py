#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import html
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ENDPOINT = "https://vkusvill.ru/ajax/index_page_lazy_load.php"
STATE_FILE = Path(__file__).with_name("state.json")
SHOP_ADDRESS = "Лиговский проспект, 232"


def open_https(request, timeout=30):
    # Python из python.org на macOS иногда не подключён к системному набору CA.
    cafile = os.getenv("SSL_CERT_FILE")
    if not cafile and Path("/etc/ssl/cert.pem").exists():
        cafile = "/etc/ssl/cert.pem"
    context = ssl.create_default_context(cafile=cafile)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class ProductParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "div" and "ProductCard" in classes and attrs.get("data-id"):
            self.card = {"id": attrs["data-id"], "texts": [], "href": "", "image": ""}
            self.stack = [tag]
            for key in ("data-max", "data-quantity"):
                if attrs.get(key):
                    self.card["stock"] = attrs[key]
        elif self.card is not None:
            if tag not in self.VOID_TAGS:
                self.stack.append(tag)
            if tag == "a" and attrs.get("href") and not self.card["href"]:
                self.card["href"] = urllib.parse.urljoin("https://vkusvill.ru", attrs["href"])
            if tag == "img":
                src = attrs.get("data-src") or attrs.get("data-original") or attrs.get("src")
                if not src and attrs.get("srcset"):
                    src = attrs["srcset"].split(",")[0].strip().split()[0]
                if src and not self.card["image"]:
                    self.card["image"] = urllib.parse.urljoin("https://vkusvill.ru", src)
            if attrs.get("data-max"):
                self.card["stock"] = attrs["data-max"]

    def handle_data(self, data):
        if self.card is not None:
            text = " ".join(data.split())
            if text:
                self.card["texts"].append(text)

    def handle_endtag(self, tag):
        if self.card is None:
            return
        if self.stack:
            self.stack.pop()
        if not self.stack:
            text = " ".join(self.card.pop("texts"))
            self.card.update(extract_fields(text))
            self.cards.append(self.card)
            self.card = None


def extract_fields(text: str) -> dict:
    result = {"text": text}
    amount = r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?"
    # В карточке название идёт после пары цен и их числовых дублей.
    match = re.search(
        rf"(?P<green>{amount})\s*руб\.?\s*/(?:кг|шт)\s+"
        rf"(?P<old>{amount})\s*руб\.?\s+{amount}\s+{amount}\s+"
        r"(?P<name>.+?)\s+В корзину\b",
        text,
    )
    if match:
        result["green_price"] = f'{match.group("green").replace(" ", "")} руб'
        result["old_price"] = f'{match.group("old").replace(" ", "")} руб'
        name = match.group("name").strip()
        result["name"] = re.sub(r"(?P<w>\d+(?:[.,]\d+)?\s*(?:кг|г|мл|л))(?:\s+(?P=w))+$", r"\g<w>", name, flags=re.IGNORECASE)
    else:
        prices = re.findall(r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?\s*₽", text)
        if prices:
            result["green_price"] = prices[0]
        if len(prices) > 1:
            result["old_price"] = prices[1]
        prefix = text.split(prices[0], 1)[0] if prices else text
        result["name"] = prefix.strip(" ·–—") or f"Товар {text[:40]}"
    return result


def fetch_products(cookie: str) -> list[dict]:
    body = urllib.parse.urlencode({
        "code": "cart_green_labels", "version": "default", "is_app": ""
    }).encode()
    request = urllib.request.Request(ENDPOINT, data=body, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://vkusvill.ru",
        "Referer": "https://vkusvill.ru/cart/",
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
    })
    with open_https(request) as response:
        payload = json.load(response)
    if payload.get("success") != "Y":
        raise RuntimeError(f"ВкусВилл вернул success={payload.get('success')!r}")
    parser = ProductParser()
    parser.feed(payload.get("html", ""))
    if payload.get("count_prods_total", 0) and not parser.cards:
        raise RuntimeError("Товары есть, но карточки не удалось распознать: разметка сайта изменилась")
    return parser.cards


def fetch_product_page(url: str, cookie: str) -> str:
    request = urllib.request.Request(url, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Cookie": cookie,
        "Referer": "https://vkusvill.ru/cart/",
        "User-Agent": "Mozilla/5.0",
    })
    with open_https(request) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def parse_nutrition(source: str) -> list[dict]:
    """Extract every manufacturer-specific nutrition variant from a product page."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", source))
    text = " ".join(text.replace("\xa0", " ").split())
    number = r"(\d+(?:[.,]\d+)?)"
    pattern = re.compile(
        rf'(?P<maker>(?:ООО|АО|ЗАО|ИП|ОАО|ПАО)\s*[«"“][^»"”]+[»"”])\s*:\s*'
        rf'белки\s*{number}\s*г\s*,\s*жиры\s*{number}\s*г\s*,\s*углеводы\s*{number}\s*г'
        rf'(?:(?!\b(?:ООО|АО|ЗАО|ИП|ОАО|ПАО)\s*[«"“]).)*?{number}\s*ккал',
        re.IGNORECASE,
    )
    variants = []
    for match in pattern.finditer(text):
        maker, protein, fat, carbs, kcal = match.groups()
        variants.append({
            "manufacturer": maker,
            "protein": float(protein.replace(",", ".")),
            "fat": float(fat.replace(",", ".")),
            "carbs": float(carbs.replace(",", ".")),
            "kcal": float(kcal.replace(",", ".")),
        })
    if not variants:
        summary = re.search(
            rf"Пищевая\s+ценность\s+на\s+100\s+грамм\s+{number}\s*[КK]кал\s+"
            rf"{number}\s+Белки\s*,?\s*г\s+{number}\s+Жиры\s*,?\s*г\s+{number}\s+Углеводы",
            text, re.IGNORECASE,
        )
        if summary:
            kcal, protein, fat, carbs = (float(value.replace(",", ".")) for value in summary.groups())
            variants.append({"manufacturer": "", "protein": protein, "fat": fat, "carbs": carbs, "kcal": kcal})
    return variants


def nutrition_summary(variants: list[dict]) -> dict | None:
    if not variants:
        return None
    keys = ("kcal", "protein", "fat", "carbs")
    return {
        **{key: sum(item[key] for item in variants) / len(variants) for key in keys},
        "approx": len(variants) > 1,
        "variants": len(variants),
    }


def telegram_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "false",
    }).encode()
    with open_https(urllib.request.Request(url, data=body)) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram отклонил сообщение: {payload}")


def telegram_send_photo(token: str, chat_id: str, photo: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML",
    }).encode()
    with open_https(urllib.request.Request(url, data=body)) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram отклонил фото: {payload}")


def as_number(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", value.replace(" ", ""))
    return float(match.group().replace(",", ".")) if match else None


def fmt(value: float) -> str:
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else str(rounded).replace(".", ",")


def product_message(product: dict) -> str:
    lines = ["🟢 <b>НОВЫЙ ЗЕЛЁНЫЙ ЦЕННИК</b>", "", f"<b>{html.escape(product['name'])}</b>"]
    old, green = as_number(product.get("old_price")), as_number(product.get("green_price"))
    if old is not None and green is not None:
        saving, percent = max(0, old - green), (max(0, old - green) / old * 100 if old else 0)
        lines.append(f"<s>{fmt(old)} ₽</s> → <b>{fmt(green)} ₽</b>")
        lines.append(f"Экономия: <b>{fmt(saving)} ₽ ({fmt(percent)}%)</b>")
    nutrition = product.get("nutrition")
    if nutrition:
        prefix = "≈" if nutrition["approx"] else ""
        lines.extend(["", "<b>КБЖУ на 100 г:</b>",
                      f"{prefix}{fmt(nutrition['kcal'])} ккал • Б {fmt(nutrition['protein'])} г • Ж {fmt(nutrition['fat'])} г • У {fmt(nutrition['carbs'])} г"])
        if nutrition["approx"]:
            lines.append("<i>Значения могут отличаться в зависимости от изготовителя</i>")
    if product.get("stock"):
        lines.extend(["", f"Доступно: <b>{html.escape(product['stock'])}</b>"])
    lines.append(f"📍 {SHOP_ADDRESS}")
    if product.get("href"):
        lines.extend(["", f'<a href="{html.escape(product["href"], quote=True)}"><b>Открыть товар</b></a>'])
    return "\n".join(lines)


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_ids": [], "nutrition": {}}
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": data.get("seen_ids", []), "nutrition": data.get("nutrition", {})}


def write_state(ids, nutrition) -> None:
    STATE_FILE.write_text(json.dumps({"seen_ids": sorted(ids), "nutrition": nutrition}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def state_id(product_id: str) -> str:
    key = os.getenv("STATE_HASH_KEY")
    if not key:
        return product_id
    return hmac.new(key.encode(), product_id.encode(), hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Монитор зелёных ценников ВкусВилла")
    parser.add_argument("--dry-run", action="store_true", help="получить и показать товары без Telegram и state.json")
    parser.add_argument("--send-test", action="store_true", help="отправить одно тестовое сообщение в Telegram")
    args = parser.parse_args()
    load_dotenv(Path(__file__).with_name(".env"))
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if args.send_test:
        if not token or not chat_id:
            raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
        telegram_send(token, chat_id, "✅ Тест: монитор зелёных ценников подключён")
        print("Тестовое сообщение отправлено.")
        return 0
    cookie = os.getenv("VV_COOKIE")
    if not cookie:
        raise RuntimeError("Заполните VV_COOKIE в .env")
    products = fetch_products(cookie)
    if args.dry_run:
        print(json.dumps(products, ensure_ascii=False, indent=2))
        return 0
    if not token or not chat_id:
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    first_run = not STATE_FILE.exists()
    state = read_state()
    seen = set(state["seen_ids"])
    nutrition_cache = state["nutrition"]
    new = [] if first_run else [p for p in products if state_id(p["id"]) not in seen]
    for product in new:
        cache_key = state_id(product["id"])
        if cache_key not in nutrition_cache and product.get("href"):
            nutrition_cache[cache_key] = nutrition_summary(parse_nutrition(fetch_product_page(product["href"], cookie)))
        product["nutrition"] = nutrition_cache.get(cache_key)
        caption = product_message(product)
        if product.get("image"):
            telegram_send_photo(token, chat_id, product["image"], caption)
        else:
            telegram_send(token, chat_id, caption)
    seen.update(state_id(p["id"]) for p in products)
    write_state(seen, nutrition_cache)
    print(f"Найдено: {len(products)}; новых: {len(new)}" + ("; база создана" if first_run else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
