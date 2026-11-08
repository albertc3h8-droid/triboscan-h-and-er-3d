"""Runtime layer: pure data, field and export-agnostic computations.

No module in this package may create a VTK render window or touch a PyVista
``Plotter``.  ``field3d`` uses VTK *computational* filters only and is imported
by both the compute worker and (indirectly) the GUI.
"""
