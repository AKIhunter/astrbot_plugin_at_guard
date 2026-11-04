"""标准库文档校验：本地链接、默认值、版本及市场资产；不联网。"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def prose(text: str) -> str:
    """忽略代码围栏，避免把代码样例当成文档链接或标题。"""
    return re.sub(r"(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$", "", text)


def anchors(text: str) -> set[str]:
    result: set[str] = set()
    seen: Counter[str] = Counter()
    for heading in re.findall(r"(?m)^#{1,6} +(.+?) *#* *$", prose(text)):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        suffix = f"-{seen[slug]}" if seen[slug] else ""
        seen[slug] += 1
        result.add(slug + suffix)
    result.update(re.findall(r'(?:id|name)=["\']([^"\']+)["\']', text))
    return result


def check(root: Path) -> list[str]:
    failures = []
    docs = [root / "README.md", root / "CHANGELOG.md", *sorted((root / "docs").rglob("*.md"))]
    for path in docs:
        if not path.is_file():
            failures.append(f"缺少 {path.relative_to(root)}")
            continue
        text = path.read_text(encoding="utf-8")
        if any(line.rstrip() != line for line in text.splitlines()):
            failures.append(f"{path.relative_to(root)}: 行末空白")
        targets = re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", prose(text))
        targets += re.findall(r'(?:src|href)=["\']([^"\']+)["\']', prose(text))
        for raw in targets:
            target = urlsplit(raw.strip().split(' "', 1)[0].strip("<>"))
            if target.scheme or target.netloc:
                continue
            destination = (path.parent / unquote(target.path)).resolve() if target.path else path
            if not destination.is_relative_to(root):
                failures.append(f"{path.relative_to(root)}: 链接越出仓库 {raw}")
            elif not destination.exists():
                failures.append(f"{path.relative_to(root)}: 无效路径 {raw}")
            elif target.fragment and destination.suffix == ".md":
                if unquote(target.fragment) not in anchors(destination.read_text(encoding="utf-8")):
                    failures.append(f"{path.relative_to(root)}: 无效锚点 {raw}")

    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    table = (root / "docs/configuration.md").read_text(encoding="utf-8")
    rows = re.findall(r"(?m)^\| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$", table)
    listed = {}
    for key, default, title in rows:
        if key in listed:
            failures.append(f"配置表重复字段: {key}")
        try:
            listed[key] = (json.loads(default), title.strip())
        except ValueError:
            failures.append(f"配置表默认值不是 JSON: {key}")
    if set(listed) != set(schema):
        failures.append(f"配置表字段差异: {sorted(set(listed) ^ set(schema))}")
    for key in set(listed) & set(schema):
        value, title = listed[key]
        # bool 与 int 在 Python 中可能相等；JSON 序列化比较保留类型。
        if json.dumps(value, ensure_ascii=False) != json.dumps(schema[key]["default"], ensure_ascii=False):
            failures.append(f"配置默认值过期: {key}")
        if title != schema[key]["description"]:
            failures.append(f"配置标题过期: {key}")

    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")
    required = {"name", "display_name", "short_desc", "desc", "version", "author",
                "repo", "help", "astrbot_version", "social_link", "tags"}
    keys = set(re.findall(r"(?m)^([a-z_]+):", metadata))
    if required - keys:
        failures.append(f"缺少元数据字段: {sorted(required - keys)}")
    version_match = re.search(r"(?m)^version: (\d+\.\d+\.\d+)$", metadata)
    if not version_match:
        failures.append("metadata.version 不是语义版本")
    else:
        version = version_match[1]
        for path in (root / "README.md", root / "CHANGELOG.md", root / "guard_webui.py"):
            if version not in path.read_text(encoding="utf-8"):
                failures.append(f"{path.name} 缺少当前版本 {version}")

    for filename, dimensions in (("logo.png", (256, 256)), (".github/img/Banner.png", (1200, 360))):
        asset = root / filename
        header = asset.read_bytes()[:24] if asset.is_file() else b""
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            failures.append(f"缺少有效 PNG: {filename}")
        elif struct.unpack(">II", header[16:24]) != dimensions:
            failures.append(f"图片尺寸不匹配: {filename}")
    if list(root.glob("test_*.py")) or not list((root / "tests").glob("test_*.py")):
        failures.append("测试应集中在 tests/")
    return failures


if __name__ == "__main__":
    errors = check(ROOT)
    for error in errors:
        print(f"[FAIL] {error}")
    if not errors:
        count = len(json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8")))
        print(f"[PASS] 文档链接、{count} 项配置默认值、版本、图片及 tests/ 布局")
    sys.exit(bool(errors))
