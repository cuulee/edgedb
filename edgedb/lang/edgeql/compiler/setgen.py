##
# Copyright (c) 2008-present MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##
"""EdgeQL set compilation functions."""


import copy
import typing

from edgedb.lang.common import parsing

from edgedb.lang.ir import ast as irast
from edgedb.lang.ir import utils as irutils

from edgedb.lang.schema import concepts as s_concepts
from edgedb.lang.schema import expr as s_expr
from edgedb.lang.schema import links as s_links
from edgedb.lang.schema import lproperties as s_linkprops
from edgedb.lang.schema import nodes as s_nodes
from edgedb.lang.schema import pointers as s_pointers
from edgedb.lang.schema import sources as s_sources
from edgedb.lang.schema import types as s_types
from edgedb.lang.schema import utils as s_utils

from edgedb.lang.edgeql import ast as qlast
from edgedb.lang.edgeql import errors
from edgedb.lang.edgeql import parser as qlparser

from . import astutils
from . import context
from . import dispatch
from . import pathctx
from . import schemactx
from . import stmtctx
from . import typegen


PtrDir = s_pointers.PointerDirection


def new_set(*, ctx: context.ContextLevel, **kwargs) -> irast.Set:
    ir_set = irast.Set(**kwargs)
    ctx.all_sets.append(ir_set)
    return ir_set


def compile_path(expr: qlast.Path, *, ctx: context.ContextLevel) -> irast.Set:
    anchors = ctx.anchors

    path_tip = None

    if expr.partial:
        if ctx.partial_path_prefix is not None:
            path_tip = ctx.partial_path_prefix
        else:
            raise errors.EdgeQLError('could not resolve partial path ',
                                     context=expr.context)

    extra_scopes = {}

    for i, step in enumerate(expr.steps):
        if isinstance(step, qlast.Self):
            # 'self' can only appear as the starting path label
            # syntactically and is a known anchor
            path_tip = anchors.get(step.__class__)

        elif isinstance(step, qlast.Subject):
            # '__subject__' can only appear as the starting path label
            # syntactically and is a known anchor
            path_tip = anchors.get(step.__class__)

        elif isinstance(step, qlast.ClassRef):
            if i > 0:
                raise RuntimeError(
                    'unexpected ClassRef as a non-first path item')

            refnode = None

            if not step.module:
                # Check if the starting path label is a known anchor
                refnode = anchors.get(step.name)

            if refnode is not None:
                path_tip = copy.copy(refnode)
            else:
                scls = schemactx.get_schema_object(step, ctx=ctx)

                if (scls.view_type is not None and
                        scls.name not in ctx.view_nodes):
                    # This is a schema-level view, as opposed to
                    # a WITH-block or inline alias view.
                    scls = stmtctx.declare_view_from_schema(scls, ctx=ctx)

                path_tip = class_set(scls, ctx=ctx)
                view_set = ctx.view_sets.get(scls)
                if view_set is not None:
                    path_tip.expr = view_set.expr
                    path_tip.path_id = view_set.path_id
                    if view_set.path_scope is not None:
                        extra_scopes[path_tip] = view_set.path_scope.copy()

                view_scls = ctx.class_view_overrides.get(scls.name)
                if view_scls is not None:
                    path_tip.scls = view_scls

        elif isinstance(step, qlast.Ptr):
            # Pointer traversal step
            ptr_expr = step
            ptr_target = None

            direction = (ptr_expr.direction or
                         s_pointers.PointerDirection.Outbound)
            if ptr_expr.target:
                # ... link [IS Target]
                ptr_target = schemactx.get_schema_object(
                    ptr_expr.target, ctx=ctx)
                if not isinstance(ptr_target, s_concepts.Concept):
                    raise errors.EdgeQLError(
                        f'invalid type filter operand: {ptr_target.name} '
                        f'is not a concept',
                        context=ptr_expr.target.context)

            ptr_name = (ptr_expr.ptr.module, ptr_expr.ptr.name)

            if ptr_expr.type == 'property':
                # Link property reference; the source is the
                # link immediately preceding this step in the path.
                source = path_tip.rptr.ptrcls
            else:
                source = path_tip.scls

            with ctx.newscope(fenced=True, temporary=True) as subctx:
                # We must treat expression Sources like the same
                # way we would treat a reference to a view node representing
                # the same expression.
                path_tip, _ = path_step(
                    path_tip, source, ptr_name, direction, ptr_target,
                    source_context=step.context, ctx=subctx)
                extra_scopes[path_tip] = subctx.path_scope

        else:
            # Arbitrary expression
            if i > 0:
                raise RuntimeError(
                    'unexpected expression as a non-first path item')

            with ctx.newscope(fenced=True, temporary=True) as subctx:
                path_tip = ensure_set(
                    dispatch.compile(step, ctx=subctx), ctx=subctx)

            extra_scopes[path_tip] = subctx.path_scope

    mapped = ctx.view_map.get(path_tip.path_id)
    if mapped is not None:
        path_tip = new_set(
            path_id=mapped.path_id,
            scls=mapped.scls, expr=mapped.expr, ctx=ctx)

    path_tip.context = expr.context
    pathctx.register_set_in_scope(path_tip, ctx=ctx)

    for ir_set, scope in extra_scopes.items():
        if not scope.is_empty():
            node = ctx.path_scope.find_descendant(ir_set.path_id)
            if node:
                node.attach_branch(scope)
                if ir_set.path_scope is None:
                    ir_set.path_scope = node
                elif ir_set.path_scope is scope:
                    ir_set.path_scope = None
    return path_tip


