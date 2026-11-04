"""标准库文档校验：本地链接、默认值、版本及市场资产；不联网。"""
from __future__ import annotations

from collections import Counter
import ast
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


def literal_assignment(path: Path, name: str):
    """解析源码常量，不导入插件或启动宿主。"""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            continue
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "dict":
            return {keyword.arg: ast.literal_eval(keyword.value) for keyword in node.value.keywords}
        return ast.literal_eval(node.value)
    raise ValueError(f"{path.name} 缺少 {name}")


def marked_table(text: str, marker: str, failures: list[str]) -> str:
    match = re.search(rf"<!-- {marker}:start -->(.*?)<!-- {marker}:end -->", text, re.S)
    if not match:
        failures.append(f"缺少配置表标记: {marker}")
        return ""
    return match[1]


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
    rows = re.findall(r"(?m)^\| `([^`]+)` \| `([^`]+)` \| ([^|]+) \|$",
                      marked_table(table, "schema-defaults", failures))
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

    # 独立生图配置不在宿主schema中，直接以源码DEFAULTS核对。
    defaults = literal_assignment(root / "guard_image_settings.py", "DEFAULTS")
    image_rows = re.findall(r"(?m)^\| `([^`]+)` \| `([^`]+)` \|",
                            marked_table(table, "image-defaults", failures))
    image_listed = {}
    for key, default in image_rows:
        if key in image_listed:
            failures.append(f"生图配置表重复字段: {key}")
        try:
            image_listed[key] = json.loads(default)
        except ValueError:
            failures.append(f"生图默认值不是JSON: {key}")
    if set(image_listed) != set(defaults):
        failures.append(f"生图配置表字段差异: {sorted(set(image_listed) ^ set(defaults))}")
    for key in set(image_listed) & set(defaults):
        if json.dumps(image_listed[key]) != json.dumps(defaults[key]):
            failures.append(f"生图默认值过期: {key}")
    native_fields = literal_assignment(root / "guard_native_settings.py", "FIELDS")
    section = re.search(r"## AstrBot 原生字段\n(.*?)\n## AstrBot 原生分段", table, re.S)
    native_rows = re.findall(r"(?m)^\| ([a-z0-9_]+) \| ([a-z0-9_.]+) \|", section[1] if section else "")
    documented_native = dict(native_rows)
    if documented_native != {key: value[0] for key, value in native_fields.items()}:
        failures.append("原生字段及保存路径已变化，请同步配置参考")

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
        image_count = len(literal_assignment(ROOT / "guard_image_settings.py", "DEFAULTS"))
        print(f"[PASS] 文档链接、{count}+{image_count} 项默认值、原生字段、版本、图片及 tests/ 布局")
    sys.exit(bool(errors))
