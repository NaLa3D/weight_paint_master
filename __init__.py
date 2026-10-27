# SPDX-License-Identifier: GPL-3.0-or-later
"""
Weight Paint Master - Blender 5.2+ 対応ウェイトペイント効率化アドオン
パイメニュー（ショートカット呼び出し）とNパネルを中心に以下の機能を提供します：
1. モード高速移動（Poseモード / Weight Paint / Editモード）＆ 全ボーンのポーズ一括クリア
2. 表示切替（ウェイト等高線 ON/OFF・ワイヤーフレーム ON/OFF・編集モードウェイト色表示・ボーン最前面）
3. ボーン（頂点グループ）種類別フィルタ付き選択リスト（全ボーン / 選択頂点に影響中 / 変形ボーン / 左.L / 右.R / 中央）
4. 編集モード/ウェイトペイント両対応：選択頂点からマウスホイールで直感的にウェイトをぼかす対話ツール
5. 選択頂点のウェイト数値一括設定・加減算・正規化・クリーンアップ（4ボーン制限など）
6. ブラシ（Mix / Add / Subtract / Multiply / Blur / Average / Smear）とウェイト値・強さのクイック切り替え
"""

bl_info = {
    "name": "Weight Paint Master (Pie & Auto Tools)",
    "author": "Antigravity",
    "version": (1, 4, 0),
    "blender": (5, 0, 0),
    "location": "Weight Paint / Edit / Pose Mode > Shortcut (Alt+W / Shift+W) & View3D > Sidebar > Weight Master",
    "description": "編集モードXミラー対称ウェイト・ホイール対話ぼかし・ボーン双方向同期・表示切替・ポーズ移動＆全クリア統合アドオン",
    "category": "Paint",
}

from collections import deque
import bpy
import bmesh
import blf
from mathutils import Vector
from mathutils.kdtree import KDTree
from bpy.types import Operator, Menu, Panel, UIList, AddonPreferences, PropertyGroup
from bpy.props import (
    FloatProperty,
    IntProperty,
    BoolProperty,
    EnumProperty,
    StringProperty,
    PointerProperty,
)


# ============================================================================
# ヘルパー関数 (Blender 5.2 API対応 / メッシュ＆アーマチュア相互検索)
# ============================================================================

def get_weight_paint_settings(context):
    """Blender 5.2+ の WeightPaint と UnifiedPaintSettings を安全に取得する"""
    ts = context.tool_settings
    wp = ts.weight_paint
    ups = getattr(wp, "unified_paint_settings", None)
    if ups is None:
        ups = getattr(ts, "unified_paint_settings", None)
    return ts, wp, ups


def get_current_brush_weight(context) -> float:
    """現在のブラシウェイト値を取得する"""
    ts, wp, ups = get_weight_paint_settings(context)
    brush = wp.brush if wp else None
    if ups and getattr(ups, "use_unified_weight", True):
        return ups.weight
    if brush:
        return brush.weight
    return ts.vertex_group_weight


def set_current_brush_weight(context, value: float):
    """現在のブラシウェイト値および頂点グループウェイト値を一括設定する"""
    val = max(0.0, min(1.0, value))
    ts, wp, ups = get_weight_paint_settings(context)
    if ups:
        ups.weight = val
    if wp and wp.brush:
        wp.brush.weight = val
    ts.vertex_group_weight = val


def get_current_brush_strength(context) -> float:
    """現在のブラシ強さ(Strength)を取得する"""
    _, wp, ups = get_weight_paint_settings(context)
    brush = wp.brush if wp else None
    if ups and getattr(ups, "use_unified_strength", True):
        return ups.strength
    if brush:
        return brush.strength
    return 1.0


def set_current_brush_strength(context, value: float):
    """現在のブラシ強さ(Strength)を設定する"""
    val = max(0.0, min(1.0, value))
    _, wp, ups = get_weight_paint_settings(context)
    if ups:
        ups.strength = val
    if wp and wp.brush:
        wp.brush.strength = val


def get_related_armature(obj):
    """メッシュ（またはアーマチュア自身）に関連付けられたアーマチュアオブジェクトを取得する"""
    if not obj:
        return None
    if obj.type == 'ARMATURE':
        return obj
    if obj.parent and obj.parent.type == 'ARMATURE':
        return obj.parent
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            return mod.object
    arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    return arms[0] if len(arms) == 1 else None


def get_target_mesh_object(context):
    """現在アクティブなメッシュ、またはPoseモード時は直前に操作していた対象メッシュを取得する"""
    obj = context.active_object
    if obj and obj.type == 'MESH':
        return obj

    settings = getattr(context.scene, "wpm_settings", None)
    last_name = getattr(settings, "last_mesh_name", "") if settings else ""
    if last_name:
        cand = context.scene.objects.get(last_name)
        if cand and cand.type == 'MESH':
            return cand

    if obj and obj.type == 'ARMATURE':
        for o in context.scene.objects:
            if o.type == 'MESH':
                if o.parent == obj:
                    return o
                for mod in o.modifiers:
                    if mod.type == 'ARMATURE' and mod.object == obj:
                        return o
    return None


def get_viewport_overlay_states(context, arm_obj=None):
    """
    Blender 5.2 の View3DOverlay および アーマチュアから安全に表示状態を取得する。
    - ウェイト等高線: overlay.show_wpaint_contours
    - ウェイトペイントのワイヤー: overlay.show_paint_wire
    - Editウェイト色: overlay.show_weight
    - ボーン最前面: arm_obj.show_in_front
    - ボーン名前: arm_obj.data.show_names
    - ボーン座標軸: arm_obj.data.show_axes
    """
    space = context.space_data
    overlay = space.overlay if (space and space.type == 'VIEW_3D' and hasattr(space, "overlay")) else None
    show_contours = False
    show_wire = False
    show_edit_w = False
    if overlay:
        if hasattr(overlay, "show_wpaint_contours"):
            show_contours = bool(overlay.show_wpaint_contours)
        elif hasattr(overlay, "show_weight_contours"):
            show_contours = bool(overlay.show_weight_contours)
        show_wire = bool(getattr(overlay, "show_paint_wire", False))
        show_edit_w = bool(getattr(overlay, "show_weight", False))

    bone_front = bool(getattr(arm_obj, "show_in_front", False)) if arm_obj else False
    arm_data = getattr(arm_obj, "data", None) if arm_obj else None
    bone_names = bool(getattr(arm_data, "show_names", False)) if arm_data else False
    bone_axes = bool(getattr(arm_data, "show_axes", False)) if arm_data else False
    return overlay, show_contours, show_wire, show_edit_w, bone_front, bone_names, bone_axes


def select_armature_bone_safe(arm_obj, bone_name: str):
    """Blender 5.0+ (PoseBone.select) と旧仕様の両方に安全に対応してボーンを選択・アクティブ化し、ビューポートを即時更新する"""
    if not arm_obj or not arm_obj.data:
        return
    bones = arm_obj.data.bones
    has_bone = bool(bone_name and bone_name in bones)

    # アクティブボーンを設定（対象ボーンがない頂点グループの場合はアクティブ解除）
    bones.active = bones[bone_name] if has_bone else None

    # Blender 5.0+ では PoseBone.select に移動しているため両方に対応
    if arm_obj.pose:
        for pb in arm_obj.pose.bones:
            is_target = (has_bone and pb.name == bone_name)
            if hasattr(pb, "select"):
                pb.select = is_target
            if hasattr(pb.bone, "select"):
                pb.bone.select = is_target
    else:
        for b in bones:
            if hasattr(b, "select"):
                b.select = (has_bone and b.name == bone_name)

    # 3Dビューポート上のボーンのハイライト（水色表示）を即時更新するためDepsgraphへ通知
    arm_obj.data.update_tag()
    arm_obj.update_tag(refresh={'OBJECT', 'DATA'})
    if bpy.context and bpy.context.view_layer:
        bpy.context.view_layer.update()
    if bpy.context and bpy.context.screen:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def sync_pose_bone_with_active_vg(context, obj):
    """アクティブ頂点グループに対応するアーマチュアのボーンを選択・アクティブ化する"""
    if not obj or obj.type != 'MESH' or not obj.vertex_groups.active:
        return
    vg_name = obj.vertex_groups.active.name
    arm_obj = get_related_armature(obj)
    if arm_obj:
        select_armature_bone_safe(arm_obj, vg_name)


def _get_wpm_active_vg_index(self) -> int:
    """NパネルのUIList用：現在のアクティブ頂点グループインデックスを返す（Alt+左クリックのボーン選択とも連動）"""
    if self.type == 'MESH' and self.vertex_groups:
        return self.vertex_groups.active_index
    return -1


def _set_wpm_active_vg_index(self, value: int):
    """NパネルのUIListでボーンがクリックされた際にアクティブ頂点グループを変更する"""
    if self.type == 'MESH' and self.vertex_groups:
        if 0 <= value < len(self.vertex_groups):
            self.vertex_groups.active_index = value


def _update_wpm_active_vg_index(self, context):
    """NパネルのUIListでボーンが選択された直後に、ビューポート上のアーマチュアのボーンもアクティブ（水色ハイライト）にする"""
    if self.type == 'MESH' and self.vertex_groups.active:
        sync_pose_bone_with_active_vg(context, self)
        if self.mode == 'EDIT':
            ensure_edit_mode_weight_overlay(context, True)
            bmesh.update_edit_mesh(self.data, loop_triangles=False, destructive=False)
        else:
            self.data.update()


def get_target_vertex_indices(obj) -> list[int]:
    """
    対象となる頂点インデックスのリストを取得する。
    - Editモード: 選択中の頂点（未選択なら全頂点）
    - Weight Paintモード: 選択頂点があれば選択頂点、なければ全頂点
    """
    mesh = obj.data
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        selected = [v.index for v in bm.verts if v.select]
        return selected if selected else [v.index for v in bm.verts]

    selected = [v.index for v in mesh.vertices if v.select]
    if selected and (mesh.use_paint_mask or mesh.use_paint_mask_vertex):
        return selected
    return selected if selected else [v.index for v in mesh.vertices]


def ensure_active_vertex_group(obj):
    """アクティブな頂点グループを取得（存在しなければ新規作成）"""
    if obj.vertex_groups.active is None:
        return obj.vertex_groups.new(name="Group")
    return obj.vertex_groups.active


def flip_side_name(name: str) -> str:
    """左右対称のボーン/頂点グループ名（.L <-> .R など）を変換する"""
    pairs = [
        (".L", ".R"), (".l", ".r"),
        ("_L", "_R"), ("_l", "_r"),
        ("-L", "-R"), ("-l", "-r"),
        (".Left", ".Right"), (".left", ".right"),
        ("_Left", "_Right"), ("_left", "_right"),
    ]
    for left, right in pairs:
        if name.endswith(left):
            return name[:-len(left)] + right
        if name.endswith(right):
            return name[:-len(right)] + left
        if left + "." in name:
            return name.replace(left + ".", right + ".", 1)
        if right + "." in name:
            return name.replace(right + ".", left + ".", 1)
    return name


def get_side_category(name: str) -> str:
    """ボーン名から LEFT / RIGHT / CENTER を判定する"""
    left_patterns = (".L", ".l", "_L", "_l", "-L", "-l", ".Left", ".left", "_Left", "_left")
    right_patterns = (".R", ".r", "_R", "_r", "-R", "-r", ".Right", ".right", "_Right", "_right")
    for p in left_patterns:
        if name.endswith(p) or (p + ".") in name:
            return 'LEFT'
    for p in right_patterns:
        if name.endswith(p) or (p + ".") in name:
            return 'RIGHT'
    return 'CENTER'


def get_deform_bone_names(obj) -> set[str]:
    """関連アーマチュアの変形ボーン(use_deform=True)の名前セットを取得する"""
    arm_obj = get_related_armature(obj)
    if not arm_obj or not arm_obj.data:
        return set()
    return {b.name for b in arm_obj.data.bones if b.use_deform}


