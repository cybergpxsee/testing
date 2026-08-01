"""
Logging module for US Pullback Scanner.
Replaces append_log with standard logging module.
"""
import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logging(
    log_file: Optional[str] = None,
    level: str = "INFO",
    console: bool = True,
    fmt: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
) -> logging.Logger:
    """
    Setup logging with file and console handlers.
    
    Args:
        log_file: Path to log file (optional)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        console: Whether to log to console
        fmt: Log format string
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger('pullback_scan')
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    formatter = logging.Formatter(fmt)
    
    # File handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    return logger


# Global logger instance
_logger: Optional[logging.Logger] = None


def get_logger() -> logging.Logger:
    """Get or create the global logger."""
    global _logger
    if _logger is None:
        _logger = setup_logging()
    return _logger


def log_info(message: str):
    """Log info message."""
    get_logger().info(message)


def log_warning(message: str):
    """Log warning message."""
    get_logger().warning(message)


def log_error(message: str, exc_info: bool = False):
    """Log error message."""
    get_logger().error(message, exc_info=exc_info)


def log_debug(message: str):
    """Log debug message."""
    get_logger().debug(message)


# For backward compatibility with append_log pattern
def append_log(logger: logging.Logger, message: str):
    """Legacy append_log compatibility."""
    logger.info(message)