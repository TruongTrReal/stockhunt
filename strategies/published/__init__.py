"""One file per published strategy, discovered by `strategies.registry`.

A module here is picked up if it defines `position(df, close, bpy, **params)` and a
non-empty `GRID`. The module's own filename is the strategy's name and therefore the
label every result CSV is keyed on, so renaming a file renames a strategy and orphans
its history.
"""