def get_selected_verts_group_weights(obj) -> dict[int, float]:
    """
    現在選択されている頂点に含まれている頂点グループとその平均ウェイトの辞書 {group_index: avg_weight} を返す。
    """
    if not obj or obj.type != 'MESH':
        return {}
    mesh = obj.data
    sums = {}
    counts = 0

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        deform = bm.verts.layers.deform.active
        if not deform:
            return {}
        sel_verts = [v for v in bm.verts if v.select]
        for v in sel_verts:
            counts += 1
            dv = v[deform]
            for g_idx, w in dv.items():
                if w > 1e-4:
                    sums[g_idx] = sums.get(g_idx, 0.0) + w
    else:
        sel_verts = [v for v in mesh.vertices if v.select]
        for v in sel_verts:
            counts += 1
            for g in v.groups:
                if g.weight > 1e-4:
                    sums[g.group] = sums.get(g.group, 0.0) + g.weight

    if counts == 0:
        return {}
    return {g_idx: total / counts for g_idx, total in sums.items()}


def ensure_edit_mode_weight_overlay(context, enable=True):
    """編集モード中でもウェイトカラーが見えるようにビューポートオーバーレイのshow_weightをONにする"""
    space = context.space_data
    if space and space.type == 'VIEW_3D' and hasattr(space, "overlay"):
        if hasattr(space.overlay, "show_weight"):
            space.overlay.show_weight = enable


def get_bone_x_sign(obj, vg_name: str) -> int:
    """
    指定ボーンがローカルX軸のどちら側にあるか (+1: +X側, -1: -X側, 0: 中央) を判定する。
    アーマチュア内の実ボーン座標を優先し、なければ名前(.L/.R)から判定する。
    """
    if not vg_name:
        return 0
    arm_obj = get_related_armature(obj)
    if arm_obj and arm_obj.data and vg_name in arm_obj.data.bones:
        bone = arm_obj.data.bones[vg_name]
        mid_x = 0.5 * (bone.head_local.x + bone.tail_local.x)
        if abs(mid_x) > 1e-4:
            return 1 if mid_x > 0.0 else -1
    side = get_side_category(vg_name)
    if side == 'LEFT':
        return 1
    if side == 'RIGHT':
        return -1
    return 0


def ensure_opposite_vertex_groups(obj) -> dict[int, int]:
    """
    メッシュ内の全頂点グループについて、左右反対側の頂点グループ（.L <-> .R）が存在しなければ自動作成し、
    {group_index: opposite_group_index} の辞書を返す（BMeshのdeformレイヤー取得前に呼ぶこと）。
    """
    if not obj or obj.type != 'MESH':
        return {}
    prev_active = obj.vertex_groups.active_index
    existing_names = [vg.name for vg in obj.vertex_groups]
    for name in existing_names:
        opp = flip_side_name(name)
        if opp != name and opp not in obj.vertex_groups:
            obj.vertex_groups.new(name=opp)
    if 0 <= prev_active < len(obj.vertex_groups):
        obj.vertex_groups.active_index = prev_active

    opp_map = {}
    for vg in obj.vertex_groups:
        opp = flip_side_name(vg.name)
        opp_vg = obj.vertex_groups.get(opp)
        opp_map[vg.index] = opp_vg.index if opp_vg else vg.index
    return opp_map


def build_mesh_mirror_map(obj) -> tuple[dict[int, int], list[Vector]]:
    """
    メッシュのローカルX軸対称頂点マップ {v_idx: mirror_v_idx} と頂点座標リストをKDTreeで高速構築する。
    """
    mesh = obj.data
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(mesh)
        bm.verts.ensure_lookup_table()
        coords = [v.co.copy() for v in bm.verts]
    else:
        coords = [v.co.copy() for v in mesh.vertices]

    n = len(coords)
    if n == 0:
        return {}, []

    kd = KDTree(n)
    for i, co in enumerate(coords):
        kd.insert(co, i)
    kd.balance()

    dim_x = obj.dimensions.x if obj.dimensions.x > 1e-4 else 1.0
    tol = max(0.05, dim_x * 0.05)

    mirror_map = {}
    for i, co in enumerate(coords):
        if abs(co.x) <= 1e-4:
            mirror_map[i] = i
        else:
            _, m_idx, dist = kd.find(Vector((-co.x, co.y, co.z)))
            if m_idx is not None and dist <= tol and (coords[m_idx].x * co.x) <= 1e-4:
                mirror_map[i] = m_idx
    return mirror_map, coords


# ============================================================================
# プロパティグループ
# ============================================================================

class WPM_Settings(PropertyGroup):
    last_mesh_name: StringProperty(
        name="直前の対象メッシュ名",
        default="",
    )
    bone_filter_mode: EnumProperty(
        name="ボーン種類フィルタ",
        description="一覧に表示するボーン（頂点グループ）の種類を絞り込みます",
        items=[
            ('ALL', "すべて", "すべての頂点グループを表示します"),
            ('SELECTED_VERTS', "選択頂点", "現在選択している頂点にウェイトが入っているボーンのみを表示します"),
            ('DEFORM', "変形ボーン", "アーマチュアの変形用ボーン（Deform）のみを表示します"),
            ('CENTER', "中央・体幹", "左右識別子（.L/.R）を持たない中央・体幹のボーンのみを表示します"),
            ('LEFT', "左 (.L)", "左側（.L / _L）のボーンのみを表示します"),
            ('RIGHT', "右 (.R)", "右側（.R / _R）のボーンのみを表示します"),
        ],
        default='ALL',
    )
    use_mirror_x_on_edit: BoolProperty(
        name="左右対称 (Xミラー) で適用",
        description="編集モード等で頂点にウェイトを振ったりホイールぼかしを行った際、反対側の対称頂点（.L ↔ .R ボーン自動変換）にも自動で左右対称にウェイトを適用します",
        default=True,
    )
    auto_normalize_on_edit: BoolProperty(
        name="数値変更時に自動正規化",
        description="ウェイトのセットや加減算・ぼかしを行った際に自動で正規化を実行します",
        default=True,
    )
    clean_threshold: FloatProperty(
        name="クリーンアップ閾値",
        description="この値以下の微小なウェイトを頂点グループから削除します",
        default=0.001,
        min=0.0,
        max=0.5,
        precision=4,
    )
    max_bone_influences: IntProperty(
        name="最大影響ボーン数",
        description="1頂点あたりの最大影響頂点グループ数（ゲームエンジン向けは4が一般的）",
        default=4,
        min=1,
        max=32,
    )
    blur_default_expand_rings: IntProperty(
        name="初期ぼかし拡張リング数",
        description="選択頂点から周囲何リング分まで一緒にぼかすかの初期値（Shift+ホイールで操作中にも変更可能）",
        default=1,
        min=0,
        max=15,
    )
    blur_step_factor: FloatProperty(
        name="1スクロールあたりのぼかし強度",
        description="マウスホイール1回転あたりの隣接頂点とのブレンド率",
        default=0.45,
        min=0.05,
        max=1.0,
    )


# ============================================================================
# 0-A. モード高速移動（ポーズモード ⇔ ウェイトペイント ⇔ 編集モード）＆ ポーズ全クリア
# ============================================================================

