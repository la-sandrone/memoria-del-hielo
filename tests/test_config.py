"""
测试: core/config.py
"""

from pathlib import Path
from src.core.config import Config


def test_config_find():
    """验证 config 能在项目根找到 config.toml（相对路径）。"""
    # 不传路径 → 自动查找（需在项目目录下运行）
    cfg = Config()
    assert cfg.get("database.host") == "127.0.0.1"
    assert cfg.get("database.port") == 9002
    assert cfg.get("database.name") == "memoria_del_hielo"
    # model 值带 local:/remote: 前缀，前缀是环境选择，断言只锚定模型名
    assert cfg.get("embedding.model").endswith("BAAI/bge-m3")
    assert cfg.get("embedding.dimension") == 1024


def test_config_get_default():
    cfg = Config()
    assert cfg.get("nonexistent.key", "fallback") == "fallback"
    assert cfg.get("") is None


def test_config_get_nested():
    cfg = Config()
    import_section = cfg.get("import")
    assert import_section is not None
    assert isinstance(import_section, dict)
    assert "default_bucket_book" in import_section
