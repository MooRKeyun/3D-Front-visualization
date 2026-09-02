#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
threefront_renderer.py —— 从原始 3D-Front JSON 直接生成「每个房间的 top2down」渲染图

与 blender_renderer.py 的区别：
  - 数据源是原始 3D-Front 设计 JSON（含 scene / mesh / material / furniture），
    不再依赖预先做好的空房间 .blend，也不再依赖 layout_result_SA.json。
  - 按用户需求：只建「地板 + 家具」，**不建墙 / 门 / 窗 / 天花板 / 踢脚线等**；
    家具里**跳过「吊灯」类**（含其他 ceiling lamp 语义）。
  - 地板材质取自 3D-Front 的 material（纯色 / 可选贴图）；
    家具材质取自 3D-FUTURE 的 raw_model.obj（导入即带 .mtl 材质）。
  - 坐标体系：原始 3D-Front 为 **Y 轴向上（Y-up）**，Blender 原生为
    **Z 轴向上（Z-up）**。转换方法分两处（均做分量重映射 (x,y,z)->(x,-z,y)，
    不用旋转矩阵）：
      * 家具等非单位子变换：在 _compose_matrix 里直接重映射 pos/scale/四元数，
        使其整体转入 Z-up（旋转经四元数共轭，不会像「左乘矩阵」那样被转歪）；
      * 地板 Floor 的子变换通常是单位阵，重映射对它无效，故在 _build_floor_mesh
        里**直接重映射顶点坐标**，使地板落到 X–Y 平面、朝上(+Z)。
    俯视图沿 -Z 轴朝下拍，得到房间平面俯视。
  - 输出：**每个（被筛选出的）房间一张 top2down PNG**，文件名按「房间类型」标注，
    形如 <json_stem>_<RoomType>_<idx>_topdown.png；另可保存 .blend 供检查。

双模式（同 blender_renderer.py）：
  - 系统 Python 运行 → 进入「启动器」，默认以 **GUI 窗口**方式打开 Blender 并停留
    （不写 -b，渲染完窗口不关闭，便于在窗口里查看/手动重渲）；
    传 --background 则改用后台(-b)批量渲染、渲染完自动退出。
  - Blender 内部运行 → 进入「构建器」，解析 JSON 并建场景/渲染。

默认行为（按需求定制）：
  - --input 默认指向示例布局 0a8d471a-2587-458a-9214-586e003e9cf9.json；
  - --room-filter 默认 "bedroom"，只摆放 / 渲染卧室类房间（MasterBedroom、
    SecondBedroom、KidsBedroom、Bedroom 等，子串匹配，大小写不敏感）。

注意：3D-FUTURE 数据集根目录、贴图根目录、category 映射文件当前均为占位符，
      请在测试时通过命令行传入真实路径（必填 --dataset）。
