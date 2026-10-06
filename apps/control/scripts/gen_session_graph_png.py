#!/usr/bin/env python
"""从真实代码生成会话图,供 architecture.html 01 节使用。

- session_graph_mermaid():graph.get_graph().draw_mermaid() 官方导出
- PNG:draw_mermaid_png() 经 mermaid.ink 渲染(需联网),暗色主题,输出到
  apps/web/public/generated/session-graph.png,并把 base64 内嵌进
  architecture.html 的 <img id="officialGraphImg">(Next dev 不热加载
  public 新文件,内嵌才能立即生效且离线可看)

再生成:../../.venv/bin/python scripts/gen_session_graph_png.py
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autolab_control.agent_session import _build_session_graph

WEB = Path(__file__).resolve().parents[2] / "web"
OUT_PNG = WEB / "public" / "generated" / "session-graph.png"
HTML = WEB / "public" / "architecture.html"

FRONTMATTER = {"config": {
    "theme": "dark",
    "themeVariables": {
        "primaryColor": "#16202b",
        "primaryTextColor": "#dce5f0",
        "primaryBorderColor": "#4f8ef7",
        "lineColor": "#7ba4f7",
        "textColor": "#c3cddb",
        "background": "#0d1319",
    },
}}


def main() -> None:
    app = _build_session_graph(lambda s: {}, lambda s: {}, lambda s: "tools")

    mermaid = app.get_graph().draw_mermaid()
    print("---- mermaid 源码(官方导出) ----")
    print(mermaid)

    png = app.get_graph().draw_mermaid_png(
        frontmatter_config=FRONTMATTER, background_color="transparent")
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    OUT_PNG.write_bytes(png)
    print(f"---- PNG 已写入 {OUT_PNG}({len(png)} bytes)----")

    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()
    html = HTML.read_text(encoding="utf-8")
    html, n = re.subn(r'(id="officialGraphImg"[^>]*src=")[^"]*(")',
                      rf"\g<1>{data_uri}\g<2>", html, count=1)
    if n != 1:
        raise SystemExit("architecture.html 里没找到 officialGraphImg 的 src,未回写")
    HTML.write_text(html, encoding="utf-8")
    print(f"---- base64 已内嵌 architecture.html(替换 {n} 处)----")


if __name__ == "__main__":
    main()
