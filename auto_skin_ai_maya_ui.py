# -*- coding: utf-8 -*-
"""
Auto Skin (AI) - Maya Python Example (No tabLayout version)

Simple UI:
- Model Preset (drop-down)
- Global Skin - Mesh: list + Add/Remove/Clear
- Global Skin - Joints: list + Add/Remove/Clear
- Start Skinning button

AI part is a dummy example (uniform weights).
Replace `predict_weights_with_model` with your own model call.
"""

import maya.cmds as cmds

# ====== Config: use PyTorch model or not ======
USE_TORCH = True
MODEL_PATH = r"C:\ai_models\auto_skin_net.pt"  # change to your model path

model = None

if USE_TORCH:
    try:
        import torch

        def load_model():
            """Load TorchScript model (change if needed)."""
            global model
            if model is None:
                print("[AutoSkinAI] Loading model from:", MODEL_PATH)
                model = torch.jit.load(MODEL_PATH, map_location="cpu")
                model.eval()
            return model

    except ImportError:
        print("[AutoSkinAI] WARNING: PyTorch not available, using dummy weights.")
        USE_TORCH = False

# ====== Global UI handles ======
g_mesh_list = None       # textScrollList for meshes
g_joint_list = None      # textScrollList for joints
g_model_menu = None      # optionMenu for model preset


# ====== Core helper functions ======

def find_skin_cluster(mesh):
    """Find existing skinCluster on a mesh, if any."""
    history = cmds.listHistory(mesh) or []
    skins = [h for h in history if cmds.nodeType(h) == "skinCluster"]
    return skins[0] if skins else None


def ensure_skin_cluster(mesh, joints):
    """Make sure the mesh has a skinCluster, create one if not."""
    skin = find_skin_cluster(mesh)
    if skin:
        print("[AutoSkinAI] Found existing skinCluster:", skin)
        return skin

    print("[AutoSkinAI] Creating new skinCluster for:", mesh)
    skin = cmds.skinCluster(joints, mesh, tsb=True, maximumInfluences=4)[0]
    return skin


def get_mesh_vertex_positions(mesh):
    """Get all vertex world positions using cmds.xform (simple, not fastest)."""
    num_verts = cmds.polyEvaluate(mesh, vertex=True)
    positions = []
    for i in range(num_verts):
        vtx = "%s.vtx[%d]" % (mesh, i)
        pos = cmds.xform(vtx, q=True, ws=True, t=True)  # [x, y, z]
        positions.append(pos)
    return positions


def predict_weights_with_model(positions, num_joints):
    """
    Predict skin weights using an AI model.

    positions: [[x, y, z], ...]
    return: weights[v][j]

    TODO: replace this with your own model logic.
    """
    num_verts = len(positions)

    if USE_TORCH and model is not None:
        import torch
        verts = torch.tensor(positions, dtype=torch.float32)  # [N, 3]
        with torch.no_grad():
            # Assume model(verts) -> [N, num_joints]
            pred = model(verts)
            pred = torch.softmax(pred, dim=-1)
            weights = pred.cpu().numpy().tolist()
        return weights

    # Dummy: uniform weights
    print("[AutoSkinAI] Using dummy uniform weights (no real AI model).")
    weights = []
    for _ in range(num_verts):
        weights.append([1.0 / num_joints] * num_joints)
    return weights


