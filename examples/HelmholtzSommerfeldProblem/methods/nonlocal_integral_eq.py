from firedrake import Function, FacetNormal, TestFunction, assemble, inner, ds, \
    TrialFunction, grad, dx, Constant
from firedrake.petsc import PETSc, OptionsManager
from sumpy.kernel import HelmholtzKernel
from .preconditioners.two_D_helmholtz import AMGTransmissionPreconditioner

import fd2mm


def nonlocal_integral_eq(mesh, scatterer_bdy_id, outer_bdy_id, wave_number,
                         options_prefix=None, solver_parameters=None,
                         fspace=None, vfspace=None,
                         true_sol_grad=None,
                         queue=None, fspace_analog=None, qbx_kwargs=None,
                         ):
    r"""
        see run_method for descriptions of unlisted args

        args:

        :arg queue: A command queue for the computing context

        gamma and beta are used to precondition
        with the following equation:

        \Delta u - \kappa^2 \gamma u = 0
        (\partial_n - i\kappa\beta) u |_\Sigma = 0
    """
    with_refinement = True
    # away from the excluded region, but firedrake and meshmode point
    # into
    pyt_inner_normal_sign = -1

    ambient_dim = mesh.geometric_dimension()

    # {{{ Create operator
    from pytential import sym

    r"""
    ..math:

    x \in \Sigma

    grad_op(x) =
        \nabla(
            \int_\Gamma(
                u(y) \partial_n H_0^{(1)}(\kappa |x - y|)
            )d\gamma(y)
        )
    """
    grad_op = pyt_inner_normal_sign * sym.grad(
        ambient_dim, sym.D(HelmholtzKernel(ambient_dim),
                           sym.var("u"), k=sym.var("k"),
                           qbx_forced_limit=None))

    r"""
    ..math:

    x \in \Sigma

    op(x) =
        i \kappa \cdot
        \int_\Gamma(
            u(y) \partial_n H_0^{(1)}(\kappa |x - y|)
        )d\gamma(y)
    """
    op = pyt_inner_normal_sign * 1j * sym.var("k") * (
        sym.D(HelmholtzKernel(ambient_dim),
              sym.var("u"), k=sym.var("k"),
              qbx_forced_limit=None)
        )

    pyt_grad_op = fd2mm.fd_bind(queue.context, fspace_analog, grad_op,
                                source=(fspace, scatterer_bdy_id),
                                target=(vfspace, outer_bdy_id),
                                with_refinement=with_refinement,
                                qbx_kwargs=qbx_kwargs,
                                )

    pyt_op = fd2mm.fd_bind(queue.context, fspace_analog, op,
                           source=(fspace, scatterer_bdy_id),
                           target=(fspace, outer_bdy_id),
                           with_refinement=with_refinement,
                           qbx_kwargs=qbx_kwargs,
                           )

    # }}}

    class MatrixFreeB(object):
        def __init__(self, A, pyt_grad_op, pyt_op, queue, kappa):
            """
            :arg kappa: The wave number
            """

            self.queue = queue
            self.k = kappa
            self.pyt_op = pyt_op
            self.pyt_grad_op = pyt_grad_op
            self.A = A

            # {{{ Create some functions needed for multing
            self.x_fntn = Function(fspace)

            self.potential_int = Function(fspace)
            self.potential_int.dat.data[:] = 0.0
            self.grad_potential_int = Function(vfspace)
            self.grad_potential_int.dat.data[:] = 0.0
            self.pyt_result = Function(fspace)

            self.n = FacetNormal(mesh)
            self.v = TestFunction(fspace)
            # }}}

        def mult(self, mat, x, y):
            # Perform pytential operation
            self.x_fntn.dat.data[:] = x[:]

            self.pyt_op(self.queue, self.potential_int,
                        u=self.x_fntn, k=self.k)
            self.pyt_grad_op(self.queue, self.grad_potential_int,
                             u=self.x_fntn, k=self.k)

            # Integrate the potential
            r"""
            Compute the inner products using firedrake. Note this
            will be subtracted later, hence appears off by a sign.

            .. math::

                \langle
                    n(x) \cdot \nabla(
                        \int_\Gamma(
                            u(y) \partial_n H_0^{(1)}(\kappa |x - y|)
                        )d\gamma(y)
                    ), v
                \rangle_\Sigma
                - \langle
                    i \kappa \cdot
                    \int_\Gamma(
                        u(y) \partial_n H_0^{(1)}(\kappa |x - y|)
                    )d\gamma(y), v
                \rangle_\Sigma
            """
            self.pyt_result = assemble(
                inner(inner(self.grad_potential_int, self.n),
                      self.v) * ds(outer_bdy_id)
                - inner(self.potential_int, self.v) * ds(outer_bdy_id)
            )

            # y <- Ax - evaluated potential
            self.A.mult(x, y)
            with self.pyt_result.dat.vec_ro as ep:
                y.axpy(-1, ep)

    # {{{ Compute normal helmholtz operator
    u = TrialFunction(fspace)
    v = TestFunction(fspace)

    r"""
    .. math::

        \langle
            \nabla u, \nabla v
        \rangle
        - \kappa^2 \cdot \langle
            u, v
        \rangle
        - i \kappa \langle
            u, v
        \rangle_\Sigma
    """
    a = inner(grad(u), grad(v)) * dx \
        - Constant(wave_number**2) * inner(u, v) * dx \
        - Constant(1j * wave_number) * inner(u, v) * ds(outer_bdy_id)

    # get the concrete matrix from a general bilinear form
    A = assemble(a).M.handle
    # }}}

    # {{{ Setup Python matrix
    B = PETSc.Mat().create()

    # build matrix context
    Bctx = MatrixFreeB(A, pyt_grad_op, pyt_op, queue, wave_number)

    # set up B as same size as A
    B.setSizes(*A.getSizes())

    B.setType(B.Type.PYTHON)
    B.setPythonContext(Bctx)
    B.setUp()
    # }}}

    # {{{ Create rhs

    # Remember f is \partial_n(true_sol)|_\Gamma
    # so we just need to compute \int_\Gamma\partial_n(true_sol) H(x-y)
    from pytential import sym

    sigma = sym.make_sym_vector("sigma", ambient_dim)
    r"""
    ..math:

    x \in \Sigma

    grad_op(x) =
        \nabla(
            \int_\Gamma(
                f(y) H_0^{(1)}(\kappa |x - y|)
            )d\gamma(y)
        )
    """
    grad_op = pyt_inner_normal_sign * \
        sym.grad(ambient_dim, sym.S(HelmholtzKernel(ambient_dim),
                                    sym.n_dot(sigma),
                                    k=sym.var("k"), qbx_forced_limit=None))
    r"""
    ..math:

    x \in \Sigma

    op(x) =
        i \kappa \cdot
        \int_\Gamma(
            f(y) H_0^{(1)}(\kappa |x - y|)
        )d\gamma(y)
        )
    """
    op = 1j * sym.var("k") * pyt_inner_normal_sign * \
        sym.S(HelmholtzKernel(ambient_dim),
              sym.n_dot(sigma),
              k=sym.var("k"),
              qbx_forced_limit=None)

    rhs_grad_op = fd2mm.fd_bind(queue.context, fspace_analog, grad_op,
                                source=(vfspace, scatterer_bdy_id),
                                target=(vfspace, outer_bdy_id),
                                with_refinement=with_refinement,
                                qbx_kwargs=qbx_kwargs,
                                )
    rhs_op = fd2mm.fd_bind(queue.context, fspace_analog, op,
                           source=(vfspace, scatterer_bdy_id),
                           target=(fspace, outer_bdy_id),
                           with_refinement=with_refinement,
                           qbx_kwargs=qbx_kwargs,
                           )

    f_grad_convoluted = Function(vfspace)
    f_convoluted = Function(fspace)
    rhs_grad_op(queue, f_grad_convoluted,
                sigma=true_sol_grad, k=wave_number)
    rhs_op(queue, f_convoluted,
           sigma=true_sol_grad, k=wave_number)

    r"""
        \langle
            f, v
        \rangle_\Gamma
        + \langle
            i \kappa \cdot \int_\Gamma(
                f(y) H_0^{(1)}(\kappa |x - y|)
            )d\gamma(y), v
        \rangle_\Sigma
        - \langle
            n(x) \cdot \nabla(
                \int_\Gamma(
                    f(y) H_0^{(1)}(\kappa |x - y|)
                )d\gamma(y)
            ), v
        \rangle_\Sigma
    """
    rhs_form = inner(inner(true_sol_grad, FacetNormal(mesh)),
                     v) * ds(scatterer_bdy_id) \
        + inner(f_convoluted, v) * ds(outer_bdy_id) \
        - inner(inner(f_grad_convoluted, FacetNormal(mesh)),
                v) * ds(outer_bdy_id)

    rhs = assemble(rhs_form)

    # {{{ set up a solver:
    solution = Function(fspace, name="Computed Solution")

    #       {{{ Used for preconditioning
    if 'gamma' in solver_parameters or 'beta' in solver_parameters:
        gamma = complex(solver_parameters.pop('gamma', 1.0))

        import cmath
        beta = complex(solver_parameters.pop('beta', cmath.sqrt(gamma)))

        p = inner(grad(u), grad(v)) * dx \
            - Constant(wave_number**2 * gamma) * inner(u, v) * dx \
            - Constant(1j * wave_number * beta) * inner(u, v) * ds(outer_bdy_id)
        P = assemble(p).M.handle

    else:
        P = A
    #       }}}

    # Set up options to contain solver parameters:
    ksp = PETSc.KSP().create()
    if solver_parameters['pc_type'] == 'pyamg':
        del solver_parameters['pc_type']  # We are using the AMG preconditioner

        pyamg_tol = solver_parameters.get('pyamg_tol', None)
        if pyamg_tol is not None:
            pyamg_tol = float(pyamg_tol)
        pyamg_maxiter = solver_parameters.get('pyamg_maxiter', None)
        if pyamg_maxiter is not None:
            pyamg_maxiter = int(pyamg_maxiter)
        ksp.setOperators(B)
        ksp.setUp()
        pc = ksp.pc
        pc.setType(pc.Type.PYTHON)
        pc.setPythonContext(AMGTransmissionPreconditioner(wave_number,
                                                          fspace,
                                                          A,
                                                          tol=pyamg_tol,
                                                          maxiter=pyamg_maxiter,
                                                          use_plane_waves=True))
    # Otherwise use regular preconditioner
    else:
        ksp.setOperators(B, P)

    options_manager = OptionsManager(solver_parameters, options_prefix)
    options_manager.set_from_options(ksp)

    with rhs.dat.vec_ro as b:
        with solution.dat.vec as x:
            ksp.solve(b, x)
    # }}}

    return ksp, solution
