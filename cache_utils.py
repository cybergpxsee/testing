# cache_utils.py
import os
import time
import hashlib
from pathlib import Path
from typing import Optional, Dict
import pandas as pd

class BarCache:
    def __init__(self, cache_dir=".cache/bars", max_age_days=30, enabled=True):
        self.cache_dir = Path(cache_dir)
        self.max_age_seconds = max_age_days * 86400
        self.enabled = enabled
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, symbol: str, period: str) -> Path:
        safe = symbol.replace('.', '-').replace('/', '_')
        key = f"{safe}_{period}"
        hash_suffix = hashlib.md5(key.encode()).hexdigest()[:8]
        return self.cache_dir / f"{safe}_{period}_{hash_suffix}.parquet"

    def get(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        if not self.enabled:
            return None
        path = self._get_cache_path(symbol, period)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.max_age_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        try:
            df = pd.read_parquet(path)
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
            return df
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def set(self, symbol: str, period: str, df: pd.DataFrame):
        if not self.enabled or df is None or len(df) == 0:
            return
        path = self._get_cache_path(symbol, period)
        try:
            df_copy = df.copy()
            if 'Date' in df_copy.columns:
                df_copy['Date'] = df_copy['Date'].dt.strftime('%Y-%m-%d')
            df_copy.to_parquet(path, index=False)
        except Exception:
            pass

    def clear_expired(self):
        for f in self.cache_dir.glob("*.parquet"):
            if time.time() - f.stat().st_mtime > self.max_age_seconds:
                try:
                    f.unlink()
                except OSError:
                    pass

_cache: Optional[BarCache] = None

def get_cache() -> BarCache:
    global _cache
    if _cache is None:
        _cache = BarCache()
    return _cache