def path_step(
        path_tip: irast.Set, source: s_sources.Source,
        ptr_name: typing.Tuple[str, str],
        direction: PtrDir,
        ptr_target: s_nodes.Node,
        source_context: parsing.ParserContext, *,
        ctx: context.ContextLevel) \
        -> typing.Tuple[irast.Set, s_pointers.Pointer]:

    if isinstance(source, s_types.Tuple):
        if ptr_name[0] is not None:
            el_name = '::'.join(ptr_name)
        else:
            el_name = ptr_name[1]

        if el_name in source.element_types:
            path_id = irutils.tuple_indirection_path_id(
                path_tip.path_id, el_name,
                source.element_types[el_name])
            expr = irast.TupleIndirection(
                expr=path_tip, name=el_name, path_id=path_id,
                context=source_context)
        else:
            raise errors.EdgeQLReferenceError(
                f'{el_name} is not a member of a struct')

        tuple_ind = generated_set(expr, ctx=ctx)
        return tuple_ind, None

    else:
        ptrcls = resolve_ptr(
            source, ptr_name, direction, target=ptr_target, ctx=ctx)

        target = ptrcls.get_far_endpoint(direction)

        mapped = ctx.view_map.get(path_tip.path_id)
        if mapped is not None:
            path_tip = new_set(
                path_id=mapped.path_id,
                scls=mapped.scls, expr=mapped.expr, ctx=ctx)

        path_tip = extend_path(
            path_tip, ptrcls, direction, target, ctx=ctx)

        if ptr_target is not None and target != ptr_target:
            path_tip = class_indirection_set(
                path_tip, ptr_target, optional=False, ctx=ctx)

        return path_tip, ptrcls


def resolve_ptr(
        near_endpoint: irast.Set,
        ptr_name: typing.Tuple[str, str],
        direction: s_pointers.PointerDirection,
        target: typing.Optional[s_nodes.Node]=None, *,
        ctx: context.ContextLevel) -> s_pointers.Pointer:
    ptr_module, ptr_nqname = ptr_name

    if ptr_module:
        pointer = schemactx.get_schema_object(
            name=ptr_nqname, module=ptr_module, ctx=ctx)
        pointer_name = pointer.name
    else:
        pointer_name = ptr_nqname

    ptr = None

    if isinstance(near_endpoint, s_sources.Source):
        ptr = near_endpoint.resolve_pointer(
            ctx.schema,
            pointer_name,
            direction=direction,
            look_in_children=False,
            include_inherited=True,
            far_endpoint=target)
    else:
        if direction == s_pointers.PointerDirection.Outbound:
            bptr = schemactx.get_schema_object(pointer_name, ctx=ctx)
            schema_cls = ctx.schema.get('schema::Atom')
            if bptr.shortname == 'std::__class__':
                ptr = bptr.derive(ctx.schema, near_endpoint, schema_cls)

    if not ptr:
        if isinstance(near_endpoint, s_links.Link):
            path = f'({near_endpoint.shortname})@({pointer_name})'
        else:
            path = f'({near_endpoint.name}).{direction}({pointer_name})'

        if target:
            path += f'[IS {target.name}]'

        raise errors.EdgeQLReferenceError(
            f'{path} does not resolve to any known path')

    return ptr


