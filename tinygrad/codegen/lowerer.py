# the job of the lowerer is to do indexing
import math
from dataclasses import dataclass
from typing import cast
from tinygrad.dtype import dtypes, PtrDType
from tinygrad.uop.ops import KernelInfo, UOp, Ops, PatternMatcher, UPat, sint, sint_to_uop
from tinygrad.renderer import Renderer
from tinygrad.helpers import all_int, prod, partition, flatten, unwrap
from tinygrad.shape.view import get_contraction

# ***** indexing *****
def _group_dims(dims:tuple[sint, ...], max_sizes:tuple[int, ...]):
  # TODO: symbolic shape
  if not all_int(dims): return dims
  while len(dims) > len(max_sizes) or any(d > m for d,m in zip(dims, max_sizes)):
    for i,m in enumerate(max_sizes):
      if i < (len(dims)-1) and dims[i] * dims[i+1] <= m:
        dims = dims[:i] + (dims[i]*dims[i+1],) + dims[i+2:]
        break
    else: return None
  return dims

def _split_dims(dims, max_sizes):
  if all(d <= m for d,m in zip(dims, max_sizes)): return dims
  _dims = list(dims) + [1]*(3-len(dims))
  for i in range(len(_dims)):
    while _dims[i] > max_sizes[i]:
      div = next((d for d in range(2, math.ceil(math.sqrt(_dims[i])) + 1) if (_dims[i] % d) == 0), 1)
      if div == 1: raise RuntimeError(f"cannot limit dim {dims=}, {max_sizes=}")
      _dims[i], _dims[(i+1)%len(_dims)] = _dims[i]//div, _dims[(i+1)%len(_dims)]*div
  return tuple(_dims[:2] if _dims[2] == 1 else _dims[0] if _dims[1:3] == [1,1] else _dims)

def get_grouped_dims(prefix, dims:tuple[sint, ...], max_sizes:tuple[int, ...]|None, reverse=False) -> list[UOp]:
  if reverse: dims = dims[::-1]
  # try to group first: (a, b, c, d) -> (ab, c, d)
  limited = (grouped if (grouped := _group_dims(dims, max_sizes)) else dims) if max_sizes is not None else dims
  # check if grouping failed
  if max_sizes is not None and len(limited) > len(max_sizes): raise RuntimeError(f"cannot limit dim {dims=}, {max_sizes=}")
  # try to split up dims: (a,) -> (b, c)
  if limited == dims: limited = _split_dims(dims, max_sizes) if max_sizes is not None else dims
  ret = raw_idxs = [UOp(Ops.SPECIAL, dtypes.int, (), (f"{prefix}{i}", s)) for i,s in enumerate(limited)]
  if len(limited) < len(dims):
    ret = []
    if (contraction:=get_contraction(dims, limited)) is None: raise AssertionError(f"get_contraction should not be None {dims=} {limited=}")
    for idx, contraction_group in zip(raw_idxs, contraction):
      for c in contraction_group[:-1]:
        ret.append(idx % dims[c])
        idx //= dims[c]
      ret.append(idx)
  elif len(limited) > len(dims):
    a, b = len(limited), len(dims)
    if a == 2 and b == 1: ret = [raw_idxs[0] * limited[1] + raw_idxs[1]]
    if a == 3 and b == 1: ret = [raw_idxs[0] * (limited[1] * limited[2]) + raw_idxs[1] * limited[2] + raw_idxs[2]]
    if a == 3 and b == 2: ret = [raw_idxs[0] * limited[1] + raw_idxs[1], raw_idxs[2]]
  return ret[::-1] if reverse else ret

@dataclass
class IndexContext:
  idxs: list[UOp]
  ridxs: list[UOp]

