"""
tariff/tariff_loader.py
Loads TariffConfig from JSON. Wrapper around core.config.load_tariff_config.
Exists as its own module per the spec repository structure.
"""
from core.config import TariffConfig, load_tariff_config

__all__ = ["TariffConfig", "load_tariff_config"]