def extend_path(
        source_set: irast.Set,
        ptrcls: s_pointers.Pointer,
        direction: PtrDir=PtrDir.Outbound,
        target: typing.Optional[s_nodes.Node]=None, *,
        force_computable: bool=False,
        unnest_fence: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    """Return a Set node representing the new path tip."""
    if target is None:
        target = ptrcls.get_far_endpoint(direction)

    if isinstance(ptrcls, s_linkprops.LinkProperty):
        path_id = source_set.path_id.ptr_path().extend(
            ptrcls, direction, target)
    elif direction != s_pointers.PointerDirection.Inbound:
        source = ptrcls.get_near_endpoint(direction)
        if not source_set.scls.issubclass(source):
            # Polymorphic link reference
            source_set = class_indirection_set(
                source_set, source, optional=True, ctx=ctx)

        path_id = source_set.path_id.extend(ptrcls, direction, target)
    else:
        path_id = source_set.path_id.extend(ptrcls, direction, target)

    target_set = new_set(scls=target, path_id=path_id, ctx=ctx)

    ptr = irast.Pointer(
        source=source_set,
        target=target_set,
        ptrcls=ptrcls,
        direction=direction
    )

    target_set.rptr = ptr

    if _is_computable_ptr(ptrcls, force_computable=force_computable, ctx=ctx):
        target_set = computable_ptr_set(
            ptr, unnest_fence=unnest_fence, ctx=ctx)

    return target_set


def _is_computable_ptr(
        ptrcls, *,
        force_computable: bool,
        ctx: context.ContextLevel) -> bool:
    try:
        qlexpr, qlctx = ctx.source_map[ptrcls]
    except KeyError:
        pass
    else:
        return qlexpr is not None

    if ptrcls.is_pure_computable():
        return True

    if force_computable and ptrcls.default is not None:
        return True


def class_indirection_set(
        source_set: irast.Set,
        target_scls: s_nodes.Node, *,
        optional: bool,
        ctx: context.ContextLevel) -> irast.Set:

    poly_set = new_set(scls=target_scls, ctx=ctx)
    rptr = source_set.rptr
    if rptr is not None and not rptr.ptrcls.singular(rptr.direction):
        cardinality = s_links.LinkMapping.ManyToMany
    else:
        cardinality = s_links.LinkMapping.ManyToOne
    poly_set.path_id = irutils.type_indirection_path_id(
        source_set.path_id, target_scls, optional=optional,
        cardinality=cardinality)

    ptr = irast.Pointer(
        source=source_set,
        target=poly_set,
        ptrcls=poly_set.path_id.rptr(),
        direction=poly_set.path_id.rptr_dir()
    )

    poly_set.rptr = ptr

    return poly_set


def class_set(
        scls: s_nodes.Node, *, ctx: context.ContextLevel) -> irast.Set:
    path_id = pathctx.get_path_id(scls, ctx=ctx)
    return new_set(path_id=path_id, scls=scls, ctx=ctx)


def generated_set(
        expr: irast.Base, path_id: typing.Optional[irast.PathId]=None, *,
        typehint: typing.Optional[s_types.Type]=None,
        ctx: context.ContextLevel) -> irast.Set:
    if typehint is not None:
        ql_typeref = s_utils.typeref_to_ast(typehint)
        ir_typeref = typegen.ql_typeref_to_ir_typeref(ql_typeref, ctx=ctx)
    else:
        ir_typeref = None

    alias = ctx.aliases.get('expr')
    ir_set = irutils.new_expression_set(
        expr, ctx.schema, path_id, alias=alias, typehint=ir_typeref)
    ctx.all_sets.append(ir_set)

    return ir_set


def scoped_set(
        expr: irast.Base, *,
        typehint: typing.Optional[s_types.Type]=None,
        path_id: typing.Optional[irast.PathId]=None,
        ctx: context.ContextLevel) -> irast.Set:
    ir_set = ensure_set(expr, typehint=typehint, path_id=path_id, ctx=ctx)
    if ir_set.path_scope is None:
        ir_set.path_scope = ctx.path_scope
    return ir_set


def ensure_set(
        expr: irast.Base, *,
        typehint: typing.Optional[s_types.Type]=None,
        path_id: typing.Optional[irast.PathId]=None,
        ctx: context.ContextLevel) -> irast.Set:
    if not isinstance(expr, irast.Set):
        expr = generated_set(expr, typehint=typehint, path_id=path_id, ctx=ctx)

    if (isinstance(expr, irast.EmptySet) and expr.scls is None and
            typehint is not None):
        irutils.amend_empty_set_type(expr, typehint, schema=ctx.schema)

    if typehint is not None and not expr.scls.issubclass(typehint):
        raise errors.EdgeQLError(
            f'expecting expression of type {typehint.name}, '
            f'got {expr.scls.name}',
            context=expr.context
        )
    return expr


def ensure_stmt(expr: irast.Base, *, ctx: context.ContextLevel) -> irast.Stmt:
    if not isinstance(expr, irast.Stmt):
        expr = irast.SelectStmt(
            result=ensure_set(expr, ctx=ctx)
        )
    return expr


def computable_ptr_set(
        rptr: irast.Pointer, *,
        unnest_fence: bool=False,
        ctx: context.ContextLevel) -> irast.Set:
    """Return ir.Set for a pointer defined as a computable."""
    ptrcls = rptr.ptrcls

    # Must use an entirely separate context, as the computable
    # expression is totally independent from the surrounding query.
    subctx = stmtctx.init_context(schema=ctx.schema)
    self_ = rptr.source
    source_scls = self_.scls
    # process_view() may generate computable pointer expressions
    # in the form "self.linkname".  To prevent infinite recursion,
    # self must resolve to view base type, NOT the view type itself.
    if source_scls.is_view():
        self_ = copy.copy(self_)
        self_.scls = source_scls.peel_view()
        self_.shape = []

    subctx.anchors[qlast.Self] = self_

    subctx.aliases = ctx.aliases
    subctx.stmt = ctx.stmt
    subctx.view_scls = ptrcls.target
    subctx.view_rptr = context.ViewRPtr(source_scls, ptrcls, rptr=rptr)
    subctx.toplevel_stmt = ctx.toplevel_stmt
    subctx.path_scope = ctx.path_scope
    subctx.class_shapes = ctx.class_shapes.copy()
    subctx.all_sets = ctx.all_sets

    if isinstance(ptrcls, s_linkprops.LinkProperty):
        source_path_id = rptr.source.path_id.ptr_path()
    else:
        source_path_id = rptr.target.path_id.src_path()

    path_id = source_path_id.extend(
        ptrcls, s_pointers.PointerDirection.Outbound, ptrcls.target)

    subctx.path_scope.contain_path(path_id)

    try:
        qlexpr, qlctx = ctx.source_map[ptrcls]
    except KeyError:
        if not ptrcls.default:
            raise ValueError(
                f'{ptrcls.shortname!r} is not a computable pointer')

        if isinstance(ptrcls.default, s_expr.ExpressionText):
            qlexpr = astutils.ensure_qlstmt(qlparser.parse(ptrcls.default))
        else:
            qlexpr = qlast.Constant(value=ptrcls.default)

        qlctx = None
    else:
        subctx.namespaces = qlctx.namespaces.copy()
        subctx.aliased_views = qlctx.aliased_views.new_child()
        if source_scls.is_view():
            subctx.aliased_views[self_.scls.name] = None
        subctx.source_map = qlctx.source_map.copy()
        subctx.view_nodes = qlctx.view_nodes.copy()
        subctx.view_sets = qlctx.view_sets.copy()
        subctx.view_map = qlctx.view_map.new_child()
        subctx.singletons = qlctx.singletons.copy()
        subctx.class_shapes = qlctx.class_shapes.copy()
        subctx.path_id_namespace = qlctx.path_id_namespace

    if qlctx is None:
        # This is a schema-level computable expression, put all
        # class refs into a separate namespace.
        subctx.path_id_namespace = (subctx.aliases.get('ns'),)
    else:
        subctx.path_scope = ctx.path_scope.add_fence()
        subctx.path_id_namespace = (
            subctx.path_id_namespace +
            (irast.WeakNamespace(ctx.aliases.get('ns')),))
        subctx.path_scope.namespaces.add(subctx.path_id_namespace[-1])

        inner_path_id = pathctx.get_path_id(self_.scls, ctx=subctx)
        subctx.view_map[inner_path_id] = rptr.source

    if isinstance(qlexpr, qlast.Statement) and unnest_fence:
        subctx.stmt_metadata[qlexpr] = context.StatementMetadata(
            is_unnest_fence=True)

    comp_ir_set = dispatch.compile(qlexpr, ctx=subctx)
    comp_ir_set.scls = ptrcls.target
    comp_ir_set.path_id = path_id
    comp_ir_set.rptr = rptr

    rptr.target = comp_ir_set

    return comp_ir_set
