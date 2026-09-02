"""batch_render.py —— 批量为 4 类房间渲染 3D-Front 俯视图（Blender 后台模式）。

目录结构（每次运行 = 一次「实验」）：
    output/
      exp<N>/                                  # 本次运行（N 自动递增）
        LivingDiningRoom/
          <layout_id>/rendered_scene.png       # 仅该房间类型存在时生成
        LivingRoom/
          <layout_id>/rendered_scene.png
        DiningRoom/
          <layout_id>/rendered_scene.png
        Bedroom/
          <layout_id>_masterbedroom/rendered_scene.png
          <layout_id>_secondbedroom/rendered_scene.png
          …（MasterBedroom / SecondBedroom / Bedroom 均归入此类，用 _房间类型 区分）

- 每次运行 batch 计为一次实验：扫描 output/ 下已有的 exp<N> 取最大序号 +1，
  把所有输入 JSON 的房间按 4 类归并到本次实验目录下。
- 始终以 --background 驱动 Blender（渲染完退出，不弹 GUI）。
- 仅依赖当前文件夹内的文件；不读取任何外部目录。
"""

import argparse
import glob
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from visualization.threefront_renderer import (  # noqa: E402
    make_parser,
    main_launcher,
    BLENDER_BIN_DEFAULT,
    _DATASET_ROOT_DEFAULT,
    _TEXTURE_ROOT_DEFAULT,
    _CATEGORY_JSON_DEFAULT,
    _slug,
)

# 四类目标房间类型（输出文件夹名固定为这 4 个）
CATEGORIES = ["LivingDiningRoom", "LivingRoom", "DiningRoom", "Bedroom"]


def _category_of(rtype):
    """3D-Front 房间类型 -> 4 类之一；不属于则返回 None。

    注意顺序：先判 LivingDiningRoom（其字符串内含 'diningroom' 子串，
    必须最先匹配，否则会被误归到 DiningRoom）。
    """
    r = (rtype or "").lower()
    if "livingdiningroom" in r:
        return "LivingDiningRoom"
    if "livingroom" in r:
        return "LivingRoom"
    if "diningroom" in r:
        return "DiningRoom"
    if "bedroom" in r:
        return "Bedroom"
    return None


