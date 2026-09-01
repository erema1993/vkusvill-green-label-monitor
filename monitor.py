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
                src = attrs.get("data-src") or attrs.get("src")
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
    # В карточке название идёт после пары цен и их числовых дублей.
    match = re.search(
        r"(?P<green>\d+(?:[.,]\d+)?)\s*руб\.?\s*/(?:кг|шт)\s+"
        r"(?P<old>\d+(?:[.,]\d+)?)\s*руб\.?\s+\d+(?:[.,]\d+)?\s+\d+(?:[.,]\d+)?\s+"
        r"(?P<name>.+?)\s+В корзину\b",
        text,
    )
    if match:
        result["green_price"] = f'{match.group("green")} руб'
        result["old_price"] = f'{match.group("old")} руб'
        result["name"] = match.group("name").strip()
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


def product_message(product: dict) -> str:
    lines = [f"🟢 <b>{html.escape(product['name'])}</b>"]
    prices = " → ".join(filter(None, [product.get("old_price"), product.get("green_price")]))
    if prices:
        lines.append(html.escape(prices))
    if product.get("stock"):
        lines.append(f"Остаток: {html.escape(product['stock'])}")
    if product.get("href"):
        lines.append(f'<a href="{html.escape(product["href"], quote=True)}">Открыть товар</a>')
    return "\n".join(lines)


def read_seen() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen_ids", []))


def write_seen(ids) -> None:
    STATE_FILE.write_text(json.dumps({"seen_ids": sorted(ids)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    current = {state_id(p["id"]) for p in products}
    first_run = not STATE_FILE.exists()
    seen = read_seen()
    new = [] if first_run else [p for p in products if state_id(p["id"]) not in seen]
    for product in new:
        telegram_send(token, chat_id, product_message(product))
    write_seen(current)
    print(f"Найдено: {len(products)}; новых: {len(new)}" + ("; база создана" if first_run else ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
