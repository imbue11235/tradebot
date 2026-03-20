"""
strategies/sentiment.py — News sentiment signal engine.

Pipeline:
  1. Pull recent news from Alpaca News API (+ optional NewsAPI/Finnhub)
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

# Lazy-loaded to avoid slow startup
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
    _load_finbert()
    import torch
    inputs = _tokenizer(
        text[:512],  # FinBERT max sequence length
        return_tensors="pt",
        truncation=True,
        padding=True,
    )
    with torch.no_grad():
        logits = _model(**inputs).logits
        probs = torch.softmax(logits, dim=-1).squeeze()
    # FinBERT labels: 0=positive, 1=negative, 2=neutral
    return (probs[0] - probs[1]).item()


class NewsSignal:
    def __init__(self, ticker: str, score: float, articles: list, headline: str):
        self.ticker = ticker
        self.score = score          # 0.0 → 1.0 confidence (magnitude of sentiment)
        self.direction = "buy" if score > 0 else "sell"
        self.articles = articles
        self.headline = headline    # Most impactful headline for logging
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
        self.newsapi_key = news_cfg.get("newsapi_key", "")
        self.finnhub_key = news_cfg.get("finnhub_key", "")
        self._seen_article_ids: set = set()

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

        # ── Source 2: NewsAPI (optional) ───────────────────────────────────
        if self.newsapi_key:
            try:
                for ticker in self.watchlist:
                    newsapi_arts = self._fetch_newsapi(ticker)
                    articles_by_ticker.setdefault(ticker, []).extend(newsapi_arts)
            except Exception as e:
                logger.warning(f"NewsAPI fetch failed: {e}")

        # ── Source 3: Finnhub (optional) ───────────────────────────────────
        if self.finnhub_key:
            try:
                for ticker in self.watchlist:
                    finnhub_arts = self._fetch_finnhub(ticker)
                    articles_by_ticker.setdefault(ticker, []).extend(finnhub_arts)
            except Exception as e:
                logger.warning(f"Finnhub fetch failed: {e}")

        # ── Score and filter ───────────────────────────────────────────────
        signals = []
        for ticker, articles in articles_by_ticker.items():
            # Filter to new articles only
            new_articles = [
                a for a in articles
                if a.get("id", a.get("url", "")) not in self._seen_article_ids
            ]
            if len(new_articles) < self.min_articles:
                continue

            # Mark as seen
            for a in new_articles:
                self._seen_article_ids.add(a.get("id", a.get("url", "")))

            # Score with FinBERT
            score = self._aggregate_score(new_articles)
            abs_score = abs(score)

            # Only act on BUY signals above threshold
            if score <= 0 or abs_score < self.threshold:
                continue

            # Watchlist check
            in_watchlist = ticker in self.watchlist
            if not in_watchlist:
                if not self.allow_opportunistic or abs_score < self.opp_threshold:
                    continue

            # Best headline for reporting
            best_headline = max(
                new_articles,
                key=lambda a: abs(score_text(a.get("headline", a.get("title", "")))),
            ).get("headline", new_articles[0].get("title", "No headline"))

            signals.append(NewsSignal(
                ticker=ticker,
                score=abs_score,
                articles=new_articles,
                headline=best_headline,
            ))
            logger.info(f"Signal: {ticker} BUY score={abs_score:.2f} | \"{best_headline[:80]}\"")

        return sorted(signals, key=lambda s: s.score, reverse=True)

    def _aggregate_score(self, articles: list) -> float:
        """
        Weighted average sentiment score.
        Recent articles weighted higher. More articles → higher confidence.
        """
        now = datetime.now(timezone.utc)
        total_weight = 0.0
        weighted_score = 0.0

        for art in articles:
            text = art.get("headline", art.get("title", "")) + " " + art.get("summary", "")
            if not text.strip():
                continue

            raw_score = score_text(text)

            # Recency decay: articles older than 30min get half weight
            pub_time = art.get("created_at") or art.get("publishedAt")
            age_minutes = 60.0  # default if unknown
            if pub_time:
                try:
                    if isinstance(pub_time, str):
                        from dateutil import parser as dtparser
                        pub_dt = dtparser.parse(pub_time)
                        if pub_dt.tzinfo is None:
                            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    else:
                        pub_dt = pub_time
                    age_minutes = (now - pub_dt).total_seconds() / 60
                except Exception:
                    pass

            recency_weight = 1.0 if age_minutes <= 30 else 0.5 if age_minutes <= 60 else 0.25

            weighted_score += raw_score * recency_weight
            total_weight += recency_weight

        if total_weight == 0:
            return 0.0
        return weighted_score / total_weight

    def _fetch_alpaca_news(self) -> list:
        """Pull news from Alpaca's built-in news feed."""
        from alpaca.data.requests import NewsRequest
        from alpaca.data.historical import NewsClient

        # Re-use the data client passed in (alpaca_client here is the NewsClient)
        since = datetime.now(timezone.utc) - timedelta(minutes=self.lookback_minutes)
        symbols = list(self.watchlist) if self.watchlist else None

        try:
            req = NewsRequest(
                symbols=symbols,
                start=since,
                limit=50,
            )
            news = self.alpaca.get_news(req)
            result = []
            for item in (news.news if hasattr(news, "news") else news):
                result.append({
                    "id": str(getattr(item, "id", "")),
                    "headline": getattr(item, "headline", ""),
                    "summary": getattr(item, "summary", ""),
                    "symbols": getattr(item, "symbols", []),
                    "created_at": getattr(item, "created_at", None),
                })
            return result
        except Exception as e:
            logger.debug(f"Alpaca news detail: {e}")
            return []

    def _fetch_newsapi(self, ticker: str) -> list:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": ticker,
            "sortBy": "publishedAt",
            "pageSize": 10,
            "apiKey": self.newsapi_key,
            "language": "en",
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {
                "id": a.get("url", ""),
                "headline": a.get("title", ""),
                "summary": a.get("description", ""),
                "symbols": [ticker],
                "created_at": a.get("publishedAt"),
            }
            for a in articles
        ]

    def _fetch_finnhub(self, ticker: str) -> list:
        url = "https://finnhub.io/api/v1/company-news"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        params = {"symbol": ticker, "from": yesterday, "to": today, "token": self.finnhub_key}
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return [
            {
                "id": str(a.get("id", "")),
                "headline": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "symbols": [ticker],
                "created_at": datetime.fromtimestamp(a["datetime"], tz=timezone.utc) if "datetime" in a else None,
            }
            for a in resp.json()
        ]
