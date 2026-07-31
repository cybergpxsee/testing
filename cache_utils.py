"""
Cache module for US Pullback Scanner.
Provides local parquet caching for downloaded bars.
"""
import os
import hashlib
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, List
import pandas as pd
from config import get_cache_config


class BarCache:
    """Local parquet cache for downloaded bars."""
    
    def __init__(self, cache_dir: Optional[str] = None, max_age_days: int = 30):
        """
        Initialize cache.
        
        Args:
            cache_dir: Directory for cache files (default from config)
            max_age_days: Max age of cache files in days
        """
        cache_config = get_cache_config()
        
        self.cache_dir = Path(cache_dir or cache_config.get('bars_dir', '.cache/bars'))
        self.max_age_days = max_age_days
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = cache_config.get('enabled', True)
    
    def _get_cache_key(self, symbol: str, period: str) -> str:
        """Generate cache filename from symbol and period."""
        # Use hash to avoid filesystem issues with special characters
        key = f"{symbol}_{period}"
        hash_suffix = hashlib.md5(key.encode()).hexdigest()[:8]
        safe_symbol = symbol.replace('.', '-').replace('/', '-').replace('\\', '-')
        return f"{safe_symbol}_{period}_{hash_suffix}.parquet"
    
    def _get_cache_path(self, symbol: str, period: str) -> Path:
        """Get full cache file path."""
        return self.cache_dir / self._get_cache_key(symbol, period)
    
    def get(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        """
        Get cached bars if available and not expired.
        
        Args:
            symbol: Stock symbol
            period: Data period (e.g., '1mo', '1y')
            
        Returns:
            DataFrame if cached and valid, None otherwise
        """
        if not self.enabled:
            return None
            
        cache_path = self._get_cache_path(symbol, period)
        
        if not cache_path.exists():
            return None
        
        # Check age
        mtime = cache_path.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        if age_days > self.max_age_days:
            # Remove expired cache
            try:
                cache_path.unlink()
            except OSError:
                pass
            return None
        
        try:
            df = pd.read_parquet(cache_path)
            return df
        except Exception:
            # Corrupted cache, remove it
            try:
                cache_path.unlink()
            except OSError:
                pass
            return None
    
    def set(self, symbol: str, period: str, df: pd.DataFrame):
        """
        Save bars to cache.
        
        Args:
            symbol: Stock symbol
            period: Data period
            df: DataFrame to cache
        """
        if not self.enabled:
            return
            
        cache_path = self._get_cache_path(symbol, period)
        
        try:
            # Ensure required columns
            required_cols = ['Date', 'Open', 'High', 'Low', 'Close', 'Volume']
            if not all(col in df.columns for col in required_cols):
                return
            
            df.to_parquet(cache_path, index=False)
        except Exception:
            # Silently fail - cache is optional
            pass
    
    def clear_expired(self) -> int:
        """Remove all expired cache files. Returns count removed."""
        removed = 0
        for cache_file in self.cache_dir.glob('*.parquet'):
            mtime = cache_file.stat().st_mtime
            age_days = (time.time() - mtime) / 86400
            if age_days > self.max_age_days:
                try:
                    cache_file.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed
    
    def clear_all(self) -> int:
        """Remove all cache files. Returns count removed."""
        removed = 0
        for cache_file in self.cache_dir.glob('*.parquet'):
            try:
                cache_file.unlink()
                removed += 1
            except OSError:
                pass
        return removed
    
    def get_stats(self) -> dict:
        """Get cache statistics."""
        files = list(self.cache_dir.glob('*.parquet'))
        total_size = sum(f.stat().st_size for f in files)
        return {
            'file_count': len(files),
            'total_size_mb': round(total_size / 1024 / 1024, 2),
            'cache_dir': str(self.cache_dir),
        }


# Global cache instance
_cache: Optional[BarCache] = None


def get_cache() -> BarCache:
    """Get or create global cache instance."""
    global _cache
    if _cache is None:
        _cache = BarCache()
    return _cache


def init_cache(cache_dir: Optional[str] = None, max_age_days: int = 30) -> BarCache:
    """Initialize global cache with custom settings."""
    global _cache
    _cache = BarCache(cache_dir, max_age_days)
    return _cache