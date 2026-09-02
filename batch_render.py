"""batch_render.py —— 批量为每个房间类型渲染 3D-Front 俯视图。

读取 3D_Front_example/ 下的每个设计 JSON，从中解析出该户型包含的房间类型，
然后针对「每个房间类型」各调用一次 threefront_renderer.py（以 Blender 后台模式
`--background` 运行，不弹出 GUI 窗口），把对应房间的俯视渲染图输出到 output/。

与直接运行 threefront_renderer.py 的区别：
  - 本脚本总是以 --background 模式驱动 Blender（渲染完即退出，不保留窗口）；
  - 每次调用只让一个房间类型通过 --room-filter 命中，从而实现「按房间类型分别输出」。

仅依赖当前文件夹内的文件；不读取任何外部目录。
"""

import argparse
import json
import os
import sys

# 把脚本所在目录加入 sys.path，以便 import visualization.threefront_renderer
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
)


def _room_types_of(json_path):
    """从 3D-Front JSON 中按出现顺序提取不重复的房间类型（scene.room[].type）。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    types = []
    for room in data.get("scene", {}).get("room", []):
        t = room.get("type")
        if t and t not in types:
            types.append(t)
    return types


def _build_argv(driver_args, json_path, out_dir, room_type):
    """拼出一次 threefront_renderer 调用的 argv（始终含 --background）。"""
    argv = [
        "--blender-bin", driver_args.blender_bin,
        "--input", os.path.abspath(json_path),
        "--output-dir", out_dir,
        "--dataset", driver_args.dataset,
        "--texture-root", driver_args.texture_root,
        "--category-json", driver_args.category_json,
        # 关键：一次只让一个房间类型命中，达到「按类型分别输出」
        "--room-filter", room_type,
        "--render-engine", driver_args.render_engine,
        "--render-resolution", str(driver_args.render_resolution),
        "--topdown-margin", str(driver_args.topdown_margin),
        "--render-samples", str(driver_args.render_samples),
        "--topdown-scope", driver_args.scope,
        "--background",  # 后台模式：渲染完退出，不弹 GUI
    ]
    if driver_args.no_origin_at_zero:
        argv.append("--no-origin-at-zero")
    if driver_args.no_save_blend:
        argv.append("--no-save-blend")
    return argv


def main():
    p = argparse.ArgumentParser(
        description="批量按房间类型渲染 3D-Front 俯视图（Blender 后台模式）。")
    p.add_argument("--input-dir", default="3D_Front_example",
                   help="含 3D-Front 设计 JSON 的目录（默认 3D_Front_example）。")
    p.add_argument("--output-dir", default="output",
                   help="渲染图输出根目录（默认 output）；每个 JSON 落到 output/<stem>/。")
    p.add_argument("--blender-bin", default=BLENDER_BIN_DEFAULT,
                   help="Blender 可执行文件路径。")
    p.add_argument("--dataset", default=_DATASET_ROOT_DEFAULT,
                   help="3D-FUTURE 数据集根目录（<model_id>/raw_model.obj）。")
    p.add_argument("--texture-root", default=_TEXTURE_ROOT_DEFAULT,
                   help="3D-Front 贴图根目录。")
    p.add_argument("--category-json", default=_CATEGORY_JSON_DEFAULT,
                   help="model_info.json（按 super-category 过滤吊灯等）。")
    p.add_argument("--render-engine", default="cycles",
                   choices=["eevee", "workbench", "cycles"],
                   help="俯视渲染引擎。")
    p.add_argument("--render-resolution", type=int, default=1024,
                   help="渲染图边长（正方形像素）。")
    p.add_argument("--topdown-margin", type=float, default=0.08,
                   help="俯视视野留白比例。")
    p.add_argument("--render-samples", type=int, default=128,
                   help="Cycles 采样数。")
    p.add_argument("--scope", default="scene", choices=["auto", "room", "scene"],
                   help="单类型只一间时三种等价；保持 scene 即可。")
    p.add_argument("--only", default="",
                   help="只渲染这些房间类型（逗号分隔，大小写不敏感）；"
                        "为空则渲染该 JSON 内的全部房间类型。")
    p.add_argument("--no-origin-at-zero", dest="no_origin_at_zero",
                   action="store_true",
                   help="关闭坐标归零（默认每个内容平移到 (0,0) 起）。")
    p.add_argument("--no-save-blend", action="store_true",
                   help="不保存 .blend（仅出 PNG）。")
    args = p.parse_args()

    in_dir = os.path.abspath(args.input_dir)
    if not os.path.isdir(in_dir):
        sys.exit(f"[batch] 输入目录不存在: {in_dir}")

    json_files = sorted(
        os.path.join(in_dir, f) for f in os.listdir(in_dir) if f.endswith(".json"))
    if not json_files:
        sys.exit(f"[batch] 在 {in_dir} 未找到任何 .json")

    only_set = {s.strip().lower() for s in args.only.split(",") if s.strip()}

    jobs = []  # (json_path, room_type, out_subdir)
    for jf in json_files:
        stem = os.path.splitext(os.path.basename(jf))[0]
        out_subdir = os.path.join(os.path.abspath(args.output_dir), stem)
        os.makedirs(out_subdir, exist_ok=True)
        types = _room_types_of(jf)
        for t in types:
            if only_set and t.lower() not in only_set:
                continue
            jobs.append((jf, t, out_subdir))

    if not jobs:
        sys.exit("[batch] 没有可渲染的 (JSON, 房间类型) 组合，退出。")

    print(f"[batch] 共 {len(json_files)} 个 JSON，"
          f"{len(jobs)} 个 (JSON × 房间类型) 渲染任务：")
    for jf, t, _ in jobs:
        print(f"  - {os.path.basename(jf)} :: {t}")

    # 顺序逐个调用 threefront_renderer.main_launcher（每次独立 Blender 后台进程）
    done = 0
    for jf, t, out_subdir in jobs:
        print(f"\n========== [batch] {os.path.basename(jf)} :: {t} ==========")
        argv = _build_argv(args, jf, out_subdir, t)
        ns = make_parser().parse_args(argv)
        try:
            main_launcher(ns)
            done += 1
        except Exception as e:  # 单个失败不影响后续
            import traceback
            print(f"[batch] 警告：{os.path.basename(jf)} :: {t} 渲染失败 -> {e}")
            traceback.print_exc()

    print(f"\n[batch] 完成：{done}/{len(jobs)} 个任务成功。"
          f"渲染图位于 {os.path.abspath(args.output_dir)}/")


if __name__ == "__main__":
    main()
