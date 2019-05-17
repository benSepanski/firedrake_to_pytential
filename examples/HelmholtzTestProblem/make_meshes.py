#!/usr/bin/python
from subprocess import call
import math

mesh_file = "domain"
ext = ".geo"
out_folder = "msh_files/"
num_refine = 8
const = 2.0

clmax = [pow(const, -n) for n in range(6, num_refine)]
clmin = [m / 2 for m in clmax]

for mx, mn in zip(clmax, clmin):
    out_name = out_folder
    out_name += mesh_file + "--max:%f,min%f" % (mx, mn)
    out_name = out_name.replace('.', '%')
    out_name += '.msh'

    call(["gmsh", "-2",
          "-clmax", str(mx), "-clmin", str(mn),
          mesh_file + ext, "-o", out_name])