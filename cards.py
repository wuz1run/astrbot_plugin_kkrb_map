"""图片卡片与 QQ 官方 Markdown 指令文字链。"""

from pathlib import Path
from urllib.parse import quote

from .kkrb import MapPassword

PASSWORD_TEMPLATE = Path(__file__).with_name("password_card.html").read_text(encoding="utf-8")
QQ_OFFICIAL = {"qq_official", "qq_official_webhook"}


def password_card(items: list[MapPassword], date: str, stale: bool, source: bool) -> tuple[dict, dict]:
    updated = next((item.updated for item in items if item.updated), "")
    data = {
        "items": [{"name": item.name, "password": item.password} for item in items],
        "date": date, "updated": updated, "stale": stale, "source": source,
    }
    # 固定的版式边距、64px 卡片和 10px 行距；裁掉浏览器视口多余留白。
    rows = (len(items) + 1) // 2
    height = 144 + 74 * rows + (49 if stale else 0) + (20 if updated or source else 0)
    options = {
        "type": "jpeg", "quality": 92, "full_page": False,
        "clip": {"x": 0, "y": 0, "width": 540, "height": height},
    }
    return data, options


def command_menu(
    title: str, rows: list[list[tuple[str, str]]], *, prefix: str, interactive: bool, note: str = "",
) -> str:
    lines = [f"## {title}" if interactive else title]
    for row in rows:
        entries = []
        for label, command in row:
            command = prefix + command
            if interactive and len(command) <= 100 and len(label) <= 100:
                entries.append(f'<qqbot-cmd-input text="{quote(command, safe="")}" show="{quote(label, safe="")}" />')
            else:
                entries.append(command.replace("<", "＜").replace(">", "＞") if interactive else command)
        if entries:
            lines.append("　·　".join(entries))
    if note:
        lines.append(note)
    if interactive and rows:
        lines.append("点选填入指令，再点发送。带 … 的入口可以补充关键词。")
    return "\n\n".join(lines) if interactive else "\n".join(lines)
