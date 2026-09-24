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
USER_AGENT = "Mozilla/5.0 (compatible; PokeStockAgent/1.5; personal stock monitor)"
AVAILABLE_TERMS = ["add to cart", "buy now", "preorder", "pre-order", "order pickup", "ship it", "shipping available", "available for shipping"]
UNAVAILABLE_TERMS = ["coming soon", "sold out", "unavailable", "out of stock"]
BLOCK_TERMS = ["captcha", "access denied", "verify you are human"]

def load_state():
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {}

def save_state(state): STATE_FILE.write_text(json.dumps(state, indent=2))

def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.stripped_strings).lower()

def extract_price(html, text):
    candidates=[]
    for raw in re.findall(r"\$(\d{1,4}(?:\.\d{2})?)", text):
        try: candidates.append(float(raw))
        except ValueError: pass
    for pattern in [r'"price"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?', r'"salePrice"\s*:\s*"?(\d{1,4}(?:\.\d{1,2})?)"?']:
        for raw in re.findall(pattern, html, flags=re.I):
            try: candidates.append(float(raw))
            except ValueError: pass
    candidates=[p for p in candidates if 1 <= p <= 1000]
    return min(candidates) if candidates else None

def classify(text):
    available=[t for t in AVAILABLE_TERMS if t in text]
    unavailable=[t for t in UNAVAILABLE_TERMS if t in text]
    return {"available_signals":available,"unavailable_signals":unavailable,"available":bool(available) and not unavailable}

def inspect(session, product):
    r=session.get(product["url"],timeout=20,allow_redirects=True)
    if r.status_code >= 400: return None
    text=normalize_text(r.text); status=classify(text); price=extract_price(r.text,text)
    blocked=any(x in text for x in BLOCK_TERMS)
    max_price=float(product["max_price"]); min_price=float(product.get("min_price",max_price*0.60))
    price_ok=price is not None and min_price <= price <= max_price+0.01
    return {"http":r.status_code,"price":price,"available":status["available"] and price_ok and not blocked,"signals":status["available_signals"],"unavailable_signals":status["unavailable_signals"],"blocked":blocked}

def alert_text(p):
    price=f"${p['price_seen']:.2f}" if isinstance(p.get('price_seen'),(int,float)) else "precio retail"
    return f"{p['retailer']}: {p['product']} CONFIRMADO PARA COMPRAR — {price} — cantidad objetivo {p['qty_desired']} — {p['url']}"

def send_ntfy(p):
    topic=os.getenv("NTFY_TOPIC","").strip()
    if not topic:return
    requests.post("https://ntfy.sh",json={"topic":topic,"title":"🔥 Pokémon CONFIRMADO PARA COMPRAR","message":alert_text(p),"priority":5,"click":p["url"],"tags":["rotating_light","shopping_cart"]},timeout=10).raise_for_status()

def send_twilio_sms(p):
    sid=os.getenv("TWILIO_ACCOUNT_SID","").strip(); token=os.getenv("TWILIO_AUTH_TOKEN","").strip(); frm=os.getenv("TWILIO_FROM_NUMBER","").strip(); to=os.getenv("SMS_TO_NUMBER","").strip()
    if all([sid,token,frm,to]): requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",data={"From":frm,"To":to,"Body":alert_text(p)},auth=(sid,token),timeout=10).raise_for_status()

def notify(p):
    print("ALERT",json.dumps(p,ensure_ascii=False),flush=True)
    try: send_ntfy(p)
    except Exception as e: print(f"NTFY error: {e}",flush=True)
    try: send_twilio_sms(p)
    except Exception as e: print(f"SMS error: {e}",flush=True)

def check_product(session, product, state):
    key=f"{product['retailer']}:{product.get('sku') or product.get('tcin') or product['url']}"
    try:
        first=inspect(session,product)
        if first is None:return
        confirmed=False; final=first
        if first["available"]:
            time.sleep(3)
            second=inspect(session,product)
            if second and second["available"]:
                confirmed=True; final=second
        fingerprint={"price":final.get("price"),"confirmed_buy":confirmed,"signals":final.get("signals",[]),"blocked":final.get("blocked",False)}
        fp=hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest(); old=state.get(key,{}).get("fingerprint")
        if old != fp:
            state[key]={"fingerprint":fp,"checked_at":datetime.now(timezone.utc).isoformat(),"snapshot":fingerprint}; save_state(state)
            if confirmed:
                qty=min(int(product.get("qty",1)),int(product.get("max_qty",product.get("qty",1))))
                notify({"type":"confirmed_buy","retailer":product["retailer"],"product":product["name"],"qty_desired":qty,"price_seen":final.get("price"),"url":product["url"],"store":product.get("store"),"checked_at":datetime.now(timezone.utc).isoformat()})
    except Exception as e: print(f"{datetime.now(timezone.utc).isoformat()} {key} ERROR {e}",flush=True)

def main():
    poll=int(os.getenv("POLL_SECONDS",WATCH.get("poll_seconds",60))); session=requests.Session(); session.headers.update({"User-Agent":USER_AGENT,"Accept-Language":"en-US,en;q=0.9"}); state=load_state()
    print(f"PokéStock Agent started. Polling every {poll}s",flush=True)
    while True:
        for product in WATCH["products"]:
            check_product(session,product,state); time.sleep(2)
        time.sleep(max(5,poll-2*len(WATCH["products"])))

if __name__ == "__main__": main()
