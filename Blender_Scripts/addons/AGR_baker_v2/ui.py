"""
UI panels for AGR Baker v2
"""

import bpy
import re
from bpy.types import Panel, UIList


class AGR_UL_TextureSetsList(UIList):
    """UI List for texture sets"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        tex_set = item

        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            # Selection checkbox
            row.prop(tex_set, "is_selected", text="")

            # 3-column split: name | resolution (center) | indicators
            split = row.split(factor=0.5)

            # Name with icon
            if tex_set.is_atlas:
                split.label(text=tex_set.name, icon='IMAGE_PLANE')
            elif tex_set.is_assigned:
                split.label(text=tex_set.name, icon='CHECKMARK')
            else:
                split.label(text=tex_set.name, icon='TEXTURE')

            # Middle: resolution
            mid_split = split.split(factor=0.5)
            mid_split.alignment = 'CENTER'
            mid_split.label(text=f"{tex_set.resolution}px")

            # Right: indicators
            right = mid_split.row(align=True)
            right.alignment = 'RIGHT'
            if tex_set.is_atlas:
                right.label(text=tex_set.atlas_type, icon='UV')
            if tex_set.has_alpha:
                right.label(text="", icon='IMAGE_ALPHA')
            if not tex_set.is_atlas and not tex_set.has_alpha:
                right.label(text="")

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            if item.is_atlas:
                layout.label(text="", icon='IMAGE_PLANE')
            else:
                layout.label(text="", icon='TEXTURE')


class AGR_PT_MainPanel(Panel):
    """Main AGR Baker panel"""
    bl_label = "AGR Baker v2"
    bl_idname = "AGR_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        # Header
        box = layout.box()
        box.label(text="Texture Baking", icon='RENDER_STILL')

        # Baking settings
        col = box.column(align=True)
        col.prop(settings, "resolution")
        col.prop(settings, "bake_with_alpha")
        col.prop(settings, "bake_normal_enabled")

        row = col.row(align=True)
        row.prop(settings, "max_ray_distance")
        row.prop(settings, "extrusion")

        # Render settings
        col.separator()
        col.label(text="Render Settings:", icon='RENDER_STILL')
        row = col.row(align=True)
        row.prop(settings, "bake_samples")
        row.prop(settings, "bake_device", text="")
        col.prop(settings, "bake_use_denoising")

        # Bake buttons
        box.separator()

        # Regular bake (selected to active)
        row = box.row()
        row.scale_y = 1.5
        row.operator("agr.bake_textures", text="Bake from High-Poly", icon='RENDER_STILL')
        box.separator()

        # Simple bake (from material)
        row = box.row()
        row.scale_y = 1.3
        row.operator("agr.simple_bake", text="Simple Bake Active Material", icon='MATERIAL')

        # Simple bake all materials
        row = box.row()
        row.scale_y = 1.3
        row.operator("agr.simple_bake_all", text="Simple Bake All Materials", icon='MATERIAL')

        # Convert materials to sets
        box.separator()
        row = box.row()
        row.scale_y = 1.2
        row.operator("agr.convert_active_material_to_set", text="Convert Active Material", icon='MATERIAL')

        row = box.row()
        row.scale_y = 1.2
        row.operator("agr.convert_materials_to_sets", text="Convert Materials to Sets", icon='MATERIAL')

        # Pillow installation check
        try:
            from PIL import Image
            pillow_available = True
        except ImportError:
            pillow_available = False

        if not pillow_available:
            box.separator()
            warning_box = box.box()
            warning_box.alert = True
            warning_box.label(text="⚠️ Pillow not installed", icon='ERROR')
            warning_box.label(text="Texture resizing unavailable")
            warning_box.operator("agr.install_pillow", text="Install Pillow", icon='IMPORT')

        # Info
        if context.active_object:
            box.label(text=f"Active: {context.active_object.name}", icon='OBJECT_DATA')
            if context.active_object.active_material:
                box.label(text=f"Material: {context.active_object.active_material.name}", icon='MATERIAL')
            mat_count = len([slot for slot in context.active_object.material_slots if slot.material])
            if mat_count > 0:
                box.label(text=f"Materials: {mat_count}", icon='MATERIAL_DATA')
            if len(context.selected_objects) > 1:
                box.label(text=f"Sources: {len(context.selected_objects) - 1} objects", icon='OUTLINER_OB_MESH')


class AGR_PT_TextureSetsPanel(Panel):
    """Texture sets management panel"""
    bl_label = "Texture Sets"
    bl_idname = "AGR_PT_texture_sets_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        # Refresh button
        layout.operator("agr.refresh_texture_sets", text="Refresh Sets", icon='FILE_REFRESH')

        # Texture sets list
        box = layout.box()

        # Count selected
        texture_sets = context.scene.agr_texture_sets
        sets_count = len(texture_sets)
        selected_count = sum(1 for ts in texture_sets if ts.is_selected)
        header_text = f"Sets ({sets_count})"
        if selected_count > 0:
            header_text += f"  |  Selected: {selected_count}"

        # Collapsible list header
        list_header = box.row()
        list_header.prop(settings, "show_sets_list",
                         icon='TRIA_DOWN' if settings.show_sets_list else 'TRIA_RIGHT',
                         text=header_text, emboss=False)

        if settings.show_sets_list:
            if sets_count > 0:
                box.template_list(
                    "AGR_UL_TextureSetsList", "",
                    context.scene, "agr_texture_sets",
                    context.scene, "agr_texture_sets_index",
                    rows=5
                )
            else:
                box.label(text="No texture sets found", icon='INFO')
                box.label(text="Bake textures or refresh list")

        # --- Sort & Select (collapsible, hidden if no sets) ---
        if sets_count > 0:
            sort_header = box.row()
            sort_header.prop(settings, "show_sort_select",
                             icon='TRIA_DOWN' if settings.show_sort_select else 'TRIA_RIGHT',
                             text="Sort & Select", emboss=False)

            if settings.show_sort_select:
                sort_box = box.box()

                # Sort buttons
                sort_row = sort_box.row(align=True)
                sort_row.label(text="Sort:")
                sort_row.operator("agr.sort_sets_by_name", text="Name", depress=(settings.sets_sort_mode == 'NAME'))
                sort_row.operator("agr.sort_sets_by_resolution", text="Res", depress=(settings.sets_sort_mode == 'RESOLUTION'))
                sort_row.operator("agr.sort_sets_by_alpha", text="Alpha", depress=(settings.sets_sort_mode == 'ALPHA'))

                # Selection buttons
                sel_row = sort_box.row(align=True)
                sel_row.label(text="Select:")
                op = sel_row.operator("agr.select_all_sets", text="All")
                op.action = 'SELECT'
                op = sel_row.operator("agr.select_all_sets", text="None")
                op.action = 'DESELECT'

                sel_row2 = sort_box.row(align=True)
                sel_row2.operator("agr.select_sets_with_alpha", text="With Alpha", icon='IMAGE_ALPHA')
                sel_row2.operator("agr.select_sets_by_resolution", text="By Res", icon='TEXTURE')
                sel_row2.operator("agr.select_sets_with_frame", text="Frame", icon='IMAGE_PLANE')

                sel_row3 = sort_box.row(align=True)
                sel_row3.operator("agr.select_sets_for_object", text="For Active", icon='OBJECT_DATA')
                sel_row3.operator("agr.select_set_for_active_material", text="For Active Mat", icon='MATERIAL')

        # --- Batch Operations (collapsible, hidden if no sets) ---
        if sets_count > 0:
            batch_header = box.row()
            batch_header.prop(settings, "show_batch_ops",
                              icon='TRIA_DOWN' if settings.show_batch_ops else 'TRIA_RIGHT',
                              text="Batch Operations", emboss=False)

            if settings.show_batch_ops:
                batch_box = box.box()

                # Connect and assign (most frequent)
                batch_box.operator("agr.connect_set_to_material", text="Connect to Materials", icon='LINKED')

                if context.active_object and context.active_object.type == 'MESH':
                    batch_box.operator("agr.assign_set_to_active", text="Assign to Active Object", icon='OBJECT_DATA')

                batch_box.separator()

                # Resize, blur, frame
                batch_box.operator("agr.resize_texture_set", text="Resize Selected Sets", icon='IMAGE_DATA')
                batch_box.operator("agr.gaussian_blur_set", text="Gaussian Blur on Selected", icon='BRUSH_DATA')

                batch_box.separator()

                batch_box.operator("agr.create_frame_on_sets", text="Create Frame on Selected", icon='IMAGE_PLANE')
                batch_box.operator("agr.create_frame_on_files", text="Create Frame on Files...", icon='FILEBROWSER')

                # --- Delete Operations (collapsible, nested) ---
                batch_box.separator()
                del_header = batch_box.row()
                del_header.prop(settings, "show_delete_ops",
                                icon='TRIA_DOWN' if settings.show_delete_ops else 'TRIA_RIGHT',
                                text="Delete Operations", emboss=False)

                if settings.show_delete_ops:
                    del_col = batch_box.column(align=True)
                    del_row = del_col.row(align=True)
                    op = del_row.operator("agr.delete_textures_from_selected", text="Del DO", icon='X')
                    op.texture_type = 'DO'
                    op = del_row.operator("agr.delete_textures_from_selected", text="Del ERM", icon='X')
                    op.texture_type = 'ERM'
                    op = del_row.operator("agr.delete_textures_from_selected", text="Del Normal", icon='X')
                    op.texture_type = 'NORMAL'
                    del_col.separator()
                    del_col.operator("agr.delete_selected_sets", text="Delete Selected Sets", icon='TRASH')

        # --- Atlas Operations (collapsible, inside main box) ---
        box.separator()
        atlas_header = box.row()
        atlas_header.prop(settings, "show_atlas_ops",
                          icon='TRIA_DOWN' if settings.show_atlas_ops else 'TRIA_RIGHT',
                          text="Atlas Operations", emboss=False)

        if settings.show_atlas_ops:
            atlas_box = box.box()

            # Atlas settings
            atlas_box.prop(settings, "atlas_size", text="Atlas Size")

            # Preview and create atlas from active object materials
            atlas_box.separator()
            if context.active_object and context.active_object.type == 'MESH' and len(context.active_object.material_slots) > 0:
                atlas_box.operator("agr.preview_atlas_layout_from_object", text="Preview Atlas Layout from Object", icon='HIDE_OFF')
                atlas_box.operator("agr.create_atlas_from_object", text="Create Atlas from Object", icon='OBJECT_DATA')
            else:
                row = atlas_box.row()
                row.enabled = False
                row.operator("agr.preview_atlas_layout_from_object", text="Preview Atlas Layout from Object", icon='HIDE_OFF')
                row = atlas_box.row()
                row.enabled = False
                row.operator("agr.create_atlas_from_object", text="Create Atlas from Object", icon='OBJECT_DATA')

            # Apply atlas to object
            atlas_box.separator()
            if context.active_object and context.active_object.type == 'MESH':
                atlas_box.operator("agr.apply_atlas_to_object", text="Apply Atlas to Object", icon='UV')

            # Unpack atlas to materials
            atlas_box.separator()
            if context.active_object and context.active_object.type == 'MESH' and context.active_object.active_material:
                atlas_box.operator("agr.unpack_atlas_to_materials", text="Unpack Atlas to Materials", icon='LOOP_BACK')
            else:
                row = atlas_box.row()
                row.enabled = False
                row.operator("agr.unpack_atlas_to_materials", text="Unpack Atlas to Materials", icon='LOOP_BACK')

            # --- Atlas from Selected Sets (collapsible, at bottom) ---
            atlas_box.separator()
            sel_atlas_header = atlas_box.row()
            sel_atlas_header.prop(settings, "show_atlas_from_selected",
                                  icon='TRIA_DOWN' if settings.show_atlas_from_selected else 'TRIA_RIGHT',
                                  text="Atlas from Selected Sets", emboss=False)

            if settings.show_atlas_from_selected:
                selected_non_atlas = sum(1 for ts in texture_sets if ts.is_selected and not ts.is_atlas)

                if selected_non_atlas > 0:
                    atlas_box.label(text=f"Selected: {selected_non_atlas} sets", icon='CHECKBOX_HLT')
                    atlas_box.operator("agr.preview_atlas_layout", text="Preview Atlas Layout", icon='HIDE_OFF')
                    atlas_box.operator("agr.create_atlas_only", text="Create Atlas Only", icon='IMAGE_PLANE')

                    if context.active_object:
                        try:
                            from .operators_atlas import process_object_name
                            address, obj_type = process_object_name(context.active_object.name)
                            atlas_box.label(text=f"Active: {obj_type} ({address})", icon='OBJECT_DATA')
                        except Exception:
                            atlas_box.label(text=f"Active: {context.active_object.name}", icon='OBJECT_DATA')
                else:
                    atlas_box.label(text="Select texture sets to create atlas", icon='INFO')

        # --- UDIM Operations (collapsible, inside main box) ---
        box.separator()
        udim_header = box.row()
        udim_header.prop(settings, "show_udim_ops",
                         icon='TRIA_DOWN' if settings.show_udim_ops else 'TRIA_RIGHT',
                         text="UDIM Operations", emboss=False)

        if settings.show_udim_ops:
            udim_box = box.box()

            # UDIM settings
            udim_box.prop(settings, "udim_use_main_directory", text="Use Main Directory")

            udim_box.separator()

            obj = context.active_object
            if obj and obj.type == 'MESH' and obj.name.startswith("SM_"):
                # Check if has UDIM
                has_udim = False
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        for node in slot.material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                if node.image.source == 'TILED':
                                    has_udim = True
                                    break
                    if has_udim:
                        break

                if has_udim:
                    udim_box.operator("agr.add_to_udim", text="Add Sets to UDIM", icon='ADD')
                    udim_box.operator("agr.revert_udim", text="Disassemble UDIM", icon='LOOP_BACK')
                else:
                    udim_box.operator("agr.create_udim", text="Create UDIM Set", icon='UV_DATA')
            else:
                udim_box.label(text="Select SM_* object for UDIM", icon='INFO')
                # Show disabled buttons
                row = udim_box.row()
                row.enabled = False
                row.operator("agr.create_udim", text="Create UDIM Set", icon='UV_DATA')
                row = udim_box.row()
                row.enabled = False
                row.operator("agr.add_to_udim", text="Add Sets to UDIM", icon='ADD')
                row = udim_box.row()
                row.enabled = False
                row.operator("agr.revert_udim", text="Disassemble UDIM", icon='LOOP_BACK')


class AGR_PT_RenamePanel(Panel):
    """AGR Rename panel - with popup dialogs"""
    bl_label = "AGR Rename"
    bl_idname = "AGR_PT_rename_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Address input
        box = layout.box()
        box.label(text="Настройки", icon='SETTINGS')
        box.prop(scene, "agr_rename_address", text="Address")
        box.prop(scene, "agr_rp_project_lowpoly_number", text="Lowpoly Number (4 цифры)")

        layout.separator()

        # Rename operations in compact column
        col = layout.column(align=True)

        # Rename Main Object
        active_obj = context.active_object
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'MESH' and scene.agr_rename_address:
            row.operator("agr.rename_main_object", text="Переименовать основной объект", icon='OBJECT_DATA')
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_main_object", text="Введите Address")
            else:
                row.operator("agr.rename_main_object", text="Выберите MESH объект")

        # Rename Materials
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'MESH':
            obj_name = re.sub(r'\.\d{3}$', '', active_obj.name)
            if re.match(r'^SM_.+?(_\d{3})?_(Main|MainGlass|Ground|GroundGlass|GroundEl|GroundElGlass|Flora)', obj_name):
                row.operator("agr.rename_materials", text="Переименовать материалы объекта", icon='MATERIAL')
            else:
                row.enabled = False
                row.operator("agr.rename_materials", text="Объект не соответствует формату")
        else:
            row.enabled = False
            row.operator("agr.rename_materials", text="Выберите MESH объект")

        # Rename Glass Materials
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'MESH' and scene.agr_rename_address:
            obj_name = re.sub(r'\.\d{3}$', '', active_obj.name)
            if re.match(r'^SM_.+?(_\d{3})?_(MainGlass|GroundGlass|GroundElGlass)', obj_name):
                row.operator("agr.rename_glass_materials", text="Переименовать материалы стекла", icon='MATERIAL')
            else:
                row.enabled = False
                row.operator("agr.rename_glass_materials", text="Только для Glass объектов")
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_glass_materials", text="Введите Address")
            else:
                row.operator("agr.rename_glass_materials", text="Выберите Glass объект")

        # Rename UCX
        row = col.row()
        row.scale_y = 1.4
        selected_count = len([obj for obj in context.selected_objects if obj.type == 'MESH'])

        if scene.agr_rename_address and selected_count > 0:
            row.operator("agr.rename_ucx", text=f"Переименовать в UCX ({selected_count})", icon='MESH_CUBE')
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_ucx", text="Введите Address")
            else:
                row.operator("agr.rename_ucx", text="Выберите объекты для UCX")

        # Rename Textures
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'MESH' and scene.agr_rename_address:
            obj_name = re.sub(r'\.\d{3}$', '', active_obj.name)
            if re.match(r'^SM_.+?(_\d{3})?_(Main|Ground|GroundEl|GroundElGlass|Flora)', obj_name):
                row.operator("agr.rename_textures", text="Переименовать текстуры", icon='TEXTURE')
            else:
                row.enabled = False
                row.operator("agr.rename_textures", text="Только для Main/Ground/GroundEl/Flora")
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_textures", text="Введите Address")
            else:
                row.operator("agr.rename_textures", text="Выберите объект")

        # Rename GEOJSON
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'MESH' and scene.agr_rename_address:
            obj_name = re.sub(r'\.\d{3}$', '', active_obj.name)
            if re.match(r'^SM_.+?(_\d{3})?_(Main|Ground)', obj_name):
                row.operator("agr.rename_geojson", text="Переименовать GEOJSON", icon='FILE_TEXT')
            else:
                row.enabled = False
                row.operator("agr.rename_geojson", text="Только для Main/Ground")
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_geojson", text="Введите Address")
            else:
                row.operator("agr.rename_geojson", text="Выберите объект Main/Ground")

        # Rename Lights
        row = col.row()
        row.scale_y = 1.4
        if active_obj and active_obj.type == 'EMPTY' and scene.agr_rename_address:
            # Check if has child lights
            has_lights = False
            for obj in context.scene.objects:
                if obj.type == 'LIGHT' and obj.parent == active_obj:
                    has_lights = True
                    break

            if has_lights:
                row.operator("agr.rename_lights", text="Переименовать свет", icon='LIGHT')
            else:
                row.enabled = False
                row.operator("agr.rename_lights", text="Empty без источников света")
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_lights", text="Введите Address")
            else:
                row.operator("agr.rename_lights", text="Выберите Empty со светом")

        layout.separator()

        # Rename Project
        box = layout.box()
        box.label(text="Переименование проекта", icon='FILE_FOLDER')

        row = box.row()
        row.scale_y = 2.0
        if scene.agr_rename_address:
            row.operator("agr.rename_project", text="Переименовать ВЕСЬ ПРОЕКТ", icon='ERROR')
        else:
            row.enabled = False
            row.operator("agr.rename_project", text="Введите Address")


class AGR_PT_SettingsPanel(Panel):
    """Settings panel"""
    bl_label = "Settings"
    bl_idname = "AGR_PT_settings_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        box = layout.box()
        box.label(text="Output Settings", icon='FILE_FOLDER')
        box.prop(settings, "output_folder", text="Folder Name")

        # Show full path
        if bpy.path.abspath("//"):
            import os
            full_path = os.path.join(bpy.path.abspath("//"), settings.output_folder)
            box.label(text=f"Path: {full_path}", icon='INFO')
        else:
            box.label(text="Save blend file to see path", icon='ERROR')


classes = (
    AGR_UL_TextureSetsList,
    AGR_PT_MainPanel,
    AGR_PT_TextureSetsPanel,
    AGR_PT_RenamePanel,
    AGR_PT_SettingsPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Register index property for UI list
    bpy.types.Scene.agr_texture_sets_index = bpy.props.IntProperty(default=0)

    print("✅ UI registered")


def unregister():
    del bpy.types.Scene.agr_texture_sets_index

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