def get_index(ast:UOp, opts:Renderer) -> IndexContext:
  ki = ast.arg if isinstance(ast.arg, KernelInfo) else KernelInfo()
  # NOTE: assumes the shape is <global dims> <local dims> <group_for_reduces> <reduces> <upcasts/unrolls>
  full_shape = ast.full_shape
  first_upcasted = len(full_shape)-ki.upcasted
  # if there's no reduce, this is first_upcasted. assumes reduces are at the end
  first_reduce = min([first_upcasted]+flatten(x.axis_arg for x in ast.toposort() if x.op is Ops.REDUCE_AXIS))
  local_loads = [x for x in ast.toposort() if x.op is Ops.LOAD and x.src[0].base.op is Ops.DEFINE_LOCAL]
  # NOTE: sum up the reduced axes looking across all local loads, yields the number of grouped reduces
  group_for_reduces = sum([any(l.st_arg.shape[i]!=ast.src[0].st_arg.shape[i] for l in local_loads) for i in range(first_reduce,first_upcasted)])
  global_dims = first_reduce-ki.local_dims

  if opts.has_local:
    if ki.dont_use_locals:
      assert ki.local_dims == 0, "can't use locals if there's no local dims"
      idxs = get_grouped_dims("idx", full_shape[:global_dims], opts.global_max, reverse=True)
    else:
      # define indexes for GPU-like execution
      idxs = get_grouped_dims("gidx", full_shape[:global_dims], opts.global_max, reverse=True) + \
             get_grouped_dims("lidx", full_shape[global_dims:first_reduce+group_for_reduces], opts.local_max)
  else:
    # all loops are RANGES
    idxs = [UOp(Ops.RANGE, dtypes.int, (sint_to_uop(g),), i) for i,g in enumerate(full_shape[:first_reduce])]

  # reduce loops
  idxs += [UOp(Ops.RANGE, dtypes.int, (sint_to_uop(g),), i)
    for i,g in enumerate(full_shape[first_reduce+group_for_reduces:first_upcasted], start=first_reduce+group_for_reduces)]

  # upcast loops
  for i,g in enumerate(full_shape[first_upcasted:], start=first_upcasted):
    assert isinstance(g, int), "needs to be int to upcast/unroll"
    idxs.append(UOp(Ops.UNROLL, dtypes.int, (UOp.const(dtypes.int.vec(g), tuple(range(g))),), ((i,g),)))

  # late indexes (group for reduce)
  ridxs = idxs[:]
  for a in range(first_reduce, first_reduce+group_for_reduces):
    ridxs[a] = UOp(Ops.RANGE, dtypes.int, (sint_to_uop(full_shape[a]),), 1000+a)

  return IndexContext(idxs, ridxs)

# ***** lowering (given index) *****

def lower_reduce_axis(ctx: IndexContext, x: UOp):
  # NOTE: always using ridxs is fine here
  reduce_range, reduce_expand = partition([ctx.ridxs[i] for i in x.axis_arg], lambda y: y.op is Ops.RANGE)
  assert all(x.op is Ops.UNROLL for x in reduce_expand), f"not all UNROLLS in {reduce_expand} for {x.axis_arg}"
  alu_op: Ops = x.arg[0]
  ret = x.src[0]
  if len(contract_axis:=flatten(x.arg for x in reduce_expand)):
    ret = UOp(Ops.CONTRACT, x.dtype.vec(prod(x[1] for x in contract_axis)), (ret,), tuple(contract_axis))
  # REDUCE supports both "horizontal" reduction and range reduction. the horizontal elements are taken in the nearest group
  return UOp(Ops.REDUCE, x.dtype, (ret,)+tuple(reduce_range), alu_op)

def lower_load_store(ctx: IndexContext, x: UOp, buf: UOp):
  idx, valid = x.st_arg.to_indexed_uops(ctx.ridxs if x.op is Ops.LOAD and buf.op is Ops.DEFINE_LOCAL else ctx.idxs)
  if x.op is Ops.LOAD:
    barrier = (UOp(Ops.BARRIER, dtypes.void, (x.src[1],)),) if buf.op is Ops.DEFINE_LOCAL else ()
    return UOp(Ops.LOAD, x.dtype, (buf.index(idx, valid),) + barrier)
  # NOTE: only store the local reduceop in the threads that are actually doing the reduce
  if cast(PtrDType, buf.dtype).local and x.src[1].op is Ops.REDUCE:
    reduce_input = x.src[1].src[0]
    store_back = reduce_input.op is Ops.LOAD and cast(PtrDType, reduce_input.src[0].dtype).local
  else: store_back = False
  # NOTE: If we're storing the reduced value back into each thread, need to zero-out the reduced axes
  if store_back: idx, _ = x.st_arg.to_indexed_uops([u.const_like(0) if u in x.src[1].src else u for u in ctx.idxs])
  if (not cast(PtrDType, buf.dtype).local) or store_back:
    for oidx, ridx in zip(ctx.idxs, ctx.ridxs):
      if oidx is not ridx: valid = valid * oidx.eq(0)
  return UOp(Ops.STORE, dtypes.void, (buf.index(idx, valid), x.src[1]))

def lower_const(x:UOp):
  assert all(v.mask is None for v in unwrap(x.st).views), f"VIEW in CONST/DEFINE_VAR source must be unmasked, got {x.st}"
  return x.replace(src=())

pm_lowerer = PatternMatcher([
  (UPat(Ops.REDUCE_AXIS, name="x"), lower_reduce_axis),
  (UPat((Ops.CONST, Ops.DEFINE_VAR), src=(UPat(Ops.VIEW),), name="x"), lower_const),
  (UPat(Ops.VALID, src=(UPat(Ops.VIEW),), name="x"), lambda ctx,x: x.st_arg.to_indexed_uops(ctx.idxs)[1]),
  # rewrite LOAD/STORE VIEW to LOAD/STORE with indexed
  (UPat((Ops.LOAD, Ops.STORE), src=(UPat.var("buf").view(),), allow_any_len=True, name="x"), lower_load_store),
  (UPat(Ops.INDEX, src=(UPat.var("b"), UPat.var("idx"), UPat.const(dtypes.bool, True))), lambda b, idx: b.index(idx)),
])
