#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_furniture_model.py

读取一个 3D-Front 布局 json 文件和一个 model_info.json 文件，
对布局中每个家具提取 jid / category / sourceCategoryId，按每行一个写入清单文件，
再以 jid 与 sourceCategoryId 分别与 model_info.json 中的 model_id 做比对，
分别统计两者的对应数量并列出具体对应关系（哪些对得上、哪些对不上）。

仅访问当前文件夹内的文件。用法示例：
    python3 compare_furniture_model.py \
        --layout 3D_Front_example/0a8d471a-2587-458a-9214-586e003e9cf9.json \
        --model_info model_info.json
"""

import argparse
import json
import os
import sys


# 当某个字段缺失时使用的占位符
MISSING = "(missing)"


def load_json(path):
    """读取并解析 json 文件，出错时给出清晰提示后退出。"""
    if not os.path.isfile(path):
        sys.exit("错误：找不到文件 -> %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        sys.exit("错误：文件不是合法 json -> %s\n%s" % (path, e))


def extract_furniture(layout):
    """
    从布局数据中提取家具列表。
    兼容两种结构：
      - 顶层直接是 list
      - 顶层是 dict，家具在 ['furniture'] 字段
    返回 list[dict]，每个 dict 含 jid / category / sourceCategoryId。
    """
    if isinstance(layout, dict):
        furn = layout.get("furniture", [])
    elif isinstance(layout, list):
        furn = layout
    else:
        sys.exit("错误：无法识别的布局结构（既不是 dict 也不是 list）。")

    if not isinstance(furn, list):
        sys.exit("错误：furniture 字段不是列表。")

    items = []
    for raw in furn:
        if not isinstance(raw, dict):
            continue
        items.append({
            "jid": raw.get("jid", MISSING),
            "category": raw.get("category", MISSING),
            "sourceCategoryId": raw.get("sourceCategoryId", MISSING),
        })
    return items


def load_model_ids(model_info):
    """读取 model_info.json，返回其中所有 model_id 的集合，并记录重复情况。"""
    if not isinstance(model_info, list):
        sys.exit("错误：model_info.json 顶层应为 list。")
    ids = []
    for item in model_info:
        if isinstance(item, dict) and "model_id" in item and item["model_id"] is not None:
            ids.append(item["model_id"])
    id_set = set(ids)
    return id_set, len(ids)


def write_listing(items, id_set, out_path):
    """
    把每个家具一行写入清单文件，并在文件开头写入表头与列含义说明。
    各纵列含义如下：
      列1 (序号 / index)        : 家具在布局文件中的出现顺序编号（从 0001 起）。
      列2 (jid)                 : 家具实例的唯一标识符，用于和 model_info.json 的 model_id 比对。
      列3 (category)            : 布局文件中该家具的类别名称，部分条目缺失，记为 (missing)。
      列4 (sourceCategoryId)    : 家具的来源分类 id，部分条目缺失，记为 (missing)。
      列5 (jid匹配model_info)   : 该 jid 是否出现在 model_info.json 的 model_id 集合中（是/否/(缺失)）。
      列6 (sourceCategoryId匹配model_info): 该 sourceCategoryId 是否出现在 model_id 集合中（是/否/(缺失)）。
    数据行以制表符 \\t 分隔，便于用表格软件或 pandas 读取。
    """
    header_cols = [
        "序号(index)", "jid", "category", "sourceCategoryId",
        "jid匹配model_info", "sourceCategoryId匹配model_info",
    ]
    legend = [
        "# 3D-Front 布局家具清单（每行一个家具，并标注与 model_info.json 的匹配结果）",
        "# 列含义：",
        "#   列1 序号(index)                 - 家具在布局文件中的出现顺序编号（从 0001 起）",
        "#   列2 jid                         - 家具实例的唯一标识符，用于和 model_info.json 的 model_id 比对",
        "#   列3 category                    - 布局文件中该家具的类别名称，缺失记为 (missing)",
        "#   列4 sourceCategoryId            - 家具的来源分类 id，缺失记为 (missing)",
        "#   列5 jid匹配model_info           - 该 jid 是否出现在 model_info.json 的 model_id 中：是 / 否 / (缺失)",
        "#   列6 sourceCategoryId匹配model_info - 该 sourceCategoryId 是否出现在 model_id 中：是 / 否 / (缺失)",
        "# 数据行以制表符(TAB)分隔",
    ]

    def mark(val):
        if val == MISSING:
            return "(缺失)"
        return "是" if val in id_set else "否"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(legend) + "\n")
        f.write("\t".join(header_cols) + "\n")
        for idx, it in enumerate(items, 1):
            f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (
                "%04d" % idx,
                it["jid"],
                it["category"],
                it["sourceCategoryId"],
                mark(it["jid"]),
                mark(it["sourceCategoryId"]),
            ))
    print("已生成家具清单文件：%s（%d 条家具 + 表头/说明）" % (out_path, len(items)))


def build_stat(field, items, id_set):
    """
    统计某个字段（'jid' 或 'sourceCategoryId'）与 model_id 的对应关系。
    返回统计结果字典。
    """
    # 所有出现过的取值（含重复），以及是否都缺失
    values_all = [it[field] for it in items if it[field] != MISSING]
    unique_vals = sorted(set(values_all))
    missing_entries = sum(1 for it in items if it[field] == MISSING)

    matched = sorted(set(v for v in unique_vals if v in id_set))
    unmatched = sorted(set(v for v in unique_vals if v not in id_set))

    # 反查：每个对得上的取值对应哪些 category（仅用于信息展示）
    matched_detail = {}
    for it in items:
        v = it[field]
        if v in id_set and v != MISSING:
            matched_detail.setdefault(v, set()).add(it["category"])

    return {
        "field": field,
        "total_entries": len(items),
        "missing_entries": missing_entries,
        "occurrences": len(values_all),
        "unique": len(unique_vals),
        "matched": matched,
        "unmatched": unmatched,
        "matched_detail": matched_detail,
    }


def fmt_list(values, limit=None):
    """把列表格式化成字符串，可限制显示条数。"""
    if not values:
        return "（无）"
    if limit is not None and len(values) > limit:
        shown = values[:limit]
        return "\n  ".join(shown) + "\n  ... 其余 %d 项省略" % (len(values) - limit)
    return "\n  ".join(values)


def write_report(stats, id_total, id_unique, out_path):
    """写详细的对应关系报告文件。"""
    lines = []
    lines.append("=" * 70)
    lines.append("model_info.json 比对报告")
    lines.append("=" * 70)
    lines.append("比较对象：3D-Front 布局文件中的 jid / sourceCategoryId")
    lines.append("          vs  model_info.json 中的 model_id")
    lines.append("model_info 中的 model_id 总数（含重复）: %d" % id_total)
    lines.append("model_info 中的唯一 model_id 数        : %d" % id_unique)
    lines.append("")

    # 每字段匹配汇总表
    lines.append("匹配结果汇总（按唯一取值计）：")
    lines.append("  %-20s %10s %10s %10s %10s" % (
        "字段", "唯一取值", "对应上", "对应不上", "字段缺失"))
    for st in stats:
        lines.append("  %-20s %10d %10d %10d %10d" % (
            st["field"], st["unique"],
            len(st["matched"]), len(st["unmatched"]), st["missing_entries"]))
    lines.append("")

    for st in stats:
        field = st["field"]
        lines.append("-" * 70)
        lines.append("字段 %s 与 model_id 的比对" % field)
        lines.append("-" * 70)
        lines.append("布局中家具条目总数            : %d" % st["total_entries"])
        lines.append("缺失该字段的家具条目数        : %d" % st["missing_entries"])
        lines.append("该字段出现次数（去 missing）  : %d" % st["occurrences"])
        lines.append("该字段唯一取值数              : %d" % st["unique"])
        lines.append("")
        lines.append("对应上的唯一取值数（在 model_info 中存在）: %d" % len(st["matched"]))
        lines.append("对应不上的唯一取值数（model_info 中不存在）: %d" % len(st["unmatched"]))
        lines.append("")
        lines.append("[对应上的取值]")
        if st["matched"]:
            for v in st["matched"]:
                cats = sorted(c for c in st["matched_detail"].get(v, set()) if c != MISSING)
                cat_str = ("（category: %s）" % ", ".join(cats)) if cats else "（category 缺失）"
                lines.append("  %s  %s" % (v, cat_str))
        else:
            lines.append("  （无）")
        lines.append("")
        lines.append("[对应不上的取值]")
        lines.append(fmt_list(st["unmatched"], limit=60))
        lines.append("")

    text = "\n".join(lines)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("已生成比对报告文件：%s" % out_path)
    return text


def main():
    parser = argparse.ArgumentParser(
        description="比对 3D-Front 布局中的 jid / sourceCategoryId 与 model_info.json 的 model_id"
    )
    parser.add_argument(
        "--layout",
        default="3D_Front_example/0a8d471a-2587-458a-9214-586e003e9cf9.json",
        help="3D-Front 布局 json 文件地址",
    )
    parser.add_argument(
        "--model_info",
        default="model_info.json",
        help="model_info.json 文件地址",
    )
    parser.add_argument(
        "--outdir",
        default="output",
        help="输出文件所在目录（默认 output，自动创建）",
    )
    args = parser.parse_args()

    # 读取数据
    layout = load_json(args.layout)
    model_info = load_json(args.model_info)

    items = extract_furniture(layout)
    if not items:
        sys.exit("错误：布局文件中未提取到任何家具条目。")

    id_set, id_total = load_model_ids(model_info)

    # 准备输出目录与文件名
    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(args.layout))[0]
    listing_path = os.path.join(args.outdir, "furniture_listing_model_%s.txt" % base)
    report_path = os.path.join(args.outdir, "furniture_match_report_model_%s.txt" % base)

    # 1) 家具清单（每行一个，并标注与 model_info 的匹配结果）
    write_listing(items, id_set, listing_path)

    # 2) 比对统计
    stats = [
        build_stat("jid", items, id_set),
        build_stat("sourceCategoryId", items, id_set),
    ]
    write_report(stats, id_total, len(id_set), report_path)

    # 3) 控制台摘要
    print("\n" + "=" * 70)
    print("摘要")
    print("=" * 70)
    print("model_info 唯一 model_id 数: %d" % len(id_set))
    for st in stats:
        print("-" * 70)
        print("字段 %s：" % st["field"])
        print("  家具条目总数=%d，缺失该字段=%d，唯一取值=%d" % (
            st["total_entries"], st["missing_entries"], st["unique"]))
        print("  对应上(唯一)=%d，对应不上(唯一)=%d" % (
            len(st["matched"]), len(st["unmatched"])))
    print("=" * 70)


if __name__ == "__main__":
    main()
