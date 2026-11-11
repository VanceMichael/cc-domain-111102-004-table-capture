#!/usr/bin/env python3
"""隔离模式运行器。

fixtures 与下载素材属于不可信数据，测试用 ``python3 -I`` 运行以禁用
PYTHONPATH/用户站点包；-I 同时隐含 -P，不会把 cwd 加入 sys.path，
因此这里显式插入可信的项目根目录。从项目根目录执行：

    python3 -I tools/run_tests.py
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

loader = unittest.TestLoader()
suite = loader.discover(str(ROOT / "tests"))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
