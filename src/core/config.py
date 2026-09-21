"""
文件名: config.py
摘要: 加载 config.toml，验证必填字段，提供 get(key) 点号路径访问。
依赖: tomllib (Python 3.11+ 标准库)
      os, pathlib
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


class Config:
    """配置对象。从 config.toml 加载，支持 get(key) 点号路径访问。"""

    _data: dict[str, Any]
    _config_path: Path

    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is None:
            config_path = self._find_config()
        self._config_path = Path(config_path)
        with open(self._config_path, "rb") as f:
            self._data = tomllib.load(f)
        self._validate()

    # ------------------------------------------------------------------
    # 配置发现
    # ------------------------------------------------------------------

    @staticmethod
    def _find_config() -> Path:
        """按优先级搜索 config.toml。"""
        # 1) 环境变量 MHI_CONFIG
        env_path = os.environ.get("MHI_CONFIG")
        if env_path:
            p = Path(env_path)
            if p.is_file():
                return p

        # 2) 当前工作目录
        cwd_path = Path.cwd() / "config.toml"
        if cwd_path.is_file():
            return cwd_path

        # 3) 相对于本文件: src/core/config.py → 项目根
        script_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
        if script_path.is_file():
            return script_path

        raise FileNotFoundError(
            "config.toml 未找到。设置 MHI_CONFIG 环境变量，"
            "或将 config.toml 放在项目根目录。"
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        required_sections = ["database", "embedding"]
        for section in required_sections:
            if section not in self._data:
                msg = f"config.toml 缺少必填节: [{section}]"
                raise ValueError(msg)

        db = self._data["database"]
        for key in ("host", "port", "name", "user"):
            if key not in db:
                msg = f"config.toml 缺少必填字段: database.{key}"
                raise ValueError(msg)

    # ------------------------------------------------------------------
    # 访问接口
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """点号路径访问。例: get('database.host') → '127.0.0.1'"""
        parts = key.split(".")
        d: Any = self._data
        for part in parts:
            if not isinstance(d, dict) or part not in d:
                return default
            d = d[part]
        return d

    @property
    def database_password(self) -> str:
        """获取数据库密码。

        优先级：
        1. config.toml [database] password 字段（非空字符串）
        2. 环境变量 MHI_DB_PASSWORD
        """
        # 1) 检查 config.toml 中的 password 字段
        pwd = self.get("database.password", "")
        if pwd:
            return pwd

        # 2) 回退到环境变量
        pwd = os.environ.get("MHI_DB_PASSWORD")
        if not pwd:
            raise RuntimeError(
                "数据库密码未设置。请通过以下任一方式设置：\n"
                "1. 在 config.toml [database] 中填写 password 字段\n"
                "2. 设置环境变量 MHI_DB_PASSWORD"
            )
        return pwd

    @property
    def config_path(self) -> Path:
        return self._config_path


# ── 模块级全局单例 ──────────────────────────────────────────

_config: Config | None = None


def load_config(config_path: str | Path | None = None) -> Config:
    """加载/获取全局配置单例。"""
    global _config
    if _config is None:
        _config = Config(config_path)
    return _config


def get_config() -> Config:
    """获取已配置单例。未加载时自动按发现规则加载。"""
    global _config
    if _config is None:
        _config = Config()
    return _config
