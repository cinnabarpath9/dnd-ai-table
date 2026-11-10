# coding=utf-8
"""剧本层：骨架 / 台账 / 议程 / 单场简报（决策记录 §5.23）。

    model.py   数据模型 + 校验 + 渲染
    runner.py  把剧本推到牌桌上（世界播种 → 同桌轮 → 战斗）
"""

from .model import (
    Act, Adventure, AdventureError, Agenda, Brief, Decision, Promise, Scene,
    Thread, campaign_guide, campaign_player_goal,
    dm_brief_text, load_adventure, parse_adventure, render_skeleton,
)
from .progress import (
    Beat, CampaignProgress, ProgressError, PromiseProgress, progress_path_for,
)
from .runner import AdventureRunner
from .campaign import (
    Campaign, CampaignError, Chapter, debt_text, debts, load_campaign,
    parse_campaign, previous_chapters,
)
from .journal import JOURNAL_NOTE, build_journal

__all__ = [
    "Adventure", "AdventureError", "AdventureRunner", "Scene", "Brief",
    "Thread", "Promise", "Decision", "Agenda", "Act",
    "load_adventure", "parse_adventure", "render_skeleton", "dm_brief_text",
    "campaign_guide", "campaign_player_goal",
    "CampaignProgress", "PromiseProgress", "Beat", "ProgressError",
    "progress_path_for",
    "Campaign", "CampaignError", "Chapter", "load_campaign", "parse_campaign",
    "previous_chapters", "debts", "debt_text",
    "build_journal", "JOURNAL_NOTE",
]