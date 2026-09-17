"""eval_harness · 插件注册表

解决开源框架「task/harness 强耦合、扩展难」的痛点：
- 评分器（Grader）、提供者（Provider）、加载器（Loader）均通过注册表解耦，
  新增能力只需 @register_xxx，零侵入。
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Type


class Registry:
    def __init__(self, name: str):
        self.name = name
        self._items: Dict[str, Type] = {}

    def register(self, key: str, cls: Optional[Type] = None):
        if cls is None:  # 支持 @REG.register("x") 装饰器写法
            def deco(c):
                return self.register(key, c)
            return deco
        if key in self._items:
            raise ValueError(f"[{self.name}] 重复注册: {key}")
        self._items[key] = cls
        return cls

    def get(self, key: str):
        if key not in self._items:
            raise KeyError(f"[{self.name}] 未注册的 {key}；可用: {list(self._items)}")
        return self._items[key]

    def available(self):
        return list(self._items)


# 三类插件注册表
GRADERS = Registry("grader")
PROVIDERS = Registry("provider")
LOADERS = Registry("loader")