def apply_weights_to_skin(mesh, joints, skin, weights):
    """
    Apply weights to skinCluster.

    joints: [joint1, joint2, ...]
    weights: weights[v][j]
    """
    num_verts = len(weights)
    if num_verts == 0:
        cmds.error("No vertices to skin.")
        return

    # Use long names for joints
    joints_long = [cmds.ls(j, long=True)[0] for j in joints]
    joints = joints_long
    num_joints = len(joints)

    if num_joints == 0:
        cmds.error("No joints to use as influences.")
        return

    # Existing influences
    current_infs = cmds.skinCluster(skin, q=True, inf=True) or []
    current_infs_long = [cmds.ls(i, long=True)[0] for i in current_infs]

    # Ensure all joints are influences
    for j in joints:
        if j in current_infs_long:
            print("[AutoSkinAI] Influence already exists:", j)
            continue
        try:
            cmds.skinCluster(skin, e=True, ai=j, lw=True, wt=0)
            current_infs_long.append(j)
        except RuntimeError as e:
            msg = str(e)
            if "already attached" in msg:
                print("[AutoSkinAI] Influence already attached (ignored):", j)
                continue
            else:
                raise

    # Apply vertex weights
    for vid in range(num_verts):
        vtx = "%s.vtx[%d]" % (mesh, vid)
        w_row = list(weights[vid])

        # Fix length mismatch
        if len(w_row) != num_joints:
            if len(w_row) > num_joints:
                w_row = w_row[:num_joints]
            else:
                w_row = w_row + [0.0] * (num_joints - len(w_row))

        # Normalize
        s = sum(w_row)
        if s <= 1e-8:
            w_row = [1.0 / num_joints] * num_joints
        else:
            w_row = [w / s for w in w_row]

        tv = [(joints[j_idx], w_row[j_idx]) for j_idx in range(num_joints)]
        cmds.skinPercent(skin, vtx, transformValue=tv)

    print("[AutoSkinAI] Applied AI weights to", num_verts, "vertices.")


def auto_skin(mesh, joints):
    """Top-level auto-skinning pipeline."""
    if not mesh or not joints:
        cmds.error("Mesh or joints missing.")
        return

    if USE_TORCH:
        load_model()

    skin = ensure_skin_cluster(mesh, joints)
    positions = get_mesh_vertex_positions(mesh)
    weights = predict_weights_with_model(positions, len(joints))
    apply_weights_to_skin(mesh, joints, skin, weights)

    cmds.inViewMessage(
        amg="<hl>Auto Skin (AI)</hl>: Finished.",
        pos="topCenter",
        fade=True
    )


# ====== UI list helpers ======

def _add_selected_mesh_to_list(*args):
    """Add selected meshes to the mesh list."""
    global g_mesh_list
    if not g_mesh_list:
        return

    sel = cmds.ls(sl=True, long=True) or []
    meshes = []

    for node in sel:
        if cmds.nodeType(node) in ("transform", "mesh"):
            shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
            for s in shapes:
                if cmds.nodeType(s) == "mesh":
                    meshes.append(node)
                    break

    if not meshes:
        cmds.warning("Please select at least one polygon mesh in the viewport.")
        return

    existing = cmds.textScrollList(g_mesh_list, q=True, ai=True) or []
    existing = list(existing)

    for m in meshes:
        if m not in existing:
            cmds.textScrollList(g_mesh_list, e=True, append=m)


def _add_selected_joints_to_list(*args):
    """Add selected joints to the joint list."""
    global g_joint_list
    if not g_joint_list:
        return

    joints = cmds.ls(sl=True, type="joint", long=True) or []
    if not joints:
        cmds.warning("Please select at least one joint in the viewport.")
        return

    existing = cmds.textScrollList(g_joint_list, q=True, ai=True) or []
    existing = list(existing)

    for j in joints:
        if j not in existing:
            cmds.textScrollList(g_joint_list, e=True, append=j)


def _remove_selected_from_list(list_control, *args):
    """Remove selected items from a textScrollList."""
    if not list_control:
        return
    sel_items = cmds.textScrollList(list_control, q=True, si=True) or []
    for item in sel_items:
        cmds.textScrollList(list_control, e=True, ri=item)


def _clear_list(list_control, *args):
    """Clear all items from a textScrollList."""
    if not list_control:
        return
    cmds.textScrollList(list_control, e=True, ra=True)


