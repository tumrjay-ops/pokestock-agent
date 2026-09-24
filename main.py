import json
import os
import re
import time
import hashlib
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
WATCH = json.loads((ROOT / "watchlist.json").read_text())
STATE_FILE = ROOT / "state.json"
USER_AGENT = "Mozilla/5.0 (compatible; PokeStockAgent/1.4; personal stock monitor)"

AVAILABLE_TERMS = [
    "add to cart", "buy now", "preorder", "pre-order", "order pickup",
    "pickup", "shipping available", "ship it", "available for shipping"
]
UNAVAILABLE_TERMS = ["coming soon", "sold out", "unavailable", "out of stock"]
BLOCK_TERMS = ["captcha", "access denied", "verify you are human"]


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.stripped_strings).lower()


def extract_price(html, text):
    visible = re.findall(r"\$(\d{1,4}(?:\.\d{2})?)", text)
    candidates = []
    for raw in visible:
        try:
            candidates.append(float(raw))
        except ValueError:
            pass

    patterns = [
        r'"price"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?',
        r'"salePrice"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?',
        r'"current[_A-Za-z]*price"\s*:\s*"?\$?(\d{1,4}(?:\.\d{1,2})?)"?',
    ]
    for pattern in patterns:
        for raw in re.findall(pattern, html, flags=re.I):
            try:
                candidates.append(float(raw))
            except ValueError:
                pass

    candidates = [p for p in candidates if 1.0 <= p <= 1000.0]
    return min(candidates) if candidates else None


def classify(text):
    found_available = [t for t in AVAILABLE_TERMS if t in text]
    found_unavailable = [t for t in UNAVAILABLE_TERMS if t in text]
    hard_unavailable = any(t in found_unavailable for t in ["coming soon", "sold out", "out of stock"])
    return {
        "available_signals": found_available,
        "unavailable_signals": found_unavailable,
        "available": bool(found_available) and not hard_unavailable,
    }


def alert_text(payload):
    price = payload.get("price_seen")
    price_text = f"${price:.2f}" if isinstance(price, (int, float)) else "precio retail"
    return (
        f"{payload['retailer']}: {payload['product']} — {price_text} — "
        f"cantidad {payload['qty_desired']} — {payload['url']}"
    )


def send_ntfy(payload):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return
    try:
        requests.post(
            "https://ntfy.sh",
            json={
                "topic": topic,
                "title": f"🔥 {payload['retailer']} Pokémon disponible",
                "message": alert_text(payload),
                "priority": 5,
                "click": payload["url"],
                "tags": ["rotating_light", "shopping_cart"],
            },
            timeout=10,
        ).raise_for_status()
        print("NTFY delivered", flush=True)
    except Exception as e:
        print(f"NTFY error: {e}", flush=True)


def send_twilio_sms(payload):
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    to_number = os.getenv("SMS_TO_NUMBER", "").strip()
    if not all([sid, token, from_number, to_number]):
        return
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": from_number, "To": to_number, "Body": alert_text(payload)},
            auth=(sid, token),
            timeout=10,
        )
        r.raise_for_status()
        print("SMS delivered", flush=True)
    except Exception as e:
        print(f"SMS error: {e}", flush=True)


def notify(payload):
    print("ALERT", json.dumps(payload, ensure_ascii=False), flush=True)
    send_ntfy(payload)
    send_twilio_sms(payload)
    webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            requests.post(webhook, json=payload, timeout=10)
        except Exception as e:
            print(f"Webhook error: {e}", flush=True)


def check_product(session, product, state):
    key = f"{product['retailer']}:{product.get('sku') or product.get('tcin') or product['url']}"
    try:
        r = session.get(product["url"], timeout=20, allow_redirects=True)
        status_code = r.status_code
        if status_code >= 400:
            print(f"{datetime.now(timezone.utc).isoformat()} {key} HTTP {status_code}", flush=True)
            return

        text = normalize_text(r.text)
        status = classify(text)
        price = extract_price(r.text, text)
        blocked = any(x in text for x in BLOCK_TERMS)
        max_price = float(product["max_price"])
        min_plausible_price = float(product.get("min_price", max_price * 0.60))
        price_ok = price is not None and min_plausible_price <= price <= max_price + 0.01
        purchasable = status["available"] and price_ok and not blocked

        fingerprint_obj = {
            "http": status_code,
            "price": price,
            "available": purchasable,
            "signals": status["available_signals"],
            "unavailable_signals": status["unavailable_signals"],
            "blocked": blocked,
        }
        fp = hashlib.sha256(json.dumps(fingerprint_obj, sort_keys=True).encode()).hexdigest()
        old_fp = state.get(key, {}).get("fingerprint")

        if old_fp != fp:
            state[key] = {
                "fingerprint": fp,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "snapshot": fingerprint_obj,
            }
            save_state(state)
            print(f"CHANGE {key} {json.dumps(fingerprint_obj)}", flush=True)

            if purchasable:
                notify({
                    "type": "stock_available",
                    "retailer": product["retailer"],
                    "product": product["name"],
                    "qty_desired": product["qty"],
                    "price_seen": price,
                    "max_price": product["max_price"],
                    "signals": status["available_signals"],
                    "url": product["url"],
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                })
    except Exception as e:
        print(f"{datetime.now(timezone.utc).isoformat()} {key} ERROR {e}", flush=True)


def main():
    poll = int(os.getenv("POLL_SECONDS", WATCH.get("poll_seconds", 60)))
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    state = load_state()
    print(f"PokéStock Agent started. Polling every {poll}s", flush=True)

    while True:
        for product in WATCH["products"]:
            check_product(session, product, state)
            time.sleep(2)
        time.sleep(max(5, poll - 2 * len(WATCH["products"])))


if __name__ == "__main__":
    main()
