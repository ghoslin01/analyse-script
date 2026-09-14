from itertools import product

import ifp_contract.trace as tracing
from ifp_contract.trace import Trace, overlaps, shape
from ifp_contract.trace_index import TraceIndex


def field(path, group=False, scope='root'):
    return {'scope': scope, 'path': path, 'group': group}


def event(number, *, writes=(), reads=(), kind='set', scope='root', guards=(), triggers=()):
    return {'id': f'e{number}', 'sequence': number, 'node': f'n{number}',
            'kind': kind, 'scope': scope, 'guards': list(guards), 'loops': [],
            'parents': [], 'triggers': list(triggers), 'writes': list(writes),
            'reads': list(reads), 'entry': 'P.Input', 'name': f'event {number}'}


def test_index_preserves_exact_group_array_and_scope_overlap():
    paths = ['X', 'X[1]', 'X[2]', 'X[A]', 'X[C]', 'X[A].Y[1].Value',
             'X[2].Y[A]', 'X[1].Y[2].Value', 'Data Store Root.X[1].Y[1]',
             '!Session.Value', 'XX.Y', 'X.YY', 'X.Y.Value']
    fields = [field(path, group, scope)
              for path, group, scope in product(paths, (True, False), ('root', 'callee'))]
    events = [event(i, writes=[value]) for i, value in enumerate(fields)]
    # A rule can write both a group and one of its fields. It is still one writer.
    events.append(event(len(events), writes=[field('X', True), field('X.Y')]))
    index = TraceIndex(events, shape)
    for read in fields:
        expected = [e['id'] for e in events if any(overlaps(w, read) for w in e['writes'])]
        actual = [e['id'] for e in index.candidates(read)
                  if any(overlaps(w, read) for w in e['writes'])]
        assert actual == expected


def test_slice_does_not_compare_unrelated_fields(monkeypatch, tmp_path):
    trace = Trace(tmp_path)
    trace.events = [event(i, writes=[field(f'Unrelated{i}.Value')]) for i in range(2000)]
    trace.events += [event(2000, writes=[field('Source.Value')]),
                     event(2001, writes=[field('Result')], reads=[field('Source.Value')])]
    trace.nodes = {e['node']: {'file': 'A.ifp', 'line': e['sequence'] + 1}
                   for e in trace.events}
    calls = 0
    original = tracing.overlaps

    def counted(left, right):
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr(tracing, 'overlaps', counted)
    data = trace.slice(['e2001'], 'Result')
    assert [e['id'] for e in data['events']] == ['e2000', 'e2001']
    assert len(data['edges']) == 1
    assert calls < 10  # A deterministic complexity check, independent of CPU speed.


def test_slice_keeps_conditional_cross_event_and_overwrite_order(tmp_path):
    trace = Trace(tmp_path)
    yes = {'event': 'e0', 'branch': 'True'}
    no = {'event': 'e0', 'branch': 'False'}
    trace.events = [
        event(0, kind='evaluate'),
        event(1, writes=[field('Value')]),
        event(2, writes=[field('Value')]),  # definite barrier hides e1
        event(3, writes=[field('Value')], guards=[yes]),
        event(4, writes=[field('Wrong')], kind='unknown', guards=[no]),
        event(5, reads=[field('Value'), field('Value')], writes=[field('Result')], guards=[yes]),
        event(6, writes=[field('Value')], triggers=['other-control']),
    ]
    trace.nodes = {e['node']: {'file': 'A.ifp', 'line': e['sequence'] + 1}
                   for e in trace.events}
    data = trace.slice(['e5'], 'Result')
    # e3 is a definite writer under this reader's guard; cross-event e6 remains.
    assert [edge['from'] for edge in data['edges'] if edge['to'] == 'e5'] == ['e3', 'e6']
    assert not any(e['id'] in {'e1', 'e2'} for e in data['events'])
    # The opposite-branch opaque rule is irrelevant to e5 but still a possible
    # influence on the guardless cross-event writer e6.
    assert any(e['id'] == 'e4' for e in data['events'])
    assert data['edges'][-1]['event_order'] == 'unknown_between_ui_events'


def test_shared_mapping_keeps_two_concrete_demands_separate(tmp_path):
    trace = Trace(tmp_path)
    trace.events = [
        event(0, writes=[field('Source[1].Value')]),
        event(1, writes=[field('Source[2].Value')]),
        event(2, kind='output_mapping', reads=[field('Source[A].Value')],
              writes=[field('Mapped[A].Value')]),
        event(3, reads=[field('Mapped[1].Value')], writes=[field('Result1')]),
        event(4, reads=[field('Mapped[2].Value')], writes=[field('Result2')]),
    ]
    trace.nodes = {e['node']: {'file': 'A.ifp', 'line': e['sequence'] + 1}
                   for e in trace.events}
    data = trace.slice(['e3', 'e4'], None)
    assert [(edge['from'], edge['field']['path']) for edge in data['edges']
            if edge['to'] == 'e2'] == [('e0', 'Source[1].Value'), ('e1', 'Source[2].Value')]
