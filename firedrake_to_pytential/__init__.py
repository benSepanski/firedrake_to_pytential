import pyopencl as cl
cl_ctx = cl.create_some_context()
queue = cl.CommandQueue(cl_ctx)

import numpy as np
from numpy import linalg as la

import firedrake as fd

from meshmode.mesh import MeshElementGroup
from meshmode.discretization import Discretization
from meshmode.discretization.poly_element import \
        InterpolatoryQuadratureSimplexGroupFactory

from pytential import bind
from pytential.qbx import QBXLayerPotentialSource


def _convert_function_space_to_meshmode(function_space, ambient_dim):
    """
    This converts a :class:`FunctionSpace` to a meshmode :class:`Mesh`
    with the given ambient dimension. Creates a 1-1 correspondence of

    firedrake dofs <-> meshmode nodes

    firedrake vertices <-> meshmode vertices

    firedrake faces <-> meshmode faces
    
    etc. Note that the 1-1 correspondence may be, in general, a non-trivial
    re-ordering.

    The resultant :class:`Mesh` will have one group of type
    :class:`SimplexElementGroup`.

    Returns a tuple (:class:`Mesh`, *np.array*). The first entry
    is converted :class:`FunctionSpace`, the second entry *b* is an
    array of floats representing orientations. For the *i*th
    element in the mesh's *.groups[0]* group, its *i*th (group-local)
    element had a positive orientation in :arg:`function_space`
    if *b[i] >= 0* and a negative orientation if *b[i] < 0*.

    :arg function_space: A Firedrake :class:`FunctionSpace`.
        Must be a DG order 1 family with triangle elements with
        topological dimension 2.
    :arg ambient_dim: Must be at least the topological dimension.
    """

    # assert that Function Space is using DG elements of
    # appropriate type
    if function_space.ufl_element().family() != 'Discontinuous Lagrange':
        raise TypeError("""Function space must use Discontinuous
                         Lagrange elements""")

    if function_space.mesh().topological_dimension() != 2:
        raise TypeError("""Only function spaces with meshes of
                        topological dimension 2 are supported""")

    if function_space.finat_element.degree != 1:
        raise TypeError("""Only function spaces with elements of
                        degree 1 are supported""")

    if str(function_space.ufl_cell()) != 'triangle':
        raise TypeError("Only triangle reference elements are supported")

    # assert ambient dimension is big enough
    if ambient_dim < function_space.topological_dimension():
        raise ValueError("""Desired ambient dimension of meshmode Mesh must be at
                            least the topological dimension of the firedrake
                            FunctionSpace""")

    mesh = fd_function_space.mesh()
    mesh.init()
    if ambient_dim is None:
        ambient_dim = mesh.geometric_dimension()
    dim = mesh.topological_dimension()

    # {{{ Construct a SimplexElementGroup
    # Note: not using meshmode.mesh.generate_group
    #       because I need to keep the node ordering
    order = fd_function_space.ufl_element().degree()

    # FIXME: We may want to allow variable dtype
    coords = np.array(mesh.coordinates.dat.data, dtype=np.float64)
    coords_fn_space = mesh.coordinates.function_space()

    # <from meshmode docs:>
    # An array of (nelements, ref_element.nvertices)
    # of (mesh-wide) vertex indices
    vertex_indices = np.copy(coords_fn_space.cell_node_list)

    # get (mesh-wide) vertex coordinates,
    # pytential wants [ambient_dim][nvertices]
    vertices = np.array(coords)
    vertices = vertices.T.copy()
    vertices.resize((ambient_dim,)+vertices.shape[1:])

    # FIXME: Node construction ONLY works for order 1
    #        elements
    #
    # construct the nodes
    # <from meshmode docs:> an array of node coordinates
    # (mesh.ambient_dim, nelements, nunit_nodes)

    node_coordinates = coords
    # give nodes as [nelements][nunit_nodes][dim]
    nodes = [[node_coordinates[inode] for inode in indices]
             for indices in vertex_indices]
    nodes = np.array(nodes)

    # convert to [ambient_dim][nelements][nunit_nodes]
    nodes = np.transpose(nodes, (2, 0, 1))
    if(nodes.shape[0] < ambient_dim):
        nodes = np.resize(nodes, (ambient_dim,) + nodes.shape[1:])
        nodes[-1, :, :] = 0

    # FIXME : This only works for firedrake mesh with
    #         geometric dimension (in meshmode language--ambient dim)
    #         of 2.
    #         (NOTE: for meshes embedded in 3-space,
    #                try using MeshGeoemtry.init_cell_orientations?)
    #
    # Now we ensure that the vertex indices
    # give a positive ordering when
    # SimplexElementGroup.face_vertex_indices is called

    #from meshmode.mesh.io import from_vertices_and_simplices
    #group = from_vertices_and_simplices(vertices, vertex_indices, 1, True)
    if mesh.geometric_dimension() == 2:
        """
        This code is nearly identical to
        meshmode.mesh.io.from_vertices_and_simplices.
        The reason we don't simply call the function is that
        for higher orders (which we hope to eventually use)
        we need to use Firedrake's nodes
        """
        if ambient_dim == 2:
            from meshmode.mesh.generation import make_group_from_vertices

            order_one_grp = make_group_from_vertices(vertices,
                                                     vertex_indices, 1)

            from meshmode.mesh.processing import (
                    find_volume_mesh_element_group_orientation,
                    flip_simplex_element_group)
            # This function fails in ambient_dim 3, hence the
            # separation between ambient_dim 2 and 3
            orient = \
                find_volume_mesh_element_group_orientation(vertices,
                                                           order_one_grp)
        else:
            orient = np.zeros(vertex_indices.shape[0])
            # Just use cross product to tell orientation
            for i, simplex in enumerate(vertex_indices):
                #       v_from
                #        o._
                #       /   ~._
                #      /       ~._
                #     /           ~._
                # va o---------------o vb
                v_from = vertices[:, simplex[0]]
                va = vertices[:, simplex[1]]
                vb = vertices[:, simplex[2]]
                orient[i] = np.cross(va - v_from, vb - v_from)[-1]
    else:
        raise TypeError("Only geometric dimension 2 is supported")

    from meshmode.mesh import SimplexElementGroup
    group = SimplexElementGroup(order, vertex_indices, nodes, dim=2)

    from meshmode.mesh.processing import flip_simplex_element_group
    group = flip_simplex_element_group(vertices,
                                       group,
                                       orient < 0)

    # FIXME : only supports one kind of element, is this OK?
    groups = [group]

    from meshmode.mesh import BTAG_ALL, BTAG_REALLY_ALL
    boundary_tags = [BTAG_ALL, BTAG_REALLY_ALL]

    efacets = mesh.exterior_facets
    if efacets.unique_markers is not None:
        for tag in efacets.unique_markers:
            boundary_tags.append(tag)
    boundary_tags = tuple(boundary_tags)

    # fvi_to_tags maps frozenset(vertex indices) to tags
    fvi_to_tags = {}
    connectivity = fd_function_space.finat_element.cell.\
        connectivity[(dim - 1, 0)]  # maps faces to vertices

    original_vertex_indices_ordering = np.array(
        coords_fn_space.cell_node_list)
    for i, (icell, ifac) in enumerate(zip(
            efacets.facet_cell, efacets.local_facet_number)):
        # unpack arguments
        ifac, = ifac
        icell, = icell
        # record face vertex indices to tag map
        facet_indices = connectivity[ifac]
        fvi = frozenset(original_vertex_indices_ordering[icell]
                        [list(facet_indices)])
        fvi_to_tags.setdefault(fvi, [])
        fvi_to_tags[fvi].append(efacets.markers[i])

    from meshmode.mesh import _compute_facial_adjacency_from_vertices
    facial_adj_grps = _compute_facial_adjacency_from_vertices(
        groups,
        boundary_tags,
        np.int32, np.int8,
        face_vertex_indices_to_tags=fvi_to_tags)

    from meshmode.mesh import Mesh
    return (Mesh(vertices, groups,
                boundary_tags=boundary_tags,
                facial_adjacency_groups=facial_adj_grps), 
           orient)