def _on_start_global_skin(*args):
    """Callback for 'Start Skinning' button."""
    global g_mesh_list, g_joint_list, g_model_menu

    meshes = cmds.textScrollList(g_mesh_list, q=True, ai=True) or []
    joints = cmds.textScrollList(g_joint_list, q=True, ai=True) or []

    if not meshes:
        cmds.error("Please add at least one mesh.")
        return
    if not joints:
        cmds.error("Please add at least one joint.")
        return

    mesh = meshes[0]  # you can loop for multiple meshes if needed
    model_name = cmds.optionMenu(g_model_menu, q=True, v=True)
    print("[AutoSkinAI] Model preset:", model_name)

    auto_skin(mesh, joints)


# ====== Build UI (no tabs, only frames) ======

def show_auto_skin_window():
    """Create and show the main window."""
    global g_mesh_list, g_joint_list, g_model_menu

    win_name = "AutoSkinAIWindow"
    if cmds.window(win_name, exists=True):
        cmds.deleteUI(win_name)

    win = cmds.window(win_name, title="Auto Skin - Maya", sizeable=False)
    main_col = cmds.columnLayout(adj=True)

    # --- Model preset frame ---
    cmds.frameLayout(label="Model Preset", collapsable=False,
                     marginWidth=8, marginHeight=8, parent=main_col)
    cmds.columnLayout(adj=True, rowSpacing=4)
    cmds.rowLayout(numberOfColumns=2, adjustableColumn=2)
    cmds.text(label="Preset:", align="left", width=60)
    g_model_menu = cmds.optionMenu(label="")
    cmds.menuItem(label="general-v1")
    cmds.menuItem(label="general-v4-beta")
    cmds.menuItem(label="custom-01")
    cmds.setParent("..")  # rowLayout
    cmds.checkBox(label="Single body", v=True)
    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    # --- Mesh list frame ---
    cmds.frameLayout(label="Global Skin - Mesh", collapsable=False,
                     marginWidth=8, marginHeight=8, parent=main_col)
    cmds.columnLayout(adj=True, rowSpacing=4)
    cmds.text(label="Select meshes in viewport, then click 'Add'.")
    cmds.rowLayout(numberOfColumns=4, adjustableColumn=1,
                   columnWidth4=(220, 60, 60, 60))
    g_mesh_list = cmds.textScrollList(numberOfRows=5, allowMultiSelection=True)
    cmds.button(label="Add", c=_add_selected_mesh_to_list)
    cmds.button(label="Remove", c=lambda *a: _remove_selected_from_list(g_mesh_list))
    cmds.button(label="Clear", c=lambda *a: _clear_list(g_mesh_list))
    cmds.setParent("..")  # rowLayout
    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    # --- Joint list frame ---
    cmds.frameLayout(label="Global Skin - Joints", collapsable=False,
                     marginWidth=8, marginHeight=8, parent=main_col)
    cmds.columnLayout(adj=True, rowSpacing=4)
    cmds.text(label="Select joints in viewport, then click 'Add'.")
    cmds.rowLayout(numberOfColumns=4, adjustableColumn=1,
                   columnWidth4=(220, 60, 60, 60))
    g_joint_list = cmds.textScrollList(numberOfRows=5, allowMultiSelection=True)
    cmds.button(label="Add", c=_add_selected_joints_to_list)
    cmds.button(label="Remove", c=lambda *a: _remove_selected_from_list(g_joint_list))
    cmds.button(label="Clear", c=lambda *a: _clear_list(g_joint_list))
    cmds.setParent("..")  # rowLayout
    cmds.setParent("..")  # columnLayout
    cmds.setParent("..")  # frameLayout

    # --- Bottom buttons ---
    cmds.rowLayout(numberOfColumns=2, columnWidth2=(140, 140),
                   columnAlign=(1, "center"), parent=main_col)
    cmds.button(label="Start Skinning", h=32, c=_on_start_global_skin)
    cmds.button(label="Fix Weights", h=32, enable=False)  # reserved
    cmds.setParent("..")  # rowLayout

    cmds.separator(h=6, style="none", parent=main_col)
    cmds.text(label="Ready. Add mesh & joints, then click 'Start Skinning'.",
              align="left", parent=main_col)

    cmds.showWindow(win)


# Auto-show when run as a script directly
if __name__ == "__main__":
    show_auto_skin_window()
