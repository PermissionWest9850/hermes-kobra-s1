#!/usr/bin/env python3

import os
import sys

import FreeCAD as App
import Mesh


input_file = os.environ.get("FCAD_INPUT")
output_file = os.environ.get("STL_OUTPUT")

if not input_file or not output_file:
    print("ERROR: FCAD_INPUT and STL_OUTPUT must be set.")
    sys.exit(1)

input_file = os.path.abspath(input_file)
output_file = os.path.abspath(output_file)

if not os.path.isfile(input_file):
    print(f"ERROR: CAD file not found: {input_file}")
    sys.exit(2)

os.makedirs(os.path.dirname(output_file), exist_ok=True)

doc = App.openDocument(input_file)
doc.recompute()

objects = [
    obj
    for obj in doc.Objects
    if hasattr(obj, "Shape") and not obj.Shape.isNull()
]

if not objects:
    print("ERROR: No exportable geometry found.")
    App.closeDocument(doc.Name)
    sys.exit(3)

Mesh.export(objects, output_file)

App.closeDocument(doc.Name)

print(f"STL_OK {output_file}")