class FiredrakeMeshmodeConnection:
    """
        This class takes a firedrake :class:`FunctionSpace`, makes a meshmode 
        :class:`Discretization', then converts functions back and forth between 
        the two

        .. attribute:: fd_function_space

            The firedrake :class:`FunctionSpace` object. It must meet
            the requirements as described by :arg:`function_space` in the
            function *_convert_function_space_to_meshmode*

        .. attribute:: _ambient_dim
            
            The ambient dimension of the meshmode mesh which corresponds
            to :attr:`fd_function_space`. Note that to compute layer potentials,
            the co-dimension of the mesh must be 1.

            Example: If one is going to compute 3-dimensional layer potentials
                     on a 2-dimensional mesh, one would want the mesh to 
                     have ambient dimension 3.

            Example: If one is going to compute 2-dimensional layer potentials
                     on the boundary of a 2-dimensional mesh, one would make
                     the mesh with ambient dimension 2, since the boundary
                     would then have codimension 1. 

            We enforce that the mesh associated to :attr:`fd_function_space`
            would have co-dimension 0 or 1 when embedded in a space of dimension
            :attr:`_ambient_dim`. (i.e. 
            ``_ambient_dim - fd_function_space.topological_dimension() = 0, 1``).

        .. attribute:: meshmode_mesh
            
            The meshmode :class:`Mesh` object associated to :attr:`fd_function_space`.
            For more details, see the function *_convert_function_space_to_meshmode*.

        .. attribute:: to_mesh
            
            This is the meshmode mesh which the user wishes to convert :class:`Function` 
            and :class:`Discretization` on.

            Either :attr:`meshmode_mesh` or the interpolation of a :attr:`meshmode_mesh`
            onto the boundary

        .. attribute:: _boundary_id

            An *int* or *None*. If *None*, then :attr:`to_mesh` coincides with
            :attr:`meshmode_mesh`. If an *int*, :attr:`to_mesh` is the result of 
            an interpolation of :attr:`meshmode_mesh` onto the boundary 
            :attr:`_boundary_id`

        .. attribute:: _meshmode_connection

             *None* if :attr:`meshmode_mesh` agrees with :attr:`to_mesh`. Else,
             a :class:`DiscretizationConnection` from :attr:`meshmode_mesh` to
             :attr:`to_mesh`.

        .. attribute:: _orient

            An *np.array* of shape *(meshmode_mesh.groups[0].nelements)* with 
            positive entries for each positively oriented element,
            and a negative entry for each negatively oriented element.

        .. attribute:: meshmode_mesh_qbx

            A :class:`QBXLayerPotentialSource` object
            created from a discretization built from :attr:`meshmode_mesh`
            using an :class:`InterpolatoryQuadratureSimplexGroupFactory`

        .. attribute:: _qbx

            A :class:`QBXLayerPotentialSource` object
            created from a discretization built from :attr:`_to_mesh`
            using an :class:`InterpolatoryQuadratureSimplexGroupFactory`

        .. attribute:: _flip_matrix

            Used to re-orient elements. For more details, look at the
            function *meshmode.mesh.processing.flip_simplex_element_group*.

        .. attribute:: _interpolation_inverse

            If :attr:`to_mesh` agrees with :attr:`meshmode_mesh` is *None*.

            Else, an *np.array* of shape *(to_mesh.nnodes)* such that the
            node with index *i* in :attr:`to_mesh` comes from the node
            at index *_interpolation_inverse[i]* in :attr:`meshmode_mesh`
    """
    
    def __init__(self, function_space, ambient_dim=None, boundary_id=None, thresh=1e-5):
        self._thresh = thresh
        """
        :arg function_space: Sets :attr:`fd_function_space`
        :arg ambient_dim: Sets :attr:`_ambient_dim`. If none, this defaults to
                          *fd_function_space.geometric_dimension()*
        :arg boundary_id: Sets :attr:`_boundary_id`

        :arg thresh: Used as threshold for some float equality checks
                     (specifically, those involved in asserting
                     positive ordering of faces)
        """

        # {{{ Declare attributes

        self.fd_function_space = fd_function_space
        self._ambient_dim = ambient_dim
        self.meshmode_mesh = None
        self.to_mesh = None
        self._boundary_id = boundary_id
        self._meshmode_connection = None
        self._orient = None
        self.meshmode_mesh_qbx = None
        self._qbx = None
        self._flip_matrix = None
        self._interpolation_inverse = None
        
        # }}}

        # {{{ construct meshmode mesh and qbx

        # If ambient dim is unset, set to geometric dimension ofd
        # fd function space
        if self._ambient_dim is None:
            self._ambient_dim = fd_function_space.geometric_dimension()

        # Ensure co-dimension is 0 or 1
        codimension = ambient_dim - function_space.mesh().topological_dimension()
        if codimension not in [0, 1]:
            raise ValueError('Co-dimension is %s, should be 0 or 1' % (codimension))

        # create mesh and qbx
        self.meshmode_mesh, self._orient = \
            _convert_function_space_to_meshmode(self.fd_function_space,
                                                self._ambient_dim)
        pre_density_discr = Discretization(
            cl_ctx,
            self.meshmode_mesh,
            InterpolatoryQuadratureSimplexGroupFactory(1))

        # FIXME : Assumes order 1
        # FIXME : Do I have the right thing for the various orders?
        self._meshmode_qbx = QBXLayerPotentialSource(pre_density_discr,
                                            fine_order=1,
                                            qbx_order=1,
                                            fmm_order=1)
        # }}}

        # {{{ Perform boundary interpolation if required
        if self._boundary_id is None:
            self.to_mesh = self.meshmode_mesh
            self._qbx = self._meshmode_qbx
            self._meshmode_connection = None
        else:
            from meshmode.discretization.connection import \
                make_face_restriction
            # FIXME : Assumes order 1
            self._meshmode_connection = make_face_restriction(
                self._qbx.density_discr,
                InterpolatoryQuadratureSimplexGroupFactory(1),
                self._boundary_id)
            self._qbx = QBXLayerPotentialSource(
                self._meshmode_connection.to_discr,
                fine_order=1,
                qbx_order=1,
                fmm_order=1)

    def _compute_flip_matrix(self):
        # This code adapted from *meshmode.mesh.processing.flip_simplex_element_group*

        if self._flip_matrix is None:
            grp = self.meshmode_mesh.groups[0]
            grp_flip_flags = (self._orient < 0)
            
            from modepy.tools import barycentric_to_unit, unit_to_barycentric

            from meshmode.mesh import SimplexElementGroup

            if not isinstance(grp, SimplexElementGroup):
                raise NotImplementedError("flips only supported on "
                        "exclusively SimplexElementGroup-based meshes")

            # Generate a resampling matrix that corresponds to the
            # first two barycentric coordinates being swapped.

            bary_unit_nodes = unit_to_barycentric(grp.unit_nodes)

            flipped_bary_unit_nodes = bary_unit_nodes.copy()
            flipped_bary_unit_nodes[0, :] = bary_unit_nodes[1, :]
            flipped_bary_unit_nodes[1, :] = bary_unit_nodes[0, :]
            flipped_unit_nodes = barycentric_to_unit(flipped_bary_unit_nodes)

            from modepy import resampling_matrix, simplex_best_available_basis
            flip_matrix = resampling_matrix(
                    simplex_best_available_basis(grp.dim, grp.order),
                    flipped_unit_nodes, grp.unit_nodes)

            flip_matrix[np.abs(flip_matrix) < 1e-15] = 0

            # Flipping twice should be the identity
            assert la.norm(
                    np.dot(flip_matrix, flip_matrix)
                    - np.eye(len(flip_matrix))) < 1e-13

            self._flip_matrix = flip_matrix

    def _reorder_nodes(self, array, invert=False):
        """
        :arg nodes: An array representing function values at each of the
                    dofs
        :arg invert: False if and only if converting firedrake to
                      meshmode ordering, else does meshmode to firedrake
                      ordering
        """
        if self._flip_matrix is None:
            self._compute_flip_matrix()

        flip_mat = self._flip_matrix
        # reorder data (Code adapted from
        # meshmode.mesh.processing.flip_simplex_element_group)
        if not invert:
            fd_mesh = self.fd_function_space.mesh()
            vertex_indices = \
                fd_mesh.coordinates.function_space().cell_node_list
        else:
            vertex_indices = self._meshmode_mesh.vertex_indices
            flip_mat = flip_mat.T

        # obtain function data in form [nelements][nunit_nodes]
        data = nodes[vertex_indices]
        # flip nodes that need to be flipped
        data[self._orient < 0] = np.einsum(
            "ij,ej->ei", flip_mat, data[self._orient < 0])

        # convert from [element][unit_nodes] to
        # global node number
        data = data.T.flatten()
        
        return data

    def __call__(self, queue, weights, invert=False):
        """
            Returns converted weights as an *np.array*
            
            :arg queue: The pyopencl queue
            :arg weights: An *np.array* with the weights
            :arg invert: True iff meshmode to firedrake instead of
                         firedrake to meshmode
        """
        data = None
        # If inverting, and :attr:`to_mesh` is a boundary interpolation,
        # undo the interpolation and store in *data*. Else *data* is
        # just :arg:`weights`.
        if invert and self._meshmode_connection is not None:
            # {{{ Compute interpolation inverse if not already done

            if self._interpolation_inverse is None:
                # indexes has shape (self.meshmode_mesh.nnodes) with 
                # indexes[i] = i
                indexes = np.arange(self.meshmode_mesh.nnodes)
                # put indexes on device and interpolate indexes 
                indexes = cl.array.to_device(queue, indexes)
                self._interpolation_inverse = \
                    self._meshmode_connection(queue, indexes).with_queue(queue)
                # convert back to numpy
                self._interpolation_inverse = \
                    self._interpolation_inverse.get(queue=queue)

            # }}}

            # {{{ Invert the interpolation 
            data = np.zeros(self.meshmode_mesh.nnodes)
            for index, weight in zip(self._interpolation_inverse, weights):
                data[index] = weight
        else:
            data = weights

        # Return the array with the re-ordering applied
        return self._reorder_nodes(data, invert)
