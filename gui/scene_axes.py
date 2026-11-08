"""Relative-coordinate axis annotation for the 3D plots (UI v2, P2).

The scene keeps a thin wireframe on the data box; the tick labels and the axis
titles are 2D text actors whose screen position is recomputed from the camera
at the start of every render.  That keeps the labels horizontal and readable at
any view angle (VTK's own cube axes rotate the labels with the edges and print
the absolute 1.6e+07 coordinates), and it lets the depth axis read 0 at the top
with depth growing downwards even though the display Z axis runs upwards.
"""

from __future__ import annotations

import math

import numpy as np
import vtk


def nice_ticks(span: float, target: int = 3) -> list:
    """Human-friendly tick values between 0 and span (both inclusive)."""
    span = float(span)
    if not math.isfinite(span) or span <= 0.0:
        return [0.0]
    raw = span / max(int(target), 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    step = magnitude
    for multiplier in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= multiplier * magnitude + 1e-12:
            step = multiplier * magnitude
            break
    count = int(math.floor(span / step + 1e-9))
    return [round(index * step, 6) for index in range(count + 1)]


def format_nm(value: float) -> str:
    """Whole nanometres with a thousands separator, like the design."""
    return '{:,.0f}'.format(float(value))


class SceneAxes:
    """Axis frame + 2D labels for one plot renderer, in relative coordinates.

    x_ticks / y_ticks / z_ticks are (world_value, label) pairs: the world value
    places the tick on the box edge, the label is what the viewer reads (so the
    depth axis can be labelled from the top).
    """

    def __init__(self, renderer, bounds, x_ticks, y_ticks, z_ticks,
                 titles=('X (nm)', 'Y (nm)', 'heff (nm)'),
                 ink='#1b3358', line='#8296ae',
                 label_font=10, title_font=11,
                 label_offset=10.0, title_offset=28.0):
        self.renderer = renderer
        self.bounds = tuple(float(value) for value in bounds)
        self.x_ticks = [(float(value), str(text)) for value, text in x_ticks]
        self.y_ticks = [(float(value), str(text)) for value, text in y_ticks]
        self.z_ticks = [(float(value), str(text)) for value, text in z_ticks]
        self.titles = tuple(str(title) for title in titles)
        self.ink = self._rgb(ink)
        self.line = self._rgb(line)
        self.label_font = int(round(label_font))
        self.title_font = int(round(title_font))
        self.label_offset = float(label_offset)
        self.title_offset = float(title_offset)
        self._base = (self.label_font, self.title_font,
                      self.label_offset, self.title_offset)
        self.frame_actor = None
        # P3: cloud mode hides everything above the current depth, so the box
        # wireframe is clipped by a plane that follows the depth slider (same
        # convention as the point-cloud clip in gui/viewer.py) and the depth
        # tick labels above the cut are hidden instead of floating in mid-air.
        self._depth_cut = None
        self._labels = {'x': [], 'y': [], 'z': []}
        self._titles = {}
        self._build_frame()
        self._build_texts()
        self.renderer.AddObserver('StartEvent', self._on_render_start)

    def set_depth_cut(self, z_cut) -> None:
        """Truncate the frame (and its depth labels) at world Z *z_cut*.

        None restores the full box.  The cut is the mirrored display Z of the
        current slice depth, i.e. the same value the point-cloud clipping plane
        uses; everything above it belongs to "deeper than the current slice".
        """
        cut = None if z_cut is None else float(z_cut)
        self._depth_cut = cut
        origin = (float(self.bounds[5]) + 1.0) if cut is None else cut
        if self._frame_clip is not None:
            self._frame_clip.SetOrigin(0.0, 0.0, origin)
            self._frame_clip.Modified()
        self._frame_actor_modified()

    def _frame_actor_modified(self) -> None:
        if self.frame_actor is not None:
            self.frame_actor.GetMapper().Modified()

    def apply_scale(self, factor: float) -> None:
        """Re-size labels and offsets for the current window scale.

        The design frame is 1536 x 992; a larger window needs proportionally
        larger tick text so the annotations keep their designed weight instead
        of shrinking relative to the box.
        """
        factor = max(0.4, min(float(factor), 4.0))
        base_label, base_title, base_label_offset, base_title_offset = self._base
        label = max(8, int(round(base_label * factor)))
        title = max(8, int(round(base_title * factor)))
        self.label_offset = base_label_offset * factor
        self.title_offset = base_title_offset * factor
        if (label, title) == (self.label_font, self.title_font):
            return
        self.label_font, self.title_font = label, title
        for actors in self._labels.values():
            for actor in actors:
                actor.GetTextProperty().SetFontSize(label)
        for actor in self._titles.values():
            actor.GetTextProperty().SetFontSize(title)

    # ------------------------------------------------------------- building
    @staticmethod
    def _rgb(value):
        if isinstance(value, str):
            text = value.lstrip('#')
            if len(text) == 3:
                text = ''.join(part * 2 for part in text)
            return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        return tuple(float(component) for component in value)

    def _build_frame(self):
        x0, x1, y0, y1, z0, z1 = self.bounds
        corners = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                   (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        edges = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7),
                 (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))
        points = vtk.vtkPoints()
        for corner in corners:
            points.InsertNextPoint(*corner)
        lines = vtk.vtkCellArray()
        for start, end in edges:
            lines.InsertNextCell(2)
            lines.InsertCellPoint(start)
            lines.InsertCellPoint(end)
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetLines(lines)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        # Normal (0, 0, -1) keeps the world Z at or below the plane origin, so
        # moving the origin down to the current depth truncates the box.
        self._frame_clip = vtk.vtkPlane()
        self._frame_clip.SetNormal(0.0, 0.0, -1.0)
        self._frame_clip.SetOrigin(0.0, 0.0, self.bounds[5] + 1.0)
        mapper.AddClippingPlane(self._frame_clip)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*self.line)
        prop.SetLineWidth(1.0)
        prop.SetOpacity(0.55)
        prop.SetLighting(False)
        actor.PickableOff()
        self.renderer.AddActor(actor)
        self.frame_actor = actor

    def _text_actor(self, text, size, orientation=0.0, right_aligned=False):
        actor = vtk.vtkTextActor()
        actor.SetInput(text)
        prop = actor.GetTextProperty()
        prop.SetFontSize(size)
        prop.SetFontFamilyToArial()
        prop.SetColor(*self.ink)
        prop.SetBold(False)
        prop.SetItalic(False)
        prop.SetShadow(False)
        if right_aligned:
            prop.SetJustificationToRight()
        else:
            prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        actor.SetTextScaleModeToNone()
        if orientation:
            actor.SetOrientation(float(orientation))
        actor.SetVisibility(False)
        self.renderer.AddActor(actor)
        return actor

    def _build_texts(self):
        for axis, ticks in (('x', self.x_ticks), ('y', self.y_ticks),
                            ('z', self.z_ticks)):
            for _value, text in ticks:
                self._labels[axis].append(
                    self._text_actor(text, self.label_font,
                                     right_aligned=(axis == 'z')))
        self._titles['x'] = self._text_actor(self.titles[0], self.title_font)
        self._titles['y'] = self._text_actor(self.titles[1], self.title_font)
        self._titles['z'] = self._text_actor(self.titles[2], self.title_font,
                                             orientation=90.0)

    # ----------------------------------------------------------- projection
    def _projector(self, width, height):
        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return None
        position = np.asarray(camera.GetPosition(), dtype=float)
        focal = np.asarray(camera.GetFocalPoint(), dtype=float)
        up = np.asarray(camera.GetViewUp(), dtype=float)
        forward = focal - position
        norm = float(np.linalg.norm(forward))
        if norm <= 0.0 or width <= 1.0 or height <= 1.0:
            return None
        forward = forward / norm
        right = np.cross(forward, up)
        right_norm = float(np.linalg.norm(right))
        if right_norm <= 0.0:
            return None
        right = right / right_norm
        true_up = np.cross(right, forward)
        aspect = float(width) / float(height)
        if camera.GetParallelProjection():
            scale = float(camera.GetParallelScale()) or 1.0

            def project(points):
                vector = np.asarray(points, dtype=float) - position
                depth = vector @ forward
                x_ndc = (vector @ right) / (scale * aspect)
                y_ndc = (vector @ true_up) / scale
                pixels = np.column_stack([(x_ndc * 0.5 + 0.5) * width,
                                          (y_ndc * 0.5 + 0.5) * height])
                return pixels, depth

            return project
        tan_y = math.tan(math.radians(float(camera.GetViewAngle())) * 0.5)
        tan_x = tan_y * aspect
        if tan_x <= 0.0:
            return None

        def project(points):
            vector = np.asarray(points, dtype=float) - position
            depth = np.maximum(vector @ forward, 1e-3)
            x_ndc = (vector @ right) / (depth * tan_x)
            y_ndc = (vector @ true_up) / (depth * tan_y)
            pixels = np.column_stack([(x_ndc * 0.5 + 0.5) * width,
                                      (y_ndc * 0.5 + 0.5) * height])
            return pixels, depth

        return project

    @staticmethod
    def _outward(start, end, centre):
        direction = np.asarray(end, dtype=float) - np.asarray(start, dtype=float)
        normal = np.array([-direction[1], direction[0]], dtype=float)
        length = float(np.linalg.norm(normal))
        if length <= 1e-9:
            return np.array([0.0, -1.0])
        normal /= length
        middle = (np.asarray(start, dtype=float) + np.asarray(end, dtype=float)) / 2.0
        if float(np.dot(normal, middle - centre)) < 0.0:
            normal = -normal
        return normal

    # ------------------------------------------------------------- updating
    def update(self):
        width, height = self.renderer.GetSize()
        if width < 8 or height < 8:
            return
        project = self._projector(width, height)
        if project is None:
            return
        x0, x1, y0, y1, z0, z1 = self.bounds
        corners = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]],
                           dtype=float)
        pixels, _depth = project(corners)
        centre = pixels.mean(axis=0)
        margin = 4.0

        def clamp(point):
            return (min(max(float(point[0]), margin), width - margin),
                    min(max(float(point[1]), margin), height - margin))

        # Lowest bottom edge for X, then for Y.
        x_edge = min(((0, 1), (3, 2)),
                     key=lambda pair: pixels[list(pair), 1].mean())
        y_edge = min(((0, 3), (1, 2)),
                     key=lambda pair: pixels[list(pair), 1].mean())
        vertical = min(((0, 4), (1, 5), (2, 6), (3, 7)),
                       key=lambda pair: pixels[pair[0], 0])
        z_foot = (float(pixels[vertical[0], 0]), float(pixels[vertical[0], 1]))

        # Both bottom edges meet at the near corner: titles go there, one row
        # outside the tick labels, shifted a little along the axis (the design
        # puts them under the zero ticks, not in the middle of the edge).
        shared = ({x_edge[0], x_edge[1]} & {y_edge[0], y_edge[1]})
        near = shared.pop() if shared else x_edge[0]

        y_value = y0 if tuple(x_edge) == (0, 1) else y1
        start, end = pixels[x_edge[0]], pixels[x_edge[1]]
        normal = self._outward(start, end, centre)
        unit_x = end - start
        unit_x = unit_x / max(float(np.linalg.norm(unit_x)), 1e-9)
        edge_x = float(np.linalg.norm(end - start))
        for actor, (value, _text) in zip(self._labels['x'], self.x_ticks):
            point, _ = project([[value, y_value, z0]])
            along = float(np.dot(point[0] - start, unit_x))
            along = min(max(along, 7.0), max(edge_x - 7.0, 7.0))
            placed = start + unit_x * along + normal * self.label_offset
            actor.SetPosition(*clamp(placed))
            actor.SetVisibility(True)
        corner = pixels[near]
        direction = pixels[x_edge[1] if x_edge[0] == near else x_edge[0]] - corner
        direction = direction / max(float(np.linalg.norm(direction)), 1e-9)
        middle = corner + normal * self.title_offset + direction * 30.0
        self._titles['x'].SetPosition(*clamp(middle))
        self._titles['x'].SetVisibility(True)

        x_value = x0 if tuple(y_edge) == (0, 3) else x1
        start, end = pixels[y_edge[0]], pixels[y_edge[1]]
        normal = self._outward(start, end, centre)
        unit_y = end - start
        unit_y = unit_y / max(float(np.linalg.norm(unit_y)), 1e-9)
        edge_y = float(np.linalg.norm(end - start))
        for actor, (value, _text) in zip(self._labels['y'], self.y_ticks):
            point, _ = project([[x_value, value, z0]])
            along = float(np.dot(point[0] - start, unit_y))
            along = min(max(along, 7.0), max(edge_y - 7.0, 7.0))
            placed = start + unit_y * along + normal * self.label_offset
            actor.SetPosition(*clamp(placed))
            actor.SetVisibility(True)
        corner = pixels[near]
        direction = pixels[y_edge[1] if y_edge[0] == near else y_edge[0]] - corner
        direction = direction / max(float(np.linalg.norm(direction)), 1e-9)
        middle = corner + normal * self.title_offset + direction * 30.0
        self._titles['y'].SetPosition(*clamp(middle))
        self._titles['y'].SetVisibility(True)

        normal = self._outward(z_foot, (z_foot[0], z_foot[1] + 1.0), centre)
        zx = float(corners[vertical[0]][0])
        zy = float(corners[vertical[0]][1])
        cut = self._depth_cut
        # Depth ticks above the cut sit on frame edges that the clipping plane
        # has already removed; drawing them anyway leaves labels floating over
        # the empty upper half of the box.
        visible_labels = []
        for actor, (value, _text) in zip(self._labels['z'], self.z_ticks):
            if cut is not None and float(value) > cut + 1e-6:
                actor.SetVisibility(False)
                continue
            point, _ = project([[zx, zy, value]])
            actor.SetPosition(*clamp(point[0] + normal * self.label_offset))
            actor.SetVisibility(True)
            visible_labels.append(actor)
        # The rotated title sits further out than the widest tick label, so it
        # never overlaps the digits.  It follows the *visible* part of the
        # vertical edge when the frame is truncated.
        if visible_labels:
            widest = max(len(actor.GetInput() or '') * self.label_font * 0.62
                         for actor in visible_labels)
            label_x = min(actor.GetPosition()[0] for actor in visible_labels)
            title_x = label_x - widest - self.title_font * 0.8 - 2.0
            if cut is None:
                top_y = float(pixels[vertical[1]][1])
            else:
                top_point, _ = project([[zx, zy,
                                        min(float(cut), float(self.bounds[5]))]])
                top_y = float(top_point[0][1])
            middle = np.array([title_x,
                               (float(pixels[vertical[0]][1]) + top_y) * 0.5])
            self._titles['z'].SetPosition(*clamp(middle))
            self._titles['z'].SetVisibility(True)
        else:
            self._titles['z'].SetVisibility(False)

    def _on_render_start(self, _obj=None, _event=None):
        self.update()

    # ------------------------------------------------------------ reporting
    def report(self):
        return {
            'relative': True,
            'bounds': list(self.bounds),
            'x_ticks': [text for _value, text in self.x_ticks],
            'y_ticks': [text for _value, text in self.y_ticks],
            'z_ticks': [text for _value, text in self.z_ticks],
            'titles': list(self.titles),
            'label_font': self.label_font,
            'title_font': self.title_font,
            'label_offset': self.label_offset,
            'title_offset': self.title_offset,
            'ink': '#%02x%02x%02x' % tuple(int(round(c * 255)) for c in self.ink),
            'depth_cut_world': self._depth_cut,
            'z_labels_visible': sum(1 for actor in self._labels['z']
                                    if bool(actor.GetVisibility())),
        }
