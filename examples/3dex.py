import pyopencl as cl

# Note: on WSL it is important to run these commands before
#       any other imports are done. If you are a normal person
#       using just regular ubuntu, you should be able to move
#       these to a more pleasant place no problem
cl_ctx = cl.create_some_context()
queue = cl.CommandQueue(cl_ctx)

# This has been my convention since both firedrake and pytential
# have some similar names. This makes defining bilinear forms really
# unpleasant.
import firedrake as fd
from sumpy.kernel import LaplaceKernel
from pytential import sym

import fd2mm


ambient_dim = 3
degree = 1

fine_order = 4 * degree
# Parameter to tune accuracy of pytential
fmm_order = 5
# This should be (order of convergence = qbx_order + 1)
qbx_order = degree

qbx_kwargs = {'fine_order': fine_order,
              'fmm_order': fmm_order,
              'qbx_order': qbx_order}
with_refinement = True

# Let's compute some layer potentials!
m = fd.Mesh("meshes/ball.msh")
V = fd.FunctionSpace(m, 'DG', degree)
Vdim = fd.VectorFunctionSpace(m, 'DG', degree)

mesh_analog = fd2mm.MeshAnalog(m)
fspace_analog = fd2mm.FunctionSpaceAnalog(cl_ctx, mesh_analog, V)

x, y, z = fd.SpatialCoordinate(m)
r"""
..math:

    \f{1}{4\pi \sqrt{(x-2)^2 + (y-2)^2 + (z-2)^2)}}

i.e. a shift of the fundamental solution
"""
expr = fd.Constant(1 / 4 / fd.pi) * 1 / fd.sqrt(
    (x - 2)**2 + (y - 2)**2 + (z-2)**2)
f = fd.Function(V).interpolate(expr)
gradf = fd.Function(Vdim).interpolate(fd.grad(expr))

# Let's create an operator which plugs in f, \partial_n f
# to Green's formula

qbx_forced_limit = None

sigma = sym.make_sym_vector("sigma", ambient_dim)
op = -(sym.D(LaplaceKernel(ambient_dim),
          sym.var("u"),
          qbx_forced_limit=qbx_forced_limit)
    - sym.S(LaplaceKernel(ambient_dim),
            sym.n_dot(sigma),
            qbx_forced_limit=qbx_forced_limit))

from meshmode.mesh import BTAG_ALL
outer_bdy_id = BTAG_ALL

# Think of this like :mod:`pytential`'s :function:`bind`
pyt_op = fd2mm.fd_bind(cl_ctx, fspace_analog, op, source=(V, outer_bdy_id),
                       target=V, with_refinement=with_refinement,
                       qbx_kwargs=qbx_kwargs)

# Compute the operation and store in g
g = fd.Function(V)
pyt_op(queue, u=f, sigma=gradf, result_function=g)

# Compare with f
fnorm = fd.sqrt(fd.assemble(fd.inner(f, f) * fd.dx))
err = fd.sqrt(fd.assemble(fd.inner(f - g, f - g) * fd.dx))
print("L2 Err=", err)
print("L2 Rel Err=", err / fnorm)