class WPM_OT_jump_mode(Operator):
    """メッシュ（Weight Paint / Edit）とアーマチュア（Poseモード）の間をワンクリックで行き来します"""
    bl_idname = "wpm.jump_mode"
    bl_label = "モード高速切替"
    bl_options = {'REGISTER', 'UNDO'}

    target_mode: EnumProperty(
        name="移動先モード",
        items=[
            ('POSE', "ポーズモード (Pose)", "関連アーマチュアを選択してポーズモードへ飛びます"),
            ('WEIGHT_PAINT', "ウェイトペイント (Weight Paint)", "対象メッシュのウェイトペイントモードへ移動します"),
            ('EDIT', "編集モード (Edit)", "対象メッシュの編集モード（ウェイト色表示ON）へ移動します"),
        ],
        default='POSE',
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type in {'MESH', 'ARMATURE'}

    def execute(self, context):
        obj = context.active_object
        settings = getattr(context.scene, "wpm_settings", None)

        if obj.type == 'MESH' and settings:
            settings.last_mesh_name = obj.name

        if self.target_mode == 'POSE':
            arm_obj = get_related_armature(obj)
            if not arm_obj:
                self.report({'WARNING'}, "関連付けられたアーマチュアが見つかりません")
                return {'CANCELLED'}

            arm_obj.hide_set(False)
            arm_obj.hide_viewport = False

            vg_name = obj.vertex_groups.active.name if (obj.type == 'MESH' and obj.vertex_groups.active) else None

            if context.active_object and context.active_object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            for o in context.selected_objects:
                o.select_set(False)

            arm_obj.select_set(True)
            context.view_layer.objects.active = arm_obj
            bpy.ops.object.mode_set(mode='POSE')

            if vg_name:
                select_armature_bone_safe(arm_obj, vg_name)

            return {'FINISHED'}

        else:
            mesh_obj = get_target_mesh_object(context)
            if not mesh_obj:
                self.report({'WARNING'}, "対象のメッシュが見つかりません")
                return {'CANCELLED'}

            arm_obj = get_related_armature(mesh_obj)

            active_bone_name = None
            if obj.type == 'ARMATURE' and obj.data.bones.active:
                active_bone_name = obj.data.bones.active.name

            if context.active_object and context.active_object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')

            for o in context.selected_objects:
                o.select_set(False)

            if self.target_mode == 'WEIGHT_PAINT' and arm_obj:
                arm_obj.hide_set(False)
                arm_obj.select_set(True)
                context.view_layer.objects.active = arm_obj
                try:
                    bpy.ops.object.mode_set(mode='POSE')
                except RuntimeError:
                    pass

            mesh_obj.hide_set(False)
            mesh_obj.select_set(True)
            context.view_layer.objects.active = mesh_obj

            if active_bone_name and active_bone_name in mesh_obj.vertex_groups:
                mesh_obj.vertex_groups.active_index = mesh_obj.vertex_groups[active_bone_name].index

            bpy.ops.object.mode_set(mode=self.target_mode)
            if self.target_mode == 'EDIT':
                ensure_edit_mode_weight_overlay(context, True)

            return {'FINISHED'}


class WPM_OT_clear_all_pose(Operator):
    """アーマチュア内の全ボーンのポーズ（位置・回転・スケール）をワンクリックで初期状態にリセットします（ボーン未選択でも全リセット）"""
    bl_idname = "wpm.clear_all_pose"
    bl_label = "ポーズをすべてクリア (全ボーン初期化)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return get_related_armature(obj) is not None

    def execute(self, context):
        arm_obj = get_related_armature(context.active_object)
        if not arm_obj or not arm_obj.pose:
            self.report({'WARNING'}, "対象のアーマチュアが見つかりません")
            return {'CANCELLED'}

        for pb in arm_obj.pose.bones:
            pb.location = (0.0, 0.0, 0.0)
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            pb.scale = (1.0, 1.0, 1.0)

        arm_obj.update_tag()
        context.view_layer.update()
        self.report({'INFO'}, f"'{arm_obj.name}' の全ボーンのポーズをリセットしました")
        return {'FINISHED'}


# ============================================================================
# 0-B. 表示切替（ウェイト等高線 / ウェイトペイントワイヤー / Editウェイト色 / アーマチュア表示）
# ============================================================================

class WPM_OT_toggle_viewport_display(Operator):
    """ウェイト等高線・ウェイトペイントワイヤー・編集モードウェイト色・アーマチュア表示（最前面/名前/座標軸）を切り替えます"""
    bl_idname = "wpm.toggle_viewport_display"
    bl_label = "ビューポート表示切替"
    bl_options = {'REGISTER'}

    display_type: EnumProperty(
        name="表示項目",
        items=[
            ('CONTOURS', "ウェイト等高線 (Weight Contours)", "ウェイトペイントオーバーレイの等高線表示のON/OFFを切り替えます"),
            ('WIREFRAME', "ウェイトペイントのワイヤー (Paint Wire)", "ウェイトペイントオーバーレイのワイヤー表示のON/OFFを切り替えます"),
            ('EDIT_WEIGHT', "編集モードのウェイト色 (Show Weights)", "編集モードでのウェイトカラー表示のON/OFFを切り替えます"),
            ('BONE_IN_FRONT', "ボーン最前面 (In Front)", "アーマチュアのボーンを常にメッシュの手前に表示するか切り替えます"),
            ('BONE_NAMES', "ボーン名表示 (Names)", "アーマチュアのボーン名の表示ON/OFFを切り替えます"),
            ('BONE_AXES', "ボーン座標軸表示 (Axes)", "アーマチュアのボーン座標軸の表示ON/OFFを切り替えます"),
        ],
        default='CONTOURS',
    )

    def execute(self, context):
        space = context.space_data
        overlay = space.overlay if (space and space.type == 'VIEW_3D' and hasattr(space, "overlay")) else None
        arm_obj = get_related_armature(context.active_object)
        arm_data = arm_obj.data if arm_obj else None

        if self.display_type == 'CONTOURS' and overlay:
            if hasattr(overlay, "show_wpaint_contours"):
                overlay.show_wpaint_contours = not overlay.show_wpaint_contours
            elif hasattr(overlay, "show_weight_contours"):
                overlay.show_weight_contours = not overlay.show_weight_contours
        elif self.display_type == 'WIREFRAME' and overlay:
            if hasattr(overlay, "show_paint_wire"):
                overlay.show_paint_wire = not overlay.show_paint_wire
        elif self.display_type == 'EDIT_WEIGHT' and overlay:
            if hasattr(overlay, "show_weight"):
                overlay.show_weight = not overlay.show_weight
        elif self.display_type == 'BONE_IN_FRONT' and arm_obj:
            if hasattr(arm_obj, "show_in_front"):
                arm_obj.show_in_front = not arm_obj.show_in_front
        elif self.display_type == 'BONE_NAMES' and arm_data:
            if hasattr(arm_data, "show_names"):
                arm_data.show_names = not arm_data.show_names
        elif self.display_type == 'BONE_AXES' and arm_data:
            if hasattr(arm_data, "show_axes"):
                arm_data.show_axes = not arm_data.show_axes

        return {'FINISHED'}


# ============================================================================
# 0-C. ボーン（頂点グループ）種類別フィルタ付き UIList & 切替オペレーター
# ============================================================================

class WPM_UL_vertex_groups(UIList):
    """ボーン種類別フィルタ＆選択頂点の平均ウェイト表示付き頂点グループ一覧"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = data
        vg = item
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            deform_bones = get_deform_bone_names(obj)
            is_bone = vg.name in deform_bones
            row.prop(vg, "name", text="", emboss=False, icon='BONE_DATA' if is_bone else 'GROUP_VERTEX')

            sel_weights = getattr(self, "_cached_sel_weights", None)
            if sel_weights is None:
                sel_weights = get_selected_verts_group_weights(obj)
            if vg.index in sel_weights:
                sub = row.row(align=True)
                sub.alignment = 'RIGHT'
                sub.label(text=f"{sel_weights[vg.index]:.2f}")

            flipped = flip_side_name(vg.name)
            if flipped != vg.name and flipped in obj.vertex_groups:
                op = row.operator("wpm.select_vertex_group_by_name", text="", icon='ARROW_LEFTRIGHT', emboss=False)
                op.group_name = flipped

            row.prop(vg, "lock_weight", text="", icon='LOCKED' if vg.lock_weight else 'UNLOCKED', emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=vg.name)

    def filter_items(self, context, data, propname):
        obj = data
        vgroups = getattr(data, propname)
        settings = getattr(context.scene, "wpm_settings", None)
        f_mode = getattr(settings, "bone_filter_mode", 'ALL') if settings else 'ALL'

        flt_flags = [self.bitflag_filter_item] * len(vgroups)
        flt_neworder = []

        if self.filter_name:
            flt_flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, vgroups, "name", reverse=self.use_filter_invert
            )

        deform_bones = get_deform_bone_names(obj) if f_mode == 'DEFORM' else set()
        sel_weights = get_selected_verts_group_weights(obj)
        self._cached_sel_weights = sel_weights

        for i, vg in enumerate(vgroups):
            if not (flt_flags[i] & self.bitflag_filter_item):
                continue

            keep = True
            if f_mode == 'SELECTED_VERTS':
                keep = (vg.index in sel_weights)
            elif f_mode == 'DEFORM':
                keep = (vg.name in deform_bones) if deform_bones else True
            elif f_mode in {'LEFT', 'RIGHT', 'CENTER'}:
                keep = (get_side_category(vg.name) == f_mode)

            if not keep:
                flt_flags[i] &= ~self.bitflag_filter_item

        if self.use_filter_sort_alpha:
            flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(vgroups, "name")

        return flt_flags, flt_neworder


class WPM_OT_select_vertex_group_by_name(Operator):
    """指定した名前のボーン（頂点グループ）をアクティブにし、アーマチュアのボーン選択も同期します"""
    bl_idname = "wpm.select_vertex_group_by_name"
    bl_label = "ボーン / 頂点グループを選択"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: StringProperty(name="グループ名")

    @classmethod
    def poll(cls, context):
        return get_target_mesh_object(context) is not None

    def execute(self, context):
        obj = get_target_mesh_object(context)
        if not obj:
            return {'CANCELLED'}
        if self.group_name in obj.vertex_groups:
            obj.vertex_groups.active_index = obj.vertex_groups[self.group_name].index
            sync_pose_bone_with_active_vg(context, obj)
            if obj.mode == 'EDIT':
                ensure_edit_mode_weight_overlay(context, True)
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            else:
                obj.data.update()
        else:
            self.report({'WARNING'}, f"グループ '{self.group_name}' が見つかりません")
            return {'CANCELLED'}
        return {'FINISHED'}


class WPM_OT_switch_opposite_bone(Operator):
    """現在アクティブなボーンの左右反対側（.L ↔ .R）のボーンに瞬時に切り替えます"""
    bl_idname = "wpm.switch_opposite_bone"
    bl_label = "左右反対のボーンへ切替 (.L ↔ .R)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = get_target_mesh_object(context)
        return obj is not None and obj.vertex_groups.active is not None

    def execute(self, context):
        obj = get_target_mesh_object(context)
        cur_name = obj.vertex_groups.active.name
        opp_name = flip_side_name(cur_name)
        if opp_name == cur_name:
            self.report({'INFO'}, f"'{cur_name}' には左右の識別子 (.L/.R) がありません")
            return {'CANCELLED'}
        if opp_name not in obj.vertex_groups:
            obj.vertex_groups.new(name=opp_name)
        obj.vertex_groups.active_index = obj.vertex_groups[opp_name].index
        sync_pose_bone_with_active_vg(context, obj)
        if obj.mode == 'EDIT':
            ensure_edit_mode_weight_overlay(context, True)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        else:
            obj.data.update()
        self.report({'INFO'}, f"アクティブボーン切替: {cur_name} → {opp_name}")
        return {'FINISHED'}


class VIEW3D_MT_wpm_bone_select_menu(Menu):
    """選択頂点に影響しているボーン（または全ボーン）を素早く切り替えるポップアップメニュー"""
    bl_idname = "VIEW3D_MT_wpm_bone_select_menu"
    bl_label = "ボーン / 頂点グループ切替"

    def draw(self, context):
        layout = self.layout
        obj = get_target_mesh_object(context)
        if not obj or obj.type != 'MESH':
            layout.label(text="メッシュが選択されていません")
            return

        sel_weights = get_selected_verts_group_weights(obj)
        active_vg = obj.vertex_groups.active

        if sel_weights:
            layout.label(text="【選択頂点に影響中のボーン】", icon='VERTEXSEL')
            sorted_groups = sorted(sel_weights.items(), key=lambda x: x[1], reverse=True)
            for g_idx, avg_w in sorted_groups:
                if g_idx < len(obj.vertex_groups):
                    vg = obj.vertex_groups[g_idx]
                    is_act = (active_vg and active_vg.index == vg.index)
                    op = layout.operator(
                        "wpm.select_vertex_group_by_name",
                        text=f"{'★ ' if is_act else ''}{vg.name}  ({avg_w:.2f})",
                        icon='BONE_DATA',
                    )
                    op.group_name = vg.name
            layout.separator()

        layout.label(text="【全ボーン / 頂点グループ】", icon='GROUP_VERTEX')
        for vg in obj.vertex_groups:
            is_act = (active_vg and active_vg.index == vg.index)
            op = layout.operator(
                "wpm.select_vertex_group_by_name",
                text=f"{'★ ' if is_act else ''}{vg.name}",
                icon='RADIOBUT_ON' if is_act else 'RADIOBUT_OFF',
            )
            op.group_name = vg.name


# ============================================================================
# 1. 選択頂点のウェイト一括設定・加減算・正規化・クリーンアップ
# ============================================================================

class WPM_OT_set_vertex_weight(Operator):
    """選択頂点（または全頂点）のウェイトを指定値に設定、または加減算します（Editモード直接更新＆Xミラー対称対応）"""
    bl_idname = "wpm.set_vertex_weight"
    bl_label = "頂点ウェイト数値設定"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="モード",
        items=[
            ('SET', "設定 (Set)", "指定したウェイト値をそのまま割り当てます"),
            ('ADD', "加算/減算 (Add)", "現在のウェイト値に指定値を加算（負数で減算）します"),
            ('SCALE', "乗算 (Multiply)", "現在のウェイト値に指定倍率を掛けます"),
        ],
        default='SET',
    )
    value: FloatProperty(
        name="値",
        description="適用するウェイト値または変化量",
        default=1.0,
        min=-1.0,
        max=2.0,
    )
    sync_brush_weight: BoolProperty(
        name="ブラシウェイトにも反映",
        description="SETモード時にブラシのウェイト値も同時に更新します",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return get_target_mesh_object(context) is not None

    def execute(self, context):
        obj = get_target_mesh_object(context)
        if not obj:
            return {'CANCELLED'}
        vg = ensure_active_vertex_group(obj)
        vg_idx = vg.index
        vg_name = vg.name
        mesh = obj.data
        settings = getattr(context.scene, "wpm_settings", None)
        do_mirror = bool(settings and settings.use_mirror_x_on_edit)

        opp_group_map = {}
        opp_vg_idx = vg_idx
        is_sided_bone = False
        mirror_map = {}
        coords = []
        bone_x_sign = 0

        if do_mirror:
            opp_group_map = ensure_opposite_vertex_groups(obj)
            opp_vg_idx = opp_group_map.get(vg_idx, vg_idx)
            is_sided_bone = (opp_vg_idx != vg_idx)
            mirror_map, coords = build_mesh_mirror_map(obj)
            bone_x_sign = get_bone_x_sign(obj, vg_name)

        do_norm = bool(settings and settings.auto_normalize_on_edit and len(obj.vertex_groups) > 1)

        if obj.mode == 'EDIT':
            ensure_edit_mode_weight_overlay(context, True)
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            deform = bm.verts.layers.deform.verify()
            selected_indices = [v.index for v in bm.verts if v.select]
            if not selected_indices:
                selected_indices = [v.index for v in bm.verts]
            selected_set = set(selected_indices)

            if do_mirror and is_sided_bone and bone_x_sign != 0:
                source_indices = []
                for idx in selected_indices:
                    m_idx = mirror_map.get(idx)
                    if m_idx is not None and m_idx != idx and m_idx in selected_set:
                        if (coords[idx].x * bone_x_sign) < -1e-4:
                            continue
                    source_indices.append(idx)
                if not source_indices:
                    source_indices = selected_indices
            else:
                source_indices = selected_indices

            for v_idx in source_indices:
                v = bm.verts[v_idx]
                dv = v[deform]
                cur_w = dv.get(vg_idx, 0.0)
                if self.mode == 'SET':
                    new_w = max(0.0, min(1.0, self.value))
                elif self.mode == 'ADD':
                    new_w = max(0.0, min(1.0, cur_w + self.value))
                else:
                    new_w = max(0.0, min(1.0, cur_w * self.value))

                is_center_vert = do_mirror and (mirror_map.get(v_idx) == v_idx)

                if is_center_vert and is_sided_bone:
                    if do_norm and (2.0 * new_w >= 1.0):
                        dv.clear()
                        dv[vg_idx] = 0.5
                        dv[opp_vg_idx] = 0.5
                    else:
                        dv[vg_idx] = new_w
                        dv[opp_vg_idx] = new_w
                        if do_norm:
                            other_keys = [k for k in dv.keys() if k not in (vg_idx, opp_vg_idx)]
                            if other_keys:
                                rem = max(0.0, 1.0 - 2.0 * new_w)
                                other_sum = sum(dv[k] for k in other_keys)
                                if other_sum > 1e-6:
                                    scale = rem / other_sum
                                    for k in other_keys:
                                        dv[k] = dv[k] * scale
                                elif rem > 0.0:
                                    share = rem / len(other_keys)
                                    for k in other_keys:
                                        dv[k] = share
                else:
                    dv[vg_idx] = new_w
                    if do_norm:
                        other_keys = [k for k in dv.keys() if k != vg_idx]
                        if other_keys:
                            rem = max(0.0, 1.0 - new_w)
                            other_sum = sum(dv[k] for k in other_keys)
                            if other_sum > 1e-6:
                                scale = rem / other_sum
                                for k in other_keys:
                                    dv[k] = dv[k] * scale
                            elif rem > 0.0:
                                share = rem / len(other_keys)
                                for k in other_keys:
                                    dv[k] = share

                    if do_mirror:
                        m_idx = mirror_map.get(v_idx)
                        if m_idx is not None and m_idx != v_idx:
                            m_dv = bm.verts[m_idx][deform]
                            src_items = list(dv.items())
                            m_dv.clear()
                            for k, w in src_items:
                                m_dv[opp_group_map.get(k, k)] = w

            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        else:
            indices = get_target_vertex_indices(obj)
            if not indices:
                self.report({'WARNING'}, "対象の頂点がありません")
                return {'CANCELLED'}
            selected_set = set(indices)

            if do_mirror and is_sided_bone and bone_x_sign != 0:
                source_indices = []
                for idx in indices:
                    m_idx = mirror_map.get(idx)
                    if m_idx is not None and m_idx != idx and m_idx in selected_set:
                        if (coords[idx].x * bone_x_sign) < -1e-4:
                            continue
                    source_indices.append(idx)
                if not source_indices:
                    source_indices = indices
            else:
                source_indices = indices

            vgroups = obj.vertex_groups
            for idx in source_indices:
                cur_w = 0.0
                for g in mesh.vertices[idx].groups:
                    if g.group == vg_idx:
                        cur_w = g.weight
                        break

                if self.mode == 'SET':
                    new_w = max(0.0, min(1.0, self.value))
                elif self.mode == 'ADD':
                    new_w = max(0.0, min(1.0, cur_w + self.value))
                else:
                    new_w = max(0.0, min(1.0, cur_w * self.value))

                vg.add([idx], new_w, 'REPLACE')
                if do_mirror:
                    m_idx = mirror_map.get(idx)
                    if m_idx is not None:
                        vgroups[opp_vg_idx].add([m_idx], new_w, 'REPLACE')

            mesh.update()
            if do_norm and context.active_object == obj:
                try:
                    bpy.ops.object.vertex_group_normalize_all(group_select_mode='ALL', lock_active=True)
                except RuntimeError:
                    pass

        if self.mode == 'SET' and self.sync_brush_weight:
            set_current_brush_weight(context, max(0.0, min(1.0, self.value)))

        return {'FINISHED'}


class WPM_OT_mirror_vertex_weights(Operator):
    """編集モード等で頂点に振ったウェイトを、反対側の対称頂点（.L ↔ .R ボーン自動変換）へ左右対称に適用します"""
    bl_idname = "wpm.mirror_vertex_weights"
    bl_label = "左右対称にウェイトを適用 (Mirror)"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="対称化の方向",
        items=[
            ('SELECTED', "選択頂点 → 反対側へ対称適用", "現在選択している頂点のウェイトを反対側の対称頂点（.L ↔ .R 自動変換）へミラー適用します"),
            ('L_TO_R', "左 (.L) → 右 (.R) 全頂点対称化", "左側の全頂点のウェイトを右側へミラーコピーしてメッシュ全体を左右対称にします"),
            ('R_TO_L', "右 (.R) → 左 (.L) 全頂点対称化", "右側の全頂点のウェイトを左側へミラーコピーしてメッシュ全体を左右対称にします"),
        ],
        default='SELECTED',
    )

    @classmethod
    def poll(cls, context):
        obj = get_target_mesh_object(context)
        return obj is not None and len(obj.vertex_groups) > 0

    def execute(self, context):
        obj = get_target_mesh_object(context)
        if not obj:
            return {'CANCELLED'}
        mesh = obj.data
        opp_group_map = ensure_opposite_vertex_groups(obj)
        mirror_map, coords = build_mesh_mirror_map(obj)
        if not mirror_map:
            self.report({'WARNING'}, "対称な頂点が見つかりませんでした")
            return {'CANCELLED'}

        act_vg_name = obj.vertex_groups.active.name if obj.vertex_groups.active else ""
        active_sign = get_bone_x_sign(obj, act_vg_name)
        if active_sign == 0:
            active_sign = 1  # デフォルトは +X (左.L) 基準

        # ソース頂点の決定
        if self.direction == 'L_TO_R':
            left_sign = get_bone_x_sign(obj, ".L") or 1
            source_indices = [i for i, co in enumerate(coords) if (co.x * left_sign) >= -1e-4]
        elif self.direction == 'R_TO_L':
            right_sign = get_bone_x_sign(obj, ".R") or -1
            source_indices = [i for i, co in enumerate(coords) if (co.x * right_sign) >= -1e-4]
        else:
            if obj.mode == 'EDIT':
                bm_tmp = bmesh.from_edit_mesh(mesh)
                bm_tmp.verts.ensure_lookup_table()
                sel_indices = [v.index for v in bm_tmp.verts if v.select]
            else:
                sel_indices = [v.index for v in mesh.vertices if v.select]

            if not sel_indices or len(sel_indices) == len(coords):
                source_indices = [i for i, co in enumerate(coords) if (co.x * active_sign) >= -1e-4]
            else:
                sel_set = set(sel_indices)
                source_indices = []
                for idx in sel_indices:
                    m_idx = mirror_map.get(idx)
                    # 両側とも選択されているペアはアクティブボーン側をコピー元にする
                    if m_idx is not None and m_idx != idx and m_idx in sel_set:
                        if (coords[idx].x * active_sign) < -1e-4:
                            continue
                    source_indices.append(idx)
                if not source_indices:
                    source_indices = sel_indices

        mirrored_count = 0

        if obj.mode == 'EDIT':
            ensure_edit_mode_weight_overlay(context, True)
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            deform = bm.verts.layers.deform.verify()

            # 元のウェイトのスナップショットを取得してから書き込む（順序依存を防止）
            snapshot = {
                idx: dict(bm.verts[idx][deform].items())
                for idx in source_indices
            }

            for v_idx in source_indices:
                m_idx = mirror_map.get(v_idx)
                if m_idx is None:
                    continue
                src_w = snapshot[v_idx]
                if m_idx == v_idx:
                    # 中央線上の頂点は .L と .R のウェイトを対称化
                    dv = bm.verts[v_idx][deform]
                    new_center = {}
                    for k, w in src_w.items():
                        opp_k = opp_group_map.get(k, k)
                        if opp_k != k:
                            if opp_k in src_w:
                                new_center[k] = 0.5 * (w + src_w[opp_k])
                                new_center[opp_k] = 0.5 * (w + src_w[opp_k])
                            else:
                                new_center[k] = w
                                new_center[opp_k] = w
                        else:
                            new_center[k] = w
                    total_c = sum(new_center.values())
                    if total_c > 1e-6:
                        inv_c = 1.0 / total_c
                        for k in new_center:
                            new_center[k] *= inv_c
                    dv.clear()
                    for k, w in new_center.items():
                        if w > 1e-5:
                            dv[k] = w
                    mirrored_count += 1
                else:
                    m_dv = bm.verts[m_idx][deform]
                    m_dv.clear()
                    for k, w in src_w.items():
                        if w > 1e-5:
                            m_dv[opp_group_map.get(k, k)] = w
                    mirrored_count += 1

            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        else:
            vgroups = obj.vertex_groups
            snapshot = {
                idx: {g.group: g.weight for g in mesh.vertices[idx].groups}
                for idx in source_indices
            }
            for v_idx in source_indices:
                m_idx = mirror_map.get(v_idx)
                if m_idx is None or m_idx == v_idx:
                    continue
                src_w = snapshot[v_idx]
                m_map = {opp_group_map.get(k, k): w for k, w in src_w.items() if w > 1e-5}
                for vg in vgroups:
                    g_idx = vg.index
                    if g_idx in m_map:
                        vg.add([m_idx], m_map[g_idx], 'REPLACE')
                    else:
                        vg.remove([m_idx])
                mirrored_count += 1
            mesh.update()

        self.report({'INFO'}, f"{mirrored_count} 頂点のウェイトを左右対称にミラー適用しました")
        return {'FINISHED'}


class WPM_OT_quick_clean_and_normalize(Operator):
    """微小ウェイト削除・影響ボーン数制限・全体正規化をワンクリックで実行します"""
    bl_idname = "wpm.quick_clean_and_normalize"
    bl_label = "ウェイト最適化 (Clean + Limit + Normalize)"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="処理内容",
        items=[
            ('NORMALIZE_ALL', "全て正規化 (Normalize All)", "全頂点グループの合計ウェイトを1.0に正規化します（アクティブ固定）"),
            ('NORMALIZE_ACTIVE', "アクティブのみ正規化", "アクティブな頂点グループを0〜1に正規化します"),
            ('CLEAN', "微小ウェイト削除 (Clean)", "閾値以下の不要な0ウェイト割当をクリーンアップします"),
            ('LIMIT', "影響ボーン数制限 (Limit Total)", "1頂点あたりの影響ボーン数を上限数（デフォルト4）に制限します"),
            ('FULL_OPTIMIZE', "一括最適化 (Clean + 4ボーン制限 + 正規化)", "クリーンアップ・ボーン数制限・正規化をまとめて実行します"),
            ('INVERT', "ウェイト反転 (Invert)", "アクティブ頂点グループのウェイトを反転します"),
        ],
        default='FULL_OPTIMIZE',
    )

    @classmethod
    def poll(cls, context):
        obj = get_target_mesh_object(context)
        return obj is not None and len(obj.vertex_groups) > 0

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            bpy.ops.wpm.jump_mode(target_mode='WEIGHT_PAINT')

        settings = context.scene.wpm_settings
        try:
            if self.action == 'NORMALIZE_ALL':
                bpy.ops.object.vertex_group_normalize_all(group_select_mode='ALL', lock_active=True)
            elif self.action == 'NORMALIZE_ACTIVE':
                bpy.ops.object.vertex_group_normalize()
            elif self.action == 'CLEAN':
                bpy.ops.object.vertex_group_clean(group_select_mode='ALL', limit=settings.clean_threshold)
            elif self.action == 'LIMIT':
                bpy.ops.object.vertex_group_limit_total(group_select_mode='ALL', limit=settings.max_bone_influences)
            elif self.action == 'INVERT':
                bpy.ops.object.vertex_group_invert()
            elif self.action == 'FULL_OPTIMIZE':
                bpy.ops.object.vertex_group_clean(group_select_mode='ALL', limit=settings.clean_threshold)
                bpy.ops.object.vertex_group_limit_total(group_select_mode='ALL', limit=settings.max_bone_influences)
                bpy.ops.object.vertex_group_normalize_all(group_select_mode='ALL', lock_active=False)
                self.report(
                    {'INFO'},
                    f"最適化完了 (閾値:{settings.clean_threshold:.4f}, 最大ボーン:{settings.max_bone_influences})"
                )
        except RuntimeError as e:
            self.report({'WARNING'}, f"実行できませんでした: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}


# ============================================================================
# 2. 編集モード/ウェイトペイント両対応：マウスホイール対話ウェイトぼかし
# ============================================================================

def _draw_scroll_blur_hud(op, context):
    """3Dビューポート上にマウスホイールぼかしの状態HUDを描画するコールバック"""
    font_id = 0
    x, y = op._mouse_x + 24, op._mouse_y + 24

    target_str = "全頂点グループ (自動正規化)" if op.affect_all_groups else "アクティブグループのみ"
    mirror_str = "ON (.L ↔ .R 自動対称)" if op.use_mirror_x else "OFF"

    lines = [
        f"【マウスホイールぼかし】 確定: 左クリック/Enter | キャンセル: 右クリック/Esc",
        f"  [ホイール上下] ぼかし反復回数 (Steps) : {op.steps} 回",
        f"  [Shift + ホイール] 周囲への広がり (Rings) : +{op.expand_rings} リング",
        f"  [Ctrl + ホイール] ぼかし強度 (Factor) : {op.factor:.2f}",
        f"  [A キー切替] 対象 : {target_str}",
        f"  [X キー切替] 左右対称ミラー (X-Mirror) : {mirror_str}",
    ]

    blf.size(font_id, 15)
    line_height = 22
    for i, text in enumerate(reversed(lines)):
        blf.position(font_id, x + 1, y + i * line_height - 1, 0)
        blf.color(font_id, 0.0, 0.0, 0.0, 0.85)
        blf.draw(font_id, text)

        blf.position(font_id, x, y + i * line_height, 0)
        if i == len(lines) - 1:
            blf.color(font_id, 1.0, 0.85, 0.3, 1.0)
        elif i == len(lines) - 2:
            blf.color(font_id, 0.4, 1.0, 0.6, 1.0)
        else:
            blf.color(font_id, 0.95, 0.95, 0.95, 1.0)
        blf.draw(font_id, text)


class WPM_OT_interactive_scroll_blur(Operator):
    """選択した頂点からマウスホイールの上下で直感的にウェイトをぼかします（編集モード・ウェイトペイント両対応＆Xミラー対称対応）"""
    bl_idname = "wpm.interactive_scroll_blur"
    bl_label = "マウスホイールでウェイトぼかし (Scroll Blur)"
    bl_options = {'REGISTER', 'UNDO', 'GRAB_CURSOR', 'BLOCKING'}

    steps: IntProperty(
        name="ぼかし回数",
        default=1,
        min=0,
        max=100,
    )
    expand_rings: IntProperty(
        name="周囲リング拡張",
        description="選択頂点から周囲何リング分の隣接頂点まで一緒にぼかすか",
        default=1,
        min=0,
        max=20,
    )
    factor: FloatProperty(
        name="ぼかし強度",
        default=0.45,
        min=0.05,
        max=1.0,
    )
    affect_all_groups: BoolProperty(
        name="全グループを対象 (自動正規化)",
        default=True,
    )
    use_mirror_x: BoolProperty(
        name="左右対称 (Xミラー)",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = get_target_mesh_object(context)
        return obj is not None and len(obj.vertex_groups) > 0

    def invoke(self, context, event):
        if context.active_object and context.active_object.type == 'ARMATURE':
            bpy.ops.wpm.jump_mode(target_mode='WEIGHT_PAINT')

        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return {'CANCELLED'}
        if obj.mode not in {'EDIT', 'WEIGHT_PAINT'}:
            bpy.ops.object.mode_set(mode='WEIGHT_PAINT')

        mesh = obj.data
        settings = getattr(context.scene, "wpm_settings", None)
        if settings:
            self.expand_rings = settings.blur_default_expand_rings
            self.factor = settings.blur_step_factor
            self.use_mirror_x = settings.use_mirror_x_on_edit

        self.steps = 1
        self._mouse_x = event.mouse_region_x
        self._mouse_y = event.mouse_region_y
        self._opp_group_map = ensure_opposite_vertex_groups(obj)
        self._mirror_map, self._coords = build_mesh_mirror_map(obj)
        self._active_vg_index = obj.vertex_groups.active_index if obj.vertex_groups.active else 0
        self._num_groups = len(obj.vertex_groups)
        act_vg_name = obj.vertex_groups.active.name if obj.vertex_groups.active else ""
        self._active_sign = get_bone_x_sign(obj, act_vg_name) or 1

        if obj.mode == 'EDIT':
            ensure_edit_mode_weight_overlay(context, True)
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            deform = bm.verts.layers.deform.verify()

            self._adj = {
                v.index: [
                    other.index
                    for e in v.link_edges
                    for other in e.verts
                    if other is not v
                ]
                for v in bm.verts
            }
            self._orig_weights = {
                v.index: dict(v[deform].items())
                for v in bm.verts
            }
            selected_indices = [v.index for v in bm.verts if v.select]
            total_verts = len(bm.verts)
        else:
            self._adj = {v.index: [] for v in mesh.vertices}
            for e in mesh.edges:
                v0, v1 = e.vertices[0], e.vertices[1]
                self._adj[v0].append(v1)
                self._adj[v1].append(v0)

            self._orig_weights = {
                v.index: {g.group: g.weight for g in v.groups}
                for v in mesh.vertices
            }
            selected_indices = [v.index for v in mesh.vertices if v.select]
            total_verts = len(mesh.vertices)

        if not selected_indices:
            selected_indices = list(self._orig_weights.keys())
            self.expand_rings = 0
        elif len(selected_indices) == total_verts:
            self.expand_rings = 0

        self._ring_dist = {}
        queue = deque()
        for idx in selected_indices:
            self._ring_dist[idx] = 0
            queue.append(idx)

        max_bfs_rings = 20
        while queue:
            curr = queue.popleft()
            d = self._ring_dist[curr]
            if d >= max_bfs_rings:
                continue
            for nbr in self._adj.get(curr, ()):
                if nbr not in self._ring_dist:
                    self._ring_dist[nbr] = d + 1
                    queue.append(nbr)

        self._apply_current_blur(context)
        self._update_header_text(context)

        self._draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_scroll_blur_hud, (self, context), 'WINDOW', 'POST_PIXEL'
        )
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _update_header_text(self, context):
        target_str = "全グループ(正規化)" if self.affect_all_groups else "アクティブのみ"
        mirror_str = "ON" if self.use_mirror_x else "OFF"
        context.area.header_text_set(
            f"【ホイールぼかし】 [ホイール上下] 回数: {self.steps} | "
            f"[Shift+ホイール] 周囲リング拡張: +{self.expand_rings} | "
            f"[Ctrl+ホイール] 強度: {self.factor:.2f} | "
            f"[A] 対象: {target_str} | [X] 左右対称: {mirror_str} | "
            f"[左クリック/Enter] 確定 | [右クリック/Esc] キャンセル"
        )

    def _apply_current_blur(self, context, restore_original=False):
        obj = context.active_object
        mesh = obj.data

        if restore_original or self.steps == 0:
            computed = {k: dict(v) for k, v in self._orig_weights.items()}
            write_vertices = list(self._orig_weights.keys())
        else:
            target_vertices = [
                v_idx for v_idx, dist in self._ring_dist.items()
                if dist <= self.expand_rings
            ]
            ring_falloff = {}
            for v_idx in target_vertices:
                dist = self._ring_dist[v_idx]
                if self.expand_rings <= 0 or dist == 0:
                    ring_falloff[v_idx] = 1.0
                else:
                    ring_falloff[v_idx] = (self.expand_rings + 1 - dist) / (self.expand_rings + 1)

            current = {k: dict(v) for k, v in self._orig_weights.items()}
            active_vg = self._active_vg_index

            for _ in range(self.steps):
                next_state = {}
                for v_idx in target_vertices:
                    nbrs = self._adj.get(v_idx)
                    if not nbrs:
                        continue
                    eff_f = self.factor * ring_falloff[v_idx]
                    inv_n = 1.0 / len(nbrs)
                    cur_v_dict = current[v_idx]

                    if self.affect_all_groups:
                        involved_groups = set(cur_v_dict.keys())
                        for n_idx in nbrs:
                            involved_groups.update(current[n_idx].keys())

                        new_v_dict = {}
                        total_w = 0.0
                        for g_idx in involved_groups:
                            w_self = cur_v_dict.get(g_idx, 0.0)
                            w_nbr_avg = sum(current[n_idx].get(g_idx, 0.0) for n_idx in nbrs) * inv_n
                            w_blended = (1.0 - eff_f) * w_self + eff_f * w_nbr_avg
                            if w_blended > 1e-5:
                                new_v_dict[g_idx] = w_blended
                                total_w += w_blended

                        if self._num_groups > 1 and total_w > 1e-5:
                            inv_total = 1.0 / total_w
                            for g_idx in new_v_dict:
                                new_v_dict[g_idx] = min(1.0, new_v_dict[g_idx] * inv_total)

                        next_state[v_idx] = new_v_dict
                    else:
                        new_v_dict = dict(cur_v_dict)
                        w_self = cur_v_dict.get(active_vg, 0.0)
                        w_nbr_avg = sum(current[n_idx].get(active_vg, 0.0) for n_idx in nbrs) * inv_n
                        w_blended = max(0.0, min(1.0, (1.0 - eff_f) * w_self + eff_f * w_nbr_avg))
                        new_v_dict[active_vg] = w_blended
                        next_state[v_idx] = new_v_dict

                for v_idx, nd in next_state.items():
                    current[v_idx] = nd

            computed = current
            write_set = set(target_vertices)

            # 左右対称ミラー (X-Mirror) の適用
            for v_idx in target_vertices:
                m_idx = self._mirror_map.get(v_idx)
                if m_idx is not None and m_idx != v_idx:
                    write_set.add(m_idx)

            if self.use_mirror_x:
                target_set = set(target_vertices)
                opp_map = self._opp_group_map
                for v_idx in target_vertices:
                    m_idx = self._mirror_map.get(v_idx)
                    if m_idx is None or m_idx == v_idx:
                        continue
                    if m_idx in target_set and (self._coords[v_idx].x * self._active_sign) < -1e-4:
                        continue
                    computed[m_idx] = {
                        opp_map.get(g_idx, g_idx): w
                        for g_idx, w in computed[v_idx].items()
                    }

            write_vertices = list(write_set)

        if obj.mode == 'EDIT':
            bm = bmesh.from_edit_mesh(mesh)
            bm.verts.ensure_lookup_table()
            deform = bm.verts.layers.deform.verify()
            for v_idx in write_vertices:
                dv = bm.verts[v_idx][deform]
                dv.clear()
                for g_idx, w in computed[v_idx].items():
                    dv[g_idx] = w
            bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
        else:
            vgroups = obj.vertex_groups
            for v_idx in write_vertices:
                w_map = computed[v_idx]
                for vg in vgroups:
                    g_idx = vg.index
                    if g_idx in w_map:
                        vg.add([v_idx], w_map[g_idx], 'REPLACE')
                    else:
                        vg.remove([v_idx])
            mesh.update()

    def _cleanup(self, context):
        if getattr(self, "_draw_handle", None):
            bpy.types.SpaceView3D.draw_handler_remove(self._draw_handle, 'WINDOW')
            self._draw_handle = None
        if context.area:
            context.area.header_text_set(None)
            context.area.tag_redraw()

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            self._mouse_x = event.mouse_region_x
            self._mouse_y = event.mouse_region_y
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'NUMPAD_PLUS', 'NUMPAD_MINUS'}:
            if event.value == 'PRESS' or event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                direction = 1 if event.type in {'WHEELUPMOUSE', 'NUMPAD_PLUS'} else -1

                if event.shift:
                    self.expand_rings = max(0, min(20, self.expand_rings + direction))
                elif event.ctrl:
                    self.factor = max(0.05, min(1.0, round(self.factor + direction * 0.05, 2)))
                else:
                    self.steps = max(0, min(100, self.steps + direction))

                self._apply_current_blur(context)
                self._update_header_text(context)
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'A' and event.value == 'PRESS':
            self.affect_all_groups = not self.affect_all_groups
            self._apply_current_blur(context)
            self._update_header_text(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'X' and event.value == 'PRESS':
            self.use_mirror_x = not self.use_mirror_x
            self._apply_current_blur(context)
            self._update_header_text(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.type == 'MIDDLEMOUSE':
            return {'PASS_THROUGH'}

        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER', 'SPACE'} and event.value == 'PRESS':
            self._cleanup(context)
            self.report({'INFO'}, f"ウェイトぼかし確定 (回数: {self.steps}, 周囲リング: +{self.expand_rings})")
            return {'FINISHED'}

        if event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._apply_current_blur(context, restore_original=True)
            self._cleanup(context)
            self.report({'INFO'}, "ウェイトぼかしをキャンセルしました")
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}


# ============================================================================
# 3. ブラシ設定＆ウェイト値のクイック切り替え (Blender 5.2 アセット対応)
# ============================================================================

class WPM_OT_select_brush_preset(Operator):
    """Blender 5.2のウェイトペイントブラシ（Draw/Add/Sub/Blur/Average/Smear）を瞬時に切り替えます"""
    bl_idname = "wpm.select_brush_preset"
    bl_label = "ブラシプリセット切替"
    bl_options = {'REGISTER', 'UNDO'}

    preset: EnumProperty(
        name="ブラシプリセット",
        items=[
            ('MIX', "Mix (通常ペイント)", "標準のミックスブラシに切り替え"),
            ('ADD', "Add (加算)", "加算ブレンドのペイントブラシに切り替え"),
            ('SUB', "Subtract (減算)", "減算ブレンドのペイントブラシに切り替え"),
            ('MUL', "Multiply (乗算)", "乗算ブレンドのペイントブラシに切り替え"),
            ('LIGHTEN', "Lighten (比較明)", "現在のウェイトより大きい場合のみ塗るブラシ"),
            ('DARKEN', "Darken (比較暗)", "現在のウェイトより小さい場合のみ塗るブラシ"),
            ('BLUR', "Blur (ぼかし・スムーズ)", "ウェイトを滑らかにぼかすブラシ"),
            ('AVERAGE', "Average (平均化)", "ブラシ範囲内のウェイトを平均化するブラシ"),
            ('SMEAR', "Smear (指先・引き伸ばし)", "ウェイトを引き伸ばすブラシ"),
        ],
        default='MIX',
    )

    @classmethod
    def poll(cls, context):
        return get_target_mesh_object(context) is not None

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or obj.mode != 'WEIGHT_PAINT':
            bpy.ops.wpm.jump_mode(target_mode='WEIGHT_PAINT')

        bpy.ops.wm.tool_set_by_id(name="builtin.brush")

        asset_map = {
            'BLUR': "brushes/essentials_brushes-mesh_weight.blend/Brush/Blur",
            'AVERAGE': "brushes/essentials_brushes-mesh_weight.blend/Brush/Average",
            'SMEAR': "brushes/essentials_brushes-mesh_weight.blend/Brush/Smear",
        }
        target_asset = asset_map.get(
            self.preset,
            "brushes/essentials_brushes-mesh_weight.blend/Brush/Paint"
        )

        try:
            bpy.ops.brush.asset_activate(
                asset_library_type='ESSENTIALS',
                asset_library_identifier="",
                relative_asset_identifier=target_asset,
            )
        except RuntimeError:
            pass

        _, wp, _ = get_weight_paint_settings(context)
        brush = wp.brush
        if brush and self.preset in {'MIX', 'ADD', 'SUB', 'MUL', 'LIGHTEN', 'DARKEN'}:
            brush.blend = self.preset

        return {'FINISHED'}


class WPM_OT_set_brush_parameter(Operator):
    """ブラシのウェイト値または強さ(Strength)をワンクリックで変更します"""
    bl_idname = "wpm.set_brush_parameter"
    bl_label = "ブラシパラメータ設定"
    bl_options = {'REGISTER', 'UNDO'}

    param_type: EnumProperty(
        name="パラメータ種別",
        items=[
            ('WEIGHT', "ウェイト (Weight)", "ブラシの目標ウェイト値"),
            ('STRENGTH', "強さ (Strength)", "ブラシの不透明度・強さ"),
        ],
        default='WEIGHT',
    )
    value: FloatProperty(
        name="値",
        default=1.0,
        min=0.0,
        max=1.0,
    )

    def execute(self, context):
        if self.param_type == 'WEIGHT':
            set_current_brush_weight(context, self.value)
        else:
            set_current_brush_strength(context, self.value)
        return {'FINISHED'}


class WPM_OT_toggle_paint_option(Operator):
    """ペイント補助機能（前面のみ・蓄積・投影フォールオフ・選択マスク）を切り替えます"""
    bl_idname = "wpm.toggle_paint_option"
    bl_label = "ペイントオプション切替"
    bl_options = {'REGISTER', 'UNDO'}

    option: EnumProperty(
        name="オプション",
        items=[
            ('AUTO_NORMALIZE', "自動正規化 (Auto Normalize)", "ペイント中の自動正規化を切り替え"),
            ('SYMMETRY_X', "X軸対称 (X Symmetry)", "X軸ミラーペイントを切り替え"),
            ('FRONTFACE', "前面のみ (Front Faces Only)", "裏面の頂点にウェイトが貫通しないように切り替え"),
            ('ACCUMULATE', "蓄積 (Accumulate)", "ストローク中のウェイト蓄積を切り替え"),
            ('FALLOFF_SHAPE', "2D投影 / 3D球体 (Falloff Shape)", "ブラシのフォールオフ形状を切り替え（貫通塗り/表面塗り）"),
            ('VERT_MASK', "頂点選択マスク", "頂点選択マスクのON/OFFを切り替え"),
            ('FACE_MASK', "面選択マスク", "面選択マスクのON/OFFを切り替え"),
        ],
        default='AUTO_NORMALIZE',
    )

    def execute(self, context):
        ts, wp, _ = get_weight_paint_settings(context)
        brush = wp.brush
        obj = get_target_mesh_object(context)

        if self.option == 'AUTO_NORMALIZE':
            ts.use_auto_normalize = not ts.use_auto_normalize
        elif self.option == 'SYMMETRY_X':
            wp.use_symmetry_x = not wp.use_symmetry_x
        elif self.option == 'FRONTFACE' and brush:
            brush.use_frontface = not brush.use_frontface
        elif self.option == 'ACCUMULATE' and brush:
            brush.use_accumulate = not brush.use_accumulate
        elif self.option == 'FALLOFF_SHAPE' and brush:
            brush.falloff_shape = 'PROJECTED' if brush.falloff_shape == 'SPHERE' else 'SPHERE'
        elif self.option == 'VERT_MASK' and obj and obj.type == 'MESH':
            obj.data.use_paint_mask_vertex = not obj.data.use_paint_mask_vertex
        elif self.option == 'FACE_MASK' and obj and obj.type == 'MESH':
            obj.data.use_paint_mask = not obj.data.use_paint_mask

        return {'FINISHED'}


# ============================================================================
# 4. 別オブジェクトからのワンクリックウェイト転送
# ============================================================================

class WPM_OT_quick_transfer_weights(Operator):
    """選択中の別メッシュ（転送元）からアクティブメッシュ（転送先）へワンクリックで高品質なウェイト転送を行います"""
    bl_idname = "wpm.quick_transfer_weights"
    bl_label = "ワンクリック・ウェイト転送 (選択 → アクティブ)"
    bl_options = {'REGISTER', 'UNDO'}

    mapping: EnumProperty(
        name="マッピング方式",
        items=[
            ('POLYINTERP_NEAREST', "最近接面補間 (衣装・別形状に最適)", "最も近いポリゴン表面から滑らかに補間して転送します"),
            ('NEAREST', "最近接頂点 (同一・類似トポロジ向け)", "最も近い頂点のウェイトをそのままコピーします"),
            ('TOPOLOGY', "トポロジ完全一致", "頂点インデックスが同一のメッシュ間で転送します"),
        ],
        default='POLYINTERP_NEAREST',
    )
    auto_clean_and_normalize: BoolProperty(
        name="転送後に最適化・正規化",
        description="転送完了後に微小ウェイト削除と正規化を自動実行します",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        active = context.active_object
        if not active or active.type != 'MESH':
            return False
        sources = [o for o in context.selected_objects if o != active and o.type == 'MESH']
        return len(sources) >= 1

    def execute(self, context):
        prev_mode = context.active_object.mode
        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        try:
            bpy.ops.object.data_transfer(
                use_reverse_transfer=True,
                data_type='VGROUP_WEIGHTS',
                use_create=True,
                vert_mapping=self.mapping,
                layers_select_src='ALL',
                layers_select_dst='NAME',
                mix_mode='REPLACE',
            )
            if self.auto_clean_and_normalize:
                settings = context.scene.wpm_settings
                bpy.ops.object.vertex_group_clean(group_select_mode='ALL', limit=settings.clean_threshold)
                bpy.ops.object.vertex_group_limit_total(group_select_mode='ALL', limit=settings.max_bone_influences)
                bpy.ops.object.vertex_group_normalize_all(group_select_mode='ALL', lock_active=False)
        except RuntimeError as e:
            if prev_mode != 'OBJECT':
                bpy.ops.object.mode_set(mode=prev_mode)
            self.report({'WARNING'}, f"ウェイト転送エラー: {e}")
            return {'CANCELLED'}

        if prev_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=prev_mode)

        self.report({'INFO'}, "選択オブジェクトからアクティブオブジェクトへウェイトを転送しました")
        return {'FINISHED'}


# ============================================================================
# 5. パイメニュー (Alt+W: メイン総合パイメニュー / Shift+W: ブラシ＆ホイールぼかし高速パイ)
# ============================================================================

class VIEW3D_MT_wpm_main_pie(Menu):
    """ウェイトペイント・編集モード・ポーズモード共通の総合パイメニュー (Alt+W)"""
    bl_idname = "VIEW3D_MT_wpm_main_pie"
    bl_label = "Weight Paint Master"

    def draw(self, context):
        layout = self.layout
        pie = layout.menu_pie()
        ts, wp, _ = get_weight_paint_settings(context)
        brush = wp.brush if wp else None
        cur_w = get_current_brush_weight(context)
        cur_s = get_current_brush_strength(context)

        active_obj = context.active_object
        mesh_obj = get_target_mesh_object(context)
        arm_obj = get_related_armature(active_obj)
        act_vg_name = mesh_obj.vertex_groups.active.name if (mesh_obj and mesh_obj.vertex_groups.active) else "未選択"

        _, show_contours, show_wire, show_edit_w, bone_front, bone_names, bone_axes = get_viewport_overlay_states(context, arm_obj)

        # [4] 西 (Left): 選択頂点のウェイト数値一括セット・加減算・左右対称ミラー
        box_left = pie.column()
        b_col = box_left.box().column(align=True)
        b_col.label(text=f"頂点ウェイト設定 [{act_vg_name}]", icon='VERTEXSEL')
        row_presets = b_col.row(align=True)
        for val in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
            op = row_presets.operator("wpm.set_vertex_weight", text=f"{val:g}")
            op.mode = 'SET'
            op.value = val

        row_delta = b_col.row(align=True)
        for delta in (-0.1, -0.05, 0.05, 0.1):
            op = row_delta.operator("wpm.set_vertex_weight", text=f"{delta:+g}")
            op.mode = 'ADD'
            op.value = delta

        settings = getattr(context.scene, "wpm_settings", None)
        if settings:
            row_opt = b_col.row(align=True)
            row_opt.prop(settings, "use_mirror_x_on_edit", text="X対称自動反映", toggle=True, icon='MOD_MIRROR')
            row_opt.prop(settings, "auto_normalize_on_edit", text="自動正規化", toggle=True)
        row_mir = b_col.row(align=True)
        row_mir.operator("wpm.mirror_vertex_weights", text="選択頂点を左右対称に適用 (Mirror)", icon='MOD_MIRROR').direction = 'SELECTED'

        # [6] 東 (Right): ブラシ切替 (Mix / Add / Sub / Blur / Average / Smear / Multiply / Lighten)
        box_right = pie.column()
        r_col = box_right.box().column(align=True)
        cur_blend = brush.blend if brush else "MIX"
        cur_btype = getattr(brush, "weight_brush_type", "DRAW") if brush else "DRAW"
        r_col.label(text=f"ブラシ切替 [{cur_btype} / {cur_blend}]", icon='BRUSH_DATA')

        row_b1 = r_col.row(align=True)
        row_b1.operator("wpm.select_brush_preset", text="Mix", depress=(cur_btype == 'DRAW' and cur_blend == 'MIX')).preset = 'MIX'
        row_b1.operator("wpm.select_brush_preset", text="Add (加算)", depress=(cur_btype == 'DRAW' and cur_blend == 'ADD')).preset = 'ADD'
        row_b1.operator("wpm.select_brush_preset", text="Sub (減算)", depress=(cur_btype == 'DRAW' and cur_blend == 'SUB')).preset = 'SUB'

        row_b2 = r_col.row(align=True)
        row_b2.operator("wpm.select_brush_preset", text="Blur (ぼかし)", icon='MOD_SMOOTH', depress=(cur_btype == 'BLUR')).preset = 'BLUR'
        row_b2.operator("wpm.select_brush_preset", text="Average", depress=(cur_btype == 'AVERAGE')).preset = 'AVERAGE'
        row_b2.operator("wpm.select_brush_preset", text="Smear", depress=(cur_btype == 'SMEAR')).preset = 'SMEAR'

        row_b3 = r_col.row(align=True)
        row_b3.operator("wpm.select_brush_preset", text="Multiply", depress=(cur_btype == 'DRAW' and cur_blend == 'MUL')).preset = 'MUL'
        row_b3.operator("wpm.select_brush_preset", text="Lighten", depress=(cur_btype == 'DRAW' and cur_blend == 'LIGHTEN')).preset = 'LIGHTEN'
        row_b3.operator("wpm.select_brush_preset", text="Darken", depress=(cur_btype == 'DRAW' and cur_blend == 'DARKEN')).preset = 'DARKEN'

        # [2] 南 (Bottom): 選択頂点からマウスホイールでウェイトぼかし (Editモード・Weight Paint両対応)
        box_bottom = pie.column()
        bot_col = box_bottom.box().column(align=True)
        bot_col.label(text="選択頂点からホイールぼかし (Edit/WP対応)", icon='MOD_SMOOTH')
        bot_col.scale_y = 1.35
        bot_col.operator(
            "wpm.interactive_scroll_blur",
            text="マウスホイールでぼかす (Scroll Blur)",
            icon='MOD_SMOOTH',
        )

        # [8] 北 (Top): ブラシのウェイト値＆強さ(Strength)クイック変更
        box_top = pie.column()
        t_col = box_top.box().column(align=True)
        t_col.label(text=f"ブラシ値: Weight={cur_w:.2f} / Strength={cur_s:.2f}", icon='WPAINT_HLT')
        row_w = t_col.row(align=True)
        row_w.label(text="Weight:")
        for w_val in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
            op = row_w.operator("wpm.set_brush_parameter", text=f"{w_val:g}", depress=(abs(cur_w - w_val) < 0.01))
            op.param_type = 'WEIGHT'
            op.value = w_val

        row_s = t_col.row(align=True)
        row_s.label(text="Strength:")
        for s_val in (0.1, 0.25, 0.5, 0.75, 1.0):
            op = row_s.operator("wpm.set_brush_parameter", text=f"{s_val:g}", depress=(abs(cur_s - s_val) < 0.01))
            op.param_type = 'STRENGTH'
            op.value = s_val

        # [7] 北西 (Top-Left): 正規化・クリーンアップ・4ボーン制限・ウェイト転送
        box_tl = pie.column()
        tl_col = box_tl.box().column(align=True)
        tl_col.label(text="最適化・クリーンアップ・転送", icon='CHECKMARK')
        tl_col.operator("wpm.quick_clean_and_normalize", text="一括最適化 (Clean+4Bone+Norm)", icon='CHECKMARK').action = 'FULL_OPTIMIZE'
        row_norm = tl_col.row(align=True)
        row_norm.operator("wpm.quick_clean_and_normalize", text="全て正規化").action = 'NORMALIZE_ALL'
        row_norm.operator("wpm.quick_clean_and_normalize", text="微小削除(Clean)").action = 'CLEAN'
        row_norm.operator("wpm.quick_clean_and_normalize", text="4ボーン制限").action = 'LIMIT'
        tl_col.operator("wpm.quick_transfer_weights", text="ウェイト転送 (選択 → アクティブ)", icon='MOD_DATA_TRANSFER')

        # [9] 北東 (Top-Right): ポーズモードへ飛ぶ ＆ ポーズをすべてクリア！
        box_tr = pie.column()
        tr_col = box_tr.box().column(align=True)
        tr_col.label(text="モード移動 & ポーズ操作", icon='ARMATURE_DATA')
        row_jmp = tr_col.row(align=True)
        is_pose = (active_obj and active_obj.mode == 'POSE')
        is_wp = (active_obj and active_obj.mode == 'WEIGHT_PAINT')
        is_ed = (active_obj and active_obj.mode == 'EDIT')
        row_jmp.operator("wpm.jump_mode", text="Poseへ飛ぶ", icon='POSE_HLT', depress=is_pose).target_mode = 'POSE'
        row_jmp.operator("wpm.jump_mode", text="Weight Paint", icon='WPAINT_HLT', depress=is_wp).target_mode = 'WEIGHT_PAINT'
        row_jmp.operator("wpm.jump_mode", text="Edit (頂点)", icon='EDITMODE_HLT', depress=is_ed).target_mode = 'EDIT'
        tr_col.scale_y = 1.25
        tr_col.operator("wpm.clear_all_pose", text="ポーズをすべてクリア (全ボーン初期化)", icon='LOOP_BACK')

        # [1] 南西 (Bottom-Left): 等高線・ペイントワイヤー・アーマチュア表示 & マスク
        box_bl = pie.column()
        bl_col = box_bl.box().column(align=True)
        bl_col.label(text="表示切替 (等高線 / ワイヤー / ボーン) & マスク", icon='SHADING_WIRE')
        row_disp1 = bl_col.row(align=True)
        row_disp1.operator(
            "wpm.toggle_viewport_display",
            text="等高線: ON" if show_contours else "等高線: OFF",
            icon='SMOOTHCURVE',
            depress=show_contours,
        ).display_type = 'CONTOURS'
        row_disp1.operator(
            "wpm.toggle_viewport_display",
            text="ワイヤー: ON" if show_wire else "ワイヤー: OFF",
            icon='SHADING_WIRE',
            depress=show_wire,
        ).display_type = 'WIREFRAME'

        row_disp2 = bl_col.row(align=True)
        row_disp2.operator(
            "wpm.toggle_viewport_display",
            text="Editウェイト色",
            icon='COLOR',
            depress=show_edit_w,
        ).display_type = 'EDIT_WEIGHT'
        row_disp2.operator(
            "wpm.toggle_viewport_display",
            text="最前面",
            icon='BONE_DATA',
            depress=bone_front,
        ).display_type = 'BONE_IN_FRONT'
        row_disp2.operator(
            "wpm.toggle_viewport_display",
            text="名前",
            depress=bone_names,
        ).display_type = 'BONE_NAMES'
        row_disp2.operator(
            "wpm.toggle_viewport_display",
            text="座標軸",
            depress=bone_axes,
        ).display_type = 'BONE_AXES'

        row_m = bl_col.row(align=True)
        v_mask = mesh_obj.data.use_paint_mask_vertex if mesh_obj else False
        f_mask = mesh_obj.data.use_paint_mask if mesh_obj else False
        row_m.operator("wpm.toggle_paint_option", text="頂点マスク", icon='VERTEXSEL', depress=v_mask).option = 'VERT_MASK'
        row_m.operator("wpm.toggle_paint_option", text="面マスク", icon='FACESEL', depress=f_mask).option = 'FACE_MASK'
        row_m.operator("wpm.toggle_paint_option", text="自動正規化", depress=ts.use_auto_normalize).option = 'AUTO_NORMALIZE'

        # [3] 南東 (Bottom-Right): ボーン選択メニュー・反対ボーン切替
        box_br = pie.column()
        br_col = box_br.box().column(align=True)
        br_col.label(text=f"ボーン選択 [現在: {act_vg_name}]", icon='BONE_DATA')
        br_col.menu("VIEW3D_MT_wpm_bone_select_menu", text="ボーン一覧から切替 (選択頂点/全ボーン)", icon='BONE_DATA')
        row_sp = br_col.row(align=True)
        row_sp.operator("wpm.switch_opposite_bone", text="反対側 (.L↔.R)", icon='ARROW_LEFTRIGHT')
        row_sp.operator("paint.weight_sample_group", text="スポイト選択", icon='EYEDROPPER')


class VIEW3D_MT_wpm_quick_brush_pie(Menu):
    """シンプル8方向フリック用・高速ブラシ＆ホイールぼかし＆ポーズ操作パイメニュー (Shift+W)"""
    bl_idname = "VIEW3D_MT_wpm_quick_brush_pie"
    bl_label = "Quick Brush & Scroll Blur Pie"

    def draw(self, context):
        pie = self.layout.menu_pie()
        active_obj = context.active_object
        # 4: West -> Subtract (減算)
        pie.operator("wpm.select_brush_preset", text="Subtract (減算)", icon='REMOVE').preset = 'SUB'
        # 6: East -> Add (加算)
        pie.operator("wpm.select_brush_preset", text="Add (加算)", icon='ADD').preset = 'ADD'
        # 2: South -> 選択頂点からマウスホイールでウェイトぼかし！
        pie.operator("wpm.interactive_scroll_blur", text="ホイールでウェイトぼかし (Scroll Blur)", icon='MOD_SMOOTH')
        # 8: North -> Mix (標準ペイント)
        pie.operator("wpm.select_brush_preset", text="Mix (標準)", icon='BRUSH_DATA').preset = 'MIX'
        # 7: NW -> ポーズモードへ飛ぶ（ポーズモード中はWeight Paintへ戻る）
        if active_obj and active_obj.mode == 'POSE':
            pie.operator("wpm.jump_mode", text="Weight Paintへ戻る", icon='WPAINT_HLT').target_mode = 'WEIGHT_PAINT'
        else:
            pie.operator("wpm.jump_mode", text="ポーズモードへ飛ぶ", icon='POSE_HLT').target_mode = 'POSE'
        # 9: NE -> ポーズをすべてクリア
        pie.operator("wpm.clear_all_pose", text="ポーズをすべてクリア", icon='LOOP_BACK')
        # 1: SW -> Blur ブラシ
        pie.operator("wpm.select_brush_preset", text="Blur ブラシ", icon='MOD_SMOOTH').preset = 'BLUR'
        # 3: SE -> ボーン切替メニュー
        pie.menu("VIEW3D_MT_wpm_bone_select_menu", text="ボーン切替メニュー", icon='BONE_DATA')


# ============================================================================
# 6. サイドバー (Nパネル) & アドオン設定 (ショートカットカスタマイズ)
# ============================================================================

class VIEW3D_PT_wpm_sidebar(Panel):
    """3Dビューポートのサイドバー(Nパネル)用パネル（Mesh・Armature両対応）"""
    bl_label = "Weight Paint Master"
    bl_idname = "VIEW3D_PT_wpm_sidebar"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Weight Master"

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type in {'MESH', 'ARMATURE'}

    def draw(self, context):
        layout = self.layout
        active_obj = context.active_object
        mesh_obj = get_target_mesh_object(context)
        arm_obj = get_related_armature(active_obj)
        settings = getattr(context.scene, "wpm_settings", None)
        ts, wp, ups = get_weight_paint_settings(context)

        _, show_contours, show_wire, show_edit_w, bone_front, bone_names, bone_axes = get_viewport_overlay_states(context, arm_obj)

        # --------------------------------------------------------------------
        # 1. モード高速移動 & ポーズ全クリア欄
        # --------------------------------------------------------------------
        box_pose = layout.box()
        box_pose.label(text="モード移動 & ポーズ操作", icon='ARMATURE_DATA')
        col_pose = box_pose.column(align=True)

        row_modes = col_pose.row(align=True)
        row_modes.scale_y = 1.25
        is_pose = (active_obj and active_obj.mode == 'POSE')
        is_wp = (active_obj and active_obj.mode == 'WEIGHT_PAINT')
        is_ed = (active_obj and active_obj.mode == 'EDIT')
        row_modes.operator("wpm.jump_mode", text="Poseへ飛ぶ", icon='POSE_HLT', depress=is_pose).target_mode = 'POSE'
        row_modes.operator("wpm.jump_mode", text="Weight Paint", icon='WPAINT_HLT', depress=is_wp).target_mode = 'WEIGHT_PAINT'
        row_modes.operator("wpm.jump_mode", text="Edit (頂点)", icon='EDITMODE_HLT', depress=is_ed).target_mode = 'EDIT'

        col_pose.separator()
        row_clr = col_pose.row(align=True)
        row_clr.scale_y = 1.35
        row_clr.operator("wpm.clear_all_pose", text="ポーズをすべてクリア (全ボーン初期化)", icon='LOOP_BACK')

        # --------------------------------------------------------------------
        # 2. 表示切替欄（ウェイトペイントオーバーレイ & アーマチュア表示）
        # --------------------------------------------------------------------
        box_disp = layout.box()
        box_disp.label(text="オーバーレイ & アーマチュア表示", icon='SHADING_WIRE')
        col_disp = box_disp.column(align=True)

        row_d1 = col_disp.row(align=True)
        row_d1.scale_y = 1.15
        row_d1.operator(
            "wpm.toggle_viewport_display",
            text="等高線: ON" if show_contours else "等高線: OFF",
            icon='SMOOTHCURVE',
            depress=show_contours,
        ).display_type = 'CONTOURS'
        row_d1.operator(
            "wpm.toggle_viewport_display",
            text="ワイヤー: ON" if show_wire else "ワイヤー: OFF",
            icon='SHADING_WIRE',
            depress=show_wire,
        ).display_type = 'WIREFRAME'

        row_d2 = col_disp.row(align=True)
        row_d2.operator(
            "wpm.toggle_viewport_display",
            text="Editウェイト色: ON" if show_edit_w else "Editウェイト色: OFF",
            icon='COLOR',
            depress=show_edit_w,
        ).display_type = 'EDIT_WEIGHT'

        if arm_obj and arm_obj.data:
            col_disp.separator()
            col_disp.label(text=f"アーマチュア表示 ({arm_obj.name}):", icon='ARMATURE_DATA')
            col_disp.prop(arm_obj.data, "display_type", text="表示方法")

            row_arm = col_disp.row(align=True)
            row_arm.scale_y = 1.1
            row_arm.operator(
                "wpm.toggle_viewport_display",
                text="名前: ON" if bone_names else "名前: OFF",
                depress=bone_names,
            ).display_type = 'BONE_NAMES'
            row_arm.operator(
                "wpm.toggle_viewport_display",
                text="座標軸: ON" if bone_axes else "座標軸: OFF",
                depress=bone_axes,
            ).display_type = 'BONE_AXES'
            row_arm.operator(
                "wpm.toggle_viewport_display",
                text="最前面: ON" if bone_front else "最前面: OFF",
                depress=bone_front,
            ).display_type = 'BONE_IN_FRONT'

        if not mesh_obj:
            layout.label(text="※対象メッシュが見つかりません", icon='INFO')
            return

        # --------------------------------------------------------------------
        # 3. ボーン / 頂点グループ選択欄 (種類別フィルタ付き)
        # --------------------------------------------------------------------
        box_vg = layout.box()
        act_vg_name = mesh_obj.vertex_groups.active.name if mesh_obj.vertex_groups.active else "未選択"
        box_vg.label(text=f"ボーン選択 [{act_vg_name}]", icon='BONE_DATA')
        col_vg = box_vg.column(align=True)

        if settings:
            row_f1 = col_vg.row(align=True)
            row_f1.prop_enum(settings, "bone_filter_mode", 'ALL')
            row_f1.prop_enum(settings, "bone_filter_mode", 'SELECTED_VERTS')
            row_f1.prop_enum(settings, "bone_filter_mode", 'DEFORM')

            row_f2 = col_vg.row(align=True)
            row_f2.prop_enum(settings, "bone_filter_mode", 'CENTER')
            row_f2.prop_enum(settings, "bone_filter_mode", 'LEFT')
            row_f2.prop_enum(settings, "bone_filter_mode", 'RIGHT')
            col_vg.separator()

        row_list = col_vg.row()
        row_list.template_list(
            "WPM_UL_vertex_groups",
            "",
            mesh_obj,
            "vertex_groups",
            mesh_obj,
            "wpm_active_vg_index",
            rows=6,
        )
        col_btn = row_list.column(align=True)
        col_btn.operator("object.vertex_group_add", icon='ADD', text="")
        col_btn.operator("object.vertex_group_remove", icon='REMOVE', text="").all = False
        col_btn.separator()
        col_btn.operator("wpm.switch_opposite_bone", icon='ARROW_LEFTRIGHT', text="")
        col_btn.operator("paint.weight_sample_group", icon='EYEDROPPER', text="")

        # --------------------------------------------------------------------
        # 4. 選択頂点からマウスホイールでウェイトぼかし
        # --------------------------------------------------------------------
        box_sb = layout.box()
        box_sb.label(text="選択頂点ホイールぼかし (Edit/WP対応)", icon='MOD_SMOOTH')
        col_sb = box_sb.column(align=True)
        col_sb.scale_y = 1.35
        col_sb.operator("wpm.interactive_scroll_blur", text="マウスホイールでぼかす (開始)", icon='MOD_SMOOTH')
        col_sb.scale_y = 1.0
        if settings:
            row_sb_prop = col_sb.row(align=True)
            row_sb_prop.prop(settings, "blur_default_expand_rings", text="初期リング拡張")
            row_sb_prop.prop(settings, "blur_step_factor", text="強度")

        # --------------------------------------------------------------------
        # 5. 選択頂点ウェイト数値設定 & 左右対称(Xミラー)
        # --------------------------------------------------------------------
        box_v = layout.box()
        box_v.label(text="選択頂点ウェイト設定 & 左右対称", icon='VERTEXSEL')
        col_v = box_v.column(align=True)
        row_vp = col_v.row(align=True)
        for v in (0.0, 0.1, 0.25, 0.5, 0.75, 1.0):
            op = row_vp.operator("wpm.set_vertex_weight", text=f"{v:g}")
            op.mode = 'SET'
            op.value = v
        row_vd = col_v.row(align=True)
        for d in (-0.1, -0.05, 0.05, 0.1):
            op = row_vd.operator("wpm.set_vertex_weight", text=f"{d:+g}")
            op.mode = 'ADD'
            op.value = d
        if settings:
            col_v.separator()
            row_vopt = col_v.row(align=True)
            row_vopt.prop(settings, "use_mirror_x_on_edit", text="X対称自動反映 (.L↔.R)", toggle=True, icon='MOD_MIRROR')
            row_vopt.prop(settings, "auto_normalize_on_edit", text="自動正規化", toggle=True)

        col_v.separator()
        row_msel = col_v.row(align=True)
        row_msel.scale_y = 1.25
        row_msel.operator(
            "wpm.mirror_vertex_weights",
            text="選択頂点を左右対称に適用 (Mirror)",
            icon='MOD_MIRROR',
        ).direction = 'SELECTED'

        row_mall = col_v.row(align=True)
        row_mall.operator(
            "wpm.mirror_vertex_weights",
            text="全頂点: 左(.L) → 右(.R)",
            icon='ARROW_LEFTRIGHT',
        ).direction = 'L_TO_R'
        row_mall.operator(
            "wpm.mirror_vertex_weights",
            text="全頂点: 右(.R) → 左(.L)",
            icon='ARROW_LEFTRIGHT',
        ).direction = 'R_TO_L'

        # --------------------------------------------------------------------
        # 6. ブラシ切替 & パラメータ
        # --------------------------------------------------------------------
        box_b = layout.box()
        box_b.label(text="ブラシ切替 & パラメータ", icon='WPAINT_HLT')
        col_b = box_b.column(align=True)
        row1 = col_b.row(align=True)
        row1.operator("wpm.select_brush_preset", text="Mix").preset = 'MIX'
        row1.operator("wpm.select_brush_preset", text="Add").preset = 'ADD'
        row1.operator("wpm.select_brush_preset", text="Sub").preset = 'SUB'
        row2 = col_b.row(align=True)
        row2.operator("wpm.select_brush_preset", text="Blur").preset = 'BLUR'
        row2.operator("wpm.select_brush_preset", text="Average").preset = 'AVERAGE'
        row2.operator("wpm.select_brush_preset", text="Smear").preset = 'SMEAR'

        if ups:
            col_b.separator()
            col_b.prop(ups, "weight", text="ブラシ Weight", slider=True)
            col_b.prop(ups, "strength", text="ブラシ Strength", slider=True)

        # --------------------------------------------------------------------
        # 7. 最適化・クリーンアップ・ウェイト転送 (ミラー削除済み)
        # --------------------------------------------------------------------
        box_u = layout.box()
        box_u.label(text="最適化・ウェイト転送", icon='CHECKMARK')
        col_u = box_u.column(align=True)
        if settings:
            row_u_prop = col_u.row(align=True)
            row_u_prop.prop(settings, "clean_threshold", text="閾値")
            row_u_prop.prop(settings, "max_bone_influences", text="最大ボーン")
        col_u.operator("wpm.quick_clean_and_normalize", text="一括最適化 (Clean+Limit+Norm)", icon='CHECKMARK').action = 'FULL_OPTIMIZE'
        col_u.separator()
        col_u.operator("wpm.quick_transfer_weights", text="ウェイト転送 (選択 → アクティブ)", icon='MOD_DATA_TRANSFER')


class WPM_AddonPreferences(AddonPreferences):
    bl_idname = __name__

    def draw(self, context):
        layout = self.layout
        layout.label(text="ショートカットキー設定:", icon='KEYINGSET')
        wm = context.window_manager
        kc = wm.keyconfigs.user
        if kc:
            for km, kmi in addon_keymaps:
                box = layout.box()
                row = box.row()
                row.label(text=km.name)
                row.prop(kmi, "type", text="", full_event=True)
                row.prop(kmi, "active", text="有効")


# ============================================================================
# 登録・キーマップ管理 (Weight Paint / Mesh / Pose の3モードでショートカット有効)
# ============================================================================

classes = (
    WPM_Settings,
    WPM_OT_jump_mode,
    WPM_OT_clear_all_pose,
    WPM_OT_toggle_viewport_display,
    WPM_UL_vertex_groups,
    WPM_OT_select_vertex_group_by_name,
    WPM_OT_switch_opposite_bone,
    VIEW3D_MT_wpm_bone_select_menu,
    WPM_OT_set_vertex_weight,
    WPM_OT_mirror_vertex_weights,
    WPM_OT_quick_clean_and_normalize,
    WPM_OT_interactive_scroll_blur,
    WPM_OT_select_brush_preset,
    WPM_OT_set_brush_parameter,
    WPM_OT_toggle_paint_option,
    WPM_OT_quick_transfer_weights,
    VIEW3D_MT_wpm_main_pie,
    VIEW3D_MT_wpm_quick_brush_pie,
    VIEW3D_PT_wpm_sidebar,
    WPM_AddonPreferences,
)

addon_keymaps = []
_msgbus_owner = object()


def _on_vertex_group_active_changed(*_args):
    """Blender標準の頂点グループリスト等でアクティブグループが変わった際にもボーン選択を同期する"""
    context = bpy.context
    if not context:
        return
    obj = get_target_mesh_object(context)
    if obj and obj.type == 'MESH' and obj.vertex_groups.active:
        sync_pose_bone_with_active_vg(context, obj)


@bpy.app.handlers.persistent
def _on_load_post(*_args):
    _register_msgbus()


def _register_msgbus():
    bpy.msgbus.clear_by_owner(_msgbus_owner)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.VertexGroups, "active_index"),
        owner=_msgbus_owner,
        args=(),
        notify=_on_vertex_group_active_changed,
    )


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.wpm_settings = PointerProperty(type=WPM_Settings)
    bpy.types.Object.wpm_active_vg_index = IntProperty(
        name="アクティブボーン/頂点グループ",
        description="選択すると対象の頂点グループをアクティブ化し、ビューポート上のボーンも同時に選択・ハイライトします",
        get=_get_wpm_active_vg_index,
        set=_set_wpm_active_vg_index,
        update=_update_wpm_active_vg_index,
    )

    _register_msgbus()
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)

    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        for km_name in ("Weight Paint", "Mesh", "Pose"):
            km = kc.keymaps.new(name=km_name, space_type='EMPTY')
            # Alt + W : 総合ウェイトペイントパイメニュー
            kmi1 = km.keymap_items.new("wm.call_menu_pie", 'W', 'PRESS', alt=True)
            kmi1.properties.name = "VIEW3D_MT_wpm_main_pie"
            addon_keymaps.append((km, kmi1))

            # Shift + W : 高速ブラシ＆ホイールぼかし＆ポーズ操作パイメニュー
            kmi2 = km.keymap_items.new("wm.call_menu_pie", 'W', 'PRESS', shift=True)
            kmi2.properties.name = "VIEW3D_MT_wpm_quick_brush_pie"
            addon_keymaps.append((km, kmi2))


def unregister():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    bpy.msgbus.clear_by_owner(_msgbus_owner)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)

    if hasattr(bpy.types.Object, "wpm_active_vg_index"):
        del bpy.types.Object.wpm_active_vg_index
    if hasattr(bpy.types.Scene, "wpm_settings"):
        del bpy.types.Scene.wpm_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
