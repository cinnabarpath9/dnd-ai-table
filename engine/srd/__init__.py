# coding=utf-8
"""SRD 5.2.1（D&D 2024）数据的采集与读取。

- `fetch.py` 负责从 open5e 拉全量并落盘（一次性、可续抓）
- `store.py` 负责给引擎提供只读查表（怪物/法术/武器…）

数据许可是 CC-BY-4.0（Wizards of the Coast 发布的 SRD 5.2）。
"""

__all__ = ["fetch", "store"]