"""

import os
import sys
import json
import argparse
from pathlib import Path

# ======================================================================
# 默认路径（均可用命令行覆盖；以下为占位符，测试时替换）
# ======================================================================
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Blender 可执行文件
BLENDER_BIN_DEFAULT = "/home/ky/Desktop/blender-5.1.2-linux-x64/blender"

# 3D-Front 设计 JSON 输入：默认指向示例布局（你指定的 0a8d471a…json）。
# 传入文件或目录均可；可经 --input 覆盖。
_INPUT_DEFAULT = str(_PROJECT_ROOT / "3D_Front_example" /
                     "0a9f5311-49e1-414c-ba7b-b42a171459a3.json")

# 输出目录：top2down 图片与（可选）整屋 .blend 落此处
_OUTPUT_DEFAULT = str(_PROJECT_ROOT / "output" / "top2down")

# 3D-FUTURE 数据集根目录（每个家具 <model_id>/raw_model.obj）。占位符！
_DATASET_ROOT_DEFAULT = "/TODO/替换为你的/3D-FUTURE-model/根目录"

# 可选：3D-Front 贴图根目录（material['texture'] 非空的图片在此查找）。占位符！
_TEXTURE_ROOT_DEFAULT = "/TODO/替换为你的/3D-Front-texture/根目录"

# 3D-FUTURE model_info.json（model_id -> category / super-category），
# 用于识别「吊灯/照明」等家具类型并取消放置。默认指向本仓库内的 model_info.json，
# 不提供时无法按类别过滤（仅能用 --skip-model-ids 显式跳过）。
_CATEGORY_JSON_DEFAULT = "model_info.json"

# 默认跳过的家具语义（子串匹配，大小写不敏感）
# 覆盖中英文常见「吊灯/吊灯类」表述：吊灯、chandelier、ceiling lamp、
# pendant（Pendant Light）、ceiling light。
_SKIP_CATEGORIES_DEFAULT = "吊灯,chandelier,ceiling lamp,pendant,ceiling light, Lighting"

# 仅保留的 mesh 类型：地板。其余（墙/门窗/天花/踢脚线等）一律跳过。
_KEEP_MESH_TYPES = {"Floor"}


# ======================================================================
# 模式判定
# ======================================================================
try:
    import bpy          # 在 Blender 内部才能导入成功
    IN_BLENDER = True
except ImportError:
    IN_BLENDER = False


# ======================================================================
# 命令行参数（启动器与构建器共用同一个解析器）
# ======================================================================
def make_parser():
    p = argparse.ArgumentParser(
        description="从 3D-Front JSON 生成每个房间的 top2down 渲染图（仅地板+家具）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # —— 启动器专用 ——
    p.add_argument("--blender-bin", default=BLENDER_BIN_DEFAULT,
                   help="Blender 可执行文件路径")
    p.add_argument("--background", action="store_true",
                   help="后台模式(-b)批量渲染，渲染完自动退出（默认不写 -b，"
                        "以 GUI 窗口打开并保持不关闭，便于查看/手动重渲）。")
    # —— 输入 / 输出 ——
    p.add_argument("--input", default=_INPUT_DEFAULT,
                   help="3D-Front 设计 JSON 文件，或包含多个 JSON 的目录")
    p.add_argument("--output-dir", default=_OUTPUT_DEFAULT,
                   help="输出目录：top2down PNG 与（可选）.blend 落此处")
    # —— 房间筛选（默认只处理卧室）——
    p.add_argument("--room-filter", default="bedroom",
                   help="只处理房间类型包含这些子串的房间（逗号分隔，大小写不敏感）。"
                        "默认 'bedroom' 仅摆放/渲染卧室类房间（MasterBedroom、"
                        "SecondBedroom、KidsBedroom、Bedroom 等）。填空字符串('') "
                        "则处理全部房间。")
    # —— 数据根（占位符，测试时替换）——
    p.add_argument("--dataset", default=_DATASET_ROOT_DEFAULT,
                   help="3D-FUTURE 数据集根目录（<model_id>/raw_model.obj）")
    p.add_argument("--texture-root", default=_TEXTURE_ROOT_DEFAULT,
                   help="3D-Front 贴图根目录（material['texture'] 非空时查找图片）")
    p.add_argument("--category-json", default=_CATEGORY_JSON_DEFAULT,
                   help="3D-FUTURE model_info.json（model_id -> category），用于按类跳过家具；"
                        "若传入目录则自动在其中查找 model_info*.json")
    # —— 家具过滤 ——
    p.add_argument("--skip-categories", default=_SKIP_CATEGORIES_DEFAULT,
                   help="按类别子串跳过的家具（逗号分隔，大小写不敏感）")
    p.add_argument("--skip-model-ids", default="",
                   help="显式跳过的 3D-FUTURE model_id（逗号分隔），无需 category 文件")
    # —— 家具导入微调 ——
    p.add_argument("--furniture-scale", type=float, default=1.0,
                   help="家具整体额外缩放（3D-FUTURE 已米制，通常 1.0）")
    p.add_argument("--roughness", type=float, default=1.0,
                   help="导入家具 Principled BSDF 的 Roughness 固定值（修正 3D-FUTURE "
                        "镜面失真，默认 1.0；设负值关闭修正保留原始值）")
    p.add_argument("--pre-rot", type=float, nargs=3, default=[0.0, 0.0, 0.0],
                   metavar=("X", "Y", "Z"),
                   help="家具预旋转（弧度，绕 X/Y/Z），修正侧躺模型")
    # —— 是否建墙体/天花等（默认全关，只保留地板+家具）——
    p.add_argument("--keep-mesh-types", default="Floor",
                   help="保留的 mesh 类型（逗号分隔）；默认仅 Floor。"
                        "填 'Floor,WallInner,Ceiling' 等可一并保留其它类型。")
    # —— 输出控制 ——
    p.add_argument("--no-save-blend", action="store_true",
                   help="不保存整屋 .blend（默认会保存 <stem>_allrooms.blend）")
    p.add_argument("--no-render", action="store_true",
                   help="不渲染 top2down（仅建场景并保存 .blend，便于调试）")
    # —— 俯视渲染参数 ——
    p.add_argument("--render-engine", choices=["eevee", "workbench", "cycles"],
                   default="cycles", help="俯视渲染引擎（默认 cycles 真实感）")
    p.add_argument("--render-resolution", type=int, default=1024,
                   help="俯视渲染图边长（正方形，像素）")
    p.add_argument("--topdown-margin", type=float, default=0.08,
                   help="俯视正交视野相对房间尺寸的留白比例（0.08=四周各留 8%）")
    p.add_argument("--render-samples", type=int, default=128,
                   help="Cycles 采样数（越高越干净越慢）")
    p.add_argument("--no-render-denoise", action="store_true",
                   help="关闭 Cycles 降噪")
    p.add_argument("--transparent-bg", action="store_true",
                   help="背景透明（默认浅灰背景）")
    # —— 俯视渲染范围 ——
    p.add_argument("--topdown-scope", choices=["auto", "room", "scene"],
                   default="auto",
                   help="俯视渲染范围：'auto'(默认)=按保留房间数自动，总渲染成一张整屋图；"
                        "'room'=每个保留房间各出一张；'scene'=所有保留房间合一张整屋图。"
                        "auto 下输入整屋(多房间)→合一张，输入单房间(如 masterbedroom)→只那一张。")
    p.add_argument("--no-origin-at-zero", dest="origin_at_zero",
                   action="store_false",
                   help="关闭坐标归零（默认每个渲染内容都平移到 (0,0) 起始的正坐标系）。")
    return p


# ======================================================================
# 启动器模式（系统 Python）：后台调起 Blender
# ======================================================================
def main_launcher(args):
    import subprocess

    inp = os.path.abspath(args.input)
    if not os.path.exists(inp):
        sys.exit(f"[启动器] 找不到输入: {inp}")

    # 默认 GUI 模式：不写 -b，Blender 开窗口并保持不关闭；
    # 仅当显式 --background 时才用 -b 后台批量渲染（渲染完自动退出）。
    cmd = [args.blender_bin]
    if args.background:
        cmd.append("-b")
    cmd += [
        "--python", os.path.abspath(__file__),
        "--",
        "--input", inp,
        "--output-dir", os.path.abspath(args.output_dir),
        "--room-filter", args.room_filter,
        "--dataset", os.path.abspath(args.dataset),
        "--texture-root", os.path.abspath(args.texture_root),
        "--category-json", os.path.abspath(args.category_json),
        "--skip-categories", args.skip_categories,
        "--skip-model-ids", args.skip_model_ids,
        "--furniture-scale", str(args.furniture_scale),
        "--roughness", str(args.roughness),
        "--pre-rot", *[str(x) for x in args.pre_rot],
        "--keep-mesh-types", args.keep_mesh_types,
        "--render-engine", args.render_engine,
        "--render-resolution", str(args.render_resolution),
        "--topdown-margin", str(args.topdown_margin),
        "--render-samples", str(args.render_samples),
        "--topdown-scope", args.topdown_scope,
    ]
    if args.no_save_blend:
        cmd.append("--no-save-blend")
    if args.no_render:
        cmd.append("--no-render")
    if args.no_render_denoise:
        cmd.append("--no-render-denoise")
    if args.transparent_bg:
        cmd.append("--transparent-bg")
    if not args.origin_at_zero:
        cmd.append("--no-origin-at-zero")

    print("[启动器] 命令:", " ".join(cmd))
    if args.background:
        print("[启动器] 后台模式(-b)：渲染完将自动退出。")
    else:
        print("[启动器] 正在启动 Blender（GUI 窗口将打开并保持不关闭）…")

    # 准备本次运行的日志文件（output_dir/logs/run_<时间戳>.log），
    # 同时把 Blender 的全部输出（含 C 层 io.obj 警告）实时 tee 到控制台与日志。
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(os.path.abspath(args.output_dir), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"run_{ts}.log")

    print(f"[启动器] 本次运行日志 -> {log_path}")
    print("[启动器] 正在用 Blender 后台渲染…")

    env = os.environ.copy()
    env["THREEFRONT_LOG"] = log_path   # 告知构建器日志路径（仅用于独立运行场景）

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=env, text=True, bufsize=1)
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("[启动器] 命令: " + " ".join(cmd) + "\n")
        lf.write(f"[启动器] 时间: {ts}\n")
        lf.flush()
        # 实时把 Blender 输出同时写控制台与日志
        for line in proc.stdout:
            sys.stdout.write(line)
            lf.write(line)
            lf.flush()
    rc = proc.wait()
    print(f"[启动器] Blender 退出码: {rc}；完整日志见 {log_path}")


# ======================================================================
# 构建器模式（Blender 内部）
# ======================================================================
def _world_bbox(objects):
    """计算给定物体的世界坐标包围盒（仅 MESH）。"""
    from mathutils import Vector
    import math
    min_co = Vector((math.inf, math.inf, math.inf))
    max_co = Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for o in objects:
        if o is None or o.type != 'MESH':
            continue
        found = True
        for corner in o.bound_box:
            co = o.matrix_world @ Vector(corner)
            for i in range(3):
                min_co[i] = min(min_co[i], co[i])
                max_co[i] = max(max_co[i], co[i])
    return (min_co, max_co) if found else None


def _normalize_to_origin(objects):
    """把一组物体整体平移，使世界包围盒的最小角落在 (0,0)，其余坐标均为正。

    用全局最小角 (min_x, min_y) 构造平移矩阵 T，左乘每个物体的 matrix_world：
      - 仅平移、不旋转，家具朝向与地板顶点重映射均不受影响；
      - 地板（单位阵 world matrix，几何已烤进顶点）同样被整体平移；
      - Z 方向不动（地板已在 Z=0，家具坐在地板上），只把水平面挪到 (0,0) 起。
    已在 (0,0) 附近（极小偏移）则不重复操作。
    """
    from mathutils import Matrix

    bbox = _world_bbox(objects)
    if bbox is None:
        return
    min_co, _ = bbox
    if abs(min_co.x) < 1e-6 and abs(min_co.y) < 1e-6:
        return
    T = Matrix.Translation((-min_co.x, -min_co.y, 0.0))
    for o in objects:
        o.matrix_world = T @ o.matrix_world
    bpy.context.view_layer.update()


def _compose_matrix(pos, rot, scale):
    """由 pos/rot(四元数 x,y,z,w)/scale 合成 4x4 世界矩阵（mathutils）。

    3D-Front 用 **Y 轴向上（Y-up）**，Blender 用 **Z 轴向上（Z-up）**。这里直接做
    坐标分量的显式重映射（等价于绕 X +90° 旋转，但仅改分量、不改用旋转矩阵），
    使家具等「非单位子变换」的物体被正确转入 Z-up：
        (x, y, z)_3dfront        ->  (x, -z, y)_blender
        pos   : (px, py, pz)      ->  (px, -pz, py)
        scale : (sx, sy, sz)      ->  (sx,   sz, sy)
        rot   : (qx, qy, qz, qw)  ->  (qx, -qz, qy, qw)   # 四元数分量共轭
    注意：该重映射对**单位阵**（如 Floor 通常 pos/rot 全 0）无效（remap(I)=I），
    因此地板本身的几何顶点映射放在 _build_floor_mesh 里单独处理。

    3D-Front 的四元数存储顺序为 [x, y, z, w]；Blender 的 Quaternion 为 (w,x,y,z)。
    """
    from mathutils import Matrix, Vector, Quaternion
    t = Matrix.Translation(Vector((pos[0], -pos[2], pos[1])))
    q = Quaternion((rot[3], rot[0], -rot[2], rot[1]))
    r = q.to_matrix().to_4x4()
    s = (Matrix.Scale(scale[0], 4, (1, 0, 0)) @
         Matrix.Scale(scale[2], 4, (0, 1, 0)) @
         Matrix.Scale(scale[1], 4, (0, 0, 1)))
    return t @ r @ s


def _load_category_info(path):
    """加载 model_info.json -> {model_id: category_str}（支持 list 或 dict 形式）。

    若传入的是目录，则自动在其中查找 model_info.json（或名称含 model_info 的 json）。
    """
    if not path:
        return None
    path = os.path.abspath(path)
    if os.path.isdir(path):
        cand = os.path.join(path, "model_info.json")
        if not os.path.exists(cand):
            import glob
            hits = glob.glob(os.path.join(path, "**", "model_info*.json"), recursive=True)
            if hits:
                cand = hits[0]
            else:
                print(f"[构建器] 警告：目录 {path} 下未找到 model_info*.json，"
                      f"将无法按类别过滤家具（吊灯等）。")
                return None
        path = cand
    if not os.path.exists(path):
        print(f"[构建器] 警告：category 文件不存在 {path}，无法按类别过滤家具。")
        return None
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"[构建器] 警告：无法读取 category 文件 {path}: {e}")
        return None
    out = {}
    if isinstance(data, list):
        for e in data:
            mid = str(e.get("model_id") or e.get("uid") or "")
            if not mid:
                continue
            # 合并 super-category 与 category，便于按 super-category 过滤
            # （如 3D-FUTURE 里吊灯/吸顶灯的 super-category 就是 'Lighting'）。
            super_cat = (e.get("super-category") or e.get("super_category")
                         or e.get("superCategory") or "")
            cat = (e.get("category") or e.get("category_zh") or
                   e.get("fine_grained_category") or e.get("category_en") or "")
            combined = " / ".join(p for p in (super_cat, cat) if p)
            out[mid] = str(combined)
    elif isinstance(data, dict):
        for mid, e in data.items():
            if isinstance(e, dict):
                super_cat = (e.get("super-category") or e.get("super_category")
                             or e.get("superCategory") or "")
                cat = (e.get("category") or e.get("category_zh") or
                       e.get("fine_grained_category") or "")
                combined = " / ".join(p for p in (super_cat, cat) if p)
                out[str(mid)] = str(combined)
    return out or None


def _should_skip(model_id, category_info, skip_cats, skip_ids):
    """判断某家具是否应跳过（吊灯/照明类等）。返回 (skip: bool, reason: str)。

    跳过判据（按优先级）：
      1) 显式 model_id 命中 --skip-model-ids；
      2) category_info 存在且 jid 对应的 super-category 为 "Lighting"
         （字符串形如 "Lighting / Pendant Lamp"），直接取消放置；
      3) category 字符串匹配 --skip-categories 任一子串（大小写不敏感）。
    category_info 来自 model_info.json，其项含 super-category 字段。
    """
    if model_id in skip_ids:
        return True, "explicit-id"
    if category_info:
        cat = category_info.get(str(model_id))
        if cat:
            low = cat.lower()
            # 精确：super-category 为 Lighting（组合串首位，形如 "Lighting / ..."）
            first = low.split(" / ")[0].strip()
            if first == "lighting":
                return True, f"super-category:Lighting"
            for s in skip_cats:
                if s.lower() in low:
                    return True, f"category:{cat}"
    return False, None


def _resolve_texture(tex, texture_root):
    """把 material['texture']（可能是相对路径或 uid）解析为本地图片路径。"""
    if not tex:
        return None
    if os.path.isabs(tex) and os.path.exists(tex):
        return tex
    if texture_root and os.path.exists(texture_root):
        cand = os.path.join(texture_root, tex)
        if os.path.exists(cand):
            return cand
        # 有些情况下 texture 是 uid，图片名可能与 uid 相关
        cand2 = os.path.join(texture_root, f"{tex}.jpg")
        if os.path.exists(cand2):
            return cand2
    return None


def _build_material(mat_item, texture_root):
    """由 3D-Front material 项构建 Blender 材质（纯色 ± 贴图）。"""
    mat = bpy.data.materials.new(name="mat_" + str(mat_item.get("uid", "x")))
    # Blender 4.0+ 新建材质默认即节点材质，use_nodes 已废弃（6.0 移除）；
    # 仅旧版本需要显式开启，避免 5.x 上的 DeprecationWarning。
    if bpy.app.version < (4, 0, 0):
        mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
        mat.node_tree.links.new(
            bsdf.outputs["BSDF"] if "BSDF" in bsdf.outputs else bsdf.outputs[0],
            mat.node_tree.nodes["Material Output"].inputs["Surface"])
    col = mat_item.get("color") or [200, 200, 200, 255]
    r = col[0] / 255.0
    g = col[1] / 255.0
    b = col[2] / 255.0
    a = (col[3] / 255.0 if len(col) > 3 else 1.0)
    if "Base Color" in bsdf.inputs:
        bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
    if "Roughness" in bsdf.inputs:
        bsdf.inputs["Roughness"].default_value = 1.0
    if "Alpha" in bsdf.inputs and a < 1.0:
        bsdf.inputs["Alpha"].default_value = a
        mat.blend_method = "BLEND"
    # 贴图（本批示例数据 texture 为空，留作后续扩展）
    tex = mat_item.get("texture")
    if tex:
        path = _resolve_texture(tex, texture_root)
        if path:
            try:
                img = bpy.data.images.load(path)
                tnode = mat.node_tree.nodes.new("ShaderNodeTexImage")
                tnode.image = img
                if "Base Color" in bsdf.inputs:
                    mat.node_tree.links.new(tnode.outputs["Color"],
                                            bsdf.inputs["Base Color"])
            except Exception as e:
                print(f"[构建器] 警告：贴图加载失败 {path}: {e}")
    return mat


def _build_floor_mesh(mesh_item, world_matrix, material_lookup, texture_root, collection):
    """由 3D-Front 的 Floor mesh 项建一个网格物体并赋予材质。

    地板几何是 3D-Front 的 **Y-up** 原始坐标（铺在 X–Z 平面、Y≈0）。家具的坐标
    已由 _compose_matrix 的「分量重映射」转入 Blender 的 Z-up；但 Floor 的子变换
    通常是单位阵（pos/rot 全 0），重映射对它不产生任何效果，因此这里**直接对顶点
    做坐标重映射** (x, y, z) -> (x, -z, y)，让地板落到 Blender 的 X–Y 平面、朝上(+Z)，
    与家具的 Z-up 坐标系对齐。

    注意：Blender 4.0+ 已移除 Mesh.calc_normals()，必须用 bmesh 重算法线；
    这里额外保证所有面法线朝上（+Z），否则俯视时地板会被打成全黑。
    """
    import bmesh
    from mathutils import Vector

    xyz = mesh_item.get("xyz") or []
    faces = mesh_item.get("faces") or []
    if not xyz or not faces:
        return None
    # 坐标重映射：Y-up -> Z-up。地板在 3D-Front 里位于 X–Z 平面(Y≈0)，
    # 映射后落到 X–Y 平面、Z≈0（朝上 +Z）。
    verts = [(xyz[i], -xyz[i + 2], xyz[i + 1])
             for i in range(0, len(xyz), 3)]
    # faces 为扁平三角索引，每 3 个一组
    face_list = [tuple(faces[i:i + 3]) for i in range(0, len(faces), 3)]

    bm = bmesh.new()
    for v in verts:
        bm.verts.new(v)
    bm.verts.ensure_lookup_table()
    for f in face_list:
        try:
            bm.faces.new(bm.verts[i] for i in f)
        except ValueError:
            # 退化面（重复索引）跳过
            continue
    bm.faces.ensure_lookup_table()
    if bm.faces:
        # 先统一绕序，再强制法线朝上（俯视需 +Z 受光）
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        for f in bm.faces:
            if f.normal.z < 0.0:
                f.normal_flip()
            f.smooth = True  # 地板平滑着色，俯视更自然

    mesh = bpy.data.meshes.new(name="floor_" + str(mesh_item.get("uid", "x")))
    bm.to_mesh(mesh)
    bm.free()

    obj = bpy.data.objects.new(mesh.name, mesh)
    obj.matrix_world = world_matrix
    collection.objects.link(obj)

    # 材质
    muid = mesh_item.get("material")
    mat_item = material_lookup.get(muid)
    if mat_item is not None:
        mat = _build_material(mat_item, texture_root)
        obj.data.materials.append(mat)
    else:
        print(f"[构建器] 警告：Floor 找不到材质 uid={muid}")
    return obj


def _fix_furniture_roughness(objs, roughness):
    """把导入家具所有 Principled BSDF 的 Roughness 设为固定值（修正镜面失真）。"""
    if roughness is None:
        return
    for o in objs:
        if o.type != 'MESH':
            continue
        for slot in o.material_slots:
            mat = slot.material
            if mat is None or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    if "Roughness" in node.inputs:
                        node.inputs["Roughness"].default_value = roughness


def _fix_furniture_textures(new_objs, model_folder):
    """修复导入家具的贴图路径。

    Blender 的 OBJ 导入器在解析 MTL 里的相对贴图路径（如 './texture.png'）时，
    会以当前工作目录（项目根）而非模型目录去查找，导致贴图丢失；
    而 3D-FUTURE 的 .mtl 通常把 Kd 设为白色并依赖 map_Kd texture.png 上色，
    一旦贴图找不到，家具就会渲染成灰白。

    这里把每个 Image Texture 节点的图片重新指向 model_folder 下同名文件；
    若节点原本图片缺失，则用 folder 内第一张可用图片兜底。

    返回 (repairs, img_nodes, folder_has_image)。
    """
    import glob as _glob
    if not model_folder or not os.path.isdir(model_folder):
        return (0, 0, False)
    files = _glob.glob(os.path.join(model_folder, "*"))
    img_files = [p for p in files
                 if p.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"))]
    repairs = 0
    img_nodes = 0
    for o in new_objs:
        if o.type != 'MESH':
            continue
        for slot in o.material_slots:
            mat = slot.material
            if mat is None or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type != 'TEX_IMAGE':
                    continue
                img_nodes += 1
                cur = node.image.filepath if (node.image and node.image.filepath) else ""
                base = os.path.basename(cur)
                target = None
                if base:
                    cand = os.path.join(model_folder, base)
                    if os.path.exists(cand):
                        target = cand
                if target is None and img_files:
                    target = img_files[0]
                if target is None:
                    continue
                # 仅当图片确实缺失 / 路径不对时才替换，避免破坏已正确加载的贴图
                if (not node.image) or (not os.path.exists(cur)) or \
                   (os.path.abspath(cur) != os.path.abspath(target)):
                    try:
                        img = bpy.data.images.load(target)
                        node.image = img
                        repairs += 1
                    except Exception:
                        pass
    return (repairs, img_nodes, len(img_files) > 0)


def _import_furniture(model_id, seq, world_matrix, dataset_root, collection, cfg):
    """导入一件 3D-FUTURE 家具并摆到 world_matrix，返回根 Empty 或 None。

    model_id 即家具的 jid（uuid 哈希）。若该文件夹不存在，再退化尝试
    序号 seq（<dataset>/<seq>/raw_model.obj）。
    """
    from mathutils import Euler
    candidates = [model_id]
    if seq and seq != model_id:
        candidates.append(seq)
    obj_path = None
    for cand in candidates:
        p = os.path.join(dataset_root, cand, "raw_model.obj")
        if os.path.exists(p):
            obj_path = p
            break
    if obj_path is None:
        tried = ", ".join(os.path.join(dataset_root, c, "raw_model.obj")
                          for c in candidates)
        print(f"[构建器] 警告：找不到家具模型，已跳过 -> {tried}")
        return None

    before = set(bpy.data.objects)
    # 注意：Blender 5.x 的 OBJ 导入器已移除 use_image_search 参数，
    # 传该关键字会直接抛 "keyword unrecognized" 导致整件家具导入失败。
    # 因此这里不带该参数；贴图路径由 _fix_furniture_textures 在导入后修正。
    try:
        bpy.ops.wm.obj_import(filepath=obj_path, global_scale=cfg["furniture_scale"])
    except Exception as e:
        print(f"[构建器] 警告：导入失败 {obj_path}: {e}")
        return None

    new_objs = [o for o in bpy.data.objects if o not in before]
    if not new_objs:
        print(f"[构建器] 警告：导入未产生物体 -> {model_id}")
        return None

    if cfg.get("roughness") is not None:
        _fix_furniture_roughness(new_objs, cfg["roughness"])

    # 修复贴图路径：Blender OBJ 导入器有时找不到 MTL 里的相对贴图，导致家具灰白。
    model_folder = os.path.dirname(obj_path)
    repairs, img_nodes, has_img = _fix_furniture_textures(new_objs, model_folder)
    if img_nodes > 0:
        if repairs < img_nodes:
            print(f"[构建器] 家具 {model_id}: 贴图节点 {img_nodes} 个, 已修复 {repairs} 个")
    elif not has_img:
        print(f"[构建器] 提示：家具 {model_id} 模型目录无贴图文件，"
              f"将以 MTL 基础色（多为白/灰）显示；若缺 texture.png 请确认数据集完整。")

    pre = cfg["pre_rot"]
    for o in new_objs:
        r = o.rotation_euler
        o.rotation_euler = Euler((r.x + pre[0], r.y + pre[1], r.z + pre[2]))

    # 用 Empty 作根，挂住多部件模型
    root = bpy.data.objects.new(f"fur_{model_id}", None)
    collection.objects.link(root)
    for o in new_objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = root
    bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

    root.matrix_world = world_matrix
    return root


def _parse_threefront(json_path, keep_mesh_types, category_info,
                      skip_cats, skip_ids, dataset_root):
    """解析 3D-Front JSON，返回房间列表（每间含 floor meshes 与 furniture）。

    每个 furniture 项已解析出 model_id、是否跳过、以及 world 矩阵。
    """
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mesh_by_uid = {m["uid"]: m for m in data.get("mesh", [])}
    furniture_by_uid = {x["uid"]: x for x in data.get("furniture", [])}
    material_lookup = {m["uid"]: m for m in data.get("material", [])}

    scene = data.get("scene", {})
    scene_M = _compose_matrix(scene.get("pos", [0, 0, 0]),
                              scene.get("rot", [0, 0, 0, 1]),
                              scene.get("scale", [1, 1, 1]))

    rooms_out = []
    for room in scene.get("room", []):
        room_type = room.get("type", "Unknown")
        room_M = _compose_matrix(room.get("pos", [0, 0, 0]),
                                 room.get("rot", [0, 0, 0, 1]),
                                 room.get("scale", [1, 1, 1]))
        room_entry = {
            "type": room_type,
            "instanceid": room.get("instanceid", ""),
            "floors": [],      # list of (mesh_item, world_matrix)
            "furnitures": [],  # list of (model_id, world_matrix, skip, reason)
        }
        for child in room.get("children", []):
            ref = child.get("ref")
            inst = child.get("instanceid", "")
            child_M = _compose_matrix(child.get("pos", [0, 0, 0]),
                                      child.get("rot", [0, 0, 0, 1]),
                                      child.get("scale", [1, 1, 1]))
            world_M = scene_M @ room_M @ child_M

            if inst.startswith("mesh/"):
                m = mesh_by_uid.get(ref)
                if m is None:
                    continue
                if m.get("type") in keep_mesh_types:
                    room_entry["floors"].append((m, world_M))
            elif inst.startswith("furniture/"):
                fur = furniture_by_uid.get(ref)
                if fur is None:
                    continue
                # 3D-Front 的 furniture.uid 是内部序号（如 '1739/model'），
                # 真正对应 3D-FUTURE 模型文件夹的是 jid（uuid 哈希，例如
                # '6c185b06-6b95-45d0-bad2-fea9895e1e5a'），模型放在
                # <dataset>/<jid>/raw_model.obj。类别过滤（model_info.json）
                # 也以 jid 为键。
                jid = (fur.get("jid") or "").strip()
                seq = ""
                uid = fur.get("uid", "")
                if uid:
                    seq = uid.split("/")[0]
                if not jid and seq:
                    jid = seq   # 退化：个别数据可能只用序号
                if not jid:
                    continue
                skip, reason = _should_skip(jid, category_info,
                                            skip_cats, skip_ids)
                room_entry["furnitures"].append(
                    {"jid": jid, "seq": seq, "M": world_M,
                     "skip": skip, "reason": reason})
        rooms_out.append(room_entry)
    return rooms_out, material_lookup


def _pick_engine(args):
    scene = bpy.context.scene
    valid = [i.identifier for i in scene.render.bl_rna.properties['engine'].enum_items]
    if args.render_engine == "eevee":
        eevee = [e for e in valid if "EEVEE" in e]
        return eevee[0] if eevee else "BLENDER_EEVEE"
    if args.render_engine == "workbench":
        return "BLENDER_WORKBENCH"
    return "CYCLES"


def _configure_cycles(args):
    scene = bpy.context.scene
    cyc = scene.cycles
    cyc.samples = args.render_samples
    cyc.max_bounces = 8
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
    try:
        vlayer = bpy.context.view_layer
        if hasattr(vlayer, "cycles") and hasattr(vlayer.cycles, "use_denoising"):
            vlayer.cycles.use_denoising = not args.no_render_denoise
    except Exception:
        pass
    # 自动启用 GPU
    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        prefs.refresh_devices()
        for d in prefs.devices:
            d.use = True
        cyc.device = 'GPU'
    except Exception:
        cyc.device = 'CPU'


def _render_topdown(objects, out_path, args, stem, hide_others=True):
    """对给定物体集合渲染一张俯视正交图。

    对齐 blender_renderer.py 的拍摄/渲染方案：
      - 自动判定竖直轴（三轴包围盒中范围最小者为竖直方向）；
      - 水平中心正上方放置正交相机朝下拍，ortho_scale 取较大水平尺寸 × 留白；
      - 斜 45° SUN 主光 + AREA 向下补光 + World 环境光，材质光影真实；
      - 默认 Cycles 路径追踪 + 降噪；渲染后清理临时灯/相机。
    hide_others=True 时（room 模式）临时隐藏其它物体，只渲染本集合；
    hide_others=False 时（scene 模式）渲染场景内全部保留物体（整屋）。
    """
    from mathutils import Vector
    import math

    scene = bpy.context.scene
    if not objects:
        print(f"[构建器] 跳过空渲染 -> {out_path}")
        return

    chosen_engine = _pick_engine(args)
    old_engine = scene.render.engine
    scene.render.engine = chosen_engine
    if chosen_engine == "CYCLES":
        _configure_cycles(args)

    # 视野只框住本集合
    bbox = _world_bbox(objects)
    if bbox is None:
        print(f"[构建器] 警告：集合无网格，跳过 -> {out_path}")
        scene.render.engine = old_engine
        return
    min_co, max_co = bbox
    ext = Vector((max_co.x - min_co.x, max_co.y - min_co.y, max_co.z - min_co.z))
    center = (min_co + max_co) / 2.0
    # 自动判定竖直轴：三轴包围盒中范围最小者为竖直方向，另两轴为水平方向。
    # （本数据恒为 Z-up，竖直轴即 Z，等价于原来写死的 (0,0,1)，但更稳健。）
    axes = ['X', 'Y', 'Z']
    vert = min(axes, key=lambda a: ext[ord(a) - 88])
    horiz = [a for a in axes if a != vert]
    h_ext = max(ext[ord(a) - 88] for a in horiz)
    if h_ext <= 0:
        h_ext = 1.0

    # 隔离：除本集合物体与相机/灯外，全部 hide_render（scene 模式不隐藏）
    all_objs = list(bpy.data.objects)
    if hide_others:
        for o in all_objs:
            if o in objects:
                continue
            o.hide_render = True
    tmp_objects = []

    # —— 灯光：俯视专用（斜射 SUN + AREA 补光 + 环境光）——
    up = {'X': Vector((1, 0, 0)), 'Y': Vector((0, 1, 0)),
          'Z': Vector((0, 0, 1))}[vert]  # 竖直轴单位向量
    sun_data = bpy.data.lights.new("TopDownKeySun", type='SUN')
    sun_data.energy = 2.5
    sun_data.angle = 0.15
    sun = bpy.data.objects.new("TopDownKeySun", sun_data)
    scene.collection.objects.link(sun)
    off = up * (h_ext * 4.0 + 10.0) + Vector((1.0, 0.0, 1.0)) * (h_ext * 4.0 + 10.0)
    sun.location = Vector(center) + off
    sun.rotation_euler = (Vector(center) - sun.location).to_track_quat('-Z', 'Y').to_euler()
    tmp_objects.append(sun)

    area_data = bpy.data.lights.new("TopDownFill", type='AREA')
    area_data.shape = 'RECTANGLE'
    area_data.size = h_ext * 1.5
    area_data.size_y = h_ext * 1.5
    area_data.energy = 1.2
    area_data.spread = 1.0
    area = bpy.data.objects.new("TopDownFill", area_data)
    scene.collection.objects.link(area)
    area.location = Vector(center) + up * (h_ext + 5.0)
    area.rotation_euler = (Vector(center) - area.location).to_track_quat('-Z', 'Y').to_euler()
    tmp_objects.append(area)

    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("TopDownWorld")
        scene.world = world
    if bpy.app.version < (4, 0, 0):
        world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs["Color"].default_value = (0.9, 0.9, 0.92, 1.0)
        bg.inputs["Strength"].default_value = 1.0

    # —— 相机：水平中心正上方，沿竖直轴朝下，正交 ——
    cam_data = bpy.data.cameras.new("TopDownCam")
    cam = bpy.data.objects.new("TopDownCam", cam_data)
    scene.collection.objects.link(cam)
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = h_ext * (1.0 + args.topdown_margin)
    cam_data.clip_start = 0.01
    cam_data.clip_end = 1e6
    target = Vector(center)
    cam.location = Vector(center) + up * (h_ext * 4.0 + 10.0)
    direction = target - cam.location          # 指向 -竖直轴（俯视）
    cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    scene.camera = cam
    tmp_objects.append(cam)

    # 背景
    scene.render.film_transparent = bool(args.transparent_bg)
    if not args.transparent_bg and scene.world is not None:
        bg = scene.world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs["Color"].default_value = (0.95, 0.95, 0.95, 1.0)

    scene.render.resolution_x = args.render_resolution
    scene.render.resolution_y = args.render_resolution
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = str(out_path)

    print(f"[构建器] 渲染 {out_path.name}（引擎={chosen_engine}）…")
    try:
        bpy.ops.render.render(write_still=True)
    except Exception as e:
        import traceback
        print(f"[构建器] 警告：渲染失败 {out_path}: {e}")
        traceback.print_exc()

    # 清理临时灯/相机，恢复其它物体的 hide_render，还原引擎
    # 注意：Blender 5.x 的 bpy_prop_collection.__contains__ 只接受字符串键，
    # 因此用 obj.name 判断成员关系，而非对象引用。
    for o in tmp_objects:
        if o.name in bpy.data.objects:
            bpy.data.objects.remove(o, do_unlink=True)
    if hide_others:
        for o in all_objs:
            o.hide_render = False
    scene.render.engine = old_engine
    scene.render.film_transparent = False


def _slug(s):
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(s))


def main_builder(args):
    import glob

    # 独立运行（直接用 blender -b 跑本脚本）时，把构建器输出也写入日志文件。
    # 通过 isatty() 判断是否由启动器以管道方式调用：管道模式下启动器已 tee 全部
    # 输出，这里不再写入，避免日志重复；TTY 模式（独立运行）才开启 tee。
    _log_path = os.environ.get("THREEFRONT_LOG")
    if _log_path and sys.stdout.isatty():
        try:
            _lf = open(_log_path, "a", encoding="utf-8")

            class _Tee:
                def __init__(self, *streams):
                    self.streams = streams

                def write(self, s):
                    for st in self.streams:
                        try:
                            st.write(s)
                            st.flush()
                        except Exception:
                            pass

                def flush(self):
                    for st in self.streams:
                        try:
                            st.flush()
                        except Exception:
                            pass

            sys.stdout = _Tee(sys.__stdout__, _lf)
            sys.stderr = _Tee(sys.__stderr__, _lf)
        except Exception:
            pass

    print(f"[构建器] input  = {os.path.abspath(args.input)}")
    print(f"[构建器] dataset= {os.path.abspath(args.dataset)}")

    # 删除 Blender 出厂默认物体（默认 Cube），避免污染最终场景 / 误导包围盒
    _def_cube = bpy.data.objects.get("Cube")
    if _def_cube is not None:
        bpy.data.objects.remove(_def_cube, do_unlink=True)

    # 收集输入 JSON
    inp = os.path.abspath(args.input)
    if os.path.isdir(inp):
        json_files = sorted(glob.glob(os.path.join(inp, "*.json")))
    else:
        json_files = [inp]
    if not json_files:
        sys.exit(f"[构建器] 未找到任何 JSON: {inp}")

    keep_mesh_types = {t.strip() for t in args.keep_mesh_types.split(",") if t.strip()}
    skip_cats = [s.strip() for s in args.skip_categories.split(",") if s.strip()]
    skip_ids = {s.strip() for s in args.skip_model_ids.split(",") if s.strip()}
    category_info = _load_category_info(os.path.abspath(args.category_json))

    # 房间类型筛选：room 的 type 含任一子串（小写）才保留；空列表=全部房间。
    room_filters = [s.strip().lower() for s in (args.room_filter or "").split(",")
                    if s.strip()]
    def _keep_room(rtype):
        if not room_filters:
            return True
        r = (rtype or "").lower()
        # 精确优先：若筛选词与某房间 type 完全相同（大小写不敏感），只保留它；
        # 否则退回子串匹配（如 'bedroom' 同时命中 MasterBedroom/SecondBedroom）。
        if any(f == r for f in room_filters):
            return True
        return any(f in r for f in room_filters)

    cfg = {
        "furniture_scale": args.furniture_scale,
        "roughness": args.roughness,
        "pre_rot": tuple(args.pre_rot),
    }

    out_dir = Path(os.path.abspath(args.output_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    scene = bpy.context.scene
    master_coll = scene.collection

    total_rooms = 0
    total_rendered = 0
    # 先构建所有「被保留」房间的可见物体，渲染阶段再按 scope 统一处理
    # （整屋合一张 / 每间各一张），并在渲染前把坐标归一到 (0,0)。
    kept_rooms = []  # 元素: (stem, rtype, ri, room_objects)
    for jf in json_files:
        stem = Path(jf).stem
        print(f"\n[构建器] === 处理 {Path(jf).name} ===")
        rooms, material_lookup = _parse_threefront(
            jf, keep_mesh_types, category_info, skip_cats, skip_ids,
            os.path.abspath(args.dataset))

        # 用一个集合隔离每个 JSON 的结果
        json_coll = bpy.data.collections.new(f"house_{stem}")
        master_coll.children.link(json_coll)

        for ri, room in enumerate(rooms):
            total_rooms += 1
            rtype = room["type"]
            # 房间筛选：不匹配则整间跳过（不建地板/家具，也不渲染）
            if not _keep_room(rtype):
                print(f"  [跳过房间] type={rtype} idx={ri}: 不匹配 "
                      f"--room-filter={args.room_filter!r}")
                continue
            room_coll = bpy.data.collections.new(f"room_{_slug(rtype)}_{ri}")
            json_coll.children.link(room_coll)

            room_objects = []

            # 地板
            for m, world_M in room["floors"]:
                obj = _build_floor_mesh(m, world_M, material_lookup,
                                        args.texture_root, room_coll)
                if obj is not None:
                    room_objects.append(obj)

            # 家具（跳过吊灯等）
            placed = skipped = 0
            for fur in room["furnitures"]:
                if fur["skip"]:
                    print(f"  [跳过家具] model={fur['jid']} 原因={fur['reason']}")
                    skipped += 1
                    continue
                world_M = fur["M"]
                root = _import_furniture(fur["jid"], fur["seq"], world_M,
                                         os.path.abspath(args.dataset),
                                         room_coll, cfg)
                if root is not None:
                    room_objects.append(root)
                    placed += 1
                else:
                    skipped += 1
            print(f"  [房间] type={rtype} idx={ri}: 地板 {len(room['floors'])} 件, "
                  f"家具已放 {placed}, 跳过 {skipped}")

            if not room_objects:
                print(f"  [房间] type={rtype} idx={ri}: 无可见物体，跳过渲染")
                continue

            kept_rooms.append((stem, rtype, ri, room_objects))

    # —— 渲染阶段：按 --topdown-scope 决定整屋 / 单间，并归一到 (0,0) ——
    if not args.no_render and kept_rooms:
        scope = args.topdown_scope if args.topdown_scope != "auto" else "scene"
        if scope == "room":
            # 每间各一张，各自平移到 (0,0)
            for stem, rtype, ri, ros in kept_rooms:
                if args.origin_at_zero:
                    _normalize_to_origin(ros)
                out_png = out_dir / f"{stem}_{_slug(rtype)}_{ri}_topdown.png"
                _render_topdown(ros, out_png, args, stem, hide_others=True)
                total_rendered += 1
        else:  # scene：所有保留房间合一张整屋图，整体平移到 (0,0)
            everything = [o for (_, _, _, ros) in kept_rooms for o in ros]
            if args.origin_at_zero:
                _normalize_to_origin(everything)
            if len(json_files) == 1:
                scene_stem = Path(json_files[0]).stem
            else:
                scene_stem = "scene"
            # 文件名带房间类型后缀：只保留单类型（如仅 MasterBedroom）时，
            # 追加 _masterbedroom 以区分；多类型则把类型用 '+' 连接。
            slugs = sorted({_slug(rtype) for (_, rtype, _, _) in kept_rooms})
            if slugs:
                suffix = "_".join(slugs)          # 单类型：masterbedroom；多类型：a_b
                out_name = f"{scene_stem}_{suffix}_topdown.png"
            else:
                out_name = f"{scene_stem}_topdown.png"
            out_png = out_dir / out_name
            _render_topdown(everything, out_png, args, scene_stem,
                            hide_others=False)
            total_rendered += 1

    # 保存 .blend（供检查；不渲染也不影响已输出的 PNG）。
    # 注意：当前只摆放了被筛选出的房间，故文件名去掉 "allrooms" 以名实相符。
    if not args.no_save_blend:
        if len(json_files) == 1:
            out_blend = out_dir / f"{Path(json_files[0]).stem}_furnished.blend"
        else:
            out_blend = out_dir / "furnished.blend"
        try:
            bpy.ops.wm.save_as_mainfile(filepath=str(out_blend))
            print(f"[构建器] 已保存场景 -> {out_blend}")
        except Exception as e:
            print(f"[构建器] 警告：保存 .blend 失败: {e}")

    print(f"\n[构建器] 完成：处理 {len(json_files)} 个 JSON，"
          f"共 {total_rooms} 个房间，渲染 {total_rendered} 张 top2down。")


# ======================================================================
# 获取脚本参数
# ======================================================================
def _script_argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return sys.argv[1:]


# ======================================================================
# 入口
# ======================================================================
if __name__ == "__main__":
    args = make_parser().parse_args(_script_argv())
    if IN_BLENDER:
        main_builder(args)
    else:
        main_launcher(args)
