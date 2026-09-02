#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blender_renderer.py —— 3D-FRONT 家具摆放到 Blender 空房间场景

工作方式（双模式单文件）：
  - 用「系统 Python」运行时：import bpy 会失败 → 进入「启动器」模式，
    自动调用 Blender 打开 .blend 墙壁/门窗场景并加载本脚本。
  - 在 Blender 内部运行时：import bpy 成功 → 进入「构建器」模式，
    读取 layout json，把每件家具的 raw_model.obj 导入并摆好。

由于启动时没有使用 -b（后台模式），Blender 会以 GUI 窗口形式打开并停留，
最终呈现为「已打开的窗口 + 已摆好的家具」。

对外暴露 launch_blender() 供 run.py 调用；也可作为独立脚本由 Blender 直接运行。
"""

import os
import sys
import argparse
from pathlib import Path

# ======================================================================
# 默认路径（仍可用命令行覆盖）
# ======================================================================

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Blender 可执行文件（你提供的；可在命令行用 --blender-bin 覆盖）
BLENDER_BIN_DEFAULT = "/home/ky/Desktop/blender-5.1.2-linux-x64/blender"

# 空房间场景 .blend（注意真实文件夹名是 "2Bedrooms_3"，带 s）
_BLEND_SCENE_DEFAULT = _PROJECT_ROOT / "data" / "blender_scenes" / "empty_house" / "2Bedrooms_3" / "2Bedrooms_3.blend"

# 布局 json（家具 id + 平面坐标 + 旋转）
_LAYOUT_JSON_DEFAULT = _PROJECT_ROOT / "data" / "layout_result_SA.json"

# 3D-FUTURE 数据集根目录（每个家具一个文件夹：<furniture_id>/raw_model.obj）
# 调用方必须提供 --dataset
_DATASET_ROOT_DEFAULT = "/TODO/替换为你的/3D-FUTURE-model/根目录"

# 坐标映射：json 的 (cx, cy) 是地平面坐标（米）。不同 blend 场景竖直轴不同，
# 可用 --coord 选择。键名表示「地平面两轴 → 世界 X/Y」，第三轴为竖直轴=0。
COORD_MAPS = {
    "xy": lambda cx, cy: (cx, cy, 0.0),   # Z 轴向上（默认）
    "xz": lambda cx, cy: (cx, 0.0, cy),   # Y 轴向上
    "yx": lambda cx, cy: (cy, cx, 0.0),
    "yz": lambda cx, cy: (0.0, cy, cx),
}

# ======================================================================
# 模式判定
# ======================================================================
try:
    import bpy          # 在 Blender 内部才能导入成功
    IN_BLENDER = True
except ImportError:
    IN_BLENDER = False


# ======================================================================
# 命令行参数定义（启动器与构建器共用同一个解析器）
# ======================================================================
def make_parser():
    p = argparse.ArgumentParser(
        description="把 layout json 里的家具摆进 Blender 空房间场景。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # —— 启动器专用（仅系统 Python 用，不会转发给构建器）——
    p.add_argument("--blender-bin", default=BLENDER_BIN_DEFAULT,
                   help="Blender 可执行文件路径")
    # —— 场景与数据 ——
    p.add_argument("--blend", default=str(_BLEND_SCENE_DEFAULT),
                   help="空房间 .blend 场景文件（墙壁/门窗）")
    p.add_argument("--layout", default=str(_LAYOUT_JSON_DEFAULT),
                   help="布局 json 文件（含 items 列表）")
    p.add_argument("--dataset", default=_DATASET_ROOT_DEFAULT,
                   help="3D-FUTURE 数据集根目录（<furniture_id>/raw_model.obj）")
    # —— 朝向 / 尺寸微调 ——
    p.add_argument("--scale", type=float, default=1.0,
                   help="模型整体缩放（3D-FUTURE 通常为米制=1.0）")
    p.add_argument("--pre-rot", type=float, nargs=3, default=[0.0, 0.0, 0.0],
                   metavar=("X", "Y", "Z"),
                   help="模型预旋转（弧度，绕 X/Y/Z），用于修正侧躺模型，如 -1.5708 0 0")
    p.add_argument("--rot-sign", type=float, default=1.0,
                   help="json 旋转角的方向符号，朝向反了改成 -1")
    p.add_argument("--rot-axis", choices=["Z", "Y", "X"], default="Z",
                   help="绕哪根轴施加 json 里的旋转角（竖直轴）")
    p.add_argument("--rot-offset", type=float, default=0.0,
                   help="全局朝向基准偏移（角度，度）。模型正面与 layout "
                        "约定相反时填 180，会与每件家具的 rotation 叠加。")
    p.add_argument("--no-drop", action="store_true",
                   help="关闭「把模型压到地面 Z=0」")
    p.add_argument("--roughness", type=float, default=1.0,
                   help="固定导入模型的 Principled BSDF Roughness（3D-FUTURE 的 "
                        "mtl Ns 会算成 ~0.0 导致镜面失真，默认 1.0 修正；"
                        "设为负值可关闭此修正，保留导入器原始值）")
    p.add_argument("--coord", choices=list(COORD_MAPS.keys()), default="xy",
                   help="地平面 (cx,cy) 到世界坐标的映射方式")
    # —— 输出 / 保存 / 俯视渲染 ——
    p.add_argument("--output-dir", default=None,
                   help="输出目录：带家具的 .blend 与俯视图将保存到此处（不会改动原始 "
                        "empty_room.blend）。默认与 layout json 同目录。")
    p.add_argument("--no-save-blend", action="store_true",
                   help="不另存 .blend（默认会另存 <stem>_furnished.blend 到 --output-dir）。")
    p.add_argument("--no-render-topdown", action="store_true",
                   help="不渲染俯视图（默认会渲染 <stem>_topdown.png）。")
    p.add_argument("--render-engine", choices=["eevee", "workbench", "cycles"],
                   default="cycles", help="俯视渲染使用的引擎（默认 cycles 真实感）。")
    p.add_argument("--render-resolution", type=int, default=1920,
                   help="俯视渲染图的长宽（正方形，单位像素）。")
    p.add_argument("--topdown-margin", type=float, default=0.1,
                   help="俯视相机正交视野相对房间尺寸的留白比例（0.1 表示四周各留 10%）。")
    p.add_argument("--render-samples", type=int, default=256,
                   help="Cycles 采样数（仅 cycles 生效，越高越干净越慢）。")
    p.add_argument("--no-render-denoise", action="store_true",
                   help="关闭 Cycles 降噪（默认开启 OpenImageDenoise 去噪）。")
    p.add_argument("--transparent-bg", action="store_true",
                   help="俯视图背景透明（默认浅灰背景，便于直接查看）。")
    return p


# ======================================================================
# 启动器模式（系统 Python）：调起 Blender GUI
# ======================================================================
def main_launcher(args):
    import subprocess

    blend = os.path.abspath(args.blend)
    if not os.path.exists(blend):
        sys.exit(f"[启动器] 找不到 .blend 场景文件: {blend}")
    if not os.path.exists(os.path.abspath(args.layout)):
        sys.exit(f"[启动器] 找不到 layout json: {os.path.abspath(args.layout)}")

    # 关键：不写 -b，Blender 会开 GUI 窗口并停留。
    # 只把「构建器相关」的参数转发给脚本（--blender-bin / --blend 不需要）。
    cmd = [
        args.blender_bin,
        blend,
        "--python", os.path.abspath(__file__),
        "--",
        "--layout", os.path.abspath(args.layout),
        "--dataset", os.path.abspath(args.dataset),
        "--scale", str(args.scale),
        "--pre-rot", str(args.pre_rot[0]), str(args.pre_rot[1]), str(args.pre_rot[2]),
        "--rot-sign", str(args.rot_sign),
        "--rot-axis", args.rot_axis,
        "--rot-offset", str(args.rot_offset),
        "--coord", args.coord,
        "--roughness", str(args.roughness),
    ]
    if args.no_drop:
        cmd.append("--no-drop")

    # 输出目录与俯视渲染相关参数（仅构建器使用）
    if args.output_dir:
        cmd += ["--output-dir", os.path.abspath(args.output_dir)]
    if args.no_save_blend:
        cmd.append("--no-save-blend")
    if args.no_render_topdown:
        cmd.append("--no-render-topdown")
    cmd += [
        "--render-engine", args.render_engine,
        "--render-resolution", str(args.render_resolution),
        "--topdown-margin", str(args.topdown_margin),
        "--render-samples", str(args.render_samples),
    ]
    if args.no_render_denoise:
        cmd.append("--no-render-denoise")
    if args.transparent_bg:
        cmd.append("--transparent-bg")

    print("[启动器] 命令:", " ".join(cmd))
    print("[启动器] 正在启动 Blender（GUI 窗口将打开）...")
    subprocess.run(cmd)


def launch_blender(layout_json: str, dataset: str, blend: str | None = None,
                   blender_bin: str = BLENDER_BIN_DEFAULT, scale: float = 1.0,
                   pre_rot=(0.0, 0.0, 0.0), rot_sign: float = 1.0,
                   rot_axis: str = "Z", rot_offset: float = 0.0,
                   no_drop: bool = False, coord: str = "xy",
                   roughness: float = 1.0, output_dir: str | None = None,
                   no_save_blend: bool = False, no_render_topdown: bool = False,
                   render_engine: str = "cycles", render_resolution: int = 1920,
                   topdown_margin: float = 0.1, render_samples: int = 256,
                   no_render_denoise: bool = False, transparent_bg: bool = False) -> None:
    """Convenience wrapper used by run.py to launch Blender with a layout.

    Args:
        layout_json: path to layout_result_SA.json (produced by generation)
        dataset: path to the 3D-FUTURE dataset root
        blend: path to the empty-room .blend scene (defaults to _BLEND_SCENE_DEFAULT)
        output_dir: directory where the furnished .blend and the top-down PNG go
            (the original empty_room.blend is never modified).
        no_save_blend: if True, do not save a copy of the furnished scene.
        no_render_topdown: if True, skip the top-down render.
        render_engine: "eevee" | "workbench" | "cycles".
        render_resolution: square image edge length in pixels.
        topdown_margin: orthographic view padding ratio around the room.
    """
    args = argparse.Namespace(
        blender_bin=blender_bin,
        blend=blend or str(_BLEND_SCENE_DEFAULT),
        layout=layout_json,
        dataset=dataset,
        scale=scale,
        pre_rot=pre_rot,
        rot_sign=rot_sign,
        rot_axis=rot_axis,
        rot_offset=rot_offset,
        no_drop=no_drop,
        coord=coord,
        roughness=roughness,
        output_dir=output_dir,
        no_save_blend=no_save_blend,
        no_render_topdown=no_render_topdown,
        render_engine=render_engine,
        render_resolution=render_resolution,
        topdown_margin=topdown_margin,
        render_samples=render_samples,
        no_render_denoise=no_render_denoise,
        transparent_bg=transparent_bg,
    )
    main_launcher(args)


# ======================================================================
# 构建器模式（Blender 内部）：导入并摆放家具
# ======================================================================
def _world_bbox(objects):
    import math
    from mathutils import Vector
    min_co = Vector((math.inf, math.inf, math.inf))
    max_co = Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for o in objects:
        if o.type != 'MESH':
            continue
        found = True
        for corner in o.bound_box:
            co = o.matrix_world @ Vector(corner)
            for i in range(3):
                min_co[i] = min(min_co[i], co[i])
                max_co[i] = max(max_co[i], co[i])
    return (min_co, max_co) if found else None


def _fix_material_roughness(objs, roughness: float):
    """把导入模型所有 Principled BSDF 的 Roughness 设为固定值。

    3D-FUTURE 的 .mtl 把 Ns（高光指数）设得很高，Blender 的 OBJ 导入器
    会把它换算成接近 0.0 的 Roughness，导致模型变成全镜面反光、材质失真。
    这里统一固定回指定值（默认 1.0）以正确展现固有色与纹理。
    """
    for o in objs:
        if o.type != 'MESH':
            continue
        for slot in o.material_slots:
            mat = slot.material
            if mat is None or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    node.inputs["Roughness"].default_value = roughness


def _import_furniture(item, cfg, scene_coll):
    """导入一件家具并摆好，返回根 Empty；缺失则打印警告并返回 None。"""
    import math
    from mathutils import Euler

    furniture_id = item.get("furniture_id")
    if not furniture_id:
        print(f"[构建器] 跳过（无 furniture_id）: {item.get('id')}")
        return None

    obj_path = os.path.join(cfg["dataset"], furniture_id, "raw_model.obj")
    if not os.path.exists(obj_path):
        print(f"[构建器] 警告：找不到模型，已跳过 -> {obj_path}")
        return None

    # Blender 5.x 的 OBJ 导入器会把物体放进新子集合，
    # 因此用 bpy.data.objects（含所有集合的物体）来捕获，而非 scene_coll.objects
    before = set(bpy.data.objects)

    # 用 global_scale 处理缩放，避免事后缩放带来的位移偏移
    bpy.ops.wm.obj_import(filepath=obj_path, global_scale=cfg["scale"])

    new_objs = [o for o in bpy.data.objects if o not in before]
    if not new_objs:
        print(f"[构建器] 警告：导入未产生物体 -> {furniture_id}")
        return None

    # (1.5) 修正材质 Roughness：3D-FUTURE 的 .mtl 把 Ns 设得很高，
    # 导入器会算成 ~0.0（全镜面），此处固定回指定值以正确展现材质。
    if cfg.get("roughness") is not None:
        _fix_material_roughness(new_objs, cfg["roughness"])

    # (1) 模型预旋转（修正可能的「侧躺」）
    # 注意：mathutils.Euler 不支持 '+' 运算符，必须逐分量相加
    pre = cfg["pre_rot"]  # (x, y, z) 弧度
    for o in new_objs:
        r = o.rotation_euler
        o.rotation_euler = Euler((r.x + pre[0], r.y + pre[1], r.z + pre[2]))

    # (2) 压到地面：在世界坐标下把所有子物体抬到 Z=0
    if cfg["drop_to_floor"]:
        bbox = _world_bbox(new_objs)
        if bbox is not None and bbox[0].z != 0.0:
            for o in new_objs:
                o.location.z -= bbox[0].z

    # (3) 用 Empty 作根，把所有导入物体挂到它下面（处理多部件模型）
    root = bpy.data.objects.new(item.get("id", furniture_id), None)
    scene_coll.objects.link(root)

    for o in new_objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = root
    bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

    # (4) 摆放：位置 + 旋转
    pos = item.get("position", {})
    cx, cy = pos.get("cx", 0.0), pos.get("cy", 0.0)
    root.location = cfg["coord_map"](cx, cy)

    # 全局基准偏移（度→弧度）与每件家具的 rotation 叠加，
    # 用于修正「模型正面与 layout 约定相反（差 180°）」的情况。
    base = math.radians(cfg["rot_offset"])
    rot_rad = math.radians(float(item.get("rotation", 0.0))) * cfg["rot_sign"] + base
    axis = cfg["rot_axis"]
    if axis == 'Y':
        root.rotation_euler = Euler((0.0, rot_rad, 0.0))
    elif axis == 'X':
        root.rotation_euler = Euler((rot_rad, 0.0, 0.0))
    else:  # Z
        root.rotation_euler = Euler((0.0, 0.0, rot_rad))

    return root


def _resolve_output_dir(args):
    """输出目录：优先用 --output-dir，否则落到 layout json 同级目录。"""
    if args.output_dir:
        out_dir = Path(os.path.abspath(args.output_dir))
    else:
        out_dir = Path(os.path.abspath(args.layout)).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _save_furnished_blend(out_dir: Path, stem: str):
    """把摆好家具的当前场景另存为新 .blend，绝不改动原始 empty_room。

    另存后 bpy.data.filepath 会指向新文件，因此用户在 GUI 里 Ctrl+S 也只会
    写到 output 副本，不会污染原始空房间场景。
    `stem` 须在 save_as 之前取好（即原始 empty_room 的名字），避免被污染。
    """
    out_blend = out_dir / f"{stem}_furnished.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))
    print(f"[构建器] 已另存带家具场景 -> {out_blend}")


def _pick_engine(args):
    """根据 --render-engine 选出跨版本兼容的引擎标识符。"""
    scene = bpy.context.scene
    valid = [i.identifier for i in scene.render.bl_rna.properties['engine'].enum_items]
    if args.render_engine == "eevee":
        eevee = [e for e in valid if "EEVEE" in e]
        return eevee[0] if eevee else "BLENDER_EEVEE"
    if args.render_engine == "workbench":
        return "BLENDER_WORKBENCH"
    return "CYCLES"


def _setup_topdown_lighting(scene, center, up_unit, h_ext, tmp_objects):
    """搭一套「俯视专用」灯光，并返回需清理的临时对象。

    关键点（修复之前正头顶硬光导致的扁平/发暗）：
      - 一盏 45° 斜射 SUN：产生柔和方向阴影，给家具有立体感；
      - 一盏 HEMISPHERE 半球补光：均匀填充，避免死角发黑；
      - World 环境光：再提供一层全局环境照明，保证材质不被压黑。
    这些对象均为临时创建，渲染后清理，不污染保存的 .blend。
    """
    from mathutils import Vector

    # 1) 斜 45° 主光（SUN）
    sun_data = bpy.data.lights.new("TopDownKeySun", type='SUN')
    sun_data.energy = 2.5
    sun_data.angle = 0.15  # 柔和阴影边缘
    sun = bpy.data.objects.new("TopDownKeySun", sun_data)
    scene.collection.objects.link(sun)
    # 在水平中心上方、沿一条水平轴偏移，形成 45° 斜射
    offset = up_unit * (h_ext * 4.0 + 10.0) + Vector((1.0, 1.0, 0.0)) * (h_ext * 4.0 + 10.0)
    sun.location = Vector(center) + offset
    sun.rotation_euler = (Vector(center) - sun.location).to_track_quat('-Z', 'Y').to_euler()
    tmp_objects.append(sun)

    # 2) 柔和补光（AREA，覆盖整间房、向下打，替代 5.x 已移除的 HEMI）
    # Blender 5.x 移除了 HEMI 类型，合法类型为 POINT/SUN/SPOT/AREA；
    # AREA 灯面积大、柔和无方向死角，最适合做俯视填充光。
    area_data = bpy.data.lights.new("TopDownFill", type='AREA')
    area_data.shape = 'RECTANGLE'
    area_data.size = h_ext * 1.5       # 覆盖整间房宽度
    area_data.size_y = h_ext * 1.5      # 覆盖整间房进深
    area_data.energy = 1.2
    area_data.spread = 1.0             # 1.0 = 全向，补光更均匀
    area = bpy.data.objects.new("TopDownFill", area_data)
    scene.collection.objects.link(area)
    area.location = Vector(center) + up_unit * (h_ext + 5.0)
    area.rotation_euler = (Vector(center) - area.location).to_track_quat('-Z', 'Y').to_euler()
    tmp_objects.append(area)

    # 3) World 环境光（即便场景自带灯也能提亮整体）
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("TopDownWorld")
        scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.9, 0.9, 0.92, 1.0)
        bg.inputs["Strength"].default_value = 1.0


def _configure_cycles(args):
    """为 Cycles 设置采样 / 降噪 / GPU，提升精度与速度。"""
    scene = bpy.context.scene
    cyc = scene.cycles
    cyc.samples = args.render_samples
    cyc.max_bounces = 8
    # 降噪：默认开启 OpenImageDenoise；--no-render-denoise 关闭。
    # Blender 5.x 把 denoiser 设置收进 view_layer，这里逐属性防御性设置。
    try:
        cyc.use_denoising = True
        if not args.no_render_denoise:
            try:
                cyc.denoiser = 'OPENIMAGEDENOISE'
            except Exception:
                pass
        else:
            try:
                cyc.denoiser = 'NONE'
            except Exception:
                cyc.use_denoising = False
    except Exception:
        pass
    # 部分版本降噪放在 view_layer 上
    try:
        vlayer = bpy.context.view_layer
        if hasattr(vlayer, "cycles") and hasattr(vlayer.cycles, "use_denoising"):
            vlayer.cycles.use_denoising = not args.no_render_denoise
    except Exception:
        pass

    # 自动启用 GPU（可用时大幅提速且不影响精度）
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        prefs.refresh_devices()
        devs = prefs.devices
        if devs:
            for d in devs:
                d.use = True
            cyc.device = 'GPU'
            print("[构建器] Cycles 启用 GPU 渲染。")
    except Exception:
        cyc.device = 'CPU'
        print("[构建器] Cycles 使用 CPU 渲染。")


def _render_topdown(out_dir: Path, args, stem: str):
    """渲染一张「布局中心正上方、向下俯视、含全部房间」的高精度图片。

    做法：
      - 计算整个场景（墙体 + 家具）的世界包围盒；
      - 包围盒三轴中范围最小的即为竖直方向，另两轴为水平方向；
      - 在水平中心正上方放一台正交相机朝下拍，ortho_scale 取较大水平尺寸 × 余量，
        保证整间房入镜；
      - 默认 Cycles 路径追踪 + 斜射主光/AREA 补光/环境光 + 降噪，材质光影真实。
    渲染前已完成 _save_furnished_blend，故这些临时对象不会进入保存的 .blend；
    渲染结束统一清理，并尽量把引擎/World 还原，保持 GUI 整洁。
    """
    import math
    from mathutils import Vector

    scene = bpy.context.scene

    chosen_engine = _pick_engine(args)
    old_engine = scene.render.engine
    scene.render.engine = chosen_engine
    if chosen_engine == "CYCLES":
        _configure_cycles(args)

    bbox = _world_bbox(bpy.data.objects)
    if bbox is None:
        print("[构建器] 警告：场景没有可渲染的网格，跳过俯视渲染。")
        scene.render.engine = old_engine
        return
    min_co, max_co = bbox

    ext = Vector((max_co.x - min_co.x, max_co.y - min_co.y, max_co.z - min_co.z))
    axes = ['X', 'Y', 'Z']
    vert = min(axes, key=lambda a: ext[ord(a) - 88])  # 范围最小者为竖直轴
    horiz = [a for a in axes if a != vert]
    center = (min_co + max_co) / 2
    h_ext = max(ext[ord(a) - 88] for a in horiz)
    if h_ext <= 0:
        h_ext = 1.0

    up_unit = {'X': Vector((1, 0, 0)), 'Y': Vector((0, 1, 0)),
               'Z': Vector((0, 0, 1))}[vert]

    tmp_objects = []
    _setup_topdown_lighting(scene, center, up_unit, h_ext, tmp_objects)

    cam_data = bpy.data.cameras.new("TopDownCam")
    cam = bpy.data.objects.new("TopDownCam", cam_data)
    scene.collection.objects.link(cam)
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = h_ext * (1.0 + args.topdown_margin)
    cam_data.clip_start = 0.01
    cam_data.clip_end = 1e6

    # 相机放在水平中心正上方，朝正下方（target）拍
    target = Vector(center)
    cam.location = Vector(center) + up_unit * (h_ext * 4.0 + 10.0)
    direction = target - cam.location
    cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    scene.camera = cam  # 设为活动相机，便于在 GUI 里重渲
    tmp_objects.append(cam)

    # 背景：干净浅色，或透明（--transparent-bg 用于合成）
    scene.render.film_transparent = bool(args.transparent_bg)
    if not args.transparent_bg:
        if scene.world is not None:
            bg = scene.world.node_tree.nodes.get("Background")
            if bg is not None:
                bg.inputs["Color"].default_value = (0.95, 0.95, 0.95, 1.0)

    scene.render.resolution_x = args.render_resolution
    scene.render.resolution_y = args.render_resolution
    scene.render.image_settings.file_format = 'PNG'

    out_png = out_dir / f"{stem}_topdown.png"
    scene.render.filepath = str(out_png)

    print(f"[构建器] 开始俯视渲染（引擎={chosen_engine}, 采样={args.render_samples}）…")
    bpy.ops.render.render(write_still=True)
    print(f"[构建器] 俯视渲染 -> {out_png}")

    # 清理临时灯/相机，并把引擎还原（保存的 .blend 已在渲染前完成，不受影响）
    for o in tmp_objects:
        if o in bpy.data.objects:
            bpy.data.objects.remove(o, do_unlink=True)
    scene.render.engine = old_engine
    scene.render.film_transparent = False


def main_builder(args):
    import json

    print(f"[构建器] layout  = {os.path.abspath(args.layout)}")
    print(f"[构建器] dataset = {os.path.abspath(args.dataset)}")

    with open(os.path.abspath(args.layout), "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    print(f"[构建器] 共 {len(items)} 件家具")

    cfg = {
        "dataset": os.path.abspath(args.dataset),
        "scale": args.scale,
        "pre_rot": tuple(args.pre_rot),
        "rot_sign": args.rot_sign,
        "rot_axis": args.rot_axis,
        "rot_offset": args.rot_offset,
        "drop_to_floor": not args.no_drop,
        "coord_map": COORD_MAPS[args.coord],
        "roughness": args.roughness,
    }

    scene_coll = bpy.context.scene.collection
    placed, skipped = 0, 0
    for item in items:
        root = _import_furniture(item, cfg, scene_coll)
        if root is not None:
            placed += 1
        else:
            skipped += 1

    bpy.context.view_layer.update()
    print(f"[构建器] 完成：已摆放 {placed} 件，跳过 {skipped} 件。")

    # 保存 / 俯视渲染（均落进 output 目录，不改原始 empty_room）
    # 在 save_as 之前取好原始 stem，避免 bpy.data.filepath 被污染后命名错乱。
    src_stem = Path(bpy.data.filepath).stem if bpy.data.filepath else "scene"
    out_dir = _resolve_output_dir(args)
    if not args.no_save_blend:
        _save_furnished_blend(out_dir, src_stem)
    if not args.no_render_topdown:
        try:
            _render_topdown(out_dir, args, src_stem)
        except Exception as exc:  # 渲染失败不应阻断 GUI 打开
            import traceback
            print(f"[构建器] 警告：俯视渲染失败 -> {exc}")
            traceback.print_exc()
            print("[构建器] 场景仍可用，可在 Blender 窗口内手动渲染。")

    print("[构建器] 场景已就绪，可在 Blender 窗口中查看。")


# ======================================================================
# 获取「脚本参数」
# ======================================================================
def _script_argv():
    """
    返回应当交给脚本 argparse 的参数列表。

    Blender 3.6 会截断 '--' 之前的内容，脚本 sys.argv 只含 '--' 之后部分；
    Blender 5.x 把整条命令行（含 blend 文件名 / --python / main.py）都传入
    sys.argv，因此这里统一只取 '--' 之后的部分，跨版本都安全。
    """
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


# ======================================================================
# 入口（作为 Blender --python 脚本时由 Blender 内部执行）
# ======================================================================
if __name__ == "__main__":
    args = make_parser().parse_args(_script_argv())
    if IN_BLENDER:
        main_builder(args)
    else:
        main_launcher(args)