def _rooms_of(json_path):
    """返回该 JSON 的房间列表 [(room_index, room_type), ...]。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [(i, room.get("type", "Unknown"))
            for i, room in enumerate(data.get("scene", {}).get("room", []))]


def _next_exp_index(output_root):
    """扫描 output_root 下 exp<N> 目录，返回下一个实验序号（从 1 开始）。"""
    if not os.path.isdir(output_root):
        return 1
    mx = 0
    for name in os.listdir(output_root):
        if name.startswith("exp"):
            digits = name[len("exp"):]
            if digits.isdigit():
                mx = max(mx, int(digits))
    return mx + 1


def _build_argv(driver, json_path, out_dir, room_filter):
    argv = [
        "--blender-bin", driver.blender_bin,
        "--input", os.path.abspath(json_path),
        "--output-dir", out_dir,
        "--dataset", driver.dataset,
        "--texture-root", driver.texture_root,
        "--category-json", driver.category_json,
        "--room-filter", room_filter,       # 精确房间类型，保证只渲染该间
        "--render-engine", driver.render_engine,
        "--render-resolution", str(driver.render_resolution),
        "--topdown-margin", str(driver.topdown_margin),
        "--render-samples", str(driver.render_samples),
        "--topdown-scope", "scene",
        "--background",                     # 后台渲染，不弹 GUI
    ]
    if driver.no_origin_at_zero:
        argv.append("--no-origin-at-zero")
    if not driver.save_blend:
        argv.append("--no-save-blend")
    return argv


def _rename_to_rendered_scene(out_dir):
    """把 threefront 输出的 *_topdown.png 重命名为 rendered_scene.png。"""
    pngs = glob.glob(os.path.join(out_dir, "*topdown*.png"))
    if not pngs:
        return None
    target = os.path.join(out_dir, "rendered_scene.png")
    src = pngs[0]
    if os.path.abspath(src) != os.path.abspath(target):
        shutil.move(src, target)
    return target


def main():
    p = argparse.ArgumentParser(
        description="批量按 4 类房间渲染 3D-Front 俯视图（每次运行 = 一次实验）。")
    p.add_argument("--input-dir", default="3D_Front_example",
                   help="含 3D-Front 设计 JSON 的目录（默认 3D_Front_example）。")
    p.add_argument("--output-dir", default="output",
                   help="渲染图根目录（默认 output）；每次运行落到 output/exp<N>/。")
    p.add_argument("--blender-bin", default=BLENDER_BIN_DEFAULT,
                   help="Blender 可执行文件路径。")
    p.add_argument("--dataset", default=_DATASET_ROOT_DEFAULT,
                   help="3D-FUTURE 数据集根目录（<model_id>/raw_model.obj）。")
    p.add_argument("--texture-root", default=_TEXTURE_ROOT_DEFAULT,
                   help="3D-Front 贴图根目录。")
    p.add_argument("--category-json", default=_CATEGORY_JSON_DEFAULT,
                   help="model_info.json（按 super-category 过滤吊灯等）。")
    p.add_argument("--render-engine", default="cycles",
                   choices=["eevee", "workbench", "cycles"], help="渲染引擎。")
    p.add_argument("--render-resolution", type=int, default=1024,
                   help="渲染图边长（正方形像素）。")
    p.add_argument("--topdown-margin", type=float, default=0.08,
                   help="俯视视野留白比例。")
    p.add_argument("--render-samples", type=int, default=128,
                   help="Cycles 采样数。")
    p.add_argument("--no-origin-at-zero", dest="no_origin_at_zero",
                   action="store_true",
                   help="关闭坐标归零（默认每个内容平移到 (0,0) 起）。")
    p.add_argument("--save-blend", action="store_true",
                   help="保存 .blend（默认仅出 PNG，不保存 .blend）。")
    args = p.parse_args()

    in_dir = os.path.abspath(args.input_dir)
    if not os.path.isdir(in_dir):
        sys.exit(f"[batch] 输入目录不存在: {in_dir}")

    json_files = sorted(
        os.path.join(in_dir, f) for f in os.listdir(in_dir) if f.endswith(".json"))
    if not json_files:
        sys.exit(f"[batch] 在 {in_dir} 未找到任何 .json")

    out_root = os.path.abspath(args.output_dir)
    os.makedirs(out_root, exist_ok=True)
    exp_idx = _next_exp_index(out_root)
    exp_dir = os.path.join(out_root, f"exp{exp_idx}")
    os.makedirs(exp_dir, exist_ok=True)
    # 始终创建 4 个类别文件夹（可能部分为空，符合「四个文件夹」结构）
    for cat in CATEGORIES:
        os.makedirs(os.path.join(exp_dir, cat), exist_ok=True)

    # 枚举所有 (json, 房间) 任务
    jobs = []  # (json_path, rtype, target_dir)
    for jf in json_files:
        stem = os.path.splitext(os.path.basename(jf))[0]
        for ri, rtype in _rooms_of(jf):
            cat = _category_of(rtype)
            if cat is None:
                continue
            if cat == "Bedroom":
                # 用 _房间类型 区分 master / second / 其它，避免互相覆盖
                folder = f"{stem}_{_slug(rtype)}"
            else:
                folder = stem
            target = os.path.join(exp_dir, cat, folder)
            os.makedirs(target, exist_ok=True)
            jobs.append((jf, rtype, target))

    if not jobs:
        sys.exit("[batch] 没有属于 4 类房间的可渲染任务，退出。")

    print(f"[batch] 实验 exp{exp_idx}：{len(json_files)} 个 JSON，"
          f"{len(jobs)} 个 (JSON × 房间) 渲染任务")
    for jf, rtype, target in jobs:
        print(f"  - {os.path.basename(jf)} :: {rtype} -> {os.path.relpath(target)}")

    done = 0
    for jf, rtype, target in jobs:
        print(f"\n========== [batch] exp{exp_idx} :: {os.path.basename(jf)} "
              f":: {rtype} ==========")
        argv = _build_argv(args, jf, target, rtype)  # 精确类型，只渲染该间
        ns = make_parser().parse_args(argv)
        try:
            main_launcher(ns)
            rendered = _rename_to_rendered_scene(target)
            if rendered:
                done += 1
                print(f"[batch]      -> {os.path.relpath(rendered)}")
            else:
                print(f"[batch]      警告：未在 {target} 找到渲染产物")
        except Exception as e:
            import traceback
            print(f"[batch] 警告：{os.path.basename(jf)} :: {rtype} 渲染失败 -> {e}")
            traceback.print_exc()

    print(f"\n[batch] 实验 exp{exp_idx} 完成：{done}/{len(jobs)} 张渲染图 "
          f"位于 {exp_dir}")


if __name__ == "__main__":
    main()
