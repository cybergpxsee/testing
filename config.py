"""
Configuration loader for US Pullback Scanner.
Loads settings from config.yaml with environment variable overrides.
"""
import os
from pathlib import Path
from typing import Any, Dict, Optional
import yaml


class Config:
    """Centralized configuration with environment variable overrides."""
    
    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._config:
            self._load_config()
    
    def _load_config(self):
        """Load config from YAML file with environment overrides."""
        config_path = Path(__file__).resolve().parent / 'config.yaml'
        
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = {}
        
        # Apply environment variable overrides
        self._apply_env_overrides()
    
    def _apply_env_overrides(self):
        """Apply environment variable overrides to config."""
        env_mappings = {
            # Liquidity
            'HERMES_MIN_AVG_DOLLAR_VOL_20D': ('liquidity', 'min_avg_dollar_volume_20d', int),
            'HERMES_BAND_HIGH': ('liquidity', 'band_high', int),
            'HERMES_SMALLCAP_AVG_DOLLAR_VOL_30D': ('liquidity', 'smallcap_avg_dollar_volume_30d', int),
            
            # Scan
            'HERMES_SWING_WINDOW': ('scan', 'swing_window', int),
            'HERMES_SHORT_TREND_LOOKBACK': ('scan', 'short_trend_lookback', int),
            'HERMES_LONG_TREND_LOOKBACK': ('scan', 'long_trend_lookback', int),
            'HERMES_MIN_DOUBLE_GAP': ('scan', 'min_double_structure_gap', int),
            'HERMES_WEEK52_LOOKBACK': ('scan', 'week52_lookback', int),
            'HERMES_WEEK52_BONUS_MAX': ('scan', 'week52_proximity_bonus_max', int),
            'HERMES_PULLBACK_20D_FILTER': ('scan', 'pullback_20d_filter', bool),
            'HERMES_MAX_CONFIRM_AGE': ('scan', 'pullback_max_confirm_age_days', int),
            
            # Pullback
            'HERMES_DIRECTION_FILTER_DAYS': ('pullback', 'direction_filter_days', int),
            'HERMES_DIRECTION_FILTER_MIN_PCT': ('pullback', 'direction_filter_min_pct', float),
            'HERMES_MIN_CLOSE_POSITION': ('pullback', 'min_close_position_pct', float),
            
            # Download
            'HERMES_STAGE1_BATCH': ('download', 'batch_size_stage1', int),
            'HERMES_STAGE2_BATCH': ('download', 'batch_size_stage2', int),
            'HERMES_DOWNLOAD_TIMEOUT': ('download', 'timeout', int),
            'HERMES_RETRY_COUNT': ('download', 'retry_count', int),
            
            # Universe
            'HERMES_UNIVERSE_SHARDS': ('universe', 'shard_count', int),
            'HERMES_MAX_SYMBOLS': ('universe', 'max_symbols', int),
            
            # Cache
            'HERMES_CACHE_ENABLED': ('cache', 'enabled', bool),
            'HERMES_CACHE_BARS_DIR': ('cache', 'bars_dir', str),
            'HERMES_CACHE_MAX_AGE': ('cache', 'max_age_days', int),
            
            # Logging
            'HERMES_LOG_LEVEL': ('logging', 'level', str),
            'HERMES_LOG_FILE': ('logging', 'file', str),
            'HERMES_LOG_CONSOLE': ('logging', 'console', bool),
        }
        
        for env_var, (section, key, caster) in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                try:
                    if caster == bool:
                        value = value.lower() in ('1', 'true', 'yes', 'on')
                    else:
                        value = caster(value)
                    self._set_nested(section, key, value)
                except (ValueError, TypeError) as e:
                    print(f"Warning: Invalid env value for {env_var}: {value}, error: {e}")
    
    def _set_nested(self, *keys, value):
        """Set nested dict value."""
        d = self._config
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
    
    def get(self, *keys, default=None):
        """Get nested config value."""
        d = self._config
        for k in keys:
            if isinstance(d, dict):
                d = d.get(k)
            else:
                return default
            if d is None:
                return default
        return d
    
    def get_section(self, section: str) -> Dict[str, Any]:
        """Get entire section as dict."""
        return self._config.get(section, {})


# Global config instance
CONFIG = Config()


# Convenience functions for common configs
def get_liquidity_config() -> Dict[str, Any]:
    return CONFIG.get_section('liquidity')


def get_scan_config() -> Dict[str, Any]:
    return CONFIG.get_section('scan')


def get_pullback_config() -> Dict[str, Any]:
    return CONFIG.get_section('pullback')


def get_download_config() -> Dict[str, Any]:
    return CONFIG.get_section('download')


def get_universe_config() -> Dict[str, Any]:
    return CONFIG.get_section('universe')


def get_cache_config() -> Dict[str, Any]:
    return CONFIG.get_section('cache')


def get_logging_config() -> Dict[str, Any]:
    return CONFIG.get_section('logging')


# For backward compatibility - expose constants as module-level variables
# These will be overridden by config values at runtime
SWING_WINDOW = CONFIG.get('scan', 'swing_window', default=3)
SHORT_TREND_LOOKBACK = CONFIG.get('scan', 'short_trend_lookback', default=30)
LONG_TREND_LOOKBACK = CONFIG.get('scan', 'long_trend_lookback', default=90)
LONG_TERM_TREND_BONUS = CONFIG.get('scan', 'long_term_trend_bonus', default=5)
MIN_DOUBLE_STRUCTURE_GAP = CONFIG.get('scan', 'min_double_structure_gap', default=20)
DOUBLE_STRUCTURE_WIDE_GAP_BONUS = CONFIG.get('scan', 'double_structure_wide_gap_bonus', default=5)
DOUBLE_STRUCTURE_WIDE_GAP_THRESHOLD = CONFIG.get('scan', 'double_structure_wide_gap_threshold', default=60)
DIRECTION_FILTER_DAYS = CONFIG.get('pullback', 'direction_filter_days', default=5)
DIRECTION_FILTER_MIN_PCT = CONFIG.get('pullback', 'direction_filter_min_pct', default=1.0)
WEEK52_PROXIMITY_BONUS_MAX = CONFIG.get('scan', 'week52_proximity_bonus_max', default=15)
WEEK52_LOOKBACK = CONFIG.get('scan', 'week52_lookback', default=252)
PULLBACK_20D_FILTER = CONFIG.get('scan', 'pullback_20d_filter', default=True)
MIN_CLOSE_POSITION_PCT = CONFIG.get('pullback', 'min_close_position_pct', default=0.40)
PULLBACK_MAX_CONFIRM_AGE_DAYS = CONFIG.get('scan', 'pullback_max_confirm_age_days', default=90)