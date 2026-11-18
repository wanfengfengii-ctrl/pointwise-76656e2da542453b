from dataclasses import dataclass
from labavro.schemas.types import MISSING

class ResolutionError(ValueError):
    pass

PROMOTIONS = {'int': ('long', 'float', 'double'),
              'long': ('float', 'double'), 'float': ('double',)}

@dataclass(frozen=True)
class FieldAction:
    writer_field: object
    reader_field: object
    plan: object

@dataclass(eq=False)
class ResolutionPlan:
    writer: object
    reader: object
    operation: str = 'error'
    actions: tuple = ()
    defaults: tuple = ()
    branches: tuple = ()
    child: object = None
    branch_index: int = -1
    reason: str = 'incompatible schemas'

def _named(w, r):
    return w.name == r.name or w.name in r.aliases

def _field_pairs(w, r):
    used, pairs, defaults = set(), {}, []
    for rf in r.fields:
        direct = [wf for wf in w.fields if wf.name == rf.name]
        candidates = direct or [wf for wf in w.fields if wf.name in rf.aliases]
        if len(candidates) > 1:
            raise ResolutionError('ambiguous reader alias: ' + rf.name)
        if not candidates:
            if rf.default is MISSING:
                raise ResolutionError('missing required field: ' + rf.name)
            defaults.append(rf)
        else:
            wf = candidates[0]
            if wf.name in used:
                raise ResolutionError('writer field claimed twice: ' + wf.name)
            used.add(wf.name)
            pairs[wf.name] = rf
    return pairs, tuple(defaults)

def compatible(w, r, active=None):
    active = set() if active is None else active
    key = (id(w), id(r))
    if key in active:
        return True
    active = active | {key}
    if w.kind == 'union':
        return any(compatible(b, r, active) for b in w.branches)
    if r.kind == 'union':
        return any(compatible(w, b, active) for b in r.branches)
    if r.kind in PROMOTIONS.get(w.kind, ()):
        return True
    if w.kind != r.kind:
        return False
    if w.kind in ('record', 'enum', 'fixed') and not _named(w, r):
        return False
    if w.kind == 'fixed':
        return w.size == r.size
    if w.kind == 'record':
        try:
            pairs, _ = _field_pairs(w, r)
        except ResolutionError:
            return False
        return all(compatible(wf.type, pairs[wf.name].type, active)
                   for wf in w.fields if wf.name in pairs)
    if w.kind in ('array', 'map'):
        return compatible(w.items, r.items, active)
    return True

def compile_resolution(writer, reader):
    wroot = getattr(writer, 'root', writer)
    rroot = getattr(reader, 'root', reader)
    cache = {}
    def compile_pair(w, r):
        key = (id(w), id(r))
        if key in cache:
            return cache[key]
        plan = ResolutionPlan(w, r)
        cache[key] = plan
        if w.kind == 'union':
            plan.operation = 'writer_union'
            plan.branches = tuple(compile_pair(b, r) for b in w.branches)
        elif r.kind == 'union':
            for index, branch in enumerate(r.branches):
                if compatible(w, branch):
                    plan.operation = 'reader_union'
                    plan.branch_index = index
                    plan.child = compile_pair(w, branch)
                    break
        elif compatible(w, r):
            plan.operation = w.kind
            if w.kind == 'record':
                pairs, plan.defaults = _field_pairs(w, r)
                plan.actions = tuple(FieldAction(wf, pairs.get(wf.name),
                                     compile_pair(wf.type, pairs[wf.name].type)
                                     if wf.name in pairs else None) for wf in w.fields)
            elif w.kind in ('array', 'map'):
                plan.child = compile_pair(w.items, r.items)
        else:
            plan.reason = w.identity + ' cannot resolve to ' + r.identity
        return plan
    result = compile_pair(wroot, rroot)
    if result.operation == 'error':
        raise ResolutionError(result.reason)
    return result
