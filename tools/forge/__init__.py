# coding=utf-8
"""剧本锻造台：把「用户自己的素材」变成本项目能跑的剧本。

    contract.py     草稿 → adventure JSON（补默认值）+ doctor（能不能跑）
    from_markdown.py  固定模板（中文 Markdown）→ 草稿
    from_novel.py     小说 txt → 粗稿（启发式，留着让人/模型补）
    cli.py            doctor / md / novel / playtest

**边界**：这里不内置、不下载、不分发任何第三方剧本；只处理用户自己给的文件，
产物也只落在用户自己的目录里。版权风险留在使用者那一侧。
"""