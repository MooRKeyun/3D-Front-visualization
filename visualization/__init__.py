"""
core.visualization — Blender 渲染与家具摆放
============================================
把 generation 产出的 layout json 摆进 Blender 空房间场景，
最终以 GUI 窗口呈现。
"""

from .blender_renderer import (
    launch_blender,
    main_launcher,
    main_builder,
    make_parser,
    BLENDER_BIN_DEFAULT,
    IN_BLENDER,
)

from .threefront_renderer import (
    main_launcher as threefront_launcher,
    main_builder as threefront_builder,
    make_parser as threefront_make_parser,
)

__all__ = [
    "launch_blender",
    "main_launcher",
    "main_builder",
    "make_parser",
    "BLENDER_BIN_DEFAULT",
    "IN_BLENDER",
    "threefront_launcher",
    "threefront_builder",
    "threefront_make_parser",
]
