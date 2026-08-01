"""
Cache module for US Pullback Scanner.
Local caching of downloaded bars in Parquet format for idempotent runs.
"""
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
import pandas as pd


class BarCache:
    """Local cache for downloaded price bars."""
    
    def __init__(
        self,
        cache_dir: str = ".cache/bars",
        max_age_days: int = 30,
        enabled: bool = True
    ):
        """
        Initialize bar cache.
        
        Args:
            cache_dir: Directory to store parquet files
            max_age_days: Maximum age of cache files before expiry
            enabled: Whether caching is enabled
        """
        self.cache_dir = Path(cache_dir)
        self.max_age_seconds = max_age_days * 24 * 3600
        self.enabled = enabled
        
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_cache_path(self, symbol: str, period: str) -> Path:
        """Get cache file path for symbol and period."""
        # Sanitize symbol for filesystem
        safe_symbol = symbol.replace('.', '-').replace('/', '_')
        return self.cache_dir / f"{safe_symbol}_{period}.parquet"
    
    def get(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        """
        Get cached bars for symbol and period.
        
        Args:
            symbol: Yahoo symbol (e.g., 'AAPL')
            period: Period string (e.g., '1mo', '1y')
            
        Returns:
            DataFrame with bars or None if not cached/expired
        """
        if not self.enabled:
            return None
            
        cache_path = self._get_cache_path(symbol, period)
        
        if not cache_path.exists():
            return None
        
        # Check age
        age = time.time() - cache_path.stat().st_mtime
        if age > self.max_age_seconds:
            try:
                cache_path.unlink()
            except OSError:
                pass
            return None
        
        try:
            df = pd.read_parquet(cache_path)
            # Ensure date column is datetime
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
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
            symbol: Yahoo symbol
            period: Period string
            df: DataFrame with Date, Open, High, Low, Close, Volume columns
        """
        if not self.enabled or df is None or len(df) == 0:
            return
            
        cache_path = self._get_cache_path(symbol, period)
        
        try:
            # Ensure Date is string for parquet compatibility
            df_copy = df.copy()
            if 'Date' in df_copy.columns:
                df_copy['Date'] = df_copy['Date'].dt.strftime('%Y-%m-%d')
            df_copy.to_parquet(cache_path, index=False)
        except Exception:
            # Silently ignore cache write failures
            pass
    
    def clear(self, older_than_days: Optional[int] = None):
        """
        Clear cache files.
        
        Args:
            older_than_days: If provided, only delete files older than this many days
        """
        if not self.cache_dir.exists():
            return
            
        cutoff = time.time()
        if older_than_days:
            cutoff -= older_than_days * 24 * 3600
        else:
            cutoff = 0  # Delete all
        
        for cache_file in self.cache_dir.glob("*.parquet"):
            try:
                if cache_file.stat().st_mtime < cutoff:
                    cache_file.unlink()
            except OSError:
                pass
    
    def get_stats(self) -> Dict[str, any]:
        """Get cache statistics."""
        if not self.cache_dir.exists():
            return {'enabled': self.enabled, 'files': 0, 'total_size_mb': 0}
        
        files = list(self.cache_dir.glob("*.parquet"))
        total_size = sum(f.stat().st_size for f in files)
        
        return {
            'enabled': self.enabled,
            'files': len(files),
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'cache_dir': str(self.cache_dir),
        }


# Global cache instance
_cache: Optional['BarCache'] = None


def get_cache() -> BarCache:
    """Get or create global cache instance."""
    global _cache
    if _cache is None:
        from config import get_cache_config
        cache_config = get_cache_config()
        _cache = BarCache(
            cache_dir=cache_config.get('bars_dir', '.cache/bars'),
            max_age_days=cache_config.get('max_age_days', 30),
            enabled=cache_config.get('enabled', True)
        )
    return _cache


def download_bars_with_cache(
    symbols,
    period,
    stderr_path,
    batch=200,
    phase='DOWNLOAD'
):
    """
    Download bars with local caching support.
    
    This wraps the original download_bars to add caching layer.
    """
    from us_pattern_scan import download_bars as original_download_bars
    from logging_utils import get_logger
    
    logger = get_logger()
    cache = get_cache()
    
    if not cache.enabled:
        return original_download_bars(symbols, period, stderr_path, batch, phase)
    
    # Split symbols into cached and uncached
    cached_frames = {}
    uncached_symbols = []
    cache_hits = 0
    cache_misses = 0
    
    for sym in symbols:
        cached = cache.get(sym, period)
        if cached is not None:
            cached_frames[sym] = cached
            cache_hits += 1
        else:
            uncached_symbols.append(sym)
            cache_misses += 1
    
    logger.info(f"Cache: hits={cache_hits} misses={cache_misses} for {len(symbols)} symbols period={period}")
    
    if uncached_symbols:
        # Download uncached symbols
        new_frames, misses = original_download_bars(
            uncached_symbols, period, stderr_path, batch, phase
        )
        
        # Cache newly downloaded frames
        for sym, df in new_frames.items():
            cache.set(sym, period, df)
        
        # Merge with cached frames
        cached_frames.update(new_frames)
    else:
        misses = set()
    
    # Return all frames and combined misses
    return cached_frames, misses


def clear_cache(older_than_days: Optional[int] = None):
    """Clear global cache."""
    cache = get_cache()
    cache.clear(older_than_days)


def get_cache_stats() -> Dict:
    """Get global cache statistics."""
    cache = get_cache()
    return cache.get_stats()