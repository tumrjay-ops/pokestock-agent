# PokéStock Agent

24/7 stock monitor for Pokémon TCG 30th Celebration products at Best Buy and Target.

## What it does
- Watches configured product pages continuously.
- Filters for expected first-party retail prices.
- Detects changes such as Add to Cart, Preorder, Shipping, Pickup, Coming Soon, Sold Out, and In Store Only.
- Logs useful changes and can send alerts to an optional webhook/ntfy topic.
- Never bypasses CAPTCHA, queues, purchase limits, login protections, or anti-bot systems.

## Current priorities
- Best Buy Booster Bundle — qty 2 — $26.94 target price.
- Best Buy Elite Trainer Box — qty 2 — $49.99 target price.
- Best Buy Ultra-Premium Collection Day or Night — qty 1 — $179.99 target price.
- Best Buy Poster Collection — qty 2 — $14.99 target price.
- Target 30th Celebration Booster Bundle — qty 2 — $31.99 target price.

## Run
```bash
pip install -r requirements.txt
python main.py
```

Configuration lives in `watchlist.json`.
