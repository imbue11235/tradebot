"""
strategies/sentiment.py — News sentiment signal engine.

Pipeline:
  1. Pull recent news from Alpaca News API (primary) + optional Finnhub
  2. Score each article with FinBERT (financial domain BERT model)
  3. Aggregate scores per ticker with recency + source weighting
  4. Return signals above threshold

FinBERT is a BERT model fine-tuned on financial text.
It outputs: positive / negative / neutral with probability scores.
We convert this to a directional confidence score:
  score = P(positive) - P(negative)   ∈ [-1, 1]
  Only act on score > threshold (default 0.40).
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests

logger = logging.getLogger("tradebot")

# Hard cutoff: articles older than this are ignored entirely regardless of source.
# Day-trading is intraday — yesterday's news is already priced in.
MAX_ARTICLE_AGE_MINUTES = 120   # 2 hours

_tokenizer = None
_model = None


def _load_finbert():
    global _tokenizer, _model
    if _tokenizer is None:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        import torch
        logger.info("Loading FinBERT model (first run takes ~30s)...")
        _tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        _model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        _model.eval()
        logger.info("FinBERT loaded.")


def score_text(text: str) -> float:
    """
    Score a single piece of text.
    Returns float in [-1, 1]: positive = bullish, negative = bearish.
    """
    if not text or not text.strip():
        return 0.0
    _load_finbert()
    import torch
    inputs = _tokenizer(
        text[:512],
        return_tensors="pt",
        truncation=True,
        padding=True,
    )
    with torch.no_grad():
        logits = _model(**inputs).logits
        probs = torch.softmax(logits, dim=-1).squeeze()
    # FinBERT labels: 0=positive, 1=negative, 2=neutral
    return (probs[0] - probs[1]).item()


def _safe_str(*values) -> str:
    """Join fields safely — converts None to empty string before concatenation."""
    return " ".join(str(v) for v in values if v is not None).strip()


class NewsSignal:
    def __init__(self, ticker: str, score: float, articles: list, headline: str):
        self.ticker = ticker
        self.score = score
        self.direction = "buy" if score > 0 else "sell"
        self.articles = articles
        self.headline = headline or ""
        self.timestamp = datetime.now(timezone.utc)

    def __repr__(self):
        return (
            f"<Signal {self.ticker} {self.direction.upper()} "
            f"confidence={abs(self.score):.2f} articles={len(self.articles)}>"
        )


class SentimentStrategy:
    def __init__(self, news_cfg: dict, alpaca_client, universe_cfg: dict):
        self.cfg = news_cfg
        self.alpaca = alpaca_client
        self.watchlist = set(universe_cfg.get("watchlist", []))
        self.allow_opportunistic = universe_cfg.get("allow_opportunistic", True)
        self.opp_threshold = universe_cfg.get("opportunistic_min_score", 0.80)
        self.lookback_minutes = news_cfg.get("lookback_minutes", 60)
        self.threshold = news_cfg.get("sentiment_threshold", 0.40)
        self.min_articles = news_cfg.get("min_articles_for_signal", 1)
        self.finnhub_key = news_cfg.get("finnhub_key", "")
        self._seen_article_ids: dict = {}  # {article_id: iso_timestamp}

        # Rate limit tracking — disable sources that return 429 until next day
        self._finnhub_disabled_until: Optional[datetime] = None

    # ── Public ────────────────────────────────────────────────────────────────

    def fetch_negative_signals(self, held_tickers: set, min_score: float, min_articles: int) -> dict:
        """
        Score news for currently held tickers and return any with strong negative sentiment.
        Returns dict {ticker: (score, headline)} for tickers that should be exited.
        min_score should be negative, e.g. -0.60.
        """
        if not held_tickers:
            return {}

        articles_by_ticker: dict[str, list] = {}
        now = datetime.now(timezone.utc)

        try:
            from alpaca.data.requests import NewsRequest
            since = now - timedelta(minutes=self.lookback_minutes)
            # Fetch per ticker — avoids Alpaca SDK list-vs-string validation issues
            for ticker in held_tickers:
                try:
                    req = NewsRequest(symbols=ticker, start=since, limit=20)
                    news = self.alpaca.get_news(req)
                    for item in (news.news if hasattr(news, "news") else news):
                        articles_by_ticker.setdefault(ticker, []).append({
                            "id": str(getattr(item, "id", "") or ""),
                            "headline": getattr(item, "headline", "") or "",
                            "summary": getattr(item, "summary", "") or "",
                            "created_at": getattr(item, "created_at", None),
                        })
                except Exception as e:
                    logger.debug(f"Negative signal fetch failed for {ticker}: {e}")
        except Exception as e:
            logger.warning(f"Negative signal fetch setup failed: {e}")
            return {}

        results = {}
        for ticker, articles in articles_by_ticker.items():
            fresh = [
                a for a in articles
                if self._article_age_minutes(a, now) <= MAX_ARTICLE_AGE_MINUTES
            ]
            if len(fresh) < min_articles:
                continue

            score = self._aggregate_score(fresh)

            if score <= min_score:
                best = max(fresh, key=lambda a: abs(score_text(
                    _safe_str(a.get("headline"), a.get("title"))
                )))
                headline = best.get("headline") or best.get("title") or "No headline"
                results[ticker] = (score, headline)
                logger.info(
                    f"Negative signal: {ticker} score={score:.2f} | \"{headline[:80]}\""
                )

        return results

    def fetch_signals(self) -> list[NewsSignal]:
        """
        Main entry point. Returns list of actionable NewsSignal objects.
        Only returns BUY signals (bot is long-only, no shorts).
        """
        articles_by_ticker: dict[str, list] = {}

        # ── Source 1: Alpaca News ──────────────────────────────────────────
        try:
            alpaca_articles = self._fetch_alpaca_news()
            for art in alpaca_articles:
                for sym in art.get("symbols", []):
                    articles_by_ticker.setdefault(sym, []).append(art)
        except Exception as e:
            logger.warning(f"Alpaca news fetch failed: {e}")

        # ── Source 2: Finnhub (optional, real-time, 60 req/min free tier) ──
        if self.finnhub_key and not self._is_disabled(self._finnhub_disabled_until, "Finnhub"):
            for ticker in self.watchlist:
                try:
                    arts = self._fetch_finnhub(ticker)
                    articles_by_ticker.setdefault(ticker, []).extend(arts)
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code == 429:
                        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        self._finnhub_disabled_until = tomorrow
                        logger.warning(
                            f"Finnhub rate limit hit. Disabled until "
                            f"{tomorrow.strftime('%Y-%m-%d %H:%M UTC')}."
                        )
                        break
                    else:
                        logger.warning(f"Finnhub fetch failed for {ticker}: {e}")
                except Exception as e:
                    logger.warning(f"Finnhub fetch failed for {ticker}: {e}")

        # ── Score and filter ───────────────────────────────────────────────
        signals = []
        for ticker, articles in articles_by_ticker.items():
            now = datetime.now(timezone.utc)
            new_articles = [
                a for a in articles
                if a.get("id", a.get("url", "")) not in self._seen_article_ids
                and self._article_age_minutes(a, now) <= MAX_ARTICLE_AGE_MINUTES
            ]
            if len(new_articles) < self.min_articles:
                continue

            now_iso = now.isoformat()
            for a in new_articles:
                self._seen_article_ids[a.get("id", a.get("url", ""))] = now_iso

            score = self._aggregate_score(new_articles)
            abs_score = abs(score)

            if score <= 0 or abs_score < self.threshold:
                continue

            in_watchlist = ticker in self.watchlist
            if not in_watchlist:
                if not self.allow_opportunistic or abs_score < self.opp_threshold:
                    continue

            # Best headline — safely handles None fields
            def article_score(a):
                return abs(score_text(_safe_str(
                    a.get("headline"), a.get("title")
                )))

            best = max(new_articles, key=article_score)
            best_headline = (
                best.get("headline") or best.get("title") or "No headline"
            )

            signals.append(NewsSignal(
                ticker=ticker,
                score=abs_score,
                articles=new_articles,
                headline=best_headline,
            ))
            logger.info(f"Signal: {ticker} BUY score={abs_score:.2f} | \"{best_headline[:80]}\"")

        return sorted(signals, key=lambda s: s.score, reverse=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_disabled(self, disabled_until: Optional[datetime], name: str) -> bool:
        if disabled_until is None:
            return False
        if datetime.now(timezone.utc) >= disabled_until:
            logger.info(f"{name} re-enabled after rate limit cooldown.")
            return False
        return True

    def _article_age_minutes(self, art: dict, now: datetime) -> float:
        """Return age of article in minutes. Returns MAX+1 if timestamp missing or unparseable."""
        pub_time = art.get("created_at") or art.get("publishedAt")
        if not pub_time:
            return MAX_ARTICLE_AGE_MINUTES + 1  # no timestamp → treat as too old
        try:
            if isinstance(pub_time, str):
                from dateutil import parser as dtparser
                pub_dt = dtparser.parse(pub_time)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            else:
                pub_dt = pub_time
            return (now - pub_dt).total_seconds() / 60
        except Exception:
            return MAX_ARTICLE_AGE_MINUTES + 1

    def _aggregate_score(self, articles: list) -> float:
        """
        Weighted average sentiment score.
        Recent articles weighted higher. More articles → higher confidence.
        """
        now = datetime.now(timezone.utc)
        total_weight = 0.0
        weighted_score = 0.0

        for art in articles:
            # Use _safe_str to guard against None values in any field
            text = _safe_str(
                art.get("headline"),
                art.get("title"),
                art.get("summary"),
                art.get("description"),
            )
            if not text:
                continue

            raw_score = score_text(text)

            age_minutes = self._article_age_minutes(art, now)

            # Hard cutoff — shouldn't reach here after pre-filter, but be safe
            if age_minutes > MAX_ARTICLE_AGE_MINUTES:
                continue

            # Decay curve:
            #   0–15 min  → full weight  (breaking news)
            #   15–30 min → 0.80         (very recent)
            #   30–60 min → 0.50         (recent)
            #   60–120min → 0.15         (getting stale, minimal influence)
            #   unknown   → 0.10         (no timestamp = treat as old)
            if age_minutes < 0:
                recency_weight = 0.10   # future-dated timestamp = suspicious
            elif age_minutes <= 15:
                recency_weight = 1.0
            elif age_minutes <= 30:
                recency_weight = 0.80
            elif age_minutes <= 60:
                recency_weight = 0.50
            else:
                recency_weight = 0.15
            weighted_score += raw_score * recency_weight
            total_weight += recency_weight

        if total_weight == 0:
            return 0.0
        return weighted_score / total_weight

    # ── News sources ──────────────────────────────────────────────────────────

    def _fetch_alpaca_news(self) -> list:
        from alpaca.data.requests import NewsRequest

        since = datetime.now(timezone.utc) - timedelta(minutes=self.lookback_minutes)
        result = []

        # Fetch per ticker — Alpaca SDK validates symbols as string not list
        tickers = list(self.watchlist) if self.watchlist else []
        for ticker in tickers:
            try:
                req = NewsRequest(symbols=ticker, start=since, limit=20)
                news = self.alpaca.get_news(req)
                for item in (news.news if hasattr(news, "news") else news):
                    result.append({
                        "id": str(getattr(item, "id", "") or ""),
                        "headline": getattr(item, "headline", "") or "",
                        "summary": getattr(item, "summary", "") or "",
                        "symbols": getattr(item, "symbols", []) or [],
                        "created_at": getattr(item, "created_at", None),
                    })
            except Exception as e:
                logger.debug(f"Alpaca news fetch failed for {ticker}: {e}")
        return result

    def _fetch_finnhub(self, ticker: str) -> list:
        url = "https://finnhub.io/api/v1/company-news"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        params = {
            "symbol": ticker,
            "from": today,   # today only — yesterday's news is already priced in
            "to": today,
            "token": self.finnhub_key,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return [
            {
                "id": str(a.get("id") or ""),
                "headline": a.get("headline") or "",
                "summary": a.get("summary") or "",
                "symbols": [ticker],
                "created_at": (
                    datetime.fromtimestamp(a["datetime"], tz=timezone.utc)
                    if a.get("datetime") else None
                ),
            }
            for a in resp.json()
        ]
