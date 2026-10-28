"""假训练脚本(eval fixture):把固定指标写进 $AUTOLAB_OUTPUT_DIR/metrics.json。

harness 回放时不会真的执行它;保持与真实 runner 约定一致的形状。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

out = Path(os.environ.get("AUTOLAB_OUTPUT_DIR", "."))
out.mkdir(parents=True, exist_ok=True)
(out / "metrics.json").write_text(json.dumps({"val_dice": 0.81}), encoding="utf-8")
print("train done")